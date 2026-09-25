"""Persisted input locks must be released when a coinjoin round fails.

Input locks are persisted to the wallet metadata file with a TTL of several
minutes. A failed ``do_coinjoin`` round that returns without releasing them
would keep the inputs "locked by another in-flight CoinJoin" for the whole
TTL, blocking retries even from fresh Taker instances (which discard the
in-memory reservation but not the on-disk lock). This is exactly what a
tumbler retry loop hits when a phase fails mid-negotiation.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from _taker_test_helpers import make_taker_config, make_utxo
from jmcore.channel_ring import RingNodeBinding
from jmcore.channel_ring_store import (
    Outpoint,
    RingParticipantRecord,
    RingParticipantRole,
    RingParticipantStore,
)
from jmcore.models import Offer, OfferType
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.utxo_metadata import UTXOMetadataStore

from taker.taker import Taker, TakerState


def _make_wallet(utxos: list) -> AsyncMock:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.get_utxos = AsyncMock(return_value=utxos)
    wallet.get_all_utxos = Mock(return_value=list(utxos))
    wallet.get_locked_input_outpoints = Mock(return_value=set())
    wallet.select_utxos = Mock(return_value=list(utxos))
    wallet.reserve_coinjoin_inputs = Mock(return_value=True)
    wallet.renew_coinjoin_inputs = Mock(return_value=True)
    wallet.release_coinjoin_inputs = Mock()
    return wallet


def _backend() -> AsyncMock:
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    backend.can_estimate_fee = Mock(return_value=False)
    backend.get_mempool_min_fee = AsyncMock(return_value=None)
    return backend


def _offer(nick: str) -> Offer:
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=1_000,
        maxsize=100_000_000,
        txfee=0,
        cjfee=500,
    )


@pytest.mark.asyncio
async def test_failure_after_reservation_releases_persisted_locks() -> None:
    """A round that reserves inputs and then fails must release the locks."""
    utxo = make_utxo(txid_char="a", value=25_000_000, confirmations=10)
    wallet = _make_wallet([utxo])
    config = make_taker_config(
        counterparty_count=2,
        minimum_makers=2,
        taker_utxo_age=5,
        taker_utxo_amtpercent=20,
        fee_rate=1.0,
    )
    taker = Taker(wallet, _backend(), config)

    offers = [_offer("J5maker1"), _offer("J5maker2")]
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=offers)
    taker._update_offers_with_bond_values = AsyncMock()  # type: ignore[method-assign]
    taker.orderbook_manager.update_offers = Mock()  # type: ignore[method-assign]
    taker.orderbook_manager.select_makers = Mock(  # type: ignore[method-assign]
        return_value=({o.counterparty: o for o in offers}, 1_000)
    )
    # Fail the round right after the reservation: no PoDLE commitment.
    taker.podle_manager.get_fresh_commitment_utxos = Mock(return_value=[utxo])  # type: ignore[method-assign]
    taker.podle_manager.generate_fresh_commitment = Mock(return_value=None)  # type: ignore[method-assign]

    result = await taker.do_coinjoin(
        amount=5_000_000,
        destination="bcrt1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        mixdepth=0,
    )

    assert result is None
    assert taker.state == TakerState.FAILED
    wallet.reserve_coinjoin_inputs.assert_called_once_with(
        {(utxo.txid, utxo.vout)},
        ttl=taker._session.input_lock_ttl_sec(),
        owner=taker._session.input_lock_owner,
    )
    wallet.release_coinjoin_inputs.assert_called_once_with(
        {(utxo.txid, utxo.vout)}, owner=taker._session.input_lock_owner
    )
    assert taker._session.reserved_inputs == set()


@pytest.mark.asyncio
async def test_initial_confirmation_timeout_stops_before_podle_and_releases_locks() -> None:
    utxo = make_utxo(txid_char="a", value=25_000_000, confirmations=10)
    wallet = _make_wallet([utxo])
    config = make_taker_config(
        counterparty_count=2,
        minimum_makers=2,
        taker_utxo_age=5,
        taker_utxo_amtpercent=20,
        fee_rate=1.0,
    )
    config.initial_confirmation_timeout_sec = 0.01  # type: ignore[assignment]

    async def wait_forever(**kwargs: object) -> bool:
        await asyncio.Event().wait()
        return True

    taker = Taker(wallet, _backend(), config, confirmation_callback=wait_forever)
    offers = [_offer("J5maker1"), _offer("J5maker2")]
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=offers)
    taker._update_offers_with_bond_values = AsyncMock()  # type: ignore[method-assign]
    taker.orderbook_manager.update_offers = Mock()  # type: ignore[method-assign]
    taker.orderbook_manager.select_makers = Mock(  # type: ignore[method-assign]
        return_value=({o.counterparty: o for o in offers}, 1_000)
    )
    taker.podle_manager.get_fresh_commitment_utxos = Mock(return_value=[utxo])  # type: ignore[method-assign]
    taker.podle_manager.generate_fresh_commitment = Mock()  # type: ignore[method-assign]
    taker._run_fill_with_replacements = AsyncMock()  # type: ignore[method-assign]

    result = await taker.do_coinjoin(
        amount=5_000_000,
        destination="bcrt1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        mixdepth=0,
    )

    assert result is None
    assert taker.state == TakerState.CANCELLED
    assert taker.last_failure_reason is not None
    assert "confirmation expired" in taker.last_failure_reason
    taker.podle_manager.generate_fresh_commitment.assert_not_called()
    taker._run_fill_with_replacements.assert_not_awaited()
    wallet.release_coinjoin_inputs.assert_called_once_with(
        {(utxo.txid, utxo.vout)}, owner=taker._session.input_lock_owner
    )
    assert taker._session.reserved_inputs == set()


def test_release_failure_preserves_inputs_for_retry() -> None:
    """A transient release failure must leave the session able to retry."""
    utxo = make_utxo()
    wallet = _make_wallet([utxo])
    wallet.release_coinjoin_inputs = Mock(side_effect=[RuntimeError("temporary failure"), None])
    taker = Taker(wallet, _backend(), make_taker_config())
    reserved_inputs = {(utxo.txid, utxo.vout)}
    taker._session.reserved_inputs = set(reserved_inputs)
    owner = taker._session.input_lock_owner

    taker.release_input_locks()

    assert taker._session.reserved_inputs == reserved_inputs
    wallet.release_coinjoin_inputs.assert_called_once_with(reserved_inputs, owner=owner)

    taker.release_input_locks()

    assert taker._session.reserved_inputs == set()
    assert wallet.release_coinjoin_inputs.call_count == 2
    wallet.release_coinjoin_inputs.assert_called_with(reserved_inputs, owner=owner)


@pytest.mark.asyncio
async def test_late_synchronous_confirmation_is_rejected() -> None:
    def confirm_after_timeout(**kwargs: object) -> bool:
        time.sleep(0.02)
        return True

    taker = Taker(
        _make_wallet([]),
        _backend(),
        make_taker_config(),
        confirmation_callback=confirm_after_timeout,
    )

    with pytest.raises(TimeoutError):
        await taker._request_confirmation(timeout=0.01, stage="initial")


@pytest.mark.asyncio
async def test_new_round_does_not_release_prior_post_sign_lease() -> None:
    """Fresh round state must not compare-and-release an earlier signed lease."""
    utxo = make_utxo(txid_char="a", value=25_000_000, confirmations=1)  # immature
    wallet = _make_wallet([utxo])
    wallet.select_utxos = Mock(side_effect=ValueError("Insufficient funds"))
    taker = Taker(wallet, _backend(), make_taker_config(taker_utxo_age=5))
    leftover = {("b" * 64, 1)}
    prior_session = taker._session
    prior_owner = prior_session.input_lock_owner
    prior_session.reserved_inputs = set(leftover)
    prior_session.signing_boundary_crossed = True

    result = await taker.do_coinjoin(amount=5_000_000, destination="INTERNAL", mixdepth=0)

    assert result is None
    assert taker._session is not prior_session
    assert taker._session.input_lock_owner != prior_owner
    wallet.release_coinjoin_inputs.assert_not_called()
    wallet.renew_coinjoin_inputs.assert_not_called()
    assert taker._session.reserved_inputs == set()


def test_active_ring_lock_is_retained_when_round_cleanup_runs() -> None:
    wallet = _make_wallet([])
    taker = Taker(wallet, _backend(), make_taker_config())
    reserved = {("c" * 64, 2)}
    taker._session.reserved_inputs = set(reserved)
    taker._session.ring_coordinator = SimpleNamespace(_record=lambda: SimpleNamespace(active=True))

    taker.release_input_locks()

    wallet.release_coinjoin_inputs.assert_not_called()
    assert taker._session.reserved_inputs == reserved


def test_pre_invite_ring_failure_releases_inputs_without_reading_a_missing_journal() -> None:
    wallet = _make_wallet([])
    taker = Taker(wallet, _backend(), make_taker_config())
    reserved = {("a" * 64, 1)}
    taker._session.reserved_inputs = set(reserved)
    owner = taker._session.input_lock_owner
    record = Mock(side_effect=AssertionError("no invitation or journal exists"))
    taker._session.ring_coordinator = SimpleNamespace(has_durable_record=False, _record=record)

    taker.release_input_locks()

    record.assert_not_called()
    wallet.release_coinjoin_inputs.assert_called_once_with(reserved, owner=owner)
    assert taker._session.reserved_inputs == set()


def test_new_round_detaches_active_ring_lock_and_rotates_owner() -> None:
    wallet = _make_wallet([])
    taker = Taker(wallet, _backend(), make_taker_config())
    previous_owner = taker._session.input_lock_owner
    taker._session.reserved_inputs = {("d" * 64, 3)}
    taker._session.ring_coordinator = SimpleNamespace(_record=lambda: SimpleNamespace(active=True))

    taker._begin_input_lock_round()

    wallet.release_coinjoin_inputs.assert_not_called()
    assert taker._session.reserved_inputs == set()
    assert taker._session.input_lock_owner != previous_owner
    assert taker._session.ring_coordinator is None


@pytest.mark.parametrize("lease_exists", [True, False])
def test_restart_renews_active_ring_lock_with_persisted_owner(lease_exists: bool) -> None:
    wallet = _make_wallet([])
    wallet.renew_coinjoin_inputs.return_value = lease_exists
    taker = Taker(wallet, _backend(), make_taker_config())
    outpoint = SimpleNamespace(txid="e" * 64, vout=4)
    taker._channel_ring_store = SimpleNamespace(
        load_all=lambda: SimpleNamespace(
            records=(
                SimpleNamespace(
                    active=True,
                    local_input_outpoints=(outpoint,),
                    input_lock_owner="taker:persisted-owner",
                ),
            ),
            corruptions=(),
        )
    )

    assert taker._renew_channel_ring_input_locks()
    wallet.renew_coinjoin_inputs.assert_called_once_with(
        {(outpoint.txid, outpoint.vout)},
        ttl=taker._session.input_lock_ttl_sec(),
        owner="taker:persisted-owner",
    )
    if lease_exists:
        wallet.reserve_coinjoin_inputs.assert_not_called()
    else:
        wallet.reserve_coinjoin_inputs.assert_called_once_with(
            {(outpoint.txid, outpoint.vout)},
            ttl=taker._session.input_lock_ttl_sec(),
            owner="taker:persisted-owner",
        )


@pytest.mark.parametrize(
    ("lease_state", "expected"),
    [
        ("owned", True),
        ("missing", True),
        ("expired", True),
        ("foreign", False),
        ("ownerless", False),
        ("frozen", False),
        ("no_metadata", False),
        ("partial", False),
    ],
)
def test_ring_reconciliation_preserves_wallet_lock_ownership(
    tmp_path: Path, lease_state: str, expected: bool
) -> None:
    outpoint = Outpoint(txid="12" * 32, vout=0)
    outpoints = (
        (outpoint, Outpoint(txid="56" * 32, vout=1)) if lease_state == "partial" else (outpoint,)
    )
    owner = "taker:durable-owner"
    ring_store = RingParticipantStore(
        tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2
    )
    ring_store.save(
        RingParticipantRecord.fresh(
            node_binding=RingNodeBinding(
                network="regtest",
                wallet_identity="00" * 32,
                source_mixdepth=0,
                node_name="local",
                local_node_id="0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
            ),
            round_nonce="34" * 32,
            revision=0,
            taker_session_identity="taker:round",
            local_role=RingParticipantRole.TAKER,
            local_position=0,
            local_input_outpoints=outpoints,
            input_lock_owner=None if lease_state == "ownerless" else owner,
        )
    )
    metadata = UTXOMetadataStore(path=tmp_path / "metadata.jsonl")
    wallet = WalletService.__new__(WalletService)
    wallet.metadata_store = metadata if lease_state != "no_metadata" else None
    points = {(outpoint.txid, outpoint.vout)}
    if lease_state in {"owned", "foreign", "expired", "partial"}:
        lock_owner = "taker:another-round" if lease_state == "foreign" else owner
        # An old clock creates an expired lease without sleeping or rewriting metadata.
        with patch(
            "jmwallet.wallet.utxo_metadata.time.time",
            return_value=1.0 if lease_state == "expired" else time.time(),
        ):
            assert wallet.reserve_coinjoin_inputs(points, ttl=60, owner=lock_owner)
    if lease_state == "frozen":
        metadata.freeze(str(outpoint))
    before = metadata.path.read_bytes() if metadata.path.exists() else None
    taker = Taker.__new__(Taker)
    taker.wallet = wallet
    taker._channel_ring_store = ring_store
    taker._session = SimpleNamespace(input_lock_ttl_sec=lambda: 600)  # type: ignore[assignment]

    assert taker._renew_channel_ring_input_locks() is expected

    if expected:
        metadata.load()
        lease = metadata.records[str(outpoint)]
        assert lease.lock_owner == owner
        assert lease.lock_until is not None and lease.lock_until > time.time() + 500
        assert not wallet.reserve_coinjoin_inputs(points, owner="taker:another-round")
    else:
        after = metadata.path.read_bytes() if metadata.path.exists() else None
        assert after == before


@pytest.mark.asyncio
async def test_fee_growth_reserves_extra_inputs_with_round_owner() -> None:
    initial = make_utxo(txid_char="f", value=1_000)
    extra = make_utxo(txid_char="0", value=1_000, vout=1)
    wallet = _make_wallet([initial, extra])
    wallet.select_utxos = Mock(return_value=[initial, extra])
    wallet.get_change_address = Mock(return_value="bcrt1qchange")
    wallet.get_next_address_index = Mock(return_value=0)
    backend = _backend()
    # HEAD builds an anti-fee-sniping locktime, so a real height is required.
    backend.get_block_height = AsyncMock(return_value=500)
    taker = Taker(wallet, backend, make_taker_config())
    session = taker._session
    session.cj_amount = 1_500
    session.preselected_utxos = [initial]
    session.reserved_inputs = {(initial.txid, initial.vout)}
    session._fee_rate = 1.0
    session._randomized_fee_rate = 1.0
    session._estimate_tx_fee = Mock(return_value=100)  # type: ignore[method-assign]

    with patch("taker.coinjoin_session.build_coinjoin_tx", return_value=(b"\x00", {})):
        assert await session._phase_build_tx("bcrt1qdestination", 0)

    wallet.reserve_coinjoin_inputs.assert_called_once_with(
        {(extra.txid, extra.vout)},
        ttl=session.input_lock_ttl_sec(),
        owner=session.input_lock_owner,
    )
