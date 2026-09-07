from __future__ import annotations

import importlib
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
