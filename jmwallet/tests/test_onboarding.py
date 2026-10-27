"""The configuration setup stage must not infer wallet or node freshness."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jmwallet.onboarding import app

runner = CliRunner()


def test_fresh_configuration_keeps_taproot_and_lightning_off(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "--yes", "--configure-only"])
    assert result.exit_code == 0, result.output
    document = tomllib.loads((tmp_path / "config.toml").read_text())
    assert document["wallet"]["address_type"] == "p2wpkh"
    assert document["lightning"]["mode"] == "off"
    assert {path.name for path in tmp_path.iterdir()} == {"config.toml"}
    assert "wallet and node state unchanged" in result.output


def test_taproot_managed_configuration_is_explicit_and_provisions_nothing(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "--yes",
            "--configure-only",
            "--network",
            "signet",
            "--taproot",
            "--lightning",
            "managed",
            "--tor-socks",
            "127.0.0.1:19050",
            "--tor-control",
            "[::1]:19051",
        ],
    )
    assert result.exit_code == 0, result.output
    document = tomllib.loads((tmp_path / "config.toml").read_text())
    assert document["wallet"]["address_type"] == "p2tr"
    assert document["lightning"]["mode"] == "managed"
    assert document["bitcoin"]["rpc_url"] == "http://127.0.0.1:38332"
    assert document["tor"]["socks_port"] == 19050
    assert document["tor"]["control_host"] == "::1"
    assert {path.name for path in tmp_path.iterdir()} == {"config.toml"}


def test_preview_does_not_create_even_a_configuration_directory(tmp_path: Path) -> None:
    directory = tmp_path / "new-installation"
    result = runner.invoke(app, ["--data-dir", str(directory), "--yes", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "no files written" in result.output
    assert not directory.exists()


def test_existing_preferences_comments_and_wallet_artifacts_are_preserved(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("""# Operator notes
[wallet]
address_type = "p2tr"
[lightning]
mode = "manual"
[bitcoin]
rpc_url = "http://user:private-password@127.0.0.1:38342"
[maker]
cj_fee_relative = "0.0012"
""")
    mnemonic = tmp_path / "wallet.jmdat"
    mnemonic.write_bytes(b"existing encrypted wallet artifact")
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "--yes", "--configure-only"])
    assert result.exit_code == 0, result.output
    assert "private-password" not in result.output
    document = tomllib.loads(config.read_text())
    assert "# Operator notes" in config.read_text()
    assert document["wallet"]["address_type"] == "p2tr"
    assert document["lightning"]["mode"] == "manual"
    assert document["maker"]["cj_fee_relative"] == "0.0012"
    assert document["bitcoin"]["rpc_url"].endswith("127.0.0.1:38342")
    assert mnemonic.read_bytes() == b"existing encrypted wallet artifact"


def test_existing_authenticated_rpc_endpoint_is_not_echoed_by_prompts(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[bitcoin]\nrpc_url = "http://user:private-password@127.0.0.1:38342"\n')
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "--configure-only"], input="\nn\n\nn\n"
    )
    assert result.exit_code == 0, result.output
    assert "private-password" not in result.output


def test_invalid_combination_never_writes_an_intermediate_config(tmp_path: Path) -> None:
    directory = tmp_path / "new-installation"
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "--yes",
            "--no-taproot",
            "--lightning",
            "managed",
        ],
    )
    assert result.exit_code != 0
    assert not directory.exists()


def test_unsupported_existing_backend_cannot_authorize_managed_setup(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    original = '[bitcoin]\nbackend_type = "older-unsupported-backend"\n'
    path.write_text(original)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "--yes",
            "--configure-only",
            "--taproot",
            "--lightning",
            "managed",
        ],
    )
    assert result.exit_code != 0
    assert path.read_text() == original


def test_interactive_managed_setup_asks_for_missing_core_cookie_without_reading_it(
    tmp_path: Path,
) -> None:
    cookie = tmp_path / "not-created.cookie"
    directory = tmp_path / "data"
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "--configure-only",
            "--network",
            "regtest",
            "--taproot",
            "--lightning",
            "managed",
            "--rpc-url",
            "http://127.0.0.1:18443",
        ],
        input=f"{cookie}\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert "path only; not read now" in result.output
    assert not cookie.exists()
    document = tomllib.loads((directory / "config.toml").read_text())
    assert document["bitcoin"]["rpc_cookie_file"] == str(cookie)


def test_explicit_data_directory_wins_over_inherited_config_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "other.toml"
    other.write_text("# Do not modify\n")
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(other))
    directory = tmp_path / "chosen"
    result = runner.invoke(app, ["--data-dir", str(directory), "--yes", "--configure-only"])
    assert result.exit_code == 0, result.output
    assert (directory / "config.toml").exists()
    assert other.read_text() == "# Do not modify\n"


def test_ambient_config_values_cannot_promote_taproot_during_yes_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WALLET__ADDRESS_TYPE", "p2tr")
    monkeypatch.setenv("LIGHTNING__MODE", "managed")
    result = runner.invoke(app, ["--data-dir", str(tmp_path), "--yes", "--configure-only"])
    assert result.exit_code == 0, result.output
    document = tomllib.loads((tmp_path / "config.toml").read_text())
    assert document["wallet"]["address_type"] == "p2wpkh"
    assert document["lightning"]["mode"] == "off"


def test_rejecting_interactive_confirmation_writes_nothing(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "--configure-only"], input="\nn\n\nn\n"
    )
    assert result.exit_code == 0, result.output
    assert "No changes written" in result.output
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize(
    "endpoint", ["host", "127.0.0.1:0", "user:secret@host:9050", "host:9050/path"]
)
def test_bad_tor_endpoint_is_rejected_without_writes(tmp_path: Path, endpoint: str) -> None:
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "--yes",
            "--tor-socks",
            endpoint,
        ],
    )
    assert result.exit_code != 0
    assert not (tmp_path / "config.toml").exists()
