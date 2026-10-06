from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
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


@pytest.mark.parametrize("registered", [True, False])
def test_selected_identity_display_does_not_need_unlock_or_cache(
    tmp_path: Path,
    registered: bool,
) -> None:
    from jmcore.wallet_metadata import (
        WalletIdentity,
        register_identity,
        select_identity,
    )

    wallet_path = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(MNEMONIC, wallet_path, "test-encryption-password")
    fingerprint = get_mnemonic_fingerprint(MNEMONIC, PASSPHRASE)
    if registered:
        register_identity(
            wallet_path, WalletIdentity(fingerprint=fingerprint, bip39="required")
        )
        select_identity(wallet_path, fingerprint)
    content = SCRIPT_PATH.read_text()
    names = ("wallet_identity_summary", "check_stale_wallet", "ensure_active_wallet")
    functions = "\n".join(
        name + "()" + content.split(name + "()", 1)[1].split("\n}", 1)[0] + "\n}"
        for name in names
    )
    script = (
        functions
        + """
get_mnemonic_file() { echo "$TEST_WALLET"; }
whiptail() { exit 99; }
check_stale_wallet
echo "$WALLET_INFO"
ensure_active_wallet || exit 1
echo "$WALLET_INFO"
"""
    )
    env = os.environ | {
        "TUI_PYTHON": sys.executable,
        "TEST_WALLET": str(wallet_path),
        "FINGERPRINT_CACHE": str(tmp_path / "absent-cache"),
    }
    result = _run_test_shell(["bash", "-c", script], input_text="", env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    expected = (
        f"{fingerprint} (BIP39: required)" if registered else "identity unconfirmed"
    )
    assert result.stdout.count(expected) == 2
    assert not (tmp_path / "absent-cache").exists()
    assert "test-encryption-password" not in result.stdout + result.stderr


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


@pytest.mark.parametrize("view", ["BASIC", "EXT"])
@pytest.mark.parametrize("encrypted", [True, False])
@pytest.mark.parametrize("passphrase", [PASSPHRASE, ""])
@pytest.mark.parametrize("confirm", [True, False])
@pytest.mark.parametrize("policy", ["enabled", "registered", "disabled"])
def test_wallet_info_prompts_for_passphrase(
    tmp_path: Path,
    view: str,
    encrypted: bool,
    passphrase: str,
    confirm: bool,
    policy: str,
) -> None:
    """Run the real menu branch and CLI, stopping before backend access."""
    wallet_path = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(
        MNEMONIC, wallet_path, "test-encryption-password" if encrypted else None
    )
    config_path = tmp_path / "config.toml"
    config_text = (
        f'[bitcoin]\nnetwork = "regtest"\n[wallet]\nmnemonic_file = "{wallet_path}"\n'
    )
    if policy == "enabled":
        config_text += "bip39_passphrase_enabled = true\n"
    config_path.write_text(config_text)
    if policy == "registered":
        from jmcore.wallet_metadata import (
            WalletIdentity,
            register_identity,
            select_identity,
        )

        fingerprint = get_mnemonic_fingerprint(MNEMONIC, passphrase)
        register_identity(
            wallet_path,
            WalletIdentity(
                fingerprint=fingerprint,
                bip39="required" if passphrase else "none",
            ),
        )
        select_identity(wallet_path, fingerprint)
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
    case "$2" in
        ' Wallet Password ') printf '%s' 'test-encryption-password' >&2 ;;
        ' BIP39 Passphrase ') printf '%s' "$TEST_PASSPHRASE" >&2 ;;
        ' Wallet Fingerprint ')
            echo "$4" >&1
            [ "$TEST_CONFIRM" = true ] ;;
        *) return 90 ;;
    esac
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
    cli_path.write_text("""
from unittest.mock import patch
from jmwallet.cli import app
from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

async def show_info(mnemonic, backend, passphrase, **kwargs):
    print('SELECTED_WALLET=' + get_mnemonic_fingerprint(mnemonic, passphrase))
    print('EXTENDED=' + str(kwargs['extended']))

with patch('jmwallet.cli.wallet._show_wallet_info', show_info):
    app()
""")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("MNEMONIC", "BIP39_", "JOINMARKET_", "WALLET__", "EXPECTED_FINGERPRINT"),
        )
    }
    env.update(
        TUI_PYTHON=sys.executable,
        CLI_PATH=str(cli_path),
        CONFIG_FILE=str(config_path),
        JOINMARKET_CONFIG_FILE=str(config_path),
        JOINMARKET_DATA_DIR=str(tmp_path),
        CURRENT_WALLET=str(wallet_path),
        MAKER_ENV=str(tmp_path / ".maker.env"),
        FINGERPRINT_CACHE=str(tmp_path / "fingerprint"),
        INFO_CHOICE=view,
        TEST_PASSPHRASE=passphrase,
        TEST_CONFIRM=str(confirm).lower(),
    )
    result = _run_test_shell(
        ["bash", str(shell_path)], input_text="", env=env, timeout=30
    )
    assert result.returncode == 0, result.stderr
    prompts = policy == "enabled" or (policy == "registered" and bool(passphrase))
    expected = get_mnemonic_fingerprint(
        MNEMONIC, passphrase if policy != "disabled" else ""
    )
    if prompts:
        assert f"Wallet fingerprint: {expected}" in result.stdout + result.stderr
        assert "Continue with this wallet?" in result.stdout + result.stderr
    else:
        assert "Continue with this wallet?" not in result.stdout + result.stderr
    if confirm or not prompts:
        assert f"SELECTED_WALLET={expected}" in result.stdout, result.stdout
        assert f"EXTENDED={view == 'EXT'}" in result.stdout
    else:
        assert "SELECTED_WALLET=" not in result.stdout
    assert PASSPHRASE not in result.stdout + result.stderr
    assert "test-encryption-password" not in result.stdout + result.stderr
    assert config_path.read_text() == config_text


@pytest.mark.parametrize("encrypted", [True, False])
@pytest.mark.parametrize("passphrase", [PASSPHRASE, ""])
def test_ensure_wallet_unlocked_global_caches_passphrase(
    tmp_path: Path, encrypted: bool, passphrase: str
) -> None:
    """With the flag on, ensure_wallet_unlocked_global prompts, confirms the
    fingerprint, and caches BIP39_PASSPHRASE (config sets
    wallet_with_passphrase=true)."""
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
        "wallet_with_passphrase = true\n"
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


@pytest.mark.parametrize("encrypted", [True, False])
def test_ensure_wallet_unlocked_global_skips_prompt_without_flag(
    tmp_path: Path, encrypted: bool
) -> None:
    """Without wallet_with_passphrase, unlock never prompts for a passphrase.

    Passphrases are strictly opt-in: the flag is unset here, so
    ensure_wallet_unlocked_global must export an explicitly empty
    BIP39_PASSPHRASE without calling the whiptail passphrase dialog (the
    whiptail stub fails the script if the dialog is ever shown).
    """
    wallet_path = tmp_path / "wallet.mnemonic"
    save_mnemonic_file(
        MNEMONIC, wallet_path, "test-encryption-password" if encrypted else None
    )
    config_path = tmp_path / "config.toml"
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
    shell_path = tmp_path / "wallet-no-prompt.sh"
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
        # The passphrase dialog must never appear without the flag.
        return 1
    fi
    if [ "$2" = " Wallet Fingerprint " ]; then
        return 0
    fi
    return 90
}
jm-wallet() {
    echo "BIP39_PASSPHRASE_IN_CLI=${BIP39_PASSPHRASE}"
    return 0
}
ensure_wallet_password() { :; }
"""
        + f'CURRENT_WALLET="{wallet_path}"\n'
        + f'FINGERPRINT_CACHE="{tmp_path}/.current_fingerprint"\n'
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
    assert "BIP39_PASSPHRASE_IN_CLI=" in result.stdout, result.stdout
    assert PASSPHRASE not in result.stdout


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
    """HIST/FREEZE/SEND/BONDS/TAKER derive the same wallet via cached BIP39_PASSPHRASE
    (config sets wallet_with_passphrase=true so the prompt path runs)."""
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
        "wallet_with_passphrase = true\n"
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
