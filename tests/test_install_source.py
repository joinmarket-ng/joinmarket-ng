"""Authenticated ancillary files and native dependency update regressions."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"
pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="bash and git are required",
)


def run_shell(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(INSTALL_SH))}\n{script}"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def make_source(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    files = {
        "jmcore/requirements.txt": "idna==3.10\n",
        "jmwallet/requirements.txt": "mnemonic==0.21\n",
        "completions/jm-wallet.bash": "# authenticated completion\n",
        "jmcore/src/jmcore/data/config.toml.template": "# authenticated config\n",
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for args in [
        ["init", "--quiet"],
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "test",
        ],
    ]:
        subprocess.run(
            ["git", "-C", str(source), *args], check=True, capture_output=True
        )
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    return source, commit


def make_source_with_starter(tmp_path: Path) -> tuple[Path, str]:
    source, _ = make_source(tmp_path)
    starter = source / "jmcore/src/jmcore/data/config-starter.toml.template"
    starter.write_text("# authenticated starter\n")
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "add",
            str(starter.relative_to(source)),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "starter",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    return source, commit


def source_env(tmp_path: Path, source: Path, commit: str) -> dict[str, str]:
    return {
        "JOINMARKET_DATA_DIR": str(tmp_path / "data"),
        "TMPDIR": str(tmp_path),
        "TEST_COMMIT": commit,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{source.as_uri()}.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/joinmarket-ng/joinmarket-ng.git",
        "GIT_ALLOW_PROTOCOL": "file",
    }


def test_locks_config_and_completions_use_authenticated_git_objects(
    tmp_path: Path,
) -> None:
    source, commit = make_source(tmp_path)
    # The worktree and its current HEAD no longer represent the signed release.
    (source / "completions/jm-wallet.bash").write_text("untrusted replacement\n")
    result = run_shell(
        """
SKIP_VERIFY=false
PINNED_DEPS=true
VERSION=1.2.3
VERIFIED_RELEASE_COMMIT="$TEST_COMMIT"
INSTALL_MAKER=false
INSTALL_TAKER=false
INSTALL_TUMBLER=false
INSTALL_ORDERBOOK_WATCHER=false
curl() { echo 'UNEXPECTED HTTP' >&2; return 1; }
pip() { return 1; }
trap cleanup_install EXIT
prepare_dep_pinning "$TEST_COMMIT"
cat "$DEP_HASHED_FILE"
setup_data_directory
setup_cli_completion
""",
        source_env(tmp_path, source, commit),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "idna==3.10" in result.stdout
    assert "mnemonic==0.21" in result.stdout
    assert "UNEXPECTED HTTP" not in result.stderr
    data = tmp_path / "data"
    assert (data / "config.toml").read_text() == "# authenticated config\n"
    assert (
        data / "completions/jm-wallet.bash"
    ).read_text() == "# authenticated completion\n"
    assert not list(tmp_path.glob("jmng-source.*"))


def test_authenticated_starter_and_full_reference_are_distinct(tmp_path: Path) -> None:
    source, commit = make_source_with_starter(tmp_path)
    # Neither current worktree file may influence the authenticated release copy.
    (source / "jmcore/src/jmcore/data/config-starter.toml.template").write_text(
        "untrusted starter\n"
    )
    (source / "jmcore/src/jmcore/data/config.toml.template").write_text(
        "untrusted full template\n"
    )
    result = run_shell(
        """
SKIP_VERIFY=false
VERSION=1.2.3
VERIFIED_RELEASE_COMMIT="$TEST_COMMIT"
prepare_verified_source "$TEST_COMMIT"
trap cleanup_install EXIT
setup_data_directory
""",
        source_env(tmp_path, source, commit),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = tmp_path / "data"
    assert (data / "config.toml").read_text() == "# authenticated starter\n"
    assert (data / "config.toml.template").read_text() == "# authenticated config\n"


def test_missing_authenticated_source_fails_without_http_fallback(
    tmp_path: Path,
) -> None:
    source, _ = make_source(tmp_path)
    result = run_shell(
        """
SKIP_VERIFY=false
VERSION=1.2.3
curl() { echo 'UNEXPECTED HTTP' >&2; return 1; }
trap cleanup_install EXIT
prepare_verified_source "$TEST_COMMIT"
echo 'UNEXPECTED CONTINUATION'
""",
        source_env(tmp_path, source, "a" * 40),
    )
    assert result.returncode != 0
    assert "Could not fetch the authenticated release source" in result.stdout
    assert "UNEXPECTED" not in result.stdout + result.stderr
    assert not list(tmp_path.glob("jmng-source.*"))


@pytest.mark.parametrize(
    "status", ["deinstall ok config-files", "unknown ok not-installed", ""]
)
def test_update_installs_missing_native_library_before_pip(
    tmp_path: Path, status: str
) -> None:
    result = run_shell(
        """
INSTALLER_AUTHENTICATED=true
detect_os() { OS_TYPE=linux; PKG_MANAGER=apt; }
dpkg-query() {
    if [[ "${@: -1}" == libsecp256k1-dev ]]; then
        printf '%s' "$TEST_PACKAGE_STATUS"
    else
        printf 'install ok installed'
    fi
}
sudo() { "$@"; }
apt() { printf 'APT:%s\n' "$*"; }
prepare_release() { :; }
setup_virtualenv() { echo 'VENV'; }
update_packages() { echo 'PACKAGES'; }
migrate_config() { :; }
create_shell_integration() { :; }
save_trusted_installer() { :; }
main --update --skip-verify --skip-tor --yes
""",
        {"TEST_PACKAGE_STATUS": status, "JOINMARKET_DATA_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "APT:install -y libsecp256k1-dev" in result.stdout
    assert result.stdout.index("APT:install") < result.stdout.index("VENV")
    assert result.stdout.index("VENV") < result.stdout.index("PACKAGES")


def test_installed_native_dependency_needs_no_package_manager_action(
    tmp_path: Path,
) -> None:
    result = run_shell(
        """
AUTO_YES=true
detect_os() { OS_TYPE=linux; PKG_MANAGER=apt; }
dpkg-query() { printf 'install ok installed'; }
apt() { echo 'UNEXPECTED APT'; return 1; }
check_system_dependencies
""",
        {"JOINMARKET_DATA_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "UNEXPECTED APT" not in result.stdout
