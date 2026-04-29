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
        # Defaults aligned with upstream JoinMarket reference (issue #468).
        assert cfg.min_size == 100_000
        assert cfg.cj_fee_relative == "0.00002"
        assert cfg.cj_fee_absolute == 500
        assert cfg.tx_fee_contribution == 0

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
        """Config with single offer (legacy mode)."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_type=OfferType.SW0_RELATIVE,
            min_size=100_000,
            cj_fee_relative="0.001",
        )

    @pytest.fixture
    def config_dual_offers(self):
        """Config with dual offers."""
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
