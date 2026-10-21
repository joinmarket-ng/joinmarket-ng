"""Optional setup runs after software success, never from installer auto-confirm."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"
pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("bash") is None,
    reason="POSIX terminals and bash required",
)

STUBS = r"""
check_system_dependencies() { :; }
prepare_release() { :; }
setup_tor() { :; }
check_python_version() { :; }
ask_components() { :; }
setup_virtualenv() { mkdir -p "$VENV_DIR/bin"; }
install_packages() {
    cp "$TEST_PAYLOAD/python" "$VENV_DIR/bin/python"
    cp "$TEST_PAYLOAD/jm-ng" "$VENV_DIR/bin/jm-ng"
}
setup_data_directory() { mkdir -p "$DATA_DIR"; printf untouched > "$DATA_DIR/config.toml"; }
create_shell_integration() { :; }
save_trusted_installer() { printf 'TRUSTED_SAVED\n'; }
print_completion() { printf 'INSTALL_COMPLETE\n'; }
cleanup_install() { :; }
update_packages() { :; }
snapshot_installed_config_template() { :; }
migrate_config() { :; }
report_config_template_changes() { :; }
"""


def fixture_env(tmp_path: Path, *, child_status: str = "0") -> dict[str, str]:
    payload = tmp_path / "payload"
    payload.mkdir()
    python = payload / "python"
    python.write_text(
        f"#!{sys.executable}\n"
        + """
import os, signal, sys
if os.environ.get('TEST_CAPABILITY_WAIT') == '1':
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    with open('/dev/tty', 'w') as terminal:
        terminal.write('CAPABILITY_READY\\n'); terminal.flush()
    while True: signal.pause()
sys.exit(int(os.environ.get('TEST_CAPABILITY_STATUS', '0')))
"""
    )
    wizard = payload / "jm-ng"
    wizard.write_text(
        f"#!{sys.executable}\n"
        + """
import json, os, signal, sys
from pathlib import Path
Path(os.environ['TEST_CALLS']).write_text(json.dumps({
    'args': sys.argv[1:], 'deferral': os.environ.get('JMNG_SETUP_DEFER_TO_PARENT')}))
if os.environ['TEST_CHILD_STATUS'] == 'interrupt':
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
print('WIZARD_READY', flush=True)
if os.environ['TEST_CHILD_STATUS'] == 'interrupt':
    while True: signal.pause()
sys.exit(int(os.environ['TEST_CHILD_STATUS']))
"""
    )
    python.chmod(0o700)
    wizard.chmod(0o700)
    return {
        **os.environ,
        "JOINMARKET_DATA_DIR": str(tmp_path / "data with spaces"),
        "JMNG_VENV_DIR": str(tmp_path / "venv with spaces"),
        "TEST_PAYLOAD": str(payload),
        "TEST_CALLS": str(tmp_path / "calls.json"),
        "TEST_CHILD_STATUS": child_status,
        "TEST_INSTALL_SOURCE": str(INSTALL_SH),
        "JMNG_SETUP_DEFER_TO_PARENT": "",
    }


def script(*args: str, wrapper: bool = False) -> str:
    auth = ""
    if wrapper:
        auth = r"""
get_latest_version() { printf 'v1.2.3\n'; }
curl() {
    local destination
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == '-o' ]]; then destination="$2"; shift 2; else shift; fi
    done
    cp "$TEST_CANDIDATE" "$destination"
}
import_verification_keys() { :; }
verify_detached_signature() { :; }
"""
    return f"source {shlex.quote(str(INSTALL_SH))}\n{STUBS}\n{auth}\nmain {shlex.join(args)}\n"


def run_pipe(body: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", body],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def run_terminal(
    body: str,
    env: dict[str, str],
    *,
    inputs: bytes = b"",
    interrupt_at: str | None = None,
) -> tuple[int, str]:
    import fcntl
    import pty
    import termios

    master, slave = pty.openpty()

    def own_terminal() -> None:
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.tcsetpgrp(0, os.getpgrp())

    child = subprocess.Popen(
        ["bash", "-c", body],
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        preexec_fn=own_terminal,
    )
    os.close(slave)
    output = b""
    deadline = time.monotonic() + 15
    sent = False
    if inputs:
        os.write(master, inputs)
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output += chunk
                if interrupt_at and not sent and interrupt_at.encode() in output:
                    os.write(master, b"\x03")
                    sent = True
            elif child.poll() is not None:
                break
        status = child.wait(timeout=1)
        if interrupt_at:
            assert sent, output.decode()
        return status, output.decode()
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        os.close(master)


@pytest.mark.parametrize("flags", [("--yes",), ("--run-setup",), ("--skip-setup",)])
def test_piped_install_never_runs_setup(tmp_path: Path, flags: tuple[str, ...]) -> None:
    env = fixture_env(tmp_path)
    result = run_pipe(script("--skip-verify", "--skip-tor", *flags), env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not Path(env["TEST_CALLS"]).exists()
    assert Path(env["JOINMARKET_DATA_DIR"], "config.toml").read_text() == "untouched"
    if "--skip-setup" not in flags:
        assert "setup not run" in result.stderr and "Run separately:" in result.stdout


@pytest.mark.parametrize("answer", [b"\n", b"n\n", b"y\n"])
def test_interactive_offer_defaults_no_and_runs_only_after_completion(
    tmp_path: Path,
    answer: bytes,
) -> None:
    env = fixture_env(tmp_path)
    status, output = run_terminal(
        script("--skip-verify", "--skip-tor"), env, inputs=answer
    )
    assert status == 0, output
    calls = Path(env["TEST_CALLS"])
    assert calls.exists() is (answer == b"y\n")
    assert (
        output.index("TRUSTED_SAVED")
        < output.index("INSTALL_COMPLETE")
        < output.index("Run guided")
    )
    if calls.exists():
        assert output.index("INSTALL_COMPLETE") < output.index("WIZARD_READY")
        assert json.loads(calls.read_text()) == {
            "args": [
                "setup",
                "--data-dir",
                env["JOINMARKET_DATA_DIR"],
                "--config-file",
                env["JOINMARKET_DATA_DIR"] + "/config.toml",
            ],
            "deferral": None,
        }


@pytest.mark.parametrize("kind", ["directory", "file", "dangling", "update"])
def test_preexisting_venv_entry_never_becomes_fresh(tmp_path: Path, kind: str) -> None:
    env = fixture_env(tmp_path)
    path = Path(env["JMNG_VENV_DIR"])
    if kind == "directory" or kind == "update":
        path.mkdir()
    elif kind == "file":
        path.write_text("preserved")
    else:
        path.symlink_to(tmp_path / "missing")
    # Replace only software stubs that require a new directory. No deletion.
    body = script(
        "--skip-verify",
        "--skip-tor",
        "--run-setup",
        *(["--update"] if kind == "update" else []),
    )
    body = body.replace(
        'setup_virtualenv() { mkdir -p "$VENV_DIR/bin"; }', "setup_virtualenv() { :; }"
    )
    body = body.replace("install_packages() {", "install_packages() { return 0;")
    status, output = run_terminal(body, env, inputs=b"n\n")
    assert status == 0, output
    assert not Path(env["TEST_CALLS"]).exists()
    if kind != "update":
        assert "setup not run on updates or reused paths" in output
    if kind == "file":
        assert path.read_text() == "preserved"
    if kind == "dangling":
        assert path.is_symlink() and not path.exists()


@pytest.mark.parametrize("wrapper", [False, True])
@pytest.mark.parametrize("outcome", ["0", "1", "interrupt"])
def test_optional_failure_and_foreground_ctrl_c_preserve_completed_install(
    tmp_path: Path,
    wrapper: bool,
    outcome: str,
) -> None:
    env = fixture_env(tmp_path, child_status=outcome)
    if wrapper:
        candidate = tmp_path / "authenticated.sh"
        candidate.write_text(
            'DEFAULT_VERSION="1.2.3"\nINSTALLER_PROTOCOL=1\n'
            'source "$TEST_INSTALL_SOURCE"\nINSTALLER_SOURCE="$0"\n'
            + STUBS
            + '\nmain "$@"\n'
        )
        env["TEST_CANDIDATE"] = str(candidate)
    flags = ["--run-setup", "--skip-tor"] + ([] if wrapper else ["--skip-verify"])
    status, output = run_terminal(
        script(*flags, wrapper=wrapper),
        env,
        interrupt_at="WIZARD_READY" if outcome == "interrupt" else None,
    )
    assert status == 0, output
    assert output.count("WIZARD_READY") == 1
    assert Path(env["JOINMARKET_DATA_DIR"], "config.toml").read_text() == "untouched"
    assert "--yes" not in json.loads(Path(env["TEST_CALLS"]).read_text())["args"]
    if outcome != "0":
        assert "optional setup incomplete" in output
    if outcome == "interrupt":
        assert "status 130" in output


@pytest.mark.parametrize(
    "prior", ["", "trap 'exit 130' INT", "trap 'printf prior-handler' INT"]
)
def test_cancellation_at_offer_restores_exact_previous_trap(
    tmp_path: Path, prior: str
) -> None:
    env = fixture_env(tmp_path)
    body = f"""source {shlex.quote(str(INSTALL_SH))}
MODE=install; AUTO_YES=false; SETUP_ACTION=offer; VENV_PRESENT_BEFORE_INSTALL=false
{prior}
before=$(trap -p INT)
offer_guided_setup
after=$(trap -p INT)
[[ "$before" == "$after" ]]
printf 'TRAP_RESTORED\\n'
"""
    status, output = run_terminal(body, env, interrupt_at="Run guided wallet setup")
    assert status == 0 and "TRAP_RESTORED" in output
    assert not Path(env["TEST_CALLS"]).exists()


def test_missing_capability_never_invokes_older_launcher(tmp_path: Path) -> None:
    env = fixture_env(tmp_path)
    env["TEST_CAPABILITY_STATUS"] = "1"
    status, output = run_terminal(
        script("--skip-verify", "--skip-tor", "--run-setup"), env
    )
    assert status == 0 and "lacks guided setup support" in output
    assert not Path(env["TEST_CALLS"]).exists()


def test_capability_probe_cancellation_never_launches_wizard_even_if_probe_returns_zero(
    tmp_path: Path,
) -> None:
    env = fixture_env(tmp_path)
    env["TEST_CAPABILITY_WAIT"] = "1"
    status, output = run_terminal(
        script("--skip-verify", "--skip-tor", "--run-setup"),
        env,
        interrupt_at="CAPABILITY_READY",
    )
    assert status == 0 and "optional setup canceled" in output
    assert not Path(env["TEST_CALLS"]).exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_auto_yes_requires_separate_interactive_setup_choice_and_ignores_hostile_path(
    tmp_path: Path,
    explicit: bool,
) -> None:
    env = fixture_env(tmp_path)
    hostile = tmp_path / "hostile-path"
    hostile.mkdir()
    executable = hostile / "jm-ng"
    executable.write_text("#!/bin/bash\nprintf HOSTILE_ENTRYPOINT; exit 71\n")
    executable.chmod(0o700)
    env["PATH"] = f"{hostile}:{env['PATH']}"
    status, output = run_terminal(
        script(
            "--skip-verify",
            "--skip-tor",
            "--yes",
            *(["--run-setup"] if explicit else []),
        ),
        env,
    )
    assert status == 0 and "HOSTILE_ENTRYPOINT" not in output
    assert Path(env["TEST_CALLS"]).exists() is explicit


def test_conflicting_setup_flags_fail_before_installation(tmp_path: Path) -> None:
    env = fixture_env(tmp_path)
    result = run_pipe(script("--run-setup", "--skip-setup"), env)
    assert result.returncode != 0 and "not both" in result.stdout
    assert not Path(env["JMNG_VENV_DIR"]).exists()


def test_failed_authentication_does_not_run_optional_phase(tmp_path: Path) -> None:
    env = fixture_env(tmp_path)
    body = script("--run-setup", wrapper=True).replace(
        "main --run-setup",
        "refresh_installer() { false; printf FAILED_WORK_CONTINUED; }\nmain --run-setup",
    )
    result = run_pipe(body, env)
    assert result.returncode != 0
    assert "FAILED_WORK_CONTINUED" not in result.stdout
    assert not Path(env["TEST_CALLS"]).exists()
