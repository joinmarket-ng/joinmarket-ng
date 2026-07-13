"""Tests for the jmwalletd transaction monitor and the shared txinfo builder."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from coincurve import PrivateKey

from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    get_txid,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from jmwallet.backends.base import WalletTxEntry
from jmwalletd.tx_monitor import run_tx_monitor
from jmwalletd.txinfo import build_txinfo_from_hex


def _make_tx(seed: int, value: int = 100_000) -> tuple[str, str]:
    """Return (txid, raw_hex) of a minimal 1-in 1-out tx."""
    priv = PrivateKey(seed.to_bytes(32, "big"))
    script = pubkey_to_p2wpkh_script(priv.public_key.format(compressed=True))
    tin = TxInput.from_hex("11" * 32, 0)
    tout = TxOutput(value=value, script=script)
    raw = serialize_transaction(2, [tin], [tout], 0).hex()
    return get_txid(raw), raw


class FakeBackend:
    supports_tx_enumeration = True

    def __init__(self, polls: list[list[WalletTxEntry]], txs: dict[str, str]) -> None:
        self._polls = polls
        self._txs = txs
        self._i = 0
        self.exhausted = asyncio.Event()

    async def list_wallet_transactions_since(self, cursor):
        if self._i < len(self._polls):
            entries = self._polls[self._i]
            self._i += 1
            return entries, f"c{self._i}"
        self.exhausted.set()
        return [], cursor

    async def get_transaction(self, txid):
        raw = self._txs.get(txid)
        if raw is None:
            return None
        return SimpleNamespace(raw=raw, txid=txid, confirmations=0)


class FakeState:
    def __init__(self, backend: FakeBackend) -> None:
        self.wallet_service = SimpleNamespace(backend=backend)
        self._tx_broadcast_notified: set[str] = set()
        self.broadcasts: list[dict] = []

    def broadcast_ws(self, message: dict) -> None:
        self.broadcasts.append(message)

    def mark_tx_broadcast(self, txid: str) -> None:
        self._tx_broadcast_notified.add(txid)


async def _drive(state: FakeState, backend: FakeBackend) -> None:
    task = asyncio.create_task(run_tx_monitor(state, poll_interval=0.001))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(backend.exhausted.wait(), timeout=3.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_new_mempool_tx_is_notified() -> None:
    txid, raw = _make_tx(1)
    backend = FakeBackend(
        polls=[[], [WalletTxEntry(txid=txid, confirmations=0)]],
        txs={txid: raw},
    )
    state = FakeState(backend)
    await _drive(state, backend)

    assert len(state.broadcasts) == 1
    frame = state.broadcasts[0]
    assert frame["txid"] == txid
    assert frame["txdetails"]["confirmations"] == 0


@pytest.mark.asyncio
async def test_inline_raw_hex_avoids_get_transaction() -> None:
    """When the backend supplies raw hex inline (e.g. neutrino), the monitor
    uses it and does not call get_transaction (which neutrino cannot serve for
    confirmed txs)."""
    txid, raw = _make_tx(30)

    class InlineBackend(FakeBackend):
        async def get_transaction(self, txid):  # pragma: no cover - must not run
            raise AssertionError("get_transaction must not be called when raw is inline")

    backend = InlineBackend(
        polls=[[], [WalletTxEntry(txid=txid, confirmations=1, raw=raw)]],
        txs={},
    )
    state = FakeState(backend)
    await _drive(state, backend)

    assert len(state.broadcasts) == 1
    assert state.broadcasts[0]["txid"] == txid
    assert state.broadcasts[0]["txdetails"]["confirmations"] == 1


@pytest.mark.asyncio
async def test_unconfirmed_tx_deduped_across_polls() -> None:
    txid, raw = _make_tx(2)
    backend = FakeBackend(
        polls=[
            [],
            [WalletTxEntry(txid=txid, confirmations=0)],
            [WalletTxEntry(txid=txid, confirmations=0)],
        ],
        txs={txid: raw},
    )
    state = FakeState(backend)
    await _drive(state, backend)

    assert len(state.broadcasts) == 1  # only the first sighting


@pytest.mark.asyncio
async def test_confirmation_produces_second_notification() -> None:
    txid, raw = _make_tx(3)
    backend = FakeBackend(
        polls=[
            [],
            [WalletTxEntry(txid=txid, confirmations=0)],
            [WalletTxEntry(txid=txid, confirmations=3)],
        ],
        txs={txid: raw},
    )
    state = FakeState(backend)
    await _drive(state, backend)

    assert len(state.broadcasts) == 2
    assert state.broadcasts[0]["txdetails"]["confirmations"] == 0
    assert state.broadcasts[1]["txdetails"]["confirmations"] == 3


@pytest.mark.asyncio
async def test_confirmed_first_notifies_once() -> None:
    txid, raw = _make_tx(4)
    backend = FakeBackend(
        polls=[[], [WalletTxEntry(txid=txid, confirmations=6)]],
        txs={txid: raw},
    )
    state = FakeState(backend)
    await _drive(state, backend)

    assert len(state.broadcasts) == 1
    assert state.broadcasts[0]["txdetails"]["confirmations"] == 6


@pytest.mark.asyncio
async def test_failed_confirmed_notification_retries_without_advancing_cursor() -> None:
    txid, raw = _make_tx(31)

    class TransientBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__(polls=[], txs={txid: raw})
            self.cursors: list[str | None] = []
            self.fetches = 0

        async def list_wallet_transactions_since(self, cursor):
            self.cursors.append(cursor)
            if len(self.cursors) == 1:
                return [], "baseline"
            if len(self.cursors) <= 3:
                return [WalletTxEntry(txid=txid, confirmations=1)], "confirmed"
            self.exhausted.set()
            return [], cursor

        async def get_transaction(self, requested_txid):
            self.fetches += 1
            if self.fetches == 1:
                return None
            return SimpleNamespace(raw=raw, txid=requested_txid, confirmations=1)

    backend = TransientBackend()
    state = FakeState(backend)  # type: ignore[arg-type]
    await _drive(state, backend)  # type: ignore[arg-type]

    assert backend.cursors[:3] == [None, "baseline", "baseline"]
    assert [frame["txid"] for frame in state.broadcasts] == [txid]


@pytest.mark.asyncio
async def test_direct_send_during_fetch_suppresses_monitor_duplicate() -> None:
    txid, raw = _make_tx(32)
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    class RacingBackend(FakeBackend):
        async def get_transaction(self, requested_txid):
            fetch_started.set()
            await release_fetch.wait()
            return SimpleNamespace(raw=raw, txid=requested_txid, confirmations=0)

    backend = RacingBackend(
        polls=[[], [WalletTxEntry(txid=txid, confirmations=0)]],
        txs={txid: raw},
    )
    state = FakeState(backend)
    task = asyncio.create_task(run_tx_monitor(state, poll_interval=0.001))  # type: ignore[arg-type]
    try:
        await asyncio.wait_for(fetch_started.wait(), timeout=1.0)
        state.mark_tx_broadcast(txid)
        release_fetch.set()
        await asyncio.wait_for(backend.exhausted.wait(), timeout=1.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert state.broadcasts == []


@pytest.mark.asyncio
async def test_direct_send_suppresses_mempool_but_not_confirmation() -> None:
    txid, raw = _make_tx(5)
    backend = FakeBackend(
        polls=[
            [],
            [WalletTxEntry(txid=txid, confirmations=0)],
            [WalletTxEntry(txid=txid, confirmations=2)],
        ],
        txs={txid: raw},
    )
    state = FakeState(backend)
    # Direct-send already announced this txid's first sighting.
    state.mark_tx_broadcast(txid)

    await _drive(state, backend)

    # No duplicate mempool notification, but the confirmation is still sent.
    assert len(state.broadcasts) == 1
    assert state.broadcasts[0]["txdetails"]["confirmations"] == 2


@pytest.mark.asyncio
async def test_first_pass_baseline_is_silent() -> None:
    txid, raw = _make_tx(6)
    backend = FakeBackend(
        polls=[[WalletTxEntry(txid=txid, confirmations=6)]],
        txs={txid: raw},
    )
    state = FakeState(backend)
    await _drive(state, backend)

    # Pre-existing transactions present at load time are not announced.
    assert state.broadcasts == []
    assert txid in state._tx_broadcast_notified


class _DelayedReadyBackend:
    """Returns a None cursor (not loaded) until ``ready_after`` polls."""

    supports_tx_enumeration = True

    def __init__(self, ready_after: int, history, new_tx, txs) -> None:
        self._ready_after = ready_after
        self._history = history
        self._new_tx = new_tx
        self._txs = txs
        self._i = 0
        self.exhausted = asyncio.Event()

    async def list_wallet_transactions_since(self, cursor):
        self._i += 1
        if self._i <= self._ready_after:
            return [], None  # backend not loaded yet
        if self._i == self._ready_after + 1:
            return list(self._history), "block-1"  # first real enumeration
        if self._i == self._ready_after + 2:
            return [*self._history, self._new_tx], "block-2"
        self.exhausted.set()
        return [], "block-2"

    async def get_transaction(self, txid):
        raw = self._txs.get(txid)
        return None if raw is None else SimpleNamespace(raw=raw, txid=txid, confirmations=0)


@pytest.mark.asyncio
async def test_baseline_waits_for_backend_ready() -> None:
    """History present when the wallet finishes loading must not be announced;
    only a genuinely new tx afterwards triggers a notification."""
    hist_txid, hist_raw = _make_tx(20)
    new_txid, new_raw = _make_tx(21)
    backend = _DelayedReadyBackend(
        ready_after=2,
        history=[WalletTxEntry(txid=hist_txid, confirmations=10)],
        new_tx=WalletTxEntry(txid=new_txid, confirmations=0),
        txs={hist_txid: hist_raw, new_txid: new_raw},
    )
    state = FakeState(backend)  # type: ignore[arg-type]
    await _drive(state, backend)  # type: ignore[arg-type]

    assert len(state.broadcasts) == 1
    assert state.broadcasts[0]["txid"] == new_txid
    assert hist_txid in state._tx_broadcast_notified


class TestBuildTxinfoFromHex:
    def test_round_trip(self) -> None:
        txid, raw = _make_tx(7, value=250_000)
        info = build_txinfo_from_hex(raw, "regtest", confirmations=4)
        assert info.txid == txid
        assert info.hex == raw
        assert info.confirmations == 4
        assert len(info.inputs) == 1
        assert info.inputs[0].outpoint == f"{'11' * 32}:0"
        assert len(info.outputs) == 1
        assert info.outputs[0].value_sats == 250_000
        assert info.outputs[0].address.startswith("bcrt1")

    def test_serializes_witness_stack_with_compact_sizes(self) -> None:
        priv = PrivateKey((33).to_bytes(32, "big"))
        script = pubkey_to_p2wpkh_script(priv.public_key.format(compressed=True))
        raw = serialize_transaction(
            2,
            [TxInput.from_hex("22" * 32, 1)],
            [TxOutput(value=50_000, script=script)],
            0,
            witnesses=[[b"\x30\x01", b"\x02\x03\x04"]],
        ).hex()

        info = build_txinfo_from_hex(raw, "regtest")

        assert info.inputs[0].witness == "0202300103020304"


class _IdleBackend:
    supports_tx_enumeration = True

    async def list_wallet_transactions_since(self, cursor):
        await asyncio.sleep(0.001)
        return [], cursor

    async def get_transaction(self, txid):
        return None


class _NoEnumBackend:
    supports_tx_enumeration = False


class TestMonitorLifecycle:
    @pytest.mark.asyncio
    async def test_start_is_idempotent_and_lock_cancels(self, tmp_path) -> None:
        from jmwalletd.state import DaemonState

        state = DaemonState(data_dir=tmp_path)
        state.wallet_service = SimpleNamespace(backend=_IdleBackend())  # type: ignore[assignment]

        state.start_tx_monitor()
        task = state._tx_monitor_task
        assert task is not None and not task.done()

        # Idempotent: a second call does not replace the running task.
        state.start_tx_monitor()
        assert state._tx_monitor_task is task

        await state.lock_wallet()
        assert state._tx_monitor_task is None
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_no_monitor_when_backend_lacks_enumeration(self, tmp_path) -> None:
        from jmwalletd.state import DaemonState

        state = DaemonState(data_dir=tmp_path)
        state.wallet_service = SimpleNamespace(backend=_NoEnumBackend())  # type: ignore[assignment]

        state.start_tx_monitor()
        assert state._tx_monitor_task is None

    @pytest.mark.asyncio
    async def test_monitor_readiness_follows_initial_baseline(self, tmp_path) -> None:
        from jmwalletd.state import DaemonState

        backend = FakeBackend(polls=[[]], txs={})
        state = DaemonState(data_dir=tmp_path)
        state.wallet_service = SimpleNamespace(backend=backend)  # type: ignore[assignment]

        state.start_tx_monitor()
        try:
            assert await state.wait_tx_monitor_ready(timeout=1.0) is True
        finally:
            await state.lock_wallet()

    @pytest.mark.asyncio
    async def test_monitor_waits_for_wallet_sync_before_baseline(self, tmp_path) -> None:
        from jmwalletd.state import DaemonState

        backend = FakeBackend(polls=[[]], txs={})
        state = DaemonState(data_dir=tmp_path)
        state.wallet_service = SimpleNamespace(backend=backend)  # type: ignore[assignment]
        sync_complete = asyncio.Event()

        async def sync_wallet() -> None:
            await sync_complete.wait()

        state._wallet_sync_task = asyncio.create_task(sync_wallet())
        state.start_tx_monitor()
        await asyncio.sleep(0)
        assert backend._i == 0

        sync_complete.set()
        try:
            assert await state.wait_tx_monitor_ready(timeout=1.0) is True
            assert backend._i == 1
        finally:
            await state.lock_wallet()

    @pytest.mark.asyncio
    async def test_new_wallet_monitor_does_not_require_history_baseline(self, tmp_path) -> None:
        from jmwalletd.state import DaemonState

        state = DaemonState(data_dir=tmp_path)
        state.wallet_service = SimpleNamespace(backend=_IdleBackend())  # type: ignore[assignment]

        state.start_tx_monitor(baseline_existing=False)
        try:
            assert await state.wait_tx_monitor_ready(timeout=1.0) is True
        finally:
            await state.lock_wallet()

    @pytest.mark.asyncio
    async def test_wallet_sync_failure_fails_monitor_readiness(self, tmp_path) -> None:
        from jmwalletd.state import DaemonState

        async def failed_sync() -> None:
            raise RuntimeError("sync failed")

        state = DaemonState(data_dir=tmp_path)
        state.wallet_service = SimpleNamespace(backend=_IdleBackend())  # type: ignore[assignment]
        state._wallet_sync_task = asyncio.create_task(failed_sync())
        state.start_tx_monitor()
        try:
            assert await state.wait_tx_monitor_ready(timeout=1.0) is False
        finally:
            await state.lock_wallet()
