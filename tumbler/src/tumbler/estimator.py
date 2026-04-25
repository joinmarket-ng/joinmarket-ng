"""
Cost and time estimates for a tumble plan.

Surfaced by the CLI ``plan`` command (and reusable from the API) so users
can sanity-check the schedule before committing the wallet to it. None of
the figures here are guarantees: counterparty fees are bounded only by the
local ``max_cj_fee`` (the actual maker fee may be lower), miner-fee
estimates assume a typical sw0 CoinJoin tx size, and the duration estimate
is the sum of the persisted ``wait_seconds`` plus a per-phase budget.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from tumbler.plan import (
    MakerSessionPhase,
    Plan,
    TakerCoinjoinPhase,
)

# Typical per-coinjoin handshake budget (seconds): orderbook gather, choose
# orders, transaction signing, broadcast, and confirmation wait. Used as
# a coarse "min" lower bound on each taker phase; the actual time is
# dominated by ``wait_seconds`` between phases plus the inter-phase
# confirmation gate (``min_confirmations_between_phases`` * block time)
# which we surface separately so the user can scale it themselves.
_TAKER_PHASE_HANDSHAKE_SECONDS = 60.0

# Conservative default sat/vB used when neither fee_rate nor fee_block_target
# is configured. 10 sat/vB is comfortably above mainnet floor congestion and
# below typical fee-spike levels; the actual rate quoted by
# ``estimatesmartfee`` at run time will replace this.
_DEFAULT_FALLBACK_FEE_RATE_SAT_VB = 10.0

# Per-mixdepth fallback when we don't know the actual balance: zero, so the
# estimate stays a lower bound rather than fabricating numbers.
_UNKNOWN_BALANCE_FALLBACK_SATS = 0


def _vbytes_for_coinjoin(counterparty_count: int) -> int:
    """
    Approximate vbyte size for a sw0 CoinJoin transaction.

    Heuristic: one taker output + change + per-counterparty (input+output)
    pair. Numbers track roughly with measurements on regtest sw0
    transactions; precise sizes are wallet-dependent so the estimator is
    deliberately coarse.
    """
    fixed = 11  # version + locktime + segwit marker/flag + input/output counts
    # Taker contributes one input (~68 vb sw0 p2wpkh) + one CJ output + one
    # change output (~31 vb each).
    taker = 68 + 31 + 31
    # Each counterparty contributes one input + one CJ output + one change.
    per_party = 68 + 31 + 31
    return fixed + taker + per_party * max(counterparty_count, 0)


@dataclass
class PhaseCostEstimate:
    """Per-phase breakdown -- useful for tabular display."""

    index: int
    kind: str
    description: str
    max_cj_fee_sats: int
    miner_fee_sats: int
    duration_seconds_min: float
    duration_seconds_expected: float
    duration_seconds_max: float


@dataclass
class PlanEstimate:
    """Aggregate plan-level cost and time estimates."""

    taker_phase_count: int
    maker_phase_count: int
    total_balance_sats: int
    """Sum of ``mixdepth_balances`` at plan time, in sats."""
    mixdepth_balances: dict[int, int] = field(default_factory=dict)
    """Snapshot of per-mixdepth balances used for sizing phases."""
    total_max_cj_fee_sats: int = 0
    total_miner_fee_sats: int = 0
    total_wait_seconds: float = 0.0
    total_duration_seconds_min: float = 0.0
    total_duration_seconds_expected: float = 0.0
    total_duration_seconds_max: float = 0.0
    confirmation_block_count: int = 0
    fee_rate_sat_vb: float = 0.0
    fee_rate_source: str = "fallback"
    """One of ``configured`` (settings.taker.fee_rate set), ``estimated``
    (settings.taker.fee_block_target set; rate inferred at plan time), or
    ``fallback`` (neither set; using a conservative built-in default)."""
    phases: list[PhaseCostEstimate] = field(default_factory=list)

    @property
    def total_max_fee_sats(self) -> int:
        """Upper bound on total fees (CJ counterparty fees + miner fees)."""
        return self.total_max_cj_fee_sats + self.total_miner_fee_sats

    @property
    def total_max_cj_fee_pct(self) -> float:
        """CJ fee upper bound as a percentage of total balance."""
        if self.total_balance_sats <= 0:
            return 0.0
        return 100.0 * self.total_max_cj_fee_sats / self.total_balance_sats

    @property
    def total_miner_fee_pct(self) -> float:
        """Miner fee estimate as a percentage of total balance."""
        if self.total_balance_sats <= 0:
            return 0.0
        return 100.0 * self.total_miner_fee_sats / self.total_balance_sats

    @property
    def total_max_fee_pct(self) -> float:
        """Total upper-bound fees as a percentage of total balance."""
        if self.total_balance_sats <= 0:
            return 0.0
        return 100.0 * self.total_max_fee_sats / self.total_balance_sats


def estimate_plan_costs(
    plan: Plan,
    *,
    mixdepth_balances: Mapping[int, int] | None = None,
    max_cj_fee_abs_sats: int,
    max_cj_fee_rel: str | float,
    fee_rate_sat_vb: float | None = None,
    fee_rate_source: str | None = None,
    confirmation_block_count: int = 5,
    block_time_seconds: float = 600.0,
) -> PlanEstimate:
    """
    Compute upper-bound CJ fees and miner-fee/time estimates for ``plan``.

    Parameters
    ----------
    plan
        The tumble plan to inspect.
    mixdepth_balances
        Current confirmed balance per mixdepth in sats. Used to size sweep
        and fractional phases (which carry ``amount=0`` / ``amount_fraction``
        on the persisted phase). When ``None``, sweeps and fractional phases
        size to zero (lower bound).
    max_cj_fee_abs_sats, max_cj_fee_rel
        Local fee bounds taken from ``settings.taker.max_cj_fee``. The
        estimator uses ``max(abs_fee, rel_fee * amount)`` as the per-maker
        upper bound and multiplies by counterparty_count, mirroring how
        ``filter_offers`` rejects offers above either bound.
    fee_rate_sat_vb
        Miner-fee rate used per CJ tx. When ``None``, the estimator uses a
        conservative built-in default (10 sat/vB) and reports
        ``fee_rate_source='fallback'`` so the caller can label the figure
        as estimated. Always producing a number is intentional: an "n/a"
        column gives users no signal of the worst-case miner cost.
    fee_rate_source
        Optional label overriding the default classification of the fee
        rate (``configured`` / ``estimated`` / ``fallback``). Useful when
        the caller resolved a ``fee_block_target`` to a concrete rate
        upstream and wants the output labelled accordingly.
    confirmation_block_count
        ``RunnerContext.min_confirmations_between_phases`` (default 5).
        Plumbed into the duration estimate so the inter-phase wait for
        confirmations is not silently ignored.
    block_time_seconds
        Average inter-block time used for the confirmation wait. Defaults
        to mainnet's 600s; tests/regtest can pass a smaller value.
    """
    rel_fee = float(Decimal(str(max_cj_fee_rel)))
    balances: Mapping[int, int] = mixdepth_balances or {}
    total_balance = sum(max(int(v), 0) for v in balances.values())

    if fee_rate_sat_vb is None:
        effective_fee_rate = _DEFAULT_FALLBACK_FEE_RATE_SAT_VB
        effective_source = fee_rate_source or "fallback"
    else:
        effective_fee_rate = float(fee_rate_sat_vb)
        effective_source = fee_rate_source or "configured"

    phases: list[PhaseCostEstimate] = []
    total_cj_fee = 0
    total_miner_fee = 0
    total_wait = 0.0
    total_min = 0.0
    total_expected = 0.0
    total_max = 0.0
    taker_count = 0
    maker_count = 0

    # Track per-mixdepth running balance: sweeps drain to the next mixdepth,
    # fractional phases reduce the source mixdepth proportionally. The CLI
    # caller already snapshotted balances at plan time; we just simulate the
    # builder's amount resolution to size each phase realistically.
    running = dict(balances)

    for phase in plan.phases:
        wait = float(phase.wait_seconds)
        total_wait += wait

        if isinstance(phase, TakerCoinjoinPhase):
            taker_count += 1
            mixdepth = phase.mixdepth
            balance = running.get(mixdepth, _UNKNOWN_BALANCE_FALLBACK_SATS)

            if phase.amount is not None:
                amount = phase.amount
                # amount=0 is a sweep: the whole mixdepth balance moves.
                effective_amount = balance if amount == 0 else amount
                spent = balance if amount == 0 else min(amount, balance)
            else:
                fraction = phase.amount_fraction or 0.0
                effective_amount = int(balance * fraction)
                spent = effective_amount

            counterparties = phase.counterparty_count
            # Per-maker fee bound: max(abs, rel * amount). Aggregate across
            # all chosen counterparties; this is the worst case
            # ``filter_offers`` would still allow.
            per_maker_fee = max(
                max_cj_fee_abs_sats,
                int(round(rel_fee * effective_amount)),
            )
            phase_cj_fee = per_maker_fee * counterparties

            phase_miner_fee = int(round(effective_fee_rate * _vbytes_for_coinjoin(counterparties)))

            description = (
                f"mixdepth={mixdepth} sweep"
                if amount == 0 and phase.amount is not None
                else f"mixdepth={mixdepth} amount={effective_amount} sats"
            )
            destination = phase.destination
            if destination != "INTERNAL":
                description += f" -> {destination[:10]}..."
            else:
                description += " -> INTERNAL"

            phase_min = wait + _TAKER_PHASE_HANDSHAKE_SECONDS
            phase_expected = wait + _TAKER_PHASE_HANDSHAKE_SECONDS
            phase_max = wait + _TAKER_PHASE_HANDSHAKE_SECONDS

            # Fold in the inter-phase confirmation wait once we know the
            # phase actually broadcasts a transaction. The runner waits for
            # ``confirmation_block_count`` confirmations between phases, so
            # add that block-time budget here.
            confirm_seconds = confirmation_block_count * block_time_seconds
            phase_expected += confirm_seconds
            phase_max += confirm_seconds

            phases.append(
                PhaseCostEstimate(
                    index=phase.index,
                    kind=phase.kind.value,
                    description=description,
                    max_cj_fee_sats=phase_cj_fee,
                    miner_fee_sats=phase_miner_fee,
                    duration_seconds_min=phase_min,
                    duration_seconds_expected=phase_expected,
                    duration_seconds_max=phase_max,
                )
            )

            total_cj_fee += phase_cj_fee
            total_miner_fee += phase_miner_fee
            total_min += phase_min
            total_expected += phase_expected
            total_max += phase_max

            # Move the spent value forward in the running balance map, so
            # the next sweep on this mixdepth sees a reduced amount.
            running[mixdepth] = max(balance - spent, 0)
            # Sweeps go to the next mixdepth (INTERNAL). Track that so a
            # later phase on (mixdepth + 1) sees the inflow. We don't model
            # destination payouts; those leave the wallet entirely.
            if amount == 0 and phase.amount is not None and destination == "INTERNAL":
                next_md = (mixdepth + 1) % max(len(running) or 5, 5)
                running[next_md] = running.get(next_md, 0) + spent

        elif isinstance(phase, MakerSessionPhase):
            maker_count += 1
            duration = phase.duration_seconds or 0.0
            idle = phase.idle_timeout_seconds

            # The maker session can exit early on idle timeout (best case)
            # or run to ``duration_seconds`` (worst case). Expected = the
            # midpoint when an idle timeout is set, else duration.
            phase_min = wait + (idle if idle is not None else duration)
            phase_max = wait + duration
            phase_expected = wait + ((phase_min - wait + duration) / 2.0)

            phases.append(
                PhaseCostEstimate(
                    index=phase.index,
                    kind=phase.kind.value,
                    description=(
                        f"maker session "
                        f"(<= {int(duration)}s"
                        + (f", idle <= {int(idle)}s" if idle is not None else "")
                        + ")"
                    ),
                    max_cj_fee_sats=0,  # Tumbler maker offers are 0-fee.
                    miner_fee_sats=0,  # Maker doesn't pay miner fees.
                    duration_seconds_min=phase_min,
                    duration_seconds_expected=phase_expected,
                    duration_seconds_max=phase_max,
                )
            )
            total_min += phase_min
            total_expected += phase_expected
            total_max += phase_max

    return PlanEstimate(
        taker_phase_count=taker_count,
        maker_phase_count=maker_count,
        total_balance_sats=total_balance,
        mixdepth_balances={int(k): int(v) for k, v in balances.items()},
        total_max_cj_fee_sats=total_cj_fee,
        total_miner_fee_sats=total_miner_fee,
        total_wait_seconds=total_wait,
        total_duration_seconds_min=total_min,
        total_duration_seconds_expected=total_expected,
        total_duration_seconds_max=total_max,
        confirmation_block_count=confirmation_block_count,
        fee_rate_sat_vb=effective_fee_rate,
        fee_rate_source=effective_source,
        phases=phases,
    )
