"""
Tests for multi-offer functionality.

Tests the maker's ability to create and handle multiple offers simultaneously,
including both relative and absolute fee offers with different offer IDs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.models import NetworkType, Offer, OfferType

from maker.bot import MakerBot
from maker.config import MakerConfig, OfferConfig
from maker.offers import OfferManager


class TestOfferConfig:
    """Tests for OfferConfig model."""

    def test_default_offer_config(self):
        """Test default OfferConfig values match upstream JoinMarket reference."""
        cfg = OfferConfig()
        assert cfg.offer_type == OfferType.SW0_RELATIVE
        # Defaults aligned with upstream yg-privacyenhanced (issue #468)
        assert cfg.min_size == 100_000
        assert cfg.cj_fee_relative == "0.00002"
        assert cfg.cj_fee_absolute == 500
        assert cfg.tx_fee_contribution == 0
        assert cfg.cjfee_factor == 0.1
        assert cfg.txfee_contribution_factor == 0.3
        assert cfg.size_factor == 0.1

    def test_relative_offer_config(self):
        """Test relative fee offer configuration."""
        cfg = OfferConfig(
            offer_type=OfferType.SW0_RELATIVE,
            min_size=50_000,
            cj_fee_relative="0.0005",
            tx_fee_contribution=100,
        )
        assert cfg.offer_type == OfferType.SW0_RELATIVE
        assert cfg.get_cjfee() == "0.0005"

    def test_absolute_offer_config(self):
        """Test absolute fee offer configuration."""
        cfg = OfferConfig(
            offer_type=OfferType.SW0_ABSOLUTE,
            min_size=50_000,
            cj_fee_absolute=1000,
            tx_fee_contribution=100,
        )
        assert cfg.offer_type == OfferType.SW0_ABSOLUTE
        assert cfg.get_cjfee() == 1000

    def test_invalid_relative_fee_zero(self):
        """Test that zero relative fee is rejected."""
        with pytest.raises(ValueError, match="cj_fee_relative must be > 0"):
            OfferConfig(
                offer_type=OfferType.SW0_RELATIVE,
                cj_fee_relative="0",
            )

    def test_invalid_relative_fee_negative(self):
        """Test that negative relative fee is rejected."""
        with pytest.raises(ValueError, match="cj_fee_relative must be > 0"):
            OfferConfig(
                offer_type=OfferType.SW0_RELATIVE,
                cj_fee_relative="-0.001",
            )


class TestMakerConfigMultiOffer:
    """Tests for MakerConfig multi-offer support."""

    def test_empty_offer_configs_uses_legacy_fields(self):
        """Test that empty offer_configs falls back to legacy single-offer fields."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_type=OfferType.SW0_RELATIVE,
            min_size=200_000,
            cj_fee_relative="0.002",
            tx_fee_contribution=50,
        )

        effective = config.get_effective_offer_configs()
        assert len(effective) == 1
        assert effective[0].offer_type == OfferType.SW0_RELATIVE
        assert effective[0].min_size == 200_000
        assert effective[0].cj_fee_relative == "0.002"
        assert effective[0].tx_fee_contribution == 50

    def test_offer_configs_overrides_legacy_fields(self):
        """Test that offer_configs takes precedence over legacy fields."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            # Legacy fields (should be ignored)
            offer_type=OfferType.SW0_RELATIVE,
            cj_fee_relative="0.001",
            # Multi-offer configs (should be used)
            offer_configs=[
                OfferConfig(offer_type=OfferType.SW0_RELATIVE, cj_fee_relative="0.002"),
                OfferConfig(offer_type=OfferType.SW0_ABSOLUTE, cj_fee_absolute=1000),
            ],
        )

        effective = config.get_effective_offer_configs()
        assert len(effective) == 2
        assert effective[0].offer_type == OfferType.SW0_RELATIVE
        assert effective[0].cj_fee_relative == "0.002"
        assert effective[1].offer_type == OfferType.SW0_ABSOLUTE
        assert effective[1].cj_fee_absolute == 1000

    def test_dual_offers_config(self):
        """Test configuration with both relative and absolute offers."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,
                    cj_fee_relative="0.001",
                    tx_fee_contribution=0,
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=50_000,
                    cj_fee_absolute=500,
                    tx_fee_contribution=0,
                ),
            ],
        )

        effective = config.get_effective_offer_configs()
        assert len(effective) == 2

        # Check relative offer
        rel_cfg = effective[0]
        assert rel_cfg.offer_type == OfferType.SW0_RELATIVE
        assert rel_cfg.min_size == 100_000
        assert rel_cfg.get_cjfee() == "0.001"

        # Check absolute offer
        abs_cfg = effective[1]
        assert abs_cfg.offer_type == OfferType.SW0_ABSOLUTE
        assert abs_cfg.min_size == 50_000
        assert abs_cfg.get_cjfee() == 500


class TestOfferManagerMultiOffer:
    """Tests for OfferManager multi-offer creation."""

    @pytest.fixture
    def mock_wallet(self):
        """Create a mock wallet service."""
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        wallet.get_balance = AsyncMock(return_value=1_000_000)
        wallet.get_balance_for_offers = AsyncMock(return_value=1_000_000)
        return wallet

    @pytest.fixture
    def config_single_offer(self):
        """Config with single offer (legacy mode).

        Disables offer randomization so the test can assert exact cjfee values.
        """
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_type=OfferType.SW0_RELATIVE,
            min_size=100_000,
            cj_fee_relative="0.001",
            cjfee_factor=0.0,
            txfee_contribution_factor=0.0,
            size_factor=0.0,
        )

    @pytest.fixture
    def config_dual_offers(self):
        """Config with dual offers.

        Disables offer randomization so the test can assert exact cjfee values.
        """
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,
                    cj_fee_relative="0.001",
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=50_000,
                    cj_fee_absolute=500,
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_create_single_offer_legacy(self, mock_wallet, config_single_offer):
        """Test creating a single offer using legacy config."""
        manager = OfferManager(mock_wallet, config_single_offer, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        assert len(offers) == 1
        assert offers[0].oid == 0
        assert offers[0].ordertype == OfferType.SW0_RELATIVE
        assert offers[0].cjfee == "0.001"

    @pytest.mark.asyncio
    async def test_create_dual_offers(self, mock_wallet, config_dual_offers):
        """Test creating dual offers (relative and absolute)."""
        manager = OfferManager(mock_wallet, config_dual_offers, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        assert len(offers) == 2

        # Check offer IDs are unique and sequential
        assert offers[0].oid == 0
        assert offers[1].oid == 1

        # Check offer types
        assert offers[0].ordertype == OfferType.SW0_RELATIVE
        assert offers[0].cjfee == "0.001"

        assert offers[1].ordertype == OfferType.SW0_ABSOLUTE
        assert offers[1].cjfee == 500  # Absolute fee stored as int

    @pytest.mark.asyncio
    async def test_offers_share_fidelity_bond(self, mock_wallet, config_dual_offers):
        """Test that all offers share the same fidelity bond value."""
        manager = OfferManager(mock_wallet, config_dual_offers, "J5TestMaker")

        mock_bond = MagicMock()
        mock_bond.bond_value = 50_000
        mock_bond.txid = "ab" * 32
        mock_bond.vout = 0
        mock_bond.value = 100_000_000

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=mock_bond)):
            offers = await manager.create_offers()

        assert len(offers) == 2
        assert offers[0].fidelity_bond_value == 50_000
        assert offers[1].fidelity_bond_value == 50_000

    @pytest.mark.asyncio
    async def test_insufficient_balance_skips_offer(self, mock_wallet):
        """Test that offers requiring more than available balance are skipped."""
        # Balance is enough for second offer but not first
        # Need to account for dust_threshold (27300) being subtracted
        # 120_000 - 27300 = 92700 (not enough for 100k, but enough for 50k)
        mock_wallet.get_balance = AsyncMock(return_value=120_000)
        mock_wallet.get_balance_for_offers = AsyncMock(return_value=120_000)

        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,  # Too high (need > 100k after dust)
                    cj_fee_relative="0.001",
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=50_000,  # OK (92700 > 50000)
                    cj_fee_absolute=500,
                ),
            ],
        )

        manager = OfferManager(mock_wallet, config, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        # Only the second offer should be created
        assert len(offers) == 1
        assert offers[0].oid == 1  # Keeps original ID
        assert offers[0].ordertype == OfferType.SW0_ABSOLUTE

    def test_get_offer_by_id_found(self, mock_wallet, config_dual_offers):
        """Test finding an offer by ID."""
        manager = OfferManager(mock_wallet, config_dual_offers, "J5TestMaker")

        offers = [
            Offer(
                counterparty="J5TestMaker",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=900_000,
                txfee=0,
                cjfee="0.001",
            ),
            Offer(
                counterparty="J5TestMaker",
                oid=1,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=50_000,
                maxsize=900_000,
                txfee=0,
                cjfee=500,
            ),
        ]

        offer_0 = manager.get_offer_by_id(offers, 0)
        assert offer_0 is not None
        assert offer_0.oid == 0
        assert offer_0.ordertype == OfferType.SW0_RELATIVE

        offer_1 = manager.get_offer_by_id(offers, 1)
        assert offer_1 is not None
        assert offer_1.oid == 1
        assert offer_1.ordertype == OfferType.SW0_ABSOLUTE

    def test_get_offer_by_id_not_found(self, mock_wallet, config_dual_offers):
        """Test that None is returned for non-existent offer ID."""
        manager = OfferManager(mock_wallet, config_dual_offers, "J5TestMaker")

        offers = [
            Offer(
                counterparty="J5TestMaker",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=900_000,
                txfee=0,
                cjfee="0.001",
            ),
        ]

        assert manager.get_offer_by_id(offers, 1) is None
        assert manager.get_offer_by_id(offers, 99) is None


class TestMakerBotMultiOfferFill:
    """Tests for MakerBot !fill handling with multiple offers."""

    @pytest.fixture
    def mock_wallet(self):
        """Create a mock wallet service."""
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        return wallet

    @pytest.fixture
    def mock_backend(self):
        """Create a mock blockchain backend."""
        backend = MagicMock()
        backend.can_provide_neutrino_metadata = MagicMock(return_value=True)
        backend.requires_neutrino_metadata = MagicMock(return_value=False)
        return backend

    @pytest.fixture
    def config(self):
        """Create a test maker config with dual offers."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,
                    cj_fee_relative="0.001",
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=50_000,
                    cj_fee_absolute=500,
                ),
            ],
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        """Create a MakerBot with dual offers."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )
        # Set up current offers
        bot.current_offers = [
            Offer(
                counterparty=bot.nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=900_000,
                txfee=0,
                cjfee="0.001",
            ),
            Offer(
                counterparty=bot.nick,
                oid=1,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=50_000,
                maxsize=900_000,
                txfee=0,
                cjfee=500,
            ),
        ]
        return bot

    @pytest.mark.asyncio
    async def test_fill_relative_offer(self, maker_bot, mock_backend):
        """Test !fill for relative fee offer (oid=0)."""
        mock_backend.requires_neutrino_metadata = MagicMock(return_value=False)

        fill_data = None

        async def capture_handle_fill(amount, commitment, taker_pk):
            nonlocal fill_data
            fill_data = {"amount": amount, "commitment": commitment, "taker_pk": taker_pk}
            return True, {"nacl_pubkey": "abc123", "features": ["neutrino_compat"]}

        # Mock the CoinJoinSession.handle_fill
        with patch("maker.protocol_handlers.CoinJoinSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.handle_fill = capture_handle_fill
            mock_session.validate_channel = MagicMock(return_value=True)
            mock_session_class.return_value = mock_session

            with patch("maker.protocol_handlers.check_commitment", return_value=True):
                with patch.object(maker_bot, "_send_response", new=AsyncMock()):
                    await maker_bot._handle_fill(
                        "J5Taker123",
                        f"fill 0 500000 taker_pk_hex P{'aa' * 32}",
                    )

        # Verify the correct offer was used
        mock_session_class.assert_called_once()
        call_kwargs = mock_session_class.call_args[1]
        assert call_kwargs["offer"].oid == 0
        assert call_kwargs["offer"].ordertype == OfferType.SW0_RELATIVE

    @pytest.mark.asyncio
    async def test_fill_absolute_offer(self, maker_bot, mock_backend):
        """Test !fill for absolute fee offer (oid=1)."""
        mock_backend.requires_neutrino_metadata = MagicMock(return_value=False)

        async def mock_handle_fill(amount, commitment, taker_pk):
            return True, {"nacl_pubkey": "abc123", "features": ["neutrino_compat"]}

        with patch("maker.protocol_handlers.CoinJoinSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.handle_fill = mock_handle_fill
            mock_session.validate_channel = MagicMock(return_value=True)
            mock_session_class.return_value = mock_session

            with patch("maker.protocol_handlers.check_commitment", return_value=True):
                with patch.object(maker_bot, "_send_response", new=AsyncMock()):
                    await maker_bot._handle_fill(
                        "J5Taker456",
                        f"fill 1 200000 taker_pk_hex P{'bb' * 32}",
                    )

        # Verify the correct offer was used
        mock_session_class.assert_called_once()
        call_kwargs = mock_session_class.call_args[1]
        assert call_kwargs["offer"].oid == 1
        assert call_kwargs["offer"].ordertype == OfferType.SW0_ABSOLUTE

    @pytest.mark.asyncio
    async def test_fill_invalid_offer_id_rejected(self, maker_bot):
        """Test that !fill with invalid offer ID is rejected."""
        with patch("maker.protocol_handlers.check_commitment", return_value=True):
            await maker_bot._handle_fill(
                "J5Taker789",
                f"fill 99 500000 taker_pk_hex P{'cc' * 32}",  # oid=99 doesn't exist
            )

        # Should not create a session - the invalid offer ID causes rejection
        assert "J5Taker789" not in maker_bot.active_sessions

    @pytest.mark.asyncio
    async def test_fill_amount_validation_per_offer(self, maker_bot):
        """Test that amount validation is per-offer."""
        # Try to fill the absolute offer (oid=1, min_size=50_000) with amount below minimum
        with patch("maker.protocol_handlers.check_commitment", return_value=True):
            await maker_bot._handle_fill(
                "J5TakerLow",
                f"fill 1 30000 taker_pk_hex P{'dd' * 32}",  # Below min_size=50_000
            )

        # Should not create a session - amount validation fails
        assert "J5TakerLow" not in maker_bot.active_sessions

    @pytest.mark.asyncio
    async def test_fill_amount_validation_succeeds_for_correct_offer(self, maker_bot, mock_backend):
        """Test that amount validation passes when using the right offer."""
        mock_backend.requires_neutrino_metadata = MagicMock(return_value=False)

        async def mock_handle_fill(amount, commitment, taker_pk):
            return True, {"nacl_pubkey": "abc123", "features": ["neutrino_compat"]}

        with patch("maker.protocol_handlers.CoinJoinSession") as mock_session_class:
            mock_session = MagicMock()
            mock_session.handle_fill = mock_handle_fill
            mock_session.validate_channel = MagicMock(return_value=True)
            mock_session_class.return_value = mock_session

            with patch("maker.protocol_handlers.check_commitment", return_value=True):
                with patch.object(maker_bot, "_send_response", new=AsyncMock()):
                    # Fill absolute offer (oid=1, min_size=50_000) with 60_000 - should work
                    await maker_bot._handle_fill(
                        "J5TakerOK",
                        f"fill 1 60000 taker_pk_hex P{'ee' * 32}",
                    )

        # Session should be created
        assert "J5TakerOK" in maker_bot.active_sessions


class TestMakerBotOfferAnnouncement:
    """Tests for offer announcement with multiple offers."""

    @pytest.fixture
    def mock_wallet(self):
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        return wallet

    @pytest.fixture
    def mock_backend(self):
        return MagicMock()

    @pytest.fixture
    def config(self):
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        return MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

    def test_format_relative_offer(self, maker_bot):
        """Test formatting a relative fee offer."""
        offer = Offer(
            counterparty=maker_bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=900_000,
            txfee=0,
            cjfee="0.001",
        )

        msg = maker_bot._format_offer_announcement(offer)
        parts = msg.split()

        assert parts[0] == "sw0reloffer"
        assert parts[1] == "0"  # oid
        assert parts[5] == "0.001"  # cjfee (relative)

    def test_format_absolute_offer(self, maker_bot):
        """Test formatting an absolute fee offer."""
        offer = Offer(
            counterparty=maker_bot.nick,
            oid=1,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=50_000,
            maxsize=900_000,
            txfee=0,
            cjfee=500,
        )

        msg = maker_bot._format_offer_announcement(offer)
        parts = msg.split()

        assert parts[0] == "sw0absoffer"
        assert parts[1] == "1"  # oid
        assert parts[5] == "500"  # cjfee (absolute)

    @pytest.mark.asyncio
    async def test_announce_multiple_offers(self, maker_bot):
        """Test that all offers are announced."""
        maker_bot.current_offers = [
            Offer(
                counterparty=maker_bot.nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=900_000,
                txfee=0,
                cjfee="0.001",
            ),
            Offer(
                counterparty=maker_bot.nick,
                oid=1,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=50_000,
                maxsize=900_000,
                txfee=0,
                cjfee=500,
            ),
        ]

        # Mock directory client
        mock_client = MagicMock()
        mock_client.send_public_message = AsyncMock()
        maker_bot.directory_clients["test:5222"] = mock_client

        await maker_bot._announce_offers()

        # Should have sent 2 messages (one per offer)
        assert mock_client.send_public_message.call_count == 2

        # Check that both offer types were announced
        calls = mock_client.send_public_message.call_args_list
        messages = [call[0][0] for call in calls]

        assert any("sw0reloffer" in msg for msg in messages)
        assert any("sw0absoffer" in msg for msg in messages)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestOfferRandomization:
    """Tests for the maker offer randomization (issue #468).

    Defaults match the upstream JoinMarket yg-privacyenhanced reference so
    jm-ng makers cannot be distinguished from reference makers by their
    advertised values alone.
    """

    @pytest.fixture
    def randomized_wallet(self):
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        wallet.get_balance = AsyncMock(return_value=10_000_000)
        wallet.get_balance_for_offers = AsyncMock(return_value=10_000_000)
        return wallet

    @pytest.fixture
    def randomized_config(self):
        # Use upstream-aligned defaults; tx_fee_contribution=0 so the
        # profitability-floor doesn't push minsize past max_balance for the
        # tiny default cj_fee_relative.
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_type=OfferType.SW0_RELATIVE,
            min_size=100_000,
            cj_fee_relative="0.00002",
            tx_fee_contribution=0,
            cjfee_factor=0.1,
            txfee_contribution_factor=0.3,
            size_factor=0.1,
        )

    @pytest.mark.asyncio
    async def test_relative_cjfee_randomized_within_factor(
        self, randomized_wallet, randomized_config
    ):
        """Advertised cjfee must stay within +/- cjfee_factor of the configured value."""
        base = 0.00002
        factor = 0.1
        seen: set[str] = set()
        for _ in range(50):
            manager = OfferManager(randomized_wallet, randomized_config, "J5TestMaker")
            with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
                offers = await manager.create_offers()
            assert len(offers) == 1
            cjfee_str = offers[0].cjfee
            assert isinstance(cjfee_str, str)
            seen.add(cjfee_str)
            value = float(cjfee_str)
            assert base * (1 - factor) <= value <= base * (1 + factor), cjfee_str
            # No scientific notation on the wire.
            assert "e" not in cjfee_str.lower()

        # We expect *some* variation across 50 draws.
        assert len(seen) > 1, "cjfee was never randomized"

    @pytest.mark.asyncio
    async def test_minsize_clamped_to_dust(self, randomized_wallet):
        """Randomized minsize must never drop below the dust threshold."""
        from jmcore.constants import DUST_THRESHOLD

        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_type=OfferType.SW0_RELATIVE,
            min_size=DUST_THRESHOLD,  # at the floor
            cj_fee_relative="0.00002",
            size_factor=0.5,  # aggressive
        )
        for _ in range(20):
            manager = OfferManager(randomized_wallet, cfg, "J5TestMaker")
            with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
                offers = await manager.create_offers()
            assert len(offers) == 1
            assert offers[0].minsize >= DUST_THRESHOLD

    @pytest.mark.asyncio
    async def test_txfee_zero_stays_zero(self, randomized_wallet):
        """A zero tx_fee_contribution must remain zero regardless of factor."""
        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_type=OfferType.SW0_RELATIVE,
            min_size=100_000,
            cj_fee_relative="0.00002",
            tx_fee_contribution=0,
            txfee_contribution_factor=0.3,
        )
        for _ in range(10):
            manager = OfferManager(randomized_wallet, cfg, "J5TestMaker")
            with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
                offers = await manager.create_offers()
            assert offers[0].txfee == 0

    @pytest.mark.asyncio
    async def test_factor_zero_disables_randomization(self, randomized_wallet):
        """All factors set to zero produce stable, deterministic offer values."""
        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_type=OfferType.SW0_RELATIVE,
            min_size=100_000,
            cj_fee_relative="0.001",  # larger fee so tx_fee_contribution>0 stays profitable
            tx_fee_contribution=1000,
            cjfee_factor=0.0,
            txfee_contribution_factor=0.0,
            size_factor=0.0,
        )
        first: tuple[str | int, int, int] | None = None
        for _ in range(5):
            manager = OfferManager(randomized_wallet, cfg, "J5TestMaker")
            with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
                offers = await manager.create_offers()
            snap = (offers[0].cjfee, offers[0].txfee, offers[0].minsize)
            if first is None:
                first = snap
            assert snap == first
        assert first is not None
        assert first[0] == "0.001"
        assert first[1] == 1000


class TestDualOfferAutoSplit:
    """Tests for the dual-offer rel/abs intersection auto-split (issue #88).

    When the maker advertises exactly one relative offer and one absolute
    offer, ``OfferManager`` carves the available size range into two
    contiguous, non-overlapping segments at the fee intersection
    ``x = cj_fee_absolute / cj_fee_relative``.  The absolute offer covers
    ``[cfg.min_size, intersection]``; the relative offer covers
    ``[intersection, max_available]``.
    """

    @pytest.fixture
    def wallet_10m(self):
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        wallet.get_balance = AsyncMock(return_value=10_000_000)
        wallet.get_balance_for_offers = AsyncMock(return_value=10_000_000)
        return wallet

    @staticmethod
    def _dual_config(
        rel_fee: str = "0.001",
        abs_fee: int = 1000,
        rel_min: int = 100_000,
        abs_min: int = 50_000,
    ) -> MakerConfig:
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=rel_min,
                    cj_fee_relative=rel_fee,
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=abs_min,
                    cj_fee_absolute=abs_fee,
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,
                ),
            ],
        )

    @pytest.mark.asyncio
    async def test_auto_split_at_intersection(self, wallet_10m):
        """abs offer is capped at the intersection, rel offer floored at it."""
        # intersection = abs_fee / rel_fee = 1000 / 0.001 = 1_000_000 sats
        cfg = self._dual_config(rel_fee="0.001", abs_fee=1000)
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        assert len(offers) == 2
        rel = next(o for o in offers if o.ordertype == OfferType.SW0_RELATIVE)
        abs_ = next(o for o in offers if o.ordertype == OfferType.SW0_ABSOLUTE)

        intersection = 1_000_000
        # abs offer covers small CJs, capped at intersection
        assert abs_.minsize == 50_000
        assert abs_.maxsize == intersection
        # rel offer takes over above intersection
        assert rel.minsize == intersection
        assert rel.maxsize > intersection
        # Contiguous, non-overlapping coverage
        assert abs_.maxsize == rel.minsize

    @pytest.mark.asyncio
    async def test_auto_split_with_different_fee_ratio(self, wallet_10m):
        """Intersection scales with the ratio of the two fees."""
        # 2000 / 0.0005 = 4_000_000
        cfg = self._dual_config(rel_fee="0.0005", abs_fee=2000)
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        assert len(offers) == 2
        abs_ = next(o for o in offers if o.ordertype == OfferType.SW0_ABSOLUTE)
        rel = next(o for o in offers if o.ordertype == OfferType.SW0_RELATIVE)
        assert abs_.maxsize == 4_000_000
        assert rel.minsize == 4_000_000

    @pytest.mark.asyncio
    async def test_intersection_below_abs_min_drops_abs_offer(self, wallet_10m):
        """When abs_fee/rel_fee is below abs.min_size the abs offer is dropped.

        The absolute offer would only be cheaper for CJ amounts below the
        intersection.  If that point sits below the configured abs.min_size
        the offer cannot ever undercut the relative one and is suppressed.
        """
        # intersection = 100 / 0.01 = 10_000, but abs.min_size = 50_000
        cfg = self._dual_config(rel_fee="0.01", abs_fee=100, rel_min=100_000, abs_min=50_000)
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        assert len(offers) == 1
        assert offers[0].ordertype == OfferType.SW0_RELATIVE

    @pytest.mark.asyncio
    async def test_intersection_above_balance_drops_rel_offer(self, wallet_10m):
        """When abs_fee/rel_fee is above max balance the rel offer is dropped."""
        # intersection = 1_000_000 / 0.00002 = 50_000_000_000 (way above 10M balance)
        cfg = self._dual_config(rel_fee="0.00002", abs_fee=1_000_000)
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        assert len(offers) == 1
        assert offers[0].ordertype == OfferType.SW0_ABSOLUTE

    @pytest.mark.asyncio
    async def test_no_split_when_both_offers_relative(self, wallet_10m):
        """Two relative offers must not trigger the rel/abs auto-split."""
        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,
                    cj_fee_relative="0.001",
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=200_000,
                    cj_fee_relative="0.002",
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,
                ),
            ],
        )
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        # Both offers retain their configured min/max ranges; no split.
        assert len(offers) == 2
        assert offers[0].minsize == 100_000
        assert offers[1].minsize == 200_000
        # Both reach up to (close to) max_available, i.e. they overlap as
        # before -- the split logic only fires for rel + abs pairs.
        assert offers[0].maxsize == offers[1].maxsize

    @pytest.mark.asyncio
    async def test_single_offer_unaffected(self, wallet_10m):
        """Single-offer configs must not be affected by the split."""
        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_type=OfferType.SW0_ABSOLUTE,
            min_size=50_000,
            cj_fee_absolute=500,
            cjfee_factor=0.0,
            txfee_contribution_factor=0.0,
            size_factor=0.0,
        )
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")

        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        assert len(offers) == 1
        assert offers[0].ordertype == OfferType.SW0_ABSOLUTE
        assert offers[0].minsize == 50_000
        # max reaches the wallet's max_available (no override).
        assert offers[0].maxsize > 1_000_000

    @pytest.mark.asyncio
    async def test_auto_split_seam_is_exact_under_randomization(self, wallet_10m):
        """The boundary at the intersection is preserved even with size_factor>0.

        The auto-split must pin the abs.maxsize and rel.minsize to the exact
        intersection so the two offers stay seamless; randomization is still
        applied to the *outer* (un-pinned) edges.
        """
        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,
                    cj_fee_relative="0.001",
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.2,  # randomize outer edges
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=50_000,
                    cj_fee_absolute=1000,
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.2,
                ),
            ],
        )
        for _ in range(20):
            manager = OfferManager(wallet_10m, cfg, "J5TestMaker")
            with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
                offers = await manager.create_offers()
            assert len(offers) == 2
            abs_ = next(o for o in offers if o.ordertype == OfferType.SW0_ABSOLUTE)
            rel = next(o for o in offers if o.ordertype == OfferType.SW0_RELATIVE)
            # Seam stays exact regardless of randomization
            assert abs_.maxsize == 1_000_000
            assert rel.minsize == 1_000_000

    def test_compute_overrides_helper_three_offers(self, wallet_10m):
        """Helper returns no overrides when there are not exactly two offers."""
        cfg = self._dual_config()
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")
        configs = [
            OfferConfig(offer_type=OfferType.SW0_RELATIVE, cj_fee_relative="0.001"),
            OfferConfig(offer_type=OfferType.SW0_ABSOLUTE, cj_fee_absolute=500),
            OfferConfig(offer_type=OfferType.SW0_RELATIVE, cj_fee_relative="0.002"),
        ]
        fees = [("0.001", 0, 0.001), ("500", 0, 500.0), ("0.002", 0, 0.002)]
        assert manager._compute_dual_offer_size_overrides(configs, fees, 10_000_000) == (
            {},
            set(),
        )

    def test_compute_overrides_helper_zero_abs_fee(self, wallet_10m):
        """Zero randomized abs fee disables the auto-split (intersection at 0)."""
        cfg = self._dual_config()
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")
        configs = [
            OfferConfig(offer_type=OfferType.SW0_RELATIVE, cj_fee_relative="0.001"),
            OfferConfig(offer_type=OfferType.SW0_ABSOLUTE, cj_fee_absolute=1000),
        ]
        # Simulate randomization that produced zero abs fee (e.g. heavy factor)
        fees = [("0.001", 0, 0.001), ("0", 0, 0.0)]
        assert manager._compute_dual_offer_size_overrides(configs, fees, 10_000_000) == (
            {},
            set(),
        )

    @pytest.mark.asyncio
    async def test_seam_exact_with_txfee_deduction(self, wallet_10m):
        """abs.maxsize stays at (or just below) the intersection with txfee_contribution > 0.

        When ``tx_fee_contribution`` is non-zero the effective ``max_available``
        inside ``_create_single_offer`` is ``max_balance - txfee``, which is
        strictly less than ``max_size_override`` (= intersection).  The old
        guard ``max_available == max_size_override`` would silently miss the
        pin and let size_factor randomization scatter the seam.  The corrected
        guard ``max_size_override is not None`` must fire regardless.
        """
        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,
                    cj_fee_relative="0.001",
                    cjfee_factor=0.0,
                    tx_fee_contribution=5000,
                    txfee_contribution_factor=0.3,  # introduces randomized deduction
                    size_factor=0.2,
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=50_000,
                    cj_fee_absolute=1000,
                    cjfee_factor=0.0,
                    tx_fee_contribution=5000,
                    txfee_contribution_factor=0.3,
                    size_factor=0.2,
                ),
            ],
        )
        intersection = 1_000_000  # unrandomized value, used only as upper bound
        for _ in range(30):
            manager = OfferManager(wallet_10m, cfg, "J5TestMaker")
            with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
                offers = await manager.create_offers()
            assert len(offers) == 2
            abs_ = next(o for o in offers if o.ordertype == OfferType.SW0_ABSOLUTE)
            rel = next(o for o in offers if o.ordertype == OfferType.SW0_RELATIVE)
            # Seam must be contiguous (both sides pinned to the same value).
            assert abs_.maxsize == rel.minsize
            # With txfee deduction the seam is at or below the nominal intersection.
            assert abs_.maxsize <= intersection

    @pytest.mark.asyncio
    async def test_outer_edges_are_randomized(self, wallet_10m):
        """Outer edges (rel.maxsize, cjfee) are still randomized.

        The seam boundary varies with randomized fees (tested separately in
        test_intersection_uses_randomized_fees_not_configured), and the
        outer bounds (rel.maxsize) vary with size_factor.
        """
        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,
                    cj_fee_relative="0.001",
                    cjfee_factor=0.0,  # keep fees fixed so only size varies
                    txfee_contribution_factor=0.0,
                    size_factor=0.3,
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=50_000,
                    cj_fee_absolute=1000,
                    cjfee_factor=0.0,
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,  # abs outer edge (minsize) is at dust threshold, not varied
                ),
            ],
        )
        rel_maxsizes: set[int] = set()
        for _ in range(40):
            manager = OfferManager(wallet_10m, cfg, "J5TestMaker")
            with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
                offers = await manager.create_offers()
            assert len(offers) == 2
            abs_ = next(o for o in offers if o.ordertype == OfferType.SW0_ABSOLUTE)
            rel = next(o for o in offers if o.ordertype == OfferType.SW0_RELATIVE)
            # Seam must stay consistent between the two offers
            assert abs_.maxsize == rel.minsize
            rel_maxsizes.add(rel.maxsize)
        # With size_factor=0.3 over 40 trials rel.maxsize should vary
        assert len(rel_maxsizes) > 1, "rel.maxsize should be randomized across announcements"

    @pytest.mark.asyncio
    async def test_intersection_uses_randomized_fees_not_configured(self, wallet_10m):
        """The size boundary must be derived from randomized fees, not config values.

        If the intersection were computed from the unrandomized config values
        (abs=1000, rel=0.001 -> always 1_000_000), the boundary would be a
        fixed constant across all announcements, leaking the true fee
        configuration.  With fee randomization applied first the boundary
        varies announcement-to-announcement, hiding the underlying config.
        """
        cfg = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=100_000,
                    cj_fee_relative="0.001",
                    cjfee_factor=0.3,  # large factor -> significant fee spread
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,
                ),
                OfferConfig(
                    offer_type=OfferType.SW0_ABSOLUTE,
                    min_size=50_000,
                    cj_fee_absolute=1000,
                    cjfee_factor=0.3,
                    txfee_contribution_factor=0.0,
                    size_factor=0.0,
                ),
            ],
        )
        # Collect the seam (abs.maxsize == rel.minsize) across many runs
        seam_values: set[int] = set()
        for _ in range(50):
            manager = OfferManager(wallet_10m, cfg, "J5TestMaker")
            with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
                offers = await manager.create_offers()
            if len(offers) == 2:
                abs_ = next(o for o in offers if o.ordertype == OfferType.SW0_ABSOLUTE)
                rel = next(o for o in offers if o.ordertype == OfferType.SW0_RELATIVE)
                assert abs_.maxsize == rel.minsize, "seam must be contiguous"
                seam_values.add(abs_.maxsize)

        # If intersection were computed from unrandomized fees there would be
        # only one seam value (1_000_000).  With randomized fees the seam
        # varies across announcements.
        assert len(seam_values) > 1, (
            "seam should vary across announcements when cjfee_factor > 0; "
            f"got constant seam at {seam_values}"
        )

    @pytest.mark.asyncio
    async def test_intersection_inside_dust_band_drops_rel_offer(self):
        """Regression: intersection in ``(max_available, max_balance]`` drops rel.

        Previously the suppression branch compared the intersection against
        the gross ``max_balance`` and only fired when the intersection
        exceeded the full balance.  An intersection that fell strictly
        inside the band between ``max_balance - dust_threshold`` (the
        actual ``max_available``) and ``max_balance`` slipped through to
        the "standard split" branch.  The rel offer then received a
        ``min_size_override`` larger than its ``max_available`` and was
        rejected by ``_create_single_offer`` with a confusing
        "Insufficient balance: max_available=X <= min_size=max_balance"
        warning instead of being suppressed cleanly.

        Reproduces the bug from the operator report between 0.28.1 and
        0.29.0: with the default ``dust_threshold = 27300``, a balance
        whose intersection falls in the dust band must cleanly drop the
        rel offer rather than emit it with an unfillable min_size.
        """
        # Pick a balance where max_available comfortably exceeds the dust
        # threshold so the abs offer can be created, while still leaving
        # an intersection that lands in the (max_available, max_balance]
        # band for the chosen fees.
        # max_balance = 200_000  ->  max_available = 200_000 - 27_300 = 172_700
        # intersection = 1000 / 0.005 = 200_000 == max_balance (inside the
        # band [172_700, 200_000]).  Pre-fix this slipped through to the
        # standard branch and produced a rel offer with min_size = 200_000.
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        wallet.get_balance = AsyncMock(return_value=200_000)
        wallet.get_balance_for_offers = AsyncMock(return_value=200_000)

        cfg = self._dual_config(rel_fee="0.005", abs_fee=1000, rel_min=50_000, abs_min=50_000)
        manager = OfferManager(wallet, cfg, "J5TestMaker")
        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        # rel offer must be suppressed cleanly: no offer announced with
        # ``min_size`` at or above the actual max_available.
        rel_offers = [o for o in offers if o.ordertype == OfferType.SW0_RELATIVE]
        assert rel_offers == [], (
            f"rel offer with intersection above max_available must be suppressed, got {rel_offers}"
        )
        # abs offer should be created, covering up to the usable balance
        abs_offers = [o for o in offers if o.ordertype == OfferType.SW0_ABSOLUTE]
        assert len(abs_offers) == 1
        # max_available = 200_000 - 27_300 = 172_700
        assert abs_offers[0].maxsize == 172_700
        # min_size must stay fillable
        assert abs_offers[0].minsize < abs_offers[0].maxsize

    @pytest.mark.asyncio
    async def test_intersection_inside_dust_band_no_offer_minsize_exceeds_balance(self, wallet_10m):
        """No announced offer may carry a ``min_size`` larger than its ``max_available``.

        General invariant covering the dual-offer auto-split: regardless
        of where the intersection falls, every offer that
        ``create_offers`` returns must be fillable (i.e. its ``minsize``
        must not exceed the wallet's ``max_available``).  This guards
        against future regressions of the "min_size == max_balance" bug.
        """
        # intersection = 1000 / 0.001 = 1_000_000 -- well below 10M balance,
        # so the standard split applies and both offers should be valid.
        cfg = self._dual_config(rel_fee="0.001", abs_fee=1000)
        manager = OfferManager(wallet_10m, cfg, "J5TestMaker")
        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        assert offers, "expected at least one offer for the standard split case"
        for offer in offers:
            assert offer.minsize < offer.maxsize, (
                f"offer {offer.oid} has minsize {offer.minsize} >= maxsize {offer.maxsize}"
            )
