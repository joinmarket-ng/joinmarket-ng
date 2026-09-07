from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
from jmwallet.cli.mnemonic import save_mnemonic_file

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "jmcore/src/jmcore/data/menu.joinmarket-ng.sh"
)
MNEMONIC = "abandon " * 11 + "about"
PASSPHRASE = "test wallet passphrase"


@pytest.mark.parametrize("view", ["BASIC", "EXT"])
@pytest.mark.parametrize("encrypted", [True, False])
@pytest.mark.parametrize("passphrase", [PASSPHRASE, ""])
@pytest.mark.parametrize("confirm", [True, False])
def test_wallet_info_prompts_for_passphrase(
    tmp_path: Path, view: str, encrypted: bool, passphrase: str, confirm: bool
) -> None:
    """Run the real menu branch and CLI, stopping before backend access."""
    wallet_path = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(
        MNEMONIC, wallet_path, "test-encryption-password" if encrypted else None
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text('[bitcoin]\nnetwork = "regtest"\n')
    content = SCRIPT_PATH.read_text()
    helpers = content.split(
        "# =============================================================================\n# Helpers",
        1,
    )[1].split(
        "# =============================================================================\n# Main Loop",
        1,
    )[0]
    info_case = content.split("case $INFO_CHOICE in", 1)[1].split("esac", 1)[0]
    shell_path = tmp_path / "wallet-info.sh"
    shell_path.write_text(
        helpers
        + """
clear() { :; }
pause() { :; }
whiptail() {
    [ "$2" = " Wallet Password " ] || return 90
    printf '%s' 'test-encryption-password' >&2
}
jm-wallet() { "$TUI_PYTHON" "$CLI_PATH" "$@"; }
case $INFO_CHOICE in
"""
        + info_case
        + "esac\n"
        + '[ -z "${MNEMONIC_PASSWORD:-}" ] || exit 91\n'
        + '[ -z "${BIP39_PASSPHRASE:-}" ] || exit 92\n'
    )
    cli_path = tmp_path / "wallet-cli.py"
    cli_path.write_text(
        """
from unittest.mock import patch
from jmwallet.cli import app
from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

async def show_info(mnemonic, backend, passphrase, **kwargs):
    print('SELECTED_WALLET=' + get_mnemonic_fingerprint(mnemonic, passphrase))
    print('EXTENDED=' + str(kwargs['extended']))

with patch('jmwallet.cli.wallet._show_wallet_info', show_info):
    app()
"""
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MNEMONIC", "BIP39_", "JOINMARKET_", "WALLET__"))
    }
    env.update(
        TUI_PYTHON=sys.executable,
        CLI_PATH=str(cli_path),
        CONFIG_FILE=str(config_path),
        JOINMARKET_CONFIG_FILE=str(config_path),
        JOINMARKET_DATA_DIR=str(tmp_path),
        MNEMONIC_FILE=str(wallet_path),
        CURRENT_WALLET=str(wallet_path),
        MAKER_ENV=str(tmp_path / ".maker.env"),
        INFO_CHOICE=view,
    )
    result = subprocess.run(
        ["bash", str(shell_path)],
        input=passphrase + ("\ny\n" if confirm else "\nn\n"),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = get_mnemonic_fingerprint(MNEMONIC, passphrase)
    assert f"Wallet fingerprint: {expected}" in result.stdout
    assert "Continue with this wallet?" in result.stdout
    if confirm:
        assert f"SELECTED_WALLET={expected}" in result.stdout, result.stdout
        assert f"EXTENDED={view == 'EXT'}" in result.stdout
    else:
        assert "SELECTED_WALLET=" not in result.stdout
    assert PASSPHRASE not in result.stdout + result.stderr
    assert "test-encryption-password" not in result.stdout + result.stderr
    assert config_path.read_text() == '[bitcoin]\nnetwork = "regtest"\n'
