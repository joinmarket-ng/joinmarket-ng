"""
Plan builder.

Constructs a :class:`~tumbler.plan.Plan` from a list of destination
addresses and the current wallet state (per-mixdepth balances). Callers are
expected to hold wallet locks while reading balances; the builder itself is
pure and side-effect free so it is trivially testable.

Shape of the produced plan
--------------------------

The plan is a sequence of phases grouped into two conceptual stages:

* **Stage 1 - origin cleavage.** For every non-empty mixdepth, schedule one
  :class:`~tumbler.plan.TakerCoinjoinPhase` sweep to an internal address
  of the next mixdepth. This severs the pre-tumble coin graph before any
  funds reach a destination.
* **Stage 2 - destination mixing.** For each destination, schedule a
  small number of fractional taker CoinJoins, optionally interleaved with
  :class:`~tumbler.plan.MakerSessionPhase` sessions, and a final sweep to
  the destination address.

The role mixing mitigates, but does not eliminate, the subset-sum signature
problem tracked in upstream issue #114. See the design doc at
``docs/technical/tumbler-redesign.md`` for rationale.
"""

from __future__ import annotations

import math
import random
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from tumbler.plan import (
    MakerSessionPhase,
    Phase,
    PhaseKind,
    Plan,
    PlanParameters,
    TakerCoinjoinPhase,
)

INTERNAL_DESTINATION = "INTERNAL"


@dataclass
class TumbleParameters:
    """
    High-level knobs passed to :meth:`PlanBuilder.build`.

    These are copied into :class:`~tumbler.plan.PlanParameters` on the plan
    so the choices can be inspected later and the plan can be rebuilt for
    debugging. They do not drive the runner directly.
    """

    destinations: list[str]
    mixdepth_balances: Mapping[int, int]
    """Current confirmed balance per mixdepth, in satoshis."""
    maker_count_min: int = 5
    maker_count_max: int = 9
    time_lambda_seconds: float = 30.0
    include_maker_sessions: bool = True
    mincjamount_sats: int = 100_000
    maker_session_seconds: float = 20.0 * 60.0
    maker_session_idle_timeout_seconds: float | None = None
    """If set, maker phases exit successfully when no CoinJoin has been served
    within this many seconds. Useful as a safety fallback when the wallet is
    never selected as a counterparty."""
    mintxcount: int = 2
    """Minimum number of destination-bearing taker CJs per mixdepth (excluding sweep)."""
    max_phase_retries: int = 3
    """Maximum re-tries per failed taker CoinJoin phase before the plan fails."""
    seed: int | None = None

    @property
    def rng(self) -> random.Random:
        return random.Random(self.seed)

    @property
    def num_destinations(self) -> int:
        return len(self.destinations)


@dataclass
class PlanBuilder:
    """Turns :class:`TumbleParameters` into a :class:`~tumbler.plan.Plan`."""

    wallet_name: str
    params: TumbleParameters
    _phase_counter: int = field(default=0, init=False)

    # ------------------------------------------------------------------ public

    def build(self) -> Plan:
        if not self.params.destinations:
            raise ValueError("at least one destination is required")
        if self.params.num_destinations > len(self._stage2_mixdepth_chain()):
            raise ValueError("tumble requires at least as many usable mixdepths as destinations")

        rng = self.params.rng
        phases: list[Phase] = []
        phases.extend(self._stage1_cleavage(rng))
        phases.extend(self._stage2_destinations(rng))

        plan_params = PlanParameters(
            maker_count_min=self.params.maker_count_min,
            maker_count_max=self.params.maker_count_max,
            time_lambda_seconds=self.params.time_lambda_seconds,
            include_maker_sessions=self.params.include_maker_sessions,
            mincjamount_sats=self.params.mincjamount_sats,
            max_phase_retries=self.params.max_phase_retries,
            seed=self.params.seed,
        )
        return Plan(
            wallet_name=self.wallet_name,
            destinations=list(self.params.destinations),
            parameters=plan_params,
            phases=phases,
        )

    # ------------------------------------------------------------------ stages

    def _stage1_cleavage(self, rng: random.Random) -> list[Phase]:
        """Sweep every non-empty mixdepth into the next mixdepth's internal address."""
        phases: list[Phase] = []
        # Match the reference tumbler: sweep higher mixdepths first so coins
        # forwarded from a lower mixdepth are not immediately swept again.
        for mixdepth in sorted(self.params.mixdepth_balances, reverse=True):
            balance = self.params.mixdepth_balances[mixdepth]
            if balance <= 0:
                continue
            phases.append(
                self._new_phase(
                    TakerCoinjoinPhase,
                    mixdepth=mixdepth,
                    amount=0,
                    counterparty_count=self._sample_counterparty_count(rng),
                    destination=INTERNAL_DESTINATION,
                    wait_seconds=self._sample_wait(rng, stage1=True),
                )
            )
        return phases

    def _stage2_destinations(self, rng: random.Random) -> list[Phase]:
        """
        Traverse the post-stage-1 mixdepth chain, emitting one role-mixed
        stage-2 block per mixdepth.

        This mirrors the reference tumbler's structure: every stage-2
        mixdepth gets maker and fractional activity, intermediate final
        sweeps advance to ``INTERNAL``, and only the last N mixdepths in the
        chain sweep to the user-supplied external destinations.
        """
        phases: list[Phase] = []
        chain = self._stage2_mixdepth_chain()
        destination_map = dict(
            zip(chain[-self.params.num_destinations :], self.params.destinations)
        )
        for chain_index, mixdepth in enumerate(chain):
            if self.params.include_maker_sessions:
                phases.append(
                    self._new_phase(
                        MakerSessionPhase,
                        duration_seconds=self.params.maker_session_seconds,
                        target_cj_count=None,
                        idle_timeout_seconds=self.params.maker_session_idle_timeout_seconds,
                        wait_seconds=self._sample_wait(rng),
                    )
                )
            # Fractional destination CJs (at least mintxcount-1 before the sweep).
            fractions = self._destination_fractions(self.params.mintxcount, rng)
            for fraction in fractions:
                phases.append(
                    self._new_phase(
                        TakerCoinjoinPhase,
                        mixdepth=mixdepth,
                        amount_fraction=fraction,
                        counterparty_count=self._sample_counterparty_count(rng),
                        destination=INTERNAL_DESTINATION,
                        wait_seconds=self._sample_wait(rng),
                    )
                )
            # Final sweep of the mixdepth: advance internally until the last
            # destination-bearing mixdepths, then sweep externally.
            is_last = chain_index == len(chain) - 1
            phases.append(
                self._new_phase(
                    TakerCoinjoinPhase,
                    mixdepth=mixdepth,
                    amount=0,
                    counterparty_count=self._sample_counterparty_count(rng),
                    destination=destination_map.get(mixdepth, INTERNAL_DESTINATION),
                    # No trailing wait on the very last phase.
                    wait_seconds=0.0 if is_last else self._sample_wait(rng),
                )
            )
        return phases

    # ------------------------------------------------------------------ helpers

    def _new_phase(self, cls: type[Phase], **kwargs: Any) -> Phase:
        phase = cls(index=self._phase_counter, **kwargs)  # type: ignore[call-arg]
        self._phase_counter += 1
        return phase

    def _nonempty_mixdepths(self) -> list[int]:
        return sorted(m for m, bal in self.params.mixdepth_balances.items() if bal > 0)

    def _nonempty_mixdepth_count(self) -> int:
        return len(self._nonempty_mixdepths())

    def _stage2_mixdepth_chain(self) -> list[int]:
        """Return the stage-2 source mixdepth chain.

        The reference tumbler mixes through ``wallet_mixdepth_count - 1``
        successive mixdepths starting from the lowest mixdepth left non-empty
        after the descending stage-1 sweeps.
        """
        wallet_mixdepth_count = max(self.params.mixdepth_balances) + 1
        occupied = set(self._nonempty_mixdepths())
        for origin in sorted(occupied, reverse=True):
            occupied.discard(origin)
            occupied.add((origin + 1) % wallet_mixdepth_count)
        if not occupied:
            return []
        lowest = min(occupied)
        chain_length = max(wallet_mixdepth_count - 1, 0)
        return [((lowest + offset) % wallet_mixdepth_count) for offset in range(chain_length)]

    def _sample_counterparty_count(self, rng: random.Random) -> int:
        lo = self.params.maker_count_min
        hi = self.params.maker_count_max
        # Light normal distribution bounded into [lo, hi].
        mu = (lo + hi) / 2.0
        sigma = max((hi - lo) / 4.0, 0.5)
        value = int(round(rng.gauss(mu, sigma)))
        return max(lo, min(hi, value))

    def _sample_wait(self, rng: random.Random, stage1: bool = False) -> float:
        lam = self.params.time_lambda_seconds * (1.5 if stage1 else 1.0)
        # Exponential with mean ``lam``. Clamp to avoid pathological pauses.
        u = rng.random()
        # rng.random() can return 0; guard against log(0).
        u = max(u, 1e-9)
        return min(-math.log(u) * lam, lam * 10.0)

    def _destination_fractions(self, mintxcount: int, rng: random.Random) -> list[float]:
        """
        Return ``mintxcount - 1`` fractions in ``(0, 1)`` summing to < 1,
        with each at least 0.05. The remainder is swept by the trailing
        sweep phase. Uses the reference 'sorted knives' uniform scheme.
        """
        count = max(mintxcount - 1, 1)
        if count == 1:
            return [round(rng.uniform(0.3, 0.6), 4)]
        knives = sorted(rng.uniform(0.0, 1.0) for _ in range(count))
        fractions: list[float] = []
        prev = 0.0
        for knife in knives:
            fractions.append(max(knife - prev, 0.05))
            prev = knife
        # Normalize so the sum does not reach 1.0 (leave >= 5% for the sweep).
        total = sum(fractions) + 0.05
        if total >= 1.0:
            scale = 0.9 / total
            fractions = [max(round(f * scale, 4), 0.05) for f in fractions]
        return fractions


_ = PhaseKind  # re-export linter hint
