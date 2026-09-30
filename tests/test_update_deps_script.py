from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def updater_tree(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    root = tmp_path / "project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / "scripts/update-deps.sh", scripts / "update-deps.sh")
    for helper in ("update-bitcointx.py", "update-flatpak-deps.py"):
        (scripts / helper).touch()
    (root / "jmcore").mkdir()
    (root / "jmcore/pyproject.toml").write_text('[project]\nname = "jmcore"\n')
    for source in ("requirements-docs.in", "requirements-security.in"):
        shutil.copy2(PROJECT_ROOT / source, root / source)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    for command in ("pip-compile", "python3"):
        stub = bin_dir / command
        stub.write_text(
            "#!/bin/bash\n"
            'printf "%s|%s|%s\\n" "${0##*/}" "$PWD" "$*" >> "$JMNG_TEST_LOG"\n'
            'if [[ "${0##*/}" == "pip-compile" && -n "$JMNG_TEST_FAIL_INPUT" '
            '&& " $* " == *" $JMNG_TEST_FAIL_INPUT "* ]]; then exit 27; fi\n'
        )
        stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "JMNG_PACKAGES": "jmcore",
        "JMNG_TEST_LOG": str(log),
        "JMNG_TEST_FAIL_INPUT": "",
    }
    return root, log, env


@pytest.mark.parametrize(
    ("mode", "expected_outputs", "expected_helpers"),
    [
        (
            [],
            [
                "requirements.txt",
                "requirements-dev.txt",
                "requirements-docs.txt",
                "requirements-security.txt",
            ],
            ["update-bitcointx.py", "update-flatpak-deps.py"],
        ),
        (
            ["--dev-only"],
            [
                "requirements-dev.txt",
                "requirements-docs.txt",
                "requirements-security.txt",
            ],
            [],
        ),
        (
            ["--prod-only"],
            ["requirements.txt"],
            ["update-bitcointx.py", "update-flatpak-deps.py"],
        ),
    ],
)
def test_updater_refreshes_locks_by_mode(
    updater_tree: tuple[Path, Path, dict[str, str]],
    mode: list[str],
    expected_outputs: list[str],
    expected_helpers: list[str],
) -> None:
    root, log, env = updater_tree
    # Invoke outside the project to protect the script's repository-root resolution.
    result = subprocess.run(
        ["bash", str(root / "scripts/update-deps.sh"), *mode],
        cwd=root.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    commands = [line.split("|", 2) for line in log.read_text().splitlines()]
    compiles = [
        (cwd, args.split())
        for command, cwd, args in commands
        if command == "pip-compile"
    ]
    assert [args[-1] for _, args in compiles] == expected_outputs
    for cwd, args in compiles:
        assert args[:3] == ["-U", "--strip-extras", "--generate-hashes"]
        if args[-1] in ("requirements-docs.txt", "requirements-security.txt"):
            assert cwd == str(root)
            assert args[3:] == [args[-1].replace(".txt", ".in"), "-o", args[-1]]
        else:
            assert cwd == str(root / "jmcore")
            assert ("--extra" in args) is (args[-1] == "requirements-dev.txt")
    helpers = [
        Path(args.split()[0]).name
        for command, _, args in commands
        if command == "python3"
    ]
    assert helpers == expected_helpers


def test_security_compile_failure_stops_updater(
    updater_tree: tuple[Path, Path, dict[str, str]],
) -> None:
    root, log, env = updater_tree
    env["JMNG_TEST_FAIL_INPUT"] = "requirements-security.in"
    result = subprocess.run(
        ["bash", str(root / "scripts/update-deps.sh")],
        cwd=root.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 27
    assert "requirements-security.in" in log.read_text()
    assert "update-flatpak-deps.py" not in log.read_text()
    assert "All dependencies updated successfully" not in result.stdout
