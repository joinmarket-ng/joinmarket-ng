"""Tests for :mod:`tumbler.estimator`."""

from __future__ import annotations

import pytest

from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.estimator import _vbytes_for_coinjoin, estimate_plan_costs
from tumbler.plan import (
    MakerSessionPhase,
    Plan,
    PlanParameters,
    TakerCoinjoinPhase,
)

_DESTINATIONS = [
    "bcrt1qdest0000000000000000000000000000000000aaa",
    "bcrt1qdest0000000000000000000000000000000000bbb",
    "bcrt1qdest0000000000000000000000000000000000ccc",
]
_BALANCES = {0: 10_000_000, 1: 5_000_000, 2: 0, 3: 0, 4: 0}


def _build_plan(**overrides: object) -> Plan:
    kwargs: dict[str, object] = dict(
        destinations=list(_DESTINATIONS),
        mixdepth_balances=_BALANCES,
        seed=42,
    )
    kwargs.update(overrides)
    params = TumbleParameters(**kwargs)  # type: ignore[arg-type]
    return PlanBuilder("w", params).build()


class TestEstimatePlanCosts:
    def test_total_max_cj_fee_respects_local_bounds(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
        )
        # Every taker phase consumes either an absolute or relative cap per
        # counterparty -- never below max(abs, rel * amount).
        assert est.taker_phase_count > 0
        assert est.total_max_cj_fee_sats > 0
        # The relative cap (0.1%) on a 10 BTC sweep would dominate the
        # absolute cap of 500 sats; sanity check that relative scaling is
        # actually applied for sweeps and not silently zeroed.
        assert est.total_max_cj_fee_sats > 500 * est.taker_phase_count * 5

    def test_balances_optional_zeroes_sweep_fees(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances=None,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
        )
        # Without balances, sweeps and fractional phases see amount=0 so the
        # only contribution is the absolute floor per counterparty.
        # That's still positive (every taker phase still picks max(abs, 0)).
        assert est.total_max_cj_fee_sats > 0
        # But strictly less than the balance-aware estimate above.
        balance_aware = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
        )
        assert est.total_max_cj_fee_sats < balance_aware.total_max_cj_fee_sats

    def test_miner_fee_scales_with_fee_rate(self) -> None:
        plan = _build_plan()
        est_low = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
            fee_rate_sat_vb=1.0,
        )
        est_high = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
            fee_rate_sat_vb=10.0,
        )
        assert est_high.total_miner_fee_sats == est_low.total_miner_fee_sats * 10
        assert est_low.fee_rate_sat_vb == 1.0

    def test_miner_fee_omitted_when_no_fee_rate(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
        )
        # When no rate is supplied we now use a labelled fallback so users
        # always see a ballpark miner-fee figure rather than "n/a".
        assert est.total_miner_fee_sats > 0
        assert est.fee_rate_sat_vb == 10.0
        assert est.fee_rate_source == "fallback"

    def test_duration_includes_wait_and_confirmations(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
            confirmation_block_count=5,
            block_time_seconds=600.0,
        )
        # Sum of wait_seconds across all phases must equal the total_wait
        # field; this guards against silent drift if the field is removed.
        expected_wait = sum(p.wait_seconds for p in plan.phases)
        assert abs(est.total_wait_seconds - expected_wait) < 1e-6
        # Min duration excludes confirmation waits (they may not actually
        # be waited if the user disables them); expected/max include them.
        assert est.total_duration_seconds_min < est.total_duration_seconds_max
        assert est.total_duration_seconds_expected <= est.total_duration_seconds_max

    def test_phase_count_matches_plan(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
        )
        taker = sum(1 for p in plan.phases if isinstance(p, TakerCoinjoinPhase))
        maker = sum(1 for p in plan.phases if isinstance(p, MakerSessionPhase))
        assert est.taker_phase_count == taker
        assert est.maker_phase_count == maker
        assert len(est.phases) == taker + maker

    def test_total_max_fee_is_cj_plus_miner(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
            fee_rate_sat_vb=2.5,
        )
        assert est.total_max_fee_sats == est.total_max_cj_fee_sats + est.total_miner_fee_sats

    def test_relative_fee_dominates_for_large_amounts(self) -> None:
        # On a 10 BTC sweep, 0.1% relative fee (1,000,000 sats) is the cap
        # per maker, vastly exceeding the 500-sat absolute cap.
        plan = Plan(
            wallet_name="w",
            destinations=["bcrt1qdest"],
            parameters=PlanParameters(),
            phases=[
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=0,
                    amount=0,  # sweep
                    counterparty_count=5,
                    destination="bcrt1qdest",
                    wait_seconds=0.0,
                ),
            ],
        )
        est = estimate_plan_costs(
            plan,
            mixdepth_balances={0: 1_000_000_000},  # 10 BTC
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
        )
        # 1,000,000 sats per maker * 5 counterparties = 5,000,000 sats.
        assert est.total_max_cj_fee_sats == 5_000_000

    def test_maker_session_contributes_no_cj_fee(self) -> None:
        plan = Plan(
            wallet_name="w",
            destinations=["bcrt1qdest"],
            parameters=PlanParameters(),
            phases=[
                MakerSessionPhase(
                    index=0,
                    duration_seconds=600.0,
                    target_cj_count=None,
                    idle_timeout_seconds=120.0,
                    wait_seconds=10.0,
                ),
            ],
        )
        est = estimate_plan_costs(
            plan,
            mixdepth_balances={},
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
        )
        assert est.total_max_cj_fee_sats == 0
        assert est.maker_phase_count == 1
        assert est.taker_phase_count == 0
        # Min uses idle_timeout, max uses full duration.
        assert est.total_duration_seconds_min == 10.0 + 120.0
        assert est.total_duration_seconds_max == 10.0 + 600.0

    def test_balance_and_percentage_fields(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
            fee_rate_sat_vb=5.0,
        )
        # Total balance and per-mixdepth map must round-trip what we passed.
        assert est.total_balance_sats == sum(_BALANCES.values())
        assert est.mixdepth_balances == dict(_BALANCES)
        # Percentages are derived; verify they line up with the absolute
        # numbers (guards against accidental divisions by something else).
        assert est.total_max_cj_fee_pct == pytest.approx(
            100.0 * est.total_max_cj_fee_sats / est.total_balance_sats
        )
        assert est.total_miner_fee_pct == pytest.approx(
            100.0 * est.total_miner_fee_sats / est.total_balance_sats
        )
        assert est.total_max_fee_pct == pytest.approx(
            est.total_max_cj_fee_pct + est.total_miner_fee_pct
        )

    def test_percentages_safe_when_balance_zero(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances={},
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
            fee_rate_sat_vb=5.0,
        )
        # Without balances we shouldn't blow up dividing by zero; the
        # rendered output just shows 0.0% which the user can interpret.
        assert est.total_balance_sats == 0
        assert est.total_max_cj_fee_pct == 0.0
        assert est.total_miner_fee_pct == 0.0
        assert est.total_max_fee_pct == 0.0

    def test_fee_rate_source_propagates(self) -> None:
        plan = _build_plan()
        est = estimate_plan_costs(
            plan,
            mixdepth_balances=_BALANCES,
            max_cj_fee_abs_sats=500,
            max_cj_fee_rel="0.001",
            fee_rate_sat_vb=3.0,
            fee_rate_source="estimated",
        )
        # Source label must round-trip; CLI uses it to annotate "(estimated)"
        # vs "(configured)" vs "(fallback)".
        assert est.fee_rate_source == "estimated"
        assert est.fee_rate_sat_vb == 3.0


class TestVbytesForCoinjoin:
    def test_grows_with_counterparty_count(self) -> None:
        small = _vbytes_for_coinjoin(1)
        large = _vbytes_for_coinjoin(10)
        assert large > small
        # Each extra counterparty adds at least one input + output worth.
        per_party = (large - small) / 9
        assert 100 < per_party < 200

    def test_negative_counterparty_clamped(self) -> None:
        # Defensive: negative or zero counterparties should not crash and
        # should not produce a smaller-than-base size.
        assert _vbytes_for_coinjoin(0) > 0
        assert _vbytes_for_coinjoin(-1) == _vbytes_for_coinjoin(0)
