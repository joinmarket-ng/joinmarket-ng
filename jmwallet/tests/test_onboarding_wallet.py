"""Wallet setup requires explicit action, secret input, and separate seed disclosure."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from jmwallet import onboarding, setup_wallet
from jmwallet.cli.mnemonic import load_mnemonic_file, save_mnemonic_file
from jmwallet.onboarding import app

runner = CliRunner()
PHRASE = " ".join(["all"] * 12)
PASSWORD = "private-stage-test-password"


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, list[str]]:
    password = tmp_path / "password"
    password.write_text(PASSWORD + "\n")
    words = tmp_path / "words"
    words.write_text(PHRASE)
    monkeypatch.setattr(setup_wallet, "generate_mnemonic_secure", Mock(return_value=PHRASE))
    args = ["--data-dir", str(tmp_path / "data"), "--yes", "--wallet-password-file", str(password)]
    return password, words, args


def test_noninteractive_generation_never_implicitly_discloses_seed(
    inputs: tuple, tmp_path: Path
) -> None:
    _, _, args = inputs
    result = runner.invoke(app, [*args, "--wallet-action", "generate"])
    assert result.exit_code == 0, result.output
    assert PHRASE not in result.output and PASSWORD not in result.output
    assert "private seed backup remains pending" in result.output
    wallet = tmp_path / "data/wallets/default.mnemonic"
    assert load_mnemonic_file(wallet, PASSWORD) == PHRASE
    config = tomllib.loads((tmp_path / "data/config.toml").read_text())
    assert config["wallet"]["mnemonic_file"] == str(wallet)
    assert "mnemonic_password" not in config["wallet"]
    assert config["lightning"]["mode"] == "off"
    assert "Lightning provisioning was not performed" in result.output


def test_seed_disclosure_requires_separate_explicit_authorization(
    inputs: tuple, tmp_path: Path
) -> None:
    _, _, args = inputs
    result = runner.invoke(app, [*args, "--wallet-action", "generate", "--show-generated-seed"])
    assert result.exit_code == 0, result.output
    assert PHRASE in result.output and PASSWORD not in result.output
    assert "PRIVATE BACKUP" in result.output
    journal = json.loads((tmp_path / "data/wallets/default.mnemonic.setup.json").read_text())
    assert journal["stage"] == "completed"


def test_redirected_output_never_gets_a_seed_from_generic_prompt_answers(inputs: tuple) -> None:
    _, _, args = inputs
    args = [item for item in args if item != "--yes"]
    result = runner.invoke(
        app,
        [
            *args,
            "--network",
            "mainnet",
            "--no-taproot",
            "--lightning",
            "off",
            "--rpc-url",
            "http://127.0.0.1:8332",
            "--wallet-action",
            "generate",
        ],
        input="y\ny\n",
    )
    assert result.exit_code == 0, result.output
    assert PHRASE not in result.output
    assert "private seed backup remains pending" in result.output


@pytest.mark.parametrize("authorize", [False, True])
def test_terminal_seed_display_requires_private_backup_confirmation(
    inputs: tuple,
    monkeypatch: pytest.MonkeyPatch,
    authorize: bool,
) -> None:
    _, _, args = inputs
    args = [item for item in args if item != "--yes"]
    terminal = SimpleNamespace(isatty=lambda: True)
    monkeypatch.setattr(onboarding, "sys", SimpleNamespace(stdin=terminal, stdout=terminal))
    result = runner.invoke(
        app,
        [
            *args,
            "--network",
            "mainnet",
            "--no-taproot",
            "--lightning",
            "off",
            "--rpc-url",
            "http://127.0.0.1:8332",
            "--wallet-action",
            "generate",
        ],
        input="y\n" + ("y\n" if authorize else "n\n"),
    )
    assert result.exit_code == 0, result.output
    assert "Stop recordings and screen sharing first" in result.output
    assert (PHRASE in result.output) is authorize


def test_explicit_import_sets_pending_recovery_without_printing_words(
    inputs: tuple, tmp_path: Path
) -> None:
    _, words, args = inputs
    result = runner.invoke(
        app,
        [
            *args,
            "--wallet-action",
            "import",
            "--import-mnemonic-file",
            str(words),
            "--show-generated-seed",
        ],
    )
    assert result.exit_code == 0, result.output
    assert PHRASE not in result.output and PASSWORD not in result.output
    metadata = json.loads((tmp_path / "data/wallets/default.mnemonic.meta").read_text())
    assert metadata == {"fidelity_bond_recovery": "pending"}


@pytest.mark.parametrize("option", ["--configure-only", "--dry-run"])
def test_configuration_modes_never_read_wallet_secrets(
    inputs: tuple,
    monkeypatch: pytest.MonkeyPatch,
    option: str,
) -> None:
    _, words, args = inputs
    monkeypatch.setattr(
        onboarding, "read_private_file", Mock(side_effect=AssertionError("secret accessed"))
    )
    result = runner.invoke(
        app,
        [
            *args,
            option,
            "--wallet-action",
            "import",
            "--import-mnemonic-file",
            str(words),
        ],
    )
    assert result.exit_code == 0, result.output
    cast(Mock, setup_wallet.generate_mnemonic_secure).assert_not_called()


def test_yes_without_wallet_action_never_infers_generation(inputs: tuple, tmp_path: Path) -> None:
    _, _, args = inputs
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "no wallet action selected" in result.output
    assert not (tmp_path / "data/wallets").exists()


@pytest.mark.parametrize("field", ["mnemonic_password", "bip39_passphrase"])
def test_switching_wallet_refuses_unresolved_configured_credentials(
    inputs: tuple,
    tmp_path: Path,
    field: str,
) -> None:
    _, _, args = inputs
    config = tmp_path / "data/config.toml"
    config.parent.mkdir()
    config.write_text(
        f'[wallet]\nmnemonic_file = "previous.mnemonic"\n{field} = "old-private-credential"\n'
    )
    result = runner.invoke(app, [*args, "--wallet-action", "generate"])
    assert result.exit_code != 0
    assert "explicitly resolve configured" in result.output
    assert "old-private-credential" not in result.output
    assert not (tmp_path / "data/wallets").exists()
    assert tomllib.loads(config.read_text())["wallet"]["mnemonic_file"] == "previous.mnemonic"
    cast(Mock, setup_wallet.generate_mnemonic_secure).assert_not_called()


def test_credentials_changed_between_stages_are_not_silently_applied(
    inputs: tuple,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, args = inputs
    original = onboarding._wallet_stage
    config = tmp_path / "data/config.toml"

    def stage(*args: Any, **kwargs: Any) -> None:
        text = config.read_text()
        config.write_text(
            text.replace("[wallet]", '[wallet]\nbip39_passphrase = "new-external-secret"')
        )
        original(*args, **kwargs)

    monkeypatch.setattr(onboarding, "_wallet_stage", stage)
    result = runner.invoke(app, [*args, "--wallet-action", "generate"])
    assert result.exit_code != 0
    assert "new-external-secret" not in result.output
    assert not (tmp_path / "data/wallets").exists()
    cast(Mock, setup_wallet.generate_mnemonic_secure).assert_not_called()


def test_existing_wallet_keeps_relative_path_semantics_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    wallet = tmp_path / "relative.mnemonic"
    wallet.write_text(PHRASE)
    metadata = wallet.with_name(wallet.name + ".meta")
    metadata.write_bytes(b'{"fidelity_bond_recovery":"started"}')
    config = tmp_path / "configuration/config.toml"
    config.parent.mkdir()
    config.write_text('[wallet]\nmnemonic_file = "relative.mnemonic"\n')
    result = runner.invoke(
        app, ["--config-file", str(config), "--yes", "--wallet-action", "existing"]
    )
    assert result.exit_code == 0, result.output
    assert "Existing wallet is plaintext" in result.output and PHRASE not in result.output
    assert metadata.read_bytes() == b'{"fidelity_bond_recovery":"started"}'
    assert not wallet.with_name(wallet.name + ".setup.json").exists()
    assert tomllib.loads(config.read_text())["wallet"]["mnemonic_file"] == str(wallet)


def test_existing_same_wallet_uses_its_configured_password_without_republishing(
    tmp_path: Path,
) -> None:
    wallet = tmp_path / "existing.mnemonic"
    save_mnemonic_file(PHRASE, wallet, PASSWORD)
    before = wallet.read_bytes()
    config = tmp_path / "config.toml"
    config.write_text(f'[wallet]\nmnemonic_file = "{wallet}"\nmnemonic_password = "{PASSWORD}"\n')
    result = runner.invoke(
        app, ["--config-file", str(config), "--yes", "--wallet-action", "existing"]
    )
    assert result.exit_code == 0, result.output
    assert PHRASE not in result.output and PASSWORD not in result.output
    assert wallet.read_bytes() == before
    assert not wallet.with_name(wallet.name + ".meta").exists()


def test_existing_wallet_config_write_error_never_claims_selection_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = tmp_path / "existing.mnemonic"
    wallet.write_text(PHRASE)
    config = tmp_path / "config.toml"
    config.write_text('[wallet]\naddress_type = "p2wpkh"\n')
    update = onboarding.update_config_values

    def write_then_fail(*args: Any, **kwargs: Any) -> None:
        update(*args, **kwargs)
        if "mnemonic_file" in args[1].get("wallet", {}):
            raise OSError("error after successful wallet selection write")

    monkeypatch.setattr(onboarding, "update_config_values", write_then_fail)
    result = runner.invoke(
        app,
        [
            "--config-file",
            str(config),
            "--yes",
            "--wallet-action",
            "existing",
            "--wallet-file",
            str(wallet),
        ],
    )
    assert result.exit_code != 0
    assert "configuration update could not be confirmed" in result.output
    assert "selection unchanged" not in result.output
    assert tomllib.loads(config.read_text())["wallet"]["mnemonic_file"] == str(wallet)
    assert wallet.read_text() == PHRASE
    assert PHRASE not in result.output


def test_conflict_after_publication_preserves_wallet_and_never_discloses_seed(
    inputs: tuple,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, args = inputs
    publish = setup_wallet.publish_setup_wallet
    config = tmp_path / "data/config.toml"

    def conflicting_publish(*args: Any, **kwargs: Any) -> setup_wallet.SetupWalletPublication:
        result = publish(*args, **kwargs)
        config.write_text('# External edit\n[wallet]\naddress_type = "p2wpkh"\n')
        return result

    monkeypatch.setattr(setup_wallet, "publish_setup_wallet", conflicting_publish)
    result = runner.invoke(app, [*args, "--wallet-action", "generate", "--show-generated-seed"])
    assert result.exit_code != 0
    assert "Wallet saved; configuration selection was not updated" in result.output
    assert PHRASE not in result.output and PASSWORD not in result.output
    assert "# External edit" in config.read_text()
    assert load_mnemonic_file(tmp_path / "data/wallets/default.mnemonic", PASSWORD) == PHRASE


def test_interrupted_import_cannot_select_or_disclose_until_metadata_is_confirmed(
    inputs: tuple,
    tmp_path: Path,
) -> None:
    _, words, args = inputs
    wallet = tmp_path / "interrupted.mnemonic"
    setup_wallet.publish_setup_wallet(
        wallet,
        SecretStr(PASSWORD),
        operation="import",
        mnemonic=SecretStr(PHRASE),
    )
    wallet.with_name(wallet.name + ".meta").unlink()
    selection = [
        *args,
        "--wallet-action",
        "import",
        "--wallet-file",
        str(wallet),
        "--resume-wallet-publication",
    ]
    result = runner.invoke(app, selection)
    assert result.exit_code != 0
    assert "setup metadata is incomplete" in result.output and PHRASE not in result.output
    config = tmp_path / "data/config.toml"
    assert "mnemonic_file" not in tomllib.loads(config.read_text())["wallet"]
    confirmed = runner.invoke(app, [*selection, "--confirm-import-recovery"])
    assert confirmed.exit_code == 0, confirmed.output
    assert PHRASE not in confirmed.output


def test_missing_existing_wallet_is_not_generated(inputs: tuple, tmp_path: Path) -> None:
    _, _, args = inputs
    result = runner.invoke(app, [*args, "--wallet-action", "existing"])
    assert result.exit_code != 0
    assert not (tmp_path / "data/wallets").exists()
    cast(Mock, setup_wallet.generate_mnemonic_secure).assert_not_called()
