from __future__ import annotations

import random
import secrets
from collections import defaultdict
from dataclasses import replace
from fractions import Fraction
from itertools import permutations, product
from math import factorial, prod

import pytest

from taker.ring_planner import (
    CofundedRingPlan,
    ParticipantChannelLimits,
    RingParty,
    RingPlanningError,
    _build_candidate,
    _capacity_bounds,
    plan_cofunded_ring,
)


def _limits(
    *,
    minimum: int = 20_000,
    maximum: int = 2_000_000,
    max_push: int = 1_000_000,
    dust: int = 354,
    outgoing_reserve: int = 1_000,
    incoming_reserve: int = 1_000,
    commitment_fee: int = 200,
    overhead: int = 100,
    margin: int = 1_000,
) -> ParticipantChannelLimits:
    return ParticipantChannelLimits(
        min_channel_capacity=minimum,
        max_channel_capacity=maximum,
        max_push_amount=max_push,
        dust_limit=dust,
        outgoing_reserve=outgoing_reserve,
        incoming_reserve=incoming_reserve,
        commitment_fee=commitment_fee,
        channel_overhead=overhead,
        spendable_margin=margin,
    )


def _parties(
    residuals: list[int], limits: ParticipantChannelLimits | None = None
) -> list[RingParty]:
    selected_limits = limits or _limits()
    identifiers = ["taker", *(f"maker-{index}" for index in range(1, len(residuals)))]
    return [
        RingParty(party_id=party_id, residual=residual, limits=selected_limits)
        for party_id, residual in zip(identifiers, residuals, strict=True)
    ]


def _assert_complete(plan: CofundedRingPlan, parties: list[RingParty]) -> None:
    party_by_id = {party.party_id: party for party in parties}
    assert set(plan.cycle) == set(party_by_id)
    assert len(plan.cycle) == len(plan.edges) == len(plan.contributions)
    assert len(set(plan.cycle)) == len(plan.cycle)
    contributions = {contribution.party_id: contribution for contribution in plan.contributions}
    assert set(contributions) == set(party_by_id)

    opened: set[str] = set()
    accepted: set[str] = set()
    for index, edge in enumerate(plan.edges):
        assert edge.opener_id == plan.cycle[index]
        assert edge.fundee_id == plan.cycle[(index + 1) % len(plan.cycle)]
        assert edge.opener_id not in opened
        assert edge.fundee_id not in accepted
        opened.add(edge.opener_id)
        accepted.add(edge.fundee_id)
        assert edge.opener_contribution > 0
        assert edge.fundee_contribution > 0
        assert edge.capacity == edge.opener_contribution + edge.fundee_contribution
        assert edge.push_amount == edge.fundee_contribution
        opener_limits = party_by_id[edge.opener_id].limits
        fundee_limits = party_by_id[edge.fundee_id].limits
        assert (
            max(opener_limits.min_channel_capacity, fundee_limits.min_channel_capacity)
            <= edge.capacity
            <= min(opener_limits.max_channel_capacity, fundee_limits.max_channel_capacity)
        )
        assert edge.push_amount <= min(opener_limits.max_push_amount, fundee_limits.max_push_amount)

    assert opened == accepted == set(party_by_id)
    for party_id, contribution in contributions.items():
        limits = party_by_id[party_id].limits
        assert contribution.outgoing + contribution.incoming == contribution.residual
        assert contribution.residual == party_by_id[party_id].residual
        assert contribution.outgoing >= limits.minimum_outgoing
        assert contribution.incoming >= limits.minimum_incoming
    assert sum(edge.capacity for edge in plan.edges) == sum(party.residual for party in parties)


@pytest.mark.parametrize("seed", range(50))
def test_randomized_uneven_residuals_form_complete_cycles(seed: int) -> None:
    source = random.Random(seed)
    residuals = [source.randint(120_000, 900_000) for _ in range(source.randint(4, 12))]
    parties = _parties(residuals)
    plan = plan_cofunded_ring(parties, rng=random.Random(seed), max_attempts=20_000)
    _assert_complete(plan, parties)


def test_three_party_ring_can_include_taker_and_two_makers() -> None:
    parties = _parties([200_000, 240_000, 280_000, 320_000])
    plan = plan_cofunded_ring(parties[:3], rng=random.Random(7))
    _assert_complete(plan, parties[:3])
    assert "taker" in plan.cycle

    with pytest.raises(RingPlanningError, match="requires 3"):
        plan_cofunded_ring(parties[:2], rng=random.Random(7))


def test_three_maker_ring_allows_ordinary_taker_to_coordinate() -> None:
    parties = _parties([200_000, 240_000, 280_000, 320_000])
    makers = parties[1:]
    with pytest.raises(RingPlanningError, match="requires 3"):
        plan_cofunded_ring(makers[:2], taker_participates=False, rng=random.Random(7))
    three_maker_plan = plan_cofunded_ring(makers, taker_participates=False, rng=random.Random(7))
    _assert_complete(three_maker_plan, makers)
    assert "taker" not in three_maker_plan.cycle
    makers.append(type(parties[0])(party_id="maker-4", residual=360_000, limits=parties[0].limits))
    with pytest.raises(RingPlanningError, match="reserved taker ID"):
        plan_cofunded_ring(makers, rng=random.Random(7))
    planned = plan_cofunded_ring(makers, taker_participates=False, rng=random.Random(7))
    _assert_complete(planned, makers)
    assert "taker" not in planned.cycle
    with pytest.raises(RingPlanningError, match="zero times"):
        plan_cofunded_ring(parties, taker_participates=False, rng=random.Random(7))


def test_channel_overhead_is_charged_only_to_opener_floor() -> None:
    limits = _limits(
        minimum=197,
        maximum=197,
        max_push=97,
        dust=0,
        outgoing_reserve=90,
        incoming_reserve=95,
        commitment_fee=5,
        overhead=3,
        margin=2,
    )
    assert limits.minimum_outgoing == 100
    assert limits.minimum_incoming == 97
    parties = _parties([197, 197, 197, 197], limits)
    plan = plan_cofunded_ring(parties, rng=random.Random(3))
    _assert_complete(plan, parties)
    assert {item.outgoing for item in plan.contributions} == {100}
    assert {item.incoming for item in plan.contributions} == {97}
    assert {edge.capacity for edge in plan.edges} == {197}


def test_capacity_can_land_exactly_on_negotiated_maximum() -> None:
    limits = _limits(
        minimum=500_000,
        maximum=500_000,
        max_push=250_000,
        dust=0,
        outgoing_reserve=249_000,
        incoming_reserve=249_000,
        commitment_fee=500,
        overhead=250,
        margin=250,
    )
    parties = _parties([500_000] * 4, limits)
    plan = plan_cofunded_ring(parties, rng=random.Random(5))
    _assert_complete(plan, parties)
    assert all(edge.capacity == limits.max_channel_capacity for edge in plan.edges)


def test_near_limits_remain_reproducible_with_injected_rng() -> None:
    parties = _parties([201_000, 202_000, 203_000, 204_000], _limits(maximum=205_000))
    first = plan_cofunded_ring(parties, rng=random.Random(19), max_attempts=50_000)
    second = plan_cofunded_ring(parties, rng=random.Random(19), max_attempts=50_000)
    assert first == second


def test_default_rng_is_system_random(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    class TrackingSystemRandom(random.Random):
        def __init__(self) -> None:
            nonlocal calls
            calls += 1
            super().__init__(42)

    monkeypatch.setattr(secrets, "SystemRandom", TrackingSystemRandom)
    plan = plan_cofunded_ring(_parties([200_000] * 4))
    assert calls == 1
    assert len(plan.edges) == 4


def test_impossible_outlier_fails_without_partial_result() -> None:
    normal = _limits(
        minimum=200,
        maximum=1_000,
        max_push=500,
        dust=0,
        outgoing_reserve=90,
        incoming_reserve=95,
        commitment_fee=5,
        overhead=3,
        margin=2,
    )
    constrained = _limits(
        minimum=1,
        maximum=150,
        max_push=100,
        dust=0,
        outgoing_reserve=90,
        incoming_reserve=95,
        commitment_fee=5,
        overhead=3,
        margin=2,
    )
    parties = _parties([200, 200, 200, 200], normal)
    parties[-1] = RingParty("maker-3", 200, constrained)
    with pytest.raises(RingPlanningError, match="no complete feasible"):
        plan_cofunded_ring(parties, rng=random.Random(0), max_attempts=100)


@pytest.mark.parametrize(
    "parties,match",
    [
        (
            [
                RingParty("taker", 200_000, _limits()),
                RingParty("maker-1", 200_000, _limits()),
                RingParty("maker-1", 200_000, _limits()),
                RingParty("maker-3", 200_000, _limits()),
            ],
            "duplicate party IDs",
        ),
        (_parties([200_000] * 4)[1:] + [RingParty("maker-4", 200_000, _limits())], "taker ID"),
    ],
)
def test_duplicate_or_missing_reserved_party_rejected(parties: list[RingParty], match: str) -> None:
    with pytest.raises(RingPlanningError, match=match):
        plan_cofunded_ring(parties, rng=random.Random(0))


def test_residual_below_contribution_floors_rejected_early() -> None:
    limits = _limits()
    parties = _parties([limits.minimum_outgoing + limits.minimum_incoming - 1] * 4, limits)
    with pytest.raises(RingPlanningError, match="contribution floor"):
        plan_cofunded_ring(parties, rng=random.Random(0))


def test_combined_residuals_must_fit_bitcoin_money_supply() -> None:
    parties = _parties([600_000_000_000_000] * 4, _limits(maximum=2_100_000_000_000_000))
    with pytest.raises(RingPlanningError, match="MAX_MONEY"):
        plan_cofunded_ring(parties, rng=random.Random(0))


def test_many_randomized_limit_intersections() -> None:
    for seed in range(100, 150):
        source = random.Random(seed)
        parties: list[RingParty] = []
        count = source.randint(4, 10)
        for index in range(count):
            limits = _limits(
                minimum=source.randint(20_000, 60_000),
                maximum=source.randint(700_000, 1_500_000),
                max_push=source.randint(400_000, 700_000),
                margin=source.randint(500, 5_000),
            )
            party_id = "taker" if index == 0 else f"maker-{index}"
            parties.append(RingParty(party_id, source.randint(150_000, 600_000), limits))
        plan = plan_cofunded_ring(parties, rng=random.Random(seed), max_attempts=20_000)
        _assert_complete(plan, parties)


class ScriptedCandidateRandom:
    """Expose exact proposal probabilities without statistical assertions."""

    def __init__(self, outgoing: tuple[int, ...], *, reject: bool = False, offset: int = 0) -> None:
        self.outgoing = outgoing
        self.reject = reject
        self.offset = offset
        self.widths: list[int] = []
        self.envelope = 0

    def shuffle(self, x: list[object]) -> None:
        raise AssertionError("candidate sampling must not shuffle its fixed cycle")

    def randrange(self, start: int, stop: int | None = None) -> int:
        if stop is not None:
            value = self.outgoing[len(self.widths)]
            assert start <= value < stop
            self.widths.append(stop - start)
            return value
        if not self.envelope:
            self.envelope = start
            weight = prod(self.widths[1:])
            return weight if self.reject else weight - 1
        assert 0 <= self.offset < start
        return self.offset


def _feasible_splits(cycle: list[RingParty]) -> list[tuple[int, ...]]:
    feasible = []
    for outgoing in product(
        *(
            range(p.limits.minimum_outgoing, p.residual - p.limits.minimum_incoming + 1)
            for p in cycle
        )
    ):
        for index, opener in enumerate(cycle):
            successor = (index + 1) % len(cycle)
            fundee = cycle[successor]
            push = fundee.residual - outgoing[successor]
            capacity = outgoing[index] + push
            if not (
                max(opener.limits.min_channel_capacity, fundee.limits.min_channel_capacity)
                <= capacity
                <= min(opener.limits.max_channel_capacity, fundee.limits.max_channel_capacity)
                and push <= min(opener.limits.max_push_amount, fundee.limits.max_push_amount)
            ):
                break
        else:
            feasible.append(outgoing)
    return feasible


@pytest.mark.parametrize("heterogeneous", [False, True])
def test_exact_conditional_sampling_mass_and_complete_support(heterogeneous: bool) -> None:
    limits = _limits(
        minimum=5,
        maximum=12,
        max_push=10,
        dust=0,
        outgoing_reserve=2,
        incoming_reserve=1,
        commitment_fee=0,
        overhead=0,
        margin=1,
    )
    parties = sorted(_parties([8, 9, 10], limits), key=lambda party: party.party_id)
    if heterogeneous:
        parties[1] = replace(parties[1], limits=replace(limits, max_channel_capacity=10))
        parties[2] = replace(parties[2], limits=replace(limits, max_push_amount=5))
    bounds = _capacity_bounds(parties)
    weights: set[int] = set()
    supported_cycles = set()
    for tail in permutations(parties[1:]):
        cycle = [parties[0], *tail]
        masses: set[Fraction] = set()
        envelopes: set[int] = set()
        for outgoing in _feasible_splits(cycle):
            source = ScriptedCandidateRandom(outgoing)
            plan = _build_candidate(cycle, source, bounds)
            assert plan is not None
            _assert_complete(plan, parties)
            assert tuple(c.outgoing for c in plan.contributions) == outgoing
            envelopes.add(source.envelope)
            weight = prod(source.widths[1:])
            weights.add(weight)
            proposal = Fraction(1, factorial(len(parties) - 1) * prod(source.widths))
            masses.add(proposal * Fraction(weight, source.envelope))
            supported_cycles.add(plan.cycle)
        assert len(envelopes) == 1
        assert len(masses) == 1
    assert len(supported_cycles) == factorial(len(parties) - 1)
    assert len(weights) > 1  # This fixture catches the old interval-width bias.


def test_uniform_rejection_boundary_and_transmitted_rotation() -> None:
    limits = _limits(
        minimum=5,
        maximum=12,
        max_push=10,
        dust=0,
        outgoing_reserve=2,
        incoming_reserve=1,
        commitment_fee=0,
        overhead=0,
        margin=1,
    )
    parties = sorted(_parties([8, 9, 10], limits), key=lambda party: party.party_id)
    bounds = _capacity_bounds(parties)
    for outgoing in _feasible_splits(parties):
        source = ScriptedCandidateRandom(outgoing)
        accepted = _build_candidate(parties, source, bounds)
        assert accepted is not None  # The last accepted integer is weight - 1.
        if prod(source.widths[1:]) == source.envelope:
            continue
        assert (
            _build_candidate(parties, ScriptedCandidateRandom(outgoing, reject=True), bounds)
            is None
        )  # The next integer must reject, not bias toward narrow intervals.
        for offset in range(len(parties)):
            rotated = _build_candidate(
                parties, ScriptedCandidateRandom(outgoing, offset=offset), bounds
            )
            assert rotated is not None
            assert rotated.cycle == accepted.cycle[offset:] + accepted.cycle[:offset]
            assert rotated.contributions == (
                accepted.contributions[offset:] + accepted.contributions[:offset]
            )
            assert {(e.opener_id, e.fundee_id, e.capacity) for e in rotated.edges} == {
                (e.opener_id, e.fundee_id, e.capacity) for e in accepted.edges
            }
        break
    else:
        pytest.fail("fixture did not exercise a nontrivial rejection boundary")


def test_wide_push_allows_fundee_to_contribute_more_than_half_capacity() -> None:
    limits = _limits(
        minimum=1_000_000,
        maximum=20_000_000,
        max_push=20_000_000,
        outgoing_reserve=10_000,
        incoming_reserve=10_000,
        commitment_fee=100_000,
        overhead=660,
        margin=100_000,
    )
    parties = _parties([2_000_000, 3_000_000, 4_000_000], limits)
    plan = plan_cofunded_ring(parties, rng=random.Random(7))
    _assert_complete(plan, parties)
    assert any(edge.push_amount * 2 > edge.capacity for edge in plan.edges)


@pytest.mark.parametrize("seed", [0, 1, 2])
@pytest.mark.parametrize("tight_capacity", [False, True])
def test_mixed_push_limits_remain_feasible_without_global_envelope_slack(
    seed: int, tight_capacity: bool
) -> None:
    limits = _limits(
        minimum=1_000_000,
        maximum=20_000_000,
        max_push=20_000_000,
        outgoing_reserve=10_000,
        incoming_reserve=10_000,
        commitment_fee=100_000,
        overhead=660,
        margin=100_000,
    )
    parties = _parties([5_000_000] * 32, limits)
    for index, party in enumerate(parties):
        if index % 2 == 0:
            parties[index] = replace(party, limits=replace(limits, max_push_amount=500_000))
    if tight_capacity:
        parties[1] = replace(parties[1], limits=replace(limits, max_channel_capacity=5_005_000))
    plan = plan_cofunded_ring(parties, rng=random.Random(seed), max_attempts=200)
    _assert_complete(plan, parties)


@pytest.mark.parametrize(
    ("push", "incoming_floor", "guessing", "worst"),
    [
        (5, 2, Fraction(63, 128), Fraction(1, 2)),
        (24, 2, Fraction(164, 687), Fraction(2, 5)),
        (24, 3, Fraction(1118, 4497), Fraction(7, 16)),
    ],
)
def test_exact_toy_funding_partition_guessing_probability(
    push: int, incoming_floor: int, guessing: Fraction, worst: Fraction
) -> None:
    """Known single-input residuals, latent splits/cycle, random output slots."""
    limits = _limits(
        minimum=8,
        maximum=30,
        max_push=push,
        dust=0,
        outgoing_reserve=3,
        incoming_reserve=incoming_floor,
        commitment_fee=0,
        overhead=0,
        margin=0,
    )
    parties = [RingParty(str(i), residual, limits) for i, residual in enumerate([12, 18, 24])]
    bounds = _capacity_bounds(parties)
    joint: dict[tuple[int, ...], dict[tuple[tuple[str, ...], ...], Fraction]] = defaultdict(
        lambda: defaultdict(Fraction)
    )
    for tail in permutations(parties[1:]):
        cycle = [parties[0], *tail]
        for outgoing in _feasible_splits(cycle):
            source = ScriptedCandidateRandom(outgoing)
            plan = _build_candidate(cycle, source, bounds)
            assert plan is not None
            mass = Fraction(1, 2 * source.widths[0] * source.envelope)
            for slots in permutations(plan.edges):
                observation = tuple(edge.capacity for edge in slots)
                partition = tuple(tuple(sorted((edge.opener_id, edge.fundee_id))) for edge in slots)
                joint[observation][partition] += mass / 6
    accepted_mass = sum((sum(partitions.values()) for partitions in joint.values()), Fraction())
    assert (
        sum((max(partitions.values()) for partitions in joint.values()), Fraction()) / accepted_mass
        == guessing
    )
    assert (
        max(max(partitions.values()) / sum(partitions.values()) for partitions in joint.values())
        == worst
    )
