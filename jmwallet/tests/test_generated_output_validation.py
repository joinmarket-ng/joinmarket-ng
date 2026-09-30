"""Known-reuse checks apply to generated outputs, not intentional recipients."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.bitcoin import ParsedTransaction, TxInput, TxOutput, address_to_scriptpubkey

from jmwallet.backends.base import BlockchainBackend
from jmwallet.history import append_history_entry, create_taker_history_entry
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import TransactionSigningError
from jmwallet.wallet.spend import DirectTxOutput, build_and_sign_direct_tx, prepare_direct_send
from jmwallet.wallet.utxo_metadata import UTXOMetadataStore


@pytest.fixture
def output_wallet(tmp_path: Path) -> WalletService:
    backend = MagicMock(spec=BlockchainBackend)
    backend.get_block_height = AsyncMock(return_value=100)
    return WalletService(
        mnemonic="abandon " * 11 + "about",
        backend=backend,
        network="regtest",
        data_dir=tmp_path,
    )


def _tx(scripts: Sequence[bytes]) -> ParsedTransaction:
    return ParsedTransaction(
        version=2,
        inputs=[TxInput.from_hex("aa" * 32, 0)],
        outputs=[TxOutput(value=10_000, script=script) for script in scripts],
        witnesses=[],
        locktime=0,
        has_witness=False,
    )


def _utxo(wallet: WalletService, address: str) -> UTXOInfo:
    return UTXOInfo(
        txid="aa" * 32,
        vout=0,
        value=100_000,
        address=address,
        confirmations=10,
        scriptpubkey=address_to_scriptpubkey(address).hex(),
        path=f"{wallet.root_path}/0'/0/0",
        mixdepth=0,
    )


@pytest.mark.parametrize("source", ["memory", "metadata", "utxo", "utxo_script"])
def test_known_funding_rejects_generated_output(output_wallet: WalletService, source: str) -> None:
    address = output_wallet.get_address(0, 1, 0)
    script = address_to_scriptpubkey(address)
    if source == "memory":
        output_wallet.addresses_with_history.add(address)
    elif source == "metadata":
        assert output_wallet.metadata_store is not None
        output_wallet.metadata_store.mark_address_used(address)
    else:
        utxo = _utxo(output_wallet, address)
        if source == "utxo_script":
            utxo.address = output_wallet.get_address(0, 0, 1)
        output_wallet.utxo_cache[0] = [utxo]

    with pytest.raises(TransactionSigningError, match="known funding history"):
        output_wallet.validate_generated_outputs(_tx([script]), [script])


def test_detached_snapshot_preserves_live_metadata(output_wallet: WalletService) -> None:
    store = output_wallet.metadata_store
    assert store is not None
    address = output_wallet.get_new_internal_address(0)
    script = address_to_scriptpubkey(address)
    outpoint = ("aa" * 32, 0)
    assert output_wallet.reserve_coinjoin_inputs({outpoint}, ttl=300, owner="round")
    records = dict(store.records)
    reservations = dict(store.reserved_records)
    UTXOMetadataStore(store.path).mark_address_used(address)
    before = store.path.read_bytes()

    with pytest.raises(TransactionSigningError, match="known funding history"):
        output_wallet.validate_generated_outputs(_tx([script]), [script])

    assert store.records == records
    assert store.reserved_records == reservations
    assert address not in store.get_used_addresses()
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("unavailable", ["absent", "read_failure", "partial"])
def test_failed_refresh_retains_positive_evidence(
    output_wallet: WalletService, unavailable: str
) -> None:
    store = output_wallet.metadata_store
    assert store is not None
    address = output_wallet.get_address(0, 1, 0)
    store.mark_address_used(address)
    script = address_to_scriptpubkey(address)
    if unavailable == "absent":
        store.path.unlink()
    elif unavailable == "partial":
        store.path.write_text('{"type":"addr"\n')

    failed_read = patch(
        "jmwallet.wallet.utxo_metadata.read_sensitive_file", side_effect=OSError("unavailable")
    )
    with failed_read if unavailable == "read_failure" else nullcontext():
        with pytest.raises(TransactionSigningError, match="known funding history"):
            output_wallet.validate_generated_outputs(_tx([script]), [script])
    assert address in store.get_used_addresses()


def test_reservations_and_pending_history_allow_unbroadcast_retry(
    output_wallet: WalletService,
) -> None:
    address = output_wallet.get_new_internal_address(0)
    script = address_to_scriptpubkey(address)
    entry = create_taker_history_entry(
        maker_nicks=["maker"],
        cj_amount=10_000,
        total_maker_fees=100,
        mining_fee=100,
        destination=address,
        change_address="",
        source_mixdepth=0,
        selected_utxos=[],
        network="regtest",
        wallet_fingerprint=output_wallet.wallet_fingerprint,
    )
    append_history_entry(entry, output_wallet.data_dir)
    assert output_wallet.metadata_store is not None
    before = output_wallet.metadata_store.path.read_bytes()

    for _ in range(2):
        output_wallet.validate_generated_outputs(_tx([script]), [script])
    assert output_wallet.metadata_store.path.read_bytes() == before


@pytest.mark.parametrize("case", ["missing", "duplicate_output", "duplicate_designation"])
def test_generated_outputs_must_be_distinct_and_present(
    output_wallet: WalletService, case: str
) -> None:
    script = address_to_scriptpubkey(output_wallet.get_address(0, 1, 0))
    other = address_to_scriptpubkey(output_wallet.get_address(0, 1, 1))
    outputs = (
        [other]
        if case == "missing"
        else [script, script]
        if case == "duplicate_output"
        else [script]
    )
    designations = [script, script] if case == "duplicate_designation" else [script]
    with pytest.raises(TransactionSigningError):
        output_wallet.validate_generated_outputs(_tx(outputs), designations)


def test_unknown_metadata_does_not_start_recovery(output_wallet: WalletService) -> None:
    store = output_wallet.metadata_store
    assert store is not None
    script = address_to_scriptpubkey(output_wallet.get_address(0, 1, 0))
    assert not store.path.exists()
    with patch.object(output_wallet.backend, "get_block_height", new_callable=AsyncMock) as height:
        output_wallet.validate_generated_outputs(_tx([script]), [script])
        output_wallet.validate_generated_outputs(_tx([script]), [])
        height.assert_not_awaited()
    assert not store.path.exists()


def test_direct_signer_rejects_change_before_any_signature(output_wallet: WalletService) -> None:
    address = output_wallet.get_address(0, 1, 0)
    script = address_to_scriptpubkey(address)
    output_wallet.addresses_with_history.add(address)
    utxo = _utxo(output_wallet, output_wallet.get_address(0, 0, 0))
    with patch.object(output_wallet, "sign_input") as sign:
        with pytest.raises(TransactionSigningError, match="known funding history"):
            build_and_sign_direct_tx(
                wallet=output_wallet,
                utxos=[utxo],
                outputs=[DirectTxOutput(90_000, script, address)],
                locktime=0,
                generated_scripts=[script],
            )
    sign.assert_not_called()


@pytest.mark.parametrize("amount", [0, 50_000])
async def test_direct_send_preserves_intentional_self_payments(
    output_wallet: WalletService, amount: int
) -> None:
    address = output_wallet.get_address(0, 0, 0)
    output_wallet.utxo_cache[0] = [_utxo(output_wallet, address)]
    output_wallet.addresses_with_history.add(address)
    result = await prepare_direct_send(
        wallet=output_wallet,
        backend=output_wallet.backend,
        mixdepth=0,
        amount_sats=amount,
        destination=address,
        fee_rate=1.0,
    )
    assert result.destination == address
    assert bool(result.change_address) == bool(amount)


async def test_reusable_direct_send_checks_generated_change(output_wallet: WalletService) -> None:
    address = output_wallet.get_address(0, 0, 0)
    output_wallet.utxo_cache[0] = [_utxo(output_wallet, address)]
    old_change = output_wallet.get_address(0, 1, 0)
    output_wallet.addresses_with_history.add(old_change)
    with (
        patch.object(output_wallet, "get_new_internal_address", return_value=old_change),
        patch.object(output_wallet, "sign_input") as sign,
    ):
        with pytest.raises(TransactionSigningError, match="known funding history"):
            await prepare_direct_send(
                wallet=output_wallet,
                backend=output_wallet.backend,
                mixdepth=0,
                amount_sats=50_000,
                destination=address,
                fee_rate=1.0,
            )
    sign.assert_not_called()
