"""
Tests for maker bot offer announcements with fidelity bond proofs.
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import MagicMock

import pytest
from jmcore.models import NetworkType, Offer, OfferType
from jmcore.network import TCPConnection

from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.fidelity import FidelityBondInfo


class TestOfferAnnouncement:
    """Tests for _format_offer_announcement method."""

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
        return MagicMock()

    @pytest.fixture
    def config(self):
        """Create a test maker config."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        """Create a MakerBot instance for testing."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )
        return bot

    @pytest.fixture
    def sample_offer(self, maker_bot):
        """Create a sample offer for testing."""
        return Offer(
            counterparty=maker_bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=1000,
            cjfee="0.0003",
            fidelity_bond_value=0,
        )

    def test_format_offer_without_bond(self, maker_bot, sample_offer):
        """Test offer formatting without fidelity bond."""
        msg = maker_bot._format_offer_announcement(sample_offer)

        # Should not contain !tbond
        assert "!tbond" not in msg

        # Check format: <ordertype> <oid> <minsize> <maxsize> <txfee> <cjfee>
        parts = msg.split()
        assert parts[0] == "sw0reloffer"
        assert parts[1] == "0"  # oid
        assert parts[2] == "100000"  # minsize
        assert parts[3] == "10000000"  # maxsize
        assert parts[4] == "1000"  # txfee
        assert parts[5] == "0.0003"  # cjfee

    def test_format_offer_with_bond(self, maker_bot, sample_offer, test_private_key, test_pubkey):
        """Test offer formatting with fidelity bond attached for PRIVMSG.

        Bonds should ONLY be included when include_bond=True (for PRIVMSG responses).
        Public broadcasts should never include bonds.
        """
        # Set up fidelity bond
        maker_bot.fidelity_bond = FidelityBondInfo(
            txid="ab" * 32,
            vout=0,
            value=100_000_000,
            locktime=800000,
            confirmation_time=1000,
            bond_value=50_000,
            pubkey=test_pubkey,
            private_key=test_private_key,
        )

        # Test 1: Public announcement should NOT include bond (default)
        msg_public = maker_bot._format_offer_announcement(sample_offer)
        assert "!tbond" not in msg_public, "Public announcements should not include bond"

        # Test 2: PRIVMSG should include bond when explicitly requested
        msg_privmsg = maker_bot._format_offer_announcement(sample_offer, include_bond=True)
        assert "!tbond " in msg_privmsg, "PRIVMSG should include bond when include_bond=True"

        # Parse the PRIVMSG message
        parts = msg_privmsg.split("!tbond ")
        assert len(parts) == 2

        # Check offer part
        offer_parts = parts[0].split()
        assert offer_parts[0] == "sw0reloffer"

        # Check bond proof is valid base64 and 252 bytes when decoded
        bond_proof = parts[1].strip()
        decoded = base64.b64decode(bond_proof)
        assert len(decoded) == 252

    def test_format_absolute_offer_without_bond(self, maker_bot):
        """Test absolute offer formatting."""
        offer = Offer(
            counterparty=maker_bot.nick,
            oid=1,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=50_000,
            maxsize=5_000_000,
            txfee=500,
            cjfee="1000",  # Absolute fee in sats
            fidelity_bond_value=0,
        )

        msg = maker_bot._format_offer_announcement(offer)

        parts = msg.split()
        assert parts[0] == "sw0absoffer"
        assert parts[1] == "1"  # oid
        assert parts[5] == "1000"  # cjfee (absolute)

    def test_bond_proof_without_private_key_skipped(self, maker_bot, sample_offer, test_pubkey):
        """Test that bond proof is skipped if private key is missing."""
        # Set up fidelity bond without private key
        maker_bot.fidelity_bond = FidelityBondInfo(
            txid="cd" * 32,
            vout=0,
            value=100_000_000,
            locktime=800000,
            confirmation_time=1000,
            bond_value=50_000,
            pubkey=test_pubkey,
            private_key=None,  # Missing!
        )

        msg = maker_bot._format_offer_announcement(sample_offer)

        # Should not contain !tbond when signing fails
        assert "!tbond" not in msg

    def test_bond_proof_without_pubkey_skipped(self, maker_bot, sample_offer, test_private_key):
        """Test that bond proof is skipped if pubkey is missing."""
        # Set up fidelity bond without pubkey
        maker_bot.fidelity_bond = FidelityBondInfo(
            txid="ef" * 32,
            vout=0,
            value=100_000_000,
            locktime=800000,
            confirmation_time=1000,
            bond_value=50_000,
            pubkey=None,  # Missing!
            private_key=test_private_key,
        )

        msg = maker_bot._format_offer_announcement(sample_offer)

        # Should not contain !tbond when signing fails
        assert "!tbond" not in msg


class TestBotInitialization:
    """Tests for MakerBot initialization."""

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

    def test_bot_initializes_without_bond(self, mock_wallet, mock_backend, config):
        """Test that bot initializes with no fidelity bond."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

        assert bot.fidelity_bond is None

    def test_bot_respects_no_fidelity_bond_config(self, mock_wallet, mock_backend):
        """Test that no_fidelity_bond=True is stored on the config.

        The bot will skip bond selection when this flag is set.
        """
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            no_fidelity_bond=True,
        )

        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

        assert bot.config.no_fidelity_bond is True
        # Bot always starts with no bond; the start() coroutine sets it during initialization
        assert bot.fidelity_bond is None

    def test_bot_has_nick(self, mock_wallet, mock_backend, config):
        """Test that bot generates a nick."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

        assert bot.nick is not None
        assert len(bot.nick) > 0

    def test_bot_initializes_without_hidden_service(self, mock_wallet, mock_backend, config):
        """Test that bot initializes without hidden service listener by default."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

        assert bot.hidden_service_listener is None
        assert bot.direct_connections == {}

    def test_bot_config_with_onion_host(self, mock_wallet, mock_backend):
        """Test that bot can be configured with onion host."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            onion_host="test1234567890abcdef.onion",
            onion_serving_host="127.0.0.1",
            onion_serving_port=5222,
            socks_host="127.0.0.1",
            socks_port=9050,
        )

        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

        # Hidden service listener is created during start(), not init
        assert bot.hidden_service_listener is None
        assert config.onion_host == "test1234567890abcdef.onion"
        assert config.onion_serving_port == 5222


class TestHiddenServiceListener:
    """Tests for hidden service listener functionality."""

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
    def config_with_onion(self):
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            onion_host="test1234567890abcdef.onion",
            onion_serving_host="127.0.0.1",
            onion_serving_port=0,  # Auto-assign port for tests
        )

    def test_direct_connection_tracking(self, mock_wallet, mock_backend, config_with_onion):
        """Test that direct connections are tracked by nick."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config_with_onion,
        )

        # Simulate adding a direct connection
        mock_conn = MagicMock(spec=TCPConnection)
        bot.direct_connections["J5test123"] = mock_conn

        assert "J5test123" in bot.direct_connections
        assert bot.direct_connections["J5test123"] == mock_conn

    @pytest.mark.asyncio
    async def test_on_direct_connection_invalid_json(
        self, mock_wallet, mock_backend, config_with_onion
    ):
        """Test that invalid JSON messages are handled gracefully."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config_with_onion,
        )
        bot.running = True

        # Create a mock connection that returns invalid JSON then disconnects
        mock_conn = MagicMock(spec=TCPConnection)
        mock_conn.is_connected.side_effect = [True, False]  # Connected once, then disconnect

        async def mock_receive() -> bytes:
            return b"not valid json"

        mock_conn.receive = mock_receive

        async def mock_close() -> None:
            pass

        mock_conn.close = mock_close

        # This should handle the invalid JSON gracefully
        await bot._on_direct_connection(mock_conn, "127.0.0.1:12345")

    @pytest.mark.asyncio
    async def test_on_direct_connection_fill_command(
        self, mock_wallet, mock_backend, config_with_onion
    ):
        """Test that direct connection fill command is routed correctly."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config_with_onion,
        )
        bot.running = True

        # Track if _handle_fill was called and verify connection tracking
        fill_called = False
        connection_was_tracked = False

        async def mock_handle_fill(taker_nick: str, msg: str, source: str = "unknown") -> None:
            nonlocal fill_called, connection_was_tracked
            fill_called = True
            # At this point, the connection should be tracked
            connection_was_tracked = taker_nick in bot.direct_connections
            assert taker_nick == "J5taker123"
            assert "fill" in msg
            assert source == "direct"  # Should be called with source="direct"

        bot._handle_fill = mock_handle_fill

        # Create a mock connection that sends a fill command then disconnects
        fill_msg = json.dumps(
            {"nick": "J5taker123", "cmd": "fill", "data": "0 1000000 abc123 Pcommitment"}
        )

        async def mock_receive() -> bytes:
            return fill_msg.encode()

        async def mock_close() -> None:
            pass

        mock_conn = MagicMock(spec=TCPConnection)
        mock_conn.is_connected.side_effect = [True, False]
        mock_conn.receive = mock_receive
        mock_conn.close = mock_close

        await bot._on_direct_connection(mock_conn, "127.0.0.1:12345")

        assert fill_called, "_handle_fill should have been called"
        # Connection is tracked during processing but cleaned up on disconnect
        assert connection_was_tracked, "Connection should be tracked during message handling"
        # After cleanup, connection should be removed
        assert "J5taker123" not in bot.direct_connections, "Connection should be cleaned up"


class TestHandlePush:
    """Tests for _handle_push method."""

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
        from unittest.mock import AsyncMock

        backend = MagicMock()
        backend.broadcast_transaction = AsyncMock(return_value="txid123abc")
        backend.get_block_height = AsyncMock(return_value=930000)
        return backend

    @pytest.fixture
    def config(self):
        """Create a test maker config."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        """Create a MakerBot instance for testing."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )
        return bot

    @pytest.mark.asyncio
    async def test_handle_push_broadcasts_transaction(self, maker_bot):
        """Test that !push broadcasts the transaction."""
        import base64

        # Create a dummy transaction (minimal valid format)
        tx_bytes = bytes.fromhex("0100000000010000000000")
        tx_b64 = base64.b64encode(tx_bytes).decode("ascii")

        await maker_bot._handle_push("J5taker123", f"push {tx_b64}")

        # Verify broadcast was called with the decoded transaction
        maker_bot.backend.broadcast_transaction.assert_called_once_with(tx_bytes.hex())

    @pytest.mark.asyncio
    async def test_handle_push_invalid_format(self, maker_bot):
        """Test that invalid !push format is handled gracefully."""
        # Missing transaction data
        await maker_bot._handle_push("J5taker123", "push")

        # Should not call broadcast
        maker_bot.backend.broadcast_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_push_invalid_base64(self, maker_bot):
        """Test that invalid base64 is handled gracefully."""
        await maker_bot._handle_push("J5taker123", "push not_valid_base64!!!")

        # Should not call broadcast (decoding fails)
        maker_bot.backend.broadcast_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_push_broadcast_failure_logged(self, maker_bot, caplog):
        """Test that broadcast failure is logged but doesn't raise."""
        import base64
        from unittest.mock import AsyncMock

        # Make broadcast fail
        maker_bot.backend.broadcast_transaction = AsyncMock(side_effect=Exception("Network error"))

        tx_bytes = bytes.fromhex("0100000000010000000000")
        tx_b64 = base64.b64encode(tx_bytes).decode("ascii")

        # Should not raise
        await maker_bot._handle_push("J5taker123", f"push {tx_b64}")

        # Broadcast was attempted
        maker_bot.backend.broadcast_transaction.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_push_via_privmsg(self, maker_bot):
        """Test that !push is routed correctly from privmsg."""
        import base64

        # Set up the bot with a mock _handle_push
        push_called = False

        async def mock_handle_push(taker_nick: str, msg: str, source: str = "unknown") -> None:
            nonlocal push_called
            push_called = True
            assert taker_nick == "J5taker123"
            assert "push" in msg

        maker_bot._handle_push = mock_handle_push

        # Simulate a privmsg with !push
        tx_bytes = bytes.fromhex("0100000000010000000000")
        tx_b64 = base64.b64encode(tx_bytes).decode("ascii")
        line = f"J5taker123!{maker_bot.nick}!!push {tx_b64}"

        await maker_bot._handle_privmsg(line)

        assert push_called, "_handle_push should have been called"


class TestWalletRescanAndOfferUpdate:
    """Tests for wallet rescan and automatic offer update functionality."""

    @pytest.fixture
    def mock_wallet(self):
        """Create a mock wallet service."""
        from unittest.mock import AsyncMock

        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        wallet.sync_all = AsyncMock()
        wallet.get_total_balance = AsyncMock(return_value=1_000_000)
        wallet.get_balance = AsyncMock(return_value=500_000)
        wallet.get_balance_for_offers = AsyncMock(return_value=500_000)
        return wallet

    @pytest.fixture
    def mock_backend(self):
        """Create a mock blockchain backend."""
        from unittest.mock import AsyncMock

        backend = MagicMock()
        backend.can_provide_neutrino_metadata = MagicMock(return_value=True)
        backend.get_block_height = AsyncMock(return_value=930000)
        return backend

    @pytest.fixture
    def config(self):
        """Create a test maker config."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            post_coinjoin_rescan_delay=5,  # Minimum value for testing
            rescan_interval_sec=60,
            offer_reannounce_delay_max=0,  # Disable delay for test speed
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        """Create a MakerBot instance for testing."""
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
                maxsize=262_144,  # Initial maxsize (2^18, as OfferManager would produce)
                txfee=1000,
                cjfee="0.001",
            )
        ]
        return bot

    @pytest.mark.asyncio
    async def test_resync_wallet_updates_offers_on_balance_change(self, maker_bot, mock_wallet):
        """Test that offers are updated when max balance changes."""
        from unittest.mock import AsyncMock

        # Set up balances: first return old balance, then new balance
        old_balance = 400_000
        new_balance = 600_000
        balance_calls = [old_balance] * 5 + [new_balance] * 5
        mock_wallet.get_balance_for_offers = AsyncMock(side_effect=balance_calls)

        # Mock offer creation
        # maxsize is rounded to nearest power of 2 by OfferManager,
        # so use a power-of-2 value here (2^19 = 524_288)
        new_offer = Offer(
            counterparty=maker_bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=524_288,  # New maxsize after balance increase (rounded)
            txfee=1000,
            cjfee="0.001",
        )
        maker_bot.offer_manager.create_offers = AsyncMock(return_value=[new_offer])
        maker_bot._announce_offers = AsyncMock()

        await maker_bot._resync_wallet_and_update_offers()

        # Wallet should have been synced
        mock_wallet.sync_all.assert_called_once()

        # Offers should have been updated (balance changed)
        maker_bot.offer_manager.create_offers.assert_called_once()
        maker_bot._announce_offers.assert_called_once()
        assert maker_bot.current_offers[0].maxsize == 524_288

    @pytest.mark.asyncio
    async def test_resync_wallet_no_update_when_balance_unchanged(self, maker_bot, mock_wallet):
        """Test that offers are not updated when balance doesn't change."""
        from unittest.mock import AsyncMock

        # Same balance before and after sync
        mock_wallet.get_balance_for_offers = AsyncMock(return_value=400_000)

        maker_bot.offer_manager.create_offers = AsyncMock()
        maker_bot._announce_offers = AsyncMock()

        await maker_bot._resync_wallet_and_update_offers()

        # Wallet should have been synced
        mock_wallet.sync_all.assert_called_once()

        # Offers should NOT have been updated (balance unchanged)
        maker_bot.offer_manager.create_offers.assert_not_called()
        maker_bot._announce_offers.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_offers_keeps_old_if_create_fails(self, maker_bot):
        """Test that old offers are kept if new offer creation fails."""
        from unittest.mock import AsyncMock

        old_maxsize = maker_bot.current_offers[0].maxsize
        maker_bot.offer_manager.create_offers = AsyncMock(return_value=[])  # No offers created
        maker_bot._announce_offers = AsyncMock()

        await maker_bot._update_offers()

        # Old offers should be kept
        assert maker_bot.current_offers[0].maxsize == old_maxsize
        maker_bot._announce_offers.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_offers_skips_if_maxsize_unchanged(self, maker_bot):
        """Test that re-announcement is skipped if maxsize didn't change."""
        from unittest.mock import AsyncMock

        old_maxsize = maker_bot.current_offers[0].maxsize
        new_offer = Offer(
            counterparty=maker_bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=old_maxsize,  # Same maxsize
            txfee=1000,
            cjfee="0.001",
        )
        maker_bot.offer_manager.create_offers = AsyncMock(return_value=[new_offer])
        maker_bot._announce_offers = AsyncMock()

        await maker_bot._update_offers()

        # Announcement should be skipped (no change)
        maker_bot._announce_offers.assert_not_called()

    def test_config_has_rescan_settings(self, config):
        """Test that maker config includes rescan settings."""
        assert hasattr(config, "post_coinjoin_rescan_delay")
        assert hasattr(config, "rescan_interval_sec")
        assert config.post_coinjoin_rescan_delay == 5
        assert config.rescan_interval_sec == 60

    def test_config_default_rescan_values(self):
        """Test default values for rescan settings."""
        default_config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )
        assert default_config.post_coinjoin_rescan_delay == 60
        assert default_config.rescan_interval_sec == 600

    def test_config_has_offer_reannounce_delay_max(self):
        """Test that maker config includes offer reannouncement delay setting."""
        default_config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )
        assert hasattr(default_config, "offer_reannounce_delay_max")
        assert default_config.offer_reannounce_delay_max == 600


class TestOfferPrivacy:
    """Tests for offer privacy improvements (issue #123).

    Verifies that maxsize is rounded to power-of-2 buckets and that
    reannouncement delays are applied to prevent maker tracking.
    """

    def test_round_maxsize_to_power_of_2_exact_powers(self):
        """Test rounding for exact powers of 2 (value is unchanged)."""
        from maker.offers import _round_maxsize_to_power_of_2

        assert _round_maxsize_to_power_of_2(1) == 1
        assert _round_maxsize_to_power_of_2(2) == 2
        assert _round_maxsize_to_power_of_2(1024) == 1024
        assert _round_maxsize_to_power_of_2(1_048_576) == 1_048_576  # 2^20
        assert _round_maxsize_to_power_of_2(67_108_864) == 67_108_864  # 2^26

    def test_round_maxsize_to_power_of_2_between_powers(self):
        """Test rounding for values between powers of 2 (value is floored)."""
        from maker.offers import _round_maxsize_to_power_of_2

        # 100M sats (1 BTC) → 2^26 = 67_108_864
        assert _round_maxsize_to_power_of_2(100_000_000) == 67_108_864
        # 150M sats (1.5 BTC) → 2^27 = 134_217_728
        assert _round_maxsize_to_power_of_2(150_000_000) == 134_217_728
        # 70M sats (0.7 BTC) → 2^26 = 67_108_864
        assert _round_maxsize_to_power_of_2(70_000_000) == 67_108_864
        # 10M sats (0.1 BTC) → 2^23 = 8_388_608
        assert _round_maxsize_to_power_of_2(10_000_000) == 8_388_608
        # 500_000 sats → 2^18 = 262_144
        assert _round_maxsize_to_power_of_2(500_000) == 262_144

    def test_round_maxsize_to_power_of_2_edge_cases(self):
        """Test rounding edge cases: zero and negative values."""
        from maker.offers import _round_maxsize_to_power_of_2

        assert _round_maxsize_to_power_of_2(0) == 0
        assert _round_maxsize_to_power_of_2(-1) == 0
        assert _round_maxsize_to_power_of_2(-100) == 0

    @pytest.fixture
    def mock_wallet(self):
        from unittest.mock import AsyncMock

        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        wallet.sync_all = AsyncMock()
        wallet.get_total_balance = AsyncMock(return_value=1_000_000)
        wallet.get_balance = AsyncMock(return_value=500_000)
        wallet.get_balance_for_offers = AsyncMock(return_value=500_000)
        return wallet

    @pytest.fixture
    def mock_backend(self):
        from unittest.mock import AsyncMock

        backend = MagicMock()
        backend.can_provide_neutrino_metadata = MagicMock(return_value=True)
        backend.get_block_height = AsyncMock(return_value=930000)
        return backend

    @pytest.fixture
    def config_with_delay(self):
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_reannounce_delay_max=300,
        )

    @pytest.fixture
    def config_no_delay(self):
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_reannounce_delay_max=0,
        )

    @pytest.mark.asyncio
    async def test_no_reannounce_when_balance_stays_in_same_bucket(
        self, mock_wallet, mock_backend, config_no_delay
    ):
        """When balance changes but stays in the same power-of-2 bucket, no re-announcement."""
        from unittest.mock import AsyncMock

        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config_no_delay,
        )
        # Initial offer with maxsize at 2^18 = 262_144
        bot.current_offers = [
            Offer(
                counterparty=bot.nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=262_144,
                txfee=1000,
                cjfee="0.001",
            )
        ]

        # New offer still rounds to 2^18 (balance shifted but stayed in bucket)
        same_bucket_offer = Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=262_144,
            txfee=1000,
            cjfee="0.001",
        )
        bot.offer_manager.create_offers = AsyncMock(return_value=[same_bucket_offer])
        bot._announce_offers = AsyncMock()

        await bot._update_offers()

        # No re-announcement since rounded maxsize is the same
        bot._announce_offers.assert_not_called()

    @pytest.mark.asyncio
    async def test_reannounce_delay_applied(self, mock_wallet, mock_backend, config_with_delay):
        """When offers change and delay is configured, asyncio.sleep is called."""
        from unittest.mock import AsyncMock, patch

        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config_with_delay,
        )
        bot.current_offers = [
            Offer(
                counterparty=bot.nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=262_144,
                txfee=1000,
                cjfee="0.001",
            )
        ]

        # New offer with different bucket
        new_offer = Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=524_288,
            txfee=1000,
            cjfee="0.001",
        )
        bot.offer_manager.create_offers = AsyncMock(return_value=[new_offer])
        bot._announce_offers = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await bot._update_offers()

            mock_sleep.assert_called_once()
            delay = mock_sleep.call_args[0][0]
            assert 0 <= delay <= 300

        # Offers should have been announced after delay
        bot._announce_offers.assert_called_once()

    @pytest.mark.asyncio
    async def test_reannounce_no_delay_when_zero(self, mock_wallet, mock_backend, config_no_delay):
        """When offer_reannounce_delay_max is 0, no sleep is applied."""
        from unittest.mock import AsyncMock, patch

        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config_no_delay,
        )
        bot.current_offers = [
            Offer(
                counterparty=bot.nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=262_144,  # 2^18
                txfee=1000,
                cjfee="0.001",
            )
        ]

        new_offer = Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=524_288,
            txfee=1000,
            cjfee="0.001",
        )
        bot.offer_manager.create_offers = AsyncMock(return_value=[new_offer])
        bot._announce_offers = AsyncMock()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await bot._update_offers()

            mock_sleep.assert_not_called()

        bot._announce_offers.assert_called_once()

    @pytest.mark.asyncio
    async def test_relative_offer_skipped_when_rounded_max_below_profit_minsize(
        self, mock_wallet, mock_backend, config_no_delay
    ):
        """Regression: rounded_max must be compared against the effective min_size
        (which accounts for profitability), not just offer_cfg.min_size.

        With a high tx_fee_contribution relative to cj_fee_relative, min_size_for_profit
        can exceed rounded_max even when rounded_max > offer_cfg.min_size, which would
        produce an invalid offer where minsize > maxsize.
        """
        from unittest.mock import AsyncMock, patch

        from maker.config import OfferConfig
        from maker.offers import OfferManager

        # max_balance=180_000, tx_fee_contribution=1000, cj_fee_relative=0.001
        # max_available = 180_000 - 27_300 (dust) = 152_700
        # rounded_max = 2^17 = 131_072
        # min_size_for_profit = int(1.5 * 1000 / 0.001) = 1_500_000
        # min_size = max(1_500_000, 27_300) = 1_500_000
        # Without fix: rounded_max (131_072) > offer_cfg.min_size (27_300) -> CREATED
        #              but minsize=1_500_000 > maxsize=131_072 (invalid offer!)
        # With fix:    rounded_max (131_072) <= min_size (1_500_000) -> SKIPPED (correct)
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            offer_reannounce_delay_max=0,
            offer_configs=[
                OfferConfig(
                    offer_type=OfferType.SW0_RELATIVE,
                    min_size=27_300,
                    cj_fee_relative="0.001",
                    tx_fee_contribution=1_000,
                )
            ],
        )
        mock_wallet.get_balance_for_offers = AsyncMock(return_value=180_000)

        manager = OfferManager(mock_wallet, config, "J5TestMaker")
        with patch("maker.offers.get_best_fidelity_bond", new=AsyncMock(return_value=None)):
            offers = await manager.create_offers()

        # Offer must be skipped, not created with minsize > maxsize
        assert offers == [], (
            f"Expected no offers, but got {len(offers)} offer(s) with "
            f"minsize={offers[0].minsize if offers else 'N/A'}, "
            f"maxsize={offers[0].maxsize if offers else 'N/A'}"
        )


class TestPeerCountDetection:
    """Tests for peer count detection after CoinJoin confirmation."""

    @pytest.fixture
    def mock_wallet(self):
        """Create a mock wallet service."""
        from unittest.mock import AsyncMock

        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        wallet.sync_all = AsyncMock()
        wallet.get_total_balance = AsyncMock(return_value=1_000_000)
        return wallet

    @pytest.fixture
    def mock_backend(self):
        """Create a mock blockchain backend with transaction data."""
        from unittest.mock import AsyncMock

        from jmwallet.backends.base import Transaction

        backend = MagicMock()
        backend.get_block_height = AsyncMock(return_value=930000)

        # Mock transaction with 3 equal-value outputs (peer count = 3)
        mock_tx = Transaction(
            txid="test_txid_123",
            confirmations=1,
            raw="01000000...",  # Minimal mock
        )
        backend.get_transaction = AsyncMock(return_value=mock_tx)

        return backend

    @pytest.fixture
    def config(self, tmp_path):
        """Create a test maker config with temp data dir."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            data_dir=tmp_path,
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        """Create a MakerBot instance for testing."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )
        return bot

    @pytest.mark.asyncio
    async def test_update_pending_history_calls_detection_function(
        self, maker_bot, mock_backend, config, tmp_path
    ):
        """Test that _update_pending_history uses update_transaction_confirmation_with_detection.

        This ensures that makers can automatically detect peer count after transaction
        confirmation, since they don't know the full transaction until it's broadcast.
        """
        from jmwallet.history import append_history_entry, create_maker_history_entry

        # Create a pending history entry
        entry = create_maker_history_entry(
            taker_nick="J5TakerNick",
            cj_amount=91554,
            fee_received=0,
            txfee_contribution=0,
            cj_address="bc1qtest",
            change_address="bc1qchange",
            our_utxos=[("abcd1234" * 8, 0)],
            txid="test_txid_123",
            network="regtest",
        )
        append_history_entry(entry, data_dir=tmp_path)

        # Mock the detection function to verify it's called
        from unittest.mock import AsyncMock, patch

        with patch("jmwallet.history.detect_coinjoin_peer_count", new=AsyncMock(return_value=3)):
            # Run the update
            await maker_bot._update_pending_history()

            # Read the history back
            from jmwallet.history import read_history

            entries = read_history(data_dir=tmp_path)
            assert len(entries) == 1

            # Transaction should be marked as confirmed
            assert entries[0].confirmations == 1
            assert entries[0].success is True

            # Peer count should be detected and set
            assert entries[0].peer_count == 3


class TestDirectoryReconnection:
    """Tests for directory server reconnection functionality."""

    @pytest.fixture
    def mock_wallet(self):
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        return wallet

    @pytest.fixture
    def mock_backend(self):
        backend = MagicMock()
        backend.can_provide_neutrino_metadata = MagicMock(return_value=True)
        return backend

    @pytest.fixture
    def config(self):
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=[
                "dir1.onion:5222",
                "dir2.onion:5222",
                "dir3.onion:5222",
            ],
            network=NetworkType.REGTEST,
            directory_reconnect_interval=300,
            directory_reconnect_max_retries=0,  # Unlimited
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )
        return bot

    def test_reconnect_attempts_tracking_initialized(self, maker_bot):
        """Test that reconnection attempts tracking is initialized."""
        assert maker_bot._directory_reconnect_attempts == {}

    def test_config_reconnect_defaults(self):
        """Test default reconnection config values."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )
        assert config.directory_reconnect_interval == 300  # 5 minutes
        assert config.directory_reconnect_max_retries == 0  # Unlimited

    def test_config_reconnect_custom_values(self):
        """Test custom reconnection config values."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            directory_reconnect_interval=60,
            directory_reconnect_max_retries=10,
        )
        assert config.directory_reconnect_interval == 60
        assert config.directory_reconnect_max_retries == 10

    @pytest.mark.asyncio
    async def test_connect_to_directory_success(self, maker_bot, mock_backend):
        """Test successful connection to a directory."""
        from unittest.mock import AsyncMock, patch

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        with patch("maker.background_tasks.DirectoryClient", return_value=mock_client):
            result = await maker_bot._connect_to_directory("test.onion:5222")

            assert result is not None
            node_id, client = result
            assert node_id == "test.onion:5222"
            assert client == mock_client
            mock_client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_connect_to_directory_failure(self, maker_bot):
        """Test failed connection to a directory."""
        from unittest.mock import AsyncMock, patch

        mock_client = MagicMock()
        mock_client.connect = AsyncMock(side_effect=Exception("Connection failed"))

        with patch("maker.background_tasks.DirectoryClient", return_value=mock_client):
            result = await maker_bot._connect_to_directory("bad.onion:5222")

            assert result is None

    @pytest.mark.asyncio
    async def test_connect_to_directory_default_port(self, maker_bot):
        """Test connection uses default port 5222 when not specified."""
        from unittest.mock import AsyncMock, patch

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        with patch(
            "maker.background_tasks.DirectoryClient", return_value=mock_client
        ) as mock_client_class:
            result = await maker_bot._connect_to_directory("test.onion")

            assert result is not None
            node_id, _ = result
            assert node_id == "test.onion:5222"
            # Verify DirectoryClient was called with port 5222
            mock_client_class.assert_called_once()
            call_kwargs = mock_client_class.call_args[1]
            assert call_kwargs["port"] == 5222

    def test_listener_removes_client_on_disconnect(self, maker_bot):
        """Test that disconnected clients are removed from directory_clients dict."""
        # Add a client
        mock_client = MagicMock()
        maker_bot.directory_clients["test.onion:5222"] = mock_client

        assert "test.onion:5222" in maker_bot.directory_clients

        # Simulate removal (as done in _listen_client on disconnect)
        maker_bot.directory_clients.pop("test.onion:5222", None)

        assert "test.onion:5222" not in maker_bot.directory_clients

    def test_retry_attempts_increment(self, maker_bot):
        """Test that retry attempts are tracked correctly."""
        node_id = "failed.onion:5222"

        # Initially no attempts
        assert maker_bot._directory_reconnect_attempts.get(node_id, 0) == 0

        # Increment
        maker_bot._directory_reconnect_attempts[node_id] = 1
        assert maker_bot._directory_reconnect_attempts[node_id] == 1

        maker_bot._directory_reconnect_attempts[node_id] = 2
        assert maker_bot._directory_reconnect_attempts[node_id] == 2

    def test_retry_attempts_reset_on_success(self, maker_bot):
        """Test that retry attempts are reset after successful reconnection."""
        node_id = "reconnected.onion:5222"

        # Set some retry attempts
        maker_bot._directory_reconnect_attempts[node_id] = 5

        # Simulate successful reconnection (pop resets)
        maker_bot._directory_reconnect_attempts.pop(node_id, None)

    @pytest.mark.asyncio
    async def test_connect_to_directories_with_retry_immediate_success(self, maker_bot):
        """All directories connect on the first attempt — no retry loop needed."""
        from unittest.mock import AsyncMock, patch

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        with patch("maker.background_tasks.DirectoryClient", return_value=mock_client):
            await maker_bot._connect_to_directories_with_retry()

        assert len(maker_bot.directory_clients) == 3

    @pytest.mark.asyncio
    async def test_connect_to_directories_with_retry_success_on_second_attempt(self, maker_bot):
        """All directories fail first attempt, succeed on second (Tor bootstrapping)."""
        from unittest.mock import AsyncMock, patch

        call_count = 0

        async def connect_side_effect() -> None:
            nonlocal call_count
            call_count += 1
            if call_count <= 3:  # 3 servers, all fail on first pass
                raise Exception("Tor not ready")

        mock_client = MagicMock()
        mock_client.connect = AsyncMock(side_effect=connect_side_effect)

        with patch("maker.background_tasks.DirectoryClient", return_value=mock_client):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await maker_bot._connect_to_directories_with_retry()

        assert len(maker_bot.directory_clients) == 3

    @pytest.mark.asyncio
    async def test_connect_to_directories_with_retry_timeout(self, maker_bot):
        """
        All directories keep failing — method returns after timeout without raising.
        The background reconnect task takes over.
        """
        from unittest.mock import AsyncMock, patch

        mock_client = MagicMock()
        mock_client.connect = AsyncMock(side_effect=Exception("Tor not ready"))

        # Very short timeout so the test doesn't take long
        object.__setattr__(maker_bot.config, "directory_startup_timeout", 1)

        with patch("maker.background_tasks.DirectoryClient", return_value=mock_client):
            # Should not raise, just return after timeout
            await maker_bot._connect_to_directories_with_retry()

        assert len(maker_bot.directory_clients) == 0

    @pytest.mark.asyncio
    async def test_connect_to_directories_with_retry_skips_already_connected(self, maker_bot):
        """Already-connected directories are not reconnected in a retry pass."""
        from unittest.mock import AsyncMock, patch

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()

        # Pre-populate one connected directory
        maker_bot.directory_clients["dir1.onion:5222"] = mock_client

        with patch("maker.background_tasks.DirectoryClient", return_value=mock_client):
            await maker_bot._connect_to_directories_with_retry()

        # All 3 should be connected now
        assert len(maker_bot.directory_clients) == 3
        # dir1 should have been connected only once (not reconnected)
        # dir2 and dir3 get new clients via connect()
        assert "dir1.onion:5222" in maker_bot.directory_clients

    def test_all_directories_disconnected_initialized_false(self, maker_bot):
        """_all_directories_disconnected flag starts as False."""
        assert maker_bot._all_directories_disconnected is False

    @pytest.mark.asyncio
    async def test_recovery_notification_sent_when_all_directories_were_disconnected(
        self, maker_bot
    ):
        """Recovery notification is sent when reconnecting after all-disconnect state."""
        from unittest.mock import AsyncMock, patch

        maker_bot._all_directories_disconnected = True
        maker_bot.running = True

        mock_client = MagicMock()
        mock_client.announce_orders = AsyncMock()

        sleep_call_count = 0

        async def sleep_and_stop(_seconds: float) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 2:
                maker_bot.running = False

        recovery_notify = AsyncMock(return_value=True)
        reconnect_notify = AsyncMock(return_value=True)

        def create_task_stub(coro: object) -> MagicMock:
            if asyncio.iscoroutine(coro):
                coro.close()
            return MagicMock()

        with (
            patch.object(
                maker_bot,
                "_connect_to_directory",
                AsyncMock(return_value=("dir1.onion:5222", mock_client)),
            ),
            patch("maker.background_tasks.asyncio.sleep", side_effect=sleep_and_stop),
            patch("maker.background_tasks.asyncio.create_task", side_effect=create_task_stub),
            patch("maker.background_tasks.get_notifier") as mock_get_notifier,
        ):
            mock_notifier = MagicMock()
            mock_notifier.notify_directory_reconnect = reconnect_notify
            mock_notifier.notify_all_directories_reconnected = recovery_notify
            mock_get_notifier.return_value = mock_notifier

            await maker_bot._periodic_directory_reconnect()

        assert maker_bot._all_directories_disconnected is False

    @pytest.mark.asyncio
    async def test_recovery_notification_not_sent_when_no_all_disconnect_state(self, maker_bot):
        """Recovery notification is not fired when _all_directories_disconnected is False."""
        from unittest.mock import AsyncMock, patch

        maker_bot._all_directories_disconnected = False
        maker_bot.running = True

        mock_client = MagicMock()
        mock_client.announce_orders = AsyncMock()

        sleep_call_count = 0

        async def sleep_and_stop(_seconds: float) -> None:
            nonlocal sleep_call_count
            sleep_call_count += 1
            if sleep_call_count >= 2:
                maker_bot.running = False

        recovery_notify = AsyncMock(return_value=True)

        def create_task_stub(coro: object) -> MagicMock:
            if asyncio.iscoroutine(coro):
                coro.close()
            return MagicMock()

        with (
            patch.object(
                maker_bot,
                "_connect_to_directory",
                AsyncMock(return_value=("dir1.onion:5222", mock_client)),
            ),
            patch("maker.background_tasks.asyncio.sleep", side_effect=sleep_and_stop),
            patch("maker.background_tasks.asyncio.create_task", side_effect=create_task_stub),
            patch("maker.background_tasks.get_notifier") as mock_get_notifier,
        ):
            mock_notifier = MagicMock()
            mock_notifier.notify_directory_reconnect = AsyncMock(return_value=True)
            mock_notifier.notify_all_directories_reconnected = recovery_notify
            mock_get_notifier.return_value = mock_notifier

            await maker_bot._periodic_directory_reconnect()

        recovery_notify.assert_not_called()

    def test_config_startup_timeout_default(self):
        """Test default startup timeout value."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )
        assert config.directory_startup_timeout == 120

    def test_config_startup_timeout_custom(self):
        """Test custom startup timeout value."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            directory_startup_timeout=60,
        )
        assert config.directory_startup_timeout == 60


class TestDirectConnectionHandshake:
    """Tests for handling handshake messages on direct connections."""

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
        # Full node backend can provide neutrino metadata
        backend.can_provide_neutrino_metadata.return_value = True
        return backend

    @pytest.fixture
    def config(self):
        """Create a test maker config."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        """Create a MakerBot instance for testing."""
        bot = MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )
        return bot

    @pytest.mark.asyncio
    async def test_try_handle_handshake_returns_false_for_non_handshake(self, maker_bot):
        """Test that non-handshake messages return False."""
        mock_conn = MagicMock(spec=TCPConnection)

        # PRIVMSG type message
        privmsg_data = json.dumps({"type": 685, "line": "test"}).encode("utf-8")
        result = await maker_bot._try_handle_handshake(mock_conn, privmsg_data, "test:1234")
        assert result is False

        # Invalid JSON
        result = await maker_bot._try_handle_handshake(mock_conn, b"not json", "test:1234")
        assert result is False

    @pytest.mark.asyncio
    async def test_try_handle_handshake_responds_with_peer_handshake(self, maker_bot):
        """Test that handshake request gets HANDSHAKE (793) response with client format.

        In the reference implementation, non-directory peers (makers) respond to
        incoming handshakes with their own HANDSHAKE (793) using the client handshake
        format -- NOT DN_HANDSHAKE (795). Only directories use DN_HANDSHAKE.
        """
        mock_conn = MagicMock(spec=TCPConnection)

        # Create a handshake request (type 793)
        handshake_request = {
            "type": 793,  # HANDSHAKE
            "line": json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": 5,
                    "features": {"peerlist_features": True},
                    "nick": "J5TestNick",
                    "network": "regtest",
                }
            ),
        }
        data = json.dumps(handshake_request).encode("utf-8")

        result = await maker_bot._try_handle_handshake(mock_conn, data, "test:1234")

        assert result is True
        mock_conn.send.assert_called_once()

        # Parse the response
        response_bytes = mock_conn.send.call_args[0][0]
        response = json.loads(response_bytes.decode("utf-8"))

        # Should be HANDSHAKE (793), NOT DN_HANDSHAKE (795)
        assert response["type"] == 793

        # Parse the response data - should use client handshake format
        response_data = json.loads(response["line"])
        assert response_data["directory"] is False
        assert response_data["proto-ver"] == 5
        assert response_data["nick"] == maker_bot.nick
        assert response_data["network"] == "regtest"
        assert "location-string" in response_data
        assert response_data["app-name"] == "joinmarket"

        # Should NOT have server-format fields
        assert "accepted" not in response_data
        assert "proto-ver-min" not in response_data
        assert "proto-ver-max" not in response_data
        assert "motd" not in response_data

        # Should include features
        features = response_data.get("features", {})
        assert "neutrino_compat" in features
        assert features["neutrino_compat"] is True
        assert "peerlist_features" in features
        assert features["peerlist_features"] is True

    @pytest.mark.asyncio
    async def test_try_handle_handshake_ignores_wrong_network(self, maker_bot):
        """Test that handshake from wrong network is silently ignored (no response)."""
        mock_conn = MagicMock(spec=TCPConnection)

        # Create a handshake request with wrong network
        handshake_request = {
            "type": 793,
            "line": json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": 5,
                    "features": {},
                    "nick": "J5TestNick",
                    "network": "mainnet",  # Wrong network (we're on regtest)
                }
            ),
        }
        data = json.dumps(handshake_request).encode("utf-8")

        result = await maker_bot._try_handle_handshake(mock_conn, data, "test:1234")

        # Should still return True (was a handshake message, handled)
        assert result is True
        # Should NOT send any response for network mismatch
        mock_conn.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_try_handle_handshake_neutrino_backend_no_neutrino_compat(self, maker_bot):
        """Test that Neutrino backend doesn't advertise neutrino_compat."""
        mock_conn = MagicMock(spec=TCPConnection)

        # Configure backend as Neutrino (can't provide neutrino metadata)
        maker_bot.backend.can_provide_neutrino_metadata.return_value = False

        handshake_request = {
            "type": 793,
            "line": json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": 5,
                    "features": {},
                    "nick": "J5TestNick",
                    "network": "regtest",
                }
            ),
        }
        data = json.dumps(handshake_request).encode("utf-8")

        await maker_bot._try_handle_handshake(mock_conn, data, "test:1234")

        response_bytes = mock_conn.send.call_args[0][0]
        response = json.loads(response_bytes.decode("utf-8"))

        # Should be HANDSHAKE (793) with client format
        assert response["type"] == 793
        response_data = json.loads(response["line"])
        assert response_data["directory"] is False

        # Should NOT include neutrino_compat
        features = response_data.get("features", {})
        assert "neutrino_compat" not in features or features.get("neutrino_compat") is False
        # But should still have peerlist_features
        assert features.get("peerlist_features") is True


class TestReferenceCompatHandshake:
    """Regression tests verifying maker handshake is accepted by the reference implementation.

    These tests replicate the reference implementation's taker-side handshake validation
    logic from jmdaemon/onionmc.py:process_handshake(). If our maker's handshake response
    would be rejected by the reference taker, these tests fail.

    Background: The reference taker has TWO code paths for processing handshake responses:
    - dn-handshake (type 795): Only accepted from peers marked as directory nodes.
      If received from a non-directory peer, it logs "Unexpected dn-handshake from non-dn
      node" and ignores the message entirely.
    - handshake (type 793): Accepted from any non-directory peer. This is the symmetric
      peer-to-peer handshake used between takers and makers.

    Our maker previously sent DN_HANDSHAKE (795) which the reference taker rejected.
    """

    JM_APP_NAME = "joinmarket"
    JM_VERSION = 5

    @pytest.fixture
    def mock_wallet(self):
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        return wallet

    @pytest.fixture
    def mock_backend(self):
        backend = MagicMock()
        backend.can_provide_neutrino_metadata.return_value = True
        return backend

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

    def _reference_taker_validate_handshake(
        self, msg_type: int, payload: dict, peer_is_directory: bool
    ) -> tuple[bool, str]:
        """Simulate the reference implementation's process_handshake() validation.

        This replicates the critical logic from joinmarket-clientserver
        src/jmdaemon/onionmc.py lines 1200-1322, specifically the checks
        that determine whether a handshake response is accepted or rejected.

        Returns (accepted, reason) tuple.
        """
        # Reference: process_control_message dispatches based on message type
        if msg_type == 795:  # dn-handshake
            # Reference: process_handshake(peerid, msgval, dn=True)
            # Line 1220: if not peer.directory -> reject
            if not peer_is_directory:
                return False, "Unexpected dn-handshake from non-dn node"
            # Directory validation (lines 1228-1268)
            app_name = payload.get("app-name")
            is_directory = payload.get("directory")
            proto_min: int = payload.get("proto-ver-min", 0)
            proto_max: int = payload.get("proto-ver-max", 0)
            accepted = payload.get("accepted")
            if not accepted:
                return False, "Directory rejected our handshake"
            if not (
                app_name == self.JM_APP_NAME
                and is_directory
                and self.JM_VERSION <= proto_max
                and self.JM_VERSION >= proto_min
                and accepted
            ):
                return False, f"Incompatible or rejected: {payload}"
            return True, "OK"

        elif msg_type == 793:  # handshake
            # Reference: process_handshake(peerid, msgval, dn=False)
            # Lines 1270-1322: non-dn peer handshake
            app_name = payload.get("app-name")
            is_directory = payload.get("directory")
            proto_ver = payload.get("proto-ver")
            # Line 1295-1296
            if not (
                app_name == self.JM_APP_NAME and proto_ver == self.JM_VERSION and not is_directory
            ):
                return False, f"Invalid handshake name/version data: {payload}"
            return True, "OK"

        else:
            return False, f"Unknown message type: {msg_type}"

    @pytest.mark.asyncio
    async def test_maker_handshake_accepted_by_reference_taker(self, maker_bot):
        """Regression: maker's handshake response must pass reference taker validation.

        The reference taker treats our maker as a non-directory peer. If we send
        DN_HANDSHAKE (795), the reference taker rejects it with 'Unexpected dn-handshake
        from non-dn node'. We must send HANDSHAKE (793) with client format.
        """
        mock_conn = MagicMock(spec=TCPConnection)

        handshake_request = {
            "type": 793,
            "line": json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": 5,
                    "features": {},
                    "nick": "J5RefTakerNick",
                    "network": "regtest",
                }
            ),
        }
        data = json.dumps(handshake_request).encode("utf-8")

        await maker_bot._try_handle_handshake(mock_conn, data, "test:1234")

        response_bytes = mock_conn.send.call_args[0][0]
        response = json.loads(response_bytes.decode("utf-8"))
        response_data = json.loads(response["line"])

        # Simulate reference taker validation: our maker is NOT a directory peer
        accepted, reason = self._reference_taker_validate_handshake(
            msg_type=response["type"],
            payload=response_data,
            peer_is_directory=False,
        )
        assert accepted, f"Reference taker would reject our handshake: {reason}"

    @pytest.mark.asyncio
    async def test_maker_handshake_must_not_use_dn_handshake_type(self, maker_bot):
        """Regression: maker must never send DN_HANDSHAKE (795) to peers.

        DN_HANDSHAKE is reserved for directory nodes. Non-directory peers that send
        it are rejected by the reference implementation.
        """
        mock_conn = MagicMock(spec=TCPConnection)

        handshake_request = {
            "type": 793,
            "line": json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": 5,
                    "features": {},
                    "nick": "J5RefTakerNick",
                    "network": "regtest",
                }
            ),
        }
        data = json.dumps(handshake_request).encode("utf-8")

        await maker_bot._try_handle_handshake(mock_conn, data, "test:1234")

        response_bytes = mock_conn.send.call_args[0][0]
        response = json.loads(response_bytes.decode("utf-8"))

        assert response["type"] != 795, (
            "Maker must not send DN_HANDSHAKE (795). "
            "Reference taker rejects dn-handshake from non-directory peers."
        )
        assert response["type"] == 793, (
            "Maker must send HANDSHAKE (793) with client format for peer-to-peer handshake."
        )

    @pytest.mark.asyncio
    async def test_maker_handshake_must_not_claim_directory(self, maker_bot):
        """Regression: maker handshake must have directory=False.

        The reference taker validates that non-directory peers have directory=False
        in their handshake (line 1296: 'not is_directory').
        """
        mock_conn = MagicMock(spec=TCPConnection)

        handshake_request = {
            "type": 793,
            "line": json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": 5,
                    "features": {},
                    "nick": "J5RefTakerNick",
                    "network": "regtest",
                }
            ),
        }
        data = json.dumps(handshake_request).encode("utf-8")

        await maker_bot._try_handle_handshake(mock_conn, data, "test:1234")

        response_bytes = mock_conn.send.call_args[0][0]
        response = json.loads(response_bytes.decode("utf-8"))
        response_data = json.loads(response["line"])

        assert response_data.get("directory") is False, (
            "Maker handshake must have directory=False. "
            "Reference taker rejects handshakes with directory=True from non-dn peers."
        )

    @pytest.mark.asyncio
    async def test_maker_handshake_uses_client_format(self, maker_bot):
        """Regression: maker handshake must use client format, not server format.

        Client format has: app-name, directory, location-string, proto-ver, features, nick, network
        Server format has: app-name, directory, proto-ver-min/max, accepted, nick, network
        """
        mock_conn = MagicMock(spec=TCPConnection)

        handshake_request = {
            "type": 793,
            "line": json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": 5,
                    "features": {},
                    "nick": "J5RefTakerNick",
                    "network": "regtest",
                }
            ),
        }
        data = json.dumps(handshake_request).encode("utf-8")

        await maker_bot._try_handle_handshake(mock_conn, data, "test:1234")

        response_bytes = mock_conn.send.call_args[0][0]
        response = json.loads(response_bytes.decode("utf-8"))
        response_data = json.loads(response["line"])

        # Must have client format fields
        assert "proto-ver" in response_data, "Missing proto-ver (client format field)"
        assert "location-string" in response_data, "Missing location-string (client format field)"
        assert response_data["proto-ver"] == 5

        # Must NOT have server format fields
        assert "proto-ver-min" not in response_data, (
            "Has proto-ver-min (server format field) -- maker should use client format"
        )
        assert "proto-ver-max" not in response_data, (
            "Has proto-ver-max (server format field) -- maker should use client format"
        )
        assert "accepted" not in response_data, (
            "Has accepted (server format field) -- maker should use client format"
        )
        assert "motd" not in response_data, (
            "Has motd (server format field) -- maker should use client format"
        )


class TestListenTasksMemoryLeak:
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

    def test_prune_done_tasks_removes_completed(self, maker_bot):
        """_prune_done_tasks should filter out tasks whose done() returns True."""
        import asyncio

        loop = asyncio.new_event_loop()
        try:
            # Create a task that is still pending
            async def run_pending() -> None:
                # Use a future that will never complete during this test
                fut: asyncio.Future[None] = loop.create_future()
                await fut

            pending_task = loop.create_task(run_pending())

            # Manually inject both into listen_tasks (simulate post-reconnect state)
            # We need a "done" task; since loop.run_until_complete finished it, reuse
            # a completed task by creating a new one and manually completing it.
            async def already_done() -> None:
                pass

            completed_task = loop.create_task(already_done())
            loop.run_until_complete(asyncio.sleep(0))  # Let completed_task finish

            maker_bot.listen_tasks = [completed_task, pending_task]

            maker_bot._prune_done_tasks()

            # Only the pending task should remain
            assert pending_task in maker_bot.listen_tasks
            assert completed_task not in maker_bot.listen_tasks
            assert len(maker_bot.listen_tasks) == 1

            pending_task.cancel()
            try:
                loop.run_until_complete(pending_task)
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            loop.close()

    def test_prune_done_tasks_noop_when_all_running(self, maker_bot):
        """_prune_done_tasks should leave running tasks untouched."""
        import asyncio

        loop = asyncio.new_event_loop()
        try:

            async def long_running() -> None:
                fut: asyncio.Future[None] = loop.create_future()
                await fut

            t1 = loop.create_task(long_running())
            t2 = loop.create_task(long_running())
            maker_bot.listen_tasks = [t1, t2]

            maker_bot._prune_done_tasks()

            assert len(maker_bot.listen_tasks) == 2

            for t in [t1, t2]:
                t.cancel()
                try:
                    loop.run_until_complete(t)
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            loop.close()

    def test_prune_done_tasks_noop_when_empty(self, maker_bot):
        """_prune_done_tasks should be safe to call on an empty list."""
        maker_bot.listen_tasks = []
        maker_bot._prune_done_tasks()
        assert maker_bot.listen_tasks == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
