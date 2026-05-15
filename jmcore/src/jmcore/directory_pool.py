"""
Shared directory-client pool primitives for maker and taker.

This module provides :class:`DirectoryClientPool`, a base class that owns
the boilerplate around connecting to one or more directory servers over Tor:

* Parsing a configured ``"host:port"`` server string into a canonical
  ``node_id`` (used as dict key in both maker and taker).
* Wiring up SOCKS stream isolation credentials when enabled.
* Building, connecting, and tearing down :class:`DirectoryClient` instances.
* A bounded ``connect_all_with_retry`` loop suitable for startup, where Tor
  may still be bootstrapping circuits, plus a reusable
  ``reconnect_disconnected`` cycle for periodic background reconnects.

Component-specific behavior (post-connect offer announcement for makers,
direct-peer connections and nick tracking for takers) is layered on by
subclassing and overriding hooks:

* :meth:`_build_client_kwargs` -- supply per-component constructor kwargs
  for ``DirectoryClient`` (e.g. ``location``, ``neutrino_compat``).
* :meth:`_on_directory_connected` -- run after each successful connect
  (e.g. start a listener task, announce offers).
* :meth:`_on_directory_disconnected` -- cleanup hook.

The pool deliberately stays unaware of higher-level concerns like message
dispatch, response correlation, or direct peer connections; those continue
to live in the subclasses that own them.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient
from jmcore.tasks import parse_directory_address
from jmcore.tor_isolation import IsolationCategory, get_isolation_credentials


class DirectoryClientPool:
    """
    Manages a pool of :class:`DirectoryClient` connections over Tor.

    Subclasses customize per-connection construction by overriding
    :meth:`_build_client_kwargs`, and react to lifecycle transitions by
    overriding :meth:`_on_directory_connected` /
    :meth:`_on_directory_disconnected`.

    Attributes:
        directory_servers: Configured "host:port" addresses to connect to.
        network: Bitcoin network ("mainnet", "testnet", "signet", "regtest").
        nick_identity: Long-lived identity used for directory handshake.
        socks_host, socks_port: Tor SOCKS proxy.
        connection_timeout: Per-connection handshake timeout, in seconds.
        stream_isolation: When True, use distinct SOCKS credentials per
            isolation category (directory, peer) so Tor opens fresh
            circuits and the directories cannot link our flows.
        clients: Connected clients keyed by canonical ``"host:port"``
            ``node_id``. Subclasses may iterate this dict but should not
            mutate it directly; use :meth:`connect_all_with_retry`,
            :meth:`reconnect_disconnected`, or :meth:`close_all`.
    """

    def __init__(
        self,
        *,
        directory_servers: list[str],
        network: str,
        nick_identity: NickIdentity,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
        connection_timeout: float = 120.0,
        stream_isolation: bool = False,
    ):
        self.directory_servers = directory_servers
        self.network = network
        self.nick_identity = nick_identity
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.connection_timeout = connection_timeout
        self.stream_isolation = stream_isolation

        # Pre-compute isolation credentials (None when disabled). The peer
        # credentials are exposed for subclasses that establish direct
        # onion-to-onion connections (taker), even though the pool itself
        # only uses _dir_creds.
        self._dir_creds: tuple[str | None, str | None] = (None, None)
        self._peer_creds: tuple[str | None, str | None] = (None, None)
        if stream_isolation:
            dir_c = get_isolation_credentials(IsolationCategory.DIRECTORY)
            self._dir_creds = (dir_c.username, dir_c.password)
            peer_c = get_isolation_credentials(IsolationCategory.PEER)
            self._peer_creds = (peer_c.username, peer_c.password)

        self.clients: dict[str, DirectoryClient] = {}

    # -- Hooks ----------------------------------------------------------

    def _build_client_kwargs(self, host: str, port: int) -> dict[str, Any]:
        """
        Return ``DirectoryClient`` constructor kwargs for one server.

        Subclasses may override to inject component-specific arguments
        such as ``location`` (maker's onion host:port) or
        ``neutrino_compat``. The base implementation supplies only the
        universally required arguments.
        """
        return {
            "host": host,
            "port": port,
            "network": self.network,
            "nick_identity": self.nick_identity,
            "socks_host": self.socks_host,
            "socks_port": self.socks_port,
            "timeout": self.connection_timeout,
            "socks_username": self._dir_creds[0],
            "socks_password": self._dir_creds[1],
        }

    async def _on_directory_connected(self, node_id: str, client: DirectoryClient) -> None:
        """
        Called after a successful connect for ``node_id``.

        Subclasses may override to start a listener task, announce
        offers, or emit notifications. The default is a no-op.
        """

    async def _on_directory_disconnected(self, node_id: str) -> None:
        """Called when a directory has been removed from the pool. Default no-op."""

    # -- Single-server connect ------------------------------------------

    async def connect_to_directory(self, dir_server: str) -> tuple[str, DirectoryClient] | None:
        """
        Connect to a single directory server.

        Args:
            dir_server: Address as ``"host"`` or ``"host:port"``.

        Returns:
            ``(node_id, client)`` on success, or ``None`` if the address
            could not be parsed or the connect/handshake failed. The
            returned client is not yet stored in ``self.clients``; the
            caller (or :meth:`connect_all_with_retry` /
            :meth:`reconnect_disconnected`) handles registration.
        """
        try:
            host, port = parse_directory_address(dir_server)
        except Exception as e:
            logger.debug(f"Cannot parse directory address {dir_server!r}: {e}")
            return None

        node_id = f"{host}:{port}"

        try:
            kwargs = self._build_client_kwargs(host, port)
            client = DirectoryClient(**kwargs)
            await client.connect()
            return (node_id, client)
        except Exception as e:
            logger.debug(f"Failed to connect to {dir_server}: {e}")
            return None

    # -- Bulk connect / reconnect ---------------------------------------

    async def connect_all_parallel(self) -> int:
        """
        Connect to every configured server in parallel (single pass).

        On success, the client is added to ``self.clients`` and the
        ``_on_directory_connected`` hook fires. Suitable for callers
        that do not want a retry loop (e.g. taker, which only runs once
        per CoinJoin).

        Returns the number of successful connections.
        """
        tasks = [self.connect_to_directory(server) for server in self.directory_servers]
        results = await asyncio.gather(*tasks)

        connected = 0
        for result in results:
            if result is None:
                continue
            node_id, client = result
            self.clients[node_id] = client
            await self._on_directory_connected(node_id, client)
            connected += 1
        return connected

    async def connect_all_with_retry(
        self,
        *,
        timeout: float,
        initial_delay: float = 5.0,
        max_delay: float = 30.0,
        backoff: float = 1.5,
    ) -> int:
        """
        Connect to all configured servers with bounded retry.

        Tor may still be bootstrapping when the caller starts; this loop
        keeps retrying failed servers until either at least one client
        connects or ``timeout`` elapses, whichever comes first. After
        timeout the method returns gracefully so callers can rely on a
        separate periodic reconnect task.

        Args:
            timeout: Hard deadline in seconds across all attempts.
            initial_delay: Sleep between full retry passes when no
                connection has succeeded yet.
            max_delay: Cap on the exponentially-backed-off delay.
            backoff: Multiplier applied to ``initial_delay`` per pass.

        Returns:
            Number of clients currently in ``self.clients`` when the
            method returns.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        delay = initial_delay
        attempt = 0

        while True:
            attempt += 1
            for dir_server in self.directory_servers:
                # Compute the node_id for the "already connected" check
                # without raising if the address is malformed; we'll
                # rely on connect_to_directory to log the parse failure.
                try:
                    host, port = parse_directory_address(dir_server)
                    node_id = f"{host}:{port}"
                except Exception:
                    node_id = dir_server

                if node_id in self.clients:
                    continue

                result = await self.connect_to_directory(dir_server)
                if result is None:
                    logger.warning(
                        f"Could not connect to {dir_server} (attempt {attempt}), "
                        "Tor may still be bootstrapping"
                    )
                    continue
                connected_id, client = result
                self.clients[connected_id] = client
                logger.info(f"Connected to directory: {dir_server}")
                await self._on_directory_connected(connected_id, client)

            if self.clients:
                return len(self.clients)

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                logger.error(
                    f"Failed to connect to any directory server after {timeout}s. "
                    "Caller should rely on the periodic reconnect task."
                )
                return 0

            wait = min(delay, remaining)
            logger.info(f"Retrying directory connections in {wait:.0f}s...")
            await asyncio.sleep(wait)
            delay = min(delay * backoff, max_delay)

    def list_disconnected(self) -> list[tuple[str, str]]:
        """
        Return ``(dir_server, node_id)`` pairs for configured servers
        not currently in ``self.clients``.

        Servers whose addresses fail to parse are skipped (with a debug
        log) rather than reported as disconnected, since the user almost
        certainly mistyped them and re-trying won't help.
        """
        connected = set(self.clients.keys())
        out: list[tuple[str, str]] = []
        for server in self.directory_servers:
            try:
                host, port = parse_directory_address(server)
            except Exception as e:
                logger.debug(f"Skipping unparseable directory {server!r}: {e}")
                continue
            node_id = f"{host}:{port}"
            if node_id not in connected:
                out.append((server, node_id))
        return out

    async def reconnect_disconnected(self) -> list[tuple[str, DirectoryClient]]:
        """
        Attempt to reconnect to every configured-but-disconnected server.

        Newly connected clients are added to ``self.clients`` and
        ``_on_directory_connected`` fires for each. The return value is
        the list of ``(node_id, client)`` for callers that need to do
        additional per-reconnect work (e.g. start a listener task).
        """
        newly_connected: list[tuple[str, DirectoryClient]] = []
        for dir_server, _expected_node_id in self.list_disconnected():
            result = await self.connect_to_directory(dir_server)
            if result is None:
                continue
            node_id, client = result
            self.clients[node_id] = client
            logger.info(f"Reconnected to directory: {dir_server}")
            await self._on_directory_connected(node_id, client)
            newly_connected.append((node_id, client))
        return newly_connected

    # -- Teardown -------------------------------------------------------

    async def close_all(self) -> None:
        """
        Close every directory client and fire the disconnected hook for
        each.

        Errors during individual closes are logged at warning level but
        do not abort the shutdown.
        """
        for node_id, client in list(self.clients.items()):
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"Error closing connection to {node_id}: {e}")
            try:
                await self._on_directory_disconnected(node_id)
            except Exception as e:
                logger.warning(f"Error in disconnect hook for {node_id}: {e}")
        self.clients.clear()
