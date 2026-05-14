"""
Tests for transaction broadcast functionality.

Tests the broadcast policy options (self, random-peer, not-self) and
the delegation of broadcasting to makers via !push command.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from taker.config import BroadcastPolicy, TakerConfig


class TestBroadcastPolicy:
    """Tests for BroadcastPolicy enum."""

    def test_policy_values(self) -> None:
        """Test broadcast policy enum values."""
        assert BroadcastPolicy.SELF.value == "self"
        assert BroadcastPolicy.RANDOM_PEER.value == "random-peer"
        assert BroadcastPolicy.MULTIPLE_PEERS.value == "multiple-peers"
        assert BroadcastPolicy.NOT_SELF.value == "not-self"

    def test_policy_from_string(self) -> None:
        """Test creating policy from string."""
        assert BroadcastPolicy("self") == BroadcastPolicy.SELF
        assert BroadcastPolicy("random-peer") == BroadcastPolicy.RANDOM_PEER
        assert BroadcastPolicy("multiple-peers") == BroadcastPolicy.MULTIPLE_PEERS
        assert BroadcastPolicy("not-self") == BroadcastPolicy.NOT_SELF


class TestTakerConfigBroadcast:
    """Tests for broadcast configuration in TakerConfig."""

    def test_default_broadcast_policy(self, sample_mnemonic: str) -> None:
        """Test default broadcast policy is multiple-peers."""
        config = TakerConfig(mnemonic=sample_mnemonic)
        assert config.tx_broadcast == BroadcastPolicy.MULTIPLE_PEERS

    def test_explicit_self_policy(self, sample_mnemonic: str) -> None:
        """Test explicitly setting self broadcast policy."""
        config = TakerConfig(
            mnemonic=sample_mnemonic,
            tx_broadcast=BroadcastPolicy.SELF,
        )
        assert config.tx_broadcast == BroadcastPolicy.SELF

    def test_explicit_not_self_policy(self, sample_mnemonic: str) -> None:
        """Test explicitly setting not-self broadcast policy."""
        config = TakerConfig(
            mnemonic=sample_mnemonic,
            tx_broadcast=BroadcastPolicy.NOT_SELF,
        )
        assert config.tx_broadcast == BroadcastPolicy.NOT_SELF

    def test_broadcast_timeout_default(self, sample_mnemonic: str) -> None:
        """Test default broadcast timeout."""
        config = TakerConfig(mnemonic=sample_mnemonic)
        assert config.broadcast_timeout_sec == 30

    def test_broadcast_timeout_custom(self, sample_mnemonic: str) -> None:
        """Test custom broadcast timeout."""
        config = TakerConfig(
            mnemonic=sample_mnemonic,
            broadcast_timeout_sec=60,
        )
        assert config.broadcast_timeout_sec == 60


class TestTakerBroadcast:
    """Tests for Taker broadcast methods."""

    @pytest.fixture
    def mock_wallet(self):
        """Create a mock wallet service."""
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.network = "regtest"
        wallet.sync_all = AsyncMock()
        wallet.close = AsyncMock()
        return wallet

    @pytest.fixture
    def mock_backend(self):
        """Create a mock blockchain backend."""
        backend = MagicMock()
        backend.broadcast_transaction = AsyncMock(return_value="txid123")
        backend.get_transaction = AsyncMock(return_value=None)
        backend.get_block_height = AsyncMock(return_value=850000)  # Mock current block height
        backend.verify_tx_output = AsyncMock(return_value=False)  # Default: verification fails
        backend.requires_neutrino_metadata = MagicMock(return_value=False)
        return backend

    @pytest.fixture
    def taker_config(self, sample_mnemonic: str):
        """Create a taker config for testing."""
        return TakerConfig(
            mnemonic=sample_mnemonic,
            network="regtest",
            directory_servers=["localhost:5222"],
            tx_broadcast=BroadcastPolicy.SELF,
            broadcast_timeout_sec=5,
        )

    @pytest.fixture
    def taker(self, mock_wallet, mock_backend, taker_config):
        """Create a Taker instance for testing."""
        from taker.taker import Taker

        taker = Taker(
            wallet=mock_wallet,
            backend=mock_backend,
            config=taker_config,
        )
        # Set up test data - a minimal valid SegWit transaction
        # This is a simple 1-in-1-out P2WPKH tx with empty witness
        # Version (4 bytes) + marker (1) + flag (1) + input count (1) + input (41) +
        # output count (1) + output (34) + witness count (1) + witness items (1 empty) +
        # locktime (4)
        taker._session.final_tx = bytes.fromhex(
            "02000000"  # version
            "0001"  # marker + flag (SegWit)
            "01"  # 1 input
            "0000000000000000000000000000000000000000000000000000000000000001"  # prev txid
            "00000000"  # prev vout
            "00"  # scriptsig length (empty for segwit)
            "ffffffff"  # sequence
            "01"  # 1 output
            "0000000000000000"  # value (0 sats)
            "160014"  # P2WPKH scriptpubkey prefix
            "0000000000000000000000000000000000000000"  # pubkey hash
            "00"  # witness - 0 items for this input (empty)
            "00000000"  # locktime
        )
        return taker

    @pytest.mark.asyncio
    async def test_broadcast_self_success(self, taker) -> None:
        """Test self-broadcast succeeds."""
        txid = await taker._session._broadcast_self()
        assert txid == "txid123"
        taker.backend.broadcast_transaction.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_self_failure(self, taker) -> None:
        """Test self-broadcast failure returns empty string."""
        taker.backend.broadcast_transaction = AsyncMock(side_effect=Exception("Network error"))
        txid = await taker._session._broadcast_self()
        assert txid == ""

    @pytest.mark.asyncio
    async def test_phase_broadcast_self_policy(self, taker) -> None:
        """Test broadcast with SELF policy uses self-broadcast."""
        taker.config.tx_broadcast = BroadcastPolicy.SELF
        taker._session.maker_sessions = {}

        txid = await taker._session._phase_broadcast()
        assert txid == "txid123"
        taker.backend.broadcast_transaction.assert_called_once()

    @pytest.mark.asyncio
    async def test_phase_broadcast_random_peer_fallback_to_self(self, taker) -> None:
        """Test RANDOM_PEER policy falls back to self if no makers."""
        taker.config.tx_broadcast = BroadcastPolicy.RANDOM_PEER
        taker._session.maker_sessions = {}

        # With no makers, should fall back to self
        with patch("random.shuffle", side_effect=lambda x: x):
            txid = await taker._session._phase_broadcast()

        assert txid == "txid123"

    @pytest.mark.asyncio
    async def test_phase_broadcast_not_self_fails_without_makers(self, taker) -> None:
        """Test NOT_SELF policy fails if no makers available."""
        taker.config.tx_broadcast = BroadcastPolicy.NOT_SELF
        taker._session.maker_sessions = {}

        txid = await taker._session._phase_broadcast()
        assert txid == ""

    @pytest.mark.asyncio
    async def test_broadcast_via_maker_sends_push(self, taker) -> None:
        """Test broadcast via maker sends !push message."""
        from jmcore.models import Offer, OfferType

        from taker.taker import MakerSession

        # Set up maker session
        mock_offer = Offer(
            counterparty="J5maker123",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=1000,
            cjfee="0.0003",
            fidelity_bond_value=0,
        )
        taker._session.maker_sessions = {
            "J5maker123": MakerSession(nick="J5maker123", offer=mock_offer)
        }

        # Mock directory client
        taker.directory_client = MagicMock()
        taker.directory_client.send_privmsg = AsyncMock()

        # Test the push message format
        tx_b64 = base64.b64encode(taker._session.final_tx).decode("ascii")
        await taker._session._broadcast_via_maker("J5maker123", tx_b64)

        # Verify push was sent (without ! prefix - the prefix is only for message routing)
        taker.directory_client.send_privmsg.assert_called_once_with(
            "J5maker123", "push", tx_b64, log_routing=True, force_channel=""
        )

    @pytest.mark.asyncio
    async def test_broadcast_via_maker_detects_success(self, taker) -> None:
        """Test broadcast via maker detects transaction in mempool."""
        from jmcore.models import Offer, OfferType

        from taker.taker import MakerSession

        # Set up maker session
        mock_offer = Offer(
            counterparty="J5maker123",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=1000,
            cjfee="0.0003",
            fidelity_bond_value=0,
        )
        taker._session.maker_sessions = {
            "J5maker123": MakerSession(nick="J5maker123", offer=mock_offer)
        }

        # Mock directory client
        taker.directory_client = MagicMock()
        taker.directory_client.send_privmsg = AsyncMock()

        # Set up tx_metadata with taker's CJ and change outputs (required for verification)
        taker._session.tx_metadata = {
            "output_owners": [("taker", "cj"), ("J5maker123", "cj"), ("taker", "change")]
        }
        taker._session.cj_destination = "bcrt1qtest123"
        taker._session.taker_change_address = "bcrt1qchange456"

        # Mock backend to return verification success for both outputs
        taker.backend.verify_tx_output = AsyncMock(return_value=True)

        tx_b64 = base64.b64encode(taker._session.final_tx).decode("ascii")
        txid = await taker._session._broadcast_via_maker("J5maker123", tx_b64)

        # Should detect the transaction
        assert txid != ""
        # Should verify both CJ and change outputs
        assert taker.backend.verify_tx_output.call_count >= 2

    @pytest.mark.asyncio
    async def test_phase_broadcast_sends_to_all_makers_without_mempool_access(self, taker) -> None:
        """Without mempool access all non-SELF policies broadcast to ALL makers.

        Regression test for issue #482: the fix must send !push to every
        session maker simultaneously (not just one) and must never call
        verify_tx_output or fall back to _broadcast_self.
        """
        from jmcore.models import Offer, OfferType

        from taker.taker import MakerSession

        makers = ["J5maker1", "J5maker2", "J5maker3"]
        taker._session.maker_sessions = {}
        for nick in makers:
            offer = Offer(
                counterparty=nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=10_000_000,
                txfee=1000,
                cjfee="0.0003",
                fidelity_bond_value=0,
            )
            taker._session.maker_sessions[nick] = MakerSession(nick=nick, offer=offer)

        taker.directory_client = MagicMock()
        taker.directory_client.send_privmsg = AsyncMock()
        taker._session.tx_metadata = {"output_owners": [(n, "cj") for n in makers]}
        taker._session.cj_destination = "bcrt1qtest123"
        taker._session.taker_change_address = "bcrt1qchange456"

        taker.backend.has_mempool_access = MagicMock(return_value=False)
        taker.backend.verify_tx_output = AsyncMock(
            side_effect=AssertionError("verify_tx_output must not be called without mempool access")
        )
        taker.backend.broadcast_transaction = AsyncMock(
            side_effect=AssertionError("self-broadcast must not be called without mempool access")
        )
        taker.config.tx_broadcast = BroadcastPolicy.RANDOM_PEER

        txid = await taker._session._phase_broadcast()

        assert txid != ""
        # All 3 makers must be contacted.
        calls = taker.directory_client.send_privmsg.call_args_list
        assert len(calls) == 3
        push_recipients = {call[0][0] for call in calls}
        assert push_recipients == set(makers)
        taker.backend.verify_tx_output.assert_not_called()
        taker.backend.broadcast_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_phase_broadcast_random_peer_tries_makers(self, taker) -> None:
        """Test RANDOM_PEER policy tries makers, falls back to self on failure (full node)."""
        from jmcore.models import Offer, OfferType

        from taker.taker import MakerSession

        taker.config.tx_broadcast = BroadcastPolicy.RANDOM_PEER
        taker.backend.has_mempool_access = MagicMock(return_value=True)  # Full node

        # Set up maker sessions
        mock_offer = Offer(
            counterparty="J5maker123",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=1000,
            cjfee="0.0003",
            fidelity_bond_value=0,
        )
        taker._session.maker_sessions = {
            "J5maker123": MakerSession(nick="J5maker123", offer=mock_offer)
        }

        # Mock directory client
        taker.directory_client = MagicMock()
        taker.directory_client.send_privmsg = AsyncMock()

        # Set up tx_metadata so verification can find output index
        taker._session.tx_metadata = {"output_owners": [("taker", "cj"), ("J5maker123", "cj")]}
        taker._session.cj_destination = "bcrt1qtest123"

        # Make maker broadcast "fail" (verification returns False) so we fall back to self
        taker.backend.verify_tx_output = AsyncMock(return_value=False)

        # Force deterministic order: maker first
        with patch("random.shuffle", side_effect=lambda x: x.sort()):
            txid = await taker._session._phase_broadcast()

        # Should succeed via self fallback (full node behavior)
        assert txid == "txid123"

    @pytest.mark.asyncio
    async def test_phase_broadcast_not_self_logs_tx_on_failure(self, taker, caplog) -> None:
        """Test NOT_SELF policy logs transaction hex on failure for manual broadcast."""
        from jmcore.models import Offer, OfferType

        from taker.taker import MakerSession

        taker.config.tx_broadcast = BroadcastPolicy.NOT_SELF

        # Set up maker session
        mock_offer = Offer(
            counterparty="J5maker123",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=1000,
            cjfee="0.0003",
            fidelity_bond_value=0,
        )
        taker._session.maker_sessions = {
            "J5maker123": MakerSession(nick="J5maker123", offer=mock_offer)
        }

        # Mock directory client
        taker.directory_client = MagicMock()
        taker.directory_client.send_privmsg = AsyncMock()

        # Set up tx_metadata so verification can find output index
        taker._session.tx_metadata = {"output_owners": [("taker", "cj"), ("J5maker123", "cj")]}
        taker._session.cj_destination = "bcrt1qtest123"

        # Make broadcast fail (verification returns False)
        taker.backend.verify_tx_output = AsyncMock(return_value=False)

        txid = await taker._session._phase_broadcast()

        # Should fail
        assert txid == ""


class TestNeutrinoBroadcast:
    """Tests for Neutrino-specific broadcast behavior (no mempool access)."""

    @pytest.fixture
    def mock_wallet(self):
        """Create a mock wallet service."""
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.network = "regtest"
        wallet.sync_all = AsyncMock()
        wallet.close = AsyncMock()
        return wallet

    @pytest.fixture
    def mock_neutrino_backend(self):
        """Create a mock Neutrino backend (no mempool access)."""
        backend = MagicMock()
        backend.broadcast_transaction = AsyncMock(return_value="txid123")
        backend.get_transaction = AsyncMock(return_value=None)  # Neutrino can't fetch by txid
        backend.get_block_height = AsyncMock(return_value=850000)
        backend.verify_tx_output = AsyncMock(return_value=False)  # Can't verify unconfirmed
        backend.requires_neutrino_metadata = MagicMock(return_value=True)
        backend.has_mempool_access = MagicMock(return_value=False)  # Key difference
        return backend

    @pytest.fixture
    def mock_fullnode_backend(self):
        """Create a mock full node backend (has mempool access)."""
        backend = MagicMock()
        backend.broadcast_transaction = AsyncMock(return_value="txid123")
        backend.get_transaction = AsyncMock(return_value=None)
        backend.get_block_height = AsyncMock(return_value=850000)
        backend.verify_tx_output = AsyncMock(return_value=True)  # Can verify immediately
        backend.requires_neutrino_metadata = MagicMock(return_value=False)
        backend.has_mempool_access = MagicMock(return_value=True)
        return backend

    @pytest.fixture
    def taker_config(self, sample_mnemonic: str):
        """Create a taker config for testing."""
        return TakerConfig(
            mnemonic=sample_mnemonic,
            network="regtest",
            directory_servers=["localhost:5222"],
            tx_broadcast=BroadcastPolicy.RANDOM_PEER,
            broadcast_timeout_sec=5,  # Minimum allowed for tests
        )

    @pytest.fixture
    def neutrino_taker(self, mock_wallet, mock_neutrino_backend, taker_config):
        """Create a Taker instance with Neutrino backend."""
        from taker.taker import Taker

        taker = Taker(
            wallet=mock_wallet,
            backend=mock_neutrino_backend,
            config=taker_config,
        )
        taker._session.final_tx = bytes.fromhex(
            "02000000"
            "0001"
            "01"
            "0000000000000000000000000000000000000000000000000000000000000001"
            "00000000"
            "00"
            "ffffffff"
            "01"
            "0000000000000000"
            "160014"
            "0000000000000000000000000000000000000000"
            "00"
            "00000000"
        )
        return taker

    @pytest.fixture
    def fullnode_taker(self, mock_wallet, mock_fullnode_backend, taker_config):
        """Create a Taker instance with full node backend."""
        from taker.taker import Taker

        taker = Taker(
            wallet=mock_wallet,
            backend=mock_fullnode_backend,
            config=taker_config,
        )
        taker._session.final_tx = bytes.fromhex(
            "02000000"
            "0001"
            "01"
            "0000000000000000000000000000000000000000000000000000000000000001"
            "00000000"
            "00"
            "ffffffff"
            "01"
            "0000000000000000"
            "160014"
            "0000000000000000000000000000000000000000"
            "00"
            "00000000"
        )
        return taker

    def _setup_makers(self, taker, maker_nicks: list[str]) -> None:
        """Helper to set up maker sessions for a taker."""
        from jmcore.models import Offer, OfferType

        from taker.taker import MakerSession

        taker._session.maker_sessions = {}
        for nick in maker_nicks:
            mock_offer = Offer(
                counterparty=nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=10_000_000,
                txfee=1000,
                cjfee="0.0003",
                fidelity_bond_value=0,
            )
            taker._session.maker_sessions[nick] = MakerSession(nick=nick, offer=mock_offer)

        taker.directory_client = MagicMock()
        taker.directory_client.send_privmsg = AsyncMock()
        taker._session.tx_metadata = {"output_owners": [(nick, "cj") for nick in maker_nicks]}
        taker._session.cj_destination = "bcrt1qtest123"
        taker._session.taker_change_address = "bcrt1qchange456"

    @pytest.mark.asyncio
    async def test_multiple_peers_broadcasts_to_n_makers(self, neutrino_taker) -> None:
        """MULTIPLE_PEERS on Neutrino broadcasts to ALL makers, ignoring peer_count cap.

        Without mempool access the no-mempool early-exit path fires before the
        MULTIPLE_PEERS branch. It always sends to every session maker for
        maximum reliability (issue #482), so ``broadcast_peer_count`` has no
        effect here.
        """
        neutrino_taker.config.tx_broadcast = BroadcastPolicy.MULTIPLE_PEERS
        neutrino_taker.config.broadcast_peer_count = 3
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2", "J5maker3", "J5maker4"])

        txid = await neutrino_taker._session._phase_broadcast()

        assert txid != ""

        # All 4 makers are contacted, not just 3.
        calls = neutrino_taker.directory_client.send_privmsg.call_args_list
        assert len(calls) == 4
        push_recipients = {call[0][0] for call in calls}
        assert push_recipients == {"J5maker1", "J5maker2", "J5maker3", "J5maker4"}

    @pytest.mark.asyncio
    async def test_random_peer_falls_back_to_self(self, neutrino_taker) -> None:
        """RANDOM_PEER on Neutrino broadcasts to ALL makers, never falls back to self.

        Without mempool access, sending to one random maker and "trusting" it
        is fragile: that maker might be offline. The correct strategy is to
        send !push to every session maker simultaneously so at least one
        relays the transaction. Self-broadcast must not be attempted (issue #482).
        """
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2"])

        neutrino_taker.backend.verify_tx_output = AsyncMock(return_value=False)
        neutrino_taker.backend.broadcast_transaction = AsyncMock(return_value="selfbroadcast_txid")

        txid = await neutrino_taker._session._phase_broadcast()

        # Returns expected txid (not self-broadcast txid).
        assert txid != ""
        assert txid != "selfbroadcast_txid"

        # Both makers must be contacted, not just one.
        privmsg_calls = neutrino_taker.directory_client.send_privmsg.call_args_list
        assert len(privmsg_calls) == 2
        push_recipients = {call[0][0] for call in privmsg_calls}
        assert push_recipients == {"J5maker1", "J5maker2"}

        # No self-broadcast, no mempool verification.
        neutrino_taker.backend.broadcast_transaction.assert_not_called()
        neutrino_taker.backend.verify_tx_output.assert_not_called()

    @pytest.mark.asyncio
    async def test_fullnode_multiple_peers_same_behavior(self, fullnode_taker) -> None:
        """Test full node MULTIPLE_PEERS works the same as Neutrino."""
        fullnode_taker.config.tx_broadcast = BroadcastPolicy.MULTIPLE_PEERS
        fullnode_taker.config.broadcast_peer_count = 2
        self._setup_makers(fullnode_taker, ["J5maker1", "J5maker2", "J5maker3"])

        txid = await fullnode_taker._session._phase_broadcast()

        # Should succeed
        assert txid != ""

        # Should send to exactly 2 makers
        calls = fullnode_taker.directory_client.send_privmsg.call_args_list
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_not_self_never_falls_back(self, neutrino_taker) -> None:
        """NOT_SELF on Neutrino: broadcast to ALL makers, never self (issue #482).

        Same as RANDOM_PEER: without mempool access we broadcast to all
        session makers simultaneously. No self-broadcast, ever.
        """
        neutrino_taker.config.tx_broadcast = BroadcastPolicy.NOT_SELF
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2"])

        neutrino_taker.backend.verify_tx_output = AsyncMock(return_value=False)
        neutrino_taker.backend.broadcast_transaction = AsyncMock(return_value="selfbroadcast_txid")

        txid = await neutrino_taker._session._phase_broadcast()

        # Non-empty txid, both makers contacted, no self broadcast.
        assert txid != ""
        assert txid != "selfbroadcast_txid"
        calls = neutrino_taker.directory_client.send_privmsg.call_args_list
        assert len(calls) == 2
        push_recipients = {call[0][0] for call in calls}
        assert push_recipients == {"J5maker1", "J5maker2"}
        neutrino_taker.backend.broadcast_transaction.assert_not_called()
        neutrino_taker.backend.verify_tx_output.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_peers_falls_back_to_self(self, neutrino_taker) -> None:
        """Test MULTIPLE_PEERS falls back to self if all N peers fail."""
        neutrino_taker.config.tx_broadcast = BroadcastPolicy.MULTIPLE_PEERS
        neutrino_taker.config.broadcast_peer_count = 2
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2"])

        # Simulate all makers failing to receive !push
        neutrino_taker.directory_client.send_privmsg = AsyncMock(
            side_effect=Exception("Connection lost")
        )

        # Mock self-broadcast to succeed
        neutrino_taker.backend.broadcast_transaction = AsyncMock(return_value="selfbroadcast_txid")

        txid = await neutrino_taker._session._phase_broadcast()

        # Should fall back to self and succeed
        assert txid == "selfbroadcast_txid"

        # Should have attempted to send to 2 makers
        assert neutrino_taker.directory_client.send_privmsg.call_count == 2

    @pytest.mark.asyncio
    async def test_fullnode_random_peer_sequential(self, fullnode_taker) -> None:
        """Test full node RANDOM_PEER tries candidates sequentially with verification."""
        self._setup_makers(fullnode_taker, ["J5maker1", "J5maker2"])

        # Add taker's CJ output to metadata so verification can find it
        fullnode_taker._session.tx_metadata["output_owners"].insert(0, ("taker", "cj"))

        # Force deterministic order: maker1 first
        with patch("random.shuffle", side_effect=lambda x: x.sort()):
            txid = await fullnode_taker._session._phase_broadcast()

        # Should succeed via first maker (verification returns True)
        assert txid != ""

        # Should only try first maker (verification succeeded)
        # Full node doesn't use multi-maker broadcast
        calls = fullnode_taker.directory_client.send_privmsg.call_args_list
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_fullnode_random_peer_tries_next_on_failure(self, fullnode_taker) -> None:
        """Test full node RANDOM_PEER tries next candidate when verification fails."""
        self._setup_makers(fullnode_taker, ["J5maker1", "J5maker2"])

        # First verification fails, second succeeds
        fullnode_taker.backend.verify_tx_output = AsyncMock(side_effect=[False, False, True, True])

        with patch("random.shuffle", side_effect=lambda x: x.sort()):
            txid = await fullnode_taker._session._phase_broadcast()

        # Should succeed
        assert txid != ""

    @pytest.mark.asyncio
    async def test_neutrino_with_tracker_random_peer_uses_sequential_path(
        self, neutrino_taker
    ) -> None:
        """RANDOM_PEER on Neutrino with the watched-mempool tracker takes the
        sequential single-maker path (no fan-out).

        When the neutrino-api fork advertises ``mempool_enabled: true`` the
        backend reports ``has_mempool_access() is True``. The no-mempool
        early-exit in ``_phase_broadcast`` must NOT trigger; instead the
        normal RANDOM_PEER loop tries one maker at a time and verifies the
        broadcast via the (now mempool-aware) backend. This is the
        behavioral change documented in docs/technical/wallet.md.
        """
        neutrino_taker.config.tx_broadcast = BroadcastPolicy.RANDOM_PEER
        # Tracker enabled: backend now has mempool access.
        neutrino_taker.backend.has_mempool_access = MagicMock(return_value=True)
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2", "J5maker3"])
        neutrino_taker._session.tx_metadata["output_owners"].insert(0, ("taker", "cj"))

        # First maker's verification succeeds, so loop exits early.
        neutrino_taker.backend.verify_tx_output = AsyncMock(return_value=True)

        with patch("random.shuffle", side_effect=lambda x: x.sort()):
            txid = await neutrino_taker._session._phase_broadcast()

        assert txid != ""
        # Exactly one maker contacted (sequential, not fan-out).
        calls = neutrino_taker.directory_client.send_privmsg.call_args_list
        assert len(calls) == 1
        assert calls[0][0][0] == "J5maker1"
        # Mempool verification was performed (proving we took the sequential path).
        assert neutrino_taker.backend.verify_tx_output.await_count >= 1

    @pytest.mark.asyncio
    async def test_neutrino_with_tracker_multiple_peers_respects_peer_count_cap(
        self, neutrino_taker
    ) -> None:
        """MULTIPLE_PEERS on Neutrino with the tracker respects ``broadcast_peer_count``.

        With mempool access, ``_phase_broadcast`` falls through to the
        MULTIPLE_PEERS branch which caps recipients at
        ``broadcast_peer_count`` instead of fanning out to every maker.
        """
        neutrino_taker.config.tx_broadcast = BroadcastPolicy.MULTIPLE_PEERS
        neutrino_taker.config.broadcast_peer_count = 2
        neutrino_taker.backend.has_mempool_access = MagicMock(return_value=True)
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2", "J5maker3", "J5maker4"])

        txid = await neutrino_taker._session._phase_broadcast()

        assert txid != ""
        # Exactly 2 (broadcast_peer_count) makers contacted, not all 4.
        calls = neutrino_taker.directory_client.send_privmsg.call_args_list
        assert len(calls) == 2
        # Self-broadcast must not happen (peers succeeded).
        neutrino_taker.backend.broadcast_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_neutrino_operator_optout_fans_out_to_all_makers(self, neutrino_taker) -> None:
        """Operator opt-out (``neutrino_include_mempool = false``) still fans out.

        Even if the upstream server supports the mempool tracker, an
        operator who sets ``neutrino_include_mempool = false`` must see the
        same behavior as the legacy-server case:
        ``has_mempool_access() is False`` and ``_phase_broadcast`` sends
        !push to every session maker simultaneously. No self-broadcast,
        no mempool verification.
        """
        # Backend reports no mempool access (operator opt-out path).
        neutrino_taker.backend.has_mempool_access = MagicMock(return_value=False)
        neutrino_taker.backend.verify_tx_output = AsyncMock(
            side_effect=AssertionError("verify_tx_output must not be called when operator opts out")
        )
        neutrino_taker.backend.broadcast_transaction = AsyncMock(
            side_effect=AssertionError("self-broadcast must not be called when operator opts out")
        )
        neutrino_taker.config.tx_broadcast = BroadcastPolicy.MULTIPLE_PEERS
        neutrino_taker.config.broadcast_peer_count = 2
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2", "J5maker3", "J5maker4"])

        txid = await neutrino_taker._session._phase_broadcast()

        assert txid != ""
        # All 4 makers contacted despite peer_count=2 (no-mempool fan-out wins).
        calls = neutrino_taker.directory_client.send_privmsg.call_args_list
        assert len(calls) == 4
        push_recipients = {call[0][0] for call in calls}
        assert push_recipients == {"J5maker1", "J5maker2", "J5maker3", "J5maker4"}
        neutrino_taker.backend.verify_tx_output.assert_not_called()
        neutrino_taker.backend.broadcast_transaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_broadcast_to_all_makers_partial_success(self, neutrino_taker) -> None:
        """Test multi-maker broadcast succeeds even if some makers fail."""
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2", "J5maker3"])

        # Simulate first maker failing, others succeed
        call_count = [0]

        async def flaky_send(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Network error")

        neutrino_taker.directory_client.send_privmsg = AsyncMock(side_effect=flaky_send)

        success_count = await neutrino_taker._session._broadcast_to_all_makers(
            ["J5maker1", "J5maker2", "J5maker3"],
            "dHh0ZXN0",  # base64 test data
        )

        # Should report 2 successes
        assert success_count == 2

    @pytest.mark.asyncio
    async def test_broadcast_to_all_makers_all_fail(self, neutrino_taker) -> None:
        """Test multi-maker broadcast reports zero on total failure."""
        self._setup_makers(neutrino_taker, ["J5maker1", "J5maker2"])

        neutrino_taker.directory_client.send_privmsg = AsyncMock(
            side_effect=Exception("All connections failed")
        )

        success_count = await neutrino_taker._session._broadcast_to_all_makers(
            ["J5maker1", "J5maker2"],
            "dHh0ZXN0",
        )

        assert success_count == 0


class TestHasMempoolAccess:
    """Tests for the has_mempool_access() backend method."""

    def test_bitcoin_core_has_mempool(self) -> None:
        """Test DescriptorWalletBackend has mempool access."""
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        backend = DescriptorWalletBackend(
            rpc_url="http://localhost:18443",
            rpc_user="test",
            rpc_password="test",
        )
        assert backend.has_mempool_access() is True

    def test_neutrino_no_mempool_until_capability_detected(self) -> None:
        """Without server capability detection, NeutrinoBackend reports no mempool.

        ``has_mempool_access`` requires both the operator-side opt-in
        (``include_mempool=True``, the default) and the server-side
        capability flag (``mempool_enabled: true`` on ``/v1/status``).
        Until ``_detect_capabilities`` runs against a live server, the
        capability remains false so callers conservatively assume no
        mempool overlay.
        """
        from jmwallet.backends.neutrino import NeutrinoBackend

        backend = NeutrinoBackend(neutrino_url="http://localhost:8080", network="regtest")
        assert backend.include_mempool is True  # default
        assert backend._server_capabilities.has_mempool_tracker is False
        assert backend.has_mempool_access() is False

    def test_neutrino_mempool_enabled_when_server_supports_it(self) -> None:
        """When the server advertises the tracker, mempool access is on."""
        from jmwallet.backends.neutrino import NeutrinoBackend

        backend = NeutrinoBackend(neutrino_url="http://localhost:8080", network="regtest")
        backend._server_capabilities.has_mempool_tracker = True
        assert backend.has_mempool_access() is True

    def test_neutrino_mempool_disabled_overrides_server_capability(self) -> None:
        """Operator opt-out (``include_mempool=False``) wins over the server."""
        from jmwallet.backends.neutrino import NeutrinoBackend

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8080",
            network="regtest",
            include_mempool=False,
        )
        backend._server_capabilities.has_mempool_tracker = True
        assert backend.has_mempool_access() is False
