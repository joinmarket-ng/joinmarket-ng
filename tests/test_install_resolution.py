"""Regression tests for fail-closed installer commit resolution."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _run_latest_version(payload: str) -> subprocess.CompletedProcess[str]:
    script = f'''\
source "{INSTALL_SH}"
set +e

curl() {{ printf '%s' "$TEST_GITHUB_RESPONSE"; }}
version=$(get_latest_version)
status=$?
printf 'VERSION:%s\n' "$version"
printf 'EXIT:%s\n' "$status"
'''
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={**os.environ, "TEST_GITHUB_RESPONSE": payload},
    )


def test_latest_version_uses_stable_release_response() -> None:
    result = _run_latest_version(
        '{"tag_name":"0.39.1","draft":false,"prerelease":false}\n'
    )

    assert result.stdout == "VERSION:0.39.1\nEXIT:0\n"
    assert result.stderr == ""


@pytest.mark.parametrize(
    "payload",
    [
        '{"message":"API rate limit exceeded"}\n',
        '{"tag_name":"0.39.2","draft":false,"prerelease":true}\n',
    ],
    ids=["missing-tag", "prerelease-tag"],
)
def test_latest_version_failure_does_not_fall_back_to_embedded_version(
    payload: str,
) -> None:
    result = _run_latest_version(payload)

    assert result.stdout == "VERSION:\nEXIT:1\n"
    assert "Could not determine the latest stable release from GitHub" in result.stderr
    assert "0.39.2" not in result.stdout


def _run_package_path(
    *, mode: str, skip_verify: bool
) -> subprocess.CompletedProcess[str]:
    """Run an install/update with resolution falling back to the release ref."""

    script = f'''\
source "{INSTALL_SH}"
set +e

print_header() {{ :; }}
print_info() {{ echo "INFO: $1"; }}
print_success() {{ echo "OK: $1"; }}
print_warning() {{ echo "WARN: $1"; }}
print_error() {{ echo "ERR: $1"; }}

get_latest_version() {{ echo "v9.9.9"; }}
resolve_to_commit_hash() {{ echo "$1"; }}
verify_release_signature() {{ echo "VERIFY: $1 $2"; return 0; }}
verify_update_imports() {{ return 0; }}
python3() {{ return 0; }}
pip() {{
    [[ "$1" == "show" ]] && return 1
    echo "PIP: $*"
    return 0
}}

INSTALL_VERSION="v9.9.9"
INSTALL_MAKER=false
INSTALL_TAKER=false
INSTALL_TUMBLER=false
INSTALL_ORDERBOOK_WATCHER=false
SKIP_VERIFY={"true" if skip_verify else "false"}
PINNED_DEPS=true

( {mode}_packages )
echo "EXIT:$?"
'''
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("mode", ["install", "update"])
def test_unresolved_release_aborts_before_verification_or_install(mode: str) -> None:
    result = _run_package_path(mode=mode, skip_verify=False)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "Could not resolve v9.9.9 to a full commit hash" in result.stdout
    assert "VERIFY:" not in result.stdout
    assert "PIP:" not in result.stdout


@pytest.mark.parametrize("mode", ["install", "update"])
def test_skip_verify_explicitly_allows_unresolved_release(mode: str) -> None:
    result = _run_package_path(mode=mode, skip_verify=True)

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "VERIFY: v9.9.9 v9.9.9" in result.stdout
    assert "PIP:" in result.stdout
