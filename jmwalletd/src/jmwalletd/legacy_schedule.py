"""Project a tumbler :class:`~tumbler.plan.Plan` into the legacy schedule format.

The reference implementation's ``GET /session`` returns the running tumble as
``taker.schedule``: a list of 7-element entries that JAM parses in
``scheduleUtils.ts``::

    [mixdepth, amount, counterparties, destination, wait_minutes, rounding, flag]

where

* ``amount`` is a fraction in ``(0, 1)`` for non-sweeps or ``0`` for sweeps
  (absolute satoshi amounts are also accepted -- JAM only requires a number);
* ``destination`` is a bitcoin address or the sentinel ``"INTERNAL"``;
* ``wait_minutes`` is the delay *after* this entry completes, before the next
  entry starts;
* ``rounding`` is the significant-figures rounding applied to the amount
  (``16`` means "no rounding" in the reference implementation);
* ``flag`` is ``0`` (not yet broadcast), the txid string (broadcast, waiting
  for confirmations), or ``1`` (confirmed).

jm-ng plans may interleave :class:`~tumbler.plan.MakerSessionPhase` phases,
which have no legacy equivalent. Their expected duration and inter-phase wait
are folded into the preceding taker entry's wait time so JAM's progress bar
and ETA remain meaningful.
"""

from __future__ import annotations

from tumbler.plan import MakerSessionPhase, PhaseStatus, Plan, TakerCoinjoinPhase

# The reference implementation uses 16 significant figures to express
# "do not round the amount" in a schedule entry.
NO_ROUNDING = 16

ScheduleEntry = list[str | int | float]


def _phase_flag(phase: TakerCoinjoinPhase, plan: Plan) -> str | int:
    """Map phase progress onto the legacy 0 / txid / 1 completion flag.

    The runner marks a taker phase ``COMPLETED`` as soon as the CoinJoin
    broadcasts, then holds ``current_phase`` at the phase's index until the
    confirmation gate passes. So a completed phase is only *confirmed* (``1``)
    once the plan has advanced past it; before that the broadcast txid is the
    correct legacy marker.
    """
    if phase.status == PhaseStatus.COMPLETED and phase.index < plan.current_phase:
        return 1
    if phase.txid:
        return phase.txid
    if phase.status == PhaseStatus.COMPLETED:
        # Broadcast reported without a txid (should not happen for taker
        # phases); confirmed is the closest legacy state.
        return 1
    return 0


def plan_to_legacy_schedule(plan: Plan) -> list[ScheduleEntry]:
    """Render ``plan`` as a reference-format schedule (list of 7-element lists).

    Maker-session phases are not representable as legacy entries; their
    expected duration (``duration_seconds`` when time-bounded) and their
    ``wait_seconds`` are added to the preceding taker entry's wait time.
    A maker session before the first taker phase is dropped entirely (the
    legacy format has no way to express a delay before the first entry).
    """
    schedule: list[ScheduleEntry] = []
    for phase in plan.phases:
        if isinstance(phase, TakerCoinjoinPhase):
            amount: int | float
            if phase.amount_fraction is not None:
                amount = phase.amount_fraction
            else:
                amount = phase.amount or 0
            schedule.append(
                [
                    phase.mixdepth,
                    amount,
                    phase.counterparty_count,
                    phase.destination,
                    phase.wait_seconds / 60.0,
                    phase.rounding_sigfigs if phase.rounding_sigfigs is not None else NO_ROUNDING,
                    _phase_flag(phase, plan),
                ]
            )
        elif isinstance(phase, MakerSessionPhase) and schedule:
            extra_seconds = (phase.duration_seconds or 0.0) + phase.wait_seconds
            entry = schedule[-1]
            entry[4] = float(entry[4]) + extra_seconds / 60.0
    return schedule
