"""
Pre-flight UTXO eligibility checks for the taker.

These helpers let the taker decide, *before* connecting to directory servers
and fetching the orderbook, whether a mixdepth can actually fund a CoinJoin.
This avoids making the user wait 5-10 minutes for network operations only to
fail with "No eligible UTXOs in mixdepth" at the end (issue #528).

The classification mirrors the filters applied later in ``do_coinjoin`` /
``CoinSelectionMixin.select_utxos`` so the pre-flight verdict matches the real
selection outcome:

- minimum confirmations (``taker_utxo_age``),
- frozen UTXOs (never auto-selected),
- fidelity bonds (never auto-spent),
- inputs locked by another in-flight CoinJoin round,
- the mixdepth-0 merge restriction (handled by a dry-run of ``select_utxos``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jmwallet.wallet.models import UTXOInfo

# Kept as a stable prefix so downstream consumers (e.g. the tumbler's
# ``_LOW_CONFIRMATION_HINTS``) can recognise confirmation-related failures and
# wait for more confirmations before retrying.
NO_ELIGIBLE_PREFIX = "No eligible UTXOs in mixdepth"


@dataclass
class EligibilityBreakdown:
    """Classification of a mixdepth's UTXOs for automatic CoinJoin selection."""

    mixdepth: int
    min_confirmations: int
    eligible: list[UTXOInfo] = field(default_factory=list)
    immature: list[UTXOInfo] = field(default_factory=list)
    frozen: list[UTXOInfo] = field(default_factory=list)
    locked_bonds: list[UTXOInfo] = field(default_factory=list)
    unlocked_bonds: list[UTXOInfo] = field(default_factory=list)
    reserved: list[UTXOInfo] = field(default_factory=list)

    @property
    def total(self) -> int:
        """Total number of UTXOs considered (across all categories)."""
        return (
            len(self.eligible)
            + len(self.immature)
            + len(self.frozen)
            + len(self.locked_bonds)
            + len(self.unlocked_bonds)
            + len(self.reserved)
        )

    @property
    def eligible_value(self) -> int:
        """Summed value of the eligible UTXOs in satoshis."""
        return sum(u.value for u in self.eligible)

    def no_eligible_reason(self) -> str:
        """Build a human-readable reason explaining why nothing is eligible.

        The message starts with :data:`NO_ELIGIBLE_PREFIX` so confirmation-aware
        retry logic keeps working.
        """
        parts: list[str] = []
        if self.immature:
            parts.append(
                f"{len(self.immature)} below {self.min_confirmations} "
                f"confirmation(s) (taker_utxo_age)"
            )
        if self.frozen:
            parts.append(f"{len(self.frozen)} frozen")
        if self.locked_bonds:
            parts.append(f"{len(self.locked_bonds)} time-locked fidelity bond(s)")
        if self.unlocked_bonds:
            parts.append(f"{len(self.unlocked_bonds)} fidelity bond(s) (not auto-spent)")
        if self.reserved:
            parts.append(f"{len(self.reserved)} locked by another in-flight CoinJoin")

        if not self.total:
            return f"{NO_ELIGIBLE_PREFIX} {self.mixdepth} (no UTXOs present)"
        detail = ", ".join(parts) if parts else "none selectable"
        return f"{NO_ELIGIBLE_PREFIX} {self.mixdepth} ({self.total} UTXOs: {detail})"


def classify_utxos(
    utxos: list[UTXOInfo],
    mixdepth: int,
    min_confirmations: int,
    reserved_outpoints: set[tuple[str, int]] | None = None,
) -> EligibilityBreakdown:
    """Classify a mixdepth's UTXOs into eligibility categories.

    Mirrors the auto-selection filters used by ``select_utxos``/``get_all_utxos``.
    A UTXO is *eligible* only if it is confirmed enough, not frozen, not a
    fidelity bond, and not locked by another in-flight round. The remaining
    categories explain *why* an otherwise present UTXO is unavailable, so the
    caller can produce an actionable error message.
    """
    reserved_outpoints = reserved_outpoints or set()
    result = EligibilityBreakdown(mixdepth=mixdepth, min_confirmations=min_confirmations)

    for utxo in utxos:
        if (utxo.txid, utxo.vout) in reserved_outpoints:
            result.reserved.append(utxo)
        elif utxo.frozen:
            result.frozen.append(utxo)
        elif utxo.is_fidelity_bond:
            if utxo.is_locked:
                result.locked_bonds.append(utxo)
            else:
                result.unlocked_bonds.append(utxo)
        elif utxo.confirmations < min_confirmations:
            result.immature.append(utxo)
        else:
            result.eligible.append(utxo)

    return result


def selectable_for_interactive(utxos: list[UTXOInfo], min_confirmations: int) -> list[UTXOInfo]:
    """Return UTXOs a user may pick in the interactive selector.

    The interactive selector (``--select-utxos``) shows frozen/locked UTXOs but
    renders them unselectable, and lets the user spend unlocked fidelity bonds.
    """
    return [
        u
        for u in utxos
        if (u.confirmations >= min_confirmations)
        and not u.frozen
        and not (u.is_fidelity_bond and u.is_locked)
    ]


def podle_threshold_met(
    utxos: list[UTXOInfo], cj_amount: int, min_confirmations: int, min_percent: int
) -> bool:
    """Check whether any eligible UTXO can back a PoDLE commitment.

    A CoinJoin requires a commitment from a UTXO worth at least
    ``min_percent`` of ``cj_amount`` with at least ``min_confirmations``. If no
    such UTXO exists the round fails deterministically, so this lets us reject
    early with a precise message.
    """
    min_value = int(cj_amount * min_percent / 100)
    return any(u.confirmations >= min_confirmations and u.value >= min_value for u in utxos)
