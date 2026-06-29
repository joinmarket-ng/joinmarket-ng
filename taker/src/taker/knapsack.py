"""Taker-side knapsack pairing and sizing for maker-to-maker channel-as-change.

Background. Channel-as-change is *always* a channel between two CoinJoin
participants: a channel funding output reveals its outpoint to the co-signing
counterparty, so opening to an *external* peer would hand the maker's
funding<->wallet link to an outside observer (no privacy gain). A maker that
cannot be paired falls back to a plain change output.

Why not a fixed pool. The simple pairing in :mod:`taker.combine` pools two
makers' *exact* change into one shared output. That defeats the per-output
change-match, but a whole-round subset-sum ("CoinJoin-Sudoku") solver that
hypothesises group-funded shared outputs can uniquely invert a rigid pool of two
whenever the pool size relates simply to the participant count (the coinjoin
simulator's ``maker-swap-provider`` study measures this: the fixed pool is
attributed ~1.0 at 4 makers and ~0.94 at 6, an even/odd sawtooth).

The knapsack construction. The taker pools *every* change-bearing maker and
splits each maker's change into grid-quantized pieces that share a single
round-wide grid value, emitted as *overlapping* maker-to-maker 2-of-2 outputs.
Because many makers emit an equal-valued grid piece, a grid piece of one maker is
interchangeable with a grid piece of another without changing any output value or
any maker's total contribution, so the whole-round partition is combinatorially
ambiguous (low-density subset-sum / knapsack hardness). In the simulator this
drives the maximal attacker's attribution to ~0 at *every* set size, where the
fixed pool fails. An *allowance* of makers can open no channel at all; their plain
change is diffused into the same grid pool, so on chain they are indistinguishable
from the channel makers (their pieces still chain across slots, the honest limit).

This module is the taker's *planning* decision (which makers pair, how each
change is sized into pieces), mirroring :func:`taker.combine.pair_combine_makers`.
The piece *amounts* feed ``build_coinjoin_tx``'s per-maker ``change_outputs``; the
maker-to-maker 2-of-2 *addresses* for the channel pieces are built by the channel
layer from the pairing recorded here.
"""

from __future__ import annotations

from dataclasses import dataclass

from taker.combine import CombineMaker

# 10x the standard dust limit, matching jmcore.constants.DUST_THRESHOLD, kept local
# so this pure planner has no import-time dependency on chain constants.
_DUST_THRESHOLD = 27_300

# Per-maker cap on knapsack pieces: one shared-grid piece plus one remainder is the
# minimum that leaves no single output equal to the maker's residual, so it is the
# cheapest on-chain footprint that still defeats the per-output change-match while the
# shared grid value keeps the whole-round partition ambiguous.
DEFAULT_MAX_PIECES = 2

# The grid value is ``denomination // DEFAULT_GRID_DIV``; a fine grid keeps every
# maker's change above one grid unit (so it is split, not a lone residual) and
# minimises the residual piece's resemblance to the full change.
DEFAULT_GRID_DIV = 50


@dataclass(frozen=True)
class KnapsackPiece:
    """One change piece of one maker in the knapsack plan.

    ``counterparty`` is the fellow-maker nick co-signing the 2-of-2 channel funding
    output for a channel piece, or ``None`` for a plain change piece (an allowance
    maker, or the degenerate case of fewer than two channel makers).
    """

    owner: str
    value: int
    counterparty: str | None

    @property
    def is_channel(self) -> bool:
        return self.counterparty is not None


@dataclass(frozen=True)
class KnapsackPlan:
    """A round's knapsack pieces and the per-maker amount breakdown."""

    pieces: tuple[KnapsackPiece, ...]
    grid: int

    def amounts_by_owner(self) -> dict[str, list[int]]:
        """Map each maker nick to its ordered piece amounts (sum == its change).

        These are the ``change_outputs`` amounts ``build_coinjoin_tx`` expects; the
        caller pairs each with a fresh change address (a maker-to-maker 2-of-2 for a
        channel piece, an ordinary address for a plain piece).
        """
        out: dict[str, list[int]] = {}
        for piece in self.pieces:
            out.setdefault(piece.owner, []).append(piece.value)
        return out

    def channel_pieces(self) -> tuple[KnapsackPiece, ...]:
        return tuple(p for p in self.pieces if p.is_channel)


def quantize(
    change: int, grid: int, *, max_pieces: int = DEFAULT_MAX_PIECES, dust: int = _DUST_THRESHOLD
) -> list[int]:
    """Split ``change`` into at most ``max_pieces`` pieces sharing the ``grid`` value.

    Returns one or more grid-valued pieces followed by a single remainder, capped at
    ``max_pieces``. A change at or below one grid unit (plus dust) is returned whole
    (it cannot be split above dust); the caller treats such a maker as plain, since a
    lone piece equal to the residual is change-matchable.
    """
    if max_pieces < 2:
        raise ValueError("max_pieces must be >= 2")
    if grid <= 0:
        raise ValueError("grid must be positive")
    if change <= grid + dust:
        return [change]
    units = min(change // grid, max_pieces - 1)
    pieces = [grid] * units
    pieces.append(change - units * grid)
    return pieces


def _grid_for(denomination: int, grid_div: int, dust: int) -> int:
    return max(dust * 2, denomination // max(1, grid_div))


def plan_knapsack(
    makers: list[CombineMaker],
    *,
    denomination: int,
    allowance: int = 0,
    grid_div: int = DEFAULT_GRID_DIV,
    max_pieces: int = DEFAULT_MAX_PIECES,
    dust: int = _DUST_THRESHOLD,
) -> KnapsackPlan:
    """Plan the round's knapsack channel-as-change pieces.

    ``makers`` are the round's makers with their taker-computed change. Channel makers
    (``combine_capable`` with an advertised ``ln_node``) are paired round-robin into
    overlapping maker-to-maker 2-of-2 pieces; ``allowance`` of them are instead emitted
    as plain pieces, diffused into the same grid pool so they are hidden on chain.
    Makers that are not channel-capable are always plain. Fewer than two channel makers
    cannot pair, so all change is plain. The pairing and sizing are order-stable
    (deterministic) so both makers can recompute and verify the plan off chain.
    """
    grid = _grid_for(denomination, grid_div, dust)
    capable = [m for m in makers if m.combine_capable and m.ln_node]
    # The allowance opts the last `allowance` capable makers out of channels (stable).
    n_allow = max(0, min(allowance, len(capable)))
    channel_makers = capable[: len(capable) - n_allow]
    plain_makers = [m for m in makers if m not in channel_makers]

    pieces: list[KnapsackPiece] = []
    n_ch = len(channel_makers)
    if n_ch < 2:
        # No fellow maker to pair with: every maker is plain.
        for m in makers:
            for value in quantize(m.change, grid, max_pieces=max_pieces, dust=dust):
                pieces.append(KnapsackPiece(owner=m.nick, value=value, counterparty=None))
        return KnapsackPlan(pieces=tuple(pieces), grid=grid)

    for j, m in enumerate(channel_makers):
        partner = channel_makers[(j + 1) % n_ch].nick
        amounts = quantize(m.change, grid, max_pieces=max_pieces, dust=dust)
        # A maker whose change is too small to split (a lone residual) cannot be a
        # channel piece without revealing the residual, so it stays plain.
        cp = partner if len(amounts) >= 2 else None
        for value in amounts:
            pieces.append(KnapsackPiece(owner=m.nick, value=value, counterparty=cp))
    for m in plain_makers:
        for value in quantize(m.change, grid, max_pieces=max_pieces, dust=dust):
            pieces.append(KnapsackPiece(owner=m.nick, value=value, counterparty=None))
    return KnapsackPlan(pieces=tuple(pieces), grid=grid)
