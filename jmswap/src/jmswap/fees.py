from __future__ import annotations

import math
from decimal import Decimal

DUST_THRESHOLD = 546
COOPERATIVE_SWAP_TX_SIZE = 111
MIN_RELAY_FEERATE = 1_000
HIGH_FEE_WARNING_RATE = 600_000


def forward_onchain_amount(lightning_amount: int, percentage: Decimal, mining_fee: int) -> int:
    """Electrum _get_send_amount(..., is_reverse=False), including ceil rounding."""
    if lightning_amount <= 0:
        raise ValueError("lightning amount must be positive")
    result = lightning_amount + math.ceil(percentage * lightning_amount / 100) + mining_fee
    if result < DUST_THRESHOLD:
        raise ValueError("on-chain amount is below dust")
    return result


def reverse_onchain_amount(lightning_amount: int, percentage: Decimal, mining_fee: int) -> int:
    """Electrum _get_recv_amount(..., is_reverse=True), excluding client claim fee."""
    if lightning_amount <= 0:
        raise ValueError("lightning amount must be positive")
    result = math.floor(
        lightning_amount - math.ceil(percentage * lightning_amount / 100) - mining_fee
    )
    if result < DUST_THRESHOLD:
        raise ValueError("on-chain amount is below dust")
    return result


def cooperative_fee_bounds() -> tuple[int, int]:
    return (
        COOPERATIVE_SWAP_TX_SIZE * MIN_RELAY_FEERATE // 1_000,
        COOPERATIVE_SWAP_TX_SIZE * HIGH_FEE_WARNING_RATE // 1_000,
    )
