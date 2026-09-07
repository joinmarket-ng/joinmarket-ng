from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
from jmwallet.cli import app

MNEMONIC = "abandon " * 11 + "about"


@pytest.fixture
def wallet_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in ("MNEMONIC_FILE", "MNEMONIC", "BIP39_PASSPHRASE", "WALLET__BIP39_PASSPHRASE"):
        monkeypatch.delenv(key, raising=False)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[bitcoin]\nnetwork = "regtest"\n')
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
    wallet_path = tmp_path / "wallet.mnemonic"
    wallet_path.write_text(MNEMONIC)
    return wallet_path


@pytest.mark.parametrize("passphrase", ["", "test secret passphrase"])
@pytest.mark.parametrize("answer", ["y", "n", "", None])
def test_prompt_confirms_wallet_before_backend_access(
    wallet_file: Path, passphrase: str, answer: str | None
) -> None:
    user_input = passphrase + "\n" + (answer + "\n" if answer is not None else "")
    with patch("jmwallet.cli.wallet._show_wallet_info", new_callable=AsyncMock) as show_info:
        result = CliRunner().invoke(
            app,
            ["info", "-f", str(wallet_file), "--prompt-bip39-passphrase"],
            input=user_input,
        )

    fingerprint = get_mnemonic_fingerprint(MNEMONIC, passphrase)
    assert f"Wallet fingerprint: {fingerprint}" in result.output
    assert f"BIP39 passphrase: {'set' if passphrase else 'empty (no passphrase)'}" in result.output
    assert "Continue with this wallet?" in result.output
    if passphrase:
        assert passphrase not in result.output
    if answer == "y":
        assert result.exit_code == 0, result.output
        show_info.assert_awaited_once()
        assert show_info.call_args.args[2] == passphrase
    else:
        assert result.exit_code != 0
        show_info.assert_not_called()


@pytest.mark.parametrize("source", ["environment", "config", "environment_over_config"])
@pytest.mark.parametrize("passphrase", ["", " configured passphrase "])
def test_configured_passphrase_stays_noninteractive(
    wallet_file: Path, monkeypatch: pytest.MonkeyPatch, source: str, passphrase: str
) -> None:
    from jmcore.config_file import set_config_value

    if source != "environment":
        set_config_value(
            wallet_file.parent / "config.toml",
            "wallet",
            "bip39_passphrase",
            "other passphrase" if source == "environment_over_config" else passphrase,
        )
    if source != "config":
        monkeypatch.setenv("BIP39_PASSPHRASE", passphrase)
    # An empty environment value falls through to config or the interactive prompt.
    should_prompt = source == "environment" and not passphrase
    expected = (
        "other passphrase" if source == "environment_over_config" and not passphrase else passphrase
    )
    with patch("jmwallet.cli.wallet._show_wallet_info", new_callable=AsyncMock) as show_info:
        result = CliRunner().invoke(
            app,
            ["info", "-f", str(wallet_file), "--prompt-bip39-passphrase"],
            input="\ny\n" if should_prompt else "",
        )
    assert result.exit_code == 0, result.output
    show_info.assert_awaited_once()
    assert show_info.call_args.args[2] == expected
    assert ("Continue with this wallet?" in result.output) == should_prompt
    assert "configured passphrase" not in result.output
    assert "other passphrase" not in result.output


def test_without_prompt_flag_stays_noninteractive(wallet_file: Path) -> None:
    with patch("jmwallet.cli.wallet._show_wallet_info", new_callable=AsyncMock) as show_info:
        result = CliRunner().invoke(app, ["info", "-f", str(wallet_file)])
    assert result.exit_code == 0, result.output
    assert "Continue with this wallet?" not in result.output
    show_info.assert_awaited_once()
    assert show_info.call_args.args[2] == ""


@pytest.mark.parametrize("answer", ["yes", "n", "", EOFError(), KeyboardInterrupt()])
def test_confirmation_without_typer(
    capsys: pytest.CaptureFixture[str], answer: str | BaseException
) -> None:
    from jmcore.cli_common import _confirm_prompted_wallet

    with (
        patch.dict(sys.modules, {"typer": None}),
        patch(
            "builtins.input",
            return_value=answer if isinstance(answer, str) else "",
            side_effect=answer if isinstance(answer, BaseException) else None,
        ),
    ):
        if answer == "yes":
            _confirm_prompted_wallet(MNEMONIC, "test secret passphrase")
        else:
            with pytest.raises(ValueError, match="Wallet selection cancelled"):
                _confirm_prompted_wallet(MNEMONIC, "test secret passphrase")
    output = capsys.readouterr().out
    assert "BIP39 passphrase: set" in output
    assert "Wallet fingerprint: 43ef144e" in output
    assert "test secret passphrase" not in output


@pytest.mark.parametrize("confirm", [True, False])
def test_core_only_confirmation_still_requires_consent(
    wallet_file: Path, capsys: pytest.CaptureFixture[str], confirm: bool
) -> None:
    import typer
    from jmcore.cli_common import resolve_mnemonic
    from jmcore.settings import JoinMarketSettings

    settings = JoinMarketSettings(data_dir=wallet_file.parent)
    with (
        patch.dict(sys.modules, {"jmwallet.backends.descriptor_wallet": None}),
        patch("typer.prompt", return_value="test secret passphrase"),
        patch("typer.confirm", side_effect=None if confirm else typer.Abort()) as confirmation,
    ):
        if confirm:
            result = resolve_mnemonic(settings, mnemonic=MNEMONIC, prompt_bip39_passphrase=True)
            assert result is not None
            assert result.bip39_passphrase == "test secret passphrase"
        else:
            with pytest.raises(typer.Abort):
                resolve_mnemonic(settings, mnemonic=MNEMONIC, prompt_bip39_passphrase=True)
        confirmation.assert_called_once_with(
            "Continue with this wallet?", default=False, abort=True
        )
    output = capsys.readouterr().out
    assert "BIP39 passphrase: set" in output
    assert "Wallet fingerprint unavailable" in output
    assert "test secret passphrase" not in output
