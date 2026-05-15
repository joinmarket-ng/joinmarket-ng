"""
Notification system for JoinMarket components.

Provides operator notifications through Apprise, supporting multiple notification
channels (Gotify, Telegram, Pushover, Discord, email, etc.).

Configuration is via environment variables:
- NOTIFY_URLS: Comma-separated list of Apprise URLs (required to enable notifications)
- NOTIFY_ENABLED: Set to "false" to disable all notifications (default: true if NOTIFY_URLS set)
- NOTIFY_TITLE_PREFIX: Prefix for notification titles (default: "JoinMarket")

Example NOTIFY_URLS:
- Gotify: gotify://hostname/token
- Telegram: tgram://bot_token/chat_id
- Pushover: pover://user_key@token
- Discord: discord://webhook_id/webhook_token
- Slack: slack://hook_id
- Email: mailto://user:pass@smtp.example.com
- Multiple: gotify://host/token,tgram://bot/chat

For full list of supported services: https://github.com/caronc/apprise#supported-notifications

Usage:
    from jmcore.notifications import get_notifier

    notifier = get_notifier()
    await notifier.notify_fill_request(taker_nick, cj_amount, offer_id)

The module is designed to be:
1. Fire-and-forget: Notification failures don't affect protocol operations
2. Async-first: All notifications are sent asynchronously
3. Privacy-aware: Sensitive data (txids, amounts) can be optionally excluded
4. Configurable: Per-event type enable/disable through environment variables
5. Resilient: Failed notifications are retried in the background with exponential
   backoff (configurable, enabled by default). This is critical for Tor-routed
   notifications where transient circuit failures are common.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from loguru import logger
from pydantic import BaseModel, Field, SecretStr

if TYPE_CHECKING:
    from jmcore.settings import JoinMarketSettings


class NotificationPriority(StrEnum):
    """Notification priority levels (maps to Apprise NotifyType)."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    FAILURE = "failure"


class NotificationEvent(StrEnum):
    """
    Typed event identifiers for the notification registry.

    Each event has an entry in EVENT_TEMPLATES describing its gating flag,
    title, body builder, and default priority. The public ``notify_*``
    methods on :class:`Notifier` are thin shims around
    :meth:`Notifier.emit`, which looks the event up in the registry.
    """

    SUMMARY = "summary"
    FILL_REQUEST = "fill_request"
    REJECTION = "rejection"
    TX_SIGNED = "tx_signed"
    MEMPOOL = "mempool"
    CONFIRMED = "confirmed"
    NICK_CHANGE = "nick_change"
    DIRECTORY_DISCONNECT = "directory_disconnect"
    DIRECTORY_RECONNECT = "directory_reconnect"
    ALL_DIRECTORIES_DISCONNECTED = "all_directories_disconnected"
    ALL_DIRECTORIES_RECONNECTED = "all_directories_reconnected"
    COINJOIN_START = "coinjoin_start"
    COINJOIN_COMPLETE = "coinjoin_complete"
    COINJOIN_FAILED = "coinjoin_failed"
    PEER_CONNECTED = "peer_connected"
    PEER_DISCONNECTED = "peer_disconnected"
    PEER_BANNED = "peer_banned"
    ORDERBOOK_STATUS = "orderbook_status"
    MAKER_OFFLINE = "maker_offline"
    STARTUP = "startup"


@dataclass(frozen=True)
class EventTemplate:
    """
    Declarative template for a notification event.

    Attributes:
        gating_attr: Name of the boolean attribute on
            :class:`NotificationConfig` that gates this event. ``None`` means
            the event is always emitted (subject only to the master switch
            in :meth:`Notifier._ensure_initialized`).
        title_builder: Callable ``(notifier, fields) -> str`` that returns
            the notification title. Static titles are wrapped via
            ``lambda _n, _f: "..."``.
        body_builder: Callable ``(notifier, fields) -> str`` that returns
            the notification body. The notifier is passed so builders can
            use the ``_format_amount`` / ``_format_nick`` / ``_format_txid``
            privacy helpers.
        priority_builder: Callable ``(fields) -> NotificationPriority`` that
            returns the priority. Static priorities are wrapped via
            ``lambda _f: NotificationPriority.INFO``.
    """

    gating_attr: str | None
    title_builder: Callable[[Notifier, dict[str, Any]], str]
    body_builder: Callable[[Notifier, dict[str, Any]], str]
    priority_builder: Callable[[dict[str, Any]], NotificationPriority]


def _summary_body(notifier: Notifier, f: dict[str, Any]) -> str:
    period_label: str = f["period_label"]
    total_requests: int = f["total_requests"]
    if total_requests == 0:
        body = f"Period: {period_label}\nNo CoinJoin activity in this period."
    else:
        successful: int = f["successful"]
        failed: int = f["failed"]
        total_earnings: int = f["total_earnings"]
        total_volume: int = f["total_volume"]
        successful_volume: int = f.get("successful_volume", 0)
        utxos_disclosed: int = f.get("utxos_disclosed", 0)
        success_rate = successful / total_requests * 100 if total_requests > 0 else 0.0
        body = (
            f"Period: {period_label}\n"
            f"Requests: {total_requests}\n"
            f"Successful: {successful}\n"
            f"Failed: {failed}\n"
            f"Success rate: {success_rate:.0f}%\n"
            f"Earnings: {notifier._format_amount(total_earnings)}\n"
            f"Volume: {notifier._format_amount(successful_volume)}"
            f" / {notifier._format_amount(total_volume)}\n"
            f"UTXOs disclosed: {utxos_disclosed}"
        )
    if notifier.config.notify_summary_balance:
        total_balance = f.get("total_balance")
        utxo_count = f.get("utxo_count")
        if total_balance is not None:
            body += f"\nBalance: {notifier._format_amount(total_balance)}"
        if utxo_count is not None:
            body += f"\nUTXOs: {utxo_count}"
    version = f.get("version")
    if version:
        body += f"\nVersion: {version}"
        update_available = f.get("update_available")
        if update_available:
            body += f" (update available: {update_available})"
    return body


def _rejection_body(notifier: Notifier, f: dict[str, Any]) -> str:
    body = f"Taker: {notifier._format_nick(f['taker_nick'])}\nReason: {f['reason']}"
    details = f.get("details", "")
    if details:
        body += f"\nDetails: {details}"
    return body


def _coinjoin_failed_body(notifier: Notifier, f: dict[str, Any]) -> str:
    body = f"Reason: {f['reason']}"
    phase = f.get("phase", "")
    if phase:
        body = f"Phase: {phase}\n" + body
    cj_amount = f.get("cj_amount", 0)
    if cj_amount > 0:
        body += f"\nAmount: {notifier._format_amount(cj_amount)}"
    return body


def _directory_disconnect_priority(f: dict[str, Any]) -> NotificationPriority:
    return (
        NotificationPriority.FAILURE if f["connected_count"] == 0 else NotificationPriority.WARNING
    )


def _startup_body(notifier: Notifier, f: dict[str, Any]) -> str:
    body = f"Component: {f['component']}"
    nick = f.get("nick", "")
    if nick:
        body += f"\nNick: {notifier._format_nick(nick)}"
    version = f.get("version", "")
    if version:
        body += f"\nVersion: {version}"
    network = f.get("network", "")
    if network:
        body += f"\nNetwork: {network}"
    return body


# Declarative registry of all notification events. The Notifier.emit() core
# looks up each event here; the per-event notify_* methods are thin shims.
EVENT_TEMPLATES: dict[NotificationEvent, EventTemplate] = {
    NotificationEvent.SUMMARY: EventTemplate(
        gating_attr="notify_summary",
        title_builder=lambda _n, f: f"{f['period_label']} Summary",
        body_builder=_summary_body,
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.FILL_REQUEST: EventTemplate(
        gating_attr="notify_fill",
        title_builder=lambda _n, _f: "Fill Request Received",
        body_builder=lambda n, f: (
            f"Taker: {n._format_nick(f['taker_nick'])}\n"
            f"Amount: {n._format_amount(f['cj_amount'])}\n"
            f"Offer ID: {f['offer_id']}"
        ),
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.REJECTION: EventTemplate(
        gating_attr="notify_rejection",
        title_builder=lambda _n, _f: "Request Rejected",
        body_builder=_rejection_body,
        priority_builder=lambda _f: NotificationPriority.WARNING,
    ),
    NotificationEvent.TX_SIGNED: EventTemplate(
        gating_attr="notify_signing",
        title_builder=lambda _n, _f: "Transaction Signed",
        body_builder=lambda n, f: (
            f"Taker: {n._format_nick(f['taker_nick'])}\n"
            f"CJ Amount: {n._format_amount(f['cj_amount'])}\n"
            f"Inputs signed: {f['num_inputs']}\n"
            f"Fee earned: {n._format_amount(f['fee_earned'])}"
        ),
        priority_builder=lambda _f: NotificationPriority.SUCCESS,
    ),
    NotificationEvent.MEMPOOL: EventTemplate(
        gating_attr="notify_mempool",
        title_builder=lambda _n, _f: "CoinJoin in Mempool",
        body_builder=lambda n, f: (
            f"Role: {f.get('role', 'maker').capitalize()}\n"
            f"TxID: {n._format_txid(f['txid'])}\n"
            f"Amount: {n._format_amount(f['cj_amount'])}"
        ),
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.CONFIRMED: EventTemplate(
        gating_attr="notify_confirmed",
        title_builder=lambda _n, _f: "CoinJoin Confirmed",
        body_builder=lambda n, f: (
            f"Role: {f.get('role', 'maker').capitalize()}\n"
            f"TxID: {n._format_txid(f['txid'])}\n"
            f"Amount: {n._format_amount(f['cj_amount'])}\n"
            f"Confirmations: {f['confirmations']}"
        ),
        priority_builder=lambda _f: NotificationPriority.SUCCESS,
    ),
    NotificationEvent.NICK_CHANGE: EventTemplate(
        gating_attr="notify_nick_change",
        title_builder=lambda _n, _f: "Nick Changed",
        body_builder=lambda n, f: (
            f"Old: {n._format_nick(f['old_nick'])}\nNew: {n._format_nick(f['new_nick'])}"
        ),
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.DIRECTORY_DISCONNECT: EventTemplate(
        gating_attr="notify_disconnect",
        title_builder=lambda _n, _f: "Directory Server Disconnected",
        body_builder=lambda _n, f: (
            f"Server: {f['server'][:30]}...\n"
            f"Status: {'reconnecting' if f.get('reconnecting', True) else 'disconnected'}\n"
            f"Connected: {f['connected_count']}/{f['total_count']}"
        ),
        priority_builder=_directory_disconnect_priority,
    ),
    NotificationEvent.DIRECTORY_RECONNECT: EventTemplate(
        gating_attr="notify_disconnect",
        title_builder=lambda _n, _f: "Directory Server Reconnected",
        body_builder=lambda _n, f: (
            f"Server: {f['server'][:30]}...\nConnected: {f['connected_count']}/{f['total_count']}"
        ),
        priority_builder=lambda _f: NotificationPriority.SUCCESS,
    ),
    NotificationEvent.ALL_DIRECTORIES_DISCONNECTED: EventTemplate(
        gating_attr="notify_all_disconnect",
        title_builder=lambda _n, _f: "CRITICAL: All Directories Disconnected",
        body_builder=lambda _n, _f: (
            "Lost connection to ALL directory servers.\n"
            "No CoinJoins possible until reconnected.\n"
            "Check network connectivity and Tor status."
        ),
        priority_builder=lambda _f: NotificationPriority.FAILURE,
    ),
    NotificationEvent.ALL_DIRECTORIES_RECONNECTED: EventTemplate(
        gating_attr="notify_all_disconnect",
        title_builder=lambda _n, _f: "RESOLVED: Directory Servers Reconnected",
        body_builder=lambda _n, f: (
            f"Reconnected to directory servers "
            f"({f['connected_count']}/{f['total_count']}).\n"
            "CoinJoins are possible again."
        ),
        priority_builder=lambda _f: NotificationPriority.SUCCESS,
    ),
    NotificationEvent.COINJOIN_START: EventTemplate(
        gating_attr="notify_coinjoin_start",
        title_builder=lambda _n, _f: "CoinJoin Started",
        body_builder=lambda n, f: (
            f"Amount: {n._format_amount(f['cj_amount'])}\n"
            f"Makers: {f['num_makers']}\n"
            f"Destination: "
            f"{'internal' if f['destination'] == 'INTERNAL' else f['destination'][:12] + '...'}"
        ),
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.COINJOIN_COMPLETE: EventTemplate(
        gating_attr="notify_coinjoin_complete",
        title_builder=lambda _n, _f: "CoinJoin Complete",
        body_builder=lambda n, f: (
            f"TxID: {n._format_txid(f['txid'])}\n"
            f"Amount: {n._format_amount(f['cj_amount'])}\n"
            f"Makers: {f['num_makers']}\n"
            f"Total fees: {n._format_amount(f['total_fees'])}"
        ),
        priority_builder=lambda _f: NotificationPriority.SUCCESS,
    ),
    NotificationEvent.COINJOIN_FAILED: EventTemplate(
        gating_attr="notify_coinjoin_failed",
        title_builder=lambda _n, _f: "CoinJoin Failed",
        body_builder=_coinjoin_failed_body,
        priority_builder=lambda _f: NotificationPriority.FAILURE,
    ),
    NotificationEvent.PEER_CONNECTED: EventTemplate(
        gating_attr="notify_peer_events",
        title_builder=lambda _n, _f: "Peer Connected",
        body_builder=lambda n, f: (
            f"Nick: {n._format_nick(f['nick'])}\n"
            f"Location: {f['location'][:30]}...\n"
            f"Total peers: {f['total_peers']}"
        ),
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.PEER_DISCONNECTED: EventTemplate(
        gating_attr="notify_peer_events",
        title_builder=lambda _n, _f: "Peer Disconnected",
        body_builder=lambda n, f: (
            f"Nick: {n._format_nick(f['nick'])}\nRemaining peers: {f['total_peers']}"
        ),
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.PEER_BANNED: EventTemplate(
        gating_attr="notify_rate_limit",
        title_builder=lambda _n, _f: "Peer Banned",
        body_builder=lambda n, f: (
            f"Nick: {n._format_nick(f['nick'])}\nReason: {f['reason']}\nDuration: {f['duration']}s"
        ),
        priority_builder=lambda _f: NotificationPriority.WARNING,
    ),
    NotificationEvent.ORDERBOOK_STATUS: EventTemplate(
        gating_attr=None,
        title_builder=lambda _n, _f: "Orderbook Status",
        body_builder=lambda _n, f: (
            f"Directories: {f['connected_directories']}/{f['total_directories']}\n"
            f"Offers: {f['total_offers']}\n"
            f"Makers: {f['total_makers']}"
        ),
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.MAKER_OFFLINE: EventTemplate(
        gating_attr=None,
        title_builder=lambda _n, _f: "Maker Offline",
        body_builder=lambda n, f: f"Nick: {n._format_nick(f['nick'])}\nLast seen: {f['last_seen']}",
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
    NotificationEvent.STARTUP: EventTemplate(
        gating_attr="notify_startup",
        title_builder=lambda _n, _f: "Component Started",
        body_builder=_startup_body,
        priority_builder=lambda _f: NotificationPriority.INFO,
    ),
}


class NotificationConfig(BaseModel):
    """
    Configuration for the notification system.

    All configuration is loaded from environment variables.
    """

    # Core settings
    enabled: bool = Field(
        default=False,
        description="Master switch for notifications",
    )
    urls: list[SecretStr] = Field(
        default_factory=list,
        description="List of Apprise notification URLs",
    )
    title_prefix: str = Field(
        default="JoinMarket NG",
        description="Prefix for all notification titles",
    )
    component_name: str = Field(
        default="",
        description="Component name to include in notification titles (e.g., 'Maker', 'Taker')",
    )

    # Privacy settings - exclude sensitive data from notifications
    include_amounts: bool = Field(
        default=True,
        description="Include amounts in notifications",
    )
    include_txids: bool = Field(
        default=False,
        description="Include transaction IDs in notifications (privacy risk)",
    )
    include_nick: bool = Field(
        default=True,
        description="Include peer nicks in notifications",
    )

    # Tor/Proxy settings
    use_tor: bool = Field(
        default=True,
        description="Route notifications through Tor SOCKS proxy",
    )
    tor_socks_host: str = Field(
        default="127.0.0.1",
        description="Tor SOCKS5 proxy host (only used if use_tor=True)",
    )
    tor_socks_port: int = Field(
        default=9050,
        ge=1,
        le=65535,
        description="Tor SOCKS5 proxy port (only used if use_tor=True)",
    )
    stream_isolation: bool = Field(
        default=True,
        description=(
            "Use SOCKS5 auth credentials to isolate notification and update-check "
            "traffic onto separate Tor circuits (only used if use_tor=True)"
        ),
    )

    # Retry settings for failed notifications (Tor is unreliable)
    retry_enabled: bool = Field(
        default=True,
        description=(
            "Retry failed notifications in the background. "
            "Retries use exponential backoff and never block the main process."
        ),
    )
    retry_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of retry attempts for a failed notification (1-10)",
    )
    retry_base_delay: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description=(
            "Base delay in seconds before the first retry (1-60). "
            "Subsequent retries double this delay (exponential backoff)."
        ),
    )

    # Event type toggles (all enabled by default if notifications are enabled)
    notify_fill: bool = Field(default=True, description="Notify on !fill requests")
    notify_rejection: bool = Field(default=True, description="Notify on rejections")
    notify_signing: bool = Field(default=True, description="Notify on tx signing")
    notify_mempool: bool = Field(default=True, description="Notify on mempool detection")
    notify_confirmed: bool = Field(default=True, description="Notify on confirmation")
    notify_nick_change: bool = Field(default=True, description="Notify on nick change")
    notify_disconnect: bool = Field(
        default=False,
        description="Notify on individual directory server disconnect/reconnect (noisy)",
    )
    notify_all_disconnect: bool = Field(
        default=True,
        description="Notify when ALL directory servers are disconnected (critical)",
    )
    notify_coinjoin_start: bool = Field(default=True, description="Notify on CoinJoin start")
    notify_coinjoin_complete: bool = Field(default=True, description="Notify on CoinJoin complete")
    notify_coinjoin_failed: bool = Field(default=True, description="Notify on CoinJoin failure")
    notify_peer_events: bool = Field(default=False, description="Notify on peer connect/disconnect")
    notify_rate_limit: bool = Field(default=True, description="Notify on rate limit bans")
    notify_startup: bool = Field(default=True, description="Notify on component startup")
    notify_summary: bool = Field(
        default=True,
        description="Send periodic summary notifications with CoinJoin stats",
    )
    notify_summary_balance: bool = Field(
        default=False,
        description=(
            "Include total wallet balance and UTXO count in periodic summary "
            "notifications. Disabled by default for privacy."
        ),
    )
    summary_interval_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description=(
            "Interval in hours between summary notifications (1-168). "
            "Common values: 24 (daily), 168 (weekly)"
        ),
    )
    check_for_updates: bool = Field(
        default=False,
        description=(
            "Check GitHub for new releases and include version info in summary notifications. "
            "PRIVACY WARNING: polls api.github.com each summary interval."
        ),
    )

    model_config = {"frozen": False}


def load_notification_config() -> NotificationConfig:
    """
    Load notification configuration from the unified settings system.

    This function uses JoinMarketSettings which loads from:
    1. Environment variables (NOTIFICATIONS__*, TOR__*)
    2. Config file (~/.joinmarket-ng/config.toml)
    3. Default values
    """
    from jmcore.settings import JoinMarketSettings

    settings = JoinMarketSettings()
    config = convert_settings_to_notification_config(settings)

    # Log notification configuration status
    if config.enabled:
        logger.info(
            f"Notifications enabled with {len(config.urls)} URL(s), use_tor={config.use_tor}"
        )
    else:
        logger.info("Notifications disabled (no URLs configured)")

    return config


def convert_settings_to_notification_config(
    settings: JoinMarketSettings,
    component_name: str = "",
) -> NotificationConfig:
    """
    Convert NotificationSettings from JoinMarketSettings to NotificationConfig.

    This allows the notification system to use the unified settings system
    (config file + env vars + CLI args) instead of only environment variables.

    Args:
        settings: JoinMarketSettings instance with notification configuration
        component_name: Optional component name to include in notification titles.
            If provided, overrides settings.notifications.component_name.
            Examples: "Maker", "Taker", "Directory", "Orderbook Watcher"

    Returns:
        NotificationConfig suitable for use with Notifier
    """
    ns = settings.notifications

    # Convert URL strings to SecretStr
    urls = [SecretStr(url) for url in ns.urls]

    # Notifications are enabled if URLs are provided (auto-enable) or explicitly enabled
    # The enabled flag is primarily for explicit control when URLs are managed elsewhere
    enabled = bool(ns.urls) or ns.enabled

    # Use provided component_name or fall back to settings
    effective_component_name = component_name or ns.component_name

    return NotificationConfig(
        enabled=enabled,
        urls=urls,
        title_prefix=ns.title_prefix,
        component_name=effective_component_name,
        include_amounts=ns.include_amounts,
        include_txids=ns.include_txids,
        include_nick=ns.include_nick,
        use_tor=ns.use_tor,
        tor_socks_host=settings.tor.socks_host,
        tor_socks_port=settings.tor.socks_port,
        stream_isolation=settings.tor.stream_isolation,
        notify_fill=ns.notify_fill,
        notify_rejection=ns.notify_rejection,
        notify_signing=ns.notify_signing,
        notify_mempool=ns.notify_mempool,
        notify_confirmed=ns.notify_confirmed,
        notify_nick_change=ns.notify_nick_change,
        notify_disconnect=ns.notify_disconnect,
        notify_all_disconnect=ns.notify_all_disconnect,
        notify_coinjoin_start=ns.notify_coinjoin_start,
        notify_coinjoin_complete=ns.notify_coinjoin_complete,
        notify_coinjoin_failed=ns.notify_coinjoin_failed,
        notify_peer_events=ns.notify_peer_events,
        notify_rate_limit=ns.notify_rate_limit,
        notify_startup=ns.notify_startup,
        notify_summary=ns.notify_summary,
        notify_summary_balance=ns.notify_summary_balance,
        summary_interval_hours=ns.summary_interval_hours,
        check_for_updates=ns.check_for_updates,
        retry_enabled=ns.retry_enabled,
        retry_max_attempts=ns.retry_max_attempts,
        retry_base_delay=ns.retry_base_delay,
    )


class Notifier:
    """
    Notification sender using Apprise.

    Thread-safe and async-friendly. Notification failures are logged but
    don't raise exceptions - notifications should never block protocol operations.

    Failed notifications are automatically retried in the background with
    exponential backoff when retry_enabled is True (the default). This is
    important for Tor-routed notifications where transient circuit failures
    are common.
    """

    def __init__(self, config: NotificationConfig | None = None):
        """
        Initialize the notifier.

        Args:
            config: Notification configuration. If None, loads from environment.
        """
        self.config = config or load_notification_config()
        self._apprise: Any | None = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._retry_tasks: set[asyncio.Task[None]] = set()

    async def _ensure_initialized(self) -> bool:
        """Lazily initialize Apprise. Returns True if ready to send."""
        if not self.config.enabled or not self.config.urls:
            return False

        if self._initialized:
            return self._apprise is not None

        async with self._lock:
            if self._initialized:
                return self._apprise is not None

            try:
                import apprise

                # Configure proxy environment variables if Tor is enabled
                if self.config.use_tor:
                    # Use the Tor configuration from settings
                    tor_host = self.config.tor_socks_host
                    tor_port = self.config.tor_socks_port

                    if self.config.stream_isolation:
                        from jmcore.tor_isolation import (
                            IsolationCategory,
                            build_isolated_proxy_url,
                        )

                        proxy_url = build_isolated_proxy_url(
                            tor_host,
                            tor_port,
                            IsolationCategory.NOTIFICATION,
                        )
                    else:
                        # Use socks5h:// to resolve DNS through the proxy
                        # (important for .onion)
                        proxy_url = f"socks5h://{tor_host}:{tor_port}"

                    # Set environment variables that Apprise/requests will use
                    os.environ["HTTP_PROXY"] = proxy_url
                    os.environ["HTTPS_PROXY"] = proxy_url
                    logger.info(f"Configuring notifications to route through Tor: {proxy_url}")

                self._apprise = apprise.Apprise()

                # Use longer timeout for Tor connections (default is 4s, too short for Tor)
                # Tor circuit establishment can take 10-30 seconds
                # Use Apprise's cto (connection timeout) and rto (read timeout) URL parameters
                for secret_url in self.config.urls:
                    # Get the actual URL string from SecretStr
                    url = secret_url.get_secret_value()

                    if self.config.use_tor:
                        # Append timeout parameters to URL for Tor connections
                        # cto = connection timeout, rto = read timeout (both in seconds)
                        timeout_params = "cto=30&rto=30"
                        if "?" in url:
                            url_with_timeout = f"{url}&{timeout_params}"
                        else:
                            url_with_timeout = f"{url}?{timeout_params}"
                    else:
                        url_with_timeout = url

                    if not self._apprise.add(url_with_timeout):
                        logger.warning(f"Failed to add notification URL: {url[:30]}...")

                if len(self._apprise) == 0:
                    logger.warning("No valid notification URLs configured")
                    self._apprise = None
                else:
                    logger.info(f"Notifications enabled with {len(self._apprise)} service(s)")

            except ImportError:
                logger.warning(
                    "Apprise not installed. Install with: pip install apprise\n"
                    "Notifications will be disabled."
                )
                self._apprise = None
            except Exception as e:
                logger.warning(f"Failed to initialize notifications: {e}")
                self._apprise = None

            self._initialized = True
            return self._apprise is not None

    async def _send(
        self,
        title: str,
        body: str,
        priority: NotificationPriority = NotificationPriority.INFO,
    ) -> bool:
        """
        Send a notification via Apprise.

        On failure, if retry is enabled, spawns a background task that retries
        with exponential backoff. The background task never blocks the caller.

        Args:
            title: Notification title (will be prefixed)
            body: Notification body
            priority: Notification priority

        Returns:
            True if sent successfully on the first attempt
        """
        # Don't attempt (or retry) if not initialized / disabled
        if not await self._ensure_initialized():
            return False

        result = await self._try_send(title, body, priority)
        if not result and self.config.retry_enabled:
            self._schedule_retry(title, body, priority)
        return result

    async def _try_send(
        self,
        title: str,
        body: str,
        priority: NotificationPriority = NotificationPriority.INFO,
    ) -> bool:
        """
        Attempt a single notification send via Apprise.

        Args:
            title: Notification title (will be prefixed)
            body: Notification body
            priority: Notification priority

        Returns:
            True if sent successfully to at least one service
        """
        if not await self._ensure_initialized():
            return False

        # At this point, _apprise is guaranteed to be initialized
        assert self._apprise is not None
        apprise_instance = self._apprise  # Bind to local for type narrowing

        try:
            import apprise

            # Map our priority to Apprise NotifyType
            notify_type = {
                NotificationPriority.INFO: apprise.NotifyType.INFO,
                NotificationPriority.SUCCESS: apprise.NotifyType.SUCCESS,
                NotificationPriority.WARNING: apprise.NotifyType.WARNING,
                NotificationPriority.FAILURE: apprise.NotifyType.FAILURE,
            }.get(priority, apprise.NotifyType.INFO)

            # Build title: "JoinMarket NG (Maker): Title" or "JoinMarket NG: Title" if no component
            if self.config.component_name:
                full_title = f"{self.config.title_prefix} ({self.config.component_name}): {title}"
            else:
                full_title = f"{self.config.title_prefix}: {title}"

            # Send asynchronously if apprise supports it, otherwise in executor
            if hasattr(apprise_instance, "async_notify"):
                result = await apprise_instance.async_notify(
                    title=full_title,
                    body=body,
                    notify_type=notify_type,
                )
            else:
                # Run synchronous notify in thread pool
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: apprise_instance.notify(
                        title=full_title,
                        body=body,
                        notify_type=notify_type,
                    ),
                )

            if not result:
                logger.warning(
                    f"Notification failed: {title}. "
                    "Check Tor connectivity and notification service URL. "
                    "Ensure PySocks is installed for SOCKS proxy support."
                )
            else:
                logger.debug(f"Notification sent: {title}")
            return result

        except Exception as e:
            logger.warning(f"Failed to send notification '{title}': {e}")
            return False

    def _schedule_retry(
        self,
        title: str,
        body: str,
        priority: NotificationPriority,
    ) -> None:
        """
        Schedule background retries for a failed notification.

        Spawns an asyncio task that retries with exponential backoff.
        The task is tracked in _retry_tasks and cleaned up on completion.
        """
        task = asyncio.create_task(self._retry_send(title, body, priority))
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def _retry_send(
        self,
        title: str,
        body: str,
        priority: NotificationPriority,
    ) -> None:
        """
        Retry sending a notification with exponential backoff.

        Runs in the background as an asyncio task. Logs each attempt
        and gives up after max_attempts retries.
        """
        delay = self.config.retry_base_delay
        max_attempts = self.config.retry_max_attempts

        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(delay)

            logger.debug(
                f"Retrying notification '{title}' "
                f"(attempt {attempt}/{max_attempts}, delay={delay:.0f}s)"
            )

            try:
                result = await self._try_send(title, body, priority)
                if result:
                    logger.info(
                        f"Notification '{title}' delivered on retry "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    return
            except Exception as e:
                logger.debug(f"Retry attempt {attempt} for '{title}' raised: {e}")

            delay *= 2  # Exponential backoff

        logger.warning(f"Notification '{title}' failed after {max_attempts} retries, giving up")

    def _format_amount(self, sats: int) -> str:
        """Format satoshi amount for display."""
        if not self.config.include_amounts:
            return "[hidden]"
        if sats >= 100_000_000:
            return f"{sats / 100_000_000:.4f} BTC"
        return f"{sats:,} sats"

    def _format_nick(self, nick: str) -> str:
        """Format nick for display."""
        if not self.config.include_nick:
            return "[hidden]"
        return nick

    def _format_txid(self, txid: str) -> str:
        """Format txid for display."""
        if not self.config.include_txids:
            return "[hidden]"
        return f"{txid[:16]}..."

    # =========================================================================
    # Typed-event seam
    # =========================================================================

    async def emit(self, event: NotificationEvent, **fields: Any) -> bool:
        """
        Emit a typed notification event through the registry.

        This is the single dispatch point shared by every public
        ``notify_*`` shim. It:

        1. Looks up the :class:`EventTemplate` for ``event``.
        2. Checks the gating flag on :class:`NotificationConfig` (if any)
           and short-circuits to ``False`` when the operator has disabled
           that event class.
        3. Builds the title, body, and priority through the template's
           builders, passing ``self`` so they can use privacy helpers.
        4. Delegates to :meth:`_send`, which handles initialization,
           Apprise dispatch, and background retries.

        Args:
            event: The :class:`NotificationEvent` to emit.
            **fields: Event-specific payload consumed by the template's
                body / title / priority builders. The accepted keys are
                documented on the corresponding ``notify_*`` shim.

        Returns:
            ``True`` if the notification was accepted on the first send
            attempt; ``False`` otherwise (gated off, never initialized,
            or first-attempt failure - in which case a background retry
            may still succeed when ``retry_enabled`` is true).
        """
        template = EVENT_TEMPLATES[event]
        if template.gating_attr is not None and not getattr(self.config, template.gating_attr):
            return False
        title = template.title_builder(self, fields)
        body = template.body_builder(self, fields)
        priority = template.priority_builder(fields)
        return await self._send(title=title, body=body, priority=priority)

    # =========================================================================
    # Maker notifications
    # =========================================================================

    async def notify_summary(
        self,
        period_label: str,
        total_requests: int,
        successful: int,
        failed: int,
        total_earnings: int,
        total_volume: int,
        successful_volume: int = 0,
        utxos_disclosed: int = 0,
        version: str | None = None,
        update_available: str | None = None,
        total_balance: int | None = None,
        utxo_count: int | None = None,
    ) -> bool:
        """Send a periodic summary notification with CoinJoin statistics."""
        return await self.emit(
            NotificationEvent.SUMMARY,
            period_label=period_label,
            total_requests=total_requests,
            successful=successful,
            failed=failed,
            total_earnings=total_earnings,
            total_volume=total_volume,
            successful_volume=successful_volume,
            utxos_disclosed=utxos_disclosed,
            version=version,
            update_available=update_available,
            total_balance=total_balance,
            utxo_count=utxo_count,
        )

    async def notify_fill_request(
        self,
        taker_nick: str,
        cj_amount: int,
        offer_id: int,
    ) -> bool:
        """Notify when a !fill request is received (maker)."""
        return await self.emit(
            NotificationEvent.FILL_REQUEST,
            taker_nick=taker_nick,
            cj_amount=cj_amount,
            offer_id=offer_id,
        )

    async def notify_rejection(
        self,
        taker_nick: str,
        reason: str,
        details: str = "",
    ) -> bool:
        """Notify when rejecting a taker request (maker)."""
        return await self.emit(
            NotificationEvent.REJECTION,
            taker_nick=taker_nick,
            reason=reason,
            details=details,
        )

    async def notify_tx_signed(
        self,
        taker_nick: str,
        cj_amount: int,
        num_inputs: int,
        fee_earned: int,
    ) -> bool:
        """Notify when transaction is signed (maker)."""
        return await self.emit(
            NotificationEvent.TX_SIGNED,
            taker_nick=taker_nick,
            cj_amount=cj_amount,
            num_inputs=num_inputs,
            fee_earned=fee_earned,
        )

    async def notify_mempool(
        self,
        txid: str,
        cj_amount: int,
        role: str = "maker",
    ) -> bool:
        """Notify when CoinJoin is seen in mempool."""
        return await self.emit(
            NotificationEvent.MEMPOOL,
            txid=txid,
            cj_amount=cj_amount,
            role=role,
        )

    async def notify_confirmed(
        self,
        txid: str,
        cj_amount: int,
        confirmations: int,
        role: str = "maker",
    ) -> bool:
        """Notify when CoinJoin is confirmed."""
        return await self.emit(
            NotificationEvent.CONFIRMED,
            txid=txid,
            cj_amount=cj_amount,
            confirmations=confirmations,
            role=role,
        )

    async def notify_nick_change(
        self,
        old_nick: str,
        new_nick: str,
    ) -> bool:
        """Notify when maker nick changes (privacy feature)."""
        return await self.emit(
            NotificationEvent.NICK_CHANGE,
            old_nick=old_nick,
            new_nick=new_nick,
        )

    async def notify_directory_disconnect(
        self,
        server: str,
        connected_count: int,
        total_count: int,
        reconnecting: bool = True,
    ) -> bool:
        """Notify when disconnected from a directory server."""
        return await self.emit(
            NotificationEvent.DIRECTORY_DISCONNECT,
            server=server,
            connected_count=connected_count,
            total_count=total_count,
            reconnecting=reconnecting,
        )

    async def notify_all_directories_disconnected(self) -> bool:
        """Notify when disconnected from ALL directory servers (critical)."""
        return await self.emit(NotificationEvent.ALL_DIRECTORIES_DISCONNECTED)

    async def notify_all_directories_reconnected(
        self,
        connected_count: int,
        total_count: int,
    ) -> bool:
        """Notify when at least one directory server is reconnected after all were lost (recovery)."""
        return await self.emit(
            NotificationEvent.ALL_DIRECTORIES_RECONNECTED,
            connected_count=connected_count,
            total_count=total_count,
        )

    async def notify_directory_reconnect(
        self,
        server: str,
        connected_count: int,
        total_count: int,
    ) -> bool:
        """Notify when successfully reconnected to a directory server."""
        return await self.emit(
            NotificationEvent.DIRECTORY_RECONNECT,
            server=server,
            connected_count=connected_count,
            total_count=total_count,
        )

    # =========================================================================
    # Taker notifications
    # =========================================================================

    async def notify_coinjoin_start(
        self,
        cj_amount: int,
        num_makers: int,
        destination: str,
    ) -> bool:
        """Notify when CoinJoin is initiated (taker)."""
        return await self.emit(
            NotificationEvent.COINJOIN_START,
            cj_amount=cj_amount,
            num_makers=num_makers,
            destination=destination,
        )

    async def notify_coinjoin_complete(
        self,
        txid: str,
        cj_amount: int,
        num_makers: int,
        total_fees: int,
    ) -> bool:
        """Notify when CoinJoin completes successfully (taker)."""
        return await self.emit(
            NotificationEvent.COINJOIN_COMPLETE,
            txid=txid,
            cj_amount=cj_amount,
            num_makers=num_makers,
            total_fees=total_fees,
        )

    async def notify_coinjoin_failed(
        self,
        reason: str,
        phase: str = "",
        cj_amount: int = 0,
    ) -> bool:
        """Notify when CoinJoin fails (taker)."""
        return await self.emit(
            NotificationEvent.COINJOIN_FAILED,
            reason=reason,
            phase=phase,
            cj_amount=cj_amount,
        )

    # =========================================================================
    # Directory server notifications
    # =========================================================================

    async def notify_peer_connected(
        self,
        nick: str,
        location: str,
        total_peers: int,
    ) -> bool:
        """Notify when a new peer connects (directory server)."""
        return await self.emit(
            NotificationEvent.PEER_CONNECTED,
            nick=nick,
            location=location,
            total_peers=total_peers,
        )

    async def notify_peer_disconnected(
        self,
        nick: str,
        total_peers: int,
    ) -> bool:
        """Notify when a peer disconnects (directory server)."""
        return await self.emit(
            NotificationEvent.PEER_DISCONNECTED,
            nick=nick,
            total_peers=total_peers,
        )

    async def notify_peer_banned(
        self,
        nick: str,
        reason: str,
        duration: int,
    ) -> bool:
        """Notify when a peer is banned for rate limit violations."""
        return await self.emit(
            NotificationEvent.PEER_BANNED,
            nick=nick,
            reason=reason,
            duration=duration,
        )

    # =========================================================================
    # Orderbook watcher notifications
    # =========================================================================

    async def notify_orderbook_status(
        self,
        connected_directories: int,
        total_directories: int,
        total_offers: int,
        total_makers: int,
    ) -> bool:
        """Notify orderbook status summary."""
        return await self.emit(
            NotificationEvent.ORDERBOOK_STATUS,
            connected_directories=connected_directories,
            total_directories=total_directories,
            total_offers=total_offers,
            total_makers=total_makers,
        )

    async def notify_maker_offline(
        self,
        nick: str,
        last_seen: str,
    ) -> bool:
        """Notify when a maker goes offline."""
        return await self.emit(
            NotificationEvent.MAKER_OFFLINE,
            nick=nick,
            last_seen=last_seen,
        )

    # =========================================================================
    # Generic notification
    # =========================================================================

    async def notify_startup(
        self,
        component: str,
        version: str = "",
        network: str = "",
        nick: str = "",
    ) -> bool:
        """Notify when a component starts up."""
        return await self.emit(
            NotificationEvent.STARTUP,
            component=component,
            version=version,
            network=network,
            nick=nick,
        )

    async def notify(
        self,
        title: str,
        body: str,
        priority: NotificationPriority = NotificationPriority.INFO,
    ) -> bool:
        """Send a generic notification."""
        return await self._send(title, body, priority)


# Global notifier instance (lazy-loaded)
_notifier: Notifier | None = None


def get_notifier(
    settings: JoinMarketSettings | None = None,
    component_name: str = "",
) -> Notifier:
    """
    Get the global Notifier instance.

    The notifier is lazily initialized on first use. Configuration is loaded
    from JoinMarketSettings if provided, otherwise from environment variables.

    Args:
        settings: Optional JoinMarketSettings instance. If provided, notification
                  configuration will be taken from settings.notifications
                  (which supports config file + env vars + CLI args).
                  If None, falls back to environment variables only (legacy).
        component_name: Component name to include in notification titles.
            Examples: "Maker", "Taker", "Directory", "Orderbook Watcher".
            This makes it easier to identify which component sent a notification
            when running multiple JoinMarket components.

    Returns:
        Notifier instance
    """
    global _notifier
    if _notifier is None:
        if settings is not None:
            config = convert_settings_to_notification_config(settings, component_name)
        else:
            config = load_notification_config()
            # If component_name provided but no settings, update the config
            if component_name:
                config = NotificationConfig(
                    **{**config.model_dump(), "component_name": component_name}
                )
        _notifier = Notifier(config)
    return _notifier


def reset_notifier() -> None:
    """Reset the global notifier (useful for testing)."""
    global _notifier
    _notifier = None


__all__ = [
    "EVENT_TEMPLATES",
    "EventTemplate",
    "NotificationConfig",
    "NotificationEvent",
    "NotificationPriority",
    "Notifier",
    "convert_settings_to_notification_config",
    "get_notifier",
    "load_notification_config",
    "reset_notifier",
]
