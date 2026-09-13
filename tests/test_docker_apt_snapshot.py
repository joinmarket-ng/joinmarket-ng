"""Debian snapshot pinning for reproducible Docker images.

Every apt install in the Dockerfiles goes through ``scripts/docker-apt-install.sh``,
which resolves packages against snapshot.debian.org at the ``DEBIAN_SNAPSHOT``
timestamp. That pins the whole transitive package closure, so releases keep
reproducing after the live Debian archive rotates (issue #631).
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "docker-apt-install.sh"
DOCKERFILES = {
    name: REPO_ROOT / name / "Dockerfile"
    for name in ("directory_server", "jmwalletd", "maker", "orderbook_watcher", "taker")
}
HELPER_MOUNT = (
    "--mount=type=bind,source=scripts/docker-apt-install.sh,"
    "target=/tmp/docker-apt-install.sh"
)
SNAPSHOT_RE = re.compile(r"^\d{8}T\d{6}Z$")

BASE_IMAGE_SOURCES = """\
Types: deb
# http://snapshot.debian.org/archive/debian/20260824T000000Z
URIs: http://deb.debian.org/debian
Suites: trixie trixie-updates
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp

Types: deb
# http://snapshot.debian.org/archive/debian-security/20260824T000000Z
URIs: http://deb.debian.org/debian-security
Suites: trixie-security
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp
"""


def _stages(dockerfile: str) -> list[str]:
    """Split a Dockerfile into per-stage chunks (text after each FROM)."""
    return re.split(r"^FROM\s", dockerfile, flags=re.MULTILINE)[1:]


def test_dockerfiles_install_apt_packages_from_one_pinned_snapshot() -> None:
    pinned: dict[str, str] = {}
    for name, path in DOCKERFILES.items():
        content = path.read_text()

        assert "apt-get install" not in content, (
            f"{name}: raw apt-get install bypasses the snapshot"
        )
        assert not re.search(r"^\s+[a-z][a-z0-9.+-]*=\S", content, re.MULTILINE), (
            f"{name}: per-package version pins are redundant with DEBIAN_SNAPSHOT"
        )

        globals_ = re.findall(r"^ARG DEBIAN_SNAPSHOT=(\S+)$", content, re.MULTILINE)
        assert len(globals_) == 1, (
            f"{name}: expected exactly one global DEBIAN_SNAPSHOT pin"
        )
        assert SNAPSHOT_RE.match(globals_[0]), f"{name}: bad snapshot id {globals_[0]}"
        pinned[name] = globals_[0]

        helper_runs = 0
        for stage in _stages(content):
            if HELPER_MOUNT not in stage:
                continue
            helper_runs += stage.count(HELPER_MOUNT)
            # The helper reads DEBIAN_SNAPSHOT from the environment, and a
            # global ARG is only visible inside a stage when re-declared.
            assert "\nARG DEBIAN_SNAPSHOT\n" in stage, (
                f"{name}: stage using the apt helper must re-declare ARG DEBIAN_SNAPSHOT"
            )
            assert stage.index("ARG DEBIAN_SNAPSHOT\n") < stage.index(HELPER_MOUNT)
        assert helper_runs >= 2, (
            f"{name}: builder and runtime stages should use the helper"
        )

    assert len(set(pinned.values())) == 1, (
        f"DEBIAN_SNAPSHOT differs across Dockerfiles: {pinned}"
    )


def test_helper_is_a_posix_sh_script_with_exec_bit() -> None:
    assert HELPER.read_text().startswith("#!/bin/sh\n")
    assert HELPER.stat().st_mode & stat.S_IXUSR


def _install_fakes(bin_dir: Path, log: Path) -> None:
    """Fake apt-get and rm that record their argv, one invocation per line.

    apt-get additionally snapshots the sources file it was pointed at, since
    the helper deletes its temporary directory before exiting.
    """
    bin_dir.mkdir()
    fake_apt = bin_dir / "apt-get"
    fake_apt.write_text(
        "#!/bin/bash\n"
        f'printf "apt-get %s\\n" "$*" >> "{log}"\n'
        "subcommand=; sources=\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    Dir::Etc::SourceList=*) sources="${arg#Dir::Etc::SourceList=}";;\n'
        '    update|install) subcommand="$arg";;\n'
        "  esac\n"
        "done\n"
        f'cp "$sources" "{log.parent}/captured-$subcommand.sources"\n'
    )
    fake_apt.chmod(0o755)
    fake_rm = bin_dir / "rm"
    fake_rm.write_text(f'#!/bin/bash\nprintf "rm %s\\n" "$*" >> "{log}"\n')
    fake_rm.chmod(0o755)


def _run_helper(
    tmp_path: Path,
    packages: list[str],
    sources_text: str = BASE_IMAGE_SOURCES,
    **env_overrides: str,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    log = tmp_path / "calls.log"
    _install_fakes(bin_dir, log)
    sources = tmp_path / "debian.sources"
    sources.write_text(sources_text)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "APT_SOURCES_FILE": str(sources),
        "DEBIAN_SNAPSHOT": "20260913T082142Z",
        **env_overrides,
    }
    return subprocess.run(
        ["sh", str(HELPER), *packages],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_helper_resolves_apt_against_the_snapshot_and_cleans_up(tmp_path: Path) -> None:
    result = _run_helper(tmp_path, ["libsodium23", "tini"])
    assert result.returncode == 0, result.stderr

    calls = (tmp_path / "calls.log").read_text().splitlines()
    apt_calls = [c for c in calls if c.startswith("apt-get ")]
    assert len(apt_calls) == 2
    update, install = apt_calls
    assert update.endswith(" update")
    assert install.endswith(" install -y --no-install-recommends libsodium23 tini")
    for call in apt_calls:
        assert "-o Acquire::Check-Valid-Until=false" in call
        assert "-o Dir::Etc::SourceList=" in call
        assert "-o Dir::Etc::SourceParts=" in call

    captured = (tmp_path / "captured-update.sources").read_text()
    assert captured == (tmp_path / "captured-install.sources").read_text()
    assert "deb.debian.org" not in captured
    assert (
        "URIs: https://snapshot.debian.org/archive/debian/20260913T082142Z\n"
        in captured
    )
    assert (
        "URIs: https://snapshot.debian.org/archive/debian-security/20260913T082142Z\n"
        in captured
    )
    # Suites and signing key are untouched, only the archive location moves.
    assert "Suites: trixie trixie-updates\n" in captured
    assert "Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp\n" in captured

    rm_calls = [c for c in calls if c.startswith("rm ")]
    assert len(rm_calls) == 1
    assert "/var/lib/apt/lists/*" in rm_calls[0]
    assert "/var/cache/ldconfig/aux-cache" in rm_calls[0]
    # The temporary sources directory must not leak into the image layer.
    sources_dir = re.search(r"Dir::Etc::SourceList=(\S+)/snapshot\.sources", update)
    assert sources_dir is not None
    assert sources_dir.group(1) in rm_calls[0]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"DEBIAN_SNAPSHOT": ""}, "DEBIAN_SNAPSHOT is not set"),
        ({"DEBIAN_SNAPSHOT": "2026-09-13"}, "must look like"),
    ],
)
def test_helper_rejects_missing_or_malformed_snapshot(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    result = _run_helper(tmp_path, ["tini"], **overrides)
    assert result.returncode == 1
    assert message in result.stderr
    assert not (tmp_path / "calls.log").exists()


def test_helper_refuses_sources_it_cannot_redirect(tmp_path: Path) -> None:
    """A stanza that still points at the live archive would silently break pinning."""
    result = _run_helper(
        tmp_path,
        ["tini"],
        sources_text="Types: deb\nURIs: http://deb.debian.org/debian-backports\nSuites: trixie\n",
    )
    assert result.returncode == 1
    assert "unrecognized apt sources" in result.stderr
    assert not (tmp_path / "calls.log").exists()
