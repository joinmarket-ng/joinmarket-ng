"""
Tests for the notification module.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmcore.notifications import (
    NotificationConfig,
    NotificationPriority,
    Notifier,
    convert_settings_to_notification_config,
    get_notifier,
    load_notification_config,
    reset_notifier,
)


class TestNotificationConfig:
    """Tests for NotificationConfig."""

    def test_default_config(self) -> None:
        """Test default configuration values."""
        config = NotificationConfig()

        assert config.enabled is False
        assert config.urls == []
        assert config.title_prefix == "JoinMarket NG"
        assert config.component_name == ""
        assert config.include_amounts is True
        assert config.include_txids is False
        assert config.include_nick is True
        assert config.notify_fill is True
        assert config.notify_rejection is True
        assert config.notify_peer_events is False  # Disabled by default
        assert config.notify_disconnect is False  # Disabled by default (noisy)
        assert config.notify_all_disconnect is True  # Enabled by default (critical)

    def test_config_from_dict(self) -> None:
        """Test creating config from dict."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            title_prefix="Test",
            component_name="Maker",
            include_amounts=False,
        )

        assert config.enabled is True
        assert [url.get_secret_value() for url in config.urls] == ["gotify://host/token"]
        assert config.title_prefix == "Test"
        assert config.component_name == "Maker"
        assert config.include_amounts is False

    def test_tor_config_defaults(self) -> None:
        """Test Tor configuration defaults."""
        config = NotificationConfig()

        assert config.use_tor is True

    def test_tor_config_custom(self) -> None:
        """Test custom Tor configuration."""
        config = NotificationConfig(
            use_tor=False,
        )

        assert config.use_tor is False


class TestLoadNotificationConfig:
    """Tests for load_notification_config."""

    def test_load_empty_env(self) -> None:
        """Test loading config with no environment variables."""
        with patch.dict(os.environ, {}, clear=True):
            config = load_notification_config()

        assert config.enabled is False
        assert config.urls == []

    def test_load_with_urls(self) -> None:
        """Test loading config with NOTIFICATIONS__URLS set."""
        env = {"NOTIFICATIONS__URLS": '["gotify://host/token", "tgram://bot/chat"]'}

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        assert config.enabled is True
        assert [url.get_secret_value() for url in config.urls] == [
            "gotify://host/token",
            "tgram://bot/chat",
        ]

    def test_load_with_quoted_urls(self) -> None:
        """Test loading config with quoted NOTIFICATIONS__URLS (JSON format)."""
        # The settings system uses JSON parsing for list values
        test_cases = [
            ('["gotify://host/token"]', ["gotify://host/token"]),
            (
                '["gotify://host/token", "tgram://bot/chat"]',
                ["gotify://host/token", "tgram://bot/chat"],
            ),
        ]

        for env_value, expected in test_cases:
            env = {"NOTIFICATIONS__URLS": env_value}
            with patch.dict(os.environ, env, clear=True):
                config = load_notification_config()

            assert config.enabled is True
            assert [url.get_secret_value() for url in config.urls] == expected

    def test_load_disabled_with_urls(self) -> None:
        """Test loading config with URLs but explicitly disabled."""
        env = {
            "NOTIFICATIONS__URLS": '["gotify://host/token"]',
            "NOTIFICATIONS__ENABLED": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        # Note: enabled becomes True because urls are provided (see convert_settings logic)
        # If you want to truly disable, you need to not provide URLs
        assert config.enabled is True  # URLs provided means enabled
        assert [url.get_secret_value() for url in config.urls] == ["gotify://host/token"]

    def test_load_privacy_settings(self) -> None:
        """Test loading privacy-related settings."""
        env = {
            "NOTIFICATIONS__URLS": '["gotify://host/token"]',
            "NOTIFICATIONS__INCLUDE_AMOUNTS": "false",
            "NOTIFICATIONS__INCLUDE_TXIDS": "true",
            "NOTIFICATIONS__INCLUDE_NICK": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        assert config.include_amounts is False
        assert config.include_txids is True
        assert config.include_nick is False

    def test_load_event_toggles(self) -> None:
        """Test loading per-event toggles."""
        env = {
            "NOTIFICATIONS__URLS": '["gotify://host/token"]',
            "NOTIFICATIONS__NOTIFY_FILL": "false",
            "NOTIFICATIONS__NOTIFY_SIGNING": "false",
            "NOTIFICATIONS__NOTIFY_PEER_EVENTS": "true",
            "NOTIFICATIONS__NOTIFY_STARTUP": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        assert config.notify_fill is False
        assert config.notify_signing is False
        assert config.notify_peer_events is True
        assert config.notify_startup is False
        # Defaults should remain
        assert config.notify_rejection is True
        assert config.notify_mempool is True

    def test_load_tor_settings(self) -> None:
        """Test loading Tor configuration from environment."""
        env = {
            "NOTIFICATIONS__URLS": '["gotify://host/token"]',
            "NOTIFICATIONS__USE_TOR": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        assert config.use_tor is False

    def test_load_tor_defaults(self) -> None:
        """Test that Tor is enabled by default with default host and port."""
        env = {"NOTIFICATIONS__URLS": '["gotify://host/token"]'}

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        assert config.use_tor is True
        assert config.tor_socks_host == "127.0.0.1"
        assert config.tor_socks_port == 9050

    def test_load_tor_custom_settings(self) -> None:
        """Test loading custom Tor proxy settings from environment."""
        env = {
            "NOTIFICATIONS__URLS": '["gotify://host/token"]',
            "TOR__SOCKS_HOST": "192.168.1.100",
            "TOR__SOCKS_PORT": "9150",
        }

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        assert config.use_tor is True
        assert config.tor_socks_host == "192.168.1.100"
        assert config.tor_socks_port == 9150


class TestNotifier:
    """Tests for Notifier class."""

    def test_notifier_disabled_by_default(self) -> None:
        """Test that notifier is disabled with empty config."""
        config = NotificationConfig()
        notifier = Notifier(config)

        assert notifier.config.enabled is False

    @pytest.mark.asyncio
    async def test_send_when_disabled(self) -> None:
        """Test that _send returns False when disabled."""
        config = NotificationConfig(enabled=False)
        notifier = Notifier(config)

        result = await notifier._send("Test", "Body")

        assert result is False

    @pytest.mark.asyncio
    async def test_send_when_no_urls(self) -> None:
        """Test that _send returns False when no URLs configured."""
        config = NotificationConfig(enabled=True, urls=[])
        notifier = Notifier(config)

        result = await notifier._send("Test", "Body")

        assert result is False

    def test_format_amount(self) -> None:
        """Test amount formatting."""
        config = NotificationConfig(include_amounts=True)
        notifier = Notifier(config)

        assert "sats" in notifier._format_amount(50000)
        assert "BTC" in notifier._format_amount(100_000_000)

    def test_format_amount_hidden(self) -> None:
        """Test amount formatting when privacy enabled."""
        config = NotificationConfig(include_amounts=False)
        notifier = Notifier(config)

        assert notifier._format_amount(50000) == "[hidden]"

    def test_format_nick(self) -> None:
        """Test nick formatting."""
        config = NotificationConfig(include_nick=True)
        notifier = Notifier(config)

        # Short nick
        assert notifier._format_nick("alice") == "alice"
        # Long nick (not truncated anymore)
        assert notifier._format_nick("verylongnickname") == "verylongnickname"

    def test_format_nick_hidden(self) -> None:
        """Test nick formatting when privacy enabled."""
        config = NotificationConfig(include_nick=False)
        notifier = Notifier(config)

        assert notifier._format_nick("alice") == "[hidden]"

    def test_format_txid(self) -> None:
        """Test txid formatting."""
        config = NotificationConfig(include_txids=True)
        notifier = Notifier(config)

        txid = "a" * 64
        formatted = notifier._format_txid(txid)
        assert "..." in formatted
        assert len(formatted) < len(txid)

    def test_format_txid_hidden(self) -> None:
        """Test txid formatting when privacy enabled."""
        config = NotificationConfig(include_txids=False)
        notifier = Notifier(config)

        assert notifier._format_txid("a" * 64) == "[hidden]"

    @pytest.mark.asyncio
    async def test_notify_fill_request_disabled(self) -> None:
        """Test that fill notification respects toggle."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_fill=False)
        notifier = Notifier(config)

        result = await notifier.notify_fill_request("taker", 100000, 0)

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_rejection_disabled(self) -> None:
        """Test that rejection notification respects toggle."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_rejection=False)
        notifier = Notifier(config)

        result = await notifier.notify_rejection("taker", "reason")

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_peer_events_disabled(self) -> None:
        """Test that peer event notifications respect toggle."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_peer_events=False)
        notifier = Notifier(config)

        result = await notifier.notify_peer_connected("alice", "onion", 10)

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_directory_disconnect_disabled_by_default(self) -> None:
        """Test that individual directory disconnect is disabled by default."""
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)

        assert config.notify_disconnect is False

        result = await notifier.notify_directory_disconnect("server1", 1, 3, reconnecting=True)

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_directory_disconnect_enabled(self) -> None:
        """Test that individual directory disconnect sends when enabled."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_disconnect=True)
        notifier = Notifier(config)

        # Mock _send to avoid needing apprise
        notifier._send = AsyncMock(return_value=True)

        result = await notifier.notify_directory_disconnect("server1", 1, 3, reconnecting=True)

        assert result is True
        notifier._send.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_directory_reconnect_disabled_by_default(self) -> None:
        """Test that directory reconnect notification is disabled by default."""
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)

        result = await notifier.notify_directory_reconnect("server1", 2, 3)

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_directory_reconnect_enabled(self) -> None:
        """Test that directory reconnect sends when notify_disconnect is enabled."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_disconnect=True)
        notifier = Notifier(config)

        notifier._send = AsyncMock(return_value=True)

        result = await notifier.notify_directory_reconnect("server1", 2, 3)

        assert result is True
        notifier._send.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_all_directories_disconnected_enabled_by_default(self) -> None:
        """Test that all-directories-disconnected is enabled by default."""
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)

        assert config.notify_all_disconnect is True

        notifier._send = AsyncMock(return_value=True)

        result = await notifier.notify_all_directories_disconnected()

        assert result is True
        notifier._send.assert_called_once()
        call_args = notifier._send.call_args
        assert "CRITICAL" in call_args[1]["title"] or "CRITICAL" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_notify_all_directories_disconnected_disabled(self) -> None:
        """Test that all-directories-disconnected respects toggle."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_all_disconnect=False)
        notifier = Notifier(config)

        result = await notifier.notify_all_directories_disconnected()

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_all_directories_reconnected_enabled_by_default(self) -> None:
        """Test that all-directories-reconnected is enabled by default (reuses notify_all_disconnect)."""
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)

        assert config.notify_all_disconnect is True

        notifier._send = AsyncMock(return_value=True)

        result = await notifier.notify_all_directories_reconnected(2, 3)

        assert result is True
        notifier._send.assert_called_once()
        call_args = notifier._send.call_args
        title = call_args[1].get("title") or call_args[0][0]
        assert "RESOLVED" in title or "Reconnected" in title

    @pytest.mark.asyncio
    async def test_notify_all_directories_reconnected_disabled(self) -> None:
        """Test that all-directories-reconnected respects notify_all_disconnect toggle."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_all_disconnect=False)
        notifier = Notifier(config)

        result = await notifier.notify_all_directories_reconnected(1, 3)

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_all_directories_reconnected_body_contains_counts(self) -> None:
        """Test that the recovery notification body includes connection counts."""
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)

        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_all_directories_reconnected(2, 3)

        call_args = notifier._send.call_args
        body = call_args[1].get("body") or call_args[0][1]
        assert "2/3" in body

    @pytest.mark.asyncio
    async def test_notify_startup_disabled(self) -> None:
        """Test that startup notification respects toggle."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_startup=False)
        notifier = Notifier(config)

        result = await notifier.notify_startup("maker", "1.0.0", "mainnet")

        assert result is False

    @pytest.mark.asyncio
    async def test_notify_with_mock_apprise(self) -> None:
        """Test notification with mocked apprise."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
        )
        notifier = Notifier(config)

        # Mock the apprise module
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1
        mock_apprise_instance.async_notify = AsyncMock(return_value=True)

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance
        mock_apprise_module.NotifyType.INFO = "info"

        with patch.dict("sys.modules", {"apprise": mock_apprise_module}):
            # Force re-initialization
            notifier._initialized = False
            notifier._apprise = None

            result = await notifier.notify_fill_request("taker123", 500000, 0)

        # Should succeed with mock
        assert result is True
        mock_apprise_instance.async_notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_title_with_component_name(self) -> None:
        """Test that notification title includes component name when set."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            title_prefix="JoinMarket NG",
            component_name="Maker",
        )
        notifier = Notifier(config)

        # Mock the apprise module
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1
        mock_apprise_instance.async_notify = AsyncMock(return_value=True)

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance
        mock_apprise_module.NotifyType.INFO = "info"

        with patch.dict("sys.modules", {"apprise": mock_apprise_module}):
            # Force re-initialization
            notifier._initialized = False
            notifier._apprise = None

            await notifier._send("Test Event", "Test body")

        # Verify the title includes component name
        mock_apprise_instance.async_notify.assert_called_once()
        call_kwargs = mock_apprise_instance.async_notify.call_args[1]
        assert call_kwargs["title"] == "JoinMarket NG (Maker): Test Event"

    @pytest.mark.asyncio
    async def test_notification_title_without_component_name(self) -> None:
        """Test that notification title works without component name."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            title_prefix="JoinMarket NG",
            component_name="",  # Empty component name
        )
        notifier = Notifier(config)

        # Mock the apprise module
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1
        mock_apprise_instance.async_notify = AsyncMock(return_value=True)

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance
        mock_apprise_module.NotifyType.INFO = "info"

        with patch.dict("sys.modules", {"apprise": mock_apprise_module}):
            # Force re-initialization
            notifier._initialized = False
            notifier._apprise = None

            await notifier._send("Test Event", "Test body")

        # Verify the title does not have parentheses when no component
        mock_apprise_instance.async_notify.assert_called_once()
        call_kwargs = mock_apprise_instance.async_notify.call_args[1]
        assert call_kwargs["title"] == "JoinMarket NG: Test Event"

    @pytest.mark.asyncio
    async def test_tor_proxy_configuration(self) -> None:
        """Test that Tor proxy environment variables are set correctly from config."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            use_tor=True,
            tor_socks_host="192.168.1.100",
            tor_socks_port=9150,
            stream_isolation=False,
        )
        notifier = Notifier(config)

        # Mock the apprise module
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance

        with patch.dict("sys.modules", {"apprise": mock_apprise_module}):
            # Force re-initialization
            notifier._initialized = False
            notifier._apprise = None

            await notifier._ensure_initialized()

            # Verify proxy environment variables were set with socks5h:// (DNS through proxy)
            assert os.environ.get("HTTP_PROXY") == "socks5h://192.168.1.100:9150"
            assert os.environ.get("HTTPS_PROXY") == "socks5h://192.168.1.100:9150"

    @pytest.mark.asyncio
    async def test_tor_proxy_with_stream_isolation(self) -> None:
        """Test that Tor proxy URL embeds isolation credentials when stream_isolation=True."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            use_tor=True,
            tor_socks_host="192.168.1.100",
            tor_socks_port=9150,
            stream_isolation=True,
        )
        notifier = Notifier(config)

        # Mock the apprise module
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance

        with patch.dict("sys.modules", {"apprise": mock_apprise_module}):
            # Force re-initialization
            notifier._initialized = False
            notifier._apprise = None

            await notifier._ensure_initialized()

            proxy_url = os.environ.get("HTTP_PROXY", "")
            # Should use socks5h:// with isolation credentials
            assert proxy_url.startswith("socks5h://jm-notification:")
            assert "@192.168.1.100:9150" in proxy_url
            assert os.environ.get("HTTPS_PROXY") == proxy_url

    @pytest.mark.asyncio
    async def test_tor_proxy_disabled(self) -> None:
        """Test that proxy is not set when Tor is disabled."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            use_tor=False,
        )
        notifier = Notifier(config)

        # Mock the apprise module
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance

        # Clear any existing proxy env vars
        env_clear = {k: v for k, v in os.environ.items() if k not in ["HTTP_PROXY", "HTTPS_PROXY"]}

        with (
            patch.dict("sys.modules", {"apprise": mock_apprise_module}),
            patch.dict(os.environ, env_clear, clear=True),
        ):
            # Force re-initialization
            notifier._initialized = False
            notifier._apprise = None

            await notifier._ensure_initialized()

            # Verify proxy environment variables were NOT set
            assert "HTTP_PROXY" not in os.environ
            assert "HTTPS_PROXY" not in os.environ


class TestGlobalNotifier:
    """Tests for global notifier functions."""

    def test_get_notifier_singleton(self) -> None:
        """Test that get_notifier returns same instance."""
        reset_notifier()

        n1 = get_notifier()
        n2 = get_notifier()

        assert n1 is n2

    def test_reset_notifier(self) -> None:
        """Test that reset_notifier clears the singleton."""
        reset_notifier()
        n1 = get_notifier()
        reset_notifier()
        n2 = get_notifier()

        assert n1 is not n2

    def test_get_notifier_with_component_name(self) -> None:
        """Test that get_notifier sets component_name in config."""
        reset_notifier()

        notifier = get_notifier(component_name="Taker")

        assert notifier.config.component_name == "Taker"

    def test_get_notifier_with_settings_and_component_name(self) -> None:
        """Test that get_notifier with settings uses component_name parameter."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        reset_notifier()

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
            )
        )

        notifier = get_notifier(settings, component_name="Maker")

        assert notifier.config.component_name == "Maker"


class TestNotificationPriority:
    """Tests for NotificationPriority enum."""

    def test_priority_values(self) -> None:
        """Test priority enum values."""
        assert NotificationPriority.INFO.value == "info"
        assert NotificationPriority.SUCCESS.value == "success"
        assert NotificationPriority.WARNING.value == "warning"
        assert NotificationPriority.FAILURE.value == "failure"


class TestNotifySummary:
    """Tests for notify_summary method."""

    @pytest.mark.asyncio
    async def test_summary_enabled_by_default(self) -> None:
        """Test that summary notification is enabled by default."""
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)

        assert config.notify_summary is True

        # Still returns False when called because notifications aren't truly sent in this test
        # (we'd need to mock _send), but the key is that notify_summary=True by default
        notifier._send = AsyncMock(return_value=True)

        result = await notifier.notify_summary(
            period_label="Daily",
            total_requests=5,
            successful=4,
            failed=1,
            total_earnings=1000,
            total_volume=5_000_000,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_summary_enabled_with_activity(self) -> None:
        """Test summary notification with CoinJoin activity."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        result = await notifier.notify_summary(
            period_label="Daily",
            total_requests=10,
            successful=8,
            failed=2,
            total_earnings=2500,
            total_volume=10_000_000,
            successful_volume=8_000_000,
            utxos_disclosed=15,
        )

        assert result is True
        notifier._send.assert_called_once()

        call_kwargs = notifier._send.call_args
        title = call_kwargs[1].get("title", call_kwargs[0][0] if call_kwargs[0] else "")
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "Daily Summary" in title
        assert "Requests: 10" in body
        assert "Successful: 8" in body
        assert "Failed: 2" in body
        assert "80%" in body
        assert "UTXOs disclosed: 15" in body

    @pytest.mark.asyncio
    async def test_summary_zero_activity(self) -> None:
        """Test summary notification with no activity in the period."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        result = await notifier.notify_summary(
            period_label="Weekly",
            total_requests=0,
            successful=0,
            failed=0,
            total_earnings=0,
            total_volume=0,
        )

        assert result is True
        notifier._send.assert_called_once()

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "No CoinJoin activity" in body

    @pytest.mark.asyncio
    async def test_summary_amounts_hidden(self) -> None:
        """Test that summary respects include_amounts toggle."""
        config = NotificationConfig(
            enabled=True,
            urls=["test://"],
            notify_summary=True,
            include_amounts=False,
        )
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=5,
            successful=5,
            failed=0,
            total_earnings=1000,
            total_volume=5_000_000,
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "[hidden]" in body

    def test_summary_interval_hours_validation(self) -> None:
        """Test that summary_interval_hours is validated to 1-168 range."""
        # Valid values
        config = NotificationConfig(summary_interval_hours=1)
        assert config.summary_interval_hours == 1
        config = NotificationConfig(summary_interval_hours=168)
        assert config.summary_interval_hours == 168

        # Invalid: too low
        with pytest.raises(ValueError):
            NotificationConfig(summary_interval_hours=0)

        # Invalid: too high
        with pytest.raises(ValueError):
            NotificationConfig(summary_interval_hours=169)

    @pytest.mark.asyncio
    async def test_summary_weekly_label(self) -> None:
        """Test summary with weekly period label."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Weekly",
            total_requests=3,
            successful=3,
            failed=0,
            total_earnings=750,
            total_volume=3_000_000,
        )

        call_kwargs = notifier._send.call_args
        title = call_kwargs[1].get("title", call_kwargs[0][0] if call_kwargs[0] else "")
        assert "Weekly Summary" in title

    @pytest.mark.asyncio
    async def test_summary_volume_split(self) -> None:
        """Test that volume shows successful / total format."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=5,
            successful=3,
            failed=2,
            total_earnings=750,
            total_volume=5_000_000,
            successful_volume=3_000_000,
            utxos_disclosed=8,
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        # Volume line should show "successful / total" format
        assert "Volume:" in body
        assert " / " in body
        assert "UTXOs disclosed: 8" in body

    @pytest.mark.asyncio
    async def test_summary_backward_compatible(self) -> None:
        """Test that notify_summary works without new optional parameters."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        # Call without the new parameters (backward compatibility)
        result = await notifier.notify_summary(
            period_label="Daily",
            total_requests=5,
            successful=5,
            failed=0,
            total_earnings=1000,
            total_volume=5_000_000,
        )

        assert result is True
        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "UTXOs disclosed: 0" in body
        # Version should not appear when not provided
        assert "Version:" not in body

    @pytest.mark.asyncio
    async def test_summary_with_version(self) -> None:
        """Test summary includes version when provided."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=5,
            successful=5,
            failed=0,
            total_earnings=1000,
            total_volume=5_000_000,
            version="0.15.0",
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "Version: 0.15.0" in body
        # No update info when update_available is None
        assert "update available" not in body

    @pytest.mark.asyncio
    async def test_summary_with_update_available(self) -> None:
        """Test summary shows update available when newer version exists."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=3,
            successful=3,
            failed=0,
            total_earnings=500,
            total_volume=3_000_000,
            version="0.15.0",
            update_available="0.16.0",
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "Version: 0.15.0" in body
        assert "(update available: 0.16.0)" in body

    @pytest.mark.asyncio
    async def test_summary_zero_activity_with_version(self) -> None:
        """Test zero-activity summary also shows version info."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=0,
            successful=0,
            failed=0,
            total_earnings=0,
            total_volume=0,
            version="0.15.0",
            update_available="0.16.0",
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "No CoinJoin activity" in body
        assert "Version: 0.15.0" in body
        assert "(update available: 0.16.0)" in body


class TestNotificationLogging:
    """Tests for notification logging."""

    def test_load_config_logs_enabled(self) -> None:
        """Test that loading config logs INFO when notifications enabled."""
        from io import StringIO

        from loguru import logger

        env = {"NOTIFICATIONS__URLS": '["gotify://host/token", "tgram://bot/chat"]'}
        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="INFO")

        try:
            with patch.dict(os.environ, env, clear=True):
                load_notification_config()
        finally:
            logger.remove(handler_id)

        log_output = output.getvalue()
        assert "Notifications enabled with 2 URL(s)" in log_output
        assert "use_tor=True" in log_output

    def test_load_config_logs_disabled_no_urls(self) -> None:
        """Test that loading config logs INFO when no URLs set."""
        from io import StringIO

        from loguru import logger

        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="INFO")

        try:
            with patch.dict(os.environ, {}, clear=True):
                load_notification_config()
        finally:
            logger.remove(handler_id)

        log_output = output.getvalue()
        assert "Notifications disabled (no URLs configured)" in log_output

    def test_load_config_logs_disabled_explicit(self) -> None:
        """Test that loading config logs disabled when no URLs (settings system auto-enables with URLs)."""
        from io import StringIO

        from loguru import logger

        # With the new settings system, notifications are auto-enabled if URLs are provided.
        # To disable, simply don't provide URLs. This test verifies no URLs = disabled.
        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="INFO")

        try:
            with patch.dict(os.environ, {}, clear=True):
                load_notification_config()
        finally:
            logger.remove(handler_id)

        log_output = output.getvalue()
        assert "Notifications disabled" in log_output

    @pytest.mark.asyncio
    async def test_send_logs_success_at_debug(self) -> None:
        """Test that successful notification sends log at DEBUG level."""
        from io import StringIO

        from loguru import logger

        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
        )
        notifier = Notifier(config)

        # Mock the apprise module
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1
        mock_apprise_instance.async_notify = AsyncMock(return_value=True)

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance
        mock_apprise_module.NotifyType.INFO = "info"

        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="DEBUG")

        try:
            with patch.dict("sys.modules", {"apprise": mock_apprise_module}):
                # Force re-initialization
                notifier._initialized = False
                notifier._apprise = None

                await notifier._send("Test Title", "Test body")
        finally:
            logger.remove(handler_id)

        log_output = output.getvalue()
        assert "Notification sent: Test Title" in log_output

    @pytest.mark.asyncio
    async def test_send_logs_failure_at_debug(self) -> None:
        """Test that failed notification sends log at DEBUG level."""
        from io import StringIO

        from loguru import logger

        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
        )
        notifier = Notifier(config)

        # Mock the apprise module
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1
        mock_apprise_instance.async_notify = AsyncMock(return_value=False)

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance
        mock_apprise_module.NotifyType.INFO = "info"

        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="DEBUG")

        try:
            with patch.dict("sys.modules", {"apprise": mock_apprise_module}):
                # Force re-initialization
                notifier._initialized = False
                notifier._apprise = None

                await notifier._send("Test Title", "Test body")
        finally:
            logger.remove(handler_id)

        log_output = output.getvalue()
        assert "Notification failed: Test Title" in log_output


class TestConvertSettingsToNotificationConfig:
    """Tests for convert_settings_to_notification_config function."""

    def test_convert_basic_settings(self) -> None:
        """Test converting basic notification settings."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                enabled=True,
                urls=["gotify://host/token", "tgram://bot/chat"],
                title_prefix="Test Prefix",
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.enabled is True
        assert len(config.urls) == 2
        assert config.urls[0].get_secret_value() == "gotify://host/token"
        assert config.urls[1].get_secret_value() == "tgram://bot/chat"
        assert config.title_prefix == "Test Prefix"

    def test_convert_privacy_settings(self) -> None:
        """Test converting privacy-related settings."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                include_amounts=False,
                include_txids=True,
                include_nick=False,
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.include_amounts is False
        assert config.include_txids is True
        assert config.include_nick is False

    def test_convert_event_toggles(self) -> None:
        """Test converting per-event notification toggles."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                notify_fill=False,
                notify_signing=False,
                notify_coinjoin_start=True,
                notify_peer_events=True,
                notify_disconnect=True,
                notify_all_disconnect=False,
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.notify_fill is False
        assert config.notify_signing is False
        assert config.notify_coinjoin_start is True
        assert config.notify_peer_events is True
        assert config.notify_disconnect is True
        assert config.notify_all_disconnect is False

    def test_convert_enabled_with_urls(self) -> None:
        """Test that having URLs automatically enables notifications."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                enabled=False,  # Explicitly disabled
                urls=["gotify://host/token"],  # But has URLs
            )
        )

        config = convert_settings_to_notification_config(settings)

        # Should be enabled because URLs are provided
        assert config.enabled is True

    def test_convert_disabled_no_urls(self) -> None:
        """Test that explicit enabled=False is respected."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                enabled=False,  # Explicitly disabled
                urls=[],
            )
        )

        config = convert_settings_to_notification_config(settings)

        # Should be disabled
        assert config.enabled is False

    def test_convert_tor_settings(self) -> None:
        """Test converting Tor proxy settings from JoinMarketSettings."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings, TorSettings

        settings = JoinMarketSettings(
            tor=TorSettings(
                socks_host="tor.example.com",
                socks_port=9999,
            ),
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                use_tor=True,
            ),
        )

        config = convert_settings_to_notification_config(settings)

        assert config.use_tor is True
        assert config.tor_socks_host == "tor.example.com"
        assert config.tor_socks_port == 9999

    def test_convert_component_name_from_parameter(self) -> None:
        """Test that component_name parameter overrides settings."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                component_name="Settings Component",
            )
        )

        config = convert_settings_to_notification_config(settings, component_name="Maker")

        # Parameter should override settings
        assert config.component_name == "Maker"

    def test_convert_component_name_from_settings(self) -> None:
        """Test that component_name falls back to settings when parameter is empty."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                component_name="Directory",
            )
        )

        config = convert_settings_to_notification_config(settings, component_name="")

        # Should use settings value
        assert config.component_name == "Directory"

    def test_convert_component_name_default(self) -> None:
        """Test that component_name defaults to empty string."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.component_name == ""

    def test_convert_summary_settings(self) -> None:
        """Test that summary notification settings are converted correctly."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                notify_summary=True,
                summary_interval_hours=168,
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.notify_summary is True
        assert config.summary_interval_hours == 168

    def test_convert_check_for_updates_setting(self) -> None:
        """Test that check_for_updates setting is converted correctly."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        # Default: disabled
        settings = JoinMarketSettings(
            notifications=NotificationSettings(urls=["gotify://host/token"])
        )
        config = convert_settings_to_notification_config(settings)
        assert config.check_for_updates is False

        # Explicitly enabled
        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                check_for_updates=True,
            )
        )
        config = convert_settings_to_notification_config(settings)
        assert config.check_for_updates is True

    def test_convert_retry_settings(self) -> None:
        """Test that retry settings are converted correctly."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                retry_enabled=False,
                retry_max_attempts=5,
                retry_base_delay=10.0,
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.retry_enabled is False
        assert config.retry_max_attempts == 5
        assert config.retry_base_delay == 10.0

    def test_convert_retry_settings_defaults(self) -> None:
        """Test that retry settings use sensible defaults."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.retry_enabled is True
        assert config.retry_max_attempts == 3
        assert config.retry_base_delay == 5.0

    def test_defaults_match_between_settings_and_config(self) -> None:
        """Guard against default value drift between NotificationSettings and NotificationConfig.

        NotificationSettings (in settings.py) is the canonical source of defaults.
        NotificationConfig (in notifications.py) must have matching defaults for all
        shared fields. A mismatch means the runtime behavior (which goes through
        settings) will differ from direct NotificationConfig() construction (used in tests),
        leading to subtle bugs like notify_summary silently being disabled.
        """
        from jmcore.settings import NotificationSettings

        settings_fields = NotificationSettings.model_fields
        config_fields = NotificationConfig.model_fields

        # Fields that exist in NotificationConfig but NOT in NotificationSettings
        # (they come from other sources like TorSettings)
        config_only_fields = {"tor_socks_host", "tor_socks_port"}

        shared_fields = set(settings_fields) & set(config_fields) - config_only_fields

        mismatches = []
        for field_name in sorted(shared_fields):
            settings_default = settings_fields[field_name].default
            config_default = config_fields[field_name].default

            if settings_default != config_default:
                mismatches.append(
                    f"  {field_name}: "
                    f"NotificationSettings={settings_default!r}, "
                    f"NotificationConfig={config_default!r}"
                )

        assert not mismatches, (
            "Default value mismatch between NotificationSettings and NotificationConfig.\n"
            "NotificationSettings (settings.py) is the canonical source of defaults.\n"
            "Update NotificationConfig to match:\n" + "\n".join(mismatches)
        )


class TestNotificationRetry:
    """Tests for notification retry with exponential backoff."""

    def test_retry_config_defaults(self) -> None:
        """Test default retry configuration values."""
        config = NotificationConfig()

        assert config.retry_enabled is True
        assert config.retry_max_attempts == 3
        assert config.retry_base_delay == 5.0

    def test_retry_config_validation(self) -> None:
        """Test retry config validation bounds."""
        # Valid bounds
        config = NotificationConfig(retry_max_attempts=1, retry_base_delay=1.0)
        assert config.retry_max_attempts == 1
        assert config.retry_base_delay == 1.0

        config = NotificationConfig(retry_max_attempts=10, retry_base_delay=60.0)
        assert config.retry_max_attempts == 10
        assert config.retry_base_delay == 60.0

        # Out of bounds
        with pytest.raises(ValueError):
            NotificationConfig(retry_max_attempts=0)

        with pytest.raises(ValueError):
            NotificationConfig(retry_max_attempts=11)

        with pytest.raises(ValueError):
            NotificationConfig(retry_base_delay=0.5)

        with pytest.raises(ValueError):
            NotificationConfig(retry_base_delay=61.0)

    @pytest.mark.asyncio
    async def test_retry_scheduled_on_failure(self) -> None:
        """Test that a background retry task is spawned when _send fails."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=2,
            retry_base_delay=1.0,
        )
        notifier = Notifier(config)

        # Pre-initialize so _ensure_initialized passes
        notifier._initialized = True
        notifier._apprise = MagicMock()

        # First call fails, second call (retry) succeeds
        notifier._try_send = AsyncMock(side_effect=[False, True])

        with patch("jmcore.notifications.asyncio.sleep", new_callable=AsyncMock):
            result = await notifier._send("Test", "Body")

            # First attempt returned False
            assert result is False

            # A retry task should have been scheduled
            assert len(notifier._retry_tasks) == 1

            # Wait for retry tasks to complete
            await asyncio.gather(*notifier._retry_tasks)

        # Retry should have called _try_send a second time
        assert notifier._try_send.call_count == 2

        # Task should be cleaned up after completion
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_retry_not_scheduled_when_disabled(self) -> None:
        """Test that no retry is scheduled when retry_enabled is False."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=False,
        )
        notifier = Notifier(config)

        notifier._initialized = True
        notifier._apprise = MagicMock()
        notifier._try_send = AsyncMock(return_value=False)

        result = await notifier._send("Test", "Body")

        assert result is False
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_no_retry_when_notifier_disabled(self) -> None:
        """Test that no retry is scheduled when notifications are disabled entirely."""
        config = NotificationConfig(
            enabled=False,
            retry_enabled=True,
        )
        notifier = Notifier(config)

        result = await notifier._send("Test", "Body")

        assert result is False
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_no_retry_when_no_urls(self) -> None:
        """Test that no retry is scheduled when no URLs are configured."""
        config = NotificationConfig(
            enabled=True,
            urls=[],
            retry_enabled=True,
        )
        notifier = Notifier(config)

        result = await notifier._send("Test", "Body")

        assert result is False
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self) -> None:
        """Test that no retry is scheduled when the first send succeeds."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
        )
        notifier = Notifier(config)

        notifier._initialized = True
        notifier._apprise = MagicMock()
        notifier._try_send = AsyncMock(return_value=True)

        result = await notifier._send("Test", "Body")

        assert result is True
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_retry_gives_up_after_max_attempts(self) -> None:
        """Test that retry gives up after max_attempts."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=2,
            retry_base_delay=1.0,
        )
        notifier = Notifier(config)

        notifier._initialized = True
        notifier._apprise = MagicMock()
        # All attempts fail
        notifier._try_send = AsyncMock(return_value=False)

        with patch("jmcore.notifications.asyncio.sleep", new_callable=AsyncMock):
            result = await notifier._send("Test", "Body")
            assert result is False

            # Wait for all retries to complete
            tasks = list(notifier._retry_tasks)
            await asyncio.gather(*tasks)

        # Initial call + 2 retries = 3 calls total
        assert notifier._try_send.call_count == 3
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self) -> None:
        """Test that retry stops after a successful attempt."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=3,
            retry_base_delay=1.0,
        )
        notifier = Notifier(config)

        notifier._initialized = True
        notifier._apprise = MagicMock()
        # First call (from _send) fails, first retry fails, second retry succeeds
        notifier._try_send = AsyncMock(side_effect=[False, False, True])

        with patch("jmcore.notifications.asyncio.sleep", new_callable=AsyncMock):
            await notifier._send("Test", "Body")

            # Wait for retries to complete
            tasks = list(notifier._retry_tasks)
            await asyncio.gather(*tasks)

        # Initial + 2 retries (stopped early because 2nd retry succeeded)
        assert notifier._try_send.call_count == 3
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_retry_exponential_backoff(self) -> None:
        """Test that retry uses exponential backoff (delay doubles each attempt)."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=3,
            retry_base_delay=5.0,
        )
        notifier = Notifier(config)

        notifier._initialized = True
        notifier._apprise = MagicMock()
        notifier._try_send = AsyncMock(return_value=False)

        sleep_delays: list[float] = []

        async def mock_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        with patch("jmcore.notifications.asyncio.sleep", side_effect=mock_sleep):
            await notifier._send("Test", "Body")
            # Wait for background task
            tasks = list(notifier._retry_tasks)
            await asyncio.gather(*tasks)

        # Should have 3 delays: base, base*2, base*4
        assert len(sleep_delays) == 3
        assert sleep_delays[0] == pytest.approx(5.0)
        assert sleep_delays[1] == pytest.approx(10.0)
        assert sleep_delays[2] == pytest.approx(20.0)

    @pytest.mark.asyncio
    async def test_retry_exception_does_not_crash(self) -> None:
        """Test that exceptions during retry don't crash the background task."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=2,
            retry_base_delay=1.0,
        )
        notifier = Notifier(config)

        notifier._initialized = True
        notifier._apprise = MagicMock()
        # First call fails normally, retries raise exceptions
        notifier._try_send = AsyncMock(
            side_effect=[False, ConnectionError("Tor circuit failed"), True]
        )

        with patch("jmcore.notifications.asyncio.sleep", new_callable=AsyncMock):
            await notifier._send("Test", "Body")

            # Wait for retries
            tasks = list(notifier._retry_tasks)
            await asyncio.gather(*tasks)

        # All 3 calls should have been made (exception didn't stop retries)
        assert notifier._try_send.call_count == 3
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_retry_task_cleanup_on_completion(self) -> None:
        """Test that retry tasks are removed from _retry_tasks set after completion."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=1,
            retry_base_delay=1.0,
        )
        notifier = Notifier(config)

        notifier._initialized = True
        notifier._apprise = MagicMock()
        notifier._try_send = AsyncMock(return_value=False)

        with patch("jmcore.notifications.asyncio.sleep", new_callable=AsyncMock):
            await notifier._send("Test1", "Body")
            await notifier._send("Test2", "Body")

            # Two retry tasks should be pending
            assert len(notifier._retry_tasks) == 2

            # Wait for retries to complete
            tasks = list(notifier._retry_tasks)
            await asyncio.gather(*tasks)

        # All tasks should be cleaned up
        assert len(notifier._retry_tasks) == 0

    @pytest.mark.asyncio
    async def test_retry_does_not_block_caller(self) -> None:
        """Test that _send returns immediately even when retry is pending."""
        import time

        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=3,
            retry_base_delay=1.0,  # Long delay
        )
        notifier = Notifier(config)

        notifier._initialized = True
        notifier._apprise = MagicMock()
        notifier._try_send = AsyncMock(return_value=False)

        start = time.monotonic()
        result = await notifier._send("Test", "Body")
        elapsed = time.monotonic() - start

        # _send should return almost immediately (well under the retry delay)
        assert result is False
        assert elapsed < 0.5  # Much less than the 1.0s retry delay
        assert len(notifier._retry_tasks) == 1

        # Clean up: cancel the pending task
        for task in notifier._retry_tasks:
            task.cancel()

    @pytest.mark.asyncio
    async def test_retry_with_real_apprise_mock(self) -> None:
        """Test retry integrates correctly with the full _send/_try_send flow."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=2,
            retry_base_delay=1.0,
        )
        notifier = Notifier(config)

        # Mock the apprise module with failing then succeeding sends
        mock_apprise_instance = MagicMock()
        mock_apprise_instance.add.return_value = True
        mock_apprise_instance.__len__ = lambda self: 1
        mock_apprise_instance.async_notify = AsyncMock(side_effect=[False, False, True])

        mock_apprise_module = MagicMock()
        mock_apprise_module.Apprise.return_value = mock_apprise_instance
        mock_apprise_module.NotifyType.INFO = "info"

        with (
            patch.dict("sys.modules", {"apprise": mock_apprise_module}),
            patch("jmcore.notifications.asyncio.sleep", new_callable=AsyncMock),
        ):
            notifier._initialized = False
            notifier._apprise = None

            result = await notifier._send("Test Event", "Test body")

            # First attempt failed
            assert result is False

            # Wait for retries
            tasks = list(notifier._retry_tasks)
            await asyncio.gather(*tasks)

        # 1 initial + 2 retries = 3 calls, last one succeeded
        assert mock_apprise_instance.async_notify.call_count == 3
        assert len(notifier._retry_tasks) == 0
