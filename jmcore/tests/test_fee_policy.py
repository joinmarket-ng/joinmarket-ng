from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jmcore.fee_policy import (
    MinimumFeeRateExceedsCapError,
    fee_rate_meets_minimum,
    format_low_fee_error,
    parse_low_fee_error,
    resolve_min_fee_rate,
)


@pytest.mark.asyncio
async def test_resolve_min_fee_rate_uses_highest_valid_source_within_cap() -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=2.0)
    backend.can_estimate_fee.return_value = True
    backend.estimate_fee = AsyncMock(return_value=4.0)

    assert (
        await resolve_min_fee_rate(backend, static_floor=1.0, block_target=10, max_fee_rate=5.0)
        == 4.0
    )
    backend.estimate_fee.assert_awaited_once_with(10)


@pytest.mark.asyncio
async def test_resolve_min_fee_rate_rejects_mempool_floor_above_cap() -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=4.0)
    backend.can_estimate_fee.return_value = False

    with pytest.raises(
        MinimumFeeRateExceedsCapError, match="exceeds the configured maximum"
    ) as exc_info:
        await resolve_min_fee_rate(backend, static_floor=1.0, block_target=10, max_fee_rate=3.0)

    assert exc_info.value.source == "mempool minimum"
    assert exc_info.value.fee_rate == 4.0


@pytest.mark.asyncio
async def test_resolve_min_fee_rate_rejects_estimate_above_cap() -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=2.0)
    backend.can_estimate_fee.return_value = True
    backend.estimate_fee = AsyncMock(return_value=4.0)

    with pytest.raises(
        MinimumFeeRateExceedsCapError, match="exceeds the configured maximum"
    ) as exc_info:
        await resolve_min_fee_rate(backend, static_floor=1.0, block_target=10, max_fee_rate=3.0)

    assert exc_info.value.source == "backend estimate"
    assert exc_info.value.fee_rate == 4.0


@pytest.mark.asyncio
async def test_resolve_min_fee_rate_falls_back_after_invalid_or_failed_sources() -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=float("nan"))
    backend.can_estimate_fee.return_value = True
    backend.estimate_fee = AsyncMock(side_effect=RuntimeError("offline"))

    assert (
        await resolve_min_fee_rate(backend, static_floor=1.5, block_target=10, max_fee_rate=1_000.0)
        == 1.5
    )


@pytest.mark.asyncio
async def test_resolve_min_fee_rate_accepts_awaitable_capability_probe() -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=None)
    backend.can_estimate_fee = AsyncMock(return_value=True)
    backend.estimate_fee = AsyncMock(return_value=2.0)

    assert (
        await resolve_min_fee_rate(backend, static_floor=1.0, block_target=10, max_fee_rate=3.0)
        == 2.0
    )


def test_fee_rate_check_enforces_exact_minimum() -> None:
    assert fee_rate_meets_minimum(200, 100, 2.0)
    assert not fee_rate_meets_minimum(199, 100, 2.0)


def test_low_fee_error_format_round_trips_canonical_rates() -> None:
    message = format_low_fee_error(1.1234, 2.0)

    assert message == "CoinJoin miner fee rate 1.1234 sat/vB is below required 2.0000 sat/vB"
    assert parse_low_fee_error(message) == (1.1234, 2.0)


def test_low_fee_error_parser_accepts_legacy_two_decimal_rates() -> None:
    assert parse_low_fee_error(
        "CoinJoin miner fee rate 1.12 sat/vB is below required 2.00 sat/vB"
    ) == (1.12, 2.0)


@pytest.mark.parametrize(
    "message",
    [
        "CoinJoin miner fee rate NaN sat/vB is below required 2.0000 sat/vB",
        "CoinJoin miner fee rate inf sat/vB is below required 2.0000 sat/vB",
        "CoinJoin miner fee rate Infinity sat/vB is below required 2.0000 sat/vB",
        "CoinJoin miner fee rate -1.0000 sat/vB is below required 2.0000 sat/vB",
        "CoinJoin miner fee rate 1e0 sat/vB is below required 2.0000 sat/vB",
        "CoinJoin miner fee rate 1.0000 sat/vB is below required 2E0 sat/vB",
        "CoinJoin miner fee rate 12345678901234567.0 sat/vB is below required 2.0 sat/vB",
        "CoinJoin miner fee rate 1.123456789 sat/vB is below required 2.0 sat/vB",
        "CoinJoin miner fee rate 2.0000 sat/vB is below required 0.0000 sat/vB",
        "CoinJoin miner fee rate 2.0001 sat/vB is below required 2.0000 sat/vB",
        "CoinJoin miner fee rate 1.0000 sat/vB is below required 2.0000 sat/vB\n",
        "CoinJoin miner fee rate 1.0000 sat/vB is below required 2.0000 sat/vB\x1b[2J",
        "peer-id CoinJoin miner fee rate 1.0000 sat/vB is below required 2.0000 sat/vB",
        None,
        1.0,
        b"CoinJoin miner fee rate 1.0000 sat/vB is below required 2.0000 sat/vB",
    ],
)
def test_low_fee_error_parser_rejects_untrusted_or_invalid_messages(message: object) -> None:
    assert parse_low_fee_error(message) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("static_floor,max_fee_rate", [(0.0, 1.0), (1.0, 0.0), (float("nan"), 1.0)])
async def test_resolve_min_fee_rate_rejects_invalid_runtime_policy_values(
    static_floor: float, max_fee_rate: float
) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        await resolve_min_fee_rate(
            MagicMock(),
            static_floor=static_floor,
            block_target=10,
            max_fee_rate=max_fee_rate,
        )
