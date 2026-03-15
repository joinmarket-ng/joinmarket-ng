"""
Tests for fee rate resolution in the Taker.

Tests the _resolve_fee_rate() method which determines the fee rate to use
for CoinJoin transactions, including the hard error when neutrino backend
cannot estimate fees and no manual --fee-rate is provided.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taker.taker import Taker


def _make_taker(
    *,
    fee_rate: float | None = None,
    fee_block_target: int | None = None,
    can_estimate_fee: bool = True,
    estimate_fee_return: float = 5.0,
    mempool_min_fee: float | None = None,
    tx_fee_factor: float = 0.2,
) -> Taker:
    """Create a Taker with mocked internals for _resolve_fee_rate testing.

    Bypasses __init__ to avoid directory client setup and other side effects.
    """
    with patch.object(Taker, "__init__", lambda self, *a, **kw: None):
        taker = Taker.__new__(Taker)

    taker.config = MagicMock()
    taker.config.fee_rate = fee_rate
    taker.config.fee_block_target = fee_block_target
    taker.config.tx_fee_factor = tx_fee_factor

    taker.backend = MagicMock()
    taker.backend.can_estimate_fee = MagicMock(return_value=can_estimate_fee)
    taker.backend.estimate_fee = AsyncMock(return_value=estimate_fee_return)
    taker.backend.get_mempool_min_fee = AsyncMock(return_value=mempool_min_fee)

    # Internal state that _resolve_fee_rate relies on
    taker._fee_rate = None
    taker._randomized_fee_rate = None

    return taker


class TestResolveFeeRate:
    """Tests for Taker._resolve_fee_rate()."""

    @pytest.mark.asyncio
    async def test_manual_fee_rate_takes_priority(self) -> None:
        """Path 1: Manual --fee-rate should be used directly."""
        taker = _make_taker(fee_rate=3.0)
        rate = await taker._resolve_fee_rate()
        assert rate == 3.0
        # Backend estimation should NOT be called
        taker.backend.estimate_fee.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_manual_fee_rate_raised_to_mempool_min(self) -> None:
        """Path 1: Manual fee rate below mempool min should be raised."""
        taker = _make_taker(fee_rate=1.0, mempool_min_fee=2.5)
        rate = await taker._resolve_fee_rate()
        assert rate == 2.5

    @pytest.mark.asyncio
    async def test_block_target_with_capable_backend(self) -> None:
        """Path 2: Block target estimation works with full node backend."""
        taker = _make_taker(
            fee_block_target=6,
            can_estimate_fee=True,
            estimate_fee_return=4.2,
        )
        rate = await taker._resolve_fee_rate()
        assert rate == 4.2
        taker.backend.estimate_fee.assert_awaited_once_with(6)

    @pytest.mark.asyncio
    async def test_block_target_with_neutrino_raises(self) -> None:
        """Path 2: Block target with neutrino backend should raise ValueError."""
        taker = _make_taker(
            fee_block_target=6,
            can_estimate_fee=False,
        )
        with pytest.raises(ValueError, match="Cannot use --block-target with neutrino"):
            await taker._resolve_fee_rate()

    @pytest.mark.asyncio
    async def test_default_estimation_with_capable_backend(self) -> None:
        """Path 3: Default 3-block estimation when backend supports it."""
        taker = _make_taker(
            can_estimate_fee=True,
            estimate_fee_return=7.5,
        )
        rate = await taker._resolve_fee_rate()
        assert rate == 7.5
        taker.backend.estimate_fee.assert_awaited_once_with(3)

    @pytest.mark.asyncio
    async def test_neutrino_without_fee_rate_falls_back(self) -> None:
        """Path 4: Neutrino backend without manual --fee-rate falls back to 1.0 sat/vB.

        Fee estimation is unavailable on neutrino, so without an explicit --fee-rate
        we fall back to a safe minimum rather than aborting the CoinJoin.
        """
        taker = _make_taker(can_estimate_fee=False)
        rate = await taker._resolve_fee_rate()
        assert rate == 1.0

    @pytest.mark.asyncio
    async def test_cached_fee_rate_returned_on_second_call(self) -> None:
        """Resolved fee rate should be cached and returned on subsequent calls."""
        taker = _make_taker(fee_rate=2.0)
        first = await taker._resolve_fee_rate()
        second = await taker._resolve_fee_rate()
        assert first == second == 2.0
        # get_mempool_min_fee should only be called once (first invocation)
        assert taker.backend.get_mempool_min_fee.await_count == 1

    @pytest.mark.asyncio
    async def test_estimation_raised_to_mempool_min(self) -> None:
        """Path 2/3: Estimated fee below mempool min should be raised."""
        taker = _make_taker(
            fee_block_target=6,
            can_estimate_fee=True,
            estimate_fee_return=0.5,
            mempool_min_fee=1.5,
        )
        rate = await taker._resolve_fee_rate()
        assert rate == 1.5

    @pytest.mark.asyncio
    async def test_mempool_min_fee_failure_does_not_block(self) -> None:
        """If get_mempool_min_fee raises, fee resolution should continue."""
        taker = _make_taker(fee_rate=3.0)
        taker.backend.get_mempool_min_fee = AsyncMock(side_effect=Exception("unavailable"))
        rate = await taker._resolve_fee_rate()
        assert rate == 3.0

    @pytest.mark.asyncio
    async def test_default_randomization_factor_uses_20_percent_band(self) -> None:
        """Default tx_fee_factor should randomize only within +20% band.

        Reference implementation default is tx_fees_factor=0.2.
        """
        taker = _make_taker(fee_rate=1.0, tx_fee_factor=0.2)

        with patch("random.uniform", return_value=1.2) as mock_uniform:
            await taker._resolve_fee_rate()

        mock_uniform.assert_called_once_with(1.0, 1.2)
        assert taker._randomized_fee_rate == 1.2
