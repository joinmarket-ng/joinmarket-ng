"""
Protocol class for MakerBot mixin type safety.

Defines a Protocol that describes the full MakerBot interface so that mixin
methods can annotate ``self: MakerBotProtocol`` when they call methods or
access attributes defined in other mixins or in MakerBot itself.

This is the mypy-recommended pattern for mixin classes:
https://mypy.readthedocs.io/en/stable/more_types.html#mixin-classes
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from jmcore.crypto import NickIdentity
from jmcore.deduplication import MessageDeduplicator
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer
from jmcore.network import TCPConnection
from jmcore.rate_limiter import RateLimiter
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService

from maker.coinjoin import CoinJoinSession
from maker.config import MakerConfig
from maker.fidelity import FidelityBondInfo
from maker.offers import OfferManager
from maker.rate_limiting import DirectConnectionRateLimiter, OrderbookRateLimiter


class MakerBotProtocol(Protocol):
    """Protocol describing the combined MakerBot interface.

    Used for ``self`` annotations in mixin methods that access attributes or
    call methods defined elsewhere in the MakerBot class hierarchy.
    """

    # -- Attributes --
    running: bool
    config: MakerConfig
    wallet: WalletService
    backend: BlockchainBackend
    nick: str
    nick_identity: NickIdentity
    current_offers: list[Offer]
    fidelity_bond: FidelityBondInfo | None
    current_block_height: int
    directory_clients: dict[str, DirectoryClient]
    active_sessions: dict[str, CoinJoinSession]
    offer_manager: OfferManager
    listen_tasks: list[asyncio.Task[None]]
    direct_connections: dict[str, TCPConnection]
    _message_deduplicator: MessageDeduplicator
    _message_rate_limiter: RateLimiter
    _orderbook_rate_limiter: OrderbookRateLimiter
    _direct_connection_rate_limiter: DirectConnectionRateLimiter
    _directory_reconnect_attempts: dict[str, int]
    _all_directories_disconnected: bool
    _own_wallet_nicks: set[str]
    _hp2_broadcast_semaphore: asyncio.Semaphore

    # -- Cross-mixin methods --

    # Defined in ProtocolHandlersMixin, called by BackgroundTasksMixin
    async def _handle_message(self, message: dict[str, Any], source: str = "unknown") -> None: ...

    # Defined in ProtocolHandlersMixin, called by DirectConnectionMixin
    async def _handle_fill(self, taker_nick: str, msg: str, source: str = "unknown") -> None: ...

    async def _handle_auth(self, taker_nick: str, msg: str, source: str = "unknown") -> None: ...

    async def _handle_tx(self, taker_nick: str, msg: str, source: str = "unknown") -> None: ...

    async def _handle_push(self, taker_nick: str, msg: str, source: str = "unknown") -> None: ...

    async def _send_offers_via_direct_connection(
        self, taker_nick: str, connection: TCPConnection
    ) -> None: ...

    # Defined in MakerBot, called by BackgroundTasksMixin
    def _cleanup_timed_out_sessions(self) -> None: ...

    async def _resync_wallet_and_update_offers(self) -> None: ...

    def _format_offer_announcement(self, offer: Offer, include_bond: bool = False) -> str: ...

    # Defined in MakerBot, called by ProtocolHandlersMixin
    def _get_session_lock(self, taker_nick: str) -> asyncio.Lock: ...

    def _cleanup_session_lock(self, taker_nick: str) -> None: ...

    # Defined in BackgroundTasksMixin, called by ProtocolHandlersMixin
    async def _deferred_wallet_resync(self) -> None: ...

    # Defined in MakerBot, called by DirectConnectionMixin
    def _log_rate_limited(
        self,
        key: str,
        message: str,
        level: str = "warning",
        interval: float = 10.0,
    ) -> None: ...

    # Defined in BackgroundTasksMixin, called internally with Protocol-typed self
    async def _connect_to_directory(
        self, dir_server: str
    ) -> tuple[str, DirectoryClient] | None: ...

    async def _connect_to_directories_with_retry(self) -> None: ...

    async def _listen_client(self, node_id: str, client: DirectoryClient) -> None: ...

    # Defined in DirectConnectionMixin, called internally with Protocol-typed self
    def _parse_direct_message(self, data: bytes) -> tuple[str, str, str] | None: ...

    async def _try_handle_handshake(
        self, connection: TCPConnection, data: bytes, peer_str: str
    ) -> bool: ...

    # Defined in ProtocolHandlersMixin, called internally with Protocol-typed self
    async def _send_response(self, taker_nick: str, command: str, data: dict[str, Any]) -> None: ...

    async def _broadcast_commitment(self, commitment: str) -> None: ...

    async def _handle_privmsg(self, line: str, source: str = "unknown") -> None: ...

    async def _handle_pubmsg(self, line: str, source: str = "unknown") -> None: ...

    async def _handle_hp2_pubmsg(self, from_nick: str, msg: str) -> None: ...

    async def _handle_hp2_privmsg(self, from_nick: str, msg: str) -> None: ...

    async def _send_offers_to_taker(self, taker_nick: str) -> None: ...
