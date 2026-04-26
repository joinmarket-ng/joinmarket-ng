"""
Tumbler plan data model.

A ``Plan`` is an ordered list of ``Phase`` objects. Each phase is one of:

* :class:`TakerCoinjoinPhase` - a single taker CoinJoin (optionally sweep).
* :class:`MakerSessionPhase` - run a maker bot for a bounded time or
  until a target number of CoinJoins complete.

The plan and its phases form the single source of truth for a running
tumble. Progress is persisted to a YAML file (see :mod:`tumbler.persistence`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class PhaseKind(StrEnum):
    """Discriminator for the phase variants."""

    TAKER_COINJOIN = "taker_coinjoin"
    MAKER_SESSION = "maker_session"


class PhaseStatus(StrEnum):
    """Lifecycle of an individual phase."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanStatus(StrEnum):
    """Lifecycle of the overall plan."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Recommended minimum number of external destination addresses for a plan.
#
# Three destinations guarantee that the final funds cannot be trivially
# re-aggregated by correlating two sweeps: with only two destinations, an
# observer who identifies one recipient can deduce the other. Three breaks
# pairwise identifiability and matches the reference tumbler's recommendation.
#
# This is only enforced at the CLI boundary; library consumers (the
# ``jmwalletd`` API, tests, development tooling, JAM v2 which sends to a
# single address) may pass fewer destinations. Protocol-level validation
# only requires ``>= 1`` here.
MIN_DESTINATIONS = 3


class _PhaseBase(BaseModel):
    """Fields shared by every phase variant."""

    index: int = Field(..., ge=0, description="Zero-based position within the plan.")
    status: PhaseStatus = PhaseStatus.PENDING
    wait_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description="Delay to sleep after this phase completes, before the next starts.",
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    # Retry bookkeeping (taker phases only use this; maker phases ignore it).
    attempt_count: int = Field(
        default=0,
        ge=0,
        description="Number of attempts made for this phase (for retry tracking).",
    )


class TakerCoinjoinPhase(_PhaseBase):
    """A single taker CoinJoin."""

    kind: Literal[PhaseKind.TAKER_COINJOIN] = PhaseKind.TAKER_COINJOIN
    mixdepth: int = Field(..., ge=0, le=9)
    # Exactly one of amount / amount_fraction must be set (validated below).
    amount: int | None = Field(default=None, ge=0)
    amount_fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    counterparty_count: int = Field(..., ge=1, le=20)
    destination: str = Field(
        ...,
        description="A bitcoin address, or the sentinel 'INTERNAL' to pick the "
        "next mixdepth's internal address at execution time.",
    )
    txid: str | None = Field(
        default=None, description="Broadcast txid, set once the CoinJoin confirms."
    )
    rounding_sigfigs: int | None = Field(
        default=None,
        ge=1,
        le=8,
        description=(
            "If set, round the resolved sat amount to this many significant "
            "figures before dispatching to the taker. Mirrors the reference "
            "implementation's ``rounding`` schedule entry: a sub-BTC amount "
            "like 0.13256 BTC rounded to 2 sigfigs becomes 0.13 BTC, which "
            "obfuscates the relationship between the wallet balance and the "
            "CoinJoin amount. Sweeps (amount==0 / amount_fraction==0) ignore "
            "this field."
        ),
    )

    @model_validator(mode="after")
    def _validate_amount(self) -> TakerCoinjoinPhase:
        if self.amount is None and self.amount_fraction is None:
            raise ValueError("TakerCoinjoinPhase requires 'amount' or 'amount_fraction'")
        if self.amount is not None and self.amount_fraction is not None:
            raise ValueError("TakerCoinjoinPhase must not set both 'amount' and 'amount_fraction'")
        return self

    @property
    def is_sweep(self) -> bool:
        """A sweep empties the mixdepth: amount==0 or amount_fraction==0."""
        return (self.amount == 0) or (self.amount_fraction == 0.0)


class MakerSessionPhase(_PhaseBase):
    """
    Run a maker bot as part of the tumble.

    The session ends when any configured bound is reached: ``duration_seconds``
    elapses, ``target_cj_count`` CoinJoins have been served, or no CoinJoin has
    been served for ``idle_timeout_seconds`` (whichever comes first). At least
    one of ``duration_seconds`` or ``target_cj_count`` must be set;
    ``idle_timeout_seconds`` is an optional safety fallback so the phase does
    not hang forever when the maker is never chosen as counterparty.
    """

    kind: Literal[PhaseKind.MAKER_SESSION] = PhaseKind.MAKER_SESSION
    duration_seconds: float | None = Field(default=None, gt=0.0)
    target_cj_count: int | None = Field(default=None, ge=1)
    idle_timeout_seconds: float | None = Field(default=None, gt=0.0)
    cj_served: int = Field(default=0, ge=0, description="CoinJoins served so far.")

    @model_validator(mode="after")
    def _validate_bound(self) -> MakerSessionPhase:
        if self.duration_seconds is None and self.target_cj_count is None:
            raise ValueError("MakerSessionPhase requires 'duration_seconds' or 'target_cj_count'")
        return self


Phase = Annotated[
    TakerCoinjoinPhase | MakerSessionPhase,
    Field(discriminator="kind"),
]


class PlanParameters(BaseModel):
    """
    User-facing knobs captured for audit and resume. The builder records
    what it was told; the runner does not re-derive phases from these.
    """

    maker_count_min: int = Field(default=5, ge=1, le=20)
    maker_count_max: int = Field(default=9, ge=1, le=20)
    time_lambda_seconds: float = Field(default=6.0 * 60.0 * 60.0, gt=0.0)
    include_maker_sessions: bool = True
    mincjamount_sats: int = Field(default=100_000, ge=0)
    max_phase_retries: int = Field(
        default=3,
        ge=0,
        le=20,
        description="Maximum number of re-tries for a failed taker CoinJoin phase. "
        "Exhausting retries fails the entire plan.",
    )
    seed: int | None = None

    @model_validator(mode="after")
    def _validate_maker_count(self) -> PlanParameters:
        if self.maker_count_max < self.maker_count_min:
            raise ValueError("maker_count_max must be >= maker_count_min")
        return self


class Plan(BaseModel):
    """A tumble plan with per-phase progress tracking."""

    plan_id: str = Field(default_factory=lambda: uuid4().hex)
    wallet_name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: PlanStatus = PlanStatus.PENDING
    destinations: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "External destination addresses. "
            f"The CLI enforces at least {MIN_DESTINATIONS} to avoid pairwise "
            "re-aggregation heuristics; library consumers may pass fewer."
        ),
    )
    parameters: PlanParameters = Field(default_factory=PlanParameters)
    phases: list[Phase] = Field(default_factory=list)
    current_phase: int = Field(
        default=0,
        ge=0,
        description="Index of the next phase to run (0 == plan not started).",
    )
    error: str | None = None

    @model_validator(mode="after")
    def _validate_phase_indices(self) -> Plan:
        for i, phase in enumerate(self.phases):
            if phase.index != i:
                raise ValueError(
                    f"phases[{i}].index must equal its list position (got {phase.index})"
                )
        if self.current_phase > len(self.phases):
            raise ValueError("current_phase exceeds number of phases")
        return self

    def current(self) -> Phase | None:
        """Return the phase at ``current_phase``, or ``None`` if the plan is done."""
        if self.current_phase >= len(self.phases):
            return None
        return self.phases[self.current_phase]

    def touch(self) -> None:
        """Update ``updated_at`` to now (UTC)."""
        self.updated_at = datetime.now(UTC)


def round_to_significant_figures(value: int, sigfigs: int) -> int:
    """Round ``value`` to ``sigfigs`` significant figures in base 10.

    Mirrors ``round_to_significant_figures`` in the reference
    ``jmclient.taker``: the smallest power of ten greater than ``value`` is
    used as the scale, then the value is rounded to ``sigfigs`` sigfigs
    around it. Examples (``sigfigs=2``)::

        13_256_421 -> 13_000_000
        9_876      -> 9_900
        1_000_000  -> 1_000_000
        0          -> 0

    Raises ``ValueError`` if ``value`` is negative or ``sigfigs`` is not in
    ``[1, 8]`` (matching the model bounds).
    """
    if value < 0:
        raise ValueError("round_to_significant_figures requires a non-negative value")
    if not 1 <= sigfigs <= 8:
        raise ValueError("sigfigs must be in [1, 8]")
    if value == 0:
        return 0
    for p in range(-10, 20):
        power10 = 10**p
        if power10 > value:
            sf_power10 = 10**sigfigs
            return int(round(value / power10 * sf_power10) * power10 / sf_power10)
    raise RuntimeError("round_to_significant_figures: value out of range")
