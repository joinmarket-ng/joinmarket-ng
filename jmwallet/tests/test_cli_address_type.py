"""The address, info, and send commands must use the configured wallet branch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from bitcointx.wallet import CBitcoinExtKey, P2TRBitcoinSignetAddress, P2WPKHBitcoinSignetAddress
from jmcore.bitcoin import address_to_scriptpubkey
from typer.testing import CliRunner

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.cli import app
from jmwallet.wallet.bip32 import mnemonic_to_seed
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import deserialize_transaction, verify_p2tr_signature
from jmwallet.wallet.spend import DirectTxOutput, build_and_sign_direct_tx

MNEMONIC = "abandon " * 11 + "about"


@pytest.mark.parametrize("address_type,purpose", [("p2wpkh", 84), ("p2tr", 86)])
@pytest.mark.parametrize("command", ["address", "info", "send"])
def test_cli_uses_configured_wallet_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    address_type: str,
    purpose: int,
    command: str,
) -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    mnemonic_file.write_text(MNEMONIC)
    (tmp_path / "config.toml").write_text(
        '[bitcoin]\nbackend_type = "descriptor_wallet"\n'
        '[network_config]\nnetwork = "signet"\n'
        f'[wallet]\nmnemonic_file = "{mnemonic_file}"\n'
        f'address_type = "{address_type}"\n'
    )
    reference = CBitcoinExtKey.from_seed(mnemonic_to_seed(MNEMONIC)).derive_path(
        f"m/{purpose}'/1'/0'/0/0"
    )
    expected_address = str(
        P2TRBitcoinSignetAddress.from_pubkey(reference.pub)
        if address_type == "p2tr"
        else P2WPKHBitcoinSignetAddress.from_pubkey(reference.pub)
    )
    constructed: list[WalletService] = []

    class WalletConstructedError(Exception):
        """Stop before any network access or transaction creation."""

    def construct_wallet(**kwargs: object) -> WalletService:
        wallet = WalletService(**kwargs)
        constructed.append(wallet)
        raise WalletConstructedError

    monkeypatch.setattr("jmwallet.wallet.service.WalletService", construct_wallet)
    monkeypatch.setattr(
        DescriptorWalletBackend, "get_mempool_min_fee", AsyncMock(return_value=None)
    )
    args = [command, "--data-dir", str(tmp_path)]
    if command == "address":
        args += ["new", "0"]
    elif command == "send":
        args += [expected_address, "--amount", "10000", "--fee-rate", "1", "--no-broadcast"]

    result = CliRunner().invoke(app, args)

    assert isinstance(result.exception, WalletConstructedError), result.output
    assert len(constructed) == 1
    wallet = constructed[0]
    assert wallet.address_type == address_type
    assert wallet.get_address(0, 0, 0) == expected_address


def test_taproot_direct_send_signatures_commit_to_all_inputs(tmp_path: Path) -> None:
    wallet = WalletService(
        mnemonic=MNEMONIC,
        backend=MagicMock(),
        network="signet",
        address_type="p2tr",
        data_dir=tmp_path,
    )
    utxos = []
    for index in range(2):
        address = wallet.get_address(0, 0, index)
        utxos.append(
            UTXOInfo(
                txid=f"{index + 1:064x}",
                vout=0,
                value=100_000 + index * 10_000,
                address=address,
                confirmations=3,
                scriptpubkey=address_to_scriptpubkey(address).hex(),
                path=f"m/86'/1'/0'/0/{index}",
                mixdepth=0,
            )
        )
    built = build_and_sign_direct_tx(
        wallet=wallet,
        utxos=utxos,
        outputs=[
            DirectTxOutput(
                address=wallet.get_address(1, 0, 0),
                value_sats=209_000,
                script_pubkey=address_to_scriptpubkey(wallet.get_address(1, 0, 0)),
            )
        ],
        locktime=100,
    )
    transaction = deserialize_transaction(built.raw)
    values = [utxo.value for utxo in built.inputs]
    scripts = [bytes.fromhex(utxo.scriptpubkey) for utxo in built.inputs]
    for index, utxo in enumerate(built.inputs):
        witness = transaction.witnesses[index]
        assert len(witness) == 1 and len(witness[0]) == 64
        output_key = bytes.fromhex(utxo.scriptpubkey)[2:]
        assert verify_p2tr_signature(transaction, index, values, scripts, witness[0], output_key)
        wrong_values = values.copy()
        wrong_values[1 - index] += 1
        assert not verify_p2tr_signature(
            transaction, index, wrong_values, scripts, witness[0], output_key
        )
