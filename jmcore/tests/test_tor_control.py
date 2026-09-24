"""
Tests for Tor control port functionality.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmcore.tor_control import (
    EphemeralHiddenService,
    HiddenServiceDoSConfig,
    TorAuthenticationError,
    TorCapabilities,
    TorControlClient,
    TorControlError,
    TorHiddenServiceError,
)


class TestHiddenServiceDoSConfig:
    """Tests for HiddenServiceDoSConfig dataclass."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = HiddenServiceDoSConfig()

        # Intro DoS defense disabled by default (not supported for ephemeral HS)
        assert config.intro_dos_enabled is False
        assert config.intro_dos_rate_per_sec == 25
        assert config.intro_dos_burst_per_sec == 200

        # PoW enabled by default (starts at effort 0, scales under attack)
        assert config.pow_enabled is True
        assert config.pow_queue_rate == 25
        assert config.pow_queue_burst == 250

        # No stream limit by default
        assert config.max_streams is None
        assert config.max_streams_close_circuit is True

    def test_custom_values(self) -> None:
        """Test custom configuration."""
        config = HiddenServiceDoSConfig(
            intro_dos_enabled=True,
            intro_dos_rate_per_sec=10,
            intro_dos_burst_per_sec=50,
            pow_enabled=True,
            pow_queue_rate=100,
            pow_queue_burst=500,
            max_streams=5,
            max_streams_close_circuit=False,
        )

        assert config.intro_dos_rate_per_sec == 10
        assert config.intro_dos_burst_per_sec == 50
        assert config.pow_enabled is True
        assert config.pow_queue_rate == 100
        assert config.pow_queue_burst == 500
        assert config.max_streams == 5
        assert config.max_streams_close_circuit is False


class TestTorCapabilities:
    """Tests for TorCapabilities detection."""

    def test_from_version_0_4_2(self) -> None:
        """Test capabilities for Tor 0.4.2 (intro DoS support)."""
        caps = TorCapabilities.from_version("0.4.2.7")

        assert caps.version == "0.4.2.7"
        assert caps.version_tuple == (0, 4, 2)
        assert caps.has_intro_dos is True
        assert caps.has_pow_module is False
        assert caps.has_add_onion_pow is False

    def test_from_version_0_4_8(self) -> None:
        """Test capabilities for Tor 0.4.8 (PoW config support, but not ADD_ONION PoW)."""
        caps = TorCapabilities.from_version("0.4.8.10")

        assert caps.version == "0.4.8.10"
        assert caps.version_tuple == (0, 4, 8)
        assert caps.has_intro_dos is True
        assert caps.has_pow_module is True
        # ADD_ONION PoW only added in 0.4.9.2
        assert caps.has_add_onion_pow is False

    def test_from_version_0_4_9_2(self) -> None:
        """Test capabilities for Tor 0.4.9.2 (ADD_ONION PoW support)."""
        caps = TorCapabilities.from_version("0.4.9.2-alpha")

        assert caps.version == "0.4.9.2-alpha"
        assert caps.version_tuple == (0, 4, 9)
        assert caps.has_intro_dos is True
        assert caps.has_pow_module is True
        assert caps.has_add_onion_pow is True

    def test_from_version_0_4_9_1(self) -> None:
        """Test capabilities for Tor 0.4.9.1 (just before ADD_ONION PoW)."""
        caps = TorCapabilities.from_version("0.4.9.1")

        assert caps.version == "0.4.9.1"
        assert caps.version_tuple == (0, 4, 9)
        assert caps.has_intro_dos is True
        assert caps.has_pow_module is True
        # ADD_ONION PoW only added in 0.4.9.2
        assert caps.has_add_onion_pow is False

    def test_from_version_with_suffix(self) -> None:
        """Test version parsing with alpha/rc suffix."""
        caps = TorCapabilities.from_version("0.4.8.1-alpha")

        assert caps.version == "0.4.8.1-alpha"
        assert caps.version_tuple == (0, 4, 8)
        assert caps.has_pow_module is True
        assert caps.has_add_onion_pow is False

    def test_from_version_old(self) -> None:
        """Test capabilities for old Tor version."""
        caps = TorCapabilities.from_version("0.4.1.5")

        assert caps.version_tuple == (0, 4, 1)
        assert caps.has_intro_dos is False
        assert caps.has_pow_module is False
        assert caps.has_add_onion_pow is False

    def test_from_version_invalid(self) -> None:
        """Test handling of invalid version string."""
        caps = TorCapabilities.from_version("invalid")

        assert caps.version == "invalid"
        assert caps.version_tuple == (0, 0, 0)
        assert caps.has_intro_dos is False
        assert caps.has_pow_module is False
        assert caps.has_add_onion_pow is False


class TestEphemeralHiddenService:
    """Tests for EphemeralHiddenService data class."""

    def test_onion_address(self) -> None:
        """Test onion_address property."""
        service_id = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv"
        hs = EphemeralHiddenService(service_id=service_id)

        assert hs.onion_address == f"{service_id}.onion"

    def test_with_ports_and_key(self) -> None:
        """Test with ports and private key."""
        service_id = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv"
        private_key = "ED25519-V3:base64encodedkey=="
        ports = [(80, "127.0.0.1:8080"), (443, "127.0.0.1:8443")]

        hs = EphemeralHiddenService(
            service_id=service_id,
            private_key=private_key,
            ports=ports,
        )

        assert hs.service_id == service_id
        assert hs.private_key == private_key
        assert hs.ports == ports

    def test_repr(self) -> None:
        """Test string representation."""
        service_id = "abcdef"
        hs = EphemeralHiddenService(service_id=service_id, ports=[(80, "localhost:8080")])

        assert "abcdef.onion" in repr(hs)
        assert "80" in repr(hs)


class TestTorControlClient:
    """Tests for TorControlClient."""

    @pytest.mark.asyncio
    async def test_connect_success(self) -> None:
        """Test successful connection to control port."""
        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(control_host="127.0.0.1", control_port=9051)

            await client.connect()

            assert client.is_connected
            mock_open.assert_called_once()

            await client.close()

    @pytest.mark.asyncio
    async def test_connect_timeout(self) -> None:
        """Test connection timeout handling."""
        with patch("asyncio.open_connection", side_effect=TimeoutError):
            client = TorControlClient(control_host="127.0.0.1", control_port=9051)

            with pytest.raises(TorControlError, match="Timeout"):
                await client.connect()

            assert not client.is_connected

    @pytest.mark.asyncio
    async def test_connect_refused(self) -> None:
        """Test connection refused handling."""
        with patch("asyncio.open_connection", side_effect=OSError("Connection refused")):
            client = TorControlClient(control_host="127.0.0.1", control_port=9051)

            with pytest.raises(TorControlError, match="Failed to connect"):
                await client.connect()

            assert not client.is_connected

    @pytest.mark.asyncio
    async def test_authenticate_cookie_success(self, tmp_path: Path) -> None:
        """Test successful cookie authentication."""
        # Create a mock cookie file
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_data = b"\x01\x02\x03\x04\x05\x06\x07\x08" * 4  # 32 bytes
        cookie_path.write_bytes(cookie_data)

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            # Simulate successful auth response
            mock_reader.readline = AsyncMock(return_value=b"250 OK\r\n")
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            assert client.is_authenticated

            # Verify AUTHENTICATE command was sent with cookie hex
            calls = mock_writer.write.call_args_list
            assert any(b"AUTHENTICATE" in call[0][0] for call in calls)

            await client.close()

    @pytest.mark.asyncio
    async def test_authenticate_cookie_wrong_length(self, tmp_path: Path) -> None:
        """Test that a cookie file with wrong length raises TorAuthenticationError."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"tooshort")  # 8 bytes, not 32

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()

            with pytest.raises(TorAuthenticationError, match="wrong length"):
                await client.authenticate()

            await client.close()

    @pytest.mark.asyncio
    async def test_authenticate_cookie_not_found(self) -> None:
        """Test cookie authentication with missing file."""
        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=Path("/nonexistent/cookie"),
            )

            await client.connect()

            with pytest.raises(TorAuthenticationError, match="not found"):
                await client.authenticate()

            await client.close()

    @pytest.mark.asyncio
    async def test_authenticate_password_success(self) -> None:
        """Test successful password authentication."""
        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            # Simulate successful auth response
            mock_reader.readline = AsyncMock(return_value=b"250 OK\r\n")
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                password="mysecretpassword",
            )

            await client.connect()
            await client.authenticate()

            assert client.is_authenticated

            # Verify password was sent
            calls = mock_writer.write.call_args_list
            assert any(b"mysecretpassword" in call[0][0] for call in calls)

            await client.close()

    @pytest.mark.asyncio
    async def test_authenticate_failure(self, tmp_path: Path) -> None:
        """Test authentication failure handling."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"x" * 32)  # 32 bytes (invalid but correct length)

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            # Simulate auth failure
            mock_reader.readline = AsyncMock(return_value=b"515 Bad authentication\r\n")
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()

            with pytest.raises(TorAuthenticationError, match="failed"):
                await client.authenticate()

            await client.close()

    @pytest.mark.asyncio
    async def test_create_hidden_service_success(self, tmp_path: Path) -> None:
        """Test successful ephemeral hidden service creation."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            # Response sequence: auth OK, then ADD_ONION response
            service_id = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv"
            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"250-ServiceID=" + service_id.encode() + b"\r\n",  # ADD_ONION
                b"250 OK\r\n",  # ADD_ONION final
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            hs = await client.create_ephemeral_hidden_service(ports=[(27183, "127.0.0.1:27183")])

            assert hs.service_id == service_id
            assert hs.onion_address == f"{service_id}.onion"
            assert len(client.hidden_services) == 1

            await client.close()

    @pytest.mark.asyncio
    async def test_create_hidden_service_with_dos_config(self, tmp_path: Path) -> None:
        """Test ephemeral hidden service creation with DoS defense config (Tor 0.4.9.2+)."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            # Response sequence: auth OK, version (for capabilities), config/names, ADD_ONION
            # Use 0.4.9.2 which supports ADD_ONION PoW
            service_id = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv"
            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"250-version=0.4.9.2\r\n",  # GETINFO version (for capabilities)
                b"250 OK\r\n",
                b"250-config/names=HiddenServicePoWDefensesEnabled\r\n",  # GETINFO config/names
                b"250 OK\r\n",
                b"250-ServiceID=" + service_id.encode() + b"\r\n",  # ADD_ONION
                b"250 OK\r\n",
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            dos_config = HiddenServiceDoSConfig(
                intro_dos_enabled=True,
                intro_dos_rate_per_sec=10,
                intro_dos_burst_per_sec=50,
                pow_enabled=True,
                pow_queue_rate=100,
                pow_queue_burst=500,
            )

            hs = await client.create_ephemeral_hidden_service(
                ports=[(27183, "127.0.0.1:27183")],
                dos_config=dos_config,
            )

            assert hs.service_id == service_id

            # Verify ADD_ONION command includes DoS defense options
            calls = mock_writer.write.call_args_list
            add_onion_call = None
            for call in calls:
                if b"ADD_ONION" in call[0][0]:
                    add_onion_call = call[0][0].decode()
                    break

            assert add_onion_call is not None
            # Note: ADD_ONION uses PoW* parameters (only in 0.4.9.2+)
            # Intro DoS defense is not supported for ephemeral hidden services
            assert "PoWDefensesEnabled=1" in add_onion_call
            assert "PoWQueueRate=100" in add_onion_call
            assert "PoWQueueBurst=500" in add_onion_call
            # These should NOT be in the command (not supported for ephemeral HS)
            assert "HiddenServiceEnableIntroDoSDefense" not in add_onion_call
            assert "HiddenServicePoWDefensesEnabled" not in add_onion_call

            await client.close()

    @pytest.mark.asyncio
    async def test_create_hidden_service_with_dos_config_old_tor(self, tmp_path: Path) -> None:
        """Test DoS config warning when Tor doesn't support ADD_ONION PoW (0.4.8.x)."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            # Use 0.4.8.10 which does NOT support ADD_ONION PoW
            service_id = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv"
            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"250-version=0.4.8.10\r\n",  # GETINFO version (for capabilities)
                b"250 OK\r\n",
                b"250-config/names=HiddenServicePoWDefensesEnabled\r\n",  # GETINFO config/names
                b"250 OK\r\n",
                b"250-ServiceID=" + service_id.encode() + b"\r\n",  # ADD_ONION
                b"250 OK\r\n",
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            dos_config = HiddenServiceDoSConfig(
                intro_dos_enabled=True,
                pow_enabled=True,
                pow_queue_rate=100,
                pow_queue_burst=500,
            )

            hs = await client.create_ephemeral_hidden_service(
                ports=[(27183, "127.0.0.1:27183")],
                dos_config=dos_config,
            )

            assert hs.service_id == service_id

            # Verify ADD_ONION command does NOT include PoW params (not supported in 0.4.8)
            calls = mock_writer.write.call_args_list
            add_onion_call = None
            for call in calls:
                if b"ADD_ONION" in call[0][0]:
                    add_onion_call = call[0][0].decode()
                    break

            assert add_onion_call is not None
            # PoW should NOT be in the command for 0.4.8.x
            assert "PoWDefensesEnabled" not in add_onion_call
            assert "PoWQueueRate" not in add_onion_call
            assert "PoWQueueBurst" not in add_onion_call

            await client.close()

    @pytest.mark.asyncio
    async def test_create_hidden_service_failure(self, tmp_path: Path) -> None:
        """Test hidden service creation failure."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"512 Invalid port\r\n",  # ADD_ONION error
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            with pytest.raises(TorHiddenServiceError, match="Failed"):
                await client.create_ephemeral_hidden_service(ports=[(27183, "127.0.0.1:27183")])

            await client.close()

    @pytest.mark.asyncio
    async def test_get_info(self, tmp_path: Path) -> None:
        """Test GETINFO command."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"250-version=0.4.7.10\r\n",  # GETINFO
                b"250 OK\r\n",
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            version = await client.get_version()
            assert version == "0.4.7.10"

            await client.close()

    @pytest.mark.asyncio
    async def test_context_manager(self, tmp_path: Path) -> None:
        """Test async context manager."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            mock_reader.readline = AsyncMock(return_value=b"250 OK\r\n")
            mock_open.return_value = (mock_reader, mock_writer)

            async with TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            ) as client:
                assert client.is_connected
                assert client.is_authenticated

            # After context exit, should be closed
            assert not client.is_connected

    @pytest.mark.asyncio
    async def test_delete_hidden_service(self, tmp_path: Path) -> None:
        """Test DEL_ONION command."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            service_id = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv"
            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"250-ServiceID=" + service_id.encode() + b"\r\n",  # ADD_ONION
                b"250 OK\r\n",  # ADD_ONION final
                b"250 OK\r\n",  # DEL_ONION
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            hs = await client.create_ephemeral_hidden_service(ports=[(27183, "127.0.0.1:27183")])
            assert len(client.hidden_services) == 1

            await client.delete_ephemeral_hidden_service(hs.service_id)
            assert len(client.hidden_services) == 0

            await client.close()

    @pytest.mark.asyncio
    async def test_command_not_authenticated(self) -> None:
        """Test commands fail when not authenticated."""
        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(control_host="127.0.0.1", control_port=9051)

            await client.connect()
            # Don't authenticate

            with pytest.raises(TorControlError, match="Not authenticated"):
                await client.get_info("version")

            with pytest.raises(TorControlError, match="Not authenticated"):
                await client.create_ephemeral_hidden_service(ports=[(80, "localhost:80")])

            await client.close()

    @pytest.mark.asyncio
    async def test_set_config(self, tmp_path: Path) -> None:
        """Test SETCONF command."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"250 OK\r\n",  # SETCONF
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            # Test setting config with various types
            await client.set_config(
                {
                    "HiddenServiceEnableIntroDoSDefense": True,
                    "HiddenServiceEnableIntroDoSRatePerSec": 25,
                    "SomeStringOption": "value with spaces",
                }
            )

            # Verify SETCONF was sent
            calls = mock_writer.write.call_args_list
            assert any(b"SETCONF" in call[0][0] for call in calls)

            await client.close()

    @pytest.mark.asyncio
    async def test_get_capabilities(self, tmp_path: Path) -> None:
        """Test capability detection."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"250-version=0.4.8.10\r\n",  # GETINFO version
                b"250 OK\r\n",
                b"250-config/names=HiddenServicePoWDefensesEnabled\r\n",  # GETINFO config/names
                b"250 OK\r\n",
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            caps = await client.get_capabilities()

            assert caps.version == "0.4.8.10"
            assert caps.has_intro_dos is True
            assert caps.has_pow_module is True

            await client.close()

    @pytest.mark.asyncio
    async def test_configure_dos_defense(self, tmp_path: Path) -> None:
        """Test configuring DoS defenses for hidden service."""
        cookie_path = tmp_path / "control_auth_cookie"
        cookie_path.write_bytes(b"validcookiedatav" * 2)  # exactly 32 bytes

        with patch("asyncio.open_connection") as mock_open:
            mock_reader = AsyncMock()
            mock_writer = MagicMock()
            mock_writer.close = MagicMock()
            mock_writer.wait_closed = AsyncMock()
            mock_writer.write = MagicMock()
            mock_writer.drain = AsyncMock()

            service_id = "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuv"
            responses = [
                b"250 OK\r\n",  # AUTHENTICATE
                b"250-version=0.4.8.10\r\n",  # GETINFO version
                b"250 OK\r\n",
                b"250-config/names=HiddenServicePoWDefensesEnabled\r\n",  # GETINFO config/names
                b"250 OK\r\n",
                b"250 OK\r\n",  # SETCONF
            ]
            mock_reader.readline = AsyncMock(side_effect=responses)
            mock_open.return_value = (mock_reader, mock_writer)

            client = TorControlClient(
                control_host="127.0.0.1",
                control_port=9051,
                cookie_path=cookie_path,
            )

            await client.connect()
            await client.authenticate()

            # Configure DoS defense with PoW enabled
            dos_config = HiddenServiceDoSConfig(
                intro_dos_enabled=True,
                intro_dos_rate_per_sec=10,
                pow_enabled=True,
            )

            await client.configure_hidden_service_dos_defense(
                service_id=service_id,
                config=dos_config,
            )

            # Verify SETCONF was called with DoS settings
            calls = mock_writer.write.call_args_list
            setconf_calls = [c for c in calls if b"SETCONF" in c[0][0]]
            assert len(setconf_calls) > 0

            # Check that intro DoS and PoW settings were included
            setconf_data = setconf_calls[-1][0][0]
            assert b"HiddenServiceEnableIntroDoSDefense" in setconf_data
            assert b"HiddenServicePoWDefensesEnabled" in setconf_data

            await client.close()


async def _authenticated_client(tmp_path: Path, replies: list[bytes]) -> TorControlClient:
    cookie_path = tmp_path / "control_auth_cookie"
    cookie_path.write_bytes(b"validcookiedatav" * 2)
    reader = AsyncMock()
    reader.readline = AsyncMock(side_effect=[b"250 OK\r\n", *replies])
    writer = MagicMock()
    writer.wait_closed = AsyncMock()
    writer.drain = AsyncMock()
    with patch("asyncio.open_connection", return_value=(reader, writer)):
        client = TorControlClient(cookie_path=cookie_path)
        await client.connect()
        await client.authenticate()
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        ([b"250-onions/current=abc\r\n", b"250 OK\r\n"], {"abc"}),
        (
            [b"250+onions/current=\r\n", b"abc\r\n", b"def\r\n", b".\r\n", b"250 OK\r\n"],
            {"abc", "def"},
        ),
        ([b"551 No onion services of the specified type.\r\n"], set()),
    ],
)
async def test_get_ephemeral_hidden_service_ids(
    tmp_path: Path, replies: list[bytes], expected: set[str]
) -> None:
    client = await _authenticated_client(tmp_path, replies)
    assert await client.get_ephemeral_hidden_service_ids() == expected


@pytest.mark.asyncio
async def test_get_ephemeral_hidden_service_ids_detects_closed_connection(
    tmp_path: Path,
) -> None:
    client = await _authenticated_client(tmp_path, [b""])
    with pytest.raises(TorControlError, match="Connection closed by Tor"):
        await client.get_ephemeral_hidden_service_ids()


@pytest.mark.asyncio
async def test_get_ephemeral_hidden_service_ids_reports_filtered_command(
    tmp_path: Path,
) -> None:
    from jmcore.tor_control import TorCommandRejectedError

    client = await _authenticated_client(tmp_path, [b"510 Command filtered\r\n"])
    with pytest.raises(TorCommandRejectedError):
        await client.get_ephemeral_hidden_service_ids()


@pytest.mark.asyncio
async def test_truncated_data_block_fails_instead_of_spinning(tmp_path: Path) -> None:
    client = await _authenticated_client(tmp_path, [b"250+onions/current=\r\n", b"abc\r\n", b""])
    with pytest.raises(TorControlError, match="Connection closed by Tor"):
        await client.get_ephemeral_hidden_service_ids()


@pytest.mark.asyncio
async def test_partial_reply_stops_later_commands_without_closing(tmp_path: Path) -> None:
    import asyncio

    stalled = asyncio.Event()
    replies = [b"250-onions/current=abc\r\n"]

    async def first_line_then_stall() -> bytes:
        if replies:
            return replies.pop(0)
        await stalled.wait()
        return b""

    client = await _authenticated_client(tmp_path, [])
    reader = client._reader
    assert reader is not None
    reader.readline = AsyncMock(side_effect=first_line_then_stall)

    real_wait_for = asyncio.wait_for

    async def short_wait_for(awaitable, timeout):  # type: ignore[no-untyped-def]
        return await real_wait_for(awaitable, timeout=0.01)

    with (
        patch("jmcore.tor_control.asyncio.wait_for", side_effect=short_wait_for),
        pytest.raises(TorControlError, match="Timeout"),
    ):
        await client.get_ephemeral_hidden_service_ids()

    # The trailing "250 OK" of that reply may still arrive; it must not be
    # read as the answer to the next command.
    reader.readline = AsyncMock(return_value=b"250 OK\r\n")
    with pytest.raises(TorControlError, match="out of sync"):
        await client.get_ephemeral_hidden_service_ids()
    assert client.is_connected
    assert client._writer is not None
    client._writer.close.assert_not_called()
