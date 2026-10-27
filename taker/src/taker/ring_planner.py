"""Strict co-funded channel-ring planner for JMP-0014."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol, TypeVar

from jmcore.cofunded_ring import MAX_RING_PARTICIPANTS, MIN_RING_PARTICIPANTS
from jmcore.constants import MAX_MONEY

DEFAULT_MAX_ATTEMPTS = 50_000
TAKER_PARTICIPANT_ID = "taker"


class RingPlanningError(ValueError):
    """Raised when no complete feasible co-funded cycle can be produced."""


class RingRandom(Protocol):
    def shuffle(self, x: list[object]) -> None: ...

    def randrange(self, start: int, stop: int | None = None) -> int: ...


T = TypeVar("T")


@dataclass(frozen=True)
class ParticipantChannelLimits:
    """Final per-party backend and balance requirements used by the planner."""

    min_channel_capacity: int
    max_channel_capacity: int
    max_push_amount: int
    dust_limit: int
    outgoing_reserve: int
    incoming_reserve: int
    commitment_fee: int
    channel_overhead: int
    spendable_margin: int

    def __post_init__(self) -> None:
        amount_fields = (
            self.min_channel_capacity,
            self.max_channel_capacity,
            self.max_push_amount,
            self.dust_limit,
            self.outgoing_reserve,
            self.incoming_reserve,
            self.commitment_fee,
            self.channel_overhead,
            self.spendable_margin,
        )
        if any(type(value) is not int for value in amount_fields):
            raise RingPlanningError("participant channel limits must be integer satoshi amounts")
        if self.min_channel_capacity < 1 or self.max_channel_capacity > MAX_MONEY:
            raise RingPlanningError("channel capacity limits are outside Bitcoin money bounds")
        if self.min_channel_capacity > self.max_channel_capacity:
            raise RingPlanningError("minimum channel capacity exceeds maximum")
        if not 1 <= self.max_push_amount <= MAX_MONEY:
            raise RingPlanningError("maximum push amount is outside Bitcoin money bounds")
        if any(value < 0 or value > MAX_MONEY for value in amount_fields[3:]):
            raise RingPlanningError("channel balance limit is outside Bitcoin money bounds")

    @property
    def minimum_outgoing(self) -> int:
        return max(
            self.dust_limit + 1,
            self.outgoing_reserve
            + self.commitment_fee
            + self.channel_overhead
            + self.spendable_margin,
        )

    @property
    def minimum_incoming(self) -> int:
        return max(
            self.dust_limit + 1,
            self.incoming_reserve + self.spendable_margin,
        )


@dataclass(frozen=True)
class RingParty:
    party_id: str
    residual: int
    limits: ParticipantChannelLimits

    def __post_init__(self) -> None:
        if not self.party_id or len(self.party_id) > 128:
            raise RingPlanningError("party_id must contain 1 through 128 characters")
        if type(self.residual) is not int or not 1 <= self.residual <= MAX_MONEY:
            raise RingPlanningError(f"party {self.party_id!r} has residual outside money bounds")


@dataclass(frozen=True)
class PlannedContribution:
    party_id: str
    residual: int
    outgoing: int
    incoming: int


@dataclass(frozen=True)
class PlannedRingEdge:
    edge_id: str
    opener_id: str
    fundee_id: str
    capacity: int
    push_amount: int
    opener_contribution: int
    fundee_contribution: int


@dataclass(frozen=True)
class CofundedRingPlan:
    """One complete directed cycle with private endpoint contributions."""

    cycle: tuple[str, ...]
    contributions: tuple[PlannedContribution, ...]
    edges: tuple[PlannedRingEdge, ...]

    @property
    def residuals(self) -> dict[str, int]:
        return {contribution.party_id: contribution.residual for contribution in self.contributions}


def _shuffled(values: list[T], rng: RingRandom) -> list[T]:
    shuffled = values.copy()
    # The protocol only needs shuffle/randrange; the cast-free object list in the
    # protocol keeps deterministic test RNGs and SystemRandom equally usable.
    rng.shuffle(shuffled)  # type: ignore[arg-type]
    return shuffled


def _outgoing_bounds(party: RingParty) -> tuple[int, int]:
    return (
        party.limits.minimum_outgoing,
        party.residual - party.limits.minimum_incoming,
    )


def _capacity_bounds(
    parties: list[RingParty],
) -> dict[tuple[str, str], tuple[int, int]]:
    """Tighten capacities using constraints shared by every feasible cycle."""
    bounds: dict[tuple[str, str], tuple[int, int]] = {}
    row_min: dict[str, int] = {}
    row_max: dict[str, int] = {}
    for opener in parties:
        outgoing_lower, outgoing_upper = _outgoing_bounds(opener)
        for fundee in parties:
            if opener.party_id == fundee.party_id:
                continue
            maximum_push = min(opener.limits.max_push_amount, fundee.limits.max_push_amount)
            if maximum_push < fundee.limits.minimum_incoming:
                continue
            lower = max(
                opener.limits.min_channel_capacity,
                fundee.limits.min_channel_capacity,
                outgoing_lower + fundee.limits.minimum_incoming,
            )
            upper = min(
                opener.limits.max_channel_capacity,
                fundee.limits.max_channel_capacity,
                outgoing_upper
                + min(fundee.residual - fundee.limits.minimum_outgoing, maximum_push),
            )
            if lower <= upper:
                bounds[opener.party_id, fundee.party_id] = (lower, upper)
        outgoing = [value for (owner, _), value in bounds.items() if owner == opener.party_id]
        if not outgoing:
            raise RingPlanningError("no complete feasible cycle: participant cannot open a channel")
        row_min[opener.party_id] = min(lower for lower, _ in outgoing)
        row_max[opener.party_id] = max(upper for _, upper in outgoing)

    # Every cycle has one edge per opener, and its capacities sum to the
    # residual total. These bounds hold for every ordering, not just one cycle.
    total = sum(party.residual for party in parties)
    total_min, total_max = sum(row_min.values()), sum(row_max.values())
    bounds = {
        (opener, fundee): (
            max(lower, total - (total_max - row_max[opener])),
            min(upper, total - (total_min - row_min[opener])),
        )
        for (opener, fundee), (lower, upper) in bounds.items()
    }
    bounds = {edge: interval for edge, interval in bounds.items() if interval[0] <= interval[1]}

    return bounds


def _build_candidate(
    cycle: list[RingParty],
    rng: RingRandom,
    capacity_bounds: dict[tuple[str, str], tuple[int, int]],
) -> CofundedRingPlan | None:
    count = len(cycle)
    candidate_intervals = [
        capacity_bounds.get((opener.party_id, cycle[(index + 1) % count].party_id))
        for index, opener in enumerate(cycle)
    ]
    if any(edge is None for edge in candidate_intervals):
        return None
    intervals = [edge for edge in candidate_intervals if edge is not None]
    total = sum(party.residual for party in cycle)
    total_min = sum(lower for lower, _ in intervals)
    total_max = sum(upper for _, upper in intervals)
    intervals = [
        (
            max(lower, total - (total_max - upper)),
            min(upper, total - (total_min - lower)),
        )
        for lower, upper in intervals
    ]
    if any(lower > upper for lower, upper in intervals):
        return None

    first_lower, first_upper = _outgoing_bounds(cycle[0])
    outgoing = [rng.randrange(first_lower, first_upper + 1)]
    proposal_weight = 1
    rejection_bound = 1
    for index in range(len(cycle) - 1):
        opener = cycle[index]
        fundee = cycle[index + 1]
        fundee_lower, fundee_upper = _outgoing_bounds(fundee)
        minimum_capacity, maximum_capacity = intervals[index]
        maximum_push = min(opener.limits.max_push_amount, fundee.limits.max_push_amount)
        lower = max(
            fundee_lower,
            outgoing[index] + fundee.residual - maximum_capacity,
            fundee.residual - maximum_push,
        )
        upper = min(
            fundee_upper,
            outgoing[index] + fundee.residual - minimum_capacity,
        )
        if lower > upper:
            return None
        proposal_weight *= upper - lower + 1
        rejection_bound *= min(
            fundee_upper - fundee_lower + 1,
            maximum_capacity - minimum_capacity + 1,
            maximum_push - fundee.limits.minimum_incoming + 1,
        )
        outgoing.append(rng.randrange(lower, upper + 1))

    closing_capacity = outgoing[-1] + cycle[0].residual - outgoing[0]
    if not intervals[-1][0] <= closing_capacity <= intervals[-1][1] or cycle[0].residual - outgoing[
        0
    ] > min(cycle[-1].limits.max_push_amount, cycle[0].limits.max_push_amount):
        return None
    if not 1 <= proposal_weight <= rejection_bound:
        raise RingPlanningError("ring proposal exceeds its uniform sampling envelope")
    # The conditional proposal has mass 1/(first_width * proposal_weight).
    # This exact integer rejection removes its interval-width bias. The
    # envelope is constant within this cycle, not across different cycles.
    if rng.randrange(rejection_bound) >= proposal_weight:
        return None

    # Anchoring removes duplicate representations of physical cycles during
    # sampling, but the transmitted position must not identify that anchor.
    offset = rng.randrange(count)
    cycle = cycle[offset:] + cycle[:offset]
    outgoing = outgoing[offset:] + outgoing[:offset]
    contributions = [
        PlannedContribution(
            party_id=party.party_id,
            residual=party.residual,
            outgoing=outgoing[index],
            incoming=party.residual - outgoing[index],
        )
        for index, party in enumerate(cycle)
    ]
    edges: list[PlannedRingEdge] = []
    for index, opener in enumerate(cycle):
        fundee_index = (index + 1) % count
        fundee = cycle[fundee_index]
        opener_contribution = contributions[index].outgoing
        fundee_contribution = contributions[fundee_index].incoming
        capacity = opener_contribution + fundee_contribution
        minimum_capacity = max(
            opener.limits.min_channel_capacity,
            fundee.limits.min_channel_capacity,
        )
        maximum_capacity = min(
            opener.limits.max_channel_capacity,
            fundee.limits.max_channel_capacity,
        )
        maximum_push = min(opener.limits.max_push_amount, fundee.limits.max_push_amount)
        if (
            minimum_capacity > maximum_capacity
            or not minimum_capacity <= capacity <= maximum_capacity
            or fundee_contribution > maximum_push
            or capacity <= opener_contribution
            or capacity <= fundee_contribution
        ):
            return None
        edges.append(
            PlannedRingEdge(
                edge_id=f"edge-{index}",
                opener_id=opener.party_id,
                fundee_id=fundee.party_id,
                capacity=capacity,
                push_amount=fundee_contribution,
                opener_contribution=opener_contribution,
                fundee_contribution=fundee_contribution,
            )
        )
    return CofundedRingPlan(
        cycle=tuple(party.party_id for party in cycle),
        contributions=tuple(contributions),
        edges=tuple(edges),
    )


def plan_cofunded_ring(
    parties: list[RingParty],
    *,
    taker_id: str = TAKER_PARTICIPANT_ID,
    taker_participates: bool = True,
    rng: RingRandom | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> CofundedRingPlan:
    """Plan all parties with uniform integer splits conditional on the cycle.

    Cycles are proposed uniformly, but acceptance weights their marginal by
    feasible split count divided by the cycle's rejection envelope. This is not
    joint-uniform sampling over cycles and splits or a partition-entropy claim.
    """

    if type(taker_participates) is not bool:
        raise RingPlanningError("taker_participates must be a boolean")
    if not MIN_RING_PARTICIPANTS <= len(parties) <= MAX_RING_PARTICIPANTS:
        raise RingPlanningError(
            f"co-funded ring requires {MIN_RING_PARTICIPANTS} through "
            f"{MAX_RING_PARTICIPANTS} channel participants"
        )
    party_ids = [party.party_id for party in parties]
    if len(set(party_ids)) != len(party_ids):
        raise RingPlanningError("co-funded ring contains duplicate party IDs")
    if party_ids.count(taker_id) != int(taker_participates):
        raise RingPlanningError(
            f"co-funded ring must contain reserved taker ID {taker_id!r} "
            f"{'exactly once' if taker_participates else 'zero times'}"
        )
    if type(max_attempts) is not int or max_attempts < 1 or max_attempts > 1_000_000:
        raise RingPlanningError("max_attempts must be an integer from 1 through 1000000")
    if sum(party.residual for party in parties) > MAX_MONEY:
        raise RingPlanningError("combined ring residuals exceed MAX_MONEY")

    for party in parties:
        required = party.limits.minimum_outgoing + party.limits.minimum_incoming
        if party.residual < required:
            raise RingPlanningError(
                f"party {party.party_id!r} residual {party.residual} is below its "
                f"contribution floor {required}"
            )
        if party.limits.min_channel_capacity > party.limits.max_channel_capacity:
            raise RingPlanningError(f"party {party.party_id!r} has inconsistent channel limits")

    ordered = sorted(parties, key=lambda party: party.party_id)
    capacity_bounds = _capacity_bounds(ordered)
    source = secrets.SystemRandom() if rng is None else rng
    for _ in range(max_attempts):
        cycle = [ordered[0], *_shuffled(ordered[1:], source)]
        candidate = _build_candidate(cycle, source, capacity_bounds)
        if candidate is not None:
            return candidate

    raise RingPlanningError(
        f"no complete feasible co-funded cycle found for {len(parties)} parties "
        f"after {max_attempts} bounded attempts"
    )


# Concise primary API name for callers already scoped to this module.
plan_ring = plan_cofunded_ring
