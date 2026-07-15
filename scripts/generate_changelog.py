#!/usr/bin/env python3
"""Generate changelog entries from conventional commits.

This script parses git commits and extracts changelog text from commit trailers.

Usage:
    python scripts/generate_changelog.py                       # Since last tag
    python scripts/generate_changelog.py --since 0.11.0        # Since specific tag
    python scripts/generate_changelog.py --preview             # Preview without modifying
    python scripts/generate_changelog.py --update              # Update CHANGELOG.md
    python scripts/generate_changelog.py --allow-missing-trailers

Rules:
    - Only feat/fix commits are included in generated changelog entries
    - Changelog text is taken from one or more "Changelog: ..." commit trailers
    - Commit order is chronological (oldest first)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from changelog_commit_utils import ParsedCommit, parse_commit
from config_changelog import (
    CONFIG_HEADING,
    ConfigChangelogError,
    generate_config_changes_section,
    replace_config_section,
)

PROJECT_ROOT = Path(__file__).parent.parent
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"

TYPE_TO_CATEGORY = {
    "feat": "Added",
    "fix": "Fixed",
}


@dataclass
class Commit:
    hash: str  # full 40-char commit hash
    type: str
    scope: str | None
    description: str
    is_breaking: bool
    changelog_entries: list[str]

    @property
    def short_hash(self) -> str:
        return self.hash[:8]


def run_git_command(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=True,
    )
    return result.stdout.strip()


def get_latest_tag() -> str | None:
    try:
        return run_git_command(["describe", "--tags", "--abbrev=0"])
    except subprocess.CalledProcessError:
        return None


def get_commits_since(since_ref: str | None = None) -> list[Commit]:
    delimiter = "---COMMIT_DELIMITER---"
    format_str = f"%H%n%s%n%b{delimiter}"

    ref_range = f"{since_ref}..HEAD" if since_ref else "HEAD"

    try:
        output = run_git_command(
            ["log", "--reverse", ref_range, f"--format={format_str}"]
        )
    except subprocess.CalledProcessError:
        return []

    commits: list[Commit] = []
    for commit_text in output.split(delimiter):
        commit_text = commit_text.strip()
        if not commit_text:
            continue

        lines = commit_text.split("\n", 2)
        if len(lines) < 2:
            continue

        commit_hash = lines[0]
        subject = lines[1]
        body = lines[2] if len(lines) > 2 else ""

        parsed: ParsedCommit | None = parse_commit(subject, body)
        if parsed is None:
            continue

        commits.append(
            Commit(
                hash=commit_hash,
                type=parsed.type,
                scope=parsed.scope,
                description=parsed.description,
                is_breaking=parsed.is_breaking,
                changelog_entries=parsed.changelog_entries,
            )
        )

    return commits


def format_changelog_entry(commit: Commit, trailer_text: str) -> str:
    text = trailer_text.strip()
    if commit.is_breaking and not text.lower().startswith("**breaking**"):
        text = f"**BREAKING**: {text}"
    commit_link = f"[{commit.short_hash}](../../commit/{commit.hash})"
    return f"- {text} ({commit_link})"


def generate_changelog_section(
    commits: list[Commit], require_trailers: bool = True
) -> tuple[str, list[str]]:
    categories: dict[str, list[Commit]] = defaultdict(list)
    errors: list[str] = []

    for commit in commits:
        category = TYPE_TO_CATEGORY.get(commit.type)
        if not category:
            continue

        if require_trailers and not commit.changelog_entries:
            errors.append(
                (
                    f"commit {commit.hash} ({commit.type}) is missing required "
                    "'Changelog: ...' trailer"
                )
            )
            continue

        categories[category].append(commit)

    lines: list[str] = []
    category_order = ["Added", "Fixed"]

    for category in category_order:
        if category not in categories:
            continue

        lines.append(f"### {category}")
        lines.append("")
        for commit in categories[category]:
            for trailer_text in commit.changelog_entries:
                lines.append(format_changelog_entry(commit, trailer_text))
        lines.append("")

    return "\n".join(lines).rstrip(), errors


def update_changelog(new_content: str) -> None:
    if not CHANGELOG.exists():
        print(f"Error: {CHANGELOG} not found")
        sys.exit(1)

    content = CHANGELOG.read_text()
    marker = "## [Unreleased]"
    marker_index = content.find(marker)

    if marker_index == -1:
        print("Warning: Could not find [Unreleased] section in CHANGELOG.md")
        return

    start = marker_index + len(marker)
    remainder = content[start:]
    next_header_index = remainder.find("\n## [")

    if next_header_index == -1:
        section_body = remainder
        tail = ""
    else:
        section_body = remainder[:next_header_index]
        tail = remainder[next_header_index:]

    section_body = section_body.strip("\n")
    generated = new_content.strip("\n")
    config_index = generated.find(CONFIG_HEADING)
    if config_index == -1:
        generated_entries = generated
        config_section = ""
    else:
        generated_entries = generated[:config_index].rstrip()
        config_section = generated[config_index:].strip()

    merged_body = "\n\n".join(
        part for part in (section_body, generated_entries) if part
    )
    if config_section:
        merged_body = replace_config_section(merged_body, config_section).rstrip()
    merged_body += "\n"

    new_changelog = content[:start] + "\n\n" + merged_body + tail

    if new_changelog == content:
        print("No changes applied to CHANGELOG.md")
        return

    CHANGELOG.write_text(new_changelog)
    print(f"Updated {CHANGELOG}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate changelog entries from conventional commits",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--since", help="Generate changelog since this tag/ref (default: last tag)"
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Preview changelog entries without modifying files",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Update CHANGELOG.md with generated entries",
    )
    parser.add_argument(
        "--allow-missing-trailers",
        action="store_true",
        help="Do not fail when feat/fix commits are missing Changelog trailers",
    )
    parser.add_argument(
        "--config-to-label",
        help="Label for the later configuration diff (normally the new version)",
    )

    args = parser.parse_args()

    since_ref = args.since
    if not since_ref:
        since_ref = get_latest_tag()
        if since_ref:
            print(f"Generating changelog since tag: {since_ref}")
        else:
            print("No tags found, generating changelog from all commits")

    commits = get_commits_since(since_ref)
    relevant_commits = [c for c in commits if c.type in TYPE_TO_CATEGORY]
    print(
        f"Found {len(commits)} commits, {len(relevant_commits)} relevant for changelog"
    )

    changelog_section, errors = generate_changelog_section(
        commits,
        require_trailers=not args.allow_missing_trailers,
    )

    if errors:
        print("Validation errors:")
        for error in errors:
            print(f"- {error}")
        sys.exit(1)

    if since_ref:
        try:
            config_section = generate_config_changes_section(
                since_ref,
                "HEAD",
                to_label=args.config_to_label or "Unreleased",
            )
        except ConfigChangelogError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        changelog_section = "\n\n".join(
            part for part in (changelog_section, config_section) if part.strip()
        )

    if not changelog_section.strip():
        print("No changelog entries generated")
        return

    print("\n" + "=" * 60)
    print("Generated Changelog Entries:")
    print("=" * 60)
    print(changelog_section)
    print("=" * 60 + "\n")

    if args.update:
        update_changelog(changelog_section)
    elif not args.preview:
        print("Use --update to write to CHANGELOG.md, or --preview to just view")


if __name__ == "__main__":
    main()
