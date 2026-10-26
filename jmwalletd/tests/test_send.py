"""Tests for the jmwalletd direct-send adapter."""

from __future__ import annotations

from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from loguru import logger

from jmwallet.wallet.spend import SignedDirectTx
from jmwalletd.send import do_direct_send


def _make_prepared_tx() -> SignedDirectTx:
    return SignedDirectTx(
        txid="bb" * 32,
        tx_hex="deadbeef",
        fee=150,
        fee_rate=1.0,
        send_amount=50_000,
        change_amount=49_850,
        num_inputs=1,
        num_outputs=2,
        destination="bcrt1qdestination",
        change_address="bcrt1qchange",
        selected_utxos=[("aa" * 32, 0)],
        source_addresses=["bcrt1qinput"],
        inputs=[{"outpoint": "aa" * 32 + ":0"}],
        outputs=[{"value_sats": 50_000, "scriptPubKey": "0014...", "address": "bcrt1qdestination"}],
    )


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast")
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_refreshes_registered_bonds_and_writes_history(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    mock_append_entry: MagicMock,
    mock_update_entry: MagicMock,
) -> None:
    wallet = MagicMock()
    wallet.data_dir = None
    wallet.network = "regtest"
    wallet.wallet_fingerprint = "aabbccdd"
    wallet.sync = AsyncMock()
    wallet.sync_with_registered_bonds = AsyncMock()

    prepared = _make_prepared_tx()
    mock_prepare_direct_send.return_value = prepared

    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(return_value="txid_123")
    mock_get_backend.return_value = backend

    fake_entry = MagicMock()
    mock_create_entry.return_value = fake_entry
    mock_update_entry.return_value = True

    call_order: list[str] = []

    def _on_append(*a: object, **kw: object) -> None:
        call_order.append("append")

    def _on_broadcast(*a: object, **kw: object) -> str:
        call_order.append("broadcast")
        return "txid_123"

    mock_append_entry.side_effect = _on_append
    backend.broadcast_transaction.side_effect = _on_broadcast

    actual = await do_direct_send(
        wallet_service=wallet,
        mixdepth=0,
        amount_sats=50_000,
        destination="bcrt1qdestination",
    )

    assert actual.txid == prepared.txid
    wallet.sync_with_registered_bonds.assert_awaited_once_with()
    wallet.sync.assert_not_awaited()

    # Verify append was called BEFORE broadcast
    assert call_order == ["append", "broadcast"]

    mock_create_entry.assert_called_once_with(
        destination="bcrt1qdestination",
        change_address="bcrt1qchange",
        amount=50_000,
        mining_fee=150,
        source_mixdepth=0,
        selected_utxos=[("aa" * 32, 0)],
        txid="",
        success=False,
        failure_reason="awaiting broadcast",
        network="regtest",
        wallet_fingerprint="aabbccdd",
        source_addresses=["bcrt1qinput"],
    )
    mock_append_entry.assert_called_once_with(fake_entry, data_dir=ANY)
    mock_update_entry.assert_called_once_with(
        fake_entry,
        txid=prepared.txid,
        success=True,
        failure_reason="",
        data_dir=ANY,
    )


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast")
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_records_canonical_destination(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    _mock_append_entry: MagicMock,
    _mock_update_entry: MagicMock,
) -> None:
    """History must match wallet addresses, so an uppercase request is recorded lowercase."""
    wallet = MagicMock()
    wallet.data_dir = None
    wallet.sync_with_registered_bonds = AsyncMock()
    mock_prepare_direct_send.return_value = _make_prepared_tx()
    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(return_value="bb" * 32)
    mock_get_backend.return_value = backend

    await do_direct_send(
        wallet_service=wallet, mixdepth=0, amount_sats=50_000, destination="BCRT1QDESTINATION"
    )

    assert mock_create_entry.call_args.kwargs["destination"] == "bcrt1qdestination"


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast")
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_detail_logs_are_sensitive(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    mock_append_entry: MagicMock,
    mock_update_entry: MagicMock,
) -> None:
    """Amounts, destinations, and transaction identifiers use sensitive records."""
    wallet = MagicMock(data_dir=None, network="regtest", wallet_fingerprint="aabbccdd")
    wallet.sync_with_registered_bonds = AsyncMock()
    prepared = _make_prepared_tx()
    mock_prepare_direct_send.return_value = prepared
    mock_create_entry.return_value = MagicMock()
    mock_update_entry.return_value = True
    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(return_value=prepared.txid)
    mock_get_backend.return_value = backend

    records: list[tuple[str, dict[str, object]]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["message"], dict(message.record["extra"])))
    )
    try:
        await do_direct_send(
            wallet_service=wallet,
            mixdepth=0,
            amount_sats=50_000,
            destination="bcrt1qdestination",
        )
    finally:
        logger.remove(handler_id)

    private_records = [
        record
        for record in records
        if record[0].startswith("Direct send:")
        or record[0].startswith("Broadcasting direct-send transaction")
        or record[0].startswith("Broadcast OK:")
    ]
    assert len(private_records) == 3
    assert all(extra.get("sensitive") is True for _, extra in private_records)


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast")
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_records_broadcast_failure(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    mock_append_entry: MagicMock,
    mock_update_entry: MagicMock,
) -> None:
    wallet = MagicMock()
    wallet.data_dir = None
    wallet.network = "regtest"
    wallet.wallet_fingerprint = "aabbccdd"
    wallet.sync_with_registered_bonds = AsyncMock()

    prepared = _make_prepared_tx()
    mock_prepare_direct_send.return_value = prepared

    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(side_effect=RuntimeError("node connection refused"))
    mock_get_backend.return_value = backend

    fake_entry = MagicMock()
    mock_create_entry.return_value = fake_entry
    mock_update_entry.return_value = True

    with pytest.raises(RuntimeError, match="node connection refused"):
        await do_direct_send(
            wallet_service=wallet,
            mixdepth=0,
            amount_sats=50_000,
            destination="bcrt1qdestination",
        )

    # Pre-broadcast append happened
    mock_append_entry.assert_called_once()

    # Post-broadcast update recorded failure
    mock_update_entry.assert_called_once_with(
        fake_entry,
        txid="",
        success=False,
        failure_reason="broadcast failed: node connection refused",
        data_dir=ANY,
    )


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast")
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_forwards_fee_overrides(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    mock_append_entry: MagicMock,
    mock_update_entry: MagicMock,
) -> None:
    """Fee overrides (configset [POLICY] tx_fees, issue #566) reach prepare_direct_send."""
    wallet = MagicMock()
    wallet.data_dir = None
    wallet.sync_with_registered_bonds = AsyncMock()
    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(return_value="txid_123")
    mock_get_backend.return_value = backend
    mock_prepare_direct_send.return_value = _make_prepared_tx()

    await do_direct_send(
        wallet_service=wallet,
        mixdepth=1,
        amount_sats=5000,
        destination="bcrt1qdestination",
        fee_rate=2.5,
        tx_fee_factor=0.4,
        rbf=False,
    )
    assert mock_prepare_direct_send.call_args.kwargs["fee_rate"] == 2.5
    assert mock_prepare_direct_send.call_args.kwargs["tx_fee_factor"] == 0.4
    assert mock_prepare_direct_send.call_args.kwargs["rbf"] is False
    assert "fee_target_blocks" not in mock_prepare_direct_send.call_args.kwargs

    await do_direct_send(
        wallet_service=wallet,
        mixdepth=1,
        amount_sats=5000,
        destination="bcrt1qdestination",
        fee_target_blocks=6,
    )
    assert mock_prepare_direct_send.call_args.kwargs["fee_rate"] is None
    assert mock_prepare_direct_send.call_args.kwargs["fee_target_blocks"] == 6


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast")
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_forwards_input_utxos(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    mock_append_entry: MagicMock,
    mock_update_entry: MagicMock,
) -> None:
    """Explicit input UTXOs (issue #587) reach prepare_direct_send unchanged."""
    wallet = MagicMock(data_dir=None, network="regtest", wallet_fingerprint="aabbccdd")
    wallet.sync_with_registered_bonds = AsyncMock()
    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(return_value="txid_123")
    mock_get_backend.return_value = backend
    mock_prepare_direct_send.return_value = _make_prepared_tx()

    await do_direct_send(
        wallet_service=wallet,
        mixdepth=0,
        amount_sats=50_000,
        destination="bcrt1qdestination",
        input_utxos=[f"{'aa' * 32}:0"],
    )

    assert mock_prepare_direct_send.call_args.kwargs["input_utxos"] == [f"{'aa' * 32}:0"]

    await do_direct_send(
        wallet_service=wallet,
        mixdepth=0,
        amount_sats=50_000,
        destination="bcrt1qdestination",
    )

    assert mock_prepare_direct_send.call_args.kwargs["input_utxos"] is None


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast")
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_uses_local_txid_when_backend_omits_it(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    mock_append_entry: MagicMock,
    mock_update_entry: MagicMock,
) -> None:
    wallet = MagicMock(data_dir=None, network="regtest", wallet_fingerprint="aabbccdd")
    wallet.sync_with_registered_bonds = AsyncMock()
    prepared = _make_prepared_tx()
    mock_prepare_direct_send.return_value = prepared
    mock_create_entry.return_value = MagicMock()
    mock_update_entry.return_value = True

    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(side_effect=["", None])
    mock_get_backend.return_value = backend

    results = [
        await do_direct_send(
            wallet_service=wallet,
            mixdepth=0,
            amount_sats=50_000,
            destination="bcrt1qdestination",
        )
        for _ in range(2)
    ]

    assert [result.txid for result in results] == [prepared.txid, prepared.txid]
    assert [call.kwargs["txid"] for call in mock_update_entry.call_args_list] == [
        prepared.txid,
        prepared.txid,
    ]


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast")
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_aborts_before_broadcast_when_history_append_fails(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    mock_append_entry: MagicMock,
    mock_update_entry: MagicMock,
) -> None:
    from jmwallet.history import HistoryWriteError

    wallet = MagicMock(data_dir=None, network="regtest", wallet_fingerprint="aabbccdd")
    wallet.sync_with_registered_bonds = AsyncMock()
    prepared = _make_prepared_tx()
    mock_prepare_direct_send.return_value = prepared
    mock_create_entry.return_value = MagicMock()
    mock_append_entry.side_effect = HistoryWriteError("disk full")

    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(return_value=prepared.txid)
    mock_get_backend.return_value = backend

    with pytest.raises(HistoryWriteError, match="disk full"):
        await do_direct_send(
            wallet_service=wallet,
            mixdepth=0,
            amount_sats=50_000,
            destination="bcrt1qdestination",
        )

    backend.broadcast_transaction.assert_not_awaited()
    mock_update_entry.assert_not_called()


@pytest.mark.asyncio
@patch("jmwalletd.send.update_send_awaiting_broadcast", return_value=False)
@patch("jmwalletd.send.append_history_entry")
@patch("jmwalletd.send.create_send_history_entry")
@patch("jmwalletd.send.prepare_direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_warns_when_history_row_cannot_be_finalized(
    mock_get_backend: AsyncMock,
    mock_prepare_direct_send: AsyncMock,
    mock_create_entry: MagicMock,
    mock_append_entry: MagicMock,
    mock_update_entry: MagicMock,
) -> None:
    wallet = MagicMock(data_dir=None, network="regtest", wallet_fingerprint="aabbccdd")
    wallet.sync_with_registered_bonds = AsyncMock()
    prepared = _make_prepared_tx()
    mock_prepare_direct_send.return_value = prepared
    mock_create_entry.return_value = MagicMock()

    backend = MagicMock()
    backend.broadcast_transaction = AsyncMock(return_value=prepared.txid)
    mock_get_backend.return_value = backend

    with patch("jmwalletd.send.logger.warning") as mock_warning:
        result = await do_direct_send(
            wallet_service=wallet,
            mixdepth=0,
            amount_sats=50_000,
            destination="bcrt1qdestination",
        )

    assert result.txid == prepared.txid
    mock_update_entry.assert_called_once()
    mock_warning.assert_called_once_with(
        "Could not find pre-broadcast send history entry to finalize"
    )
