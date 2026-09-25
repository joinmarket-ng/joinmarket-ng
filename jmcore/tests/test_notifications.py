"""
Tests for the notification module.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from jmcore._notification_worker import (
    PROXY_ENVIRONMENT_KEYS,
    AppriseWorker,
    NotificationWorkerConfig,
    NotificationWorkerResult,
    _AppriseDiagnosticHandler,
    _configure_worker_environment,
    _sanitize_worker_diagnostic,
)
from jmcore.notifications import (
    NotificationConfig,
    NotificationPriority,
    Notifier,
    convert_settings_to_notification_config,
    get_notifier,
    load_notification_config,
    reset_notifier,
)
from jmcore.tor_isolation import IsolationCategory


@pytest.fixture(autouse=True)
def isolate_notification_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty-env cases must not discover the operator's real config through Path.home()."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


class RecordingNotificationWorker:
    """Deterministic worker seam for notifier tests."""

    def __init__(
        self,
        send_results: list[NotificationWorkerResult] | None = None,
        start_result: NotificationWorkerResult | None = None,
    ):
        self.config: NotificationWorkerConfig | None = None
        self.send_results = send_results or []
        self.start_result = start_result or NotificationWorkerResult(True)
        self.calls: list[tuple[str, str, str]] = []
        self.closed = False

    def __call__(self, config: NotificationWorkerConfig) -> RecordingNotificationWorker:
        self.config = config
        return self

    def start(self) -> NotificationWorkerResult:
        return self.start_result

    def send(self, title: str, body: str, priority: str) -> NotificationWorkerResult:
        self.calls.append((title, body, priority))
        return self.send_results.pop(0) if self.send_results else NotificationWorkerResult(True)

    def close(self) -> None:
        self.closed = True


class BlockingStartNotificationWorker(RecordingNotificationWorker):
    """Worker seam that exposes delayed completion of a blocking start call."""

    def __init__(self) -> None:
        super().__init__()
        self.start_started = threading.Event()
        self.release_start = threading.Event()
        self.close_completed = threading.Event()

    def start(self) -> NotificationWorkerResult:
        self.start_started.set()
        self.release_start.wait()
        return NotificationWorkerResult(True)

    def close(self) -> None:
        super().close()
        self.close_completed.set()


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
        assert config.include_coinjoin_id is True
        assert config.include_nick is True
        assert config.notify_fill is True
        assert config.notify_rejection is True
        assert config.notify_nick_change is False  # Disabled by default (noisy)
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


class TestCoinJoinNotificationIDs:
    """CoinJoin notifications include an optional log correlation ID."""

    @pytest.mark.parametrize(
        ("method_name", "args"),
        [
            ("notify_fill_request", ("taker", 100_000, 1, "cj-abcdef123456")),
            ("notify_rejection", ("taker", "reason", "details", "cj-abcdef123456")),
            ("notify_tx_signed", ("taker", 100_000, 1, 500, "cj-abcdef123456")),
            ("notify_coinjoin_start", (100_000, 2, "INTERNAL", "cj-abcdef123456")),
            (
                "notify_coinjoin_complete",
                ("ab" * 32, 100_000, 2, 500, "cj-abcdef123456"),
            ),
            ("notify_coinjoin_failed", ("reason", "phase", 100_000, "cj-abcdef123456")),
        ],
    )
    @pytest.mark.parametrize("include_coinjoin_id", [True, False])
    @pytest.mark.asyncio
    async def test_coinjoin_id_respects_privacy_setting(
        self,
        method_name: str,
        args: tuple[object, ...],
        include_coinjoin_id: bool,
    ) -> None:
        notifier = Notifier(
            NotificationConfig(
                enabled=True,
                urls=["test://"],
                include_coinjoin_id=include_coinjoin_id,
            )
        )
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await getattr(notifier, method_name)(*args)

        body = notifier._send.call_args.kwargs["body"]
        assert ("CoinJoin ID: cj-abcdef123456" in body) is include_coinjoin_id

    @pytest.mark.asyncio
    async def test_coinjoin_start_id_is_optional_and_rendered_when_present(self) -> None:
        notifier = Notifier(NotificationConfig(enabled=True, urls=["test://"]))
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await notifier.notify_coinjoin_start(100_000, 2, "INTERNAL")
        assert "CoinJoin ID:" not in notifier._send.call_args.kwargs["body"]

        await notifier.notify_coinjoin_start(100_000, 2, "INTERNAL", "cj-abcdef123456")
        assert "CoinJoin ID: cj-abcdef123456" in notifier._send.call_args.kwargs["body"]

    @pytest.mark.asyncio
    async def test_coinjoin_complete_reports_broadcast_outcome(self) -> None:
        notifier = Notifier(NotificationConfig(enabled=True, urls=["test://"]))
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await notifier.notify_coinjoin_complete(
            "ab" * 32,
            100_000,
            2,
            500,
            broadcast_method="maker:J5maker",
        )
        assert "Broadcast: maker:J5maker" in notifier._send.call_args.kwargs["body"]
        assert notifier._send.call_args.kwargs["priority"] == NotificationPriority.SUCCESS

        await notifier.notify_coinjoin_complete(
            "cd" * 32,
            100_000,
            2,
            500,
            broadcast_method="self-fallback",
            broadcast_fallback_reason="peer_delivery_failed",
        )
        call_kwargs = notifier._send.call_args.kwargs
        assert "Broadcast: self-fallback" in call_kwargs["body"]
        assert "Privacy fallback: peer_delivery_failed" in call_kwargs["body"]
        assert call_kwargs["priority"] == NotificationPriority.WARNING


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

    def test_load_scalar_url_from_toml(self, tmp_path: Path) -> None:
        """A bare string ``urls`` in config.toml is coerced into a list.

        Regression: users wrote ``urls = "tgram://..."`` (a TOML scalar)
        instead of a list and the maker crashed at startup with a bare
        exit code 1 (systemd ``status=1/FAILURE``). The scalar form must
        now load as a single-element list and auto-enable notifications.
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[notifications]\nurls = "tgram://bottoken/ChatID"\n',
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"JOINMARKET_CONFIG_FILE": str(config_file)}, clear=True):
            config = load_notification_config()

        assert config.enabled is True
        assert [url.get_secret_value() for url in config.urls] == ["tgram://bottoken/ChatID"]

    def test_load_comma_separated_urls_from_toml(self, tmp_path: Path) -> None:
        """A comma-separated ``urls`` scalar in config.toml splits into a list."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[notifications]\nurls = "tgram://bot/chat, gotify://host/token"\n',
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"JOINMARKET_CONFIG_FILE": str(config_file)}, clear=True):
            config = load_notification_config()

        assert [url.get_secret_value() for url in config.urls] == [
            "tgram://bot/chat",
            "gotify://host/token",
        ]

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
            "NOTIFICATIONS__INCLUDE_COINJOIN_ID": "false",
            "NOTIFICATIONS__INCLUDE_NICK": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        assert config.include_amounts is False
        assert config.include_txids is True
        assert config.include_coinjoin_id is False
        assert config.include_nick is False

    def test_load_coinjoin_id_setting_from_toml(self, tmp_path: Path) -> None:
        """The CoinJoin ID privacy option can be disabled in config.toml."""
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            "[notifications]\ninclude_coinjoin_id = false\n",
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"JOINMARKET_CONFIG_FILE": str(config_file)}, clear=True):
            config = load_notification_config()

        assert config.include_coinjoin_id is False

    def test_load_event_toggles(self) -> None:
        """Test loading per-event toggles."""
        env = {
            "NOTIFICATIONS__URLS": '["gotify://host/token"]',
            "NOTIFICATIONS__NOTIFY_FILL": "false",
            "NOTIFICATIONS__NOTIFY_SIGNING": "false",
            "NOTIFICATIONS__NOTIFY_NICK_CHANGE": "true",
            "NOTIFICATIONS__NOTIFY_PEER_EVENTS": "true",
            "NOTIFICATIONS__NOTIFY_STARTUP": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = load_notification_config()

        assert config.notify_fill is False
        assert config.notify_signing is False
        assert config.notify_nick_change is True
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


class TestNotificationSettingsUrlCoercion:
    """Tests for NotificationSettings.urls accepting scalar/string inputs."""

    def test_single_string_is_wrapped_in_list(self) -> None:
        """A bare URL string is wrapped into a one-element list."""
        from jmcore.settings import NotificationSettings

        ns = NotificationSettings(urls="tgram://bottoken/ChatID")  # type: ignore[arg-type]

        assert ns.urls == ["tgram://bottoken/ChatID"]

    def test_comma_separated_string_is_split(self) -> None:
        """A comma-separated URL string splits into multiple entries."""
        from jmcore.settings import NotificationSettings

        ns = NotificationSettings(urls="tgram://bot/chat, gotify://host/token")  # type: ignore[arg-type]

        assert ns.urls == ["tgram://bot/chat", "gotify://host/token"]

    def test_json_array_string_is_parsed(self) -> None:
        """A JSON-array string is parsed into a list (env/CLI ergonomics)."""
        from jmcore.settings import NotificationSettings

        ns = NotificationSettings(urls='["tgram://bot/chat", "gotify://host/token"]')  # type: ignore[arg-type]

        assert ns.urls == ["tgram://bot/chat", "gotify://host/token"]

    def test_empty_string_is_empty_list(self) -> None:
        """An empty/whitespace string yields an empty list, not [""]."""
        from jmcore.settings import NotificationSettings

        assert NotificationSettings(urls="   ").urls == []  # type: ignore[arg-type]

    def test_list_input_is_preserved(self) -> None:
        """A proper list input is passed through unchanged."""
        from jmcore.settings import NotificationSettings

        urls = ["tgram://bottoken/ChatID", "gotify://hostname/token"]

        assert NotificationSettings(urls=urls).urls == urls


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

    def test_prepare_url_plain(self) -> None:
        """No parameters are added with default (direct, verified) config."""
        config = NotificationConfig(use_tor=False)
        notifier = Notifier(config)

        url = "gotify://host/token"
        assert notifier._prepare_url(url) == url

    def test_prepare_url_tor_timeouts(self) -> None:
        """Tor routing extends Apprise's connection/read timeouts."""
        config = NotificationConfig(use_tor=True)
        notifier = Notifier(config)

        assert notifier._prepare_url("gotify://host/token") == "gotify://host/token?cto=30&rto=30"
        # Existing query strings are extended, not clobbered
        assert (
            notifier._prepare_url("gotify://host/token?priority=5")
            == "gotify://host/token?priority=5&cto=30&rto=30"
        )

    def test_prepare_url_verify_tls_disabled(self) -> None:
        """verify_tls=False appends Apprise's verify=no parameter."""
        config = NotificationConfig(use_tor=False, verify_tls=False)
        notifier = Notifier(config)

        assert notifier._prepare_url("gotify://host/token") == "gotify://host/token?verify=no"

    def test_prepare_url_verify_tls_disabled_with_tor(self) -> None:
        """Both timeout and verify parameters combine on one URL."""
        config = NotificationConfig(use_tor=True, verify_tls=False)
        notifier = Notifier(config)

        assert (
            notifier._prepare_url("gotify://host/token")
            == "gotify://host/token?cto=30&rto=30&verify=no"
        )

    def test_prepare_url_respects_existing_verify_param(self) -> None:
        """A per-URL verify parameter wins over the global verify_tls."""
        config = NotificationConfig(use_tor=False, verify_tls=False)
        notifier = Notifier(config)

        url = "gotify://host/token?verify=yes"
        assert notifier._prepare_url(url) == url

    def test_worker_diagnostic_is_bounded_and_sanitized(self) -> None:
        """Worker diagnostics retain the failure class without endpoint credentials."""
        notification_url = "https://notify.example.invalid/token-secret"
        proxy_url = "socks5h://jm-notification:isolation-secret@127.0.0.1:9050"
        message = "notification-body-secret"
        diagnostic = _sanitize_worker_diagnostic(
            f"certificate verify failed for {notification_url} via {proxy_url}: {message}"
        )
        worker_result = NotificationWorkerResult(
            False,
            f"certificate verify failed for {notification_url} via {proxy_url}: {message}",
        )

        assert diagnostic == "TLS certificate verification failed"
        assert worker_result.diagnostic == diagnostic
        assert notification_url not in diagnostic
        assert proxy_url not in diagnostic
        assert "isolation-secret" not in diagnostic
        assert message not in diagnostic

    def test_apprise_diagnostic_handler_ignores_unclassified_logs(self) -> None:
        """Benign debug chatter cannot overwrite a useful failure classification."""
        import logging

        handler = _AppriseDiagnosticHandler()
        handler.emit(
            logging.LogRecord(
                "apprise",
                logging.DEBUG,
                __file__,
                0,
                "certificate verify failed: self-signed",
                (),
                None,
            )
        )
        handler.emit(
            logging.LogRecord(
                "apprise",
                logging.DEBUG,
                __file__,
                0,
                "Preparing notification payload",
                (),
                None,
            )
        )

        assert handler.diagnostic == "TLS certificate verification failed"

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
        """Full txid is shown when enabled (a truncated prefix is useless)."""
        config = NotificationConfig(include_txids=True)
        notifier = Notifier(config)

        txid = "a" * 64
        formatted = notifier._format_txid(txid)
        assert formatted == txid
        assert "..." not in formatted

    def test_format_txid_mempool_link(self) -> None:
        """A configured explorer base turns the txid into a clickable link."""
        txid = "b" * 64
        config = NotificationConfig(include_txids=True, mempool_url="https://mempool.space/signet")
        assert Notifier(config)._format_txid(txid) == f"https://mempool.space/signet/tx/{txid}"

        # Trailing slashes are normalized so the link never doubles up.
        config_slash = NotificationConfig(include_txids=True, mempool_url="https://mempool.space/")
        assert Notifier(config_slash)._format_txid(txid) == f"https://mempool.space/tx/{txid}"

    def test_format_txid_link_suppressed_when_hidden(self) -> None:
        """mempool_url never overrides the privacy switch."""
        config = NotificationConfig(include_txids=False, mempool_url="https://mempool.space")
        assert Notifier(config)._format_txid("c" * 64) == "[hidden]"

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
    async def test_notify_nick_change_disabled_by_default(self) -> None:
        """Test that nick change notifications are opt-in."""
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        result = await notifier.notify_nick_change("old-nick", "new-nick")

        assert result is False
        notifier._send.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_nick_change_enabled(self) -> None:
        """Test that nick change notifications can be explicitly enabled."""
        config = NotificationConfig(
            enabled=True,
            urls=["test://"],
            notify_nick_change=True,
        )
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        result = await notifier.notify_nick_change("old-nick", "new-nick")

        assert result is True
        notifier._send.assert_called_once()

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
    async def test_notify_uses_isolated_worker(self) -> None:
        """Notification payloads are handed to the worker without parent Apprise state."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
        )
        worker = RecordingNotificationWorker()
        notifier = Notifier(config, worker_factory=worker)

        result = await notifier.notify_fill_request("taker123", 500000, 0)

        assert result is True
        assert worker.config is not None
        assert worker.config.urls == ("gotify://host/token?cto=30&rto=30",)
        assert worker.calls[0][0] == "JoinMarket NG: Fill Request Received"

    @pytest.mark.asyncio
    async def test_notification_title_with_component_name(self) -> None:
        """Test that notification title includes component name when set."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            title_prefix="JoinMarket NG",
            component_name="Maker",
        )
        worker = RecordingNotificationWorker()
        notifier = Notifier(config, worker_factory=worker)

        await notifier._send("Test Event", "Test body")

        # Verify the title includes component name
        assert worker.calls[0][0] == "JoinMarket NG (Maker): Test Event"

    @pytest.mark.asyncio
    async def test_notification_title_without_component_name(self) -> None:
        """Test that notification title works without component name."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            title_prefix="JoinMarket NG",
            component_name="",  # Empty component name
        )
        worker = RecordingNotificationWorker()
        notifier = Notifier(config, worker_factory=worker)

        await notifier._send("Test Event", "Test body")

        # Verify the title does not have parentheses when no component
        assert worker.calls[0][0] == "JoinMarket NG: Test Event"

    @pytest.mark.asyncio
    async def test_tor_worker_does_not_mutate_parent_proxy_environment(self) -> None:
        """Tor worker setup and delivery leave every parent proxy variable intact."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=False,
            use_tor=True,
            tor_socks_host="192.168.1.100",
            tor_socks_port=9150,
            stream_isolation=False,
        )
        worker = RecordingNotificationWorker(send_results=[NotificationWorkerResult(False)])
        notifier = Notifier(config, worker_factory=worker)
        parent_proxy_environment = {key: os.environ.get(key) for key in PROXY_ENVIRONMENT_KEYS}

        assert await notifier._send("Test Event", "Test body") is False

        assert {
            key: os.environ.get(key) for key in PROXY_ENVIRONMENT_KEYS
        } == parent_proxy_environment
        assert worker.config is not None
        assert worker.config.use_tor is True
        assert worker.config.tor_socks_host == "192.168.1.100"
        assert worker.config.tor_socks_port == 9150
        assert worker.config.stream_isolation is False

    def test_tor_worker_environment_uses_stream_isolation_proxy(self) -> None:
        """The child environment has the configured SOCKS proxy and no inherited bypasses."""
        config = NotificationConfig(
            urls=["gotify://host/token"],
            use_tor=True,
            tor_socks_host="192.168.1.100",
            tor_socks_port=9150,
            stream_isolation=True,
        )
        worker_config = NotificationWorkerConfig(
            urls=("gotify://host/token",),
            use_tor=config.use_tor,
            tor_socks_host=config.tor_socks_host,
            tor_socks_port=config.tor_socks_port,
            stream_isolation=config.stream_isolation,
        )
        child_environment = dict.fromkeys(PROXY_ENVIRONMENT_KEYS, "ambient-proxy")
        child_environment["UNCHANGED"] = "value"
        proxy_url = "socks5h://jm-notification:child-secret@192.168.1.100:9150"

        def build_proxy_url(host: str, port: int, category: IsolationCategory) -> str:
            assert (host, port, category) == (
                "192.168.1.100",
                9150,
                IsolationCategory.NOTIFICATION,
            )
            return proxy_url

        _configure_worker_environment(worker_config, child_environment, build_proxy_url)

        assert child_environment["HTTP_PROXY"] == proxy_url
        assert child_environment["HTTPS_PROXY"] == proxy_url
        assert child_environment["http_proxy"] == proxy_url
        assert child_environment["https_proxy"] == proxy_url
        assert "ALL_PROXY" not in child_environment
        assert "all_proxy" not in child_environment
        assert "NO_PROXY" not in child_environment
        assert "no_proxy" not in child_environment
        assert child_environment["UNCHANGED"] == "value"

    def test_direct_worker_environment_clears_ambient_proxies(self) -> None:
        """Direct notifications do not inherit parent proxy configuration."""
        worker_config = NotificationWorkerConfig(
            urls=("gotify://host/token",),
            use_tor=False,
            tor_socks_host="127.0.0.1",
            tor_socks_port=9050,
            stream_isolation=True,
        )
        child_environment = dict.fromkeys(PROXY_ENVIRONMENT_KEYS, "ambient-proxy")

        _configure_worker_environment(worker_config, child_environment)

        assert not set(PROXY_ENVIRONMENT_KEYS) & set(child_environment)

    def test_apprise_worker_uses_one_child_and_closes(self) -> None:
        """A valid Apprise configuration is initialized in one owned child process."""
        worker = AppriseWorker(
            NotificationWorkerConfig(
                urls=("json://localhost",),
                use_tor=True,
                tor_socks_host="127.0.0.1",
                tor_socks_port=9050,
                stream_isolation=True,
            )
        )

        assert worker.start().success is True
        process = worker._process
        assert process is not None
        assert process.pid != os.getpid()

        worker.close()

        assert process.is_alive() is False

    @pytest.mark.asyncio
    async def test_apprise_child_routes_through_isolated_socks_proxy(self) -> None:
        """The spawned Apprise process uses its private stream-isolated proxy."""
        captured: dict[str, object] = {}
        request_received = asyncio.Event()

        async def fake_socks_proxy(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            try:
                version, method_count = await reader.readexactly(2)
                assert version == 5
                methods = await reader.readexactly(method_count)
                assert 2 in methods
                writer.write(b"\x05\x02")
                await writer.drain()

                auth_version, username_length = await reader.readexactly(2)
                assert auth_version == 1
                username = await reader.readexactly(username_length)
                password_length = (await reader.readexactly(1))[0]
                password = await reader.readexactly(password_length)
                captured["auth"] = (username.decode(), password.decode())
                writer.write(b"\x01\x00")
                await writer.drain()

                version, command, reserved, address_type = await reader.readexactly(4)
                assert (version, command, reserved) == (5, 1, 0)
                if address_type == 3:
                    host_length = (await reader.readexactly(1))[0]
                    host = (await reader.readexactly(host_length)).decode()
                elif address_type == 1:
                    host = ".".join(str(part) for part in await reader.readexactly(4))
                else:
                    raise AssertionError(f"Unexpected SOCKS address type: {address_type}")
                port = int.from_bytes(await reader.readexactly(2), "big")
                captured["target"] = (host, port)
                writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()

                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
                await writer.drain()
                request_received.set()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(fake_socks_proxy, "127.0.0.1", 0)
        proxy_port = server.sockets[0].getsockname()[1]
        parent_proxy_environment = {key: os.environ.get(key) for key in PROXY_ENVIRONMENT_KEYS}
        worker = AppriseWorker(
            NotificationWorkerConfig(
                urls=("json://notification.invalid:18080",),
                use_tor=True,
                tor_socks_host="127.0.0.1",
                tor_socks_port=proxy_port,
                stream_isolation=True,
            )
        )

        try:
            async with server:
                assert (await asyncio.to_thread(worker.start)).success is True
                result = await asyncio.to_thread(worker.send, "Test", "Body", "info")
                assert result.success is True
                await asyncio.wait_for(request_received.wait(), timeout=1.0)
        finally:
            await asyncio.to_thread(worker.close)

        auth = captured["auth"]
        assert isinstance(auth, tuple)
        username, password = auth
        assert username == "jm-notification"
        assert password
        assert captured["target"] == ("notification.invalid", 18080)
        assert {
            key: os.environ.get(key) for key in PROXY_ENVIRONMENT_KEYS
        } == parent_proxy_environment

    @pytest.mark.asyncio
    async def test_cancelled_worker_start_is_closed_after_completion(self) -> None:
        """Cancellation cannot strand a child whose blocking start is still running."""
        worker = BlockingStartNotificationWorker()
        notifier = Notifier(
            NotificationConfig(enabled=True, urls=["test://"]),
            worker_factory=worker,
        )
        initialization_task = asyncio.create_task(notifier._ensure_initialized())

        assert await asyncio.to_thread(worker.start_started.wait, 1.0)
        initialization_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await initialization_task

        worker.release_start.set()
        assert await asyncio.to_thread(worker.close_completed.wait, 1.0)
        assert worker.closed is True
        assert notifier._worker is None


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

    def test_reset_notifier_closes_worker(self) -> None:
        """Reset releases an initialized worker instead of leaving a child behind."""
        import jmcore.notifications as notifications_module

        reset_notifier()
        worker = RecordingNotificationWorker()
        notifier = Notifier(
            NotificationConfig(enabled=True, urls=["test://"]), worker_factory=worker
        )
        notifier._worker = worker
        notifier._initialized = True
        notifications_module._notifier = notifier

        reset_notifier()

        assert worker.closed is True
        assert notifications_module._notifier is None

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

    @pytest.mark.asyncio
    async def test_summary_balance_disabled_by_default(self) -> None:
        """Test that balance/UTXO info is NOT included when notify_summary_balance is False."""
        config = NotificationConfig(enabled=True, urls=["test://"], notify_summary=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        assert config.notify_summary_balance is False

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=5,
            successful=5,
            failed=0,
            total_earnings=1000,
            total_volume=5_000_000,
            total_balance=1_500_000,
            utxo_count=12,
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "Balance:" not in body
        assert "UTXOs: 12" not in body

    @pytest.mark.asyncio
    async def test_summary_balance_enabled(self) -> None:
        """Test that balance and UTXO count are included when notify_summary_balance is True."""
        config = NotificationConfig(
            enabled=True,
            urls=["test://"],
            notify_summary=True,
            notify_summary_balance=True,
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
            total_balance=1_500_000,
            utxo_count=12,
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "Balance:" in body
        assert "1,500,000 sats" in body
        assert "UTXOs: 12" in body

    @pytest.mark.asyncio
    async def test_summary_balance_enabled_zero_activity(self) -> None:
        """Test balance info is shown even with zero CoinJoin activity."""
        config = NotificationConfig(
            enabled=True,
            urls=["test://"],
            notify_summary=True,
            notify_summary_balance=True,
        )
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=0,
            successful=0,
            failed=0,
            total_earnings=0,
            total_volume=0,
            total_balance=2_000_000,
            utxo_count=5,
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "No CoinJoin activity" in body
        assert "Balance:" in body
        assert "UTXOs: 5" in body

    @pytest.mark.asyncio
    async def test_summary_balance_enabled_none_values(self) -> None:
        """Test that None balance/utxo_count are gracefully omitted."""
        config = NotificationConfig(
            enabled=True,
            urls=["test://"],
            notify_summary=True,
            notify_summary_balance=True,
        )
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=1,
            successful=1,
            failed=0,
            total_earnings=100,
            total_volume=1_000_000,
            total_balance=None,
            utxo_count=None,
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        assert "Balance:" not in body
        assert "UTXOs:" not in body

    @pytest.mark.asyncio
    async def test_summary_balance_amounts_hidden(self) -> None:
        """Test that balance respects include_amounts=False privacy setting."""
        config = NotificationConfig(
            enabled=True,
            urls=["test://"],
            notify_summary=True,
            notify_summary_balance=True,
            include_amounts=False,
        )
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)

        await notifier.notify_summary(
            period_label="Daily",
            total_requests=1,
            successful=1,
            failed=0,
            total_earnings=100,
            total_volume=1_000_000,
            total_balance=1_500_000,
            utxo_count=8,
        )

        call_kwargs = notifier._send.call_args
        body = call_kwargs[1].get("body", call_kwargs[0][1] if len(call_kwargs[0]) > 1 else "")
        # Balance amount should be hidden but label should still be present
        assert "Balance: [hidden]" in body
        # UTXO count is not an amount, should still show
        assert "UTXOs: 8" in body


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
    async def test_send_logs_success_at_info(self) -> None:
        """Test that successful notification sends log at INFO level."""
        from io import StringIO

        from loguru import logger

        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
        )
        worker = RecordingNotificationWorker()
        notifier = Notifier(config, worker_factory=worker)

        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="INFO")

        try:
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

        config = NotificationConfig(enabled=True, urls=["gotify://host/token"], retry_enabled=False)
        worker = RecordingNotificationWorker(send_results=[NotificationWorkerResult(False)])
        notifier = Notifier(config, worker_factory=worker)

        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="DEBUG")

        try:
            await notifier._send("Test Title", "Test body")
        finally:
            logger.remove(handler_id)

        log_output = output.getvalue()
        assert "Notification failed: Test Title" in log_output

    @pytest.mark.asyncio
    async def test_send_logs_sanitized_worker_diagnostic(self) -> None:
        """A child TLS failure reaches parent loguru without notification secrets."""
        from io import StringIO

        from loguru import logger

        notification_url = "https://notify.example.invalid/token-secret"
        proxy_url = "socks5h://jm-notification:isolation-secret@127.0.0.1:9050"
        title = "title-secret"
        body = "body-secret"
        diagnostic = f"certificate verify failed for {notification_url} via {proxy_url}: {body}"
        worker = RecordingNotificationWorker(
            send_results=[NotificationWorkerResult(False, diagnostic)]
        )
        notifier = Notifier(
            NotificationConfig(enabled=True, urls=[notification_url], retry_enabled=False),
            worker_factory=worker,
        )
        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="DEBUG")

        try:
            assert await notifier._send(title, body) is False
        finally:
            logger.remove(handler_id)

        log_output = output.getvalue()
        assert "TLS certificate verification failed" in log_output
        assert title in log_output
        for secret in (notification_url, proxy_url, "isolation-secret", body):
            assert secret not in log_output

    @pytest.mark.asyncio
    async def test_initialization_failure_logs_worker_diagnostic(self) -> None:
        """Initialization failures report the safe worker cause, not a URL error."""
        from io import StringIO

        from loguru import logger

        worker = RecordingNotificationWorker(
            start_result=NotificationWorkerResult(False, "Apprise is not installed")
        )
        notifier = Notifier(
            NotificationConfig(enabled=True, urls=["https://notify.example.invalid/token-secret"]),
            worker_factory=worker,
        )
        output = StringIO()
        handler_id = logger.add(output, format="{message}", level="WARNING")

        try:
            assert await notifier._ensure_initialized() is False
        finally:
            logger.remove(handler_id)

        log_output = output.getvalue()
        assert "Failed to initialize notification worker: Apprise is not installed" in log_output
        assert "No valid notification URLs configured" not in log_output
        assert "token-secret" not in log_output


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
                include_coinjoin_id=False,
                include_nick=False,
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.include_amounts is False
        assert config.include_txids is True
        assert config.include_coinjoin_id is False
        assert config.include_nick is False

    def test_convert_mempool_url(self) -> None:
        """The explorer base URL must flow from settings into the config."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                include_txids=True,
                mempool_url="https://mempool.space/signet",
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.mempool_url == "https://mempool.space/signet"

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

    def test_convert_verify_tls_setting(self) -> None:
        """verify_tls must flow from settings into the config (round-trip)."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        # Default: verification enabled
        settings = JoinMarketSettings(
            notifications=NotificationSettings(urls=["gotify://host/token"])
        )
        assert convert_settings_to_notification_config(settings).verify_tls is True

        # Explicitly disabled (self-signed / private CA servers)
        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                verify_tls=False,
            )
        )
        assert convert_settings_to_notification_config(settings).verify_tls is False

    def test_convert_summary_settings(self) -> None:
        """Test that summary notification settings are converted correctly."""
        from jmcore.settings import JoinMarketSettings, NotificationSettings

        settings = JoinMarketSettings(
            notifications=NotificationSettings(
                urls=["gotify://host/token"],
                notify_summary=True,
                summary_interval_hours=168,
                notify_summary_balance=True,
            )
        )

        config = convert_settings_to_notification_config(settings)

        assert config.notify_summary is True
        assert config.summary_interval_hours == 168
        assert config.notify_summary_balance is True

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
        notifier._worker = RecordingNotificationWorker()

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
        notifier._worker = RecordingNotificationWorker()
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
        notifier._worker = RecordingNotificationWorker()
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
        notifier._worker = RecordingNotificationWorker()
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
        notifier._worker = RecordingNotificationWorker()
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
        notifier._worker = RecordingNotificationWorker()
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
        notifier._worker = RecordingNotificationWorker()
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
        notifier._worker = RecordingNotificationWorker()
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
        notifier._worker = RecordingNotificationWorker()
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
    async def test_retry_with_worker(self) -> None:
        """Test retry integrates correctly with the full _send/_try_send flow."""
        config = NotificationConfig(
            enabled=True,
            urls=["gotify://host/token"],
            retry_enabled=True,
            retry_max_attempts=2,
            retry_base_delay=1.0,
        )
        worker = RecordingNotificationWorker(
            send_results=[
                NotificationWorkerResult(False),
                NotificationWorkerResult(False),
                NotificationWorkerResult(True),
            ]
        )
        notifier = Notifier(config, worker_factory=worker)

        with patch("jmcore.notifications.asyncio.sleep", new_callable=AsyncMock):
            result = await notifier._send("Test Event", "Test body")

            # First attempt failed
            assert result is False

            # Wait for retries
            tasks = list(notifier._retry_tasks)
            await asyncio.gather(*tasks)

        # 1 initial + 2 retries = 3 calls, last one succeeded
        assert len(worker.calls) == 3
        assert len(notifier._retry_tasks) == 0
