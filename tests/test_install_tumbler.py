"""Hermetic installer profile tests for the tumbler package."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
COMMIT = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _run_packages(
    *,
    mode: str,
    maker: bool,
    taker: bool,
    tumbler_installed: bool = False,
    pinned_deps: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run an installer package path with all external operations stubbed."""

    script = f'''\
source "{INSTALL_SH}"
set +e

print_header() {{ :; }}
print_info() {{ echo "INFO: $1"; }}
print_success() {{ echo "OK: $1"; }}
print_warning() {{ echo "WARN: $1"; }}
print_error() {{ echo "ERR: $1"; }}
get_latest_version() {{ echo "v9.9.9"; }}
resolve_to_commit_hash() {{ echo "{COMMIT}"; }}
verify_release_signature() {{ return 0; }}
verify_update_imports() {{ return 0; }}
python3() {{ return 0; }}

RELEASE_FILE_LOG="$(mktemp)"
prepare_verified_source() {{ return 0; }}
read_release_file() {{
    printf '%s\n' "$1" >> "$RELEASE_FILE_LOG"
    printf 'idna==3.10\n'
}}

pip() {{
    if [[ "$1" == "show" ]]; then
        [[ "$2" == "jm-tumbler" && "{"true" if tumbler_installed else "false"}" == "true" ]]
        return
    fi
    echo "PIP: $*"
}}

INSTALL_VERSION="v9.9.9"
INSTALL_MAKER="{"true" if maker else "false"}"
INSTALL_TAKER="{"true" if taker else "false"}"
derive_install_tumbler
SKIP_VERIFY="{"false" if pinned_deps else "true"}"
PINNED_DEPS=true

{mode}_packages
echo "EXIT:$?"
echo "RELEASE_FILE_LOG_START"
cat "$RELEASE_FILE_LOG"
rm -f "$RELEASE_FILE_LOG"
'''
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _pip_lines(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [
        line.removeprefix("PIP: ")
        for line in result.stdout.splitlines()
        if line.startswith("PIP: ")
    ]


def _release_file_paths(result: subprocess.CompletedProcess[str]) -> list[str]:
    return result.stdout.split("RELEASE_FILE_LOG_START\n", maxsplit=1)[1].splitlines()


def test_complete_profile_installs_tumbler_after_maker_and_taker() -> None:
    result = _run_packages(mode="install", maker=True, taker=True)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    packages = _pip_lines(result)
    maker_index = next(
        i for i, line in enumerate(packages) if "subdirectory=maker" in line
    )
    taker_index = next(
        i for i, line in enumerate(packages) if "subdirectory=taker" in line
    )
    tumbler_index = next(
        i for i, line in enumerate(packages) if "subdirectory=tumbler" in line
    )
    assert maker_index < taker_index < tumbler_index


@pytest.mark.parametrize(("maker", "taker"), [(True, False), (False, True)])
def test_single_role_profiles_do_not_install_tumbler(maker: bool, taker: bool) -> None:
    result = _run_packages(mode="install", maker=maker, taker=taker)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert not any("subdirectory=tumbler" in line for line in _pip_lines(result))


def test_update_installs_missing_tumbler_for_complete_profile() -> None:
    result = _run_packages(
        mode="update", maker=True, taker=True, tumbler_installed=False
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "Installing tumbler" in result.stdout
    assert any("subdirectory=tumbler" in line for line in _pip_lines(result))


def test_update_upgrades_existing_tumbler_for_minimal_profile() -> None:
    result = _run_packages(
        mode="update", maker=False, taker=True, tumbler_installed=True
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "Updating tumbler" in result.stdout
    assert any(
        "--force-reinstall --no-deps" in line and "tumbler" in line
        for line in _pip_lines(result)
    )


def test_complete_profile_fetches_tumbler_lock_for_hash_verification() -> None:
    result = _run_packages(mode="install", maker=True, taker=True, pinned_deps=True)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert _release_file_paths(result) == [
        "jmcore/requirements.txt",
        "jmwallet/requirements.txt",
        "maker/requirements.txt",
        "taker/requirements.txt",
        "tumbler/requirements.txt",
        "jmwalletd/requirements.txt",
    ]
    assert any("--require-hashes" in line for line in _pip_lines(result))
