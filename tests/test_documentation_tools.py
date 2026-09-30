from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

update_readme_help = importlib.import_module("update_readme_help")
build_docs = importlib.import_module("build_docs")


def test_local_build_matches_strict_ci_build(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(build_docs, "_run", commands.append)

    build_docs.main()

    assert commands[0] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "-r",
        "requirements-docs.txt",
    ]
    assert commands[1][:4] == [sys.executable, "-m", "pip", "install"]
    assert commands[1][4::2] == ["-e"] * 8
    assert [Path(path).name for path in commands[1][5::2]] == [
        "jmcore",
        "jmwallet",
        "taker",
        "maker",
        "directory_server",
        "orderbook_watcher",
        "jmwalletd",
        "tumbler",
    ]
    assert commands[2] == [
        sys.executable,
        "-m",
        "properdocs",
        "build",
        "--strict",
        "-f",
        "properdocs.yml",
    ]


def test_help_generation_only_targets_component_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets: list[tuple[Path, str]] = []

    def record_target(path: Path, command: str) -> bool:
        targets.append((path.relative_to(SCRIPTS_DIR.parent), command))
        return True

    monkeypatch.setattr(
        update_readme_help, "get_command_help", lambda command: "Usage: command"
    )
    monkeypatch.setattr(update_readme_help, "update_readme_help", record_target)

    assert update_readme_help.main() == 1
    assert targets == [
        (Path("jmwallet/README.md"), "jm-wallet"),
        (Path("maker/README.md"), "jm-maker"),
        (Path("taker/README.md"), "jm-taker"),
        (Path("directory_server/README.md"), "jm-directory-ctl"),
        (Path("tumbler/README.md"), "jm-tumbler"),
    ]


def test_help_update_preserves_prose_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = tmp_path / "README.md"
    reference.write_text(
        "# Wallet reference\n\nSee the user guide.\n\n"
        "<!-- AUTO-GENERATED HELP START: jm-wallet -->\nold help\n"
        "<!-- AUTO-GENERATED HELP END: jm-wallet -->\n\nAfter the help.\n"
    )
    monkeypatch.setattr(
        update_readme_help, "generate_all_help_sections", lambda command: "new help"
    )

    assert update_readme_help.update_readme_help(reference, "jm-wallet") is True
    result = reference.read_text()
    assert result.startswith("# Wallet reference\n\nSee the user guide.\n")
    assert result.endswith("\n\nAfter the help.\n")
    assert "old help" not in result
    assert "new help" in result
    assert update_readme_help.update_readme_help(reference, "jm-wallet") is False


@pytest.mark.parametrize("modified", [False, True])
def test_help_maintenance_mode_succeeds_after_updates(
    modified: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        update_readme_help, "get_command_help", lambda command: "Usage: command"
    )
    monkeypatch.setattr(
        update_readme_help, "update_readme_help", lambda path, command: modified
    )

    assert update_readme_help.main(exit_zero_on_changes=True) == 0


def test_help_maintenance_mode_fails_when_command_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(update_readme_help, "get_command_help", lambda command: "")
    monkeypatch.setattr(
        update_readme_help,
        "update_readme_help",
        lambda path, command: pytest.fail(
            "Unavailable commands must not update documentation"
        ),
    )

    assert update_readme_help.main(exit_zero_on_changes=True) == 1


def test_failed_help_command_is_not_used_as_documentation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    command = [sys.executable, "-c", "print('not help'); raise SystemExit(2)"]

    assert update_readme_help.get_command_help(command) == ""
    assert "Failed to run" in capsys.readouterr().err


@pytest.mark.parametrize(
    "failure", ["generation", "discovery", "subcommand", "missing_readme"]
)
@pytest.mark.parametrize("maintenance_mode", [False, True])
def test_generation_failures_preserve_documentation_and_report_maintenance_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    maintenance_mode: bool,
) -> None:
    readme = tmp_path / "jmwallet/README.md"
    readme.parent.mkdir()
    original = (
        "# Wallet\n<!-- AUTO-GENERATED HELP START: jm-wallet -->\n"
        "complete existing help\n<!-- AUTO-GENERATED HELP END: jm-wallet -->\n"
    )
    if failure != "missing_readme":
        readme.write_text(original)
    monkeypatch.setattr(
        update_readme_help, "__file__", str(tmp_path / "scripts/help.py")
    )
    main_calls = 0

    def command_help(command: list[str]) -> str:
        nonlocal main_calls
        if command[0] != "jm-wallet":
            return "Usage: command"
        if len(command) == 3:
            return "" if failure == "subcommand" else "Usage: status"
        main_calls += 1
        if (failure == "generation" and main_calls == 2) or (
            failure == "discovery" and main_calls == 3
        ):
            return ""
        return "Usage: command\nCommands:\n  status  Show status\n"

    real_update = update_readme_help.update_readme_help

    def update_wallet(path: Path, command: str) -> bool:
        return real_update(path, command) if command == "jm-wallet" else False

    monkeypatch.setattr(update_readme_help, "get_command_help", command_help)
    monkeypatch.setattr(update_readme_help, "update_readme_help", update_wallet)

    assert update_readme_help.main(exit_zero_on_changes=maintenance_mode) == (
        1 if maintenance_mode else 0
    )
    if failure == "missing_readme":
        assert not readme.exists()
    else:
        assert readme.read_text() == original


@pytest.mark.parametrize("maintenance_mode", [False, True])
def test_help_cli_exit_status_and_idempotence(
    tmp_path: Path, maintenance_mode: bool
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "update_readme_help.py"
    shutil.copy2(SCRIPTS_DIR / script.name, script)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for component, command in (
        ("jmwallet", "jm-wallet"),
        ("maker", "jm-maker"),
        ("taker", "jm-taker"),
        ("directory_server", "jm-directory-ctl"),
        ("tumbler", "jm-tumbler"),
    ):
        directory = tmp_path / component
        directory.mkdir()
        (directory / "README.md").write_text(f"# {component}\n")
        stub = bin_dir / command
        stub.write_text('#!/bin/sh\nprintf "Usage: command\\n"\n')
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    args = [sys.executable, str(script)]
    if maintenance_mode:
        args.append("--exit-zero-on-changes")
    result = subprocess.run(
        args, env=env, capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == (0 if maintenance_mode else 1), result.stderr
    assert (
        "AUTO-GENERATED HELP START: jm-wallet"
        in (tmp_path / "jmwallet/README.md").read_text()
    )
    result = subprocess.run(
        args, env=env, capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr
