"""Tests for :mod:`tumbler.builder`."""

from __future__ import annotations

import pytest

from tumbler.builder import INTERNAL_DESTINATION, PlanBuilder, TumbleParameters
from tumbler.plan import (
    BondlessTakerBurstPhase,
    MakerSessionPhase,
    PhaseKind,
    TakerCoinjoinPhase,
)


def _params(
    *, destinations: list[str] | None = None, seed: int | None = 42, **overrides: object
) -> TumbleParameters:
    kwargs: dict[str, object] = dict(
        destinations=destinations or ["bcrt1qdest000000000000000000000000000000000000"],
        mixdepth_balances={0: 10_000_000, 1: 5_000_000, 2: 0, 3: 0, 4: 0},
        seed=seed,
    )
    kwargs.update(overrides)
    return TumbleParameters(**kwargs)  # type: ignore[arg-type]


class TestPlanBuilder:
    def test_emits_stage1_sweeps_for_nonempty_mixdepths_only(self) -> None:
        plan = PlanBuilder("w", _params()).build()
        stage1 = [
            phase
            for phase in plan.phases[:2]
            if isinstance(phase, TakerCoinjoinPhase) and phase.is_sweep
        ]
        internal_sweeps = [p for p in stage1 if p.destination == INTERNAL_DESTINATION]
        assert {p.mixdepth for p in internal_sweeps} == {0, 1}

    def test_phase_indices_are_dense(self) -> None:
        plan = PlanBuilder("w", _params()).build()
        assert [p.index for p in plan.phases] == list(range(len(plan.phases)))

    def test_deterministic_with_seed(self) -> None:
        plan_a = PlanBuilder("w", _params(seed=123)).build()
        plan_b = PlanBuilder("w", _params(seed=123)).build()
        assert [p.kind for p in plan_a.phases] == [p.kind for p in plan_b.phases]
        assert [p.wait_seconds for p in plan_a.phases] == [p.wait_seconds for p in plan_b.phases]

    def test_destinations_land_in_distinct_mixdepths(self) -> None:
        params = _params(
            destinations=[
                "bcrt1qdest0000000000000000000000000000000000aaa",
                "bcrt1qdest0000000000000000000000000000000000bbb",
            ]
        )
        plan = PlanBuilder("w", params).build()
        final_sweeps = [
            p
            for p in plan.phases
            if isinstance(p, TakerCoinjoinPhase)
            and p.destination.startswith("bcrt1q")
            and p.is_sweep
        ]
        assert len({p.mixdepth for p in final_sweeps}) == 2

    def test_maker_and_bondless_are_optional(self) -> None:
        params = _params(include_maker_sessions=False, include_bondless_bursts=False)
        plan = PlanBuilder("w", params).build()
        assert not any(isinstance(p, MakerSessionPhase) for p in plan.phases)
        assert not any(isinstance(p, BondlessTakerBurstPhase) for p in plan.phases)

    def test_maker_session_present_before_destination_sweep_by_default(self) -> None:
        plan = PlanBuilder("w", _params()).build()
        # The first MakerSessionPhase must appear before the last TakerCoinjoinPhase.
        maker_idx = next(i for i, p in enumerate(plan.phases) if isinstance(p, MakerSessionPhase))
        last_sweep_idx = max(
            i
            for i, p in enumerate(plan.phases)
            if isinstance(p, TakerCoinjoinPhase) and p.destination != INTERNAL_DESTINATION
        )
        assert maker_idx < last_sweep_idx

    def test_maker_session_idle_timeout_is_plumbed_through(self) -> None:
        params = _params(maker_session_idle_timeout_seconds=5.0)
        plan = PlanBuilder("w", params).build()
        maker_phases = [p for p in plan.phases if isinstance(p, MakerSessionPhase)]
        assert maker_phases, "builder should emit at least one maker phase"
        for mp in maker_phases:
            assert mp.idle_timeout_seconds == 5.0

    def test_maker_session_idle_timeout_defaults_to_none(self) -> None:
        plan = PlanBuilder("w", _params()).build()
        maker_phases = [p for p in plan.phases if isinstance(p, MakerSessionPhase)]
        assert maker_phases
        for mp in maker_phases:
            assert mp.idle_timeout_seconds is None

    def test_allows_more_destinations_than_nonempty_mixdepths(self) -> None:
        params = _params(
            destinations=["a", "b", "c"],
            mixdepth_balances={0: 1_000_000, 1: 0, 2: 0, 3: 0, 4: 0},
        )
        plan = PlanBuilder("w", params).build()
        destination_sweeps = [
            phase
            for phase in plan.phases
            if isinstance(phase, TakerCoinjoinPhase)
            and phase.is_sweep
            and phase.destination in params.destinations
        ]
        assert len(destination_sweeps) == 3

    def test_allows_multiple_destinations_from_single_funded_mixdepth(self) -> None:
        params = _params(
            destinations=[
                "bcrt1qdest0000000000000000000000000000000000aaa",
                "bcrt1qdest0000000000000000000000000000000000bbb",
            ],
            mixdepth_balances={0: 0, 1: 23_430_165, 2: 0, 3: 0, 4: 0},
            include_maker_sessions=False,
            include_bondless_bursts=False,
        )
        plan = PlanBuilder("w", params).build()

        destination_sweeps = [
            phase
            for phase in plan.phases
            if isinstance(phase, TakerCoinjoinPhase)
            and phase.is_sweep
            and phase.destination in params.destinations
        ]
        assert [phase.mixdepth for phase in destination_sweeps] == [4, 0]
        assert [phase.destination for phase in destination_sweeps] == params.destinations

    def test_rejects_more_destinations_than_stage2_chain_mixdepths(self) -> None:
        params = _params(
            destinations=["a", "b", "c", "d", "e"],
            mixdepth_balances={0: 0, 1: 23_430_165, 2: 0, 3: 0, 4: 0},
        )
        with pytest.raises(ValueError, match="usable mixdepths"):
            PlanBuilder("w", params).build()

    def test_rejects_empty_destinations(self) -> None:
        with pytest.raises(ValueError):
            PlanBuilder(
                "w",
                TumbleParameters(destinations=[], mixdepth_balances={0: 1_000_000}),
            ).build()

    def test_kinds_present_cover_all_three(self) -> None:
        kinds = {p.kind for p in PlanBuilder("w", _params()).build().phases}
        assert PhaseKind.TAKER_COINJOIN in kinds
        assert PhaseKind.MAKER_SESSION in kinds
        assert PhaseKind.BONDLESS_TAKER_BURST in kinds

    def test_last_phase_has_zero_wait(self) -> None:
        plan = PlanBuilder("w", _params()).build()
        assert plan.phases[-1].wait_seconds == 0.0
