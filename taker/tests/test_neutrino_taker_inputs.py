"""Reject stale local discovery outputs before using their PoDLE commitments."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from _taker_test_helpers import (
    SAMPLE_MNEMONIC,
    initialize_test_commitment_history,
    make_taker_config,
)
from jmcore.bitcoin import address_to_scriptpubkey
from jmcore.models import Offer, OfferType
from jmwallet.backends.base import BlockchainBackend, UTXOVerificationResult
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService

from taker.models import MakerSession, PhaseResult
from taker.taker import Taker, TakerState


@pytest.fixture
def taker(tmp_path: Path) -> Taker:
    backend = Mock(spec=BlockchainBackend)
    backend.requires_neutrino_metadata.return_value = True
    backend.can_provide_neutrino_metadata.return_value = True
    backend.can_estimate_fee.return_value = False
    backend.get_mempool_min_fee = AsyncMock(return_value=None)
    wallet = WalletService(SAMPLE_MNEMONIC, backend, network="regtest", data_dir=tmp_path)
    coins = []
    for index, value in enumerate((50_000_000, 25_000_000, 20_000_000)):
        address = wallet.get_address(1, 0, index)
        coins.append(
            UTXOInfo(
                txid="abc"[index] * 64,
                vout=0,
                value=value,
                address=address,
                scriptpubkey=address_to_scriptpubkey(address).hex(),
                height=100,
                confirmations=100,
                path=f"m/84'/1'/1'/0/{index}",
                mixdepth=1,
            )
        )
    wallet.utxo_cache[1] = coins[:2]

    async def verify(txid: str, **kwargs: object) -> UTXOVerificationResult:
        if txid == coins[0].txid:
            return UTXOVerificationResult(valid=False, error="UTXO not found")
        coin = next(c for c in coins if c.txid == txid)
        return UTXOVerificationResult(
            valid=True, value=coin.value, confirmations=10, block_height=190
        )

    backend.verify_wallet_utxo_with_metadata = AsyncMock(side_effect=verify)
    initialize_test_commitment_history(tmp_path)
    result = Taker(
        wallet,
        backend,
        make_taker_config(
            data_dir=tmp_path,
            fee_rate=1.0,
            counterparty_count=2,
            minimum_makers=2,
            taker_utxo_age=5,
        ),
    )
    offers = {
        nick: Offer(
            counterparty=nick,
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=1_000,
            maxsize=100_000_000,
            txfee=0,
            cjfee=500,
        )
        for nick in ("J5maker1", "J5maker2")
    }
    result.directory_client.fetch_orderbook = AsyncMock(return_value=list(offers.values()))
    result._update_offers_with_bond_values = AsyncMock()
    result.orderbook_manager.update_offers = Mock()
    result.orderbook_manager.select_makers = Mock(return_value=(offers, 1_000))
    result.orderbook_manager.select_makers_for_sweep = Mock(return_value=(offers, 5_000_000, 1_000))
    # Stop at the commitment boundary so its arguments show exactly what would
    # consume a fresh index, without requiring network peers.
    result.podle_manager.generate_fresh_commitment = Mock(return_value=None)
    result._run_fill_with_replacements = AsyncMock()
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [5_000_000, 0])
async def test_automatic_inputs_skip_ghost_before_consuming_commitment(
    taker: Taker, amount: int
) -> None:
    ghost, live = taker.wallet.utxo_cache[1]
    await taker.do_coinjoin(amount, taker.wallet.get_address(2, 0, 0), mixdepth=1)

    consumed = taker.podle_manager.generate_fresh_commitment.call_args.kwargs["wallet_utxos"]
    assert consumed == [live]
    assert ghost in taker.wallet.utxo_cache[1]
    assert taker.wallet.get_locked_input_outpoints() == set()
    taker._run_fill_with_replacements.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_ghost_fails_without_commitment_or_substitution(taker: Taker) -> None:
    ghost, _ = taker.wallet.utxo_cache[1]
    await taker.do_coinjoin(
        5_000_000,
        taker.wallet.get_address(2, 0, 0),
        mixdepth=1,
        input_utxos=[ghost.outpoint],
    )

    assert taker.state == TakerState.FAILED
    assert "no longer available" in taker.last_failure_reason
    taker.podle_manager.generate_fresh_commitment.assert_not_called()
    taker._run_fill_with_replacements.assert_not_awaited()
    assert taker.wallet.get_locked_input_outpoints() == set()


@pytest.mark.asyncio
async def test_unavailable_final_check_releases_reservations(taker: Taker) -> None:
    live = taker.wallet.utxo_cache[1][1]
    taker.wallet.utxo_cache[1] = [live]
    taker.backend.verify_wallet_utxo_with_metadata.side_effect = [
        UTXOVerificationResult(valid=True, value=live.value, confirmations=10),
        UTXOVerificationResult(valid=False, conclusive=False, error="backend unavailable"),
    ]
    await taker.do_coinjoin(5_000_000, taker.wallet.get_address(2, 0, 0), mixdepth=1)

    assert "unavailable" in taker.last_failure_reason
    taker.podle_manager.generate_fresh_commitment.assert_not_called()
    assert taker.wallet.get_locked_input_outpoints() == set()
    assert taker._session.reserved_inputs == set()
    assert taker._session.rejected_input_outpoints == set()


@pytest.mark.asyncio
async def test_cancellation_during_final_check_releases_reservations(taker: Taker) -> None:
    live = taker.wallet.utxo_cache[1][1]
    taker.wallet.utxo_cache[1] = [live]
    entered = asyncio.Event()
    calls = 0

    async def verify(**kwargs: object) -> UTXOVerificationResult:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await asyncio.Event().wait()
        return UTXOVerificationResult(valid=True, value=live.value, confirmations=10)

    taker.backend.verify_wallet_utxo_with_metadata.side_effect = verify
    task = asyncio.create_task(
        taker.do_coinjoin(5_000_000, taker.wallet.get_address(2, 0, 0), mixdepth=1)
    )
    await entered.wait()
    assert taker.wallet.get_locked_input_outpoints() == {(live.txid, live.vout)}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    taker.podle_manager.generate_fresh_commitment.assert_not_called()
    assert taker.wallet.get_locked_input_outpoints() == set()


@pytest.mark.asyncio
async def test_range_negative_is_reconsidered_on_next_attempt(taker: Taker) -> None:
    ghost, live = taker.wallet.utxo_cache[1]
    destination = taker.wallet.get_address(2, 0, 0)
    await taker.do_coinjoin(5_000_000, destination, mixdepth=1)
    assert taker.podle_manager.generate_fresh_commitment.call_args.kwargs["wallet_utxos"] == [live]

    taker.backend.verify_wallet_utxo_with_metadata.side_effect = None
    taker.backend.verify_wallet_utxo_with_metadata.return_value = UTXOVerificationResult(
        valid=True, value=ghost.value, confirmations=10
    )
    await taker.do_coinjoin(5_000_000, destination, mixdepth=1)
    assert taker.podle_manager.generate_fresh_commitment.call_args.kwargs["wallet_utxos"] == [ghost]
    assert taker._session.rejected_input_outpoints == set()
    assert taker.wallet.get_locked_input_outpoints() == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["auth-replacement", "blacklist"])
async def test_retry_does_not_consume_another_commitment_from_ghost(
    taker: Taker, phase: str
) -> None:
    ghost = taker.wallet.utxo_cache[1][0]
    session = taker._session
    session.preselected_utxos = [ghost]
    session.cj_amount = 5_000_000
    session.maker_target_count = 2
    session._fee_rate = session._randomized_fee_rate = 1.0
    offers = taker.orderbook_manager.select_makers.return_value[0]
    session.maker_sessions = {
        nick: MakerSession(nick=nick, offer=offer) for nick, offer in offers.items()
    }
    session.podle_commitment = Mock()
    session.podle_commitment.commitment.commitment.hex.return_value = "ab" * 32
    taker._fill_replacement_makers = AsyncMock()
    if phase == "auth-replacement":
        session.maker_sessions.pop("J5maker2")
        session.maker_sessions["J5maker1"].responded_auth = True
        session._phase_auth = AsyncMock(return_value=PhaseResult(success=True, podle_revealed=True))
        result = await taker._run_auth_with_replacements(None, Mock(), 1)
        taker._fill_replacement_makers.assert_not_awaited()
    else:
        session._phase_fill = AsyncMock(
            return_value=PhaseResult(
                success=True,
                failed_makers=["J5maker1"],
                blacklist_error=True,
                blacklist_makers=["J5maker1"],
            )
        )
        notifier = Mock()
        notifier.notify_coinjoin_start = AsyncMock()
        with (
            patch("taker.taker.get_notifier", return_value=notifier),
            patch("jmcore.commitment_blacklist.add_commitment"),
        ):
            result = await Taker._run_fill_with_replacements(
                taker, taker.wallet.get_address(2, 0, 0), offers, None, 1, Mock(), 0
            )
        session._phase_fill.assert_awaited_once()
    assert result is False
    assert taker.state == TakerState.FAILED
    assert "no longer available" in taker.last_failure_reason
    taker.podle_manager.generate_fresh_commitment.assert_not_called()


@pytest.mark.asyncio
async def test_commitment_expansion_skips_ghost_and_releases_its_lock(taker: Taker) -> None:
    ghost, live = taker.wallet.utxo_cache[1]
    extra_address = taker.wallet.get_address(1, 0, 2)
    extra = UTXOInfo(
        txid="c" * 64,
        vout=0,
        value=20_000_000,
        address=extra_address,
        scriptpubkey=address_to_scriptpubkey(extra_address).hex(),
        height=100,
        confirmations=90,
        path="m/84'/1'/1'/0/2",
        mixdepth=1,
    )
    taker.wallet.utxo_cache[1].append(extra)
    session = taker._session
    session.cj_amount = 5_000_000
    session.preselected_utxos = [live]
    original = {(live.txid, live.vout)}
    assert taker.wallet.reserve_coinjoin_inputs(original, owner=session.input_lock_owner)
    session.reserved_inputs = original

    assert await session._expand_verified_preselected_utxos(1) == 1
    assert session.preselected_utxos == [live, extra]
    assert taker.wallet.get_locked_input_outpoints() == {
        (live.txid, live.vout),
        (extra.txid, extra.vout),
    }
    assert (ghost.txid, ghost.vout) in session.rejected_input_outpoints
    taker.release_input_locks()


@pytest.mark.asyncio
@pytest.mark.parametrize("known_rejection", [False, True])
async def test_later_funding_additions_skip_ghost_before_building_transaction(
    taker: Taker, known_rejection: bool
) -> None:
    ghost, committed = taker.wallet.utxo_cache[1]
    address = taker.wallet.get_address(1, 0, 2)
    extra = UTXOInfo(
        txid="c" * 64,
        vout=0,
        value=20_000_000,
        address=address,
        scriptpubkey=address_to_scriptpubkey(address).hex(),
        height=100,
        confirmations=90,
        path="m/84'/1'/1'/0/2",
        mixdepth=1,
    )
    taker.wallet.utxo_cache[1].append(extra)
    taker.backend.get_block_height = AsyncMock(return_value=200)
    session = taker._session
    session.cj_amount = committed.value
    session._fee_rate = session._randomized_fee_rate = 10.0
    session.preselected_utxos = [committed]
    if known_rejection:
        session.rejected_input_outpoints.add((ghost.txid, ghost.vout))
    original = {(committed.txid, committed.vout)}
    assert taker.wallet.reserve_coinjoin_inputs(original, owner=session.input_lock_owner)
    session.reserved_inputs = original

    assert await session._phase_build_tx(taker.wallet.get_address(2, 0, 0), mixdepth=1)
    assert session.selected_utxos == [committed, extra]
    assert (ghost.txid, ghost.vout) in session.rejected_input_outpoints
    if known_rejection:
        assert all(
            call.kwargs["txid"] != ghost.txid
            for call in taker.backend.verify_wallet_utxo_with_metadata.await_args_list
        )
    assert taker.wallet.get_locked_input_outpoints() == {
        (committed.txid, committed.vout),
        (extra.txid, extra.vout),
    }
    taker.release_input_locks()
