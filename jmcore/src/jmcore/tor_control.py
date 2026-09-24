"""
Tor control port client for creating ephemeral hidden services.

This module provides async interface to Tor's control protocol (spec v1)
for dynamically creating hidden services with cookie authentication.

Reference: https://spec.torproject.org/control-spec/index.html

DoS Defense Options:
    - Introduction Point Rate Limiting: HiddenServiceEnableIntroDoSDefense
    - Proof of Work: HiddenServicePoWDefensesEnabled (Tor 0.4.8+)
    - Max Streams: Limit concurrent streams per circuit

Reference: https://community.torproject.org/onion-services/advanced/dos/
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger


class TorControlError(Exception):
    """Base exception for Tor control errors."""

    pass


class TorAuthenticationError(TorControlError):
    """Authentication with Tor control port failed."""

    pass


class TorHiddenServiceError(TorControlError):
    """Failed to create or manage hidden service."""

    pass


class TorCommandRejectedError(TorControlError):
    """Tor (or a control-port filter) answered a command with an error status."""

    pass


@dataclass
class HiddenServiceDoSConfig:
    """
    Configuration for Tor hidden service DoS defenses.

    These settings control Tor-level DoS protection. Note that different features
    are available depending on whether you use ephemeral or persistent hidden services:

    - **Ephemeral HS** (ADD_ONION, used by JoinMarket makers for privacy):
      - PoW defense: Requires Tor 0.4.9.2+ (auto-enabled when available)
      - Intro point rate limiting: NOT supported (Tor protocol limitation)
      - MaxStreams: Supported on all versions

    - **Persistent HS** (torrc HiddenServiceDir):
      - PoW defense: Requires Tor 0.4.8+ with --enable-gpl
      - Intro point rate limiting: Supported since Tor 0.4.2+
      - MaxStreams: Supported on all versions

    Attributes:
        intro_dos_enabled: Enable rate limiting at introduction points.
            **Only works for persistent HS defined in torrc, not ephemeral HS.**
            Disabled by default since JoinMarket uses ephemeral HS.
        intro_dos_rate_per_sec: Allowed introduction rate per second at intro point.
            Default 25 provides reasonable protection while allowing legitimate traffic.
        intro_dos_burst_per_sec: Allowed introduction burst per second at intro point.
            Should be >= intro_dos_rate_per_sec. Default 200.
        pow_enabled: Enable Proof-of-Work defense.
            Clients must solve a computational puzzle to connect, making flooding expensive.
            The effort required auto-adjusts based on queue depth (starts at 0, scales up
            under attack). Enabled by default. For ephemeral HS, requires Tor 0.4.9.2+.
        pow_queue_rate: Rate to process rendezvous requests from PoW queue (per second).
            Lower values provide more protection but may delay legitimate connections.
        pow_queue_burst: Burst size for processing from PoW queue.
        max_streams: Maximum concurrent streams per rendezvous circuit.
            None for unlimited. Works on all Tor versions.
        max_streams_close_circuit: If True, exceeding max_streams tears down the circuit.
            If False, excess stream requests are silently ignored.
    """

    # Introduction Point rate limiting (Tor 0.4.2+, persistent HS only)
    # Disabled by default since ephemeral HS (used by JoinMarket) don't support this
    intro_dos_enabled: bool = False
    intro_dos_rate_per_sec: int = 25
    intro_dos_burst_per_sec: int = 200

    # Proof of Work defense (Tor 0.4.8+, requires --enable-gpl build)
    # Enabled by default - suggested effort starts at 0 (no puzzle required) and
    # auto-scales under attack. Older Tor versions without PoW support will simply
    # ignore this setting gracefully.
    pow_enabled: bool = True
    pow_queue_rate: int = 25  # Rendezvous requests processed per second
    pow_queue_burst: int = 250  # Burst size for PoW queue

    # Stream limits per circuit
    max_streams: int | None = None
    max_streams_close_circuit: bool = True


@dataclass
class TorCapabilities:
    """
    Tor daemon capabilities detected at runtime.

    Used to determine which DoS defense features are available.

    Note on ADD_ONION vs SETCONF:
        - PoW defense via SETCONF/torrc: Available since 0.4.8
        - PoW defense via ADD_ONION: Available since 0.4.9.2-alpha
        - Intro DoS defense: Only available via SETCONF/torrc for persistent HS
    """

    version: str = ""
    has_pow_module: bool = False
    has_intro_dos: bool = False
    # ADD_ONION gained PoW support in 0.4.9.2-alpha
    has_add_onion_pow: bool = False
    # Version tuple for comparison (major, minor, patch)
    version_tuple: tuple[int, int, int] = field(default_factory=lambda: (0, 0, 0))

    @classmethod
    def from_version(cls, version_str: str) -> TorCapabilities:
        """Parse Tor version string and determine capabilities."""
        caps = cls(version=version_str)
        parts: list[str] = []

        # Parse version like "0.4.8.10" or "0.4.8.1-alpha" or "0.4.9.2-alpha"
        try:
            # Remove any suffix like "-alpha", "-rc"
            version_clean = version_str.split("-")[0]
            parts = version_clean.split(".")
            if len(parts) >= 3:
                caps.version_tuple = (int(parts[0]), int(parts[1]), int(parts[2]))
        except (ValueError, IndexError):
            logger.warning(f"Could not parse Tor version: {version_str}")

        # Intro DoS defense: available since 0.4.2 (but only for persistent HS via torrc)
        caps.has_intro_dos = caps.version_tuple >= (0, 4, 2)

        # PoW defense via SETCONF/torrc: available since 0.4.8 (requires --enable-gpl build)
        # We'll detect actual pow module availability separately
        caps.has_pow_module = caps.version_tuple >= (0, 4, 8)

        # PoW defense via ADD_ONION: only available since 0.4.9.2
        # See: https://spec.torproject.org/control-spec/commands.html
        # "PoWDefensesEnabled, PoWQueueRate and PoWQueueBurst support added 0.4.9.2-alpha"
        if len(parts) >= 4:
            try:
                patch = int(parts[3].split("-")[0])  # Handle "2-alpha"
                caps.has_add_onion_pow = caps.version_tuple >= (0, 4, 9) and (
                    caps.version_tuple > (0, 4, 9) or patch >= 2
                )
            except (ValueError, IndexError):
                # If we can't parse the patch version, be conservative
                caps.has_add_onion_pow = caps.version_tuple > (0, 4, 9)
        else:
            caps.has_add_onion_pow = caps.version_tuple > (0, 4, 9)

        return caps


class EphemeralHiddenService:
    """
    Represents an ephemeral hidden service created via Tor control port.

    Ephemeral hidden services are transient - they exist only while
    the control connection is open. When the connection closes,
    the hidden service is automatically removed.
    """

    def __init__(
        self,
        service_id: str,
        private_key: str | None = None,
        ports: list[tuple[int, str]] | None = None,
    ):
        """
        Initialize ephemeral hidden service info.

        Args:
            service_id: The .onion address without .onion suffix (56 chars for v3)
            private_key: Optional private key for recreating the service
            ports: List of (virtual_port, target) mappings
        """
        self.service_id = service_id
        self.private_key = private_key
        self.ports = ports or []

    @property
    def onion_address(self) -> str:
        """Get the full .onion address."""
        return f"{self.service_id}.onion"

    def __repr__(self) -> str:
        return f"EphemeralHiddenService({self.onion_address}, ports={self.ports})"


class TorControlClient:
    """
    Async client for Tor control protocol.

    Supports cookie authentication and ephemeral hidden service creation.
    The client maintains a persistent connection to control port.

    Example:
        async with TorControlClient() as client:
            hs = await client.create_ephemeral_hidden_service(
                ports=[(8765, "127.0.0.1:8765")]
            )
            print(f"Hidden service: {hs.onion_address}")
            # Service exists while connection is open
        # Service removed when context exits
    """

    def __init__(
        self,
        control_host: str = "127.0.0.1",
        control_port: int = 9051,
        cookie_path: str | Path | None = None,
        password: str | None = None,
    ):
        """
        Initialize Tor control client.

        Args:
            control_host: Tor control port host
            control_port: Tor control port number
            cookie_path: Path to cookie auth file (usually /var/lib/tor/control_auth_cookie)
            password: Optional password for HASHEDPASSWORD auth (not recommended)
        """
        self.control_host = control_host
        self.control_port = control_port
        self.cookie_path = Path(cookie_path) if cookie_path else None
        self.password = password

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False
        self._authenticated = False
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        # Set when a reply was only partly read. Later replies could then be
        # attributed to the wrong command, so the connection stops accepting
        # commands. The socket stays open: closing it would drop every
        # non-detached onion service it owns.
        self._out_of_sync = False

        # Track created hidden services for cleanup
        self._hidden_services: list[EphemeralHiddenService] = []

    async def __aenter__(self) -> TorControlClient:
        """Async context manager entry - connect and authenticate."""
        await self.connect()
        await self.authenticate()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Async context manager exit - close connection."""
        await self.close()

    async def connect(self) -> None:
        """Connect to Tor control port."""
        if self._connected:
            return

        try:
            logger.bind(sensitive=True).debug(
                f"Connecting to Tor control port {self.control_host}:{self.control_port}"
            )
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.control_host, self.control_port),
                timeout=10.0,
            )
            self._connected = True
            self._out_of_sync = False
            logger.bind(sensitive=True).info(
                f"Connected to Tor control port at {self.control_host}:{self.control_port}"
            )
        except TimeoutError as e:
            raise TorControlError(
                f"Timeout connecting to Tor control port at {self.control_host}:{self.control_port}"
            ) from e
        except OSError as e:
            raise TorControlError(
                f"Failed to connect to Tor control port at "
                f"{self.control_host}:{self.control_port}: {e}"
            ) from e

    async def close(self) -> None:
        """Close connection to Tor control port."""
        if not self._connected:
            return

        self._connected = False
        self._authenticated = False
        self._hidden_services.clear()

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None

        logger.debug("Closed Tor control connection")

    async def _send_command(self, command: str) -> None:
        """Send a command to Tor control port."""
        if not self._connected or not self._writer:
            raise TorControlError("Not connected to Tor control port")
        if self._out_of_sync:
            raise TorControlError("Tor control connection is out of sync")

        async with self._write_lock:
            logger.trace("Tor control command sent")
            self._writer.write(f"{command}\r\n".encode())
            await self._writer.drain()

    async def _read_response(self) -> list[tuple[str, str, str]]:
        """
        Read response from Tor control port.

        Returns:
            List of (status_code, separator, message) tuples.
            Separator is '-' for multi-line, ' ' for last/single line, '+' for data.
        """
        if not self._connected or not self._reader:
            raise TorControlError("Not connected to Tor control port")

        async with self._read_lock:
            try:
                return await self._read_reply_lines()
            except TorControlError:
                self._out_of_sync = True
                raise

    async def _read_line(self) -> str:
        assert self._reader is not None
        try:
            line = await asyncio.wait_for(self._reader.readline(), timeout=30.0)
        except TimeoutError as e:
            raise TorControlError("Timeout reading from Tor control port") from e
        if not line:
            raise TorControlError("Connection closed by Tor")
        return line.decode("utf-8").rstrip("\r\n")

    async def _read_reply_lines(self) -> list[tuple[str, str, str]]:
        responses: list[tuple[str, str, str]] = []
        while True:
            line_str = await self._read_line()
            logger.trace("Tor control response received")

            if len(line_str) < 4:
                raise TorControlError(f"Invalid response format: {line_str}")

            status_code = line_str[:3]
            separator = line_str[3]
            message = line_str[4:]

            responses.append((status_code, separator, message))

            # Handle multi-line data responses (status+data)
            if separator == "+":
                # Read until we see a line with just "."
                data_lines: list[str] = []
                while (data_str := await self._read_line()) != ".":
                    data_lines.append(data_str)
                # Store data as message content
                responses[-1] = (status_code, separator, "\n".join(data_lines))

            # Single line or last line of multi-line response
            if separator == " ":
                return responses

    async def _command(self, command: str) -> list[tuple[str, str, str]]:
        """Send command and read response."""
        await self._send_command(command)
        return await self._read_response()

    def _check_success(
        self, responses: list[tuple[str, str, str]], expected_code: str = "250"
    ) -> None:
        """Check if response indicates success."""
        if not responses:
            raise TorControlError("Empty response from Tor")

        # Check the last response (final status)
        status_code, _, message = responses[-1]
        if status_code != expected_code:
            raise TorCommandRejectedError(f"Tor command failed: {status_code} {message}")

    async def authenticate(self) -> None:
        """
        Authenticate with Tor control port.

        Tries cookie authentication first if cookie_path is set,
        then falls back to password if provided.
        """
        if self._authenticated:
            return

        if not self._connected:
            await self.connect()

        # Try cookie authentication
        if self.cookie_path:
            await self._authenticate_cookie()
            return

        # Try password authentication
        if self.password:
            await self._authenticate_password()
            return

        # Try null authentication (for permissive configs)
        try:
            responses = await self._command("AUTHENTICATE")
            self._check_success(responses)
            self._authenticated = True
            logger.info("Authenticated with Tor (null auth)")
        except TorControlError as e:
            raise TorAuthenticationError(
                "No authentication method configured. Provide cookie_path or password."
            ) from e

    async def _authenticate_cookie(self) -> None:
        """Authenticate using cookie file."""
        if not self.cookie_path:
            raise TorAuthenticationError("Cookie path not configured")

        try:
            cookie_data = self.cookie_path.read_bytes()
        except FileNotFoundError as e:
            raise TorAuthenticationError(f"Cookie file not found: {self.cookie_path}") from e
        except PermissionError as e:
            raise TorAuthenticationError(
                f"Permission denied reading cookie file: {self.cookie_path}"
            ) from e

        # Tor auth cookies are always exactly 32 bytes. A shorter cookie (e.g. 0 bytes)
        # means the file was partially written — Tor will reject it with "wrong length".
        expected_cookie_bytes = 32
        if len(cookie_data) != expected_cookie_bytes:
            raise TorAuthenticationError(
                f"Cookie file has wrong length ({len(cookie_data)}), "
                f"expected {expected_cookie_bytes}. "
                f"Tor may still be starting up: {self.cookie_path}"
            )

        cookie_hex = cookie_data.hex()
        try:
            responses = await self._command(f"AUTHENTICATE {cookie_hex}")
            self._check_success(responses)
            self._authenticated = True
            logger.info("Authenticated with Tor using cookie")
        except TorControlError as e:
            raise TorAuthenticationError(f"Cookie authentication failed: {e}") from e

    async def _authenticate_password(self) -> None:
        """Authenticate using password."""
        if not self.password:
            raise TorAuthenticationError("Password not configured")

        # Quote the password properly
        escaped_password = self.password.replace("\\", "\\\\").replace('"', '\\"')

        try:
            responses = await self._command(f'AUTHENTICATE "{escaped_password}"')
            self._check_success(responses)
            self._authenticated = True
            logger.info("Authenticated with Tor using password")
        except TorControlError as e:
            raise TorAuthenticationError(f"Password authentication failed: {e}") from e

    async def get_info(self, key: str) -> str:
        """
        Get information from Tor.

        Args:
            key: Information key (e.g., "version", "config-file")

        Returns:
            The requested information value
        """
        if not self._authenticated:
            raise TorControlError("Not authenticated")

        responses = await self._command(f"GETINFO {key}")
        self._check_success(responses)

        # Parse key=value from first response
        for status, _, message in responses:
            if status == "250" and "=" in message:
                _, value = message.split("=", 1)
                return value

        raise TorControlError(f"Could not parse GETINFO response for {key}")

    async def create_ephemeral_hidden_service(
        self,
        ports: list[tuple[int, str]],
        key_type: str = "NEW",
        key_blob: str = "ED25519-V3",
        discard_pk: bool = False,
        detach: bool = False,
        await_publication: bool = False,
        max_streams: int | None = None,
        dos_config: HiddenServiceDoSConfig | None = None,
    ) -> EphemeralHiddenService:
        """
        Create an ephemeral hidden service using ADD_ONION.

        Ephemeral services exist only while the control connection is open.
        When the connection closes, the hidden service is automatically removed.

        Args:
            ports: List of (virtual_port, target) tuples.
                   Target is "host:port" or just "port" for localhost.
            key_type: "NEW" for new key, "ED25519-V3" or "RSA1024" for existing key
            key_blob: For NEW: "ED25519-V3" (recommended) or "RSA1024"
                      For existing: base64-encoded private key
            discard_pk: If True, don't return the private key
            detach: If True, service persists after control connection closes
            await_publication: If True, wait for HS descriptor to be published
            max_streams: Maximum concurrent streams (None for unlimited)
            dos_config: DoS defense configuration (intro rate limiting, PoW, etc.)

        Returns:
            EphemeralHiddenService with the created service details

        Example:
            # Create service that forwards port 80 to local 8080
            hs = await client.create_ephemeral_hidden_service(
                ports=[(80, "127.0.0.1:8080")]
            )
        """
        if not self._authenticated:
            raise TorControlError("Not authenticated")

        # Build port specifications
        port_specs = []
        for virtual_port, target in ports:
            port_specs.append(f"Port={virtual_port},{target}")

        # Build flags
        flags = []
        if discard_pk:
            flags.append("DiscardPK")
        if detach:
            flags.append("Detach")
        if await_publication:
            flags.append("AwaitPublication")

        # Build command
        cmd_parts = [f"ADD_ONION {key_type}:{key_blob}"]
        cmd_parts.extend(port_specs)

        if flags:
            cmd_parts.append(f"Flags={','.join(flags)}")

        if max_streams is not None:
            cmd_parts.append(f"MaxStreams={max_streams}")

        # Add DoS defense options if configured
        # NOTE: ADD_ONION PoW support was added in Tor 0.4.9.2-alpha
        # Intro DoS defense (HiddenServiceEnableIntroDoSDefense) only works for persistent
        # hidden services defined in torrc or via SETCONF with HiddenServiceDir.
        if dos_config is not None:
            caps = await self.get_capabilities()

            # Introduction point rate limiting is NOT supported for ephemeral hidden services
            # This is expected - ephemeral HS are the normal/preferred mode for privacy.
            # Only log at debug level since this isn't actionable for most users.
            if dos_config.intro_dos_enabled:
                logger.debug(
                    "Note: Intro point rate limiting (HiddenServiceEnableIntroDoSDefense) is "
                    "not available for ephemeral hidden services. This is a Tor protocol "
                    "limitation and doesn't affect PoW defense which IS supported."
                )

            # Proof of Work defense via ADD_ONION (Tor 0.4.9.2+)
            # ADD_ONION uses different parameter names: PoWDefensesEnabled, PoWQueueRate, etc.
            # Note: PoW config options exist in 0.4.8 but ADD_ONION support was only added in 0.4.9.2
            if dos_config.pow_enabled and caps.has_add_onion_pow:
                cmd_parts.append("PoWDefensesEnabled=1")
                cmd_parts.append(f"PoWQueueRate={dos_config.pow_queue_rate}")
                cmd_parts.append(f"PoWQueueBurst={dos_config.pow_queue_burst}")
                logger.info(
                    f"Enabling PoW defense: queue_rate={dos_config.pow_queue_rate}/s, "
                    f"queue_burst={dos_config.pow_queue_burst}"
                )
            elif dos_config.pow_enabled and caps.has_pow_module and not caps.has_add_onion_pow:
                logger.warning(
                    f"PoW defense requested but Tor {caps.version} doesn't support it via "
                    f"ADD_ONION (requires 0.4.9.2+). PoW for ephemeral hidden services is not "
                    f"available in this Tor version. Configure a persistent hidden service in "
                    f"torrc if you need PoW protection."
                )
            elif dos_config.pow_enabled and not caps.has_pow_module:
                logger.warning(
                    f"PoW defense requested but Tor {caps.version} doesn't support it "
                    f"(requires 0.4.8+ with --enable-gpl for config, 0.4.9.2+ for ADD_ONION)"
                )

        command = " ".join(cmd_parts)

        try:
            responses = await self._command(command)
            self._check_success(responses)
        except TorControlError as e:
            raise TorHiddenServiceError(f"Failed to create hidden service: {e}") from e

        # Parse response to get service ID and optional private key
        service_id: str | None = None
        private_key: str | None = None

        for status, _, message in responses:
            if status == "250":
                if message.startswith("ServiceID="):
                    service_id = message.split("=", 1)[1]
                elif message.startswith("PrivateKey="):
                    private_key = message.split("=", 1)[1]

        if not service_id:
            raise TorHiddenServiceError("No ServiceID in ADD_ONION response")

        hs = EphemeralHiddenService(
            service_id=service_id,
            private_key=private_key,
            ports=list(ports),
        )

        if not detach:
            self._hidden_services.append(hs)

        logger.bind(sensitive=True).info(f"Created ephemeral hidden service: {hs.onion_address}")
        return hs

    async def delete_ephemeral_hidden_service(self, service_id: str) -> None:
        """
        Delete an ephemeral hidden service.

        Args:
            service_id: The service ID (without .onion suffix)
        """
        if not self._authenticated:
            raise TorControlError("Not authenticated")

        # Strip .onion if included
        if service_id.endswith(".onion"):
            service_id = service_id[:-6]

        try:
            responses = await self._command(f"DEL_ONION {service_id}")
            self._check_success(responses)
            logger.bind(sensitive=True).info(f"Deleted hidden service: {service_id}")
        except TorControlError as e:
            raise TorHiddenServiceError(f"Failed to delete hidden service: {e}") from e

        # Remove from tracking
        self._hidden_services = [hs for hs in self._hidden_services if hs.service_id != service_id]

    async def get_ephemeral_hidden_service_ids(self) -> set[str]:
        """Return the ephemeral onion service IDs owned by this control connection.

        Non-detached services created with ``ADD_ONION`` are listed here only
        while this control connection stays open, so this doubles as a check
        that the connection and its services are still alive. Raises
        ``TorCommandRejectedError`` when Tor or a control-port filter refuses
        the query, and ``TorControlError`` when the connection is closed or
        unresponsive.
        """
        if not self._authenticated:
            raise TorControlError("Not authenticated")

        responses = await self._command("GETINFO onions/current")
        status_code, _, message = responses[-1]
        if status_code == "551" and "No onion services" in message:
            return set()
        self._check_success(responses)

        service_ids: set[str] = set()
        for status, separator, line in responses:
            if status != "250":
                continue
            if separator == "+":
                # Multi-line data block: one service ID per line.
                service_ids.update(item.strip() for item in line.splitlines() if item.strip())
            elif line.startswith("onions/current="):
                value = line.split("=", 1)[1].strip()
                if value:
                    service_ids.add(value)
        return service_ids

    async def get_version(self) -> str:
        """Get Tor version string."""
        return await self.get_info("version")

    async def get_capabilities(self) -> TorCapabilities:
        """
        Detect Tor daemon capabilities.

        Returns:
            TorCapabilities with detected features
        """
        version = await self.get_version()
        caps = TorCapabilities.from_version(version)

        # Try to detect actual PoW module availability
        if caps.version_tuple >= (0, 4, 8):
            try:
                # Check if pow module is compiled in by trying to get its status
                # This will fail gracefully if not available
                modules_info = await self.get_info("config/names")
                caps.has_pow_module = "HiddenServicePoW" in modules_info
            except TorControlError:
                # Can't detect, assume available if version supports it
                pass

        logger.debug(
            f"Tor capabilities: version={caps.version}, "
            f"intro_dos_torrc={caps.has_intro_dos}, pow_torrc={caps.has_pow_module}, "
            f"pow_add_onion={caps.has_add_onion_pow}"
        )
        return caps

    async def set_config(self, settings: dict[str, str | int | bool]) -> None:
        """
        Set Tor configuration options using SETCONF.

        Args:
            settings: Dictionary of config option -> value pairs

        Raises:
            TorControlError: If setting configuration fails
        """
        if not self._authenticated:
            raise TorControlError("Not authenticated")

        if not settings:
            return

        # Build SETCONF command with proper quoting
        parts = []
        for key, value in settings.items():
            if isinstance(value, bool):
                parts.append(f"{key}={1 if value else 0}")
            elif isinstance(value, int):
                parts.append(f"{key}={value}")
            else:
                # Quote string values
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'{key}="{escaped}"')

        command = "SETCONF " + " ".join(parts)

        try:
            responses = await self._command(command)
            self._check_success(responses)
            logger.debug(f"Set Tor config: {list(settings.keys())}")
        except TorControlError as e:
            raise TorControlError(f"Failed to set config: {e}") from e

    async def configure_hidden_service_dos_defense(
        self,
        service_id: str,
        config: HiddenServiceDoSConfig,
    ) -> None:
        """
        Configure DoS defenses for a PERSISTENT hidden service defined in torrc.

        This applies introduction point rate limiting and optionally PoW defense
        using SETCONF commands. These settings take effect immediately.

        IMPORTANT: This method does NOT work for ephemeral hidden services created
        via ADD_ONION. For ephemeral services, pass the dos_config parameter to
        create_ephemeral_hidden_service() instead.

        Args:
            service_id: The hidden service ID (without .onion suffix)
            config: DoS defense configuration

        Raises:
            TorControlError: If configuration fails (e.g., for ephemeral services)
            TorHiddenServiceError: If the service doesn't exist
        """
        if not self._authenticated:
            raise TorControlError("Not authenticated")

        # Strip .onion if included
        if service_id.endswith(".onion"):
            service_id = service_id[:-6]

        # Get capabilities to check what's supported
        caps = await self.get_capabilities()

        settings: dict[str, str | int | bool] = {}

        # Introduction point rate limiting (available since Tor 0.4.2)
        if caps.has_intro_dos and config.intro_dos_enabled:
            settings["HiddenServiceEnableIntroDoSDefense"] = True
            settings["HiddenServiceEnableIntroDoSRatePerSec"] = config.intro_dos_rate_per_sec
            settings["HiddenServiceEnableIntroDoSBurstPerSec"] = config.intro_dos_burst_per_sec
            logger.info(
                f"Enabling intro point DoS defense: rate={config.intro_dos_rate_per_sec}/s, "
                f"burst={config.intro_dos_burst_per_sec}/s"
            )
        elif config.intro_dos_enabled and not caps.has_intro_dos:
            logger.warning(
                f"Intro point DoS defense requested but Tor {caps.version} doesn't support it "
                f"(requires 0.4.2+)"
            )

        # Proof of Work defense (available since Tor 0.4.8 with pow module)
        if caps.has_pow_module and config.pow_enabled:
            settings["HiddenServicePoWDefensesEnabled"] = True
            settings["HiddenServicePoWQueueRate"] = config.pow_queue_rate
            settings["HiddenServicePoWQueueBurst"] = config.pow_queue_burst
            logger.info(
                f"Enabling PoW defense: queue_rate={config.pow_queue_rate}/s, "
                f"queue_burst={config.pow_queue_burst}"
            )
        elif config.pow_enabled and not caps.has_pow_module:
            logger.warning(
                f"PoW defense requested but Tor {caps.version} doesn't support it "
                f"(requires 0.4.8+ with --enable-gpl)"
            )

        # Stream limits (applied at service creation, log for reference)
        if config.max_streams is not None:
            logger.debug(
                f"Max streams per circuit: {config.max_streams} "
                f"(close_circuit={config.max_streams_close_circuit})"
            )

        if settings:
            await self.set_config(settings)
            logger.bind(sensitive=True).info(
                f"DoS defenses configured for hidden service {service_id[:8]}..."
            )
        else:
            logger.debug("No DoS defense settings to apply")

    @property
    def is_connected(self) -> bool:
        """Check if connected to control port."""
        return self._connected

    @property
    def is_authenticated(self) -> bool:
        """Check if authenticated."""
        return self._authenticated

    @property
    def hidden_services(self) -> list[EphemeralHiddenService]:
        """Get list of active ephemeral hidden services created by this client."""
        return list(self._hidden_services)
