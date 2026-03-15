"""
End-to-end tests for fee estimation functionality.

Tests fee rate handling with real Bitcoin Core backend:
- Backend fee estimation returns float (supports sub-1 sat/vB)
- Default 3-block fee estimation when connected to full node
- Manual fee rate override
- Block target override
- Neutrino backend returns can_estimate_fee() = False

Requires: docker compose up -d bitcoin
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
from jmwallet.backends.neutrino import NeutrinoBackend
from loguru import logger


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest_asyncio.fixture
async def bitcoin_backend(bitcoin_rpc_config: dict[str, str]):
    """Create and verify Bitcoin Core backend connection."""
    backend = BitcoinCoreBackend(
        rpc_url=bitcoin_rpc_config["rpc_url"],
        rpc_user=bitcoin_rpc_config["rpc_user"],
        rpc_password=bitcoin_rpc_config["rpc_password"],
    )

    # Verify Bitcoin Core is available
    try:
        height = await backend.get_block_height()
        logger.info(f"Bitcoin Core connected, height: {height}")
    except Exception as e:
        pytest.skip(f"Bitcoin Core not available: {e}")

    yield backend
    await backend.close()


# ==============================================================================
# Bitcoin Core Fee Estimation Tests
# ==============================================================================


@pytest.mark.e2e
class TestBitcoinCoreFeeEstimation:
    """Test fee estimation with real Bitcoin Core backend."""

    async def test_can_estimate_fee_returns_true(self, bitcoin_backend):
        """Test that Bitcoin Core backend reports it can estimate fees."""
        assert bitcoin_backend.can_estimate_fee() is True

    async def test_estimate_fee_returns_float(self, bitcoin_backend):
        """Test that fee estimation returns a float value."""
        fee = await bitcoin_backend.estimate_fee(target_blocks=3)

        assert isinstance(fee, float), f"Fee should be float, got {type(fee)}"
        logger.info(f"3-block fee estimate: {fee} sat/vB")

    async def test_estimate_fee_positive_or_fallback(self, bitcoin_backend):
        """Test that fee estimation returns a positive value (or fallback)."""
        fee = await bitcoin_backend.estimate_fee(target_blocks=6)

        # On regtest with no transactions, estimation may fail and return fallback (1.0)
        assert fee > 0, "Fee should be positive"
        assert fee >= 1.0, "Fee should be at least 1.0 sat/vB (minimum relay fee)"
        logger.info(f"6-block fee estimate: {fee} sat/vB")

    async def test_estimate_fee_different_targets(self, bitcoin_backend):
        """Test fee estimation with different block targets."""
        fee_1 = await bitcoin_backend.estimate_fee(target_blocks=1)
        fee_3 = await bitcoin_backend.estimate_fee(target_blocks=3)
        fee_6 = await bitcoin_backend.estimate_fee(target_blocks=6)
        fee_144 = await bitcoin_backend.estimate_fee(target_blocks=144)

        logger.info(
            f"Fee estimates: 1-block={fee_1}, 3-block={fee_3}, "
            f"6-block={fee_6}, 144-block={fee_144} sat/vB"
        )

        # All should be floats
        assert all(isinstance(f, float) for f in [fee_1, fee_3, fee_6, fee_144])

        # All should be positive
        assert all(f > 0 for f in [fee_1, fee_3, fee_6, fee_144])

        # On regtest with no mempool activity, all estimates may be the same (fallback)
        # In production, shorter targets should have higher fees

    async def test_fallback_fee_is_one_sat(self, bitcoin_backend):
        """Test that fallback fee when estimation fails is 1.0 sat/vB.

        On regtest with an empty mempool, estimatesmartfee returns an error.
        The backend should fall back to 1.0 sat/vB (not the old 10 sat/vB).

        This test verifies the fallback behavior regardless of whether Bitcoin Core
        can provide actual fee estimates.
        """
        # Request fee for very high block target which may not have estimation data
        fee = await bitcoin_backend.estimate_fee(target_blocks=1008)

        # On regtest with no mempool data, all estimates use the fallback (1.0)
        # This is expected behavior - the test verifies the fallback value is correct
        assert fee == 1.0, f"Expected fallback of 1.0 sat/vB, got {fee}"
        logger.info(f"1008-block fee (fallback): {fee} sat/vB")


# ==============================================================================
# Neutrino Backend Fee Estimation Tests
# ==============================================================================


@pytest.mark.neutrino
class TestNeutrinoFeeEstimation:
    """Test fee estimation with Neutrino backend."""

    @pytest_asyncio.fixture
    async def neutrino_backend(self):
        """Create Neutrino backend (may not be available)."""
        backend = NeutrinoBackend(
            neutrino_url="http://127.0.0.1:8335",
            network="regtest",
        )

        try:
            height = await backend.get_block_height()
            logger.info(f"Neutrino connected, height: {height}")
        except Exception as e:
            await backend.close()
            pytest.skip(f"Neutrino not available: {e}")

        yield backend
        await backend.close()

    async def test_can_estimate_fee_returns_false(self, neutrino_backend):
        """Test that Neutrino backend reports it cannot estimate fees."""
        assert neutrino_backend.can_estimate_fee() is False

    async def test_estimate_fee_returns_float(self, neutrino_backend):
        """Test that Neutrino returns float fallback values."""
        fee = await neutrino_backend.estimate_fee(target_blocks=3)

        assert isinstance(fee, float), f"Fee should be float, got {type(fee)}"
        assert fee == 2.0, "3-block should return 2.0 sat/vB fallback"
        logger.info(f"Neutrino 3-block fee: {fee} sat/vB")

    async def test_neutrino_fallback_values(self, neutrino_backend):
        """Test Neutrino fallback values for different targets.

        Neutrino cannot estimate fees and always returns hardcoded fallbacks.
        """
        fee_1 = await neutrino_backend.estimate_fee(target_blocks=1)
        fee_3 = await neutrino_backend.estimate_fee(target_blocks=3)
        fee_6 = await neutrino_backend.estimate_fee(target_blocks=6)
        fee_12 = await neutrino_backend.estimate_fee(target_blocks=12)

        logger.info(
            f"Neutrino fallback fees: 1-block={fee_1}, 3-block={fee_3}, "
            f"6-block={fee_6}, 12-block={fee_12} sat/vB"
        )

        # Verify exact fallback values (from neutrino.py)
        assert fee_1 == 5.0, "1-block fallback should be 5.0"
        assert fee_3 == 2.0, "3-block fallback should be 2.0"
        assert fee_6 == 1.0, "6-block fallback should be 1.0"
        assert fee_12 == 1.0, "12-block fallback should be 1.0"


# ==============================================================================
# Taker Fee Resolution Tests (Integration)
# ==============================================================================


@pytest.mark.e2e
class TestTakerFeeResolution:
    """Test taker fee resolution with real backend."""

    async def test_taker_resolves_fee_from_backend(self, bitcoin_backend):
        """Test that Taker resolves fee rate from backend."""
        from jmcore.models import NetworkType
        from jmwallet.wallet.service import WalletService
        from taker.config import TakerConfig
        from taker.taker import Taker

        # Test mnemonic
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

        wallet = WalletService(
            mnemonic=mnemonic,
            backend=bitcoin_backend,
            network="regtest",
        )

        config = TakerConfig(
            mnemonic=mnemonic,
            network=NetworkType.TESTNET,
            directory_servers=["127.0.0.1:5222"],
            # No fee_rate or fee_block_target specified - should use default 3-block
        )

        taker = Taker(wallet=wallet, backend=bitcoin_backend, config=config)

        # Resolve fee rate
        fee_rate = await taker._resolve_fee_rate()

        assert isinstance(fee_rate, float), (
            f"Fee rate should be float, got {type(fee_rate)}"
        )
        assert fee_rate > 0, "Fee rate should be positive"
        assert taker._fee_rate == fee_rate, "Fee rate should be cached"

        logger.info(f"Taker resolved fee rate: {fee_rate} sat/vB (default 3-block)")

    async def test_taker_uses_manual_fee_rate(self, bitcoin_backend):
        """Test that Taker uses manual fee rate when specified."""
        from jmcore.models import NetworkType
        from jmwallet.wallet.service import WalletService
        from taker.config import TakerConfig
        from taker.taker import Taker

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

        wallet = WalletService(
            mnemonic=mnemonic,
            backend=bitcoin_backend,
            network="regtest",
        )

        config = TakerConfig(
            mnemonic=mnemonic,
            network=NetworkType.TESTNET,
            directory_servers=["127.0.0.1:5222"],
            fee_rate=2.5,  # Manual fee rate
        )

        taker = Taker(wallet=wallet, backend=bitcoin_backend, config=config)

        fee_rate = await taker._resolve_fee_rate()

        assert fee_rate == 2.5, f"Expected manual fee rate 2.5, got {fee_rate}"
        logger.info(f"Taker using manual fee rate: {fee_rate} sat/vB")

    async def test_taker_uses_block_target(self, bitcoin_backend):
        """Test that Taker uses specified block target for estimation."""
        from jmcore.models import NetworkType
        from jmwallet.wallet.service import WalletService
        from taker.config import TakerConfig
        from taker.taker import Taker

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

        wallet = WalletService(
            mnemonic=mnemonic,
            backend=bitcoin_backend,
            network="regtest",
        )

        config = TakerConfig(
            mnemonic=mnemonic,
            network=NetworkType.TESTNET,
            directory_servers=["127.0.0.1:5222"],
            fee_block_target=6,  # Custom block target
        )

        taker = Taker(wallet=wallet, backend=bitcoin_backend, config=config)

        fee_rate = await taker._resolve_fee_rate()

        assert isinstance(fee_rate, float)
        assert fee_rate > 0
        logger.info(f"Taker resolved fee rate with 6-block target: {fee_rate} sat/vB")

    async def test_taker_sub_sat_fee_rate(self, bitcoin_backend):
        """Test that Taker supports sub-1 sat/vB fee rates."""
        from jmcore.models import NetworkType
        from jmwallet.wallet.service import WalletService
        from taker.config import TakerConfig
        from taker.taker import Taker

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

        wallet = WalletService(
            mnemonic=mnemonic,
            backend=bitcoin_backend,
            network="regtest",
        )

        config = TakerConfig(
            mnemonic=mnemonic,
            network=NetworkType.TESTNET,
            directory_servers=["127.0.0.1:5222"],
            fee_rate=0.5,  # Sub-1 sat/vB
        )

        taker = Taker(wallet=wallet, backend=bitcoin_backend, config=config)

        fee_rate = await taker._resolve_fee_rate()

        assert fee_rate == 0.5, f"Expected 0.5 sat/vB, got {fee_rate}"
        logger.info(f"Taker using sub-sat fee rate: {fee_rate} sat/vB")

        # Test fee calculation with sub-sat rate
        # 10 inputs, 10 outputs: vsize = 10*68 + 10*31 + 11 = 1001 vbytes
        # Fee at 0.5 sat/vB * 3.0 factor = 1.5 sat/vB effective
        # 1001 * 1.5 = 1501.5, rounded up = 1502 sats
        estimated_fee = taker._estimate_tx_fee(num_inputs=10, num_outputs=10)
        logger.info(f"Estimated fee for 10in/10out at 0.5 sat/vB: {estimated_fee} sats")
        assert estimated_fee > 0


# ==============================================================================
# Neutrino Taker Fee Resolution Tests
# ==============================================================================


@pytest.mark.neutrino
class TestNeutrinoTakerFeeResolution:
    """Test taker fee resolution with Neutrino backend."""

    @pytest_asyncio.fixture
    async def neutrino_backend(self):
        """Create Neutrino backend."""
        backend = NeutrinoBackend(
            neutrino_url="http://127.0.0.1:8335",
            network="regtest",
        )

        try:
            await backend.get_block_height()
        except Exception as e:
            await backend.close()
            pytest.skip(f"Neutrino not available: {e}")

        yield backend
        await backend.close()

    async def test_taker_block_target_with_neutrino_raises_error(
        self, neutrino_backend
    ):
        """Test that using --block-target with neutrino raises an error."""
        from jmcore.models import NetworkType
        from jmwallet.wallet.service import WalletService
        from taker.config import TakerConfig
        from taker.taker import Taker

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

        wallet = WalletService(
            mnemonic=mnemonic,
            backend=neutrino_backend,
            network="regtest",
        )

        config = TakerConfig(
            mnemonic=mnemonic,
            network=NetworkType.TESTNET,
            directory_servers=["127.0.0.1:5222"],
            fee_block_target=3,  # This should cause an error with neutrino
        )

        taker = Taker(wallet=wallet, backend=neutrino_backend, config=config)

        # Should raise ValueError because neutrino cannot estimate fees
        with pytest.raises(ValueError) as excinfo:
            await taker._resolve_fee_rate()

        assert "Cannot use --block-target with neutrino backend" in str(excinfo.value)
        logger.info("Correctly rejected block-target with neutrino backend")

    async def test_taker_manual_fee_with_neutrino_works(self, neutrino_backend):
        """Test that manual fee rate works with neutrino backend."""
        from jmcore.models import NetworkType
        from jmwallet.wallet.service import WalletService
        from taker.config import TakerConfig
        from taker.taker import Taker

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

        wallet = WalletService(
            mnemonic=mnemonic,
            backend=neutrino_backend,
            network="regtest",
        )

        config = TakerConfig(
            mnemonic=mnemonic,
            network=NetworkType.TESTNET,
            directory_servers=["127.0.0.1:5222"],
            fee_rate=2.0,  # Manual rate - should work with neutrino
        )

        taker = Taker(wallet=wallet, backend=neutrino_backend, config=config)

        fee_rate = await taker._resolve_fee_rate()

        assert fee_rate == 2.0, f"Expected 2.0 sat/vB, got {fee_rate}"
        logger.info(f"Neutrino taker using manual fee rate: {fee_rate} sat/vB")

    async def test_taker_default_fee_with_neutrino_uses_fallback(
        self, neutrino_backend
    ):
        """Test that default (no fee specified) uses fallback with neutrino."""
        from jmcore.models import NetworkType
        from jmwallet.wallet.service import WalletService
        from taker.config import TakerConfig
        from taker.taker import Taker

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )

        wallet = WalletService(
            mnemonic=mnemonic,
            backend=neutrino_backend,
            network="regtest",
        )

        config = TakerConfig(
            mnemonic=mnemonic,
            network=NetworkType.TESTNET,
            directory_servers=["127.0.0.1:5222"],
            # No fee_rate or fee_block_target - should use fallback
        )

        taker = Taker(wallet=wallet, backend=neutrino_backend, config=config)

        fee_rate = await taker._resolve_fee_rate()

        # Should use fallback (1.0 sat/vB) since neutrino can't estimate
        assert fee_rate == 1.0, f"Expected fallback 1.0 sat/vB, got {fee_rate}"
        logger.info(f"Neutrino taker using fallback fee rate: {fee_rate} sat/vB")
