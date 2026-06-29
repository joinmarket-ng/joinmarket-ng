"""Tests for the taker-side knapsack channel-as-change planner.

Pin the privacy properties the simulator's ``maker-swap-provider`` study relies on:
no single output equals a maker's residual, many pieces share the grid value (so the
whole-round partition is ambiguous), every channel piece is maker-to-maker (never
external), and the allowance hides non-channel makers in the same pool.
"""

from __future__ import annotations

from collections import Counter

from taker.combine import CombineMaker
from taker.knapsack import (
    DEFAULT_GRID_DIV,
    KnapsackPlan,
    plan_knapsack,
    quantize,
)

DENOM = 1_000_000


def _makers(n: int, *, capable: bool = True) -> list[CombineMaker]:
    # Distinct changes, all comfortably above one grid unit (denom // 50 = 20_000).
    return [
        CombineMaker(
            f"m{i}",
            300_000 + i * 70_000,
            capable,
            f"02{i:02x}@h:9735" if capable else None,
        )
        for i in range(n)
    ]


def _sum_by_owner(plan: KnapsackPlan) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in plan.pieces:
        out[p.owner] = out.get(p.owner, 0) + p.value
    return out


def test_quantize_splits_into_grid_plus_remainder() -> None:
    grid = DENOM // DEFAULT_GRID_DIV  # 20_000
    pieces = quantize(500_000, grid)
    assert sum(pieces) == 500_000
    assert len(pieces) == 2  # one grid piece + remainder (max_pieces=2)
    assert pieces[0] == grid
    # A change at or below one grid unit cannot be split: returned whole.
    assert quantize(grid, grid) == [grid]


def test_each_maker_change_is_preserved() -> None:
    makers = _makers(6)
    plan = plan_knapsack(makers, denomination=DENOM)
    by_owner = _sum_by_owner(plan)
    for m in makers:
        assert by_owner[m.nick] == m.change


def test_no_single_piece_equals_a_makers_residual() -> None:
    makers = _makers(6)
    plan = plan_knapsack(makers, denomination=DENOM)
    residuals = {m.change for m in makers}
    # No output equals any maker's full residual, so the per-output change-match finds
    # nothing to attribute.
    assert all(p.value not in residuals for p in plan.pieces)


def test_channel_pieces_are_maker_to_maker_never_external() -> None:
    makers = _makers(6)
    plan = plan_knapsack(makers, denomination=DENOM)
    nicks = {m.nick for m in makers}
    channel = plan.channel_pieces()
    assert channel  # there are channel pieces
    for p in channel:
        assert p.counterparty in nicks  # a fellow maker
        assert p.counterparty != p.owner  # never itself, never external


def test_grid_pieces_collide_across_makers() -> None:
    # Many makers emit an equal-valued grid piece: the interchangeable pieces are what
    # make the whole-round subset-sum partition ambiguous.
    makers = _makers(6)
    plan = plan_knapsack(makers, denomination=DENOM)
    counts = Counter(p.value for p in plan.pieces)
    most_common_value, most_common_count = counts.most_common(1)[0]
    assert most_common_value == plan.grid
    assert most_common_count >= 2


def test_allowance_makers_are_plain_but_pooled() -> None:
    makers = _makers(6)
    plan = plan_knapsack(makers, denomination=DENOM, allowance=2)
    plain = [p for p in plan.pieces if not p.is_channel]
    channel = [p for p in plan.pieces if p.is_channel]
    # The allowance produced plain pieces and channel pieces coexist in one pool.
    assert plain and channel
    # The allowance makers' change is still split into grid pieces (diffused), so even
    # the plain pieces do not equal a maker's residual.
    residuals = {m.change for m in makers}
    assert all(p.value not in residuals for p in plain)


def test_allowance_raises_plain_share_without_exposing_residuals() -> None:
    makers = _makers(6)
    none = plan_knapsack(makers, denomination=DENOM, allowance=0)
    some = plan_knapsack(makers, denomination=DENOM, allowance=3)
    plain_share_none = sum(1 for p in none.pieces if not p.is_channel) / len(none.pieces)
    plain_share_some = sum(1 for p in some.pieces if not p.is_channel) / len(some.pieces)
    assert plain_share_some > plain_share_none
    residuals = {m.change for m in makers}
    assert all(p.value not in residuals for p in some.pieces)


def test_fewer_than_two_channel_makers_falls_back_to_plain() -> None:
    # A lone channel-capable maker has no fellow maker to pair with: all plain.
    makers = [
        CombineMaker("solo", 500_000, True, "02aa@h:9735"),
        CombineMaker("nocap", 400_000, False, None),
    ]
    plan = plan_knapsack(makers, denomination=DENOM)
    assert all(not p.is_channel for p in plan.pieces)


def test_plan_is_deterministic() -> None:
    makers = _makers(6)
    a = plan_knapsack(makers, denomination=DENOM, allowance=2)
    b = plan_knapsack(makers, denomination=DENOM, allowance=2)
    assert a == b


def test_amounts_by_owner_matches_pieces() -> None:
    makers = _makers(5)
    plan = plan_knapsack(makers, denomination=DENOM)
    amounts = plan.amounts_by_owner()
    for m in makers:
        assert sum(amounts[m.nick]) == m.change
