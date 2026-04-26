"""Tests for the privacy-driven CoinJoin amount rounding.

Covers three layers:

* :func:`tumbler.plan.round_to_significant_figures` - the pure helper.
* :class:`tumbler.builder.PlanBuilder` - sigfig sampling distribution and
  the guarantee that sweeps are never tagged for rounding.
* The runner's amount resolution, exercised end-to-end on a synthetic
  wallet so we know the rounded sat amount actually reaches the taker.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.plan import TakerCoinjoinPhase, round_to_significant_figures
from tumbler.runner import TumbleRunner

_DESTINATIONS = [f"bcrt1qdest{i:02d}xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" for i in range(3)]


# ---------------------------------------------------------------------------
# round_to_significant_figures
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,sigfigs,expected",
    [
        (0, 1, 0),
        (0, 5, 0),
        (1, 1, 1),
        (12, 1, 10),
        (15, 1, 20),
        (123, 2, 120),
        (13_256_421, 2, 13_000_000),
        (13_256_421, 3, 13_300_000),
        (13_256_421, 5, 13_256_000),
        (9_876, 2, 9_900),
        (1_000_000, 1, 1_000_000),
        (1_000_000, 5, 1_000_000),
        # Sub-BTC sat amounts get aggressively flattened at low sigfigs.
        (15_500_000, 1, 20_000_000),
    ],
)
def test_round_to_significant_figures(value: int, sigfigs: int, expected: int) -> None:
    assert round_to_significant_figures(value, sigfigs) == expected


def test_round_rejects_negative() -> None:
    with pytest.raises(ValueError):
        round_to_significant_figures(-1, 2)


@pytest.mark.parametrize("sigfigs", [0, 9, -1])
def test_round_rejects_out_of_range_sigfigs(sigfigs: int) -> None:
    with pytest.raises(ValueError):
        round_to_significant_figures(123, sigfigs)


# ---------------------------------------------------------------------------
# Builder sampling
# ---------------------------------------------------------------------------


def _params(**overrides: Any) -> TumbleParameters:
    base: dict[str, Any] = dict(
        destinations=list(_DESTINATIONS),
        mixdepth_balances={0: 50_000_000, 1: 60_000_000, 2: 40_000_000, 3: 0, 4: 0},
        seed=1,
    )
    base.update(overrides)
    return TumbleParameters(**base)


def test_sweeps_never_have_rounding_sigfigs() -> None:
    """Sweeps must dispatch as amount=0; rounding is meaningless and would
    be a privacy footgun (it would round zero, but more importantly it
    would imply the runner could substitute a non-sweep amount).
    """
    plan = PlanBuilder("RoundTest", _params(rounding_chance=1.0)).build()
    sweeps = [p for p in plan.phases if isinstance(p, TakerCoinjoinPhase) and p.is_sweep]
    assert sweeps, "expected at least one sweep phase"
    for sw in sweeps:
        assert sw.rounding_sigfigs is None


def test_rounding_chance_zero_disables_rounding() -> None:
    plan = PlanBuilder("RoundTest", _params(rounding_chance=0.0)).build()
    fractional = [p for p in plan.phases if isinstance(p, TakerCoinjoinPhase) and not p.is_sweep]
    assert fractional
    assert all(p.rounding_sigfigs is None for p in fractional)


def test_rounding_chance_one_always_rounds() -> None:
    plan = PlanBuilder("RoundTest", _params(rounding_chance=1.0)).build()
    fractional = [p for p in plan.phases if isinstance(p, TakerCoinjoinPhase) and not p.is_sweep]
    assert fractional
    assert all(p.rounding_sigfigs is not None for p in fractional)
    assert all(1 <= p.rounding_sigfigs <= 5 for p in fractional)  # type: ignore[operator]


def test_sigfig_weights_skew_distribution() -> None:
    """With weight 1 only on sigfigs=1, all rounded phases must use 1 sigfig."""
    plan = PlanBuilder(
        "RoundTest",
        _params(
            rounding_chance=1.0,
            rounding_sigfig_weights=(1.0, 0.0, 0.0, 0.0, 0.0),
        ),
    ).build()
    fractional = [p for p in plan.phases if isinstance(p, TakerCoinjoinPhase) and not p.is_sweep]
    assert fractional
    assert all(p.rounding_sigfigs == 1 for p in fractional)


def test_sigfig_distribution_matches_weights_over_many_seeds() -> None:
    """Aggregate sigfig draws across many seeds; the heavily-weighted
    sigfigs (1 and 4) should jointly account for the majority of draws.

    We don't pin the exact mode because the per-plan sample is small
    (3-5 fractional phases per mixdepth, ~5 mixdepths) so a single seed
    can swing the histogram - but across hundreds of seeds the weight
    distribution must show through.
    """
    counter: Counter[int] = Counter()
    for seed in range(500):
        plan = PlanBuilder("RoundTest", _params(seed=seed, rounding_chance=1.0)).build()
        for p in plan.phases:
            if isinstance(p, TakerCoinjoinPhase) and not p.is_sweep:
                assert p.rounding_sigfigs is not None
                counter[p.rounding_sigfigs] += 1
    total = sum(counter.values())
    assert total > 100, f"expected many draws, got {total}"
    # All five buckets should fire at least once.
    assert set(counter.keys()) == {1, 2, 3, 4, 5}
    # Default weights = (55, 15, 25, 65, 40). The two heaviest are 4 (65)
    # and 1 (55), summing to 120/200 = 60% of weight; the lightest is 2
    # (15) at 7.5%. Require: heaviest two together > the lightest two
    # together by a comfortable margin.
    heaviest_two = counter[4] + counter[1]
    lightest_two = counter[2] + counter[3]
    assert heaviest_two > lightest_two, f"weight skew not visible: counts={dict(counter)}"
    # And every bucket's share is in the right ballpark of its weight share.
    weights = {1: 55, 2: 15, 3: 25, 4: 65, 5: 40}
    weight_total = sum(weights.values())
    for sf, count in counter.items():
        observed = count / total
        expected = weights[sf] / weight_total
        # Generous 10pp tolerance: the per-plan correlation makes the
        # binomial CI wider than independent draws would suggest.
        assert abs(observed - expected) < 0.10, (
            f"sigfig={sf}: observed={observed:.3f} expected={expected:.3f}"
        )


# ---------------------------------------------------------------------------
# Runner integration
# ---------------------------------------------------------------------------


def _make_runner_with_balance(balance_sats: int) -> TumbleRunner:
    """Build a TumbleRunner whose only exercised dependency is
    ``ctx.wallet_service.get_balance``. Avoids the heavy fixture surface.
    """
    runner = TumbleRunner.__new__(TumbleRunner)
    ctx = MagicMock()
    ctx.wallet_service = MagicMock()
    ctx.wallet_service.get_balance = AsyncMock(return_value=balance_sats)
    runner.ctx = ctx  # type: ignore[attr-defined]
    return runner


def test_runner_applies_rounding_to_resolved_amount() -> None:
    runner = _make_runner_with_balance(50_000_000)
    phase = TakerCoinjoinPhase(
        index=0,
        mixdepth=0,
        amount_fraction=0.265128,
        counterparty_count=5,
        destination="INTERNAL",
        rounding_sigfigs=2,
    )
    # Raw fraction would yield 13_256_400; rounded to 2 sf -> 13_000_000.
    assert asyncio.run(runner._resolve_amount(phase)) == 13_000_000


def test_runner_skips_rounding_when_unset() -> None:
    runner = _make_runner_with_balance(50_000_000)
    phase = TakerCoinjoinPhase(
        index=0,
        mixdepth=0,
        amount_fraction=0.265128,
        counterparty_count=5,
        destination="INTERNAL",
    )
    # No rounding requested -> raw int truncation only (note: float math
    # may shave 1 sat vs. naive expectation; that is the existing behavior).
    assert asyncio.run(runner._resolve_amount(phase)) == int(50_000_000 * 0.265128)


def test_runner_does_not_round_zero() -> None:
    """A fractional phase that resolves to 0 sats (e.g., empty mixdepth)
    must short-circuit to 0 without touching the rounding helper, which
    would otherwise raise on the ``while power10 > 0`` loop.
    """
    runner = _make_runner_with_balance(0)
    phase = TakerCoinjoinPhase(
        index=0,
        mixdepth=0,
        amount_fraction=0.5,
        counterparty_count=5,
        destination="INTERNAL",
        rounding_sigfigs=2,
    )
    assert asyncio.run(runner._resolve_amount(phase)) == 0


def test_runner_ignores_rounding_on_explicit_amount_phase() -> None:
    """If a phase carries a concrete ``amount`` (no fraction), rounding is
    not applied - the operator/builder set that exact sat value on purpose.
    """
    runner = _make_runner_with_balance(99_999_999)
    phase = TakerCoinjoinPhase(
        index=0,
        mixdepth=0,
        amount=13_256_421,
        counterparty_count=5,
        destination="INTERNAL",
        rounding_sigfigs=2,
    )
    assert asyncio.run(runner._resolve_amount(phase)) == 13_256_421
