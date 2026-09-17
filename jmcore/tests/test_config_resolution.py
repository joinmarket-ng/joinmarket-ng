"""Configuration diagnostics and Tor upgrade behavior."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger
from pydantic import ValidationError

from jmcore.config import build_tor_control_config
from jmcore.settings import JoinMarketSettings, TorSettings, WalletSettings


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "config.toml"
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(config))
    return config


@pytest.fixture
def warnings() -> Generator[list[str], None, None]:
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        yield messages
    finally:
        logger.remove(sink)


def test_unknown_entries_warn_without_values_or_rewriting(
    isolated_config: Path, warnings: list[str]
) -> None:
    content = """[tor]
socks_host = "proxy.internal"
pasword = "secret-typo"
[tor_control]
host = "old.internal"
password = "legacy-secret"
[future_section]
key = "future-secret"
"""
    isolated_config.write_text(content)
    settings = JoinMarketSettings()
    assert settings.tor.control_host == "proxy.internal"
    assert isolated_config.read_text() == content
    assert len(warnings) == 3
    output = "".join(warnings)
    for name in ("tor.pasword", "tor_control", "future_section", "[tor] control_enabled"):
        assert name in output
    for secret in ("secret-typo", "legacy-secret", "future-secret", "old.internal"):
        assert secret not in output


def test_valid_tui_and_dictionary_entries_do_not_warn(
    isolated_config: Path, warnings: list[str]
) -> None:
    isolated_config.write_text("""[tui]
log_level = "INFO"
[network_config.nick_auth_directory_ids]
"directory.internal:5222" = "test:directory-a"
""")
    settings = JoinMarketSettings()
    assert settings.tui.log_level == "INFO"
    assert settings.logging.level == "INFO"
    assert warnings == []


@pytest.mark.parametrize("canonical", ["", "background_full_rescan = true\n"])
def test_legacy_wallet_toml_fails_without_rewriting(isolated_config: Path, canonical: str) -> None:
    content = "[wallet]\nbackground_full_scan = false\n" + canonical
    isolated_config.write_text(content)
    with pytest.raises(ValidationError, match="rename it to wallet.background_full_rescan"):
        JoinMarketSettings()
    assert isolated_config.read_text() == content


def test_legacy_wallet_direct_input_fails() -> None:
    with pytest.raises(ValidationError, match="WALLET__BACKGROUND_FULL_RESCAN"):
        WalletSettings.model_validate({"background_full_scan": False})


@pytest.mark.parametrize("explicit", [False, True])
def test_tor_source_precedence(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    content = '[tor]\nsocks_host = "file-proxy.internal"\n'
    if explicit:
        content += 'control_host = "file-control.internal"\n'
    isolated_config.write_text(content)
    assert JoinMarketSettings().tor.control_host == (
        "file-control.internal" if explicit else "file-proxy.internal"
    )
    monkeypatch.setenv("TOR__SOCKS_HOST", "env-proxy.internal")
    assert JoinMarketSettings().tor.control_host == (
        "file-control.internal" if explicit else "env-proxy.internal"
    )
    monkeypatch.setenv("TOR__CONTROL_HOST", "env-control.internal")
    assert JoinMarketSettings().tor.control_host == "env-control.internal"
    assert JoinMarketSettings(tor={"control_host": "init-control.internal"}).tor.control_host == (
        "init-control.internal"
    )


@pytest.mark.parametrize(
    ("tor", "socks_override", "control_override", "expected"),
    [
        ({}, None, None, "127.0.0.1"),
        ({"socks_host": "proxy.internal"}, None, None, "proxy.internal"),
        ({"socks_host": "proxy.internal"}, "cli-proxy.internal", None, "cli-proxy.internal"),
        ({"control_host": "127.0.0.1"}, "cli-proxy.internal", None, "127.0.0.1"),
        ({}, "cli-proxy.internal", "cli-control.internal", "cli-control.internal"),
    ],
)
def test_control_host_resolution(
    tor: dict[str, str], socks_override: str | None, control_override: str | None, expected: str
) -> None:
    settings = TorSettings.model_validate(tor)
    with patch("jmcore.config.detect_tor_cookie_path", return_value=None):
        config = build_tor_control_config(
            settings, socks_host=socks_override, control_host=control_override
        )
    assert config.host == expected


def test_control_overrides_and_disabled_behavior() -> None:
    settings = TorSettings(
        control_enabled=False, control_port=9151, cookie_path="/config.cookie", password="secret"
    )
    with patch("jmcore.config.detect_tor_cookie_path") as detect:
        config = build_tor_control_config(
            settings, control_port=9251, cookie_path=Path("/cli.cookie")
        )
        assert config.enabled is False
        assert config.port == 9251
        assert config.cookie_path == Path("/cli.cookie")
        assert config.password is not None
        assert config.password.get_secret_value() == "secret"
        assert build_tor_control_config(TorSettings(), disable_control=True).enabled is False
        detect.assert_not_called()
