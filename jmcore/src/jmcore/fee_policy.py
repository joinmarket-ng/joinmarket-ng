"""Shared minimum miner-fee policy for CoinJoin participants."""

from __future__ import annotations

import math
import re
from inspect import isawaitable
from typing import Any

from loguru import logger

_LOW_FEE_ERROR = re.compile(
    r"CoinJoin miner fee rate ([0-9]{1,16}\.[0-9]{1,8}) sat/vB is below required "
    r"([0-9]{1,16}\.[0-9]{1,8}) sat/vB"
)


def format_low_fee_error(fee_rate: float, minimum_fee_rate: float) -> str:
    """Return a human-readable !error with only the two fee-policy values."""
    return (
        f"CoinJoin miner fee rate {fee_rate:.4f} sat/vB is below required "
        f"{minimum_fee_rate:.4f} sat/vB"
    )


def parse_low_fee_error(message: object) -> tuple[float, float] | None:
    """Extract bounded numeric diagnostics without logging arbitrary peer text.

    Accept the current and earlier decimal precision. Full matching excludes
    identifiers, terminal escapes, and extra lines from ordinary INFO logs.
    """
    if not isinstance(message, str):
        return None
    match = _LOW_FEE_ERROR.fullmatch(message)
    if match is None:
        return None
    fee_rate, minimum_fee_rate = map(float, match.groups())
    if minimum_fee_rate <= 0 or fee_rate > minimum_fee_rate:
        return None
    return fee_rate, minimum_fee_rate


class MinimumFeeRateExceedsCapError(ValueError):
    """Raised when a required minimum fee rate exceeds the configured cap."""

    def __init__(self, *, source: str, fee_rate: float) -> None:
        self.source = source
        self.fee_rate = fee_rate
        super().__init__(f"Minimum fee rate from {source} exceeds the configured maximum fee rate")


def estimate_p2wpkh_vsize(num_inputs: int, num_outputs: int) -> int:
    """Return the conservative P2WPKH virtual-size estimate used by CoinJoin."""
    return num_inputs * 68 + num_outputs * 31 + 11


def fee_rate_meets_minimum(fee: int, vsize: int, minimum_fee_rate: float) -> bool:
    """Check whether an integer fee meets the exact minimum fee rate."""
    return fee >= 0 and vsize > 0 and fee >= minimum_fee_rate * vsize


def _is_finite_positive(value: object) -> bool:
    """Return whether a runtime policy value is a finite positive number."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


async def resolve_min_fee_rate(
    backend: Any,
    *,
    static_floor: float,
    block_target: int,
    max_fee_rate: float,
) -> float:
    """Resolve the maximum of static, mempool, and estimate fee floors within the cap."""
    if not _is_finite_positive(static_floor):
        raise ValueError("Minimum fee static floor must be a finite positive sat/vB value")
    if not _is_finite_positive(max_fee_rate):
        raise ValueError("Maximum fee rate must be a finite positive sat/vB value")
    sources: list[tuple[str, float]] = [("static floor", static_floor)]

    try:
        mempool_minimum = await backend.get_mempool_min_fee()
        if mempool_minimum is not None:
            if _is_finite_positive(mempool_minimum):
                sources.append(("mempool minimum", mempool_minimum))
            else:
                logger.warning("Ignoring invalid local mempool minimum fee rate")
    except Exception as exc:
        logger.warning(f"Could not resolve local mempool minimum fee rate: {exc}")

    try:
        can_estimate = backend.can_estimate_fee()
        if isawaitable(can_estimate):
            can_estimate = await can_estimate
        if can_estimate:
            estimate = await backend.estimate_fee(block_target)
            if _is_finite_positive(estimate):
                sources.append(("backend estimate", estimate))
            else:
                logger.warning("Ignoring invalid fee estimate for minimum miner-fee policy")
    except Exception as exc:
        logger.warning(f"Could not resolve fee estimate for minimum miner-fee policy: {exc}")

    source, minimum_fee_rate = max(sources, key=lambda source_and_rate: source_and_rate[1])
    if minimum_fee_rate > max_fee_rate:
        raise MinimumFeeRateExceedsCapError(source=source, fee_rate=minimum_fee_rate)
    return minimum_fee_rate
