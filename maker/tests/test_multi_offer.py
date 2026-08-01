"""
Tests for multi-offer functionality.

Tests the maker's ability to create and handle multiple offers simultaneously,
including both relative and absolute fee offers with different offer IDs.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.models import NetworkType, Offer, OfferType

from maker.bot import MakerBot
from maker.coinjoin import CoinJoinState
from maker.config import MakerConfig, OfferConfig
from maker.maker_session import MakerSession
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
        assert cfg.cjfee_factor == 0.0
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
    @staticmethod
    def _session(
        taker_nick: str,
        commitment: str,
        *,
        state: CoinJoinState = CoinJoinState.PUBKEY_SENT,
        timeout: float = 60.0,
    ) -> MakerSession:
        inner = MagicMock()
        inner.taker_nick = taker_nick
        inner.session_timeout_sec = timeout
        inner.state = state
        inner.commitment = bytes.fromhex(commitment)
        inner.commitment_authenticated = False
        inner.our_utxos = {}
        inner.input_lock_owner = "test-owner"
        inner.input_lock_ttl_sec = 3600.0
        return MakerSession(inner)

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
        assert call_kwargs["min_confirmations"] == maker_bot.config.min_confirmations
        assert call_kwargs["input_lock_ttl_sec"] == maker_bot.config.pending_tx_timeout_min * 60

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
    async def test_fill_reserves_commitment_across_local_sessions(self, maker_bot):
        """Only one local in-flight session may use a commitment."""

        async def mock_handle_fill(amount, commitment, taker_pk):
            return True, {"nacl_pubkey": "abc123", "features": []}

        with (
            patch("maker.protocol_handlers.CoinJoinSession") as mock_session_class,
            patch("maker.protocol_handlers.check_commitment", return_value=True),
            patch.object(maker_bot, "_send_response", new=AsyncMock()),
        ):
            mock_session = MagicMock()
            mock_session.handle_fill = mock_handle_fill
            mock_session.validate_channel = MagicMock(return_value=True)
            mock_session_class.return_value = mock_session

            await maker_bot._handle_fill("J5FirstTaker", f"fill 0 500000 taker_pk_hex P{'ab' * 32}")
            await maker_bot._handle_fill(
                "J5SecondTaker", f"fill 0 500000 taker_pk_hex P{'AB' * 32}"
            )

        mock_session_class.assert_called_once()
        assert "J5FirstTaker" in maker_bot.active_sessions
        assert "J5SecondTaker" not in maker_bot.active_sessions
        assert "ab" * 32 in maker_bot._reserved_commitments

    @pytest.mark.asyncio
    async def test_fill_failure_releases_commitment_reservation(self, maker_bot):
        """A failed fill releases its commitment reservation."""

        async def mock_handle_fill(amount, commitment, taker_pk):
            return False, {"error": "invalid taker pubkey"}

        with (
            patch("maker.protocol_handlers.CoinJoinSession") as mock_session_class,
            patch("maker.protocol_handlers.check_commitment", return_value=True),
        ):
            mock_session = MagicMock()
            mock_session.handle_fill = mock_handle_fill
            mock_session.validate_channel = MagicMock(return_value=True)
            mock_session_class.return_value = mock_session

            await maker_bot._handle_fill(
                "J5FailedTaker", f"fill 0 500000 taker_pk_hex P{'ac' * 32}"
            )

        assert "ac" * 32 not in maker_bot._reserved_commitments

    @pytest.mark.asyncio
    @pytest.mark.parametrize("session_replaced", [False, True])
    async def test_fill_exception_removes_only_installed_candidate(
        self, maker_bot, session_replaced
    ):
        """A post-install exception releases the candidate reservation only."""
        taker_nick = "J5ExceptionTaker"
        commitment = "a0" * 32
        replacement = MagicMock()

        def close_spawned(coro):
            coro.close()

        with (
            patch("maker.protocol_handlers.CoinJoinSession") as mock_session_class,
            patch("maker.protocol_handlers.check_commitment", return_value=True),
            patch("maker.protocol_handlers.spawn_task", side_effect=close_spawned),
        ):
            mock_session = MagicMock()
            mock_session.commitment = bytes.fromhex(commitment)
            mock_session.state = CoinJoinState.PUBKEY_SENT
            mock_session.handle_fill = AsyncMock(
                return_value=(True, {"nacl_pubkey": "abc123", "features": []})
            )
            mock_session.validate_channel = MagicMock(return_value=True)
            mock_session_class.return_value = mock_session

            async def fail_send(*args, **kwargs):
                installed_session = maker_bot.active_sessions[taker_nick]
                assert installed_session.inner is mock_session
                if session_replaced:
                    maker_bot.active_sessions[taker_nick] = replacement
                raise RuntimeError("send failed")

            with patch.object(maker_bot, "_send_response", new=fail_send):
                await maker_bot._handle_fill(
                    taker_nick, f"fill 0 500000 taker_pk_hex P{commitment}"
                )

        assert commitment not in maker_bot._reserved_commitments
        if session_replaced:
            assert maker_bot.active_sessions[taker_nick] is replacement
        else:
            assert taker_nick not in maker_bot.active_sessions

    @pytest.mark.asyncio
    async def test_fill_exception_before_reservation_keeps_other_session_reservation(
        self, maker_bot
    ):
        """Cleanup only releases a reservation acquired by this fill call."""
        commitment = "a1" * 32
        maker_bot._reserved_commitments.add(commitment)

        with patch(
            "maker.protocol_handlers.check_commitment",
            side_effect=RuntimeError("blacklist unavailable"),
        ):
            await maker_bot._handle_fill(
                "J5OtherTaker", f"fill 0 500000 taker_pk_hex P{commitment}"
            )

        assert commitment in maker_bot._reserved_commitments

    @pytest.mark.asyncio
    async def test_fill_replaces_same_taker_without_orphaning_reservation(self, maker_bot):
        """A pre-auth commitment retry replaces and releases the first fill."""

        def make_mock_session(*args, **kwargs):
            mock_session = MagicMock()

            async def mock_handle_fill(amount, commitment, taker_pk):
                mock_session.commitment = bytes.fromhex(commitment)
                mock_session.state = CoinJoinState.PUBKEY_SENT
                return True, {"nacl_pubkey": "abc123", "features": []}

            mock_session.handle_fill = mock_handle_fill
            mock_session.validate_channel = MagicMock(return_value=True)
            return mock_session

        with (
            patch("maker.protocol_handlers.CoinJoinSession") as mock_session_class,
            patch("maker.protocol_handlers.check_commitment", return_value=True),
            patch.object(maker_bot, "_send_response", new=AsyncMock()),
        ):
            mock_session_class.side_effect = make_mock_session

            await maker_bot._handle_fill(
                "J5RepeatedTaker", f"fill 0 500000 taker_pk_hex P{'ad' * 32}"
            )
            first_session = maker_bot.active_sessions["J5RepeatedTaker"]
            first_session.release_input_locks = MagicMock()
            await maker_bot._handle_fill(
                "J5RepeatedTaker", f"fill 0 500000 taker_pk_hex P{'ae' * 32}"
            )

        assert mock_session_class.call_count == 2
        assert maker_bot.active_sessions["J5RepeatedTaker"] is not first_session
        assert "ad" * 32 not in maker_bot._reserved_commitments
        assert "ae" * 32 in maker_bot._reserved_commitments
        first_session.release_input_locks.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_fill_does_not_replace_session_during_auth(self, maker_bot):
        """A rejected replacement preserves the authenticating session."""

        def make_mock_session(*args, **kwargs):
            mock_session = MagicMock()

            async def mock_handle_fill(amount, commitment, taker_pk):
                mock_session.commitment = bytes.fromhex(commitment)
                mock_session.state = CoinJoinState.PUBKEY_SENT
                return True, {"nacl_pubkey": "abc123", "features": []}

            mock_session.handle_fill = mock_handle_fill
            mock_session.validate_channel = MagicMock(return_value=True)
            return mock_session

        with (
            patch("maker.protocol_handlers.CoinJoinSession") as mock_session_class,
            patch("maker.protocol_handlers.check_commitment", return_value=True),
            patch.object(maker_bot, "_send_response", new=AsyncMock()),
        ):
            mock_session_class.side_effect = make_mock_session

            await maker_bot._handle_fill(
                "J5AuthenticatingTaker", f"fill 0 500000 taker_pk_hex P{'b1' * 32}"
            )
            first_session = maker_bot.active_sessions["J5AuthenticatingTaker"]
            first_session.release_input_locks = MagicMock()
            await first_session.lock.acquire()
            await maker_bot._handle_fill(
                "J5AuthenticatingTaker", f"fill 0 500000 taker_pk_hex P{'b2' * 32}"
            )

        assert maker_bot.active_sessions["J5AuthenticatingTaker"] is first_session
        assert "b1" * 32 in maker_bot._reserved_commitments
        assert "b2" * 32 not in maker_bot._reserved_commitments
        first_session.release_input_locks.assert_not_called()
        first_session.lock.release()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "callback_name"),
        [("_handle_auth", "on_auth"), ("_handle_tx", "on_tx")],
    )
    async def test_stale_waiter_ignores_replacement(self, maker_bot, handler_name, callback_name):
        """A handler queued on an obsolete session must stop after acquiring its lock."""
        taker_nick = "J5ReplacedTaker"
        old_session = self._session(taker_nick, "a2" * 32)
        old_session.on_auth = AsyncMock()
        old_session.on_tx = AsyncMock()
        replacement = MagicMock()
        replacement.on_auth = AsyncMock()
        replacement.on_tx = AsyncMock()
        maker_bot.active_sessions[taker_nick] = old_session
        commitments = set(maker_bot._reserved_commitments)

        await old_session.lock.acquire()
        task = asyncio.create_task(
            getattr(maker_bot, handler_name)(taker_nick, f"{callback_name[3:]} payload")
        )
        await asyncio.sleep(0)
        assert not task.done()

        assert maker_bot.active_sessions.pop(taker_nick) is old_session
        maker_bot.active_sessions[taker_nick] = replacement
        old_session.lock.release()
        await task

        assert maker_bot.active_sessions[taker_nick] is replacement
        getattr(old_session, callback_name).assert_not_awaited()
        getattr(replacement, callback_name).assert_not_awaited()
        old_session.inner.wallet.release_coinjoin_inputs.assert_not_called()
        replacement.release_input_locks.assert_not_called()
        assert maker_bot._reserved_commitments == commitments

    @pytest.mark.asyncio
    async def test_timeout_cleanup_releases_commitment_reservation(self, maker_bot):
        commitment = "af" * 32
        session = MagicMock()
        session.is_timed_out.return_value = True
        session.lock = asyncio.Lock()
        session.commitment = bytes.fromhex(commitment)
        session.state = CoinJoinState.PUBKEY_SENT
        maker_bot.active_sessions["J5TimedOutTaker"] = session
        maker_bot._reserved_commitments.add(commitment)

        await maker_bot._cleanup_timed_out_sessions()

        assert "J5TimedOutTaker" not in maker_bot.active_sessions
        assert commitment not in maker_bot._reserved_commitments
        session.release_input_locks.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_auth_received_timeout_releases_commitment_for_replacement(self, maker_bot):
        commitment = "b3" * 32
        session = MagicMock()
        session.is_timed_out.return_value = True
        session.lock = asyncio.Lock()
        session.commitment = bytes.fromhex(commitment)
        session.state = CoinJoinState.AUTH_RECEIVED
        session.ioauth_boundary_crossed = False
        maker_bot.active_sessions["J5AuthStageTaker"] = session
        maker_bot._reserved_commitments.add(commitment)
        maker_bot._broadcast_commitment = AsyncMock(return_value=True)

        await maker_bot._cleanup_timed_out_sessions()

        assert commitment not in maker_bot._reserved_commitments
        maker_bot._broadcast_commitment.assert_not_awaited()
        session.release_input_locks.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_ioauth_state_ordering_across_successful_fanout(self, maker_bot):
        commitment = "bc" * 32
        session = self._session("J5FanoutTaker", commitment, state=CoinJoinState.AUTH_RECEIVED)
        session.inner.crypto.encrypt.return_value = "encrypted-ioauth"
        observed_states: list[CoinJoinState] = []

        async def record_state(*args):
            observed_states.append(session.state)

        first = MagicMock()
        first.send_private_message = AsyncMock(side_effect=record_state)
        second = MagicMock()
        second.send_private_message = AsyncMock(side_effect=record_state)
        maker_bot.directory_clients = {"first": first, "second": second}
        maker_bot.active_sessions[session.taker_nick] = session

        sent = await session.send_response(
            maker_bot,
            "ioauth",
            {
                "utxo_list": "ab:0",
                "auth_pub": "auth-pub",
                "cj_addr": "coinjoin-address",
                "change_addr": "change-address",
                "btc_sig": "signature",
            },
        )

        assert sent is True
        assert observed_states == [
            CoinJoinState.IOAUTH_SEND_STARTED,
            CoinJoinState.IOAUTH_SEND_STARTED,
        ]
        assert session.state == CoinJoinState.IOAUTH_SENT
        states = list(CoinJoinState)
        assert states.index(CoinJoinState.AUTH_RECEIVED) < states.index(
            CoinJoinState.IOAUTH_SEND_STARTED
        )
        assert states.index(CoinJoinState.IOAUTH_SEND_STARTED) < states.index(
            CoinJoinState.IOAUTH_SENT
        )

    @pytest.mark.asyncio
    async def test_partial_ioauth_fanout_timeout_persists_and_releases_pre_sign_locks(
        self, maker_bot
    ):
        commitment = "bd" * 32
        outpoint = ("ab" * 32, 0)
        session = self._session(
            "J5PartialFanoutTaker", commitment, state=CoinJoinState.AUTH_RECEIVED
        )
        session.inner.crypto.encrypt.return_value = "encrypted-ioauth"
        session.inner.our_utxos = {outpoint: MagicMock()}
        session.inner.signing_boundary_crossed = False
        second_started = asyncio.Event()
        second_cancelled = asyncio.Event()

        first = MagicMock()
        first.send_private_message = AsyncMock()
        second = MagicMock()

        async def block_second(*args):
            second_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                second_cancelled.set()
                raise

        second.send_private_message = AsyncMock(side_effect=block_second)
        maker_bot.directory_clients = {"first": first, "second": second}
        maker_bot.active_sessions[session.taker_nick] = session
        maker_bot._reserved_commitments.add(commitment)
        maker_bot._broadcast_commitment = AsyncMock(return_value=True)

        async def send_ioauth() -> None:
            await session.send_response(
                maker_bot,
                "ioauth",
                {
                    "utxo_list": "ab:0",
                    "auth_pub": "auth-pub",
                    "cj_addr": "coinjoin-address",
                    "change_addr": "change-address",
                    "btc_sig": "signature",
                },
            )

        dispatch = asyncio.create_task(
            maker_bot._dispatch_session_handler(
                session, send_ioauth, name="test-partial-ioauth-fanout"
            )
        )
        await second_started.wait()

        first.send_private_message.assert_awaited_once()
        assert session.state == CoinJoinState.IOAUTH_SEND_STARTED
        session.deadline = asyncio.get_running_loop().time() - 1
        session.inner.deadline = session.deadline
        await maker_bot._cleanup_timed_out_sessions()
        await dispatch

        assert second_cancelled.is_set()
        assert session.state == CoinJoinState.IOAUTH_SEND_STARTED
        assert session.taker_nick not in maker_bot.active_sessions
        assert commitment not in maker_bot._reserved_commitments
        maker_bot._broadcast_commitment.assert_awaited_once_with(commitment)
        session.inner.wallet.release_coinjoin_inputs.assert_called_once_with(
            {outpoint}, owner=session.inner.input_lock_owner
        )
        session.inner.wallet.renew_coinjoin_inputs.assert_not_called()

    @pytest.mark.asyncio
    async def test_ioauth_preparation_failure_does_not_persist_commitment(self, maker_bot):
        commitment = "be" * 32
        session = self._session(
            "J5IoauthPreparationFailure", commitment, state=CoinJoinState.AUTH_RECEIVED
        )
        session.inner.crypto.encrypt.side_effect = ValueError("encryption failed")
        client = MagicMock()
        client.send_private_message = AsyncMock()
        maker_bot.directory_clients = {"only": client}
        maker_bot.active_sessions[session.taker_nick] = session
        maker_bot._reserved_commitments.add(commitment)
        maker_bot._broadcast_commitment = AsyncMock(return_value=True)

        sent = await session.send_response(
            maker_bot,
            "ioauth",
            {
                "utxo_list": "ab:0",
                "auth_pub": "auth-pub",
                "cj_addr": "coinjoin-address",
                "change_addr": "change-address",
                "btc_sig": "signature",
            },
        )

        assert sent is False
        assert session.state == CoinJoinState.AUTH_RECEIVED
        client.send_private_message.assert_not_awaited()
        session.deadline = asyncio.get_running_loop().time() - 1
        session.inner.deadline = session.deadline
        await maker_bot._cleanup_timed_out_sessions()

        maker_bot._broadcast_commitment.assert_not_awaited()
        assert commitment not in maker_bot._reserved_commitments
        session.inner.wallet.release_coinjoin_inputs.assert_called_once_with(
            set(), owner=session.inner.input_lock_owner
        )

    @pytest.mark.asyncio
    async def test_timeout_cleanup_keeps_ioauth_unpersisted_reservation(self, maker_bot):
        commitment = "b3" * 32
        session = MagicMock()
        session.is_timed_out.return_value = True
        session.lock = asyncio.Lock()
        session.commitment = bytes.fromhex(commitment)
        session.state = CoinJoinState.IOAUTH_SENT
        session.ioauth_boundary_crossed = True
        maker_bot.active_sessions["J5UnpersistedTaker"] = session
        maker_bot._reserved_commitments.add(commitment)
        maker_bot._broadcast_commitment = AsyncMock(return_value=False)

        await maker_bot._cleanup_timed_out_sessions()

        assert "J5UnpersistedTaker" not in maker_bot.active_sessions
        assert commitment in maker_bot._reserved_commitments
        session.release_input_locks.assert_called_once_with()
        maker_bot._broadcast_commitment.assert_awaited_once_with(commitment)

    @pytest.mark.asyncio
    async def test_timeout_cleanup_retains_sig_sent_input_locks(self, maker_bot):
        commitment = "b4" * 32
        session = MagicMock()
        session.is_timed_out.return_value = True
        session.lock = asyncio.Lock()
        session.commitment = bytes.fromhex(commitment)
        session.state = CoinJoinState.SIG_SENT
        maker_bot.active_sessions["J5SignedTaker"] = session
        maker_bot._reserved_commitments.add(commitment)
        maker_bot._broadcast_commitment = AsyncMock(return_value=True)

        await maker_bot._cleanup_timed_out_sessions()

        assert "J5SignedTaker" not in maker_bot.active_sessions
        assert commitment not in maker_bot._reserved_commitments
        session.release_input_locks.assert_not_called()
        session.retain_input_locks.assert_called_once_with()
        maker_bot._broadcast_commitment.assert_awaited_once_with(commitment)

    @pytest.mark.asyncio
    async def test_timeout_cleanup_detaches_locked_session(self, maker_bot):
        commitment = "b0" * 32
        session = MagicMock()
        session.is_timed_out.return_value = True
        session.lock = asyncio.Lock()
        await session.lock.acquire()
        session.commitment = bytes.fromhex(commitment)
        session.state = CoinJoinState.PUBKEY_SENT
        maker_bot.active_sessions["J5BusyTaker"] = session
        maker_bot._reserved_commitments.add(commitment)

        await maker_bot._cleanup_timed_out_sessions()

        assert "J5BusyTaker" not in maker_bot.active_sessions
        assert commitment not in maker_bot._reserved_commitments
        session.release_input_locks.assert_called_once_with()
        session.lock.release()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_name", "callback_name"),
        [("_handle_auth", "on_auth"), ("_handle_tx", "on_tx")],
    )
    async def test_handler_deadline_cancels_and_unwinds_before_cleanup(
        self, maker_bot, handler_name, callback_name
    ):
        taker_nick = "J5BlockedHandler"
        commitment = "b5" * 32
        session = self._session(taker_nick, commitment, timeout=0.01)
        cancelled_while_locked = False
        started = asyncio.Event()

        async def block(*args):
            nonlocal cancelled_while_locked
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_while_locked = session.lock.locked()
                raise

        setattr(session, callback_name, block)
        maker_bot.active_sessions[taker_nick] = session
        maker_bot._reserved_commitments.add(commitment)

        dispatch = asyncio.create_task(
            getattr(maker_bot, handler_name)(taker_nick, "message payload")
        )
        await started.wait()
        await asyncio.sleep(0.02)
        await maker_bot._cleanup_timed_out_sessions()
        await dispatch

        assert cancelled_while_locked is True
        assert session.lock.locked() is False
        assert taker_nick not in maker_bot.active_sessions
        assert commitment not in maker_bot._reserved_commitments
        session.inner.wallet.release_coinjoin_inputs.assert_called_once_with(
            set(), owner="test-owner"
        )

    @pytest.mark.asyncio
    async def test_lock_wait_timeout_detaches_without_holder_unwind(self, maker_bot):
        taker_nick = "J5BlockedLock"
        commitment = "b6" * 32
        session = self._session(taker_nick, commitment, timeout=0.0)
        await session.lock.acquire()
        session.on_auth = AsyncMock()
        maker_bot.active_sessions[taker_nick] = session
        maker_bot._reserved_commitments.add(commitment)

        dispatch = asyncio.create_task(maker_bot._handle_auth(taker_nick, "auth payload"))
        await asyncio.sleep(0)
        await maker_bot._cleanup_timed_out_sessions()
        await dispatch

        assert taker_nick not in maker_bot.active_sessions
        assert commitment not in maker_bot._reserved_commitments
        session.on_auth.assert_not_awaited()
        session.lock.release()

    @pytest.mark.asyncio
    async def test_reaper_detaches_handler_that_suppresses_cancellation(self, maker_bot):
        taker_nick = "J5UncooperativeTaker"
        commitment = "b9" * 32
        session = self._session(taker_nick, commitment, timeout=60.0)
        session.inner.crypto.is_encrypted = True
        session.inner.crypto.decrypt.return_value = (
            f"{'ab' * 32}:0|02{'bc' * 32}|02{'cd' * 32}|11|22"
        )
        handler_started = asyncio.Event()
        cancelled = asyncio.Event()
        resume = asyncio.Event()

        async def suppress_cancellation(*args, **kwargs):
            handler_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await resume.wait()
            return True, {
                "utxo_list": "ab:0",
                "auth_pub": "02" + "de" * 32,
                "cj_addr": "bcrt1qcoinjoin",
                "change_addr": "bcrt1qchange",
                "btc_sig": "signature",
            }

        session.inner.handle_auth = AsyncMock(side_effect=suppress_cancellation)
        session.send_response = AsyncMock()
        maker_bot.active_sessions[taker_nick] = session
        maker_bot._reserved_commitments.add(commitment)

        dispatch = asyncio.create_task(
            maker_bot._handle_auth(taker_nick, "auth ciphertext", source="dir:test")
        )
        await handler_started.wait()
        loop = asyncio.get_running_loop()
        session.deadline = loop.time() - 1.0
        session.inner.deadline = session.deadline
        started = loop.time()
        await maker_bot._cleanup_timed_out_sessions()
        elapsed = loop.time() - started
        await dispatch

        assert cancelled.is_set()
        assert elapsed < 0.8
        assert session.detached is True
        assert taker_nick not in maker_bot.active_sessions
        detached_task = session.handler_task
        assert detached_task is not None
        assert detached_task in maker_bot._detached_handler_tasks

        resume.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        session.send_response.assert_not_awaited()
        assert maker_bot._detached_handler_tasks == set()

    @pytest.mark.asyncio
    async def test_shutdown_bounded_with_permanently_cancellation_suppressing_handler(
        self, maker_bot
    ):
        taker_nick = "J5PermanentHandler"
        commitment = "ba" * 32
        session = self._session(taker_nick, commitment, timeout=60.0)
        started = asyncio.Event()
        terminate = asyncio.Event()
        cancellation_count = 0

        async def suppress_every_cancellation(*args):
            nonlocal cancellation_count
            started.set()
            while not terminate.is_set():
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancellation_count += 1

        session.on_auth = suppress_every_cancellation
        maker_bot.active_sessions[taker_nick] = session
        maker_bot._reserved_commitments.add(commitment)
        dispatch = asyncio.create_task(maker_bot._handle_auth(taker_nick, "auth payload"))
        await started.wait()
        session.deadline = asyncio.get_running_loop().time() - 1
        await maker_bot._cleanup_timed_out_sessions()
        await dispatch
        detached_task = session.handler_task
        assert detached_task is not None

        started_at = asyncio.get_running_loop().time()
        await maker_bot.stop()

        assert asyncio.get_running_loop().time() - started_at < 1.5
        assert cancellation_count >= 2
        assert detached_task in maker_bot._detached_handler_tasks

        terminate.set()
        detached_task.cancel()
        await asyncio.wait_for(detached_task, timeout=1)
        await asyncio.sleep(0)
        assert maker_bot._detached_handler_tasks == set()

    @pytest.mark.asyncio
    async def test_handler_admission_cap_prevents_unbounded_task_creation(self, maker_bot):
        from maker.protocol_handlers import MAX_SESSION_HANDLER_TASKS

        session = self._session("J5CappedTaker", "bb" * 32)
        session.on_auth = AsyncMock()
        maker_bot.active_sessions[session.taker_nick] = session
        maker_bot._session_handler_task_count = MAX_SESSION_HANDLER_TASKS

        await maker_bot._handle_auth(session.taker_nick, "auth payload")

        session.on_auth.assert_not_awaited()
        assert maker_bot._session_handler_task_count == MAX_SESSION_HANDLER_TASKS

    @pytest.mark.asyncio
    async def test_timeout_expiration_ignores_replacement_identity(self, maker_bot):
        taker_nick = "J5TimeoutReplacement"
        stale = MagicMock()
        stale.lock = asyncio.Lock()
        stale.commitment = bytes.fromhex("b7" * 32)
        stale.state = CoinJoinState.PUBKEY_SENT
        replacement = MagicMock()
        maker_bot.active_sessions[taker_nick] = replacement

        expired = await maker_bot._expire_timed_out_session(taker_nick, stale)

        assert expired is False
        assert maker_bot.active_sessions[taker_nick] is replacement
        stale.release_input_locks.assert_not_called()
        replacement.release_input_locks.assert_not_called()

    @pytest.mark.asyncio
    async def test_directory_dispatch_continues_after_session_timeout(self, maker_bot):
        taker_nick = "J5DirectoryTimeout"
        commitment = "b8" * 32
        session = self._session(taker_nick, commitment, timeout=0.01)
        started = asyncio.Event()

        async def block_auth(*args):
            started.set()
            await asyncio.Event().wait()

        session.on_auth = AsyncMock(side_effect=block_auth)
        maker_bot.active_sessions[taker_nick] = session
        maker_bot._reserved_commitments.add(commitment)
        maker_bot._handle_push = AsyncMock()

        dispatch = asyncio.create_task(
            maker_bot._handle_privmsg(
                f"{taker_nick}!{maker_bot.nick}!!auth payload", source="dir:test"
            )
        )
        await started.wait()
        await asyncio.sleep(0.02)
        await maker_bot._cleanup_timed_out_sessions()
        await dispatch
        await maker_bot._handle_privmsg(
            f"{taker_nick}!{maker_bot.nick}!!push transaction", source="dir:test"
        )

        assert taker_nick not in maker_bot.active_sessions
        maker_bot._handle_push.assert_awaited_once_with(
            taker_nick, "push transaction", source="dir:test"
        )

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
    async def test_fill_rejects_commitment_without_scheme_prefix(self, maker_bot):
        await maker_bot._handle_fill(
            "J5BareCommitment",
            f"fill 0 500000 taker_pk_hex {'cc' * 32}",
        )

        assert "J5BareCommitment" not in maker_bot.active_sessions

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
