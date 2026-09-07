from __future__ import annotations

import difflib
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
TemplateSnapshot = config_changelog.TemplateSnapshot
backfill_changelog = config_changelog.backfill_changelog
generate_config_changes_section = config_changelog.generate_config_changes_section
render_template_diff = config_changelog.render_template_diff
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


def render_diff(from_content: str, to_content: str) -> str:
    return render_template_diff(
        TemplateSnapshot(ref="old", path="config.toml.template", content=from_content),
        TemplateSnapshot(ref="new", path="config.toml.template", content=to_content),
        "old",
        "new",
    )


@pytest.mark.parametrize("change", ["insert", "update", "delete"])
def test_render_template_diff_labels_middle_of_long_section(change: str) -> None:
    from_lines = ["[taker]", *(f"# value_{index} = {index}" for index in range(12))]
    to_lines = from_lines.copy()
    if change == "insert":
        to_lines.insert(8, "# inserted = true")
    elif change == "update":
        to_lines[7] = "# value_6 = 99"
    else:
        del to_lines[7]

    diff = render_diff("\n".join(from_lines) + "\n", "\n".join(to_lines) + "\n")
    diff_lines = diff.splitlines()
    hunk_header = next(line for line in diff_lines if line.startswith("@@"))

    assert hunk_header.endswith("[taker]")
    assert " [taker]" not in (line for line in diff_lines if not line.startswith("@@"))


def test_render_template_diff_only_annotates_unified_hunk_headers() -> None:
    from_content = "[maker]\n# fee = 1\n# unchanged\n[taker]\n# fee = 1\n"
    to_content = "[maker]\n# fee = 2\n# unchanged\n[taker]\n# fee = 2\n"
    expected_diff = list(
        difflib.unified_diff(
            from_content.splitlines(),
            to_content.splitlines(),
            fromfile="config.toml.template (old)",
            tofile="config.toml.template (new)",
            lineterm="",
        )
    )
    rendered_diff = render_diff(from_content, to_content).splitlines()

    assert len(rendered_diff) == len(expected_diff)
    for rendered_line, expected_line in zip(rendered_diff, expected_diff, strict=True):
        if expected_line.startswith("@@"):
            assert rendered_line.startswith(f"{expected_line} ")
        else:
            assert rendered_line == expected_line


def test_render_template_diff_labels_multiple_separate_hunks() -> None:
    from_content = "[maker]\n# fee = 1\n" + "# unchanged\n" * 8 + "[taker]\n# fee = 1\n"
    to_content = from_content.replace(
        "[maker]\n# fee = 1", "[maker]\n# fee = 2"
    ).replace("[taker]\n# fee = 1", "[taker]\n# fee = 2")

    hunk_headers = [
        line
        for line in render_diff(from_content, to_content).splitlines()
        if line.startswith("@@")
    ]

    assert len(hunk_headers) == 2
    assert hunk_headers[0].endswith("[maker]")
    assert hunk_headers[1].endswith("[taker]")


def test_render_template_diff_labels_all_sections_in_one_hunk() -> None:
    from_content = "[maker]\n# fee = 1\n\n[taker]\n# fee = 1\n"
    to_content = "[maker]\n# fee = 2\n\n[taker]\n# fee = 2\n"

    diff = render_diff(from_content, to_content)

    assert "@@ -1,5 +1,5 @@ [maker], [taker]" in diff


@pytest.mark.parametrize(
    ("from_content", "to_content", "expected_context"),
    [
        ("[maker]\n# fee = 1\n", "", "[maker]"),
        ("", "[taker]\n# fee = 1\n", "[taker]"),
        ("[maker]\n# fee = 1\n", "[taker]\n# fee = 1\n", "old: [maker]; new: [taker]"),
    ],
)
def test_render_template_diff_labels_added_deleted_and_renamed_sections(
    from_content: str, to_content: str, expected_context: str
) -> None:
    hunk_header = next(
        line
        for line in render_diff(from_content, to_content).splitlines()
        if line.startswith("@@")
    )

    assert hunk_header.endswith(expected_context)


def test_render_template_diff_labels_preamble_as_top_level() -> None:
    from_content = 'value = "[not_a_section]"\n[wallet]\n# gap_limit = 20\n'
    to_content = (
        'value = "[not_a_section]"\n# added = true\n[wallet]\n# gap_limit = 20\n'
    )

    assert "@@ -1,3 +1,4 @@ top level" in render_diff(from_content, to_content)


def test_render_template_diff_labels_commented_section_placeholders() -> None:
    diff = render_diff("# [wallet]\n# value = 1\n", "# [wallet]\n# value = 2\n")

    assert "@@ -1,2 +1,2 @@ [wallet]" in diff


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


def test_file_diff_cli_works_outside_a_git_repository(tmp_path: Path) -> None:
    before = tmp_path / "before.toml"
    after = tmp_path / "after.toml"
    before.write_text("[taker]\n" + "# explanation\n" * 8 + "# max_cj_fee_abs = 500\n")
    after.write_text(before.read_text().replace("500", "600"))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "config_changelog.py"),
            "--from-file",
            str(before),
            "--to-file",
            str(after),
            "--from-label",
            "1.0.0",
            "--to-label",
            "2.0.0",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "config.toml.template (1.0.0)" in result.stdout
    assert "config.toml.template (2.0.0)" in result.stdout
    assert "@@ [taker]" in result.stdout
    assert "+# max_cj_fee_abs = 600" in result.stdout


def test_file_diff_cli_requires_both_files(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "config_changelog.py"),
            "--from-file",
            str(tmp_path / "old"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "must be used together" in result.stderr


def test_legacy_core_placeholder_is_labeled_top_level() -> None:
    before = config_changelog.TemplateSnapshot(
        "old", "template", '# [core]\n# data_dir = "old"\n'
    )
    after = config_changelog.TemplateSnapshot("new", "template", '# data_dir = "new"\n')
    diff = config_changelog.render_template_diff(before, after, "old", "new")
    assert next(line for line in diff.splitlines() if line.startswith("@@")).endswith(
        "top level"
    )
