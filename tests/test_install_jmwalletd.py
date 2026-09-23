"""Hermetic installer coverage for daemon selection and upgrades."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"
COMMIT = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _run(
    *args: str,
    installed: bool = False,
    missing_lock: bool = False,
    switch_to_update: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Exercise selection and package actions without network or a virtualenv."""
    script = f'''\
source "{INSTALL_SH}"
set +e
print_header() {{ :; }}
print_info() {{ echo "INFO: $1"; }}
print_success() {{ :; }}
print_warning() {{ :; }}
print_error() {{ echo "ERR: $1"; }}
get_latest_version() {{ echo v9.9.9; }}
resolve_to_commit_hash() {{ echo {COMMIT}; }}
verify_release_signature() {{ return 0; }}
verify_update_imports() {{ return 0; }}
prepare_verified_source() {{ return 0; }}
RELEASE_LOG=$(mktemp)
read_release_file() {{
    echo "LOCK: $1" >> "$RELEASE_LOG"
    if [[ "$1" == jmwalletd/requirements.txt && "{str(missing_lock).lower()}" == true ]]; then
        return 1
    fi
    printf 'idna==3.10\n'
}}
pip() {{
    if [[ "$1" == show ]]; then
        [[ "$2" == jmwalletd && "{str(installed).lower()}" == true ]]
        return
    fi
    echo "PIP: $*"
}}
parse_args {" ".join(args)}
if [[ "{str(switch_to_update).lower()}" == true ]]; then MODE=update; fi
echo "SELECT: $MODE $INSTALL_MAKER $INSTALL_TAKER $INSTALL_TUMBLER $INSTALL_JMWALLETD"
SKIP_VERIFY=false
INSTALL_VERSION=v9.9.9
( "${{MODE}}_packages" )
echo "EXIT:$?"
cat "$RELEASE_LOG"
rm -f "$RELEASE_LOG"
'''
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, check=False
    )


def _lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [
        line.removeprefix("PIP: ")
        for line in result.stdout.splitlines()
        if line.startswith("PIP: ")
    ]


@pytest.mark.parametrize("args", [(), ("--maker", "--taker", "--orderbook-watcher")])
def test_fresh_complete_profile_installs_daemon_and_runtime(
    args: tuple[str, ...],
) -> None:
    result = _run(*args)
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "SELECT: install true true true false" in result.stdout
    assert "subdirectory=jmwalletd" in " ".join(_lines(result))
    daemon_install = next(
        line
        for line in _lines(result)
        if line.startswith("install git+") and "subdirectory=jmwalletd" in line
    )
    assert all(
        f"subdirectory={pkg}" in daemon_install for pkg in ("maker", "taker", "tumbler")
    )
    assert "--require-hashes" in result.stdout
    assert "LOCK: jmwalletd/requirements.txt" in result.stdout


@pytest.mark.parametrize("args", [("--taker",), ("--orderbook-watcher",)])
def test_fresh_minimal_profile_omits_daemon(args: tuple[str, ...]) -> None:
    result = _run(*args)
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert not any("subdirectory=jmwalletd" in line for line in _lines(result))
    assert "LOCK: jmwalletd/requirements.txt" not in result.stdout


def test_explicit_daemon_on_minimal_fresh_profile_adds_runtime() -> None:
    result = _run("--jmwalletd")
    assert "SELECT: install true true true true" in result.stdout
    assert "subdirectory=jmwalletd" in " ".join(_lines(result))


@pytest.mark.parametrize("args", [(), ("--taker",)])
def test_old_install_without_daemon_does_not_implicitly_add_it(
    args: tuple[str, ...],
) -> None:
    result = _run("--update", *args)
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "SELECT: update" in result.stdout
    assert not any("subdirectory=jmwalletd" in line for line in _lines(result))
    assert "LOCK: jmwalletd/requirements.txt" not in result.stdout


def test_existing_venv_switch_to_update_does_not_backfill_daemon() -> None:
    result = _run(switch_to_update=True)
    assert "SELECT: update true true true false" in result.stdout
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert not any("subdirectory=jmwalletd" in line for line in _lines(result))


def test_update_opt_in_installs_daemon_and_dependencies() -> None:
    result = _run("--update", "--jmwalletd")
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "SELECT: update true true true true" in result.stdout
    assert all(
        f"subdirectory={pkg}" in _lines(result)[-1]
        for pkg in ("jmwalletd", "maker", "taker", "tumbler")
    )
    assert "LOCK: jmwalletd/requirements.txt" in result.stdout


def test_existing_daemon_updates_even_when_minimal_role_selected() -> None:
    result = _run("--update", "--taker", installed=True)
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "SELECT: update false true false false" in result.stdout
    assert any(
        "--force-reinstall --no-deps" in line and "subdirectory=jmwalletd" in line
        for line in _lines(result)
    )
    assert all(
        f"LOCK: {pkg}/requirements.txt" in result.stdout
        for pkg in ("maker", "taker", "tumbler", "jmwalletd")
    )


def test_missing_daemon_lock_aborts_before_package_install() -> None:
    result = _run("--jmwalletd", missing_lock=True)
    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert not _lines(result)
