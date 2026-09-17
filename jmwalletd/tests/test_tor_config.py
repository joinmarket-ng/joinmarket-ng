"""Daemon maker configuration shares Tor resolution with the CLI."""

from pathlib import Path
from unittest.mock import patch

import pytest

from jmcore.settings import JoinMarketSettings
from jmwalletd.maker_config import build_daemon_maker_config


@pytest.mark.parametrize("explicit_host", [None, "127.0.0.1"])
def test_daemon_tor_config_round_trip(tmp_path: Path, explicit_host: str | None) -> None:
    tor = {"socks_host": "proxy.internal"}
    if explicit_host is not None:
        tor["control_host"] = explicit_host
    settings = JoinMarketSettings(tor=tor)
    with patch("jmcore.config.detect_tor_cookie_path", return_value=Path("/detected.cookie")):
        config = build_daemon_maker_config(settings, "abandon " * 11 + "about", tmp_path)
    assert config.tor_control.host == (explicit_host or "proxy.internal")
    assert config.tor_control.cookie_path == Path("/detected.cookie")
