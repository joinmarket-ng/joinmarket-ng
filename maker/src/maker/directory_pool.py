"""
Maker-specific :class:`DirectoryClientPool` subclass.

Layers maker-only construction details onto the shared pool:

* ``location`` -- the bot's published onion ``host:port`` (or the protocol
  sentinel ``"NOT-SERVING-ONION"`` when the maker is not yet serving an
  onion). Directories use this as the maker's contact address for direct
  peer connections.
* ``neutrino_compat`` -- whether the backend can answer neutrino-style
  queries, advertised to directories at handshake time.

Lifecycle hooks (starting listener tasks, announcing offers, emitting
operator notifications) are intentionally NOT placed here -- they remain
in :mod:`maker.background_tasks` where they have direct access to the
bot's mutable state. This keeps the pool focused on connection
plumbing and matches the taker's separation of concerns.
"""

from __future__ import annotations

from typing import Any

from jmcore.directory_pool import DirectoryClientPool

from maker.config import MakerConfig


class MakerDirectoryPool(DirectoryClientPool):
    """
    Pool of directory clients for the maker bot.

    Adds ``location`` and ``neutrino_compat`` to the per-connection
    ``DirectoryClient`` kwargs and otherwise defers to the base
    implementation.

    Args:
        config: The :class:`MakerConfig` to read ``onion_host``,
            ``onion_serving_port``, ``socks_host``, ``socks_port``,
            ``connection_timeout``, ``stream_isolation``, and
            ``directory_servers`` from.
        nick_identity: Long-lived directory handshake identity.
        neutrino_compat: Advertised to directories; takers use this to
            route requests for neutrino-aware metadata.
    """

    def __init__(
        self,
        *,
        config: MakerConfig,
        nick_identity: Any,  # NickIdentity, kept Any to avoid import cycle
        neutrino_compat: bool,
    ):
        self._config = config
        self._neutrino_compat = neutrino_compat
        super().__init__(
            directory_servers=list(config.directory_servers),
            network=config.network.value,
            nick_identity=nick_identity,
            socks_host=config.socks_host,
            socks_port=config.socks_port,
            connection_timeout=config.connection_timeout,
            stream_isolation=config.stream_isolation,
        )

    def _build_client_kwargs(self, host: str, port: int) -> dict[str, Any]:
        kwargs = super()._build_client_kwargs(host, port)
        onion_host = self._config.onion_host
        if onion_host:
            location = f"{onion_host}:{self._config.onion_serving_port}"
        else:
            location = "NOT-SERVING-ONION"
        kwargs["location"] = location
        kwargs["neutrino_compat"] = self._neutrino_compat
        return kwargs

    def refresh_neutrino_compat(self, neutrino_compat: bool) -> None:
        """
        Update the advertised neutrino capability for future connections.

        Existing clients are unaffected; the new value applies to any
        subsequent :meth:`connect_to_directory` /
        :meth:`reconnect_disconnected` call.
        """
        self._neutrino_compat = neutrino_compat
