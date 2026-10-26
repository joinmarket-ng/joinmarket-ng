"""A maker input must never duplicate a taker input in the final transaction.

The !auth duplicate check only compares against the preselected UTXOs, but in
normal mode ``_phase_build_tx`` can add more taker UTXOs afterwards. A maker
that declared one of those must be rejected and ignored, not produce a
duplicate-input (consensus-invalid) transaction.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config, make_utxo
from jmcore.bitcoin import parse_transaction_bytes
from jmcore.models import Offer, OfferType

from taker.coinjoin_session import CoinJoinSession
from taker.models import MakerSession
from taker.taker import Taker, TakerState

DESTINATION = "bcrt1qqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcruj60yu"


def _wallet(preselected: list, reselected: list) -> AsyncMock:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.wallet_fingerprint = "deadbeef"
    wallet.utxo_cache = {0: preselected}
    wallet.get_utxos = AsyncMock(return_value=preselected)
    wallet.get_all_utxos = Mock(return_value=preselected)
    wallet.get_locked_input_outpoints = Mock(return_value=set())
    wallet.get_new_internal_address = Mock(
        return_value="bcrt1qq5zs2pg9q5zs2pg9q5zs2pg9q5zs2pg9ajutfp"
    )
    wallet.reserve_coinjoin_inputs = Mock(return_value=True)
    wallet.release_coinjoin_inputs = Mock()
    wallet.select_utxos = Mock(return_value=reselected)
    wallet.get_key_for_address = Mock(return_value=None)
    return wallet


def _backend() -> AsyncMock:
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    backend.can_estimate_fee = Mock(return_value=False)
    backend.get_mempool_min_fee = AsyncMock(return_value=None)
    backend.get_block_height = AsyncMock(return_value=840_000)
    return backend


def _maker(nick: str, utxo_txid: str) -> MakerSession:
    offer = Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=0,
        cjfee=0,
    )
    session = MakerSession(nick=nick, offer=offer)
    session.responded_fill = True
    session.responded_auth = True
    session.utxos = [{"txid": utxo_txid, "vout": 0, "value": 2_000_000, "address": "bcrt1qm"}]
    session.cj_address = "bcrt1qqyqszqgpqyqszqgpqyqszqgpqyqszqgpvxat9t"
    session.change_address = "bcrt1qqgpqyqszqgpqyqszqgpqyqszqgpqyqszazmwwa"
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("upper", [False, True])
async def test_build_rejects_maker_input_added_as_taker_input(tmp_path: Path, upper: bool) -> None:
    small = make_utxo(txid_char="a", value=50_000)
    extra = make_utxo(txid_char="b", value=2_000_000)
    wallet = _wallet([small], [small, extra])
    config = make_taker_config(minimum_makers=1, fee_rate=1.0, data_dir=tmp_path)
    taker = Taker(wallet, _backend(), config)
    session = taker._session
    session.cj_amount = 1_000_000
    session.preselected_utxos = [small]
    session.reserved_inputs = {(small.txid, small.vout)}
    session._fee_rate = 1.0
    maker_txid = extra.txid.upper() if upper else extra.txid
    session.maker_sessions = {"J5maker": _maker("J5maker", maker_txid)}

    result = await session._phase_build_tx(destination=DESTINATION, mixdepth=0)

    if session.unsigned_tx:
        inputs = parse_transaction_bytes(session.unsigned_tx).inputs
        outpoints = [(i.txid_le, i.vout) for i in inputs]
        assert len(outpoints) == len(set(outpoints)), "duplicate input in transaction"
    assert result is False
    assert session.conflicting_input_nicks == {"J5maker"}
    assert (extra.txid, extra.vout) not in session.reserved_inputs


@pytest.mark.asyncio
async def test_build_accepts_distinct_maker_input_after_reselection(tmp_path: Path) -> None:
    small = make_utxo(txid_char="a", value=50_000)
    extra = make_utxo(txid_char="b", value=2_000_000)
    wallet = _wallet([small], [small, extra])
    config = make_taker_config(minimum_makers=1, fee_rate=1.0, data_dir=tmp_path)
    taker = Taker(wallet, _backend(), config)
    session = taker._session
    session.cj_amount = 1_000_000
    session.preselected_utxos = [small]
    session.reserved_inputs = {(small.txid, small.vout)}
    session._fee_rate = 1.0
    session.maker_sessions = {"J5maker": _maker("J5maker", "c" * 64)}

    assert await session._phase_build_tx(destination=DESTINATION, mixdepth=0) is True
    assert session.conflicting_input_nicks == set()


@pytest.mark.asyncio
async def test_do_coinjoin_ignores_maker_declaring_taker_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    small = make_utxo(txid_char="a", value=1_100_000, confirmations=10)
    extra = make_utxo(txid_char="b", value=2_000_000, confirmations=10)
    wallet = _wallet([small], [small])
    config = make_taker_config(
        counterparty_count=1,
        minimum_makers=1,
        taker_utxo_age=5,
        fee_rate=2.0,
        data_dir=tmp_path,
    )
    taker = Taker(wallet, _backend(), config)
    maker = _maker("J5maker", extra.txid)
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=[maker.offer])
    taker._update_offers_with_bond_values = AsyncMock()  # type: ignore[method-assign]
    taker.orderbook_manager.select_makers = Mock(  # type: ignore[method-assign]
        return_value=({"J5maker": maker.offer}, 0)
    )
    taker.orderbook_manager.add_ignored_maker = Mock()  # type: ignore[method-assign]
    taker.podle_manager.get_fresh_commitment_utxos = Mock(  # type: ignore[method-assign]
        return_value=[small]
    )
    commitment = Mock()
    commitment.commitment.commitment = bytes.fromhex("ab" * 32)
    taker.podle_manager.generate_fresh_commitment = Mock(  # type: ignore[method-assign]
        return_value=commitment
    )

    async def fill(**kwargs: object) -> bool:
        taker._session.maker_sessions = {"J5maker": maker}
        # Preselection was just enough at the initial fee estimate; the build
        # step has to add the second wallet UTXO.
        taker._session.cj_amount = 1_099_900
        wallet.select_utxos.return_value = [small, extra]
        return True

    taker._run_fill_with_replacements = AsyncMock(side_effect=fill)  # type: ignore[method-assign]
    taker._run_auth_with_replacements = AsyncMock(return_value=True)  # type: ignore[method-assign]
    collect = AsyncMock(return_value=False)
    monkeypatch.setattr(CoinJoinSession, "_phase_collect_signatures", collect)

    result = await taker.do_coinjoin(amount=1_000_000, destination=DESTINATION, mixdepth=0)

    assert result is None
    assert taker.state == TakerState.FAILED
    collect.assert_not_awaited()
    taker.orderbook_manager.add_ignored_maker.assert_any_call("J5maker")
