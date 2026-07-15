from __future__ import annotations

import sys
from pathlib import Path
import importlib

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

changelog_commit_utils = importlib.import_module("changelog_commit_utils")
generate_changelog = importlib.import_module("generate_changelog")
validate_commit_message = importlib.import_module("validate_commit_message")

parse_changelog_trailers = changelog_commit_utils.parse_changelog_trailers
Commit = generate_changelog.Commit
generate_changelog_section = generate_changelog.generate_changelog_section
update_changelog = generate_changelog.update_changelog
validate_message = validate_commit_message.validate_message


def test_validate_message_requires_changelog_trailer_for_feat() -> None:
    message = "feat(wallet): improve address selection\n\nSome context without trailer"
    result = validate_message(message, source_label="test")

    assert not result.ok
    assert any(
        "must include at least one 'Changelog: ...' trailer" in err
        for err in result.errors
    )


def test_validate_message_accepts_feat_with_changelog_trailer() -> None:
    message = "feat(wallet): improve address selection\n\nChangelog: Add smarter address selection"
    result = validate_message(message, source_label="test")

    assert result.ok
    assert result.errors == []


def test_validate_message_does_not_require_trailer_for_docs() -> None:
    message = "docs: update installation docs\n\nNo changelog trailer required"
    result = validate_message(message, source_label="test")

    assert result.ok


def test_parse_changelog_trailers_supports_continuation_lines() -> None:
    body = (
        "Implementation details\n\n"
        "Changelog: Add release-time changelog generation\n"
        "  from feat/fix commit trailers\n"
        "Changelog: Enforce trailer for feat/fix commits\n"
    )

    parsed = parse_changelog_trailers(body)

    assert parsed == [
        "Add release-time changelog generation from feat/fix commit trailers",
        "Enforce trailer for feat/fix commits",
    ]


def test_generate_changelog_section_uses_trailers() -> None:
    commits = [
        Commit(
            hash="aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111",
            type="feat",
            scope="wallet",
            description="add x",
            is_breaking=False,
            changelog_entries=["Add automated changelog generation"],
        ),
        Commit(
            hash="bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
            type="fix",
            scope="maker",
            description="fix y",
            is_breaking=False,
            changelog_entries=["Fix changelog merge conflict workflow"],
        ),
    ]

    section, errors = generate_changelog_section(commits)

    assert errors == []
    assert "### Added" in section
    assert "### Fixed" in section
    assert (
        "- Add automated changelog generation "
        "([aaaa1111](../../commit/aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111))" in section
    )
    assert (
        "- Fix changelog merge conflict workflow "
        "([bbbb2222](../../commit/bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222))" in section
    )


def test_update_changelog_replaces_generated_config_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        """# Changelog

## [Unreleased]

### Configuration Changes

Old configuration notes.

## [1.0.0] - 2026-01-01

- Initial release.
"""
    )
    monkeypatch.setattr(generate_changelog, "CHANGELOG", changelog)

    update_changelog(
        """### Added

- New feature.

### Configuration Changes

New configuration notes."""
    )

    content = changelog.read_text()
    assert content.count("### Configuration Changes") == 1
    assert "Old configuration notes." not in content
    assert "New configuration notes." in content
    assert "- New feature." in content
