"""Regressions for Bech32m destinations in direct-send fee estimation and signing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bitcointx.core import CTransaction
from bitcointx.segwit_addr import encode as encode_segwit_address
from jmcore.cli_common import ResolvedBackendSettings

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.cli.send import _send_transaction
from jmwallet.wallet.address import pubkey_to_p2wpkh_script
from jmwallet.wallet.coin_selection import select_direct_send_utxos
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import (
    create_p2wpkh_script_code,
    deserialize_transaction,
    verify_p2wpkh_signature,
)
from jmwallet.wallet.spend import prepare_direct_send

DESTINATION_PROGRAM = b"\xcd" * 32
DESTINATION = encode_segwit_address("bcrt", 1, DESTINATION_PROGRAM)
assert DESTINATION is not None


@pytest.fixture
def funded_wallet(
    test_mnemonic: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> WalletService:
    """Keep real derivation and signing, with only backend I/O stubbed out."""
    backend = MagicMock(spec=DescriptorWalletBackend)
    backend.get_block_height = AsyncMock(return_value=200)
    backend.get_mempool_min_fee = AsyncMock(return_value=None)
    backend.close = AsyncMock()
    wallet = WalletService(
        mnemonic=test_mnemonic, backend=backend, network="regtest", data_dir=tmp_path
    )
    address = wallet.get_address(0, 0, 0)
    key = wallet.get_key_for_address(address)
    assert key is not None
    wallet.utxo_cache[0] = [
        UTXOInfo(
            txid="ab" * 32,
            vout=0,
            value=100_000,
            address=address,
            confirmations=6,
            scriptpubkey=pubkey_to_p2wpkh_script(
                key.get_public_key_bytes(compressed=True).hex()
            ).hex(),
            path="m/84'/1'/0'/0/0",
            mixdepth=0,
        )
    ]
    monkeypatch.setattr(wallet, "sync_with_registered_bonds", AsyncMock(return_value={}))
    return wallet


@pytest.mark.parametrize(
    ("amount", "expected_fee", "expected_vsize", "expected_change"),
    [(50_000, 153, 153, 49_847), (99_800, 200, 122, 0)],
)
def test_taproot_selection_accounts_for_output_size(
    funded_wallet: WalletService,
    amount: int,
    expected_fee: int,
    expected_vsize: int,
    expected_change: int,
) -> None:
    selection = select_direct_send_utxos(
        funded_wallet.utxo_cache[0], amount, DESTINATION, 1.0, mixdepth=0
    )

    assert selection.fee == expected_fee
    assert selection.vsize == expected_vsize
    assert selection.change_amount == expected_change


def test_taproot_selection_rejects_funds_covering_only_p2wpkh_fee(
    funded_wallet: WalletService,
) -> None:
    # 116 sats covers a P2WPKH recipient (110 vB), but not Taproot (122 vB).
    with pytest.raises(ValueError, match="Insufficient eligible funds"):
        select_direct_send_utxos(funded_wallet.utxo_cache[0], 99_884, DESTINATION, 1.0, mixdepth=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_point", ["cli", "prepare"])
@pytest.mark.parametrize(
    ("amount", "expected_fee", "expected_recipient"),
    [(50_000, 153, 50_000), (0, 122, 99_878), (99_800, 200, 99_800)],
    ids=["change", "sweep", "dust-change"],
)
async def test_signed_taproot_send_pays_requested_rate(
    funded_wallet: WalletService,
    test_mnemonic: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    entry_point: str,
    amount: int,
    expected_fee: int,
    expected_recipient: int,
) -> None:
    if entry_point == "cli":
        settings = ResolvedBackendSettings(
            network="regtest",
            bitcoin_network="regtest",
            backend_type="descriptor_wallet",
            rpc_url="http://127.0.0.1:18443",
            rpc_user="test",
            rpc_password="test",
            neutrino_url="",
            neutrino_add_peers=[],
            data_dir=tmp_path,
            scan_start_height=None,
        )
        with (
            patch(
                "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend",
                return_value=funded_wallet.backend,
            ),
            patch("jmwallet.wallet.service.WalletService", return_value=funded_wallet),
        ):
            await _send_transaction(
                mnemonic=test_mnemonic,
                destination=DESTINATION,
                amount=amount,
                mixdepth=0,
                fee_rate=1.0,
                block_target=None,
                backend_settings=settings,
                broadcast=False,
                skip_confirmation=True,
                interactive_utxo_selection=False,
            )
        tx_hex = capsys.readouterr().out.split("Full hex: ", 1)[1].strip()
    else:
        prepared = await prepare_direct_send(
            wallet=funded_wallet,
            backend=funded_wallet.backend,
            mixdepth=0,
            amount_sats=amount,
            destination=DESTINATION,
            fee_rate=1.0,
            tx_fee_factor=0.0,
        )
        tx_hex = prepared.tx_hex

    raw = bytes.fromhex(tx_hex)
    tx = CTransaction.deserialize(raw)
    fee = 100_000 - sum(output.nValue for output in tx.vout)
    assert fee == expected_fee
    # Independently measure signed vsize with bitcointx, not our fee estimator.
    assert fee / tx.get_virtual_size() >= 1.0
    recipient = next(output for output in tx.vout if bytes(output.scriptPubKey)[0] == 0x51)
    assert bytes(recipient.scriptPubKey) == b"\x51\x20" + DESTINATION_PROGRAM
    assert recipient.nValue == expected_recipient
    change = [output for output in tx.vout if output is not recipient]
    if amount == 50_000:
        assert len(change) == 1
        assert change[0].nValue == 49_847
        assert bytes(change[0].scriptPubKey).startswith(b"\x00\x14")
    else:
        assert change == []

    parsed = deserialize_transaction(raw)
    assert parsed.witnesses is not None
    signature, pubkey = parsed.witnesses[0]
    assert verify_p2wpkh_signature(
        parsed, 0, create_p2wpkh_script_code(pubkey), 100_000, signature, pubkey
    )
