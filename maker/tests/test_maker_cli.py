"""CLI tests for maker CLI app."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from typer.testing import CliRunner

from maker.cli import app
from maker.config import MakerConfig
from maker.fidelity import ExpiredFidelityBondCertificateError

runner = CliRunner()


def test_root_help_shows_completion_options() -> None:
    """Maker CLI should expose Typer shell completion options."""
    result = runner.invoke(app, ["--help"], prog_name="jm-maker")
    output = click.unstyle(result.stdout)

    assert result.exit_code == 0
    assert "--install-completion" in output
    assert "--show-completion" in output


def test_help_output_is_alphabetically_sorted() -> None:
    """Subcommands and options must be listed alphabetically in --help."""
    from jmcore.cli_help import find_unsorted_help

    assert find_unsorted_help(app) == []


@pytest.mark.parametrize("configured_cookie", [None, "/configured.cookie"])
def test_build_maker_config_auto_detects_tor_cookie(configured_cookie: str | None) -> None:
    """Cookie detection remains a fallback after explicit configuration (#471)."""
    from jmcore.settings import JoinMarketSettings

    from maker.cli import build_maker_config

    settings = JoinMarketSettings(tor={"cookie_path": configured_cookie})
    with patch(
        "jmcore.config.detect_tor_cookie_path", return_value=Path("/detected.cookie")
    ) as detect:
        config = build_maker_config(settings, "abandon " * 11 + "about", "")
    assert config.tor_control.cookie_path == Path(configured_cookie or "/detected.cookie")
    assert detect.call_count == (0 if configured_cookie else 1)


@pytest.mark.parametrize("explicit_host", [None, "127.0.0.1"])
def test_maker_tor_host_round_trip(explicit_host: str | None) -> None:
    from jmcore.settings import JoinMarketSettings

    from maker.cli import build_maker_config

    tor = {"socks_host": "config-proxy.internal", "cookie_path": "/config.cookie"}
    if explicit_host is not None:
        tor["control_host"] = explicit_host
    settings = JoinMarketSettings(tor=tor)
    config = build_maker_config(
        settings, "abandon " * 11 + "about", "", tor_socks_host="cli-proxy.internal"
    )
    assert config.socks_host == "cli-proxy.internal"
    assert config.tor_control.host == (explicit_host or "cli-proxy.internal")


def test_descriptor_scan_settings_reach_maker_backend() -> None:
    """Descriptor scan settings must survive the maker config round trip."""
    from jmcore.settings import JoinMarketSettings

    from maker.cli import build_maker_config, create_wallet_service

    settings = JoinMarketSettings(
        bitcoin={"backend_type": "descriptor_wallet"},
        wallet={"scan_start_height": 765_432, "scan_lookback_blocks": 12_345},
    )
    config = build_maker_config(settings, "abandon " * 11 + "about", "")

    assert config.backend_config["scan_start_height"] == 765_432
    assert config.backend_config["scan_lookback_blocks"] == 12_345

    mock_backend = MagicMock()
    with patch(
        "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend", return_value=mock_backend
    ) as mock_cls:
        create_wallet_service(config)

    assert mock_cls.call_args is not None
    assert mock_cls.call_args.kwargs["scan_start_height"] == 765_432
    assert mock_cls.call_args.kwargs["scan_lookback_blocks"] == 12_345


def test_config_init_exposes_config_file_option() -> None:
    """``config-init`` must advertise the --config-file flag (#537)."""
    result = runner.invoke(app, ["config-init", "--help"], prog_name="jm-maker")
    output = click.unstyle(result.stdout)

    assert result.exit_code == 0
    assert "--config-file" in output


def test_config_init_creates_config_at_config_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--config-file decouples the created config from --data-dir (#537)."""
    monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)
    data_dir = tmp_path / "var" / "lib" / "joinmarket"
    data_dir.mkdir(parents=True)
    config_file = tmp_path / "etc" / "joinmarket" / "config.toml"

    result = runner.invoke(
        app,
        ["config-init", "--data-dir", str(data_dir), "--config-file", str(config_file)],
        prog_name="jm-maker",
    )

    assert result.exit_code == 0, result.stdout
    assert config_file.exists()
    assert "[tor]" in config_file.read_text()
    # The data directory must not receive its own config.toml.
    assert not (data_dir / "config.toml").exists()


def test_start_expired_certificate_exits_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from maker import cli as cli_module

    settings = MagicMock()
    settings.get_data_dir.return_value = tmp_path
    config = MakerConfig(
        mnemonic="test " * 12,
        directory_servers=["localhost:5222"],
        network="regtest",
        data_dir=tmp_path,
    )
    wallet = MagicMock()
    wallet.backend = MagicMock()
    bot = MagicMock()
    bot.nick = "J5ExpiredMaker"
    bot.start = AsyncMock(side_effect=ExpiredFidelityBondCertificateError("renew the certificate"))
    bot.stop = AsyncMock()
    notifier = MagicMock()
    notifier.notify_startup = AsyncMock()
    write_nick_state = MagicMock()
    remove_nick_state = MagicMock()
    maker_kwargs: dict[str, object] = {}
    mnemonic_file = tmp_path / "wallets" / "imported.mnemonic"

    def make_bot(*_args: object, **kwargs: object) -> MagicMock:
        maker_kwargs.update(kwargs)
        return bot

    monkeypatch.setattr(cli_module, "setup_cli", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(cli_module, "ensure_config_file", lambda _data_dir: None)
    monkeypatch.setattr(
        cli_module,
        "resolve_mnemonic",
        lambda *_args, **_kwargs: SimpleNamespace(
            mnemonic="test " * 12,
            bip39_passphrase="",
            creation_height=None,
            mnemonic_file=mnemonic_file,
        ),
    )
    monkeypatch.setattr(cli_module, "build_maker_config", lambda **_kwargs: config)
    monkeypatch.setattr(cli_module, "create_wallet_service", lambda _config: wallet)
    monkeypatch.setattr(cli_module, "MakerBot", make_bot)
    monkeypatch.setattr(cli_module, "get_notifier", lambda *_args, **_kwargs: notifier)
    monkeypatch.setattr(cli_module, "write_nick_state", write_nick_state)
    monkeypatch.setattr(cli_module, "remove_nick_state", remove_nick_state)

    result = runner.invoke(app, ["start"], prog_name="jm-maker")

    assert result.exit_code == 1
    assert config.mnemonic_file == mnemonic_file
    bot.stop.assert_awaited_once()
    remove_nick_state.assert_called_once_with(tmp_path, "maker")

    callback = maker_kwargs["nick_change_callback"]
    assert callable(callback)
    callback("J5ExpiredMaker", "J5RotatedMaker")
    write_nick_state.assert_any_call(tmp_path, "maker", "J5RotatedMaker")
