"""
Regression tests for ``install.sh`` update behavior.

These lock in the fix for the post-upgrade ``ModuleNotFoundError: No
module named 'nacl'`` users hit after the libnacl -> PyNaCl swap.

Root cause: ``update_packages`` reinstalled the JoinMarket-NG packages
with ``--no-deps`` and then tried to satisfy dependencies with
``pip install --upgrade jmcore jmwallet`` -- but ``jmcore`` / ``jmwallet``
do not exist on PyPI, so that step was a no-op and never installed
changed/new third-party dependencies (like PyNaCl). The venv was left
missing the ``nacl`` module.

The tests are hermetic: they source ``install.sh`` and stub out ``pip``,
network helpers, and signature verification so no network, apt, or venv
is touched. We capture the exact ``pip`` invocations and assert the
update resolves dependencies from the git source rather than PyPI, and
that a missing ``nacl`` module triggers actionable remediation.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _run_update(
    extra_setup: str = "",
    python_import_ok: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Source install.sh, stub externals, and run ``update_packages``.

    ``pip`` is replaced by a function that logs its full argument list to
    stdout (prefixed ``PIP:``) so the test can assert on the invocations.
    ``python3 -c`` import checks succeed or fail based on
    ``python_import_ok`` to exercise the verification path.
    """
    # NOTE: use ``return`` (not ``exit``) so the stub does not terminate
    # the surrounding shell when invoked as a plain command (e.g. inside
    # ``if python3 ...; then``). A real ``python3`` is a subprocess, but a
    # shell function runs in the current shell.
    python_stub = "return 0" if python_import_ok else "return 1"
    script = f"""
source "{INSTALL_SH}"
# install.sh runs ``set -e`` at the top; disable it AFTER sourcing so a
# non-zero return from update_packages does not abort before we print
# the captured exit status.
set +e

# Avoid touching the real network / signatures / version lookups.
get_latest_version() {{ echo "v9.9.9"; }}
resolve_to_commit_hash() {{ echo "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"; }}
verify_release_signature() {{ return 0; }}
read_release_file() {{ cat "{REPO_ROOT}/$1"; }}

# Quiet, deterministic logging helpers.
print_header() {{ :; }}
print_info() {{ echo "INFO: $1"; }}
print_success() {{ echo "OK: $1"; }}
print_warning() {{ echo "WARN: $1"; }}
print_error() {{ echo "ERR: $1"; }}

# Capture pip invocations instead of executing them.
pip() {{ echo "PIP: $*"; return 0; }}

# Control whether the import verification "succeeds". Use ``return`` (not
# ``exit``) so the stub does not kill the shell when called as a plain
# command inside ``if python3 ...; then``.
python3() {{ {python_stub}; }}

SKIP_VERIFY=true
INSTALL_MAKER=false
INSTALL_TAKER=false
{extra_setup}

# update_packages itself calls ``... || exit 1``; guard with a subshell
# so we always reach the EXIT line and can observe the status.
( update_packages )
echo "EXIT:$?"
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_update_resolves_deps_from_git_not_pypi() -> None:
    """Dependency resolution must use the git source, never bare PyPI names.

    The old bug ran ``pip install --upgrade jmcore jmwallet`` (PyPI names
    that do not exist), so new deps were never installed. The fix must
    instead pass the git subdirectory URLs so pip resolves and installs
    dependencies like PyNaCl.
    """
    result = _run_update()
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr

    pip_lines = [
        line[len("PIP: ") :]
        for line in result.stdout.splitlines()
        if line.startswith("PIP: ")
    ]
    assert pip_lines, f"no pip invocations captured:\n{result.stdout}"

    # There must be at least one dependency-resolving install (no
    # --no-deps) that points at the jmcore git subdirectory.
    resolving = [
        line
        for line in pip_lines
        if "--no-deps" not in line and "subdirectory=jmcore" in line
    ]
    assert resolving, (
        "expected a dependency-resolving install from the jmcore git "
        "subdirectory; pip calls were:\n" + "\n".join(pip_lines)
    )

    # The buggy PyPI-name resolution must be gone: no install line should
    # request the bare ``jmcore``/``jmwallet`` PyPI names without a URL.
    for line in pip_lines:
        if "git+https" in line or "subdirectory=" in line:
            continue
        assert " jmcore" not in f" {line}", (
            f"update still tries to install jmcore from PyPI: {line}"
        )
        assert " jmwallet" not in f" {line}", (
            f"update still tries to install jmwallet from PyPI: {line}"
        )


def test_update_verifies_nacl_import() -> None:
    """A successful update verifies core libraries import cleanly."""
    result = _run_update(python_import_ok=True)
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "Core libraries verified" in result.stdout, result.stdout


def test_update_fails_loudly_when_import_broken() -> None:
    """A broken import after update must fail and print remediation.

    Even when automatic repair (re-installing PyNaCl) is attempted, a
    persistently broken venv must exit non-zero with manual guidance so
    the user is not left with a silent ModuleNotFoundError later.
    """
    result = _run_update(python_import_ok=False)
    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    combined = result.stdout
    assert "verification failed" in combined.lower(), combined
    assert "pynacl" in combined.lower(), combined


def test_update_attempts_pynacl_repair_on_missing_nacl() -> None:
    """When the import error names ``nacl``, repair installs PyNaCl.

    We stub python3 to fail with the exact ModuleNotFoundError message so
    the remediation branch that pip-installs ``pynacl`` is exercised.
    """
    script = f"""
source "{INSTALL_SH}"
set +e
print_header() {{ :; }}
print_info() {{ echo "INFO: $1"; }}
print_success() {{ echo "OK: $1"; }}
print_warning() {{ echo "WARN: $1"; }}
print_error() {{ echo "ERR: $1"; }}
pip() {{ echo "PIP: $*"; return 0; }}
python3() {{ echo "ModuleNotFoundError: No module named 'nacl'" >&2; return 1; }}
VENV_DIR="/tmp/does-not-matter"
verify_update_imports
echo "EXIT:$?"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    # python3 always fails here, so verification ultimately fails, but the
    # repair branch must have attempted to install PyNaCl.
    assert "PIP: install --upgrade pynacl>=1.5.0 --quiet" in result.stdout, (
        result.stdout + result.stderr
    )
    assert "EXIT:1" in result.stdout, result.stdout


def test_verify_does_not_require_nacl_for_versions_without_pynacl() -> None:
    """Verification must not force-import ``nacl``.

    Older releases (e.g. 0.30.0) do not use PyNaCl, so ``import nacl``
    fails even though the install is healthy. Verification must succeed as
    long as ``jmcore``/``jmwallet`` import, and must NOT run the PyNaCl
    "repair" (which would install a dependency the release does not need
    and print a confusing warning). We stub python3 to succeed only for an
    import line that does not mention ``nacl``.
    """
    script = f"""
source "{INSTALL_SH}"
set +e
print_header() {{ :; }}
print_info() {{ echo "INFO: $1"; }}
print_success() {{ echo "OK: $1"; }}
print_warning() {{ echo "WARN: $1"; }}
print_error() {{ echo "ERR: $1"; }}
pip() {{ echo "PIP: $*"; return 0; }}
# Succeed for the real verification import (jmcore, jmwallet), but fail
# loudly if anything tries to import the version-specific ``nacl`` module.
python3() {{
    case "$*" in
        *nacl*) echo "ModuleNotFoundError: No module named 'nacl'" >&2; return 1 ;;
        *) return 0 ;;
    esac
}}
VENV_DIR="/tmp/does-not-matter"
verify_update_imports
echo "EXIT:$?"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "Core libraries verified" in result.stdout, result.stdout
    # The misleading PyNaCl repair must not have run.
    assert "pynacl" not in result.stdout.lower(), result.stdout
    assert "PIP:" not in result.stdout, result.stdout


def _run_update_with_pinning(
    pinned_deps: bool = True,
    fetch_ok: bool = True,
    hash_install_ok: bool = True,
    installed_roles: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run ``update_packages`` with dependency pinning enabled.

    Unlike ``_run_update`` this leaves ``SKIP_VERIFY=false`` and provides a
    resolved commit hash so ``prepare_dep_pinning`` is active. The authenticated
    source and release-file reader are stubbed at their module boundaries so we
    can assert the pip invocations carry the right pinning arguments without
    fetching the fake commit.

    ``hash_install_ok`` controls whether the stubbed ``pip
    install --require-hashes`` succeeds; when ``False`` it fails so the
    hard-abort path can be exercised.
    """
    pinned = "true" if pinned_deps else "false"
    # Stubbed release-file reader: succeed (writing one pin) or fail to read
    # the lock after recording the requested relative path.
    read_file_body = 'printf "idna==3.10\\n"; return 0' if fetch_ok else "return 22"
    # pip stub: optionally fail the hash-checked install so the hard-abort
    # path is exercised; all other pip calls succeed.
    if hash_install_ok:
        pip_body = 'echo "PIP: $*"; return 0'
    else:
        pip_body = (
            'echo "PIP: $*"; '
            'case "$*" in '
            "*--require-hashes*) return 1 ;; "
            "*) return 0 ;; "
            "esac"
        )
    script = f"""
source "{INSTALL_SH}"
set +e

get_latest_version() {{ echo "v9.9.9"; }}
resolve_to_commit_hash() {{ echo "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"; }}
verify_release_signature() {{ return 0; }}

print_header() {{ :; }}
print_info() {{ echo "INFO: $1"; }}
print_success() {{ echo "OK: $1"; }}
print_warning() {{ echo "WARN: $1"; }}
print_error() {{ echo "ERR: $1"; }}

RELEASE_FILE_LOG="$(mktemp)"
prepare_verified_source() {{ return 0; }}
read_release_file() {{
    if [[ "$1" == */pyproject.toml ]]; then
        cat "{REPO_ROOT}/$1"
        return
    fi
    printf '%s\n' "$1" >> "$RELEASE_FILE_LOG"
    {read_file_body}
}}

pip() {{
    if [[ "$1" == "show" ]]; then
        [[ ",{",".join(installed_roles)}," == *",$2,"* ]]
        return
    fi
    {pip_body}
}}
python3() {{ return 0; }}

SKIP_VERIFY=false
PINNED_DEPS={pinned}
INSTALL_MAKER=false
INSTALL_TAKER=false

( update_packages )
echo "EXIT:$?"
echo "RELEASE_FILE_LOG_START"
cat "$RELEASE_FILE_LOG"
rm -f "$RELEASE_FILE_LOG"
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _pip_lines(stdout: str) -> list[str]:
    return [
        line[len("PIP: ") :] for line in stdout.splitlines() if line.startswith("PIP: ")
    ]


def _release_file_paths(stdout: str) -> list[str]:
    return stdout.split("RELEASE_FILE_LOG_START\n", maxsplit=1)[1].splitlines()


def test_update_default_hash_checks_dependencies() -> None:
    """By default the update hash-checks deps then installs with --no-deps."""
    result = _run_update_with_pinning(pinned_deps=True, fetch_ok=True)
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "Hash-verified dependencies installed" in result.stdout, result.stdout
    pip_lines = _pip_lines(result.stdout)
    assert any("--require-hashes" in line for line in pip_lines), (
        "default mode must hash-check dependencies:\n" + "\n".join(pip_lines)
    )
    # Package installs that resolve deps must be --no-deps so pip never
    # tries to hash the un-hashable git checkout.
    resolving = [
        line
        for line in pip_lines
        if "subdirectory=jmcore" in line and "--require-hashes" not in line
    ]
    assert resolving, "\n".join(pip_lines)
    assert all("--no-deps" in line for line in resolving), (
        "package installs must use --no-deps in default hash mode:\n"
        + "\n".join(pip_lines)
    )
    assert _release_file_paths(result.stdout) == [
        "jmcore/requirements.txt",
        "jmwallet/requirements.txt",
    ]


@pytest.mark.parametrize("role", ["jm-maker", "jm-taker", "jmwalletd"])
def test_upgrade_old_install_backfills_required_swap_without_new_roles(
    role: str,
) -> None:
    result = _run_update_with_pinning(installed_roles=(role,))
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    paths = _release_file_paths(result.stdout)
    assert "jmswap/requirements.txt" in paths
    assert f"{role.removeprefix('jm-')}/requirements.txt" in paths
    pip_calls = _pip_lines(result.stdout)
    swap_index = next(
        i
        for i, call in enumerate(pip_calls)
        if "--force-reinstall --no-deps" in call and "subdirectory=jmswap" in call
    )
    role_index = next(
        i
        for i, call in enumerate(pip_calls)
        if "--force-reinstall --no-deps" in call
        and f"subdirectory={role.removeprefix('jm-')}" in call
    )
    assert swap_index < role_index
    assert any(
        "subdirectory=jmswap" in call
        and f"subdirectory={role.removeprefix('jm-')}" in call
        for call in pip_calls
    )
    if role != "jmwalletd":
        other_role = "taker" if role == "jm-maker" else "maker"
        assert not any(
            f"subdirectory={component}" in call
            for call in pip_calls
            for component in (other_role, "tumbler", "jmwalletd")
        )


def test_update_no_hash_deps_pins_versions_only() -> None:
    """--no-hash-deps version-pins without --require-hashes."""
    result = _run_update_with_pinning(pinned_deps=False, fetch_ok=True)
    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    pip_lines = _pip_lines(result.stdout)
    resolving = [
        line
        for line in pip_lines
        if "--no-deps" not in line and "subdirectory=jmcore" in line
    ]
    assert resolving, "\n".join(pip_lines)
    assert any(" -c " in line for line in resolving), (
        "expected -c constraints on the dependency-resolving install:\n"
        + "\n".join(pip_lines)
    )
    # --no-hash-deps must NOT use --require-hashes.
    assert not any("--require-hashes" in line for line in pip_lines), (
        "--no-hash-deps must not hash-check:\n" + "\n".join(pip_lines)
    )
    assert _release_file_paths(result.stdout) == [
        "jmcore/requirements.txt",
        "jmwallet/requirements.txt",
    ]


def test_update_aborts_when_hash_check_fails() -> None:
    """Hash checking is a hard requirement: any failure aborts with a hint.

    There is no silent fallback. If ``pip install --require-hashes`` fails
    (e.g. no compatible pre-built wheel for this platform/Python), the
    installer must exit non-zero and point the user at ``--no-hash-deps``.
    """
    result = _run_update_with_pinning(
        pinned_deps=True, fetch_ok=True, hash_install_ok=False
    )
    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "--no-hash-deps" in result.stdout, result.stdout
    assert _release_file_paths(result.stdout) == [
        "jmcore/requirements.txt",
        "jmwallet/requirements.txt",
    ]
    # It must not silently version-pin instead.
    pip_lines = _pip_lines(result.stdout)
    assert not any(" -c " in line for line in pip_lines), (
        "must not fall back to version-pinning:\n" + "\n".join(pip_lines)
    )


def test_update_aborts_when_locks_are_unavailable() -> None:
    """A lock-fetch failure must abort before installing selected packages."""
    result = _run_update_with_pinning(pinned_deps=True, fetch_ok=False)
    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    pip_lines = _pip_lines(result.stdout)
    assert not any(" -c " in line for line in pip_lines), (
        "no constraints should be applied after a lock-fetch failure:\n"
        + "\n".join(pip_lines)
    )
    assert not any("--require-hashes" in line for line in pip_lines), (
        "hash installation should not start with incomplete locks:\n"
        + "\n".join(pip_lines)
    )
    assert not any("subdirectory=" in line for line in pip_lines), (
        "package updates must not start with incomplete locks:\n" + "\n".join(pip_lines)
    )
    assert _release_file_paths(result.stdout) == ["jmcore/requirements.txt"]


@pytest.mark.parametrize(
    ("pinned_deps", "failed_call", "expected_error"),
    [
        (True, 1, "temporary storage for dependency locks"),
        (False, 2, "temporary storage for dependency constraints"),
    ],
)
def test_lock_preparation_aborts_when_temporary_storage_fails(
    tmp_path: Path,
    pinned_deps: bool,
    failed_call: int,
    expected_error: str,
) -> None:
    """Temporary-file failures must not weaken dependency verification."""
    script = f'''\
source "{INSTALL_SH}"
set +e
print_error() {{ echo "ERR: $1"; }}
print_warning() {{ echo "WARN: $1"; }}
pip() {{ [[ "$1" == "show" ]] && return 1; return 0; }}
RELEASE_FILE_LOG="{tmp_path}/release-file-paths"
: > "$RELEASE_FILE_LOG"
prepare_verified_source() {{ return 0; }}
read_release_file() {{
    printf '%s\n' "$1" >> "$RELEASE_FILE_LOG"
    printf 'idna==3.10\n'
}}
REAL_MKTEMP="$(command -v mktemp)"
MKTEMP_COUNTER="{tmp_path}/mktemp-calls"
printf '0\n' > "$MKTEMP_COUNTER"
mktemp() {{
    local calls
    calls=$(<"$MKTEMP_COUNTER")
    calls=$((calls + 1))
    printf '%s\n' "$calls" > "$MKTEMP_COUNTER"
    if [[ "$calls" -eq "{failed_call}" ]]; then
        return 1
    fi
    "$REAL_MKTEMP" "$@"
}}
SKIP_VERIFY=false
PINNED_DEPS="{"true" if pinned_deps else "false"}"
INSTALL_MAKER=false
INSTALL_TAKER=false
INSTALL_TUMBLER=false
INSTALL_ORDERBOOK_WATCHER=true
prepare_dep_pinning "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
echo "EXIT:$?"
echo "RELEASE_FILE_LOG_START"
cat "$RELEASE_FILE_LOG"
cleanup_dep_pinning
'''
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert expected_error in result.stdout
    expected_paths = []
    if failed_call == 2:
        expected_paths = [
            "jmcore/requirements.txt",
            "jmwallet/requirements.txt",
            "orderbook_watcher/requirements.txt",
        ]
    assert _release_file_paths(result.stdout) == expected_paths
