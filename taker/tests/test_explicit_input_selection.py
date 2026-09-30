"""Tests for strict explicit CoinJoin input selection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config, make_utxo
from jmcore.models import Offer, OfferType

from taker.coinjoin_session import CoinJoinSession
from taker.taker import Taker, TakerState


def _wallet(utxos: list) -> AsyncMock:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.wallet_fingerprint = "deadbeef"
    wallet.utxo_cache = {0: utxos}
    wallet.get_utxos = AsyncMock(return_value=utxos)
    wallet.get_locked_input_outpoints = Mock(return_value=set())
    wallet.get_new_internal_address = Mock(return_value="bcrt1qinternaldest")
    wallet.reserve_coinjoin_inputs = Mock(return_value=True)
    wallet.release_coinjoin_inputs = Mock()
    wallet.select_utxos = Mock(return_value=utxos)
    wallet.get_all_utxos = Mock(return_value=utxos)
    wallet.get_key_for_address = Mock(return_value=None)
    return wallet


def _backend() -> AsyncMock:
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    backend.get_mempool_min_fee = AsyncMock(return_value=None)
    backend.get_block_height = AsyncMock(return_value=840_000)
    return backend


def _config(tmp_path: Path, **overrides: object):  # noqa: ANN202
    defaults: dict[str, object] = {
        "select_utxos": False,
        "taker_utxo_age": 5,
        "counterparty_count": 1,
        "minimum_makers": 1,
        "fee_rate": 1.0,
        "data_dir": tmp_path,
    }
    defaults.update(overrides)
    return make_taker_config(**defaults)


def _offer() -> Offer:
    return Offer(
        counterparty="maker1",
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=0,
        cjfee=0,
    )


@pytest.mark.asyncio
async def test_resolves_explicit_inputs_in_request_order(tmp_path: Path) -> None:
    first = make_utxo(txid_char="a", vout=0, value=8_000_000, confirmations=10)
    second = make_utxo(txid_char="b", vout=1, value=4_000_000, confirmations=10)
    taker = Taker(_wallet([first, second]), _backend(), _config(tmp_path))

    selected = await taker._resolve_explicit_input_utxos(
        [f"{second.txid}:{second.vout}", f"{first.txid}:{first.vout}"],
        mixdepth=0,
        amount=5_000_000,
    )

    assert selected == [second, first]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["locked", "immature", "bond"])
async def test_rejects_coinjoin_ineligible_explicit_input(
    tmp_path: Path,
    failure: str,
) -> None:
    utxo = make_utxo(txid_char="a", value=8_000_000, confirmations=10)
    wallet = _wallet([utxo])
    backend = _backend()
    expected: str
    if failure == "locked":
        wallet.get_locked_input_outpoints.return_value = {(utxo.txid, utxo.vout)}
        expected = "locked by another in-flight CoinJoin"
    elif failure == "immature":
        utxo.confirmations = 1
        expected = "CoinJoin requires at least 5"
    else:
        utxo.locktime = 1
        expected = "CoinJoin inputs cannot be bonds"

    taker = Taker(wallet, backend, _config(tmp_path))
    reason = await taker.check_utxo_eligibility(
        5_000_000,
        0,
        input_utxos=[f"{utxo.txid}:{utxo.vout}"],
    )

    assert reason is not None
    assert expected in reason
    if failure == "bond":
        backend.get_median_time_past.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_inputs_must_provide_podle_candidate(tmp_path: Path) -> None:
    first = make_utxo(txid_char="a", value=600_000, confirmations=10)
    second = make_utxo(txid_char="b", vout=1, value=600_000, confirmations=10)
    taker = Taker(
        _wallet([first, second]),
        _backend(),
        _config(tmp_path, taker_utxo_amtpercent=80),
    )

    reason = await taker.check_utxo_eligibility(
        1_000_000,
        0,
        input_utxos=[f"{first.txid}:0", f"{second.txid}:1"],
    )

    assert reason is not None
    assert "No explicit input UTXO is large enough" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0, 5_000_000], ids=["sweep", "fixed"])
async def test_coinjoin_preselects_exact_explicit_set(tmp_path: Path, amount: int) -> None:
    selected = make_utxo(txid_char="a", value=25_000_000, confirmations=10)
    other = make_utxo(txid_char="b", vout=1, value=30_000_000, confirmations=10)
    wallet = _wallet([selected, other])
    taker = Taker(wallet, _backend(), _config(tmp_path))
    offer = _offer()
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=[offer])
    taker._update_offers_with_bond_values = AsyncMock()  # type: ignore[method-assign]
    taker.orderbook_manager.select_makers = Mock(  # type: ignore[method-assign]
        return_value=({"maker1": offer}, 0)
    )
    taker.orderbook_manager.select_makers_for_sweep = Mock(  # type: ignore[method-assign]
        return_value=({"maker1": offer}, 5_000_000, 0)
    )
    taker.podle_manager.generate_fresh_commitment = Mock(  # type: ignore[method-assign]
        return_value=None
    )

    result = await taker.do_coinjoin(
        amount=amount,
        destination="INTERNAL",
        mixdepth=0,
        input_utxos=[f"{selected.txid}:{selected.vout}"],
    )

    assert result is None
    assert taker._session.preselected_utxos == [selected]
    assert taker._session.strict_input_selection is True
    wallet.select_utxos.assert_not_called()
    wallet.get_all_utxos.assert_not_called()


@pytest.mark.asyncio
async def test_generated_destination_provenance_survives_round_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = make_utxo(txid_char="a", value=25_000_000, confirmations=10)
    wallet = _wallet([selected])
    taker = Taker(wallet, _backend(), _config(tmp_path))
    offer = _offer()
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=[offer])
    taker._update_offers_with_bond_values = AsyncMock()  # type: ignore[method-assign]
    taker.orderbook_manager.select_makers = Mock(  # type: ignore[method-assign]
        return_value=({"maker1": offer}, 0)
    )
    commitment = Mock()
    commitment.commitment.commitment = bytes.fromhex("ab" * 32)
    taker.podle_manager.generate_fresh_commitment = Mock(  # type: ignore[method-assign]
        return_value=commitment
    )
    taker._run_fill_with_replacements = AsyncMock(return_value=True)  # type: ignore[method-assign]
    taker._run_auth_with_replacements = AsyncMock(return_value=True)  # type: ignore[method-assign]
    build = AsyncMock(return_value=False)
    monkeypatch.setattr(CoinJoinSession, "_phase_build_tx", build)
    external = "bcrt1qqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcruj60yu"
    for requested, resolved, internal in [
        ("INTERNAL", "bcrt1qinternaldest", True),
        (external, external, False),
    ]:
        build.reset_mock()
        assert (
            await taker.do_coinjoin(
                amount=5_000_000,
                destination=requested,
                mixdepth=0,
                input_utxos=[f"{selected.txid}:{selected.vout}"],
            )
            is None
        )
        build.assert_awaited_once_with(
            destination=resolved, mixdepth=0, destination_is_internal=internal
        )
    wallet.get_new_internal_address.assert_called_once_with(1)


@pytest.mark.asyncio
async def test_invalid_explicit_input_fails_before_orderbook(tmp_path: Path) -> None:
    wallet = _wallet([])
    taker = Taker(wallet, _backend(), _config(tmp_path))
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=[])

    result = await taker.do_coinjoin(
        amount=5_000_000,
        destination="INTERNAL",
        mixdepth=0,
        input_utxos=[f"{'cc' * 32}:0"],
    )

    assert result is None
    assert taker.state == TakerState.FAILED
    assert taker.last_failure_reason is not None
    assert "not found in mixdepth 0" in taker.last_failure_reason
    taker.directory_client.fetch_orderbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_inputs_do_not_expand_when_negotiated_fees_increase(tmp_path: Path) -> None:
    selected = make_utxo(txid_char="a", value=1_000_000, confirmations=10)
    wallet = _wallet([selected])
    taker = Taker(wallet, _backend(), _config(tmp_path))
    taker._session.preselected_utxos = [selected]
    taker._session.strict_input_selection = True
    taker._session.cj_amount = 1_000_000
    taker._session.is_sweep = False
    taker._session._fee_rate = 1.0
    taker._session._randomized_fee_rate = 1.0

    result = await taker._session._phase_build_tx(
        destination="bcrt1qqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcruj60yu",
        mixdepth=0,
    )

    assert result is False
    assert taker.last_failure_reason is not None
    assert "Explicit input UTXOs are insufficient" in taker.last_failure_reason
    wallet.select_utxos.assert_not_called()


def test_strict_inputs_do_not_expand_for_podle_retry(tmp_path: Path) -> None:
    selected = make_utxo(txid_char="a", value=10_000_000, confirmations=10)
    wallet = _wallet([selected])
    taker = Taker(wallet, _backend(), _config(tmp_path))
    taker._session.preselected_utxos = [selected]
    taker._session.strict_input_selection = True

    assert taker._session._expand_preselected_utxos_same_mixdepth(0) == 0
    wallet.get_all_utxos.assert_not_called()
