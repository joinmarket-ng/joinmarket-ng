"""
Maker fee quantization grid and helpers.

Background
----------
Each maker advertises a per-CoinJoin fee policy. Because policies on the live
network are diverse, the realized fee paid to each maker slot is typically
unique within a round. A passive on-chain observer can use that uniqueness to
attribute an equal output to a specific maker slot when it is reused in a
downstream CoinJoin (see issue #508).

The mitigation is *fee homogenization*: if every maker slot in a round is paid
the same effective fee, the within-round fingerprint disappears and the
attribution stops working. To make that fee land on a small, shared, public set
of values (so it is also stable across rounds and takers), we round fees onto a
coarse base-10 grid.

This module only defines the grid and the rounding primitives. The taker-side
policy that selects a per-round quantum and the per-slot homogenized fee lives
in the taker package; the orderbook watcher uses the same grid (exposed in its
JSON payload) to chart where maker offers land.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal

# Relative-fee grid (fraction of the CoinJoin amount). Base-10 mantissas:
#   0.002%, 0.005%, 0.01%, 0.02%, 0.05%, 0.1%, 0.2%, 0.5%, 1%, 2%, 5%, 10%
QUANT_REL: tuple[Decimal, ...] = tuple(
    Decimal(v)
    for v in (
        "0.00002",
        "0.00005",
        "0.0001",
        "0.0002",
        "0.0005",
        "0.001",
        "0.002",
        "0.005",
        "0.01",
        "0.02",
        "0.05",
        "0.1",
    )
)

# Absolute-fee grid (satoshis per slot). The leading 0 is the free-maker band:
# makers that advertise a zero absolute fee land there and stay distinguishable
# from (and countable separately to) makers on the paid bands.
QUANT_ABS: tuple[int, ...] = (0, 100, 200, 500, 1000, 2000, 5000, 10000)


def quantize_rel_down(rel_fee: str | float | Decimal) -> Decimal | None:
    """Return the largest grid value ``<= rel_fee``.

    Returns ``None`` when ``rel_fee`` is below the smallest grid value, meaning
    no quantum fits under the configured limit and quantization cannot apply.
    """
    d = Decimal(str(rel_fee))
    best: Decimal | None = None
    for q in QUANT_REL:
        if q <= d:
            best = q
        else:
            break
    return best


def quantize_abs_down(abs_fee: int) -> int | None:
    """Return the largest absolute grid value ``<= abs_fee``.

    Returns ``None`` only when ``abs_fee`` is negative (no grid value fits);
    the grid includes ``0`` so any non-negative fee resolves to a quantum.
    """
    best: int | None = None
    for q in QUANT_ABS:
        if q <= abs_fee:
            best = q
        else:
            break
    return best


def quantize_rel_up(rel_fee: str | float | Decimal) -> Decimal | None:
    """Return the smallest grid value ``>= rel_fee``.

    Returns ``None`` when ``rel_fee`` exceeds the largest grid value.
    """
    d = Decimal(str(rel_fee))
    for q in QUANT_REL:
        if q >= d:
            return q
    return None


def quantize_abs_up(abs_fee: int) -> int | None:
    """Return the smallest absolute grid value ``>= abs_fee``.

    Returns ``None`` when ``abs_fee`` exceeds the largest grid value.
    """
    for q in QUANT_ABS:
        if q >= abs_fee:
            return q
    return None


def rel_quantum_to_sats(rel_quantum: Decimal, cj_amount: int) -> int:
    """Convert a relative quantum to satoshis for a given CoinJoin amount.

    Uses banker's rounding (ROUND_HALF_EVEN) to match
    :func:`jmcore.bitcoin.calculate_relative_fee`.
    """
    return int((Decimal(cj_amount) * rel_quantum).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
