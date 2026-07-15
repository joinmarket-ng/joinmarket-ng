from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

config_changelog = importlib.import_module("config_changelog")
bump_version = importlib.import_module("bump_version")

ConfigChangelogError = config_changelog.ConfigChangelogError
backfill_changelog = config_changelog.backfill_changelog
generate_config_changes_section = config_changelog.generate_config_changes_section
replace_config_section = config_changelog.replace_config_section


def run_git(repo_dir: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


def commit_template(repo_dir: Path, path: str, content: str, tag: str) -> None:
    template = repo_dir / path
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(content)
    run_git(repo_dir, "add", path)
    run_git(repo_dir, "commit", "-m", f"test: template for {tag}")
    run_git(repo_dir, "tag", tag)


def make_template_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    run_git(repo_dir, "init")
    return repo_dir


def test_generate_config_changes_section_includes_comments_and_values(
    tmp_path: Path,
) -> None:
    repo_dir = make_template_repo(tmp_path)
    path = "jmcore/src/jmcore/data/config.toml.template"
    commit_template(repo_dir, path, "[maker]\n# fee = 100\n", "1.0.0")
    commit_template(
        repo_dir,
        path,
        "[maker]\n# Fee in satoshis\n# fee = 200\n# dual_offers = false\n",
        "1.1.0",
    )

    section = generate_config_changes_section("1.0.0", "1.1.0", project_root=repo_dir)

    assert section.startswith("### Configuration Changes")
    assert "Existing `config.toml` files are not updated automatically" in section
    assert "--- config.toml.template (1.0.0)" in section
    assert "+++ config.toml.template (1.1.0)" in section
    assert "-# fee = 100" in section
    assert "+# Fee in satoshis" in section
    assert "+# dual_offers = false" in section
    assert not any(line == " " for line in section.splitlines())


def test_generate_config_changes_section_supports_historical_path_move(
    tmp_path: Path,
) -> None:
    repo_dir = make_template_repo(tmp_path)
    commit_template(repo_dir, "config.toml.template", "[core]\n# value = 1\n", "1.0.0")
    old_path = repo_dir / "config.toml.template"
    new_path = repo_dir / "jmcore/src/jmcore/data/config.toml.template"
    new_path.parent.mkdir(parents=True)
    old_path.rename(new_path)
    new_path.write_text("[core]\n# value = 2\n")
    run_git(repo_dir, "add", "--all")
    run_git(repo_dir, "commit", "-m", "refactor: package template")
    run_git(repo_dir, "tag", "1.1.0")

    section = generate_config_changes_section("1.0.0", "1.1.0", project_root=repo_dir)

    assert "-# value = 1" in section
    assert "+# value = 2" in section


def test_generate_config_changes_section_reports_unchanged_template(
    tmp_path: Path,
) -> None:
    repo_dir = make_template_repo(tmp_path)
    commit_template(repo_dir, "config.toml.template", "[core]\n", "1.0.0")
    (repo_dir / "README.md").write_text("new release\n")
    run_git(repo_dir, "add", "README.md")
    run_git(repo_dir, "commit", "-m", "docs: update readme")
    run_git(repo_dir, "tag", "1.0.1")

    section = generate_config_changes_section("1.0.0", "1.0.1", project_root=repo_dir)

    assert section == (
        "### Configuration Changes\n\n"
        "This release did not change the bundled `config.toml.template`."
    )


def test_generate_config_changes_section_rejects_missing_ref(tmp_path: Path) -> None:
    repo_dir = make_template_repo(tmp_path)
    commit_template(repo_dir, "config.toml.template", "[core]\n", "1.0.0")

    with pytest.raises(ConfigChangelogError, match="Fetch tags and full history"):
        generate_config_changes_section("0.9.0", "1.0.0", project_root=repo_dir)


def test_replace_config_section_handles_empty_release_notes() -> None:
    config_section = "### Configuration Changes\n\nNo changes."

    assert replace_config_section("", config_section) == f"{config_section}\n\n"


def test_backfill_changelog_is_idempotent(tmp_path: Path) -> None:
    repo_dir = make_template_repo(tmp_path)
    commit_template(repo_dir, "config.toml.template", "[core]\n", "1.0.0")
    commit_template(repo_dir, "config.toml.template", "[core]\n# value = 2\n", "1.1.0")
    changelog = """# Changelog

## [Unreleased]

## [1.1.0] - 2026-01-02

### Added

- Add value.

## [1.0.0] - 2026-01-01

### Added

- Initial release.

[Unreleased]: ../../compare/1.1.0...HEAD
[1.1.0]: ../../compare/1.0.0...1.1.0
[1.0.0]: ../../releases/tag/1.0.0
"""

    updated, versions = backfill_changelog(changelog, project_root=repo_dir)
    updated_again, repeated_versions = backfill_changelog(
        updated, project_root=repo_dir
    )

    assert versions == ["1.1.0"]
    assert repeated_versions == ["1.1.0"]
    assert updated_again == updated
    assert updated.count("### Configuration Changes") == 1
    assert "+# value = 2" in updated


def test_version_bump_dry_run_executes_config_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bool]] = []

    def fake_run_command(
        cmd: list[str], dry_run: bool = False, check: bool = True
    ) -> None:
        del check
        calls.append((cmd, dry_run))

    monkeypatch.setattr(bump_version, "run_command", fake_run_command)

    bump_version.generate_changelog_entries("1.0.0", "1.1.0", dry_run=True)

    assert calls == [
        (
            [
                "python",
                "scripts/generate_changelog.py",
                "--since",
                "1.0.0",
                "--config-to-label",
                "1.1.0",
                "--preview",
            ],
            False,
        )
    ]
