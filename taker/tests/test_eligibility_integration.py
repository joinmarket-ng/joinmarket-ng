"""Integration tests for Taker pre-flight eligibility wiring (issue #528)."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config, make_utxo

from taker.taker import Taker, TakerState


def _make_wallet(utxos: list, *, locked: set | None = None) -> AsyncMock:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.get_utxos = AsyncMock(return_value=utxos)
    wallet.get_locked_input_outpoints = Mock(return_value=locked or set())

    # Default real-ish select_utxos: greedy by value, raise if insufficient.
    def _select(mixdepth, amount, min_conf, exclude=None):  # noqa: ANN001, ANN202
        exclude = exclude or set()
        eligible = sorted(
            (
                u
                for u in utxos
                if u.confirmations >= min_conf
                and not u.frozen
                and not u.is_fidelity_bond
                and (u.txid, u.vout) not in exclude
            ),
            key=lambda u: u.value,
            reverse=True,
        )
        selected: list = []
        total = 0
        for u in eligible:
            selected.append(u)
            total += u.value
            if total >= amount:
                return selected
        raise ValueError(f"Insufficient funds: need {amount:,} sats, have {total:,} sats")

    wallet.select_utxos = Mock(side_effect=_select)
    return wallet


def _make_config(**overrides: object):  # noqa: ANN202
    base: dict[str, object] = {
        "counterparty_count": 2,
        "minimum_makers": 2,
        "taker_utxo_age": 5,
        "taker_utxo_amtpercent": 20,
    }
    base.update(overrides)
    return make_taker_config(**base)


def _backend() -> AsyncMock:
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    return backend


@pytest.mark.asyncio
async def test_eligible_pool_passes() -> None:
    utxos = [make_utxo(txid_char="a", value=25_000_000, confirmations=10)]
    taker = Taker(_make_wallet(utxos), _backend(), _make_config())
    assert await taker.check_utxo_eligibility(5_000_000, 0) is None


@pytest.mark.asyncio
async def test_immature_pool_rejected() -> None:
    utxos = [make_utxo(txid_char="a", value=25_000_000, confirmations=2)]
    taker = Taker(_make_wallet(utxos), _backend(), _make_config())
    reason = await taker.check_utxo_eligibility(5_000_000, 0)
    assert reason is not None
    assert "No eligible UTXOs in mixdepth 0" in reason
    assert "taker_utxo_age" in reason


@pytest.mark.asyncio
async def test_sweep_only_needs_nonempty_pool() -> None:
    utxos = [make_utxo(txid_char="a", value=1_000, confirmations=10)]
    taker = Taker(_make_wallet(utxos), _backend(), _make_config())
    # Sweep (amount=0) must pass with any eligible UTXO, regardless of size.
    assert await taker.check_utxo_eligibility(0, 0) is None


@pytest.mark.asyncio
async def test_podle_threshold_rejected_early() -> None:
    # Many small UTXOs: together they cover the amount, but no single UTXO
    # reaches 20% of it (200_000), so no PoDLE commitment is possible.
    utxos = [
        make_utxo(txid_char=c, vout=i, value=180_000, confirmations=10)
        for i, c in enumerate("abcdefg")
    ]
    taker = Taker(_make_wallet(utxos), _backend(), _make_config())
    reason = await taker.check_utxo_eligibility(1_000_000, 1)
    assert reason is not None
    assert "PoDLE commitment" in reason
    assert "taker_utxo_amtpercent" in reason


@pytest.mark.asyncio
async def test_insufficient_amount_rejected() -> None:
    # Large enough for PoDLE but not enough total to cover the amount.
    utxos = [make_utxo(txid_char="a", value=3_000_000, confirmations=10)]
    taker = Taker(_make_wallet(utxos), _backend(), _make_config())
    reason = await taker.check_utxo_eligibility(10_000_000, 1)
    assert reason is not None
    assert "Insufficient funds" in reason
    assert "taker_utxo_age" in reason


@pytest.mark.asyncio
async def test_reserved_inputs_excluded() -> None:
    u = make_utxo(txid_char="a", value=25_000_000, confirmations=10)
    wallet = _make_wallet([u], locked={(u.txid, u.vout)})
    taker = Taker(wallet, _backend(), _make_config())
    reason = await taker.check_utxo_eligibility(5_000_000, 0)
    assert reason is not None
    assert "in-flight CoinJoin" in reason


@pytest.mark.asyncio
async def test_do_coinjoin_fails_before_orderbook_fetch() -> None:
    """Ineligible UTXOs must fail before any directory/orderbook network call."""
    utxos = [make_utxo(txid_char="a", value=25_000_000, confirmations=1)]
    taker = Taker(_make_wallet(utxos), _backend(), _make_config())
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=[])

    result = await taker.do_coinjoin(amount=5_000_000, destination="INTERNAL", mixdepth=0)

    assert result is None
    assert taker.state == TakerState.FAILED
    taker.directory_client.fetch_orderbook.assert_not_called()
    assert taker._session.last_failure_reason is not None
    assert "No eligible UTXOs in mixdepth 0" in taker._session.last_failure_reason
