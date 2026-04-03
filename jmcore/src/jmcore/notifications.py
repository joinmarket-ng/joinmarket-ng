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
        """
        Send a periodic summary notification with CoinJoin statistics.

        Args:
            period_label: Human-readable period (e.g., "Daily", "Weekly")
            total_requests: Total CoinJoin requests in the period
            successful: Number of successful CoinJoins
            failed: Number of failed CoinJoins
            total_earnings: Total fees earned in sats
            total_volume: Total CoinJoin volume in sats (all requests)
            successful_volume: CoinJoin volume in sats (successful only)
            utxos_disclosed: Number of UTXOs disclosed to takers
            version: Current version string (e.g., "0.15.0"), shown if provided
            update_available: Latest version string if an update is available, None otherwise
            total_balance: Total wallet balance in sats (only included when
                           notify_summary_balance is enabled)
            utxo_count: Total number of spendable UTXOs (only included when
                        notify_summary_balance is enabled)
        """
        if not self.config.notify_summary:
            return False

        if total_requests == 0:
            body = f"Period: {period_label}\nNo CoinJoin activity in this period."
        else:
            success_rate = successful / total_requests * 100 if total_requests > 0 else 0.0
            body = (
                f"Period: {period_label}\n"
                f"Requests: {total_requests}\n"
                f"Successful: {successful}\n"
                f"Failed: {failed}\n"
                f"Success rate: {success_rate:.0f}%\n"
                f"Earnings: {self._format_amount(total_earnings)}\n"
                f"Volume: {self._format_amount(successful_volume)}"
                f" / {self._format_amount(total_volume)}\n"
                f"UTXOs disclosed: {utxos_disclosed}"
            )

        # Append wallet balance info if enabled and provided
        if self.config.notify_summary_balance:
            if total_balance is not None:
                body += f"\nBalance: {self._format_amount(total_balance)}"
            if utxo_count is not None:
                body += f"\nUTXOs: {utxo_count}"

        # Append version info if provided
        if version:
            body += f"\nVersion: {version}"
            if update_available:
                body += f" (update available: {update_available})"

        return await self._send(
            title=f"{period_label} Summary",
            body=body,
            priority=NotificationPriority.INFO,
        )

    async def notify_fill_request(
        self,
        taker_nick: str,
        cj_amount: int,
        offer_id: int,
    ) -> bool:
        """Notify when a !fill request is received (maker)."""
        if not self.config.notify_fill:
            return False

        return await self._send(
            title="Fill Request Received",
            body=(
                f"Taker: {self._format_nick(taker_nick)}\n"
                f"Amount: {self._format_amount(cj_amount)}\n"
                f"Offer ID: {offer_id}"
            ),
            priority=NotificationPriority.INFO,
        )

    async def notify_rejection(
        self,
        taker_nick: str,
        reason: str,
        details: str = "",
    ) -> bool:
        """Notify when rejecting a taker request (maker)."""
        if not self.config.notify_rejection:
            return False

        body = f"Taker: {self._format_nick(taker_nick)}\nReason: {reason}"
        if details:
            body += f"\nDetails: {details}"

        return await self._send(
            title="Request Rejected",
            body=body,
            priority=NotificationPriority.WARNING,
        )

    async def notify_tx_signed(
        self,
        taker_nick: str,
        cj_amount: int,
        num_inputs: int,
        fee_earned: int,
    ) -> bool:
        """Notify when transaction is signed (maker)."""
        if not self.config.notify_signing:
            return False

        return await self._send(
            title="Transaction Signed",
            body=(
                f"Taker: {self._format_nick(taker_nick)}\n"
                f"CJ Amount: {self._format_amount(cj_amount)}\n"
                f"Inputs signed: {num_inputs}\n"
                f"Fee earned: {self._format_amount(fee_earned)}"
            ),
            priority=NotificationPriority.SUCCESS,
        )

    async def notify_mempool(
        self,
        txid: str,
        cj_amount: int,
        role: str = "maker",
    ) -> bool:
        """Notify when CoinJoin is seen in mempool."""
        if not self.config.notify_mempool:
            return False

        return await self._send(
            title="CoinJoin in Mempool",
            body=(
                f"Role: {role.capitalize()}\n"
                f"TxID: {self._format_txid(txid)}\n"
                f"Amount: {self._format_amount(cj_amount)}"
            ),
            priority=NotificationPriority.INFO,
        )

    async def notify_confirmed(
        self,
        txid: str,
        cj_amount: int,
        confirmations: int,
        role: str = "maker",
    ) -> bool:
        """Notify when CoinJoin is confirmed."""
        if not self.config.notify_confirmed:
            return False

        return await self._send(
            title="CoinJoin Confirmed",
            body=(
                f"Role: {role.capitalize()}\n"
                f"TxID: {self._format_txid(txid)}\n"
                f"Amount: {self._format_amount(cj_amount)}\n"
                f"Confirmations: {confirmations}"
            ),
            priority=NotificationPriority.SUCCESS,
        )

    async def notify_nick_change(
        self,
        old_nick: str,
        new_nick: str,
    ) -> bool:
        """Notify when maker nick changes (privacy feature)."""
        if not self.config.notify_nick_change:
            return False

        return await self._send(
            title="Nick Changed",
            body=(f"Old: {self._format_nick(old_nick)}\nNew: {self._format_nick(new_nick)}"),
            priority=NotificationPriority.INFO,
        )

    async def notify_directory_disconnect(
        self,
        server: str,
        connected_count: int,
        total_count: int,
        reconnecting: bool = True,
    ) -> bool:
        """Notify when disconnected from a directory server."""
        if not self.config.notify_disconnect:
            return False

        status = "reconnecting" if reconnecting else "disconnected"
        priority = NotificationPriority.WARNING
        if connected_count == 0:
            priority = NotificationPriority.FAILURE

        return await self._send(
            title="Directory Server Disconnected",
            body=(
                f"Server: {server[:30]}...\n"
                f"Status: {status}\n"
                f"Connected: {connected_count}/{total_count}"
            ),
            priority=priority,
        )

    async def notify_all_directories_disconnected(self) -> bool:
        """Notify when disconnected from ALL directory servers (critical)."""
        if not self.config.notify_all_disconnect:
            return False

        return await self._send(
            title="CRITICAL: All Directories Disconnected",
            body=(
                "Lost connection to ALL directory servers.\n"
                "No CoinJoins possible until reconnected.\n"
                "Check network connectivity and Tor status."
            ),
            priority=NotificationPriority.FAILURE,
        )

    async def notify_all_directories_reconnected(
        self,
        connected_count: int,
        total_count: int,
    ) -> bool:
        """Notify when at least one directory server is reconnected after all were lost (recovery)."""
        if not self.config.notify_all_disconnect:
            return False

        return await self._send(
            title="RESOLVED: Directory Servers Reconnected",
            body=(
                f"Reconnected to directory servers ({connected_count}/{total_count}).\n"
                "CoinJoins are possible again."
            ),
            priority=NotificationPriority.SUCCESS,
        )

    async def notify_directory_reconnect(
        self,
        server: str,
        connected_count: int,
        total_count: int,
    ) -> bool:
        """Notify when successfully reconnected to a directory server."""
        if not self.config.notify_disconnect:
            return False

        return await self._send(
            title="Directory Server Reconnected",
            body=(f"Server: {server[:30]}...\nConnected: {connected_count}/{total_count}"),
            priority=NotificationPriority.SUCCESS,
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
        if not self.config.notify_coinjoin_start:
            return False

        dest_display = "internal" if destination == "INTERNAL" else f"{destination[:12]}..."

        return await self._send(
            title="CoinJoin Started",
            body=(
                f"Amount: {self._format_amount(cj_amount)}\n"
                f"Makers: {num_makers}\n"
                f"Destination: {dest_display}"
            ),
            priority=NotificationPriority.INFO,
        )

    async def notify_coinjoin_complete(
        self,
        txid: str,
        cj_amount: int,
        num_makers: int,
        total_fees: int,
    ) -> bool:
        """Notify when CoinJoin completes successfully (taker)."""
        if not self.config.notify_coinjoin_complete:
            return False

        return await self._send(
            title="CoinJoin Complete",
            body=(
                f"TxID: {self._format_txid(txid)}\n"
                f"Amount: {self._format_amount(cj_amount)}\n"
                f"Makers: {num_makers}\n"
                f"Total fees: {self._format_amount(total_fees)}"
            ),
            priority=NotificationPriority.SUCCESS,
        )

    async def notify_coinjoin_failed(
        self,
        reason: str,
        phase: str = "",
        cj_amount: int = 0,
    ) -> bool:
        """Notify when CoinJoin fails (taker)."""
        if not self.config.notify_coinjoin_failed:
            return False

        body = f"Reason: {reason}"
        if phase:
            body = f"Phase: {phase}\n" + body
        if cj_amount > 0:
            body += f"\nAmount: {self._format_amount(cj_amount)}"

        return await self._send(
            title="CoinJoin Failed",
            body=body,
            priority=NotificationPriority.FAILURE,
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
        if not self.config.notify_peer_events:
            return False

        return await self._send(
            title="Peer Connected",
            body=(
                f"Nick: {self._format_nick(nick)}\n"
                f"Location: {location[:30]}...\n"
                f"Total peers: {total_peers}"
            ),
            priority=NotificationPriority.INFO,
        )

    async def notify_peer_disconnected(
        self,
        nick: str,
        total_peers: int,
    ) -> bool:
        """Notify when a peer disconnects (directory server)."""
        if not self.config.notify_peer_events:
            return False

        return await self._send(
            title="Peer Disconnected",
            body=(f"Nick: {self._format_nick(nick)}\nRemaining peers: {total_peers}"),
            priority=NotificationPriority.INFO,
        )

    async def notify_peer_banned(
        self,
        nick: str,
        reason: str,
        duration: int,
    ) -> bool:
        """Notify when a peer is banned for rate limit violations."""
        if not self.config.notify_rate_limit:
            return False

        return await self._send(
            title="Peer Banned",
            body=(f"Nick: {self._format_nick(nick)}\nReason: {reason}\nDuration: {duration}s"),
            priority=NotificationPriority.WARNING,
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
        return await self._send(
            title="Orderbook Status",
            body=(
                f"Directories: {connected_directories}/{total_directories}\n"
                f"Offers: {total_offers}\n"
                f"Makers: {total_makers}"
            ),
            priority=NotificationPriority.INFO,
        )

    async def notify_maker_offline(
        self,
        nick: str,
        last_seen: str,
    ) -> bool:
        """Notify when a maker goes offline."""
        return await self._send(
            title="Maker Offline",
            body=(f"Nick: {self._format_nick(nick)}\nLast seen: {last_seen}"),
            priority=NotificationPriority.INFO,
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
        """
        Notify when a component starts up.

        Args:
            component: Component name (e.g., "Maker", "Taker", "Directory", "Orderbook Watcher")
            version: Optional version string
            network: Optional network name (e.g., "mainnet", "signet")
            nick: Optional component nick (e.g., "J5XXXXXXXXX")
        """
        if not self.config.notify_startup:
            return False

        body = f"Component: {component}"
        if nick:
            body += f"\nNick: {self._format_nick(nick)}"
        if version:
            body += f"\nVersion: {version}"
        if network:
            body += f"\nNetwork: {network}"

        return await self._send(
            title="Component Started",
            body=body,
            priority=NotificationPriority.INFO,
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
    "NotificationConfig",
    "NotificationPriority",
    "Notifier",
    "get_notifier",
    "reset_notifier",
    "load_notification_config",
    "convert_settings_to_notification_config",
]
