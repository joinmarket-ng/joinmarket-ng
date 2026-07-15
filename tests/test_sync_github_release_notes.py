from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

sync_release_notes = importlib.import_module("sync_github_release_notes")

extract_release_notes = sync_release_notes.extract_release_notes


def test_extract_release_notes_matches_release_workflow_sections() -> None:
    changelog = """# Changelog

## [Unreleased]

## [1.1.0] - 2026-01-02

### Added

- New option.

### Configuration Changes

````diff
+# new_option = true
````

## [1.0.0] - 2026-01-01

Initial release.

[Unreleased]: ../../compare/1.1.0...HEAD
[1.1.0]: ../../compare/1.0.0...1.1.0
[1.0.0]: ../../releases/tag/1.0.0
"""

    notes = extract_release_notes(changelog)

    assert (
        notes["1.1.0"]
        == """### Added

- New option.

### Configuration Changes

````diff
+# new_option = true
````"""
    )
    assert notes["1.0.0"] == "Initial release."


def test_main_previews_all_published_releases_without_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("# Changelog\n\n## [1.0.0] - 2026-01-01\n\nInitial release.\n")
    updates: list[tuple[str, str, str]] = []
    monkeypatch.setattr(sync_release_notes, "CHANGELOG", changelog)
    monkeypatch.setattr(
        sync_release_notes, "list_release_tags", lambda repository, limit: ["1.0.0"]
    )
    monkeypatch.setattr(
        sync_release_notes,
        "update_release",
        lambda repository, tag, notes: updates.append((repository, tag, notes)),
    )
    monkeypatch.setattr(sys, "argv", ["sync_github_release_notes.py"])

    sync_release_notes.main()

    assert updates == []
    assert "Would update 1 release(s)" in capsys.readouterr().out


def test_main_apply_updates_only_requested_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n"
        "## [1.1.0] - 2026-01-02\n\nSecond release.\n\n"
        "## [1.0.0] - 2026-01-01\n\nInitial release.\n"
    )
    updates: list[tuple[str, str, str]] = []
    monkeypatch.setattr(sync_release_notes, "CHANGELOG", changelog)
    monkeypatch.setattr(
        sync_release_notes,
        "list_release_tags",
        lambda repository, limit: ["1.1.0", "1.0.0"],
    )
    monkeypatch.setattr(
        sync_release_notes,
        "update_release",
        lambda repository, tag, notes: updates.append((repository, tag, notes)),
    )
    monkeypatch.setattr(
        sys, "argv", ["sync_github_release_notes.py", "1.0.0", "--apply"]
    )

    sync_release_notes.main()

    assert updates == [("joinmarket-ng/joinmarket-ng", "1.0.0", "Initial release.")]
