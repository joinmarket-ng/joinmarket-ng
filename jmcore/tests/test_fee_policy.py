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
@pytest.mark.parametrize(
    "static_floor,max_fee_rate",
    [(-0.1, 1.0), (1.0, 0.0), (float("nan"), 1.0), (float("inf"), 1.0), (False, 1.0)],
)
async def test_resolve_min_fee_rate_rejects_invalid_runtime_policy_values(
    static_floor: float, max_fee_rate: float
) -> None:
    with pytest.raises(ValueError, match="finite"):
        await resolve_min_fee_rate(
            MagicMock(),
            static_floor=static_floor,
            block_target=10,
            max_fee_rate=max_fee_rate,
        )


@pytest.mark.asyncio
async def test_zero_static_floor_allows_sub_one_fee_rates() -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=0.1)
    backend.can_estimate_fee.return_value = True
    backend.estimate_fee = AsyncMock(return_value=0.2)

    minimum = await resolve_min_fee_rate(
        backend, static_floor=0.0, block_target=10, max_fee_rate=1_000.0
    )

    assert minimum == 0.2
    assert fee_rate_meets_minimum(89, 100, minimum)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["unavailable", "invalid", "failed"])
async def test_zero_static_floor_does_not_invent_fallback(source_state: str) -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=None)
    backend.can_estimate_fee.return_value = False
    backend.estimate_fee = AsyncMock()
    if source_state == "invalid":
        backend.get_mempool_min_fee.return_value = float("nan")
        backend.can_estimate_fee.return_value = True
        backend.estimate_fee.return_value = -1.0
    elif source_state == "failed":
        backend.get_mempool_min_fee.side_effect = RuntimeError("offline")
        backend.can_estimate_fee.return_value = True
        backend.estimate_fee.side_effect = RuntimeError("offline")

    assert (
        await resolve_min_fee_rate(backend, static_floor=0.0, block_target=10, max_fee_rate=1_000.0)
        == 0.0
    )
    assert fee_rate_meets_minimum(0, 100, 0.0)
    assert not fee_rate_meets_minimum(-1, 100, 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "static_floor,mempool_minimum,expected", [(0.0, None, 0.0), (0.0, 0.1, 0.1), (1.0, 0.1, 1.0)]
)
async def test_disabled_estimator_retains_other_floors(
    static_floor: float, mempool_minimum: float | None, expected: float
) -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=mempool_minimum)
    backend.estimate_fee = AsyncMock()

    assert (
        await resolve_min_fee_rate(
            backend, static_floor=static_floor, block_target=-1, max_fee_rate=1_000.0
        )
        == expected
    )
    backend.get_mempool_min_fee.assert_awaited_once()
    backend.can_estimate_fee.assert_not_called()
    backend.estimate_fee.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_estimator_still_enforces_mempool_cap() -> None:
    backend = MagicMock()
    backend.get_mempool_min_fee = AsyncMock(return_value=4.0)
    backend.estimate_fee = AsyncMock()

    with pytest.raises(MinimumFeeRateExceedsCapError):
        await resolve_min_fee_rate(backend, static_floor=0.0, block_target=-1, max_fee_rate=3.0)
    backend.can_estimate_fee.assert_not_called()
    backend.estimate_fee.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_target", [-2, 0, 1009, True])
async def test_resolve_min_fee_rate_rejects_invalid_block_targets(block_target: int) -> None:
    with pytest.raises(ValueError, match="block target"):
        await resolve_min_fee_rate(
            MagicMock(), static_floor=0.0, block_target=block_target, max_fee_rate=1_000.0
        )
