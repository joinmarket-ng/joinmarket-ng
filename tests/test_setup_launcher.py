"""Guided setup routes through this interpreter without changing the shell menu."""

from __future__ import annotations

import sys
import os
from pathlib import Path
import subprocess
import tomllib
from types import ModuleType
from unittest.mock import Mock

import pytest

from jmcore import tui


def test_setup_arguments_are_forwarded_without_whiptail_or_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("jmwallet.onboarding")
    main = Mock()
    setattr(module, "main", main)
    monkeypatch.setitem(sys.modules, "jmwallet.onboarding", module)
    monkeypatch.setattr(
        sys, "argv", ["jm-ng", "setup", "--data-dir", "/path with spaces", "--yes"]
    )
    monkeypatch.setattr(
        tui.shutil,
        "which",
        lambda executable: pytest.fail("setup must not require whiptail"),
    )
    monkeypatch.setattr(
        tui.os,
        "execvpe",
        lambda *args: pytest.fail("setup must not launch the shell menu"),
    )
    tui.main()
    main.assert_called_once_with(["--data-dir", "/path with spaces", "--yes"])


@pytest.mark.skipif(
    os.name != "posix", reason="durable setup publication currently supports POSIX"
)
def test_console_setup_publishes_encrypted_wallet_without_seed_output(
    tmp_path: Path,
) -> None:
    from jmwallet.cli.mnemonic import load_mnemonic_file

    directory = tmp_path / "private setup"
    password = tmp_path / "password"
    password.write_text("launcher-test-encryption-password\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from jmcore.tui import main; main()",
            "setup",
            "--data-dir",
            str(directory),
            "--yes",
            "--wallet-action",
            "generate",
            "--wallet-password-file",
            str(password),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    wallet = directory / "wallets/default.mnemonic"
    phrase = load_mnemonic_file(wallet, "launcher-test-encryption-password")
    assert phrase not in result.stdout + result.stderr
    assert "launcher-test-encryption-password" not in result.stdout + result.stderr
    config = tomllib.loads((directory / "config.toml").read_text())
    assert config["wallet"]["mnemonic_file"] == str(wallet)
    assert config["wallet"]["address_type"] == "p2wpkh"
    assert config["lightning"]["mode"] == "off"
    assert "Seed not displayed" in result.stdout
