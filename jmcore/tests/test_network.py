"""
Tests for jmcore.network
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from jmcore.crypto import NickIdentity
from jmcore.network import (
    ONION_HOSTID,
    ConnectionError,
    ConnectionPool,
    OnionPeer,
    PeerStatus,
    TCPConnection,
)


@pytest.mark.asyncio
async def test_tcp_connection_send():
    reader = AsyncMock()
    writer = Mock()
    writer.drain = AsyncMock()

    conn = TCPConnection(reader, writer)
    await conn.send(b"hello")

    writer.write.assert_called_with(b"hello\r\n")
    writer.drain.assert_called()

    # Test message too large
    conn.max_message_size = 5
    with pytest.raises(ValueError):
        await conn.send(b"123456")


@pytest.mark.asyncio
async def test_tcp_connection_receive():
    reader = AsyncMock()
    reader.readuntil.return_value = b"response\r\n"
    writer = Mock()

    conn = TCPConnection(reader, writer)
    data = await conn.receive()

    assert data == b"response"

    # Test connection closed
    conn._connected = False
    with pytest.raises(ConnectionError):
        await conn.receive()


def test_connection_pool():
    pool = ConnectionPool(max_connections=2)
    c1 = Mock()
    c2 = Mock()
    c3 = Mock()

    pool.add("p1", c1)
    pool.add("p2", c2)

    assert pool.get("p1") == c1
    assert len(pool) == 2

    with pytest.raises(ConnectionError):
        pool.add("p3", c3)

    pool.remove("p1")
    assert len(pool) == 1
    pool.add("p3", c3)
    assert len(pool) == 2


@pytest.mark.asyncio
async def test_connection_pool_close_all():
    pool = ConnectionPool()
    c1 = Mock()
    c1.close = AsyncMock()
    pool.add("p1", c1)

    await pool.close_all()
    c1.close.assert_called()
    assert len(pool) == 0


@pytest.mark.asyncio
async def test_tcp_connection_concurrent_receive() -> None:
    """Test that concurrent receive calls are serialized by the receive lock.

    This test reproduces the bug:
    "readuntil() called while another coroutine is already waiting for incoming data"

    The issue occurs when:
    1. listen_continuously() is waiting on receive() in an infinite loop
    2. get_peerlist_with_features() tries to receive() concurrently

    Without the receive lock, asyncio.StreamReader.readuntil() raises RuntimeError
    when called by multiple coroutines simultaneously.
    """
    import asyncio

    # Create a real StreamReader/StreamWriter pair using pipes
    # This allows us to test actual concurrent read behavior
    reader = asyncio.StreamReader()
    writer = Mock()

    conn = TCPConnection(reader, writer)

    # Track the order of operations
    events: list[str] = []
    results: list[bytes] = []

    async def slow_reader(name: str) -> None:
        """Simulate a slow reader that waits for data."""
        events.append(f"{name}_start")
        try:
            data = await conn.receive()
            results.append(data)
            events.append(f"{name}_got_{data.decode()}")
        except Exception as e:
            events.append(f"{name}_error_{type(e).__name__}")

    async def feed_data_delayed() -> None:
        """Feed data to the reader after a short delay."""
        await asyncio.sleep(0.05)
        reader.feed_data(b"msg1\r\n")
        await asyncio.sleep(0.05)
        reader.feed_data(b"msg2\r\n")

    # Start two concurrent readers and the data feeder
    task1 = asyncio.create_task(slow_reader("reader1"))
    task2 = asyncio.create_task(slow_reader("reader2"))
    feeder = asyncio.create_task(feed_data_delayed())

    # Wait for all tasks
    await asyncio.gather(task1, task2, feeder, return_exceptions=True)

    # Both readers should complete successfully (serialized by lock)
    assert "reader1_start" in events
    assert "reader2_start" in events

    # Both messages should be received (one by each reader)
    assert len(results) == 2
    assert set(results) == {b"msg1", b"msg2"}

    # No RuntimeError should have occurred
    error_events = [e for e in events if "error" in e]
    assert not error_events, f"Unexpected errors: {error_events}"


# =============================================================================
# OnionPeer Tests
# =============================================================================


class TestOnionPeerBasic:
    """Basic OnionPeer tests without network calls."""

    def test_peer_initialization(self):
        """Test OnionPeer initialization with valid location."""
        peer = OnionPeer(
            nick="J5maker123",
            location="abc123def.onion:5222",
        )

        assert peer.nick == "J5maker123"
        assert peer.location == "abc123def.onion:5222"
        assert peer.hostname == "abc123def.onion"
        assert peer.port == 5222
        assert peer.status() == PeerStatus.UNCONNECTED
        assert not peer.is_connected()
        assert peer.can_connect()

    def test_peer_default_timeout(self):
        """Test OnionPeer default timeout is 120s (matches Tor circuit timeout).

        The timeout covers the entire SOCKS5 connection lifecycle including
        Tor circuit building and PoW solving. Under PoW defense, Tor clients
        solve proof-of-work challenges that can take significantly longer
        than normal circuit establishment.
        """
        peer = OnionPeer(
            nick="J5test",
            location="test.onion:5222",
        )
        assert peer.timeout == 120.0

    def test_peer_custom_timeout(self):
        """Test OnionPeer accepts custom timeout."""
        peer = OnionPeer(
            nick="J5test",
            location="test.onion:5222",
            timeout=60.0,
        )
        assert peer.timeout == 60.0

    def test_peer_not_serving_onion(self):
        """Test OnionPeer with NOT-SERVING-ONION location."""
        peer = OnionPeer(
            nick="J5taker456",
            location="NOT-SERVING-ONION",
        )

        assert peer.nick == "J5taker456"
        assert peer.hostname is None
        assert peer.port is None
        assert not peer.can_connect()  # Cannot connect to non-serving peer

    def test_peer_invalid_location(self):
        """Test OnionPeer with invalid location format."""
        peer = OnionPeer(
            nick="J5bad",
            location="invalid-no-port",
        )

        assert peer.hostname is None
        assert peer.port is None
        assert not peer.can_connect()

    def test_peer_status_transitions(self):
        """Test that peer status is tracked correctly."""
        peer = OnionPeer(
            nick="J5test",
            location="test.onion:5222",
        )

        assert peer.status() == PeerStatus.UNCONNECTED
        assert peer.can_connect()
        assert not peer.is_connected()
        assert not peer.is_connecting()


class TestOnionPeerConnection:
    """OnionPeer connection tests with mocked network."""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """Test successful peer connection and handshake."""
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
        )

        # Mock the connection
        mock_connection = AsyncMock()
        mock_connection.is_connected.return_value = True

        # Mock handshake response (peer-to-peer format uses "line" with JSON-encoded string)
        import json

        handshake_data = {
            "app-name": "joinmarket",
            "proto-ver": 5,
            "directory": False,
            "features": {},
            "location-string": "test.onion:5222",
            "nick": "J5maker",
            "network": "regtest",
        }
        handshake_response = {
            "type": 793,  # HANDSHAKE
            "line": json.dumps(handshake_data),
        }

        mock_connection.receive.return_value = json.dumps(handshake_response).encode()

        with patch("jmcore.network.connect_via_tor", return_value=mock_connection):
            success = await peer.connect(
                our_nick="J5taker",
                our_location="NOT-SERVING-ONION",
                network="regtest",
            )

            # Disconnect immediately to stop the receive loop task
            await peer.disconnect()

        assert success
        # Status will be DISCONNECTED after disconnect()
        # But success indicates the connect+handshake worked

    @pytest.mark.asyncio
    async def test_connect_records_peer_features(self):
        """OnionPeer stores features advertised by the peer during handshake.

        This lets the taker skip incompatible makers before sending !fill,
        rather than discovering the mismatch only during !auth / !pubkey
        response parsing.
        """
        peer = OnionPeer(nick="J5maker", location="test.onion:5222")

        # Unknown until handshake completes.
        assert peer.supports_feature("neutrino_compat") is None

        mock_connection = AsyncMock()
        mock_connection.is_connected.return_value = True

        import json

        handshake_data = {
            "app-name": "joinmarket",
            "proto-ver": 5,
            "directory": False,
            "features": {"neutrino_compat": True, "peerlist_features": True},
            "location-string": "test.onion:5222",
            "nick": "J5maker",
            "network": "regtest",
        }
        handshake_response = {"type": 793, "line": json.dumps(handshake_data)}
        mock_connection.receive.return_value = json.dumps(handshake_response).encode()

        with patch("jmcore.network.connect_via_tor", return_value=mock_connection):
            success = await peer.connect(
                our_nick="J5taker",
                our_location="NOT-SERVING-ONION",
                network="regtest",
            )
            await peer.disconnect()

        assert success
        assert peer.peer_features == {"neutrino_compat": True, "peerlist_features": True}
        assert peer.supports_feature("neutrino_compat") is True
        assert peer.supports_feature("peerlist_features") is True
        # Known feature dict, feature absent -> False (not None).
        assert peer.supports_feature("push_encrypted") is False

    @pytest.mark.asyncio
    async def test_connect_peer_without_features_is_unknown(self):
        """A peer that handshakes with an empty features dict is treated as
        "unknown compatibility" (None), so callers don't wrongly assume legacy
        peers advertise no features when in fact they advertised nothing.
        """
        peer = OnionPeer(nick="J5maker", location="test.onion:5222")

        mock_connection = AsyncMock()
        mock_connection.is_connected.return_value = True

        import json

        handshake_data = {
            "app-name": "joinmarket",
            "proto-ver": 5,
            "directory": False,
            "features": {},
            "location-string": "test.onion:5222",
            "nick": "J5maker",
            "network": "regtest",
        }
        handshake_response = {"type": 793, "line": json.dumps(handshake_data)}
        mock_connection.receive.return_value = json.dumps(handshake_response).encode()

        with patch("jmcore.network.connect_via_tor", return_value=mock_connection):
            success = await peer.connect(
                our_nick="J5taker",
                our_location="NOT-SERVING-ONION",
                network="regtest",
            )
            await peer.disconnect()

        assert success
        assert peer.peer_features == {}
        # Empty features dict -> unknown (not "confirmed-no-support").
        assert peer.supports_feature("neutrino_compat") is None

    @pytest.mark.asyncio
    async def test_connect_handshake_rejected(self):
        """Test connection when handshake has wrong app name."""
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
        )

        mock_connection = AsyncMock()
        mock_connection.is_connected.return_value = True

        # Wrong app name
        import json

        handshake_data = {
            "app-name": "wrongapp",
            "proto-ver": 5,
            "directory": False,
            "features": {},
            "location-string": "test.onion:5222",
            "nick": "J5maker",
            "network": "regtest",
        }
        handshake_response = {
            "type": 793,
            "line": json.dumps(handshake_data),
        }

        mock_connection.receive.return_value = json.dumps(handshake_response).encode()

        with patch("jmcore.network.connect_via_tor", return_value=mock_connection):
            success = await peer.connect(
                our_nick="J5taker",
                our_location="NOT-SERVING-ONION",
                network="regtest",
            )

        assert not success
        assert peer.status() == PeerStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_connect_network_mismatch(self):
        """Test connection when network doesn't match."""
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
        )

        mock_connection = AsyncMock()
        mock_connection.is_connected.return_value = True

        # Different network
        import json

        handshake_data = {
            "app-name": "joinmarket",
            "proto-ver": 5,
            "directory": False,
            "features": {},
            "location-string": "test.onion:5222",
            "nick": "J5maker",
            "network": "mainnet",  # We expect regtest
        }
        handshake_response = {
            "type": 793,
            "line": json.dumps(handshake_data),
        }

        mock_connection.receive.return_value = json.dumps(handshake_response).encode()

        with patch("jmcore.network.connect_via_tor", return_value=mock_connection):
            success = await peer.connect(
                our_nick="J5taker",
                our_location="NOT-SERVING-ONION",
                network="regtest",
            )

        assert not success
        assert peer.status() == PeerStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_connect_connection_failure(self):
        """Test connection when network connection fails."""
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
        )

        with patch(
            "jmcore.network.connect_via_tor", side_effect=ConnectionError("Connection refused")
        ):
            success = await peer.connect(
                our_nick="J5taker",
                our_location="NOT-SERVING-ONION",
                network="regtest",
            )

        assert not success
        assert peer.status() == PeerStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_send_privmsg(self):
        """Test sending a private message via direct connection."""
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
        )

        # Set up as connected (without starting receive loop)
        mock_connection = AsyncMock()
        mock_connection.is_connected.return_value = True
        peer._connection = mock_connection
        peer._status = PeerStatus.HANDSHAKED

        success = await peer.send_privmsg(
            our_nick="J5taker",
            command="fill",
            message="123 456 abc",
        )

        assert success
        mock_connection.send.assert_called_once()

        # Verify message format
        import json

        sent_data = mock_connection.send.call_args[0][0]
        msg = json.loads(sent_data.decode())
        assert msg["type"] == 685  # PRIVMSG
        assert "J5taker!J5maker!fill 123 456 abc" in msg["line"]

    @pytest.mark.asyncio
    async def test_send_when_not_connected(self):
        """Test that send fails when not connected."""
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
        )

        success = await peer.send(b"test message")
        assert not success

        success = await peer.send_privmsg(
            our_nick="J5taker",
            command="fill",
            message="test",
        )
        assert not success

    @pytest.mark.asyncio
    async def test_send_privmsg_with_signature(self):
        """Test that messages are signed when nick_identity is provided.

        This is critical for compatibility with the reference implementation.
        The reference maker verifies all private messages, whether received via
        directory relay or direct peer connection. Without proper signing,
        messages are rejected with "Sig not properly appended to privmsg".
        """
        # Create a nick identity for signing
        nick_identity = NickIdentity()

        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
            nick_identity=nick_identity,
        )

        # Set up as connected (without starting receive loop)
        mock_connection = AsyncMock()
        mock_connection.is_connected.return_value = True
        peer._connection = mock_connection
        peer._status = PeerStatus.HANDSHAKED

        success = await peer.send_privmsg(
            our_nick=nick_identity.nick,
            command="auth",
            message="encrypted_data_here",
        )

        assert success
        mock_connection.send.assert_called_once()

        # Verify message format includes signature
        import json

        sent_data = mock_connection.send.call_args[0][0]
        msg = json.loads(sent_data.decode())
        assert msg["type"] == 685  # PRIVMSG

        # Message should have format: nick!recipient!command message pubkey sig
        line = msg["line"]
        parts = line.split("!")
        assert len(parts) == 3
        assert parts[0] == nick_identity.nick  # from_nick
        assert parts[1] == "J5maker"  # to_nick

        # The message part should contain: "auth encrypted_data_here pubkey_hex sig_b64"
        message_part = parts[2]
        assert message_part.startswith("auth ")

        # Split command from rest
        _, signed_data = message_part.split(" ", 1)

        # signed_data should be: "encrypted_data_here pubkey_hex sig_b64"
        data_parts = signed_data.split(" ")
        assert len(data_parts) == 3, f"Expected 3 parts (data, pubkey, sig), got: {data_parts}"
        assert data_parts[0] == "encrypted_data_here"
        assert data_parts[1] == nick_identity.public_key_hex
        # data_parts[2] is the base64 signature

        # Verify the signature is valid by manually checking
        import base64

        from coincurve import PublicKey

        from jmcore.crypto import bitcoin_message_hash

        sig_bytes = base64.b64decode(data_parts[2])
        msg_to_verify = "encrypted_data_here" + ONION_HOSTID
        msg_hash = bitcoin_message_hash(msg_to_verify)
        pubkey = PublicKey(bytes.fromhex(nick_identity.public_key_hex))
        assert pubkey.verify(sig_bytes, msg_hash, hasher=None)

    @pytest.mark.asyncio
    async def test_send_privmsg_without_identity_no_signature(self):
        """Test that messages are NOT signed when nick_identity is not provided.

        This maintains backward compatibility but will not work with reference makers.
        """
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
            # No nick_identity provided
        )

        # Set up as connected
        mock_connection = AsyncMock()
        mock_connection.is_connected.return_value = True
        peer._connection = mock_connection
        peer._status = PeerStatus.HANDSHAKED

        success = await peer.send_privmsg(
            our_nick="J5taker",
            command="fill",
            message="123 456 abc",
        )

        assert success

        import json

        sent_data = mock_connection.send.call_args[0][0]
        msg = json.loads(sent_data.decode())

        # Message should NOT have pubkey/sig appended (old behavior)
        line = msg["line"]
        assert "J5taker!J5maker!fill 123 456 abc" in line
        # Should only have the original message, not 3 space-separated parts
        message_part = line.split("!")[-1]
        assert message_part == "fill 123 456 abc"


class TestOnionPeerBackoff:
    """Test connection backoff and retry behavior."""

    @pytest.mark.asyncio
    async def test_try_to_connect_backoff(self):
        """Test that failed connections trigger backoff."""
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
        )

        # First attempt should be allowed
        assert peer.can_connect()

        # Simulate a failed connection attempt
        peer._connect_attempts = 1
        peer._last_connect_attempt = asyncio.get_event_loop().time()
        peer._status = PeerStatus.DISCONNECTED

        # Immediate retry should be blocked by backoff
        task = peer.try_to_connect(
            our_nick="J5taker",
            our_location="NOT-SERVING-ONION",
            network="regtest",
        )
        assert task is None  # Blocked by backoff

    @pytest.mark.asyncio
    async def test_max_attempts_exceeded(self):
        """Test that connection gives up after max attempts."""
        peer = OnionPeer(
            nick="J5maker",
            location="test.onion:5222",
        )

        peer._connect_attempts = 3  # Max default
        peer._status = PeerStatus.DISCONNECTED
        peer._last_connect_attempt = 0  # Long ago, no backoff

        task = peer.try_to_connect(
            our_nick="J5taker",
            our_location="NOT-SERVING-ONION",
            network="regtest",
        )
        assert task is None  # Gave up


class FakeSocks5Server:
    """Minimal in-process SOCKS5 server for testing connect_via_tor.

    Performs the SOCKS5 greeting, optional username/password authentication
    (RFC 1929), and the CONNECT request, then echoes back a canned line so the
    resulting TCPConnection can be exercised end to end. Records the
    destination host/port and credentials it received.
    """

    def __init__(self, require_auth: bool = False) -> None:
        self.require_auth = require_auth
        self.server: asyncio.Server | None = None
        self.port: int = 0
        self.dest_host: str | None = None
        self.dest_port: int | None = None
        self.username: str | None = None
        self.password: str | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Greeting: VER NMETHODS METHODS...
            ver, nmethods = await reader.readexactly(2)
            assert ver == 5
            await reader.readexactly(nmethods)

            if self.require_auth:
                writer.write(b"\x05\x02")  # username/password
                await writer.drain()
                auth_ver = (await reader.readexactly(1))[0]
                assert auth_ver == 1
                ulen = (await reader.readexactly(1))[0]
                self.username = (await reader.readexactly(ulen)).decode()
                plen = (await reader.readexactly(1))[0]
                self.password = (await reader.readexactly(plen)).decode()
                writer.write(b"\x01\x00")  # auth success
            else:
                writer.write(b"\x05\x00")  # no auth
            await writer.drain()

            # CONNECT request: VER CMD RSV ATYP ...
            ver, cmd, _rsv, atyp = await reader.readexactly(4)
            assert ver == 5 and cmd == 1
            assert atyp == 3, "expected domain address type (rdns)"
            domain_len = (await reader.readexactly(1))[0]
            self.dest_host = (await reader.readexactly(domain_len)).decode()
            port_bytes = await reader.readexactly(2)
            self.dest_port = int.from_bytes(port_bytes, "big")

            # Success reply with a zero bind address
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()

            # Behave like the destination: echo one line prefixed with "echo:"
            line = await reader.readuntil(b"\n")
            writer.write(b"echo:" + line)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, AssertionError):
            pass
        finally:
            writer.close()


class TestConnectViaTor:
    """Tests for the async SOCKS5 dialer used for all Tor connections."""

    @pytest.mark.asyncio
    async def test_connect_and_roundtrip(self):
        from jmcore.network import connect_via_tor

        proxy = FakeSocks5Server()
        await proxy.start()
        try:
            conn = await connect_via_tor(
                "test.onion",
                5222,
                socks_host="127.0.0.1",
                socks_port=proxy.port,
                timeout=5.0,
            )
            await conn.send(b"hello")
            reply = await asyncio.wait_for(conn.receive(), timeout=5.0)
            assert reply == b"echo:hello"
            await conn.close()

            # The onion hostname must be resolved by the proxy (rdns), never
            # locally: local resolution would leak the destination to DNS.
            assert proxy.dest_host == "test.onion"
            assert proxy.dest_port == 5222
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_connect_with_stream_isolation_credentials(self):
        from jmcore.network import connect_via_tor

        proxy = FakeSocks5Server(require_auth=True)
        await proxy.start()
        try:
            conn = await connect_via_tor(
                "test.onion",
                5222,
                socks_host="127.0.0.1",
                socks_port=proxy.port,
                timeout=5.0,
                socks_username="jm-peer",
                socks_password="isolation",
            )
            await conn.close()
            assert proxy.username == "jm-peer"
            assert proxy.password == "isolation"
        finally:
            await proxy.stop()

    @pytest.mark.asyncio
    async def test_cancellation_is_prompt_and_threadless(self):
        """Cancelling a pending dial must complete immediately.

        Regression test for the shutdown hang: the old implementation ran a
        blocking PySocks connect in the default thread-pool executor, so
        cancelled dials kept non-daemon threads alive that blocked
        ``asyncio.run`` teardown and interpreter exit for up to the SOCKS
        timeout per dial (or forever on Python 3.11).
        """
        import contextlib
        import threading

        from jmcore.network import connect_via_tor

        # A proxy that accepts the TCP connection but never answers the
        # SOCKS5 greeting, like Tor building a circuit for a slow onion.
        # It exits on client EOF so server.wait_closed() does not linger.
        async def silent_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            with contextlib.suppress(Exception):
                await reader.read(-1)
            writer.close()

        server = await asyncio.start_server(silent_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        threads_before = threading.active_count()
        try:
            task = asyncio.create_task(
                connect_via_tor(
                    "test.onion",
                    5222,
                    socks_host="127.0.0.1",
                    socks_port=port,
                    timeout=120.0,
                )
            )
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2.0)

            # No executor threads may linger from the dial.
            assert threading.active_count() <= threads_before
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_timeout_raises_connection_error(self):
        from jmcore.network import connect_via_tor

        async def silent_handler(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            import contextlib

            with contextlib.suppress(Exception):
                await reader.read(-1)
            writer.close()

        server = await asyncio.start_server(silent_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            with pytest.raises(ConnectionError):
                await connect_via_tor(
                    "test.onion",
                    5222,
                    socks_host="127.0.0.1",
                    socks_port=port,
                    timeout=0.3,
                )
        finally:
            server.close()
            await server.wait_closed()

    @pytest.mark.asyncio
    async def test_proxy_refused_raises_connection_error(self):
        from jmcore.network import connect_via_tor

        # Bind and close a socket to get a port with nothing listening.
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()

        with pytest.raises(ConnectionError):
            await connect_via_tor(
                "test.onion",
                5222,
                socks_host="127.0.0.1",
                socks_port=port,
                timeout=2.0,
            )
