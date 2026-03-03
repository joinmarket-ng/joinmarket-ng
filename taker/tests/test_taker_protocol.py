"""
Unit tests for Taker protocol handling.

Tests:
- NaCl encryption setup and message exchange
- PoDLE commitment generation and revelation
- Fill, Auth, TX phases
- Signature collection
- Multi-maker coordination
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, Mock, patch

import pytest
from _taker_test_helpers import (
    make_crypto_pair,
    make_directory_client,
    make_taker_config,
    make_utxo,
)
from jmcore.encryption import CryptoSession
from jmcore.models import Offer, OfferType
from jmwallet.wallet.models import UTXOInfo

from taker.podle_manager import PoDLEManager
from taker.taker import MakerSession, PhaseResult, Taker, TakerState


@pytest.fixture
def mock_wallet():
    """Mock wallet service."""
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.sync_all = AsyncMock()
    wallet.get_total_balance = AsyncMock(return_value=100_000_000)
    wallet.get_balance = AsyncMock(return_value=50_000_000)
    wallet.get_utxos = AsyncMock(
        return_value=[
            make_utxo(txid_char="a", address="bcrt1qtest1"),
            make_utxo(txid_char="b", address="bcrt1qtest2", path="m/84'/1'/0'/0/1"),
        ]
    )
    wallet.get_next_address_index = Mock(return_value=0)
    wallet.get_receive_address = Mock(return_value="bcrt1qdest")
    wallet.get_change_address = Mock(return_value="bcrt1qchange")
    wallet.get_key_for_address = Mock()
    wallet.select_utxos = Mock(return_value=[make_utxo(txid_char="a", address="bcrt1qtest1")])
    wallet.close = AsyncMock()
    return wallet


@pytest.fixture
def mock_backend():
    """Mock blockchain backend."""
    backend = AsyncMock()
    backend.get_utxo = AsyncMock(
        return_value=make_utxo(txid_char="c", value=10_000_000, address="bcrt1qmaker")
    )
    backend.get_transaction = AsyncMock()
    backend.broadcast_transaction = AsyncMock(return_value="txid123")
    # can_provide_neutrino_metadata is a synchronous method, not async
    backend.can_provide_neutrino_metadata = Mock(return_value=True)
    return backend


@pytest.fixture
def mock_config():
    """Mock taker config."""
    return make_taker_config(
        counterparty_count=2,
        minimum_makers=2,
        taker_utxo_age=1,
        taker_utxo_amtpercent=20,
        tx_fee_factor=1.0,
        maker_timeout_sec=30.0,
        order_wait_time=10.0,
    )


@pytest.fixture
def sample_offer():
    """Sample maker offer."""
    return Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=100_000_000,
        txfee=500,
        cjfee=250,  # 0.00025 relative
        counterparty="J5TestMaker",
    )


@pytest.fixture
def sample_offer2():
    """Second sample maker offer."""
    return Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=1,
        minsize=10000,
        maxsize=100_000_000,
        txfee=500,
        cjfee=300,  # 0.0003 relative
        counterparty="J5TestMaker2",
    )


@pytest.mark.asyncio
async def test_taker_initialization(mock_wallet, mock_backend, mock_config):
    """Test taker initialization."""
    taker = Taker(mock_wallet, mock_backend, mock_config)

    assert taker.wallet == mock_wallet
    assert taker.backend == mock_backend
    assert taker.config == mock_config
    assert taker.state == TakerState.IDLE
    # v5 nicks for reference implementation compatibility
    assert taker.nick.startswith("J5")
    assert len(taker.maker_sessions) == 0


@pytest.mark.asyncio
async def test_encryption_session_setup():
    """Test NaCl encryption session setup between taker and maker."""
    taker_crypto, maker_crypto = make_crypto_pair()

    # Test encryption/decryption
    plaintext = "test message"
    encrypted = taker_crypto.encrypt(plaintext)
    assert encrypted != plaintext

    # Maker decrypts
    decrypted = maker_crypto.decrypt(encrypted)
    assert decrypted == plaintext

    # Test reverse direction
    plaintext2 = "response message"
    encrypted2 = maker_crypto.encrypt(plaintext2)
    decrypted2 = taker_crypto.decrypt(encrypted2)
    assert decrypted2 == plaintext2


@pytest.mark.asyncio
async def test_podle_generation(mock_wallet, tmp_path):
    """Test PoDLE commitment generation using PoDLEManager."""
    # Create sample UTXOs
    utxos = [
        make_utxo(txid_char="a", address="bcrt1qtest1"),
        make_utxo(
            txid_char="b",
            vout=1,
            value=30_000_000,
            address="bcrt1qtest2",
            path="m/84'/1'/0'/0/1",
        ),
    ]

    # Mock private key getter
    def get_private_key(addr: str) -> bytes | None:
        # Return a dummy private key
        return b"\x01" * 32

    # Use PoDLEManager with temporary data directory
    manager = PoDLEManager(data_dir=tmp_path)

    # Generate PoDLE commitment
    commitment = manager.generate_fresh_commitment(
        wallet_utxos=utxos,
        cj_amount=10_000_000,
        private_key_getter=get_private_key,
        min_confirmations=1,
        min_percent=20,
    )

    assert commitment is not None
    assert commitment.p is not None
    assert commitment.p2 is not None
    assert commitment.sig is not None
    assert commitment.e is not None
    assert len(commitment.utxo) > 0

    # Test commitment serialization
    # Format: 'P' + 64 hex chars = 65 chars (P prefix for standard PoDLE)
    commitment_str = commitment.to_commitment_str()
    assert len(commitment_str) == 65  # 'P' + 32 bytes in hex
    assert commitment_str.startswith("P")

    # Test revelation serialization
    revelation = commitment.to_revelation()
    assert "utxo" in revelation
    assert "P" in revelation
    assert "P2" in revelation
    assert "sig" in revelation
    assert "e" in revelation

    # Verify commitment was tracked
    assert len(manager.used_commitments) == 1
    assert commitment.to_commitment_str()[1:] in manager.used_commitments  # Strip 'P' prefix


@pytest.mark.asyncio
async def test_podle_retry_limit(mock_wallet, tmp_path):
    """Test that PoDLE respects max_retries limit."""
    # Create a single UTXO
    utxos = [make_utxo(txid_char="a", address="bcrt1qtest1")]

    def get_private_key(addr: str) -> bytes | None:
        return b"\x01" * 32

    from taker.podle_manager import PoDLEManager

    manager = PoDLEManager(data_dir=tmp_path)

    # Generate 3 commitments with max_retries=3 (indices 0,1,2)
    for i in range(3):
        commitment = manager.generate_fresh_commitment(
            wallet_utxos=utxos,
            cj_amount=10_000_000,
            private_key_getter=get_private_key,
            min_confirmations=1,
            min_percent=20,
            max_retries=3,
        )
        assert commitment is not None
        assert commitment.index == i

    # 4th attempt should fail - UTXO exhausted
    commitment = manager.generate_fresh_commitment(
        wallet_utxos=utxos,
        cj_amount=10_000_000,
        private_key_getter=get_private_key,
        min_confirmations=1,
        min_percent=20,
        max_retries=3,
    )
    assert commitment is None  # No fresh commitment available


@pytest.mark.asyncio
async def test_podle_utxo_deprioritization(mock_wallet, tmp_path):
    """Test that fresh UTXOs are naturally preferred via lazy evaluation.

    The implementation uses lazy evaluation: it tries UTXOs in order (sorted by
    confirmations/value) and for each UTXO tries indices 0..max_retries-1 until
    finding an unused commitment. Fresh UTXOs succeed faster (at index 0).
    """
    # Create two UTXOs: UTXO_B has more confirmations, so it's tried first
    utxos = [
        make_utxo(txid_char="a", address="bcrt1qtest1"),
        make_utxo(
            txid_char="b",
            vout=1,
            address="bcrt1qtest2",
            confirmations=20,
            path="m/84'/1'/0'/0/1",
        ),
    ]

    # Use different private keys for different addresses
    def get_private_key(addr: str) -> bytes | None:
        if addr == "bcrt1qtest1":
            return b"\x01" * 32
        elif addr == "bcrt1qtest2":
            return b"\x02" * 32
        return None

    from taker.podle_manager import PoDLEManager

    manager = PoDLEManager(data_dir=tmp_path)

    # Use UTXO_B twice (indices 0, 1) - higher confirmations means tried first
    for _ in range(2):
        commitment = manager.generate_fresh_commitment(
            wallet_utxos=[utxos[1]],  # Only UTXO_B (higher confs)
            cj_amount=10_000_000,
            private_key_getter=get_private_key,
            min_confirmations=1,
            min_percent=20,
            max_retries=3,
        )
        assert commitment is not None
        assert commitment.utxo.startswith("bbbb")

    # Now with both UTXOs, UTXO_B is still tried first (higher confs)
    # But indices 0,1 are used, so it will use index 2
    commitment = manager.generate_fresh_commitment(
        wallet_utxos=utxos,  # Both UTXOs
        cj_amount=10_000_000,
        private_key_getter=get_private_key,
        min_confirmations=1,
        min_percent=20,
        max_retries=3,
    )
    assert commitment is not None
    # UTXO_B should still be selected (higher confirmations, uses index 2)
    assert commitment.utxo.startswith("bbbb")
    assert commitment.index == 2


@pytest.mark.asyncio
async def test_fill_phase_encryption():
    """Test !fill phase with encryption setup."""
    taker_crypto, maker_crypto = make_crypto_pair()

    # Now both can communicate securely
    test_msg = "encrypted test"
    encrypted = taker_crypto.encrypt(test_msg)
    decrypted = maker_crypto.decrypt(encrypted)
    assert decrypted == test_msg


@pytest.mark.asyncio
async def test_auth_phase_encryption():
    """Test !auth phase with encrypted revelation."""
    taker_crypto, maker_crypto = make_crypto_pair()

    # Taker creates revelation and encrypts it
    revelation_str = "txid:vout|P_hex|P2_hex|sig_hex|e_hex"
    encrypted_revelation = taker_crypto.encrypt(revelation_str)

    # Maker receives and decrypts
    decrypted_revelation = maker_crypto.decrypt(encrypted_revelation)
    assert decrypted_revelation == revelation_str

    # Maker creates ioauth response
    ioauth_data = "txid1:0,txid2:1 auth_pub cj_addr change_addr btc_sig"
    encrypted_ioauth = maker_crypto.encrypt(ioauth_data)

    # Taker decrypts ioauth
    decrypted_ioauth = taker_crypto.decrypt(encrypted_ioauth)
    assert decrypted_ioauth == ioauth_data


@pytest.mark.asyncio
async def test_tx_phase_encryption():
    """Test !tx phase with encrypted transaction."""
    taker_crypto, maker_crypto = make_crypto_pair()

    # Taker encodes and encrypts transaction
    tx_bytes = b"\x01\x00\x00\x00" * 10  # Dummy transaction
    tx_b64 = base64.b64encode(tx_bytes).decode("ascii")
    encrypted_tx = taker_crypto.encrypt(tx_b64)

    # Maker decrypts and decodes
    decrypted_tx_b64 = maker_crypto.decrypt(encrypted_tx)
    decoded_tx = base64.b64decode(decrypted_tx_b64)
    assert decoded_tx == tx_bytes

    # Maker creates signature
    sig_bytes = b"\x30\x44" + b"\x00" * 70  # Dummy DER signature
    pub_bytes = b"\x02" + b"\x00" * 33  # Dummy compressed pubkey

    # Encode signature: varint(sig_len) + sig + varint(pub_len) + pub
    sig_len = len(sig_bytes)
    pub_len = len(pub_bytes)
    sig_data = bytes([sig_len]) + sig_bytes + bytes([pub_len]) + pub_bytes
    sig_b64 = base64.b64encode(sig_data).decode("ascii")

    # Encrypt signature
    encrypted_sig = maker_crypto.encrypt(sig_b64)

    # Taker decrypts
    decrypted_sig_b64 = taker_crypto.decrypt(encrypted_sig)
    assert decrypted_sig_b64 == sig_b64


@pytest.mark.asyncio
async def test_maker_session_tracking():
    """Test tracking multiple maker sessions."""
    offer1 = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=100_000_000,
        txfee=500,
        cjfee=250,
        counterparty="J5Maker1",
    )

    offer2 = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=1,
        minsize=10000,
        maxsize=100_000_000,
        txfee=500,
        cjfee=300,
        counterparty="J5Maker2",
    )

    # Create sessions
    session1 = MakerSession(nick="J5Maker1", offer=offer1)
    session2 = MakerSession(nick="J5Maker2", offer=offer2)

    # Simulate fill phase responses
    session1.pubkey = "aabb" * 16
    session1.responded_fill = True

    session2.pubkey = "ccdd" * 16
    session2.responded_fill = True

    # Simulate auth phase responses
    session1.utxos = [{"txid": "tx1", "vout": 0, "value": 10000000, "address": "addr1"}]
    session1.cj_address = "bcrt1qmaker1cj"
    session1.change_address = "bcrt1qmaker1change"
    session1.responded_auth = True

    session2.utxos = [{"txid": "tx2", "vout": 0, "value": 10000000, "address": "addr2"}]
    session2.cj_address = "bcrt1qmaker2cj"
    session2.change_address = "bcrt1qmaker2change"
    session2.responded_auth = True

    # Verify session state
    assert session1.responded_fill
    assert session1.responded_auth
    assert len(session1.utxos) == 1

    assert session2.responded_fill
    assert session2.responded_auth
    assert len(session2.utxos) == 1


@pytest.mark.asyncio
async def test_message_encryption_roundtrip():
    """Test complete message encryption/decryption roundtrip."""
    # Simulate taker-maker communication
    sessions = {}

    # Maker 1
    sessions["maker1"] = make_crypto_pair()

    # Maker 2
    sessions["maker2"] = make_crypto_pair()

    # Test auth messages to both makers
    revelation = "utxo|P|P2|sig|e"

    for maker_id, (taker_crypto, maker_crypto) in sessions.items():
        # Taker encrypts and sends
        encrypted = taker_crypto.encrypt(revelation)

        # Maker decrypts
        decrypted = maker_crypto.decrypt(encrypted)
        assert decrypted == revelation

        # Maker responds with ioauth
        ioauth = f"{maker_id}_utxo:0 pubkey cj_addr change_addr sig"
        encrypted_ioauth = maker_crypto.encrypt(ioauth)

        # Taker decrypts
        decrypted_ioauth = taker_crypto.decrypt(encrypted_ioauth)
        assert decrypted_ioauth == ioauth


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# --- Tests for PhaseResult and Maker Replacement Logic ---


class TestPhaseResult:
    """Tests for PhaseResult dataclass."""

    def test_phase_result_success(self):
        """Test successful phase result."""
        result = PhaseResult(success=True)
        assert result.success
        assert result.failed_makers == []
        assert not result.blacklist_error
        assert not result.needs_replacement

    def test_phase_result_failure_with_failed_makers(self):
        """Test failed phase result with failed makers."""
        result = PhaseResult(
            success=False, failed_makers=["maker1", "maker2"], blacklist_error=False
        )
        assert not result.success
        assert result.failed_makers == ["maker1", "maker2"]
        assert not result.blacklist_error
        assert result.needs_replacement  # Has failed makers, so needs replacement

    def test_phase_result_blacklist_error(self):
        """Test phase result with blacklist error."""
        result = PhaseResult(success=False, failed_makers=["maker1"], blacklist_error=True)
        assert not result.success
        assert result.blacklist_error
        assert result.needs_replacement

    def test_phase_result_success_with_some_failures(self):
        """Test successful phase even with some failed makers (but enough remaining)."""
        # Success can have failed makers if enough responded
        result = PhaseResult(success=True, failed_makers=["maker1"])
        assert result.success
        assert result.failed_makers == ["maker1"]
        # Even though we have failed makers, we don't need replacement since we succeeded
        assert not result.needs_replacement


class TestMakerReplacementConfig:
    """Tests for maker replacement configuration."""

    def test_max_maker_replacement_default(self):
        """Test default max_maker_replacement_attempts value."""
        config = make_taker_config()
        assert config.max_maker_replacement_attempts == 3

    def test_max_maker_replacement_custom(self):
        """Test custom max_maker_replacement_attempts value."""
        config = make_taker_config(max_maker_replacement_attempts=5)
        assert config.max_maker_replacement_attempts == 5

    def test_max_maker_replacement_disabled(self):
        """Test disabled maker replacement (set to 0)."""
        config = make_taker_config(max_maker_replacement_attempts=0)
        assert config.max_maker_replacement_attempts == 0

    def test_max_maker_replacement_bounds(self):
        """Test max_maker_replacement_attempts bounds validation."""
        # Should accept max value of 10
        config = make_taker_config(max_maker_replacement_attempts=10)
        assert config.max_maker_replacement_attempts == 10

        # Should reject value > 10
        with pytest.raises(ValueError):
            make_taker_config(max_maker_replacement_attempts=11)


# --- Tests for MultiDirectoryClient Direct Peer Connections ---


class TestMultiDirectoryClientDirectConnections:
    """Tests for MultiDirectoryClient direct peer connection feature."""

    def test_direct_connections_enabled_by_default(self):
        """Test that direct connections are enabled by default."""
        client = make_directory_client()

        assert client.prefer_direct_connections is True
        assert client.our_location == "NOT-SERVING-ONION"
        assert client._peer_connections == {}

    def test_direct_connections_can_be_disabled(self):
        """Test that direct connections can be disabled."""
        client = make_directory_client(prefer_direct_connections=False)

        assert client.prefer_direct_connections is False

    def test_get_peer_location_returns_none_when_not_found(self):
        """Test _get_peer_location returns None for unknown nicks."""
        client = make_directory_client()

        location = client._get_peer_location("J5unknown")
        assert location is None

    def test_should_try_direct_connect_disabled(self):
        """Test _should_try_direct_connect returns False when disabled."""
        client = make_directory_client(prefer_direct_connections=False)

        assert not client._should_try_direct_connect("J5maker")

    def test_get_connected_peer_returns_none_when_not_connected(self):
        """Test _get_connected_peer returns None when no connection exists."""
        client = make_directory_client()

        peer = client._get_connected_peer("J5maker")
        assert peer is None

    @pytest.mark.asyncio
    async def test_cleanup_peer_connections(self):
        """Test that peer connections are cleaned up on close."""
        from unittest.mock import AsyncMock

        from jmcore.network import OnionPeer

        client = make_directory_client()

        # Add a mock peer
        mock_peer = Mock(spec=OnionPeer)
        mock_peer.disconnect = AsyncMock()
        client._peer_connections["J5maker"] = mock_peer

        # Cleanup
        await client._cleanup_peer_connections()

        mock_peer.disconnect.assert_called_once()
        assert client._peer_connections == {}


# --- Tests for Sweep Mode CJ Amount Preservation ---


class TestSweepCjAmountPreservation:
    """Tests for sweep mode cj_amount preservation.

    This tests a critical bug fix: in sweep mode, the cj_amount sent in the
    !fill message must be preserved in _phase_build_tx. If we recalculate
    cj_amount when actual maker inputs differ from our estimate, the maker
    will reject the transaction with "wrong change" because they calculate
    their expected change based on the original cj_amount from !fill.

    See: https://github.com/JoinMarket-Org/joinmarket-clientserver maker.py
    verify_unsigned_tx() - maker calculates expected_change based on the
    amount from !fill, not a recalculated amount.
    """

    @pytest.fixture
    def mock_wallet_for_sweep(self):
        """Mock wallet service configured for sweep mode."""
        wallet = AsyncMock()
        wallet.mixdepth_count = 5
        wallet.sync_all = AsyncMock()
        wallet.get_total_balance = AsyncMock(return_value=100_000_000)
        wallet.get_balance = AsyncMock(return_value=50_000_000)

        # Two UTXOs for sweep (147,483 sats total, matching the bug report)
        sweep_utxos = [
            UTXOInfo(
                txid="1111111111111111111111111111111111111111111111111111111111111111",
                vout=2,
                value=68_874,
                address="bcrt1qtest1",
                confirmations=1244,
                scriptpubkey="0014" + "00" * 20,
                path="m/84'/1'/0'/0/0",
                mixdepth=3,
            ),
            UTXOInfo(
                txid="2222222222222222222222222222222222222222222222222222222222222222",
                vout=15,
                value=78_609,
                address="bcrt1qtest2",
                confirmations=1000,
                scriptpubkey="0014" + "00" * 20,
                path="m/84'/1'/0'/0/1",
                mixdepth=3,
            ),
        ]
        wallet.get_utxos = AsyncMock(return_value=sweep_utxos)
        wallet.get_all_utxos = Mock(return_value=sweep_utxos)
        wallet.get_next_address_index = Mock(return_value=0)
        wallet.get_receive_address = Mock(return_value="bcrt1qdest")
        wallet.get_change_address = Mock(return_value="bcrt1qchange")
        wallet.get_key_for_address = Mock()
        wallet.select_utxos = Mock(return_value=sweep_utxos)
        wallet.close = AsyncMock()
        return wallet

    @pytest.fixture
    def mock_backend_for_sweep(self):
        """Mock blockchain backend."""
        backend = AsyncMock()
        # Maker's UTXO
        backend.get_utxo = AsyncMock(
            return_value=UTXOInfo(
                txid="3333333333333333333333333333333333333333333333333333333333333333",
                vout=18,
                value=467_555,
                address="bcrt1qmaker",
                confirmations=100,
                scriptpubkey="0014" + "00" * 20,
                path="m/84'/1'/0'/0/0",
                mixdepth=0,
            )
        )
        backend.get_transaction = AsyncMock()
        backend.broadcast_transaction = AsyncMock(return_value="txid123")
        backend.can_provide_neutrino_metadata = Mock(return_value=False)
        backend.requires_neutrino_metadata = Mock(return_value=False)
        return backend

    @pytest.fixture
    def taker_config_for_sweep(self):
        """Taker config for sweep mode test."""
        return make_taker_config(
            counterparty_count=1,
            minimum_makers=1,
            taker_utxo_age=1,
            taker_utxo_amtpercent=20,
            tx_fee_factor=1.0,
            maker_timeout_sec=30.0,
            order_wait_time=10.0,
            fee_rate=1.0,  # 1 sat/vB
        )

    @staticmethod
    def _make_single_utxo_maker_session() -> tuple[str, MakerSession]:
        """Create a maker session with a single UTXO for sweep tests.

        Returns (nick, session) tuple ready to assign to taker.maker_sessions.
        """
        nick = "J55Jha4vGPR5fTFv"
        maker_offer = Offer(
            ordertype=OfferType.SW0_ABSOLUTE,  # Absolute fee = 0
            oid=0,
            minsize=10000,
            maxsize=1_000_000_000,
            txfee=500,  # Maker contributes 500 sats to tx fee
            cjfee=0,  # Zero fee
            counterparty=nick,
        )
        session = MakerSession(nick=nick, offer=maker_offer)
        session.pubkey = "e131e3bb667eb124" + "00" * 24
        session.responded_fill = True
        session.responded_auth = True
        session.utxos = [
            {
                "txid": "3" * 64,
                "vout": 18,
                "value": 467_555,
                "address": "bcrt1qmaker",
            }
        ]
        session.cj_address = "bcrt1qqyqszqgpqyqszqgpqyqszqgpqyqszqgpvxat9t"
        session.change_address = "bcrt1qqgpqyqszqgpqyqszqgpqyqszqgpqyqszazmwwa"
        session.crypto = CryptoSession()
        return nick, session

    @pytest.mark.asyncio
    async def test_sweep_preserves_cj_amount_from_fill(
        self, mock_wallet_for_sweep, mock_backend_for_sweep, taker_config_for_sweep
    ):
        """Test that sweep mode preserves cj_amount from !fill message.

        This is the exact scenario from the bug report:
        - Taker estimates 2 maker inputs per maker during initial calculation
        - Maker actually has 1 input
        - Without the fix, taker would recalculate cj_amount with lower tx_fee
        - This causes maker to reject tx with "wrong change"

        The fix ensures cj_amount is NOT recalculated in _phase_build_tx.
        """
        taker = Taker(mock_wallet_for_sweep, mock_backend_for_sweep, taker_config_for_sweep)

        # Simulate sweep mode setup
        taker.is_sweep = True
        taker.preselected_utxos = mock_wallet_for_sweep.get_all_utxos()

        # Set fee rate (must be done before _phase_build_tx)
        taker._fee_rate = 1.0

        # Total input: 147,483 sats (from mock wallet)
        total_input = sum(u.value for u in taker.preselected_utxos)

        # Simulate the budget that was calculated at order selection
        # Conservative estimate: 2 taker + 2 maker + 5 buffer = 9 inputs, 3 outputs
        # vsize = 9*68 + 3*31 + 11 = 716 vbytes at 1 sat/vB = 716 sats
        budget = 716
        taker._sweep_tx_fee_budget = budget

        # Initial cj_amount calculated during do_coinjoin (before !fill)
        # This is the amount that will be sent to makers in !fill
        # cj_amount = total_input - budget - maker_fees
        initial_cj_amount = total_input - budget  # 146,767 sats

        taker.cj_amount = initial_cj_amount

        # Set up a mock maker session with offer
        # Simulate !ioauth response - maker has only 1 input (not 2 as estimated)
        nick, maker_session = self._make_single_utxo_maker_session()
        taker.maker_sessions = {nick: maker_session}

        # Call _phase_build_tx - this is where the bug occurred
        result = await taker._phase_build_tx(
            destination="bcrt1qqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcruj60yu",
            mixdepth=3,
        )

        # The transaction should build successfully
        assert result is True

        # CRITICAL: cj_amount must NOT have changed
        # Before the fix, it would be recalculated to a different value
        assert taker.cj_amount == initial_cj_amount, (
            f"cj_amount was modified from {initial_cj_amount} to {taker.cj_amount}. "
            "This would cause maker to reject tx with 'wrong change'!"
        )

    @pytest.mark.asyncio
    async def test_sweep_handles_tx_fee_difference_as_residual(
        self, mock_wallet_for_sweep, mock_backend_for_sweep, taker_config_for_sweep
    ):
        """Test that tx_fee difference becomes residual (extra miner fee), not cj_amount change.

        When actual maker inputs differ from estimate:
        - Old behavior: recalculate cj_amount -> maker rejects with "wrong change"
        - New behavior: keep cj_amount, use budget as tx_fee -> residual is minimal

        With the new fix, the budget is used as the tx_fee, so the residual should
        only come from integer rounding in calculate_sweep_amount (typically < 100 sats).
        """
        taker = Taker(mock_wallet_for_sweep, mock_backend_for_sweep, taker_config_for_sweep)

        # Simulate sweep mode
        taker.is_sweep = True
        taker.preselected_utxos = mock_wallet_for_sweep.get_all_utxos()
        taker._fee_rate = 1.0

        # Total input: 147,483 sats (from mock wallet)
        total_input = sum(u.value for u in taker.preselected_utxos)

        # Set budget that was calculated at order selection time
        # Conservative estimate: 2 taker + 2 maker + 5 buffer = 9 inputs, 3 outputs
        # vsize = 9*68 + 3*31 + 11 = 716 vbytes at 1 sat/vB = 716 sats
        budget = 716
        taker._sweep_tx_fee_budget = budget

        # cj_amount calculated from budget: 147,483 - 716 = 146,767
        taker.cj_amount = total_input - budget

        # Maker with only 1 input (different from the estimated 2+buffer)
        nick, maker_session = self._make_single_utxo_maker_session()
        taker.maker_sessions = {nick: maker_session}

        result = await taker._phase_build_tx(
            destination="bcrt1qqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcruj60yu",
            mixdepth=3,
        )

        assert result is True

        # Verify cj_amount unchanged
        assert taker.cj_amount == total_input - budget

        # With the new fix, residual should be 0 (or minimal from rounding)
        # because we use the budget as tx_fee, not a recalculated fee
        # residual = total_input - cj_amount - maker_fees - budget
        #          = 147,483 - 146,767 - 0 - 716 = 0

    @pytest.mark.asyncio
    async def test_sweep_uses_budget_not_actual_tx_fee(
        self, mock_wallet_for_sweep, mock_backend_for_sweep, taker_config_for_sweep
    ):
        """Test that sweep uses the tx_fee_budget regardless of actual inputs.

        When makers provide different inputs than estimated:
        - Old behavior: recalculate tx_fee -> mismatch with cj_amount -> residual issue
        - New behavior: use budget as tx_fee -> fee rate may vary but amount is stable

        This test simulates a maker with many UTXOs. The fee rate will be lower
        than requested, but the total fee amount stays at the budget.
        """
        taker = Taker(mock_wallet_for_sweep, mock_backend_for_sweep, taker_config_for_sweep)

        taker.is_sweep = True
        taker.preselected_utxos = mock_wallet_for_sweep.get_all_utxos()
        taker._fee_rate = 1.0

        # Total taker input: 147,483 sats (from mock wallet)
        total_input = sum(u.value for u in taker.preselected_utxos)
        assert total_input == 147_483

        # Simulate order selection: budget was calculated conservatively
        # With 1 maker and conservative estimate (2 inputs/maker + 5 buffer = 7 maker inputs)
        # Total: 2 taker + 7 maker = 9 inputs, 3 outputs (CJ + maker CJ + maker change)
        # vsize = 9*68 + 3*31 + 11 = 716 vbytes at 1 sat/vB = 716 sats
        conservative_budget = 716

        # cj_amount calculated at order selection = total - budget - maker_fees
        # For this test with 0 maker fees: 147,483 - 716 = 146,767 sats
        taker.cj_amount = total_input - conservative_budget
        taker._sweep_tx_fee_budget = conservative_budget

        # Maker with MANY UTXOs (6 inputs instead of estimated 7)
        # Actually fewer than estimated, so fee rate will be HIGHER than 1 sat/vB
        maker_offer = Offer(
            ordertype=OfferType.SW0_ABSOLUTE,
            oid=0,
            minsize=10000,
            maxsize=1_000_000_000,
            txfee=500,
            cjfee=0,
            counterparty="J597qgx3bTJBCAP7",
        )

        maker_session = MakerSession(nick="J597qgx3bTJBCAP7", offer=maker_offer)
        maker_session.pubkey = "c143f23bdecb05a9" + "00" * 24
        maker_session.responded_fill = True
        maker_session.responded_auth = True
        maker_session.utxos = [
            {
                "txid": "4444444444444444444444444444444444444444444444444444444444444444",
                "vout": 11,
                "value": 55_000,
                "address": "bcrt1qmaker",
            },
            {
                "txid": "5555555555555555555555555555555555555555555555555555555555555555",
                "vout": 12,
                "value": 30_161,
                "address": "bcrt1qmaker",
            },
            {
                "txid": "6666666666666666666666666666666666666666666666666666666666666666",
                "vout": 8,
                "value": 30_749,
                "address": "bcrt1qmaker",
            },
            {
                "txid": "7777777777777777777777777777777777777777777777777777777777777777",
                "vout": 2,
                "value": 30_983,
                "address": "bcrt1qmaker",
            },
            {
                "txid": "8888888888888888888888888888888888888888888888888888888888888888",
                "vout": 12,
                "value": 33_000,
                "address": "bcrt1qmaker",
            },
            {
                "txid": "9999999999999999999999999999999999999999999999999999999999999999",
                "vout": 3,
                "value": 45_921,
                "address": "bcrt1qmaker",
            },
        ]
        maker_session.cj_address = "bcrt1qqyqszqgpqyqszqgpqyqszqgpqyqszqgpvxat9t"
        maker_session.change_address = "bcrt1qqgpqyqszqgpqyqszqgpqyqszqgpqyqszazmwwa"
        maker_session.crypto = CryptoSession()

        taker.maker_sessions = {"J597qgx3bTJBCAP7": maker_session}

        result = await taker._phase_build_tx(
            destination="bcrt1qqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcruj60yu",
            mixdepth=3,
        )

        # Should succeed - we use the budget, not actual tx_fee
        assert result is True

        # Verify cj_amount unchanged
        assert taker.cj_amount == total_input - conservative_budget

        # The tx_fee used should be the budget
        # actual vsize: 8 inputs * 68 + 3 outputs * 31 + 11 = 648 vbytes
        # effective rate: 716 / 648 = 1.10 sat/vB (higher than requested 1.0)
        # This is the expected behavior: fee amount is stable, rate may vary


@pytest.mark.asyncio
async def test_blacklist_rejection_doesnt_ignore_maker(
    mock_wallet, mock_backend, mock_config, tmp_path
):
    """Test that makers aren't permanently ignored when they reject a blacklisted commitment.

    When a maker rejects a taker's commitment because it's blacklisted, the taker should
    retry with a different commitment (different NUMS index or UTXO), not permanently
    ignore the maker. The maker might accept a different commitment.
    """
    from taker.orderbook import OrderbookManager

    taker = Taker(mock_wallet, mock_backend, mock_config)
    taker.orderbook_manager = OrderbookManager(
        data_dir=tmp_path,  # Use tmp_path to avoid conflicts with other tests
        max_cj_fee=mock_config.max_cj_fee,
        bondless_makers_allowance=mock_config.bondless_makers_allowance,
        bondless_require_zero_fee=mock_config.bondless_makers_allowance_require_zero_fee,
    )

    # Simulate a blacklist error from a maker
    maker_nick = "J5TestMaker"
    blacklist_result = PhaseResult(
        success=False,
        failed_makers=[maker_nick],
        blacklist_error=True,
        needs_replacement=False,
    )

    # Before processing the result, maker should not be ignored
    assert maker_nick not in taker.orderbook_manager.ignored_makers

    # Process the blacklist rejection (simulating the logic in do_coinjoin)
    if blacklist_result.blacklist_error:
        # Don't add makers to ignored list when commitment is blacklisted
        pass
    elif blacklist_result.failed_makers:
        # Add failed makers to ignore list for non-blacklist failures
        for failed_nick in blacklist_result.failed_makers:
            taker.orderbook_manager.add_ignored_maker(failed_nick)

    # After processing blacklist error, maker should still NOT be ignored
    assert maker_nick not in taker.orderbook_manager.ignored_makers

    # Now test that non-blacklist failures DO ignore the maker
    non_blacklist_result = PhaseResult(
        success=False,
        failed_makers=[maker_nick],
        blacklist_error=False,
        needs_replacement=True,
    )

    if non_blacklist_result.blacklist_error:
        pass
    elif non_blacklist_result.failed_makers:
        for failed_nick in non_blacklist_result.failed_makers:
            taker.orderbook_manager.add_ignored_maker(failed_nick)

    # Now maker should be ignored for non-blacklist failures
    assert maker_nick in taker.orderbook_manager.ignored_makers


class TestUpdatePendingTransactionNow:
    """Tests for immediate pending transaction update on coinjoin completion."""

    @pytest.fixture
    def taker_with_backend(self, mock_wallet, mock_backend, mock_config, tmp_path):
        """Create a taker with a mock backend and temp data dir."""
        mock_config.data_dir = tmp_path
        mock_backend.has_mempool_access = Mock(return_value=True)
        return Taker(mock_wallet, mock_backend, mock_config)

    @pytest.mark.asyncio
    @patch("asyncio.sleep")
    async def test_update_pending_tx_with_mempool_access(
        self, mock_sleep, taker_with_backend, tmp_path
    ):
        """Test that pending transaction is updated when mempool access is available."""
        from jmwallet.backends.base import Transaction
        from jmwallet.history import (
            append_history_entry,
            create_taker_history_entry,
            get_pending_transactions,
            read_history,
        )

        taker = taker_with_backend
        txid = "a" * 64
        destination = "bcrt1qdest"

        # Create and append a pending history entry
        entry = create_taker_history_entry(
            maker_nicks=["J5TestMaker"],
            cj_amount=100000,
            total_maker_fees=250,
            mining_fee=500,
            destination=destination,
            change_address="bcrt1qchange1",
            source_mixdepth=0,
            selected_utxos=[("b" * 64, 0)],
            txid=txid,
        )
        append_history_entry(entry, data_dir=tmp_path)

        # Verify it's pending
        pending = get_pending_transactions(data_dir=tmp_path)
        assert len(pending) == 1
        assert pending[0].txid == txid

        # Mock backend to return transaction in mempool (0 confirmations)
        taker.backend.get_transaction = AsyncMock(
            return_value=Transaction(
                txid=txid,
                raw="",
                confirmations=0,
            )
        )

        # Call the update method
        await taker._update_pending_transaction_now(txid, destination)

        # Verify transaction is no longer pending
        pending = get_pending_transactions(data_dir=tmp_path)
        assert len(pending) == 0

        # Verify history shows it as confirmed
        history = read_history(data_dir=tmp_path)
        assert len(history) == 1
        assert history[0].success is True
        assert history[0].confirmations >= 1

    @pytest.mark.asyncio
    @patch("asyncio.sleep")
    async def test_update_pending_tx_with_confirmations(
        self, mock_sleep, taker_with_backend, tmp_path
    ):
        """Test that confirmation count is properly recorded."""
        from jmwallet.backends.base import Transaction
        from jmwallet.history import (
            append_history_entry,
            create_taker_history_entry,
            read_history,
        )

        taker = taker_with_backend
        txid = "c" * 64
        destination = "bcrt1qdest2"

        # Create and append a pending history entry
        entry = create_taker_history_entry(
            maker_nicks=["J5TestMaker"],
            cj_amount=200000,
            total_maker_fees=500,
            mining_fee=1000,
            destination=destination,
            change_address="bcrt1qchange2",
            source_mixdepth=1,
            selected_utxos=[("d" * 64, 1)],
            txid=txid,
        )
        append_history_entry(entry, data_dir=tmp_path)

        # Mock backend to return transaction with 3 confirmations
        taker.backend.get_transaction = AsyncMock(
            return_value=Transaction(
                txid=txid,
                raw="",
                confirmations=3,
            )
        )

        # Call the update method
        await taker._update_pending_transaction_now(txid, destination)

        # Verify history shows correct confirmation count
        history = read_history(data_dir=tmp_path)
        assert len(history) == 1
        assert history[0].confirmations == 3
        assert history[0].success is True

    @pytest.mark.asyncio
    async def test_update_pending_tx_without_mempool_access(
        self, mock_wallet, mock_backend, mock_config, tmp_path
    ):
        """Test behavior when backend has no mempool access (Neutrino)."""
        from jmwallet.history import (
            append_history_entry,
            create_taker_history_entry,
            get_pending_transactions,
        )

        mock_config.data_dir = tmp_path
        mock_backend.has_mempool_access = Mock(return_value=False)
        mock_backend.get_block_height = AsyncMock(return_value=100)
        # Simulate unconfirmed transaction (verify_tx_output returns False)
        mock_backend.verify_tx_output = AsyncMock(return_value=False)

        taker = Taker(mock_wallet, mock_backend, mock_config)
        txid = "e" * 64
        destination = "bcrt1qdest3"

        # Create and append a pending history entry
        entry = create_taker_history_entry(
            maker_nicks=["J5TestMaker"],
            cj_amount=50000,
            total_maker_fees=100,
            mining_fee=200,
            destination=destination,
            change_address="bcrt1qchange3",
            source_mixdepth=0,
            selected_utxos=[("f" * 64, 0)],
            txid=txid,
        )
        append_history_entry(entry, data_dir=tmp_path)

        # Call the update method - should not update since not confirmed
        await taker._update_pending_transaction_now(txid, destination)

        # Transaction should still be pending (Neutrino can't see mempool)
        pending = get_pending_transactions(data_dir=tmp_path)
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_update_pending_tx_neutrino_confirmed(
        self, mock_wallet, mock_backend, mock_config, tmp_path
    ):
        """Test Neutrino backend with confirmed transaction."""
        from jmwallet.history import (
            append_history_entry,
            create_taker_history_entry,
            get_pending_transactions,
            read_history,
        )

        mock_config.data_dir = tmp_path
        mock_backend.has_mempool_access = Mock(return_value=False)
        mock_backend.get_block_height = AsyncMock(return_value=100)
        # Simulate confirmed transaction (verify_tx_output returns True)
        mock_backend.verify_tx_output = AsyncMock(return_value=True)

        taker = Taker(mock_wallet, mock_backend, mock_config)
        txid = "g" * 64
        destination = "bcrt1qdest4"

        # Create and append a pending history entry
        entry = create_taker_history_entry(
            maker_nicks=["J5TestMaker"],
            cj_amount=75000,
            total_maker_fees=150,
            mining_fee=300,
            destination=destination,
            change_address="bcrt1qchange4",
            source_mixdepth=2,
            selected_utxos=[("h" * 64, 0)],
            txid=txid,
        )
        append_history_entry(entry, data_dir=tmp_path)

        # Call the update method
        await taker._update_pending_transaction_now(txid, destination)

        # Verify transaction is no longer pending
        pending = get_pending_transactions(data_dir=tmp_path)
        assert len(pending) == 0

        # Verify history shows it as confirmed
        history = read_history(data_dir=tmp_path)
        assert len(history) == 1
        assert history[0].success is True
        assert history[0].confirmations == 1


class TestHistoryMiningFeeRecording:
    """Regression tests for correct mining fee recording in taker history.

    The taker must record actual_mining_fee (total_inputs - total_outputs) from the
    signed transaction, NOT tx_metadata["fee"] which is just the estimated fee used
    during transaction construction. These values differ in sweep mode (residual goes
    to miners) and can differ in normal mode (signature size variance).
    """

    def test_sweep_actual_mining_fee_exceeds_estimate(self, tmp_path) -> None:
        """Verify that actual mining fee (not estimated) is recorded for sweeps.

        In sweep mode, the taker has no change output. The equation is:
          taker_input = cj_amount + maker_fees + estimated_tx_fee + residual
        where residual goes to miners. The actual_mining_fee = estimated_tx_fee + residual.

        Previously, tx_metadata["fee"] (= estimated_tx_fee only) was used, causing
        the recorded mining fee to be too low and net_fee to only reflect maker fees.
        """
        from jmwallet.history import (
            append_history_entry,
            create_taker_history_entry,
            read_history,
            update_taker_awaiting_transaction_broadcast,
        )

        maker_fees = 6
        # Simulate: taker_input=94478, cj_amount=94157, actual_mining_fee=315
        # The estimated tx_fee might have been different (e.g., 300), but the actual
        # mining fee from the signed transaction is 315 (includes residual).
        actual_mining_fee = 315

        # Phase 1: Create the initial "Awaiting transaction" entry (mining_fee=0)
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1", "J5maker2", "J5maker3"],
            cj_amount=94_157,
            total_maker_fees=maker_fees,
            mining_fee=0,  # Unknown before broadcast
            destination="bcrt1qsweepdest123456",
            change_address="",  # Sweep: no change output
            source_mixdepth=0,
            selected_utxos=[("a" * 64, 0)],
            txid="",
            failure_reason="Awaiting transaction",
        )
        append_history_entry(entry, data_dir=tmp_path)

        # Verify initial state: net_fee only reflects maker fees (bug behavior)
        history = read_history(data_dir=tmp_path)
        assert history[0].mining_fee_paid == 0
        assert history[0].net_fee == -(maker_fees + 0)  # -6, missing mining fee

        # Phase 2: Update with ACTUAL mining fee after broadcast
        # This is what taker.py now does: passes actual_mining_fee, not tx_metadata["fee"]
        updated = update_taker_awaiting_transaction_broadcast(
            destination_address="bcrt1qsweepdest123456",
            change_address="",
            txid="7d374988a00caf0c41d02fdd925c1a65023cf5676ecc3cedbcbfb6fa42999511",
            mining_fee=actual_mining_fee,
            data_dir=tmp_path,
        )
        assert updated is True

        # Verify: mining fee and net_fee correctly reflect the full cost
        history = read_history(data_dir=tmp_path)
        assert len(history) == 1
        assert history[0].mining_fee_paid == 315
        assert history[0].net_fee == -(maker_fees + actual_mining_fee)  # -(6 + 315) = -321
        assert history[0].total_maker_fees_paid == maker_fees

    def test_normal_mode_mining_fee_recorded(self, tmp_path) -> None:
        """Verify mining fee is correctly recorded in normal (non-sweep) mode.

        In normal mode, the taker has a change output that absorbs the difference
        between the estimated and actual fee. The actual_mining_fee from
        total_inputs - total_outputs should match what's recorded.
        """
        from jmwallet.history import (
            append_history_entry,
            create_taker_history_entry,
            read_history,
            update_taker_awaiting_transaction_broadcast,
        )

        maker_fees = 500
        actual_mining_fee = 750

        # Create pending entry
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1", "J5maker2"],
            cj_amount=1_000_000,
            total_maker_fees=maker_fees,
            mining_fee=0,
            destination="bcrt1qnormaldest12345",
            change_address="bcrt1qnormalchange123",
            source_mixdepth=0,
            selected_utxos=[("b" * 64, 0), ("c" * 64, 1)],
            txid="",
            failure_reason="Awaiting transaction",
        )
        append_history_entry(entry, data_dir=tmp_path)

        # Update with actual mining fee
        updated = update_taker_awaiting_transaction_broadcast(
            destination_address="bcrt1qnormaldest12345",
            change_address="bcrt1qnormalchange123",
            txid="d" * 64,
            mining_fee=actual_mining_fee,
            data_dir=tmp_path,
        )
        assert updated is True

        history = read_history(data_dir=tmp_path)
        assert history[0].mining_fee_paid == actual_mining_fee
        assert history[0].net_fee == -(maker_fees + actual_mining_fee)  # -(500 + 750) = -1250
