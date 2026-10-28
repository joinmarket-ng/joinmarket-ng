"""Exercise diagnostic retention through the parallel runner's public CLI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

STUB_COMMAND = r"""
import json
import os
import subprocess
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["JM_TEST_ROOT"])
stack = root / "stack-running"
with Path(os.environ["JM_TEST_EVENTS"]).open("a") as events:
    events.write(json.dumps({"command": name, "args": args}) + "\n")

if name in ("nc", "pip"):
    sys.exit(0)
if name == "curl":
    print('{"offers": [{}, {}]}')
    sys.exit(0)
if name == "pytest":
    if os.environ.get("JM_TEST_RETRY_PROBE") == "1" and "--reruns=1" in args:
        command = [
            sys.executable, "-m", "pytest", "-o", "addopts=",
            "--confcutdir=" + str(root), "--reruns=1", "--reruns-delay=0",
            "--tb=short", str(root / "test_retry_probe.py"),
        ]
        if "--rerun-show-tracebacks" in args:
            command.append("--rerun-show-tracebacks")
        sys.exit(subprocess.call(command))
    sys.exit(int(os.environ.get("JM_TEST_PYTEST_EXIT", "0")))
if name == "docker":
    if args == ["compose", "config", "--environment"]:
        sys.exit(0)
    if args[0] == "inspect":
        print("exited 0")
        sys.exit(0)
    if args[0] == "compose":
        if "down" in args:
            stack.unlink(missing_ok=True)
        elif "up" in args:
            stack.touch()
        elif "exec" in args:
            pass
        elif "ps" in args:
            print("funder-id" if "-aq" in args else "running")
        elif "logs" in args:
            assert stack.exists(), "diagnostics requested after teardown"
            service = args[-1]
            if service == os.environ.get("JM_TEST_LOG_FAILURE"):
                sys.exit(29)
            print("daemon-evidence:" + service)
        else:
            raise AssertionError("unexpected Docker arguments: " + repr(args))
        sys.exit(0)
raise AssertionError("unexpected command: " + name + " " + repr(args))
"""


@pytest.fixture
def runner_tree(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "project"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts/run_parallel_tests.sh", root / "scripts")
    (root / "joinmarket-clientserver/src/jmclient").mkdir(parents=True)
    (root / "test_retry_probe.py").write_text(
        "from pathlib import Path\n"
        "def test_retry_probe():\n"
        "    counter = Path(__file__).with_suffix('.attempts')\n"
        "    attempts = int(counter.read_text()) if counter.exists() else 0\n"
        "    counter.write_text(str(attempts + 1))\n"
        "    assert attempts > 0, 'original-attempt-evidence'\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("docker", "curl", "nc", "pip", "pytest"):
        stub = bin_dir / name
        stub.write_text(f"#!{sys.executable}\n{STUB_COMMAND}")
        stub.chmod(0o755)
    events = tmp_path / "events.jsonl"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "JM_TEST_ROOT": str(root),
        "JM_TEST_EVENTS": str(events),
        "JM_TEST_INSTANCE": "0",
        "JM_SHARED_IMAGE_PROJECT": "diagnostic-test",
        "MAX_CONCURRENT": "0",
        "PYTEST_ADDOPTS": "",
    }
    return root, env, events


@pytest.mark.parametrize(
    ("pytest_exit", "log_failure"), [(0, ""), (7, ""), (7, "maker1")]
)
def test_tumbler_retains_diagnostics_before_teardown(
    runner_tree: tuple[Path, dict[str, str], Path], pytest_exit: int, log_failure: str
) -> None:
    root, env, events_path = runner_tree
    result = subprocess.run(
        [
            "bash",
            str(root / "scripts/run_parallel_tests.sh"),
            "--no-build",
            "--suite",
            "tumbler",
        ],
        env={
            **env,
            "JM_TEST_PYTEST_EXIT": str(pytest_exit),
            "JM_TEST_LOG_FAILURE": log_failure,
        },
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == pytest_exit, result.stdout + result.stderr
    log = (root / "tmp/parallel-tests/i0/tumbler.log").read_text()
    assert "daemon-evidence:jmwalletd" in log
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    startup = next(i for i, event in enumerate(events) if "up" in event["args"])
    pytest_call = next(
        i for i, event in enumerate(events) if event["command"] == "pytest"
    )
    diagnostics = next(
        i
        for i, event in enumerate(events)
        if "logs" in event["args"] and "jmwalletd" in event["args"]
    )
    teardown = next(
        i for i, event in enumerate(events) if i > startup and "down" in event["args"]
    )
    assert startup < pytest_call < diagnostics < teardown


def test_e2e_retains_retried_failure_traceback_and_ci_matches(
    runner_tree: tuple[Path, dict[str, str], Path],
) -> None:
    root, env, _ = runner_tree
    result = subprocess.run(
        [
            "bash",
            str(root / "scripts/run_parallel_tests.sh"),
            "--no-build",
            "--suite",
            "e2e",
        ],
        env={**env, "JM_TEST_RETRY_PROBE": "1"},
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    log = (root / "tmp/parallel-tests/i0/e2e.log").read_text()
    assert "1 passed, 1 rerun" in log
    assert "original-attempt-evidence" in log

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yaml").read_text())
    ci_command = next(
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Run e2e tests (our implementation only)"
    )
    assert "--rerun-show-tracebacks" in shlex.split(ci_command)
