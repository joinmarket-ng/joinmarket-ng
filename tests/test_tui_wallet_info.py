from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jmwallet.cli.mnemonic import save_mnemonic_file

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "jmcore/src/jmcore/data/menu.joinmarket-ng.sh"
)
MNEMONIC = "abandon " * 11 + "about"
PASSPHRASE = "test wallet passphrase"


def _run_test_shell(
    command: list[str], *, input_text: str, env: dict[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    """Run a shell without a controlling terminal and clean up its process group on errors."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input_text, timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise

    assert process.returncode is not None
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_run_test_shell_kills_descendants_on_timeout(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    dummy_script_path = tmp_path / "dummy-script.py"
    dummy_script_path.write_text(
        """
import os
import signal
import subprocess
import sys
from pathlib import Path

child = subprocess.Popen([sys.executable, "-c", "import signal; signal.pause()"])
Path(os.environ["CHILD_PID_PATH"]).write_text(str(child.pid))
child.wait()
"""
    )
    env = os.environ.copy()
    env["CHILD_PID_PATH"] = str(child_pid_path)

    with pytest.raises(subprocess.TimeoutExpired):
        _run_test_shell(
            [sys.executable, str(dummy_script_path)],
            input_text="",
            env=env,
            timeout=3,
        )

    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 1
    while _process_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_exists(child_pid)


@pytest.mark.parametrize("encrypted", [True, False])
@pytest.mark.parametrize("passphrase", [PASSPHRASE, ""])
def test_ensure_wallet_unlocked_global_caches_passphrase(
    tmp_path: Path, encrypted: bool, passphrase: str
) -> None:
    """ensure_wallet_unlocked_global prompts, confirms fingerprint, and caches BIP39_PASSPHRASE."""
    wallet_path = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(
        MNEMONIC, wallet_path, "test-encryption-password" if encrypted else None
    )
    config_path = tmp_path / "config.toml"
    # The wallet section is required: get_mnemonic_file reads the active
    # wallet from config.toml, and the fingerprint confirmation (now
    # fail-closed) needs it to derive the fingerprint for real.
    config_path.write_text(
        f'[bitcoin]\nnetwork = "regtest"\n\n[wallet]\nmnemonic_file = "{wallet_path}"\n'
    )
    content = SCRIPT_PATH.read_text()
    helpers = content.split(
        "# =============================================================================\n# Helpers",
        1,
    )[1].split(
        "# =============================================================================\n# Main Loop",
        1,
    )[0]
    shell_path = tmp_path / "wallet-info.sh"
    escaped_passphrase = passphrase.replace("'", "'\\''")
    shell_path.write_text(
        helpers
        + """
clear() { :; }
pause() { :; }
whiptail() {
    if [ "$2" = " Wallet Password " ]; then
        printf '%s' 'test-encryption-password' >&2
        return 0
    fi
    if [ "$2" = " BIP39 Passphrase " ]; then
        printf '%s' '"""
        + escaped_passphrase
        + """' >&2
        return 0
    fi
    if [ "$2" = " Wallet Fingerprint " ]; then
        # Default-Yes confirmation
        return 0
    fi
    return 90
}
# Mock jm-wallet so we can assert the cached BIP39_PASSPHRASE reaches the CLI.
jm-wallet() {
    echo "BIP39_PASSPHRASE_IN_CLI=${BIP39_PASSPHRASE}"
    echo "MNEMONIC_PASSWORD_IN_CLI=${MNEMONIC_PASSWORD}"
    return 0
}
ensure_wallet_password() { :; }
CURRENT_WALLET="""
        + f'"{wallet_path}"'
        + f'\nFINGERPRINT_CACHE="{tmp_path}/.current_fingerprint"\n'
        # The wallet password is passed inline (not exported) so the jm-wallet
        # stub cannot echo it -- mirroring the real flow, where
        # ensure_wallet_password provides it for the fingerprint computation.
        + ("MNEMONIC_PASSWORD='test-encryption-password' " if encrypted else "")
        + """ensure_wallet_unlocked_global || exit 1
jm-wallet info --prompt-bip39-passphrase
"""
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MNEMONIC", "BIP39_", "JOINMARKET_", "WALLET__"))
    }
    env.update(
        TUI_PYTHON=sys.executable,
        CONFIG_FILE=str(config_path),
        JOINMARKET_CONFIG_FILE=str(config_path),
        JOINMARKET_DATA_DIR=str(tmp_path),
        MNEMONIC_FILE=str(wallet_path),
        CURRENT_WALLET=str(wallet_path),
        MAKER_ENV=str(tmp_path / ".maker.env"),
    )
    result = _run_test_shell(
        ["bash", str(shell_path)],
        input_text="",
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert f"BIP39_PASSPHRASE_IN_CLI={passphrase}" in result.stdout, result.stdout
    assert "test-encryption-password" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "command",
    [
        "history",
        "freeze",
        "send",
        "list-bonds",
        "generate-bond-address",
        "taker-coinjoin",
    ],
)
@pytest.mark.parametrize("passphrase", [PASSPHRASE, ""])
@pytest.mark.parametrize("encrypted", [True, False])
def test_wallet_operations_reuse_cached_passphrase(
    tmp_path: Path, command: str, passphrase: str, encrypted: bool
) -> None:
    """HIST/FREEZE/SEND/BONDS/TAKER derive the same wallet via cached BIP39_PASSPHRASE."""
    wallet_path = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(
        MNEMONIC, wallet_path, "test-encryption-password" if encrypted else None
    )
    config_path = tmp_path / "config.toml"
    # The wallet section is required: get_mnemonic_file reads the active
    # wallet from config.toml, and the fingerprint confirmation (now
    # fail-closed) needs it to derive the fingerprint for real.
    config_path.write_text(
        f'[bitcoin]\nnetwork = "regtest"\n\n[wallet]\nmnemonic_file = "{wallet_path}"\n'
    )
    content = SCRIPT_PATH.read_text()
    helpers = content.split(
        "# =============================================================================\n# Helpers",
        1,
    )[1].split(
        "# =============================================================================\n# Main Loop",
        1,
    )[0]
    shell_path = tmp_path / "wallet-op.sh"
    escaped_passphrase = passphrase.replace("'", "'\\''")
    shell_path.write_text(
        helpers
        + """
clear() { :; }
pause() { :; }
whiptail() {
    if [ "$2" = " Wallet Password " ]; then
        printf '%s' 'test-encryption-password' >&2
        return 0
    fi
    if [ "$2" = " BIP39 Passphrase " ]; then
        printf '%s' '"""
        + escaped_passphrase
        + """' >&2
        return 0
    fi
    if [ "$2" = " Wallet Fingerprint " ]; then
        # Default-Yes confirmation
        return 0
    fi
    return 90
}
ensure_wallet_password() { :; }
jm-wallet() {
    echo "BIP39_PASSPHRASE_IN_CLI=${BIP39_PASSPHRASE}"
    return 0
}
jm-taker() {
    echo "BIP39_PASSPHRASE_IN_CLI=${BIP39_PASSPHRASE}"
    return 0
}
"""
        + f"""
CURRENT_WALLET="{wallet_path}"
FINGERPRINT_CACHE="{tmp_path}/.current_fingerprint"
"""
        # Inline (not exported) so only the fingerprint computation sees it,
        # mirroring the real flow where ensure_wallet_password provides it.
        + ("MNEMONIC_PASSWORD='test-encryption-password' " if encrypted else "")
        + f"""ensure_wallet_unlocked_global || exit 1
[ "${{BIP39_PASSPHRASE}}" = '{escaped_passphrase}' ] || exit 93
if [ "{command}" = "taker-coinjoin" ]; then
    jm-taker dummy
else
    jm-wallet {command} --prompt-bip39-passphrase
fi
"""
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MNEMONIC", "BIP39_", "JOINMARKET_", "WALLET__"))
    }
    env.update(
        TUI_PYTHON=sys.executable,
        CONFIG_FILE=str(config_path),
        JOINMARKET_CONFIG_FILE=str(config_path),
        JOINMARKET_DATA_DIR=str(tmp_path),
        MNEMONIC_FILE=str(wallet_path),
        CURRENT_WALLET=str(wallet_path),
        MAKER_ENV=str(tmp_path / ".maker.env"),
    )
    result = _run_test_shell(
        ["bash", str(shell_path)],
        input_text="",
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert f"BIP39_PASSPHRASE_IN_CLI={passphrase}" in result.stdout, result.stdout
    assert "test-encryption-password" not in result.stdout + result.stderr


def test_cli_resolve_mnemonic_empty_env_means_no_passphrase(tmp_path: Path) -> None:
    """An explicitly empty BIP39_PASSPHRASE env must not trigger a prompt when
    wallet_with_passphrase is enabled (the TUI session cache relies on this)."""
    from jmcore.cli_common import resolve_mnemonic
    from jmcore.settings import get_settings

    wallet_path = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, wallet_path, None)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[bitcoin]\nnetwork = "regtest"\n[wallet]\nwallet_with_passphrase = true\n'
    )
    os.environ["JOINMARKET_CONFIG_FILE"] = str(config_path)
    os.environ["BIP39_PASSPHRASE"] = ""
    settings = get_settings()
    resolved = resolve_mnemonic(
        settings=settings,
        mnemonic_file=wallet_path,
        bip39_passphrase="",
        prompt_bip39_passphrase=True,
    )
    assert resolved is not None
    assert resolved.bip39_passphrase == ""
    assert resolved.mnemonic is not None
