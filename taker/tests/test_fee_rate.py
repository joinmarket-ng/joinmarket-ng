"""
Tests for fee rate resolution in the Taker.

Tests the _resolve_fee_rate() method which determines the fee rate to use
for CoinJoin transactions, including the hard error when neutrino backend
cannot estimate fees and no manual --fee-rate is provided.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.bitcoin import TxInput, TxOutput, serialize_transaction

from taker.coinjoin_session import CoinJoinSession
from taker.taker import Taker


def _make_taker(
    *,
    fee_rate: float | None = None,
    fee_block_target: int | None = None,
    can_estimate_fee: bool = True,
    estimate_fee_return: float = 5.0,
    mempool_min_fee: float | None = None,
    tx_fee_factor: float = 1.0,
    max_fee_rate_sat_vb: float = 1_000.0,
) -> Taker:
    """Create a Taker with mocked internals for _resolve_fee_rate testing.

    Bypasses __init__ to avoid directory client setup and other side effects.
    """
    with patch.object(Taker, "__init__", lambda self, *a, **kw: None):
        taker = Taker.__new__(Taker)

    # Per-CoinJoin state (fee rates, maker sessions, etc.) lives on a
    # CoinJoinSession; populate it so the @property proxies on Taker resolve.
    taker._session = CoinJoinSession()
    taker._session.attach(taker)

    taker.config = MagicMock()
    taker.config.fee_rate = fee_rate
    taker.config.fee_block_target = fee_block_target
    taker.config.tx_fee_factor = tx_fee_factor
    taker.config.max_fee_rate_sat_vb = max_fee_rate_sat_vb
    taker.config.min_fee_rate_sat_vb = 1.0
    taker.config.min_fee_block_target = 10

    taker.backend = MagicMock()
    taker.backend.can_estimate_fee = MagicMock(return_value=can_estimate_fee)
    taker.backend.estimate_fee = AsyncMock(return_value=estimate_fee_return)
    taker.backend.get_mempool_min_fee = AsyncMock(return_value=mempool_min_fee)

    # Internal state that _resolve_fee_rate relies on
    taker._session._fee_rate = None
    taker._session._randomized_fee_rate = None

    return taker


class TestResolveFeeRate:
    """Tests for Taker._resolve_fee_rate()."""

    @pytest.mark.asyncio
    async def test_manual_fee_rate_takes_priority(self) -> None:
        """Path 1: Manual --fee-rate should be used directly."""
        taker = _make_taker(fee_rate=6.0)
        rate = await taker._session._resolve_fee_rate()
        assert rate == 6.0
        # The transaction rate is manual, but the safety floor still resolves at 10 blocks.
        taker.backend.estimate_fee.assert_awaited_once_with(10)

    @pytest.mark.asyncio
    async def test_manual_fee_rate_below_minimum_is_rejected(self) -> None:
        """An explicit manual fee choice must not be silently raised."""
        taker = _make_taker(fee_rate=1.0, mempool_min_fee=2.5)
        with pytest.raises(ValueError, match="below the required minimum"):
            await taker._session._resolve_fee_rate()

    @pytest.mark.asyncio
    async def test_block_target_with_capable_backend(self) -> None:
        """Path 2: Block target estimation works with full node backend."""
        taker = _make_taker(
            fee_block_target=6,
            can_estimate_fee=True,
            estimate_fee_return=4.2,
        )
        rate = await taker._session._resolve_fee_rate()
        assert rate == 4.2
        assert taker.backend.estimate_fee.await_args_list == [((10,), {}), ((6,), {})]

    @pytest.mark.asyncio
    async def test_block_target_with_neutrino_raises(self) -> None:
        """Path 2: Block target with neutrino backend should raise ValueError."""
        taker = _make_taker(
            fee_block_target=6,
            can_estimate_fee=False,
        )
        with pytest.raises(ValueError, match="Cannot use --block-target with neutrino"):
            await taker._session._resolve_fee_rate()

    @pytest.mark.asyncio
    async def test_default_estimation_with_capable_backend(self) -> None:
        """Path 3: Default 3-block estimation when backend supports it."""
        taker = _make_taker(
            can_estimate_fee=True,
            estimate_fee_return=7.5,
        )
        rate = await taker._session._resolve_fee_rate()
        assert rate == 7.5
        assert taker.backend.estimate_fee.await_args_list == [((10,), {}), ((3,), {})]

    @pytest.mark.asyncio
    async def test_neutrino_without_fee_rate_falls_back(self) -> None:
        """Path 4: Neutrino backend without manual --fee-rate falls back to 1.0 sat/vB.

        Fee estimation is unavailable on neutrino, so without an explicit --fee-rate
        we fall back to a safe minimum rather than aborting the CoinJoin.
        """
        taker = _make_taker(can_estimate_fee=False)
        rate = await taker._session._resolve_fee_rate()
        assert rate == 1.0

    @pytest.mark.asyncio
    async def test_cached_fee_rate_returned_on_second_call(self) -> None:
        """Resolved fee rate should be cached and returned on subsequent calls."""
        taker = _make_taker(fee_rate=6.0)
        first = await taker._session._resolve_fee_rate()
        second = await taker._session._resolve_fee_rate()
        assert first == second == 6.0
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
        rate = await taker._session._resolve_fee_rate()
        assert rate == 1.5

    @pytest.mark.asyncio
    async def test_mempool_min_fee_failure_does_not_block(self) -> None:
        """If get_mempool_min_fee raises, fee resolution should continue."""
        taker = _make_taker(fee_rate=6.0)
        taker.backend.get_mempool_min_fee = AsyncMock(side_effect=Exception("unavailable"))
        rate = await taker._session._resolve_fee_rate()
        assert rate == 6.0


def _unsigned_fee_policy_tx(output_value: int) -> bytes:
    return serialize_transaction(
        version=2,
        inputs=[TxInput.from_hex("aa" * 32, 0)],
        outputs=[TxOutput(value=output_value, script=bytes.fromhex("0014" + "11" * 20))],
        locktime=0,
    )


class TestUnsignedMinimumFeePolicy:
    """Taker validation before any !tx signing request is sent."""

    def test_accepts_sufficient_unsigned_miner_fee(self) -> None:
        taker = _make_taker(can_estimate_fee=False)
        session = taker._session
        session._minimum_fee_rate_sat_vb = 2.0
        session.selected_utxos = [MagicMock(value=10_000)]
        session.maker_sessions = {}
        session.unsigned_tx = _unsigned_fee_policy_tx(9_700)

        assert session._verify_unsigned_miner_fee() is None

    @pytest.mark.asyncio
    async def test_rejects_low_unsigned_miner_fee_before_sending_tx(self) -> None:
        taker = _make_taker(can_estimate_fee=False)
        session = taker._session
        session._minimum_fee_rate_sat_vb = 2.0
        session.selected_utxos = [MagicMock(value=10_000)]
        session.maker_sessions = {}
        session.unsigned_tx = _unsigned_fee_policy_tx(9_900)
        taker.directory_client = MagicMock()
        taker.directory_client.send_privmsg = AsyncMock()

        assert await session._phase_collect_signatures() is False
        assert "below required" in (session.last_failure_reason or "")
        taker.directory_client.send_privmsg.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("minimum", [None, 1.0])
    async def test_rejects_excessive_effective_rate_before_signing_request(
        self, minimum: float | None
    ) -> None:
        taker = _make_taker(fee_rate=50.0, max_fee_rate_sat_vb=100.0)
        session = taker._session
        session._minimum_fee_rate_sat_vb = minimum
        session.selected_utxos = [MagicMock(value=100_000)]
        session.maker_sessions = {}
        # Selected 50 sat/vB does not protect against an inflated sweep budget.
        session.unsigned_tx = _unsigned_fee_policy_tx(80_000)
        taker.directory_client = MagicMock()
        taker.directory_client.send_privmsg = AsyncMock()
        session._sign_our_inputs = AsyncMock()

        assert await session._phase_collect_signatures() is False
        assert "exceeds safety cap" in (session.last_failure_reason or "")
        taker.directory_client.send_privmsg.assert_not_awaited()
        session._sign_our_inputs.assert_not_awaited()

    def test_unsigned_effective_rate_respects_explicit_higher_cap(self) -> None:
        taker = _make_taker(fee_rate=50.0, max_fee_rate_sat_vb=1_000.0)
        session = taker._session
        session._minimum_fee_rate_sat_vb = 1.0
        session.selected_utxos = [MagicMock(value=100_000)]
        session.maker_sessions = {}
        session.unsigned_tx = _unsigned_fee_policy_tx(80_000)

        assert session._verify_unsigned_miner_fee() is None


# ---------------------------------------------------------------------------
# Excessive-fee-rate cap
# ---------------------------------------------------------------------------


class TestResolveFeeRateCap:
    """Tests for the wallet-level fee-rate safety cap on the taker path."""

    @pytest.mark.asyncio
    async def test_manual_fee_rate_above_cap_rejected(self) -> None:
        from jmwallet.wallet.spend import ExcessiveFeeRateError

        taker = _make_taker(fee_rate=5_000.0, max_fee_rate_sat_vb=1_000.0)
        with pytest.raises(ExcessiveFeeRateError, match="exceeds safety cap"):
            await taker._session._resolve_fee_rate()
        # No randomized rate should have been computed.
        assert taker._session._randomized_fee_rate is None

    @pytest.mark.asyncio
    async def test_estimated_fee_rate_above_cap_rejected(self) -> None:
        """A backend that reports a wildly inflated estimate must not be used."""
        from jmwallet.wallet.spend import ExcessiveFeeRateError

        taker = _make_taker(
            fee_block_target=6,
            can_estimate_fee=True,
            estimate_fee_return=50_000.0,
            max_fee_rate_sat_vb=1_000.0,
        )
        with pytest.raises(ExcessiveFeeRateError, match="backend estimate"):
            await taker._session._resolve_fee_rate()
        assert taker._session._randomized_fee_rate is None

    @pytest.mark.asyncio
    async def test_default_estimation_above_cap_rejected(self) -> None:
        """The default 3-block estimation path is also bounded."""
        from jmwallet.wallet.spend import ExcessiveFeeRateError

        taker = _make_taker(
            can_estimate_fee=True,
            estimate_fee_return=10_000.0,
            max_fee_rate_sat_vb=1_000.0,
        )
        with pytest.raises(ExcessiveFeeRateError, match="backend estimate"):
            await taker._session._resolve_fee_rate()
        assert taker._session._randomized_fee_rate is None

    @pytest.mark.asyncio
    async def test_static_fallback_above_cap_is_rejected(self) -> None:
        from jmwallet.wallet.spend import ExcessiveFeeRateError

        taker = _make_taker(can_estimate_fee=False, max_fee_rate_sat_vb=0.5)
        with pytest.raises(ExcessiveFeeRateError, match="static floor"):
            await taker._session._resolve_fee_rate()

    @pytest.mark.asyncio
    async def test_manual_fee_rate_at_cap_passes(self) -> None:
        """The cap is inclusive: exactly the cap is acceptable."""
        taker = _make_taker(fee_rate=1_000.0, max_fee_rate_sat_vb=1_000.0)
        rate = await taker._session._resolve_fee_rate()
        assert rate == 1_000.0
        assert taker._session._randomized_fee_rate == 1_000.0

    @pytest.mark.asyncio
    async def test_randomized_fee_rate_is_limited_by_cap(self) -> None:
        taker = _make_taker(
            fee_rate=900.0,
            tx_fee_factor=1.0,
            max_fee_rate_sat_vb=1_000.0,
        )
        with patch("jmcore.randomness.secure_random.uniform", side_effect=lambda _low, high: high):
            await taker._session._resolve_fee_rate()
        assert taker._session._randomized_fee_rate == 1_000.0
