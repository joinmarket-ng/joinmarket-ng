"""Tests for swap recovery persistence and the standalone claim/reconcile flow."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Any

import pytest
from jmcore.bitcoin import (
    pubkey_to_p2wpkh_address,
    script_to_p2wsh_address,
    script_to_p2wsh_scriptpubkey,
)
from jmwallet.backends.base import UTXO, Transaction

from taker.swap.persistence import (
    SwapPersistence,
    SwapPersistenceError,
    SwapRecord,
    SwapRecordStatus,
    build_swap_persistence,
)
from taker.swap.recovery import (
    DUST_THRESHOLD_SATS,
    MIN_CLAIM_FEE_SATS,
    RecoveryOutcome,
    SwapRecovery,
    build_claim_transaction,
    build_swap_recovery,
    wallet_address_provider,
)

NETWORK = "regtest"


def _witness_script() -> bytes:
    # Any push-only script suffices: recovery never executes it, it just needs
    # to hash to the expected P2WSH scriptPubKey and round-trip through hex.
    return b"\x21" + secrets.token_bytes(33) + b"\xac"


def _p2wpkh_address() -> str:
    from coincurve import PrivateKey

    pubkey = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)
    return pubkey_to_p2wpkh_address(pubkey, NETWORK)


class _FakeKeyProvider:
    """Minimal SwapWallet stand-in for recovery tests."""

    def __init__(self, *, data_dir: Path | None, fingerprint: str = "abcd1234") -> None:
        self.data_dir = data_dir
        self.wallet_fingerprint = fingerprint
        self._storage_key = b"\x07" * 32
        self._addr_calls = 0

    def derive_swap_storage_key(self) -> bytes:
        return self._storage_key

    def create_swap_key_material(self) -> Any:  # pragma: no cover - unused here
        raise NotImplementedError

    def derive_swap_key_material(self, index: int) -> Any:  # pragma: no cover - unused here
        raise NotImplementedError

    def build_swap_claim_witness(
        self,
        tx: Any,
        input_index: int,
        witness_script: bytes,
        value: int,
        swap_index: int,
    ) -> list[bytes]:
        # A dummy-but-structurally-valid witness; recovery only serializes it.
        return [b"\x00" * 72, b"\x11" * 32, witness_script]

    def get_new_address(self, mixdepth: int) -> str:
        self._addr_calls += 1
        return _p2wpkh_address()


class _FakeBackend:
    """Backend exposing only what recovery needs, with scriptable behavior."""

    def __init__(
        self,
        *,
        utxos: list[UTXO] | None = None,
        mempool: bool = True,
        known_txids: set[str] | None = None,
    ) -> None:
        self._utxos = utxos or []
        self._mempool = mempool
        self._known_txids = known_txids or set()
        self.broadcast_calls: list[str] = []
        self.block_height = 100

    async def scan_external_address(self, address: str) -> list[UTXO]:
        return [u for u in self._utxos if u.address == address]

    async def broadcast_transaction(self, tx_hex: str) -> str:
        self.broadcast_calls.append(tx_hex)
        from jmcore.bitcoin import get_txid

        return get_txid(tx_hex)

    async def get_transaction(self, txid: str) -> Transaction | None:
        if txid in self._known_txids:
            return Transaction(txid=txid, raw="", confirmations=0)
        return None

    def has_mempool_access(self) -> bool:
        return self._mempool

    async def get_block_height(self) -> int:
        return self.block_height

    def can_provide_neutrino_metadata(self) -> bool:
        return True

    def can_estimate_fee(self) -> bool:
        return False


def _make_record(
    ws: bytes,
    *,
    status: SwapRecordStatus = SwapRecordStatus.LOCKED,
    txid: str = "aa" * 32,
    value: int = 100_000,
    coinjoin_txid: str | None = None,
) -> SwapRecord:
    return SwapRecord(
        swap_id=hashlib.sha256(ws).hexdigest(),
        network=NETWORK,
        swap_index=7,
        redeem_script_hex=ws.hex(),
        lockup_address=script_to_p2wsh_address(ws, NETWORK),
        timeout_block_height=200,
        txid=txid,
        vout=0,
        value=value,
        status=status,
        coinjoin_txid=coinjoin_txid,
    )


def _utxo_for(record: SwapRecord, *, value: int | None = None) -> UTXO:
    return UTXO(
        txid=record.txid,
        vout=record.vout,
        value=record.value if value is None else value,
        address=record.lockup_address,
        confirmations=3,
        scriptpubkey=script_to_p2wsh_scriptpubkey(record.witness_script).hex(),
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


class TestSwapPersistence:
    def _store(self, tmp_path: Path) -> SwapPersistence:
        return SwapPersistence(b"\x07" * 32, data_dir=tmp_path, fingerprint="abcd1234")

    def test_round_trip(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        record = _make_record(_witness_script())
        store.save(record)
        loaded = store.load(record.swap_id)
        assert loaded is not None
        assert loaded == record

    def test_load_missing_returns_none(self, tmp_path: Path) -> None:
        assert self._store(tmp_path).load("deadbeef") is None

    def test_salt_randomizes_ciphertext(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        record = _make_record(_witness_script())
        path = store.save(record)
        first = path.read_bytes()
        store.save(record)
        second = path.read_bytes()
        assert first != second  # fresh salt each write
        assert store.load(record.swap_id) == record

    def test_wrong_key_cannot_decrypt(self, tmp_path: Path) -> None:
        record = _make_record(_witness_script())
        self._store(tmp_path).save(record)
        other = SwapPersistence(b"\x09" * 32, data_dir=tmp_path, fingerprint="abcd1234")
        with pytest.raises(SwapPersistenceError):
            other.load(record.swap_id)
        # list_records skips undecryptable files rather than raising.
        assert other.list_records() == []

    def test_list_unresolved_excludes_terminal(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        live = _make_record(_witness_script(), status=SwapRecordStatus.LOCKED)
        done = _make_record(_witness_script(), status=SwapRecordStatus.RESOLVED)
        store.save(live)
        store.save(done)
        unresolved = store.list_unresolved()
        assert [r.swap_id for r in unresolved] == [live.swap_id]

    def test_delete(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        record = _make_record(_witness_script())
        store.save(record)
        assert store.delete(record.swap_id) is True
        assert store.load(record.swap_id) is None
        assert store.delete(record.swap_id) is False

    def test_empty_storage_key_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            SwapPersistence(b"", data_dir=tmp_path, fingerprint="abcd1234")

    def test_build_swap_persistence_none_without_data_dir(self) -> None:
        assert build_swap_persistence(_FakeKeyProvider(data_dir=None)) is None

    def test_build_swap_persistence_uses_wallet(self, tmp_path: Path) -> None:
        store = build_swap_persistence(_FakeKeyProvider(data_dir=tmp_path))
        assert store is not None
        record = _make_record(_witness_script())
        store.save(record)
        assert store.load(record.swap_id) == record


# --------------------------------------------------------------------------- #
# build_claim_transaction
# --------------------------------------------------------------------------- #


class TestBuildClaimTransaction:
    def test_builds_signed_tx(self) -> None:
        ws = _witness_script()
        provider = _FakeKeyProvider(data_dir=None)
        signed_hex, output_value = build_claim_transaction(
            lockup_txid="bb" * 32,
            lockup_vout=0,
            lockup_value=100_000,
            witness_script=ws,
            destination_address=_p2wpkh_address(),
            fee_sats=350,
            witness_builder=lambda tx, i, w, v: provider.build_swap_claim_witness(tx, i, w, v, 7),
        )
        assert output_value == 100_000 - 350
        assert isinstance(signed_hex, str) and len(signed_hex) > 0

    def test_dust_rejected(self) -> None:
        ws = _witness_script()
        with pytest.raises(ValueError):
            build_claim_transaction(
                lockup_txid="bb" * 32,
                lockup_vout=0,
                lockup_value=DUST_THRESHOLD_SATS + 100,
                witness_script=ws,
                destination_address=_p2wpkh_address(),
                fee_sats=200,
                witness_builder=lambda tx, i, w, v: [b"", b"", w],
            )


# --------------------------------------------------------------------------- #
# SwapRecovery.recover_record
# --------------------------------------------------------------------------- #


@pytest.fixture
def setup(tmp_path: Path):
    def _make(
        *,
        utxos: list[UTXO] | None = None,
        mempool: bool = True,
        known_txids: set[str] | None = None,
    ) -> tuple[SwapRecovery, _FakeBackend, SwapPersistence]:
        store = SwapPersistence(b"\x07" * 32, data_dir=tmp_path, fingerprint="abcd1234")
        backend = _FakeBackend(utxos=utxos, mempool=mempool, known_txids=known_txids)
        provider = _FakeKeyProvider(data_dir=tmp_path)
        recovery = SwapRecovery(backend, store, provider, network=NETWORK)  # type: ignore[arg-type]
        return recovery, backend, store

    return _make


class TestRecoverRecord:
    @pytest.mark.asyncio
    async def test_claims_unspent_lockup(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws)
        recovery, backend, store = setup(utxos=[_utxo_for(record)])
        store.save(record)
        result = await recovery.recover_record(record, destination_address=_p2wpkh_address())
        assert result.outcome is RecoveryOutcome.CLAIMED
        assert len(backend.broadcast_calls) == 1
        assert store.load(record.swap_id).status is SwapRecordStatus.RECOVERED

    @pytest.mark.asyncio
    async def test_no_lockup_when_never_seen(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws, txid="", value=0)
        recovery, backend, store = setup(utxos=[])
        store.save(record)
        result = await recovery.recover_record(record, destination_address=_p2wpkh_address())
        assert result.outcome is RecoveryOutcome.NO_LOCKUP
        assert not backend.broadcast_calls

    @pytest.mark.asyncio
    async def test_spent_with_coinjoin_marks_resolved(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws, coinjoin_txid="cc" * 32)
        recovery, backend, store = setup(utxos=[])  # lockup gone
        store.save(record)
        result = await recovery.recover_record(record, destination_address=_p2wpkh_address())
        assert result.outcome is RecoveryOutcome.ALREADY_SPENT
        assert store.load(record.swap_id).status is SwapRecordStatus.RESOLVED

    @pytest.mark.asyncio
    async def test_spent_without_coinjoin_marks_refunded(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws)
        recovery, backend, store = setup(utxos=[])  # lockup gone, no CoinJoin
        store.save(record)
        result = await recovery.recover_record(record, destination_address=_p2wpkh_address())
        assert result.outcome is RecoveryOutcome.ALREADY_SPENT
        assert store.load(record.swap_id).status is SwapRecordStatus.REFUNDED

    @pytest.mark.asyncio
    async def test_pending_coinjoin_blocks_claim(self, setup) -> None:
        ws = _witness_script()
        cj = "cc" * 32
        record = _make_record(ws, coinjoin_txid=cj)
        # Lockup still unspent AND the CoinJoin is visible (in mempool): must not claim.
        recovery, backend, store = setup(utxos=[_utxo_for(record)], known_txids={cj})
        store.save(record)
        result = await recovery.recover_record(record, destination_address=_p2wpkh_address())
        assert result.outcome is RecoveryOutcome.PENDING_COINJOIN
        assert not backend.broadcast_calls

    @pytest.mark.asyncio
    async def test_force_claim_overrides_pending(self, setup) -> None:
        ws = _witness_script()
        cj = "cc" * 32
        record = _make_record(ws, coinjoin_txid=cj)
        recovery, backend, store = setup(utxos=[_utxo_for(record)], known_txids={cj})
        store.save(record)
        result = await recovery.recover_record(
            record, destination_address=_p2wpkh_address(), force_claim=True
        )
        assert result.outcome is RecoveryOutcome.CLAIMED
        assert len(backend.broadcast_calls) == 1

    @pytest.mark.asyncio
    async def test_dropped_coinjoin_allows_claim(self, setup) -> None:
        ws = _witness_script()
        cj = "cc" * 32
        record = _make_record(ws, coinjoin_txid=cj)
        # Lockup unspent, CoinJoin NOT known (dropped) and mempool visible -> claim.
        recovery, backend, store = setup(utxos=[_utxo_for(record)], known_txids=set())
        store.save(record)
        result = await recovery.recover_record(record, destination_address=_p2wpkh_address())
        assert result.outcome is RecoveryOutcome.CLAIMED

    @pytest.mark.asyncio
    async def test_light_client_holds_off_without_mempool(self, setup) -> None:
        ws = _witness_script()
        cj = "cc" * 32
        record = _make_record(ws, coinjoin_txid=cj)
        # No mempool access: cannot prove the CoinJoin is gone -> do not claim.
        recovery, backend, store = setup(
            utxos=[_utxo_for(record)], mempool=False, known_txids=set()
        )
        store.save(record)
        result = await recovery.recover_record(record, destination_address=_p2wpkh_address())
        assert result.outcome is RecoveryOutcome.PENDING_COINJOIN

    @pytest.mark.asyncio
    async def test_terminal_record_skipped(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws, status=SwapRecordStatus.RECOVERED)
        recovery, backend, store = setup(utxos=[_utxo_for(record)])
        store.save(record)
        result = await recovery.recover_record(record, destination_address=_p2wpkh_address())
        assert result.outcome is RecoveryOutcome.SKIPPED
        assert not backend.broadcast_calls

    @pytest.mark.asyncio
    async def test_dust_lockup_not_swept(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws, value=DUST_THRESHOLD_SATS + 10)
        recovery, backend, store = setup(utxos=[_utxo_for(record)])
        store.save(record)
        result = await recovery.recover_record(
            record, destination_address=_p2wpkh_address(), feerate_sat_vb=50.0
        )
        assert result.outcome is RecoveryOutcome.DUST
        assert not backend.broadcast_calls

    @pytest.mark.asyncio
    async def test_dry_run_does_not_broadcast(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws)
        recovery, backend, store = setup(utxos=[_utxo_for(record)])
        store.save(record)
        result = await recovery.recover_record(
            record, destination_address=_p2wpkh_address(), broadcast=False
        )
        assert result.outcome is RecoveryOutcome.CLAIMED
        assert result.txid is not None
        assert not backend.broadcast_calls

    @pytest.mark.asyncio
    async def test_recover_all_reconciles_each(self, setup) -> None:
        ws1, ws2 = _witness_script(), _witness_script()
        live = _make_record(ws1)
        gone = _make_record(ws2, coinjoin_txid="dd" * 32)
        recovery, backend, store = setup(utxos=[_utxo_for(live)])  # only live present
        store.save(live)
        store.save(gone)
        provider = _FakeKeyProvider(data_dir=None)
        results = await recovery.recover_all(address_provider=wallet_address_provider(provider))
        outcomes = {r.swap_id: r.outcome for r in results}
        assert outcomes[live.swap_id] is RecoveryOutcome.CLAIMED
        assert outcomes[gone.swap_id] is RecoveryOutcome.ALREADY_SPENT

    @pytest.mark.asyncio
    async def test_recover_all_only_consumes_address_for_claims(self, setup) -> None:
        # One claimable lockup, one already-spent record. Lazy allocation must
        # consume exactly one fresh address (for the claim), not one per record.
        ws1, ws2 = _witness_script(), _witness_script()
        live = _make_record(ws1)
        gone = _make_record(ws2, coinjoin_txid="dd" * 32)
        recovery, backend, store = setup(utxos=[_utxo_for(live)])
        store.save(live)
        store.save(gone)
        provider = _FakeKeyProvider(data_dir=None)
        await recovery.recover_all(address_provider=wallet_address_provider(provider))
        assert provider._addr_calls == 1

    @pytest.mark.asyncio
    async def test_dust_lockup_consumes_no_address(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws)
        dust_value = MIN_CLAIM_FEE_SATS + DUST_THRESHOLD_SATS - 1
        recovery, backend, store = setup(utxos=[_utxo_for(record, value=dust_value)])
        store.save(record)
        provider = _FakeKeyProvider(data_dir=None)
        result = await recovery.recover_record(
            record, address_provider=wallet_address_provider(provider)
        )
        assert result.outcome is RecoveryOutcome.DUST
        assert provider._addr_calls == 0
        assert not backend.broadcast_calls

    @pytest.mark.asyncio
    async def test_requires_exactly_one_destination(self, setup) -> None:
        ws = _witness_script()
        record = _make_record(ws)
        recovery, _backend, store = setup(utxos=[_utxo_for(record)])
        store.save(record)
        with pytest.raises(ValueError):
            await recovery.recover_record(record)
        with pytest.raises(ValueError):
            await recovery.recover_record(
                record,
                destination_address=_p2wpkh_address(),
                address_provider=wallet_address_provider(_FakeKeyProvider(data_dir=None)),
            )


class TestBuildSwapRecovery:
    def test_none_without_data_dir(self) -> None:
        backend = _FakeBackend()
        assert build_swap_recovery(_FakeKeyProvider(data_dir=None), backend) is None  # type: ignore[arg-type]

    def test_built_with_data_dir(self, tmp_path: Path) -> None:
        backend = _FakeBackend()
        engine = build_swap_recovery(_FakeKeyProvider(data_dir=tmp_path), backend)  # type: ignore[arg-type]
        assert isinstance(engine, SwapRecovery)


# --------------------------------------------------------------------------- #
# CoinJoinSession persistence hooks
# --------------------------------------------------------------------------- #


def _swap_input(ws: bytes, *, value: int = 100_000):
    from taker.swap.models import SwapInput

    return SwapInput(
        txid="ab" * 32,
        vout=0,
        value=value,
        witness_script=ws,
        preimage=b"\x11" * 32,
        swap_index=7,
        lockup_address=script_to_p2wsh_address(ws, NETWORK),
        timeout_block_height=200,
        swap_id=hashlib.sha256(ws).hexdigest(),
        redeem_script_hex=ws.hex(),
    )


class _FakeTaker:
    def __init__(self, wallet: Any, config: Any, backend: Any) -> None:
        self.wallet = wallet
        self.config = config
        self.backend = backend
        self.directory_client = None


class TestSessionPersistenceHooks:
    def _session(self, tmp_path: Path):
        from jmcore.models import NetworkType

        from taker.coinjoin_session import CoinJoinSession
        from taker.swap.persistence import SwapPersistence

        wallet = _FakeKeyProvider(data_dir=tmp_path)
        config = type(
            "Cfg",
            (),
            {"bitcoin_network": NetworkType.REGTEST, "network": NetworkType.REGTEST},
        )()
        session = CoinJoinSession()
        session._taker = _FakeTaker(wallet, config, _FakeBackend())  # type: ignore[assignment]
        store = SwapPersistence(
            wallet.derive_swap_storage_key(),
            data_dir=tmp_path,
            fingerprint=wallet.wallet_fingerprint,
        )
        return session, store

    def test_persist_swap_locked_writes_record(self, tmp_path: Path) -> None:
        session, store = self._session(tmp_path)
        si = _swap_input(_witness_script())
        session.swap_input = si
        session._persist_swap_locked(si)
        record = store.load(si.swap_id)
        assert record is not None
        assert record.status is SwapRecordStatus.LOCKED
        assert record.swap_index == si.swap_index
        assert record.txid == si.txid

    def test_mark_swap_broadcast_updates_record(self, tmp_path: Path) -> None:
        session, store = self._session(tmp_path)
        si = _swap_input(_witness_script())
        session.swap_input = si
        session._persist_swap_locked(si)
        session.txid = "cc" * 32
        session._mark_swap_broadcast()
        record = store.load(si.swap_id)
        assert record is not None
        assert record.status is SwapRecordStatus.BROADCAST
        assert record.coinjoin_txid == "cc" * 32

    def test_hooks_noop_without_data_dir(self, tmp_path: Path) -> None:
        from jmcore.models import NetworkType

        from taker.coinjoin_session import CoinJoinSession

        wallet = _FakeKeyProvider(data_dir=None)
        config = type(
            "Cfg",
            (),
            {"bitcoin_network": NetworkType.REGTEST, "network": NetworkType.REGTEST},
        )()
        session = CoinJoinSession()
        session._taker = _FakeTaker(wallet, config, _FakeBackend())  # type: ignore[assignment]
        si = _swap_input(_witness_script())
        session.swap_input = si
        # Should not raise even though persistence is unavailable.
        session._persist_swap_locked(si)
        session.txid = "cc" * 32
        session._mark_swap_broadcast()
        assert session._swap_store() is None


# --------------------------------------------------------------------------- #
# Pre-broadcast locktime safety guard
# --------------------------------------------------------------------------- #


class TestSwapLocktimeSafety:
    def _session(self, backend: _FakeBackend, swap_input: Any):
        from jmcore.models import NetworkType

        from taker.coinjoin_session import CoinJoinSession

        wallet = _FakeKeyProvider(data_dir=None)
        config = type(
            "Cfg",
            (),
            {"bitcoin_network": NetworkType.REGTEST, "network": NetworkType.REGTEST},
        )()
        session = CoinJoinSession()
        session._taker = _FakeTaker(wallet, config, backend)  # type: ignore[assignment]
        session.swap_input = swap_input
        return session

    @pytest.mark.asyncio
    async def test_safe_when_no_swap_input(self) -> None:
        backend = _FakeBackend()
        session = self._session(backend, None)
        assert await session._swap_locktime_safe_to_broadcast() is True

    @pytest.mark.asyncio
    async def test_safe_with_ample_margin(self) -> None:
        backend = _FakeBackend()
        backend.block_height = 100  # timeout_block_height=200 -> 100 blocks left
        session = self._session(backend, _swap_input(_witness_script()))
        assert await session._swap_locktime_safe_to_broadcast() is True

    @pytest.mark.asyncio
    async def test_aborts_when_inside_safety_margin(self) -> None:
        from taker.swap.models import BROADCAST_LOCKTIME_SAFETY_MARGIN

        backend = _FakeBackend()
        # Leave fewer blocks than the required margin before refund (200).
        backend.block_height = 200 - (BROADCAST_LOCKTIME_SAFETY_MARGIN - 1)
        session = self._session(backend, _swap_input(_witness_script()))
        assert await session._swap_locktime_safe_to_broadcast() is False
        assert session.last_failure_reason
        assert "Aborting broadcast" in session.last_failure_reason

    @pytest.mark.asyncio
    async def test_safe_without_mempool_access(self) -> None:
        backend = _FakeBackend(mempool=False)
        backend.block_height = 199  # would be unsafe, but height is unverifiable
        session = self._session(backend, _swap_input(_witness_script()))
        assert await session._swap_locktime_safe_to_broadcast() is True


# --------------------------------------------------------------------------- #
# Pre-lockup acquisition failure cleanup
# --------------------------------------------------------------------------- #


class TestSwapAcquireFailureCleanup:
    def _session(self, backend: _FakeBackend):
        from types import SimpleNamespace

        from jmcore.models import NetworkType

        from taker.coinjoin_session import CoinJoinSession

        wallet = _FakeKeyProvider(data_dir=None)
        swap_cfg = SimpleNamespace(
            enabled=True,
            provider_offer_id=None,
            nostr_relays=[],
            max_swap_fee_pct=1.0,
            lnd_rest_url=None,
            lnd_cert_path=None,
            lnd_macaroon_path=None,
            hold_invoice_timeout=600.0,
            lockup_poll_interval=5.0,
            lockup_timeout=300.0,
        )
        config = SimpleNamespace(
            bitcoin_network=NetworkType.REGTEST,
            network=NetworkType.REGTEST,
            swap_input=swap_cfg,
            socks_host=None,
            socks_port=9050,
        )
        session = CoinJoinSession()
        session._taker = _FakeTaker(wallet, config, backend)  # type: ignore[assignment]
        session.cj_amount = 100_000
        return session

    @pytest.mark.asyncio
    async def test_pre_lockup_failure_cancels_pending_payment(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        from taker.swap.models import SwapState

        backend = _FakeBackend()
        session = self._session(backend)

        client = AsyncMock()
        client.state = SwapState.REQUESTING  # pre-lockup: payment may be in flight
        client.discover_provider = AsyncMock()
        client.acquire_swap_input = AsyncMock(side_effect=RuntimeError("prepay failed"))
        client.cancel_pending_payment = AsyncMock()

        monkeypatch.setattr("taker.swap.client.SwapClient", lambda *a, **k: client)

        result = await session._phase_acquire_swap_input(fake_taker_fee=0)

        assert result is False
        client.cancel_pending_payment.assert_awaited_once()
        assert session.swap_client is None
        assert session.swap_input is None


# --------------------------------------------------------------------------- #
# Taker.recover_swaps
# --------------------------------------------------------------------------- #


class TestTakerRecoverSwaps:
    def _taker(self, tmp_path: Path, backend: _FakeBackend):
        from _taker_test_helpers import make_taker_config

        from taker.taker import Taker

        wallet = _FakeKeyProvider(data_dir=tmp_path)
        config = make_taker_config(data_dir=str(tmp_path))
        taker = Taker(wallet, backend, config)  # type: ignore[arg-type]
        store = SwapPersistence(
            wallet.derive_swap_storage_key(),
            data_dir=tmp_path,
            fingerprint=wallet.wallet_fingerprint,
        )
        return taker, store

    @pytest.mark.asyncio
    async def test_recover_swaps_claims_orphaned_lockup(self, tmp_path: Path) -> None:
        ws = _witness_script()
        record = _make_record(ws, coinjoin_txid="dd" * 32)
        backend = _FakeBackend(utxos=[_utxo_for(record)], known_txids=set())  # CJ dropped
        taker, store = self._taker(tmp_path, backend)
        store.save(record)
        results = await taker.recover_swaps()
        assert len(results) == 1
        assert results[0].outcome is RecoveryOutcome.CLAIMED
        assert len(backend.broadcast_calls) == 1

    @pytest.mark.asyncio
    async def test_recover_swaps_empty_when_nothing_pending(self, tmp_path: Path) -> None:
        backend = _FakeBackend()
        taker, _store = self._taker(tmp_path, backend)
        assert await taker.recover_swaps() == []

    @pytest.mark.asyncio
    async def test_recover_swaps_holds_off_in_flight_coinjoin(self, tmp_path: Path) -> None:
        ws = _witness_script()
        cj = "dd" * 32
        record = _make_record(ws, coinjoin_txid=cj)
        backend = _FakeBackend(utxos=[_utxo_for(record)], known_txids={cj})  # CJ still alive
        taker, store = self._taker(tmp_path, backend)
        store.save(record)
        results = await taker.recover_swaps()
        assert results[0].outcome is RecoveryOutcome.PENDING_COINJOIN
        assert not backend.broadcast_calls
