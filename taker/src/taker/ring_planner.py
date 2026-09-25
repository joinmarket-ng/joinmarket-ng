"""Strict co-funded channel-ring planner for JMP-0010."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol, TypeVar

from jmcore.constants import MAX_MONEY

MIN_RING_PARTICIPANTS = 4
MAX_RING_PARTICIPANTS = 32
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


def _build_candidate(
    cycle: list[RingParty],
    rng: RingRandom,
) -> CofundedRingPlan | None:
    first_lower, first_upper = _outgoing_bounds(cycle[0])
    outgoing = [rng.randrange(first_lower, first_upper + 1)]
    for index in range(len(cycle) - 1):
        opener = cycle[index]
        fundee = cycle[index + 1]
        fundee_lower, fundee_upper = _outgoing_bounds(fundee)
        minimum_capacity = max(
            opener.limits.min_channel_capacity,
            fundee.limits.min_channel_capacity,
        )
        maximum_capacity = min(
            opener.limits.max_channel_capacity,
            fundee.limits.max_channel_capacity,
        )
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
        outgoing.append(rng.randrange(lower, upper + 1))

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
    count = len(cycle)
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
    rng: RingRandom | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> CofundedRingPlan:
    """Plan all supplied parties or raise without returning a partial result."""

    if not MIN_RING_PARTICIPANTS <= len(parties) <= MAX_RING_PARTICIPANTS:
        raise RingPlanningError(
            f"co-funded ring requires {MIN_RING_PARTICIPANTS} through "
            f"{MAX_RING_PARTICIPANTS} parties including the taker"
        )
    party_ids = [party.party_id for party in parties]
    if len(set(party_ids)) != len(party_ids):
        raise RingPlanningError("co-funded ring contains duplicate party IDs")
    if party_ids.count(taker_id) != 1:
        raise RingPlanningError(
            f"co-funded ring must contain reserved taker ID {taker_id!r} exactly once"
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

    source = secrets.SystemRandom() if rng is None else rng
    for _ in range(max_attempts):
        cycle = _shuffled(parties, source)
        candidate = _build_candidate(cycle, source)
        if candidate is not None:
            return candidate

    raise RingPlanningError(
        f"no complete feasible co-funded cycle found for {len(parties)} parties "
        f"after {max_attempts} bounded attempts"
    )


# Concise primary API name for callers already scoped to this module.
plan_ring = plan_cofunded_ring
