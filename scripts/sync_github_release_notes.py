#!/usr/bin/env python3
"""Synchronize published GitHub release notes from CHANGELOG.md."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"
DEFAULT_REPOSITORY = "joinmarket-ng/joinmarket-ng"
VERSION_HEADING_PATTERN = re.compile(
    r"^## \[(?P<version>[^]]+)](?: - \d{4}-\d{2}-\d{2})?\s*$", re.MULTILINE
)
REFERENCE_LINK_PATTERN = re.compile(r"^\[[^]]+]: ", re.MULTILINE)


class ReleaseSyncError(Exception):
    """Raised when release notes cannot be synchronized."""


def extract_release_notes(content: str) -> dict[str, str]:
    headings = list(VERSION_HEADING_PATTERN.finditer(content))
    notes: dict[str, str] = {}
    for index, heading in enumerate(headings):
        version = heading.group("version")
        if version == "Unreleased":
            continue
        start = heading.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        reference_links = REFERENCE_LINK_PATTERN.search(content, start, end)
        if reference_links:
            end = reference_links.start()
        notes[version] = content[start:end].strip()
    return notes


def list_release_tags(repository: str, limit: int) -> list[str]:
    result = subprocess.run(
        [
            "gh",
            "release",
            "list",
            "--repo",
            repository,
            "--limit",
            str(limit),
            "--json",
            "tagName",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    return [item["tagName"] for item in payload]


def update_release(repository: str, tag: str, notes: str) -> None:
    subprocess.run(
        [
            "gh",
            "release",
            "edit",
            tag,
            "--repo",
            repository,
            "--notes-file",
            "-",
        ],
        cwd=PROJECT_ROOT,
        input=notes,
        text=True,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tags", nargs="*", help="Release tags to update (default: all published)"
    )
    parser.add_argument("--repo", default=DEFAULT_REPOSITORY, help="GitHub OWNER/REPO")
    parser.add_argument(
        "--limit", type=int, default=100, help="Maximum releases to query"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates with gh release edit (preview is the default)",
    )
    args = parser.parse_args()

    try:
        changelog_notes = extract_release_notes(CHANGELOG.read_text())
        published_tags = list_release_tags(args.repo, args.limit)
        requested_tags = args.tags or published_tags
        missing_releases = sorted(set(requested_tags) - set(published_tags))
        missing_notes = sorted(set(requested_tags) - set(changelog_notes))
        if missing_releases:
            raise ReleaseSyncError(
                f"not published on GitHub: {', '.join(missing_releases)}"
            )
        if missing_notes:
            raise ReleaseSyncError(
                f"missing from CHANGELOG.md: {', '.join(missing_notes)}"
            )

        action = "Updating" if args.apply else "Would update"
        print(f"{action} {len(requested_tags)} release(s) in {args.repo}:")
        for tag in requested_tags:
            print(f"- {tag}")
            if args.apply:
                update_release(args.repo, tag, changelog_notes[tag])
        if not args.apply:
            print("Preview only. Re-run with --apply to edit GitHub releases.")
    except (
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
        ReleaseSyncError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
