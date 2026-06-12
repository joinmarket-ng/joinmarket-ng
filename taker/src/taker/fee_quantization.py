"""
Taker-side fee quantization policy.

Implements the homogenized per-slot fee described in issue #508. When enabled
(the default), the taker pays every selected maker the same effective fee,
derived from the taker's own configured fee limits rounded *down* onto the
public grid in :mod:`jmcore.fee_quantization`.

Key invariant: the homogenized per-slot fee never exceeds the per-maker fee the
taker already agreed to pay (the configured ``max_cj_fee`` limit). Quantization
only ever *raises* what an individual cheap maker is paid up to the shared
quantum; it never pays any maker more than the configured limit, and it never
selects a maker whose advertised fee is above the quantum.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jmcore.fee_quantization import (
    quantize_abs_down,
    quantize_rel_down,
    rel_quantum_to_sats,
)


@dataclass(frozen=True)
class FeeQuantizer:
    """Resolved per-round quantization policy.

    Attributes:
        enabled: Whether quantization is active. When ``False`` the taker pays
            each maker its exact advertised fee (legacy behavior).
        rel_quantum: Largest relative grid value ``<=`` the configured relative
            limit, or ``None`` if the limit is below the smallest grid value.
        abs_quantum: Largest absolute grid value ``<=`` the configured absolute
            limit, or ``None`` if the limit is below the smallest grid value.
    """

    enabled: bool
    rel_quantum: Decimal | None
    abs_quantum: int | None

    @classmethod
    def from_limits(cls, abs_fee: int, rel_fee: str, *, enabled: bool) -> FeeQuantizer:
        """Build a quantizer from the taker's configured fee limits."""
        if not enabled:
            return cls(enabled=False, rel_quantum=None, abs_quantum=None)
        return cls(
            enabled=True,
            rel_quantum=quantize_rel_down(rel_fee),
            abs_quantum=quantize_abs_down(abs_fee),
        )

    @property
    def active(self) -> bool:
        """True when quantization is enabled and at least one quantum resolved.

        If both quanta are ``None`` (the configured limits are below the
        smallest grid value), quantization cannot apply and the taker falls
        back to exact per-maker fees even though it is nominally enabled.
        """
        return self.enabled and (self.rel_quantum is not None or self.abs_quantum is not None)

    def slot_fee(self, cj_amount: int) -> int | None:
        """Homogenized fee paid to every selected maker for ``cj_amount``.

        Returns the maximum of the relative and absolute quanta (in sats), so
        that the single per-slot value covers makers selected via either fee
        type. Returns ``None`` when quantization is inactive.
        """
        if not self.active:
            return None
        candidates: list[int] = []
        if self.rel_quantum is not None:
            candidates.append(rel_quantum_to_sats(self.rel_quantum, cj_amount))
        if self.abs_quantum is not None:
            candidates.append(self.abs_quantum)
        return max(candidates) if candidates else None

    def paid_fee(self, exact_fee: int, cj_amount: int) -> int:
        """Fee the taker will pay a maker.

        With quantization active this is the homogenized slot fee (never below
        the maker's advertised ``exact_fee``); otherwise the exact fee.
        """
        slot = self.slot_fee(cj_amount)
        if slot is None:
            return exact_fee
        # Guard: never underpay a maker relative to its advertised fee. Filtering
        # should already guarantee exact_fee <= slot, but max() keeps the tx
        # valid even if an unfiltered offer slips through.
        return max(slot, exact_fee)
