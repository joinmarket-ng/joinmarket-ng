"""Guided initialization requires independent creation, exclusion, and identity choices."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from jmwallet import onboarding, setup_lightning, setup_wallet
from jmwallet.onboarding import app
from jmwallet.setup_lightning import SetupLightningError

PHRASE = " ".join(["all"] * 12)
PASSWORD = "private-wallet-password"
runner = CliRunner()


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("BIP39_PASSPHRASE", raising=False)
    monkeypatch.delenv("EXPECTED_FINGERPRINT", raising=False)
    password = tmp_path / "password"
    password.write_text(PASSWORD)
    directory = tmp_path / "selected data"
    initialize = Mock(return_value=SimpleNamespace(nodes=[object(), object()]))
    monkeypatch.setattr(setup_lightning, "initialize_managed_nodes", initialize)
    monkeypatch.setattr(setup_wallet, "generate_mnemonic_secure", Mock(return_value=PHRASE))
    return SimpleNamespace(
        directory=directory,
        initialize=initialize,
        args=[
            "--data-dir",
            str(directory),
            "--yes",
            "--network",
            "regtest",
            "--taproot",
            "--lightning",
            "managed",
            "--wallet-action",
            "generate",
            "--wallet-password-file",
            str(password),
        ],
    )


def test_explicit_creation_reuses_unlocked_wallet_but_only_prints_supervisor_instructions(
    fixture: Any,
) -> None:
    state = fixture
    result = runner.invoke(
        app,
        [*state.args, "--create-managed-nodes", "--applications-quiesced", "--no-bip39-passphrase"],
    )
    assert result.exit_code == 0, result.output
    state.initialize.assert_called_once()
    args, kwargs = state.initialize.call_args
    assert args[0].lightning.mode == "managed" and args[0].wallet.address_type == "p2tr"
    assert isinstance(args[1], SecretStr) and args[1].get_secret_value() == PHRASE
    assert isinstance(args[2], SecretStr) and args[2].get_secret_value() == ""
    assert kwargs["consumers_excluded"] is True and kwargs["install_lnd"] is False
    assert kwargs["data_dir"] == state.directory
    assert kwargs["wallet_file"] == state.directory / "wallets/default.mnemonic"
    assert "Initialized 2 managed nodes; they are not running" in result.output
    assert "Start the persistent supervisor separately" in result.output
    assert "No markets were activated" in result.output
    assert PHRASE not in result.output and PASSWORD not in result.output


def test_managed_mode_and_yes_alone_never_authorize_node_creation(fixture: Any) -> None:
    state = fixture
    result = runner.invoke(app, state.args)
    assert result.exit_code == 0, result.output
    state.initialize.assert_not_called()
    assert "Lightning provisioning was not performed" in result.output


@pytest.mark.parametrize("missing", ["exclusion", "passphrase"])
def test_yes_never_substitutes_for_exclusion_or_bip39_choice(fixture: Any, missing: str) -> None:
    state = fixture
    args = [*state.args, "--create-managed-nodes"]
    args += ["--no-bip39-passphrase"] if missing == "exclusion" else ["--applications-quiesced"]
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    expected = "--applications-quiesced" if missing == "exclusion" else "--bip39-passphrase-file"
    assert expected in result.output
    state.initialize.assert_not_called()
    assert not (state.directory / "lightning").exists()


@pytest.mark.parametrize("option", ["--configure-only", "--dry-run"])
def test_nonprovisioning_modes_skip_every_node_secret_and_effect(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    option: str,
) -> None:
    state = fixture
    monkeypatch.setattr(
        onboarding, "read_private_file", Mock(side_effect=AssertionError("secret read"))
    )
    result = runner.invoke(
        app,
        [
            *state.args,
            option,
            "--create-managed-nodes",
            "--install-lnd",
            "--applications-quiesced",
            "--bip39-passphrase-file",
            str(state.directory / "missing-passphrase"),
        ],
    )
    assert result.exit_code == 0, result.output
    state.initialize.assert_not_called()
    assert not (state.directory / "wallets").exists()


def test_installation_and_passphrase_are_explicit_and_never_persisted(
    fixture: Any,
    tmp_path: Path,
) -> None:
    state = fixture
    passphrase = tmp_path / "passphrase"
    passphrase.write_text("private-bip39-passphrase")
    result = runner.invoke(
        app,
        [
            *state.args,
            "--create-managed-nodes",
            "--install-lnd",
            "--applications-quiesced",
            "--bip39-passphrase-file",
            str(passphrase),
        ],
    )
    assert result.exit_code == 0, result.output
    args, kwargs = state.initialize.call_args
    assert args[2].get_secret_value() == "private-bip39-passphrase"
    assert kwargs["install_lnd"] is True
    text = (state.directory / "config.toml").read_text() + result.output
    assert "private-bip39-passphrase" not in text and PASSWORD not in text and PHRASE not in text
    assert "--prompt-bip39-passphrase" in result.output


def test_conflicting_passphrase_environment_is_not_implicitly_used(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    monkeypatch.setenv("BIP39_PASSPHRASE", "old-environment-secret")
    result = runner.invoke(
        app,
        [*state.args, "--create-managed-nodes", "--applications-quiesced", "--no-bip39-passphrase"],
    )
    assert result.exit_code != 0
    assert "conflicting BIP39_PASSPHRASE" in result.output
    assert "old-environment-secret" not in result.output
    state.initialize.assert_not_called()


def test_staged_expected_fingerprint_is_not_bypassed(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    monkeypatch.setenv("EXPECTED_FINGERPRINT", "00000000")
    result = runner.invoke(
        app,
        [
            *state.args,
            "--create-managed-nodes",
            "--applications-quiesced",
            "--no-bip39-passphrase",
        ],
    )
    assert result.exit_code != 0
    assert "does not match EXPECTED_FINGERPRINT" in result.output
    assert PHRASE not in result.output
    state.initialize.assert_not_called()


def test_config_change_between_wallet_and_node_stages_blocks_initialization(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    select = onboarding._wallet_stage

    def changed(*args: Any, **kwargs: Any) -> Any:
        wallet = select(*args, **kwargs)
        path = state.directory / "config.toml"
        path.write_text(path.read_text() + "\n# Changed after selection\n")
        return wallet

    monkeypatch.setattr(onboarding, "_wallet_stage", changed)
    result = runner.invoke(
        app,
        [*state.args, "--create-managed-nodes", "--applications-quiesced", "--no-bip39-passphrase"],
    )
    assert result.exit_code != 0
    assert "Configuration changed after wallet selection" in result.output
    state.initialize.assert_not_called()


def test_initializer_refusal_preserves_completed_wallet_and_metadata(fixture: Any) -> None:
    state = fixture
    state.initialize.side_effect = SetupLightningError("Existing node state requires recovery")
    result = runner.invoke(
        app,
        [*state.args, "--create-managed-nodes", "--applications-quiesced", "--no-bip39-passphrase"],
    )
    assert result.exit_code != 0
    assert "Wallet remains selected" in result.output
    wallet = state.directory / "wallets/default.mnemonic"
    assert wallet.exists()
    assert (
        json.loads(wallet.with_name(wallet.name + ".setup.json").read_text())["stage"]
        == "completed"
    )
    assert json.loads(wallet.with_name(wallet.name + ".meta").read_text()) == {
        "fidelity_bond_recovery": "not_required"
    }
    assert "Initialized" not in result.output and PHRASE not in result.output


def test_configuration_changed_after_initialization_is_not_reported_as_ready(fixture: Any) -> None:
    state = fixture

    def initialized(*args: Any, **kwargs: Any) -> Any:
        path = state.directory / "config.toml"
        path.write_text(path.read_text() + "\n# Concurrent change\n")
        return SimpleNamespace(nodes=[object(), object()])

    state.initialize.side_effect = initialized
    result = runner.invoke(
        app,
        [
            *state.args,
            "--create-managed-nodes",
            "--applications-quiesced",
            "--no-bip39-passphrase",
        ],
    )
    assert result.exit_code != 0
    assert "Nodes were initialized but configuration changed" in result.output
    assert "Start the persistent supervisor" not in result.output
    assert "Keep new-node consumers" in result.output


def test_passphrase_file_cannot_silently_conflict_with_selected_wallet_configuration(
    fixture: Any,
    tmp_path: Path,
) -> None:
    state = fixture
    state.directory.mkdir()
    wallet = state.directory / "existing.mnemonic"
    wallet.write_text(PHRASE)
    config = state.directory / "config.toml"
    config.write_text(
        f'[wallet]\nmnemonic_file = "{wallet}"\n'
        'bip39_passphrase = "configured-private-passphrase"\n'
    )
    passphrase = tmp_path / "other-passphrase"
    passphrase.write_text("other-private-passphrase")
    args = list(state.args)
    args[args.index("--wallet-action") + 1] = "existing"
    result = runner.invoke(
        app,
        [
            *args,
            "--create-managed-nodes",
            "--applications-quiesced",
            "--bip39-passphrase-file",
            str(passphrase),
        ],
    )
    assert result.exit_code != 0
    assert "Resolve the configured BIP39 passphrase" in result.output
    assert "configured-private-passphrase" not in result.output
    assert "other-private-passphrase" not in result.output
    state.initialize.assert_not_called()


def test_node_creation_never_infers_a_wallet_action(fixture: Any) -> None:
    state = fixture
    args = list(state.args)
    index = args.index("--wallet-action")
    del args[index : index + 2]
    result = runner.invoke(app, [*args, "--create-managed-nodes", "--install-lnd"])
    assert result.exit_code != 0
    assert "explicit --wallet-action" in result.output
    assert not (state.directory / "wallets").exists()
    state.initialize.assert_not_called()


def test_missing_managed_component_keeps_the_selected_wallet_and_reports_next_step(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    monkeypatch.setitem(sys.modules, "jmwallet.setup_lightning", None)
    result = runner.invoke(
        app,
        [
            *state.args,
            "--create-managed-nodes",
            "--applications-quiesced",
            "--no-bip39-passphrase",
        ],
    )
    assert result.exit_code != 0
    assert "requires the jm-lightning component" in result.output
    assert (state.directory / "wallets/default.mnemonic").exists()
    assert PHRASE not in result.output and PASSWORD not in result.output
