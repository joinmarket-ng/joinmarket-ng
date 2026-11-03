"""Complete mining-fee bounds protect commitments before maker inputs are known."""

from __future__ import annotations

import math
import random

import pytest
from _taker_test_helpers import make_taker_config, make_utxo
from jmcore.constants import BITCOIN_DUST_THRESHOLD
from jmcore.fee_policy import estimate_p2wpkh_vsize
from jmcore.models import Offer, OfferType

from taker.coinjoin_session import CoinJoinSession
from taker.taker import Taker, TakerState, _estimate_initial_tx_shape


def _round(
    *,
    makers: int = 2,
    rate: float = 10,
    cap: float = 100,
    contribution: int = 0,
    sweep: bool = False,
    residual: int = 0,
) -> tuple[Taker, dict[str, Offer]]:
    taker = Taker.__new__(Taker)
    taker.config = make_taker_config(
        minimum_makers=1,
        max_fee_rate_sat_vb=cap,
        tx_fee_factor=0,
    )
    session = CoinJoinSession()
    session.attach(taker)
    taker._session = session
    session.preselected_utxos = [make_utxo(value=25_000_000)]
    session.is_sweep = sweep
    session._fee_rate = session._randomized_fee_rate = rate
    inputs, outputs = _estimate_initial_tx_shape(1, makers, is_sweep=True, max_maker_utxos=15)
    session._sweep_tx_fee_budget = session._estimate_tx_fee(inputs, outputs, use_base_rate=True)
    session.cj_amount = 25_000_000 - session._sweep_tx_fee_budget - residual if sweep else 1_000_000
    offers = {
        f"maker{i}": Offer(
            counterparty=f"maker{i}",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=10_000,
            maxsize=100_000_000,
            txfee=contribution,
            cjfee=0,
        )
        for i in range(makers)
    }
    return taker, offers


@pytest.mark.parametrize(
    "rate, cap, expected", [(60, 100, False), (40, 100, True), (60, 120, True)]
)
def test_eight_maker_sweep_headroom(rate: float, cap: float, expected: bool) -> None:
    taker, offers = _round(makers=8, rate=rate, cap=cap, sweep=True)
    assert taker._preflight_fee_headroom(offers) is expected
    if not expected:
        assert taker.state == TakerState.FAILED
        assert "wallet.max_fee_rate_sat_vb" in (taker.last_failure_reason or "")


@pytest.mark.parametrize("contribution, expected", [(20_000, False), (5_000, True)])
def test_fixed_amount_includes_maker_mining_contributions(
    contribution: int, expected: bool
) -> None:
    taker, offers = _round(contribution=contribution)
    assert taker._preflight_fee_headroom(offers) is expected


@pytest.mark.parametrize("residual", [0, 100, BITCOIN_DUST_THRESHOLD, BITCOIN_DUST_THRESHOLD + 1])
def test_sweep_only_charges_residual_without_a_change_output(residual: int) -> None:
    taker, offers = _round(rate=20, cap=50, sweep=True, residual=residual)
    fee = taker._session._sweep_tx_fee_budget
    outputs = 1 + 2 * len(offers)
    if residual > BITCOIN_DUST_THRESHOLD:
        outputs += 1
    else:
        fee += residual
    expected = fee <= 50 * estimate_p2wpkh_vsize(1 + len(offers), outputs)
    assert taker._preflight_fee_headroom(offers) is expected


def test_unfunded_sweep_is_rejected_before_commitment() -> None:
    taker, offers = _round(sweep=True, residual=-1)
    assert not taker._preflight_fee_headroom(offers)
    assert "selected funding" in (taker.last_failure_reason or "")


def test_passing_fixed_bound_covers_more_inputs_and_change_transitions() -> None:
    rng = random.Random(427)
    accepted = 0
    for _ in range(200):
        makers = rng.randint(1, 10)
        rate = rng.uniform(1, 100)
        cap = rng.choice([100.0, 150.0])
        contribution = rng.randint(0, 10_000)
        taker, offers = _round(makers=makers, rate=rate, cap=cap, contribution=contribution)
        if not taker._preflight_fee_headroom(offers):
            continue
        accepted += 1
        for extra_inputs in (0, 1, 7, 40, 256):
            inputs = 1 + makers + extra_inputs
            allocation = math.ceil(rate * estimate_p2wpkh_vsize(inputs, 2 + 2 * makers))
            for change, residual in ((True, 0), (False, 0), (False, 1), (False, 2730)):
                vsize = estimate_p2wpkh_vsize(inputs, 1 + 2 * makers + int(change))
                complete_fee = allocation + makers * contribution + residual
                assert complete_fee <= cap * vsize
    assert accepted > 50
