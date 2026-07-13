#!/usr/bin/env python3
"""
Version bumping script for JoinMarket NG.

This script automates the release process by:
1. Bumping the version in all relevant files
2. Generating changelog entries from commit trailers (feat/fix only)
3. Updating the CHANGELOG.md with version and date
4. Updating install.sh DEFAULT_VERSION
5. Creating a git commit with a standard message
6. Creating a git tag
7. Pushing the changes and tag (default, use --no-push to skip)

Usage:
    python scripts/bump_version.py patch          # 0.10.0 -> 0.10.1
    python scripts/bump_version.py minor          # 0.10.0 -> 0.11.0
    python scripts/bump_version.py major          # 0.10.0 -> 1.0.0
    python scripts/bump_version.py 0.12.0         # Explicit version
    python scripts/bump_version.py patch --no-push
    python scripts/bump_version.py --dry-run patch

The script will:
- Update jmcore/src/jmcore/version.py
- Update all pyproject.toml files
- Update install.sh DEFAULT_VERSION
- Update CHANGELOG.md (change [Unreleased] to [X.Y.Z] - YYYY-MM-DD)
- Add diff link at the bottom of CHANGELOG.md
- Commit with message "release: X.Y.Z"
- Tag with "X.Y.Z"
- Push changes and tag (unless --no-push is specified)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

# Project root directory
PROJECT_ROOT = Path(__file__).parent.parent

# Files to update with version
VERSION_FILE = PROJECT_ROOT / "jmcore" / "src" / "jmcore" / "version.py"
INSTALL_SCRIPT = PROJECT_ROOT / "install.sh"
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"

# All pyproject.toml files to update
PYPROJECT_FILES = [
    PROJECT_ROOT / "jmcore" / "pyproject.toml",
    PROJECT_ROOT / "jmwallet" / "pyproject.toml",
    PROJECT_ROOT / "maker" / "pyproject.toml",
    PROJECT_ROOT / "taker" / "pyproject.toml",
    PROJECT_ROOT / "tumbler" / "pyproject.toml",
    PROJECT_ROOT / "directory_server" / "pyproject.toml",
    PROJECT_ROOT / "orderbook_watcher" / "pyproject.toml",
]

# Semantic version regex
SEMVER_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Valid bump types
BumpType = Literal["major", "minor", "patch"]


def parse_version(version: str) -> tuple[int, int, int] | None:
    """Parse a semantic version string, returning None if invalid."""
    match = SEMVER_PATTERN.match(version)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def validate_version(version: str) -> tuple[int, int, int]:
    """Validate and parse a semantic version string."""
    parts = parse_version(version)
    if parts is None:
        print(
            f"Error: Invalid version format '{version}'. Expected X.Y.Z (e.g., 0.10.0)"
        )
        sys.exit(1)
    return parts


def bump_version(current: tuple[int, int, int], bump_type: BumpType) -> str:
    """Calculate next version based on bump type."""
    major, minor, patch = current
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    else:  # patch
        return f"{major}.{minor}.{patch + 1}"


def resolve_version(version_arg: str, current_version: str) -> str:
    """Resolve version argument to an actual version string.

    Args:
        version_arg: Either 'major', 'minor', 'patch', or an explicit version
        current_version: Current version string

    Returns:
        Resolved version string
    """
    if version_arg in ("major", "minor", "patch"):
        current_parts = validate_version(current_version)
        return bump_version(current_parts, version_arg)  # type: ignore[arg-type]
    return version_arg


def get_current_version() -> str:
    """Get the current version from version.py."""
    content = VERSION_FILE.read_text()
    match = re.search(r'__version__\s*=\s*"([^"]+)"', content)
    if not match:
        print(f"Error: Could not find __version__ in {VERSION_FILE}")
        sys.exit(1)
    return match.group(1)


def update_version_file(new_version: str, dry_run: bool = False) -> None:
    """Update the version.py file."""
    content = VERSION_FILE.read_text()
    new_content = re.sub(
        r'__version__\s*=\s*"[^"]+"', f'__version__ = "{new_version}"', content
    )

    if dry_run:
        print(f"Would update {VERSION_FILE}")
        print(f'  __version__ = "{new_version}"')
    else:
        VERSION_FILE.write_text(new_content)
        print(f"Updated {VERSION_FILE}")


def update_pyproject_files(new_version: str, dry_run: bool = False) -> None:
    """Update all pyproject.toml files."""
    for pyproject in PYPROJECT_FILES:
        if not pyproject.exists():
            print(f"Warning: {pyproject} not found, skipping")
            continue

        content = pyproject.read_text()
        # Match version = "X.Y.Z" in [project] section
        new_content = re.sub(
            r'^version\s*=\s*"[^"]+"',
            f'version = "{new_version}"',
            content,
            flags=re.MULTILINE,
        )

        if dry_run:
            print(f"Would update {pyproject}")
        else:
            pyproject.write_text(new_content)
            print(f"Updated {pyproject}")


def update_install_script(new_version: str, dry_run: bool = False) -> None:
    """Update the DEFAULT_VERSION in install.sh."""
    content = INSTALL_SCRIPT.read_text()
    new_content = re.sub(
        r'DEFAULT_VERSION="[^"]+"', f'DEFAULT_VERSION="{new_version}"', content
    )

    if dry_run:
        print(f"Would update {INSTALL_SCRIPT}")
        print(f'  DEFAULT_VERSION="{new_version}"')
    else:
        INSTALL_SCRIPT.write_text(new_content)
        print(f"Updated {INSTALL_SCRIPT}")


def update_changelog(
    new_version: str, current_version: str, dry_run: bool = False
) -> None:
    """
    Update CHANGELOG.md:
    1. Change [Unreleased] to [X.Y.Z] - YYYY-MM-DD
    2. Add new [Unreleased] section
    3. Update diff links at the bottom (supports both relative and absolute URLs)
    """
    content = CHANGELOG.read_text()
    today = datetime.now().strftime("%Y-%m-%d")

    # Replace [Unreleased] with new version and date
    # First, add a new [Unreleased] section
    unreleased_pattern = r"## \[Unreleased\]"
    new_unreleased = f"## [Unreleased]\n\n## [{new_version}] - {today}"
    new_content = re.sub(unreleased_pattern, new_unreleased, content)

    # Update the diff links at the bottom
    # Support both relative (../../compare/...) and absolute GitHub URLs
    # Pattern matches: [Unreleased]: <path>/compare/<version>...HEAD
    # Using \S+ to match version strings that contain dots (e.g., 0.13.9)
    unreleased_link_pattern = r"\[Unreleased\]: (.+/compare/)\S+\.\.\.HEAD"
    match = re.search(unreleased_link_pattern, new_content)
    if match:
        base_path = match.group(1)  # e.g., "../../compare/" or full GitHub URL
        new_unreleased_link = f"[Unreleased]: {base_path}{new_version}...HEAD"
        new_content = re.sub(unreleased_link_pattern, new_unreleased_link, new_content)

        # Add new version diff link after the [Unreleased] link
        new_version_link = (
            f"[{new_version}]: {base_path}{current_version}...{new_version}"
        )

        # Insert the new version link right after the [Unreleased] link
        new_content = re.sub(
            r"(\[Unreleased\]: [^\n]+)",
            f"\\1\n{new_version_link}",
            new_content,
        )

    if dry_run:
        print(f"Would update {CHANGELOG}")
        print(f"  [Unreleased] -> [{new_version}] - {today}")
        print(f"  Update [Unreleased] link to compare from {new_version}")
        print(f"  Add [{new_version}] link: {current_version}...{new_version}")
    else:
        CHANGELOG.write_text(new_content)
        print(f"Updated {CHANGELOG}")


def run_command(
    cmd: list[str], dry_run: bool = False, check: bool = True
) -> subprocess.CompletedProcess | None:
    """Run a command, optionally in dry-run mode."""
    if dry_run:
        print(f"Would run: {' '.join(cmd)}")
        return None

    print(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, check=check, cwd=PROJECT_ROOT)


def generate_changelog_entries(
    current_version: str, dry_run: bool = False, allow_missing_trailers: bool = False
) -> None:
    """Generate changelog entries from commit trailers since the current version tag."""
    cmd = [
        "python",
        "scripts/generate_changelog.py",
        "--since",
        current_version,
    ]
    if dry_run:
        cmd.append("--preview")
    else:
        cmd.append("--update")

    if allow_missing_trailers:
        cmd.append("--allow-missing-trailers")

    run_command(cmd, dry_run=dry_run)


def open_editor_for_changelog(dry_run: bool = False) -> None:
    """Open the user's editor to review/amend the generated changelog entries."""
    if dry_run:
        print("Would open $EDITOR (or vim) to edit CHANGELOG.md")
        return
    editor = os.environ.get("EDITOR", "vim")
    print(f"Opening {CHANGELOG} in {editor!r} for review...")
    subprocess.run([editor, str(CHANGELOG)], check=True, cwd=PROJECT_ROOT)


def git_commit_and_tag(
    new_version: str, dry_run: bool = False, push: bool = True
) -> None:
    """Create git commit and tag."""
    # Stage all changed files
    files_to_stage = [
        str(VERSION_FILE.relative_to(PROJECT_ROOT)),
        str(INSTALL_SCRIPT.relative_to(PROJECT_ROOT)),
        str(CHANGELOG.relative_to(PROJECT_ROOT)),
    ]
    files_to_stage.extend(str(f.relative_to(PROJECT_ROOT)) for f in PYPROJECT_FILES)

    run_command(["git", "add", *files_to_stage], dry_run=dry_run)

    # Create commit
    commit_msg = f"release: {new_version}"
    run_command(["git", "commit", "-m", commit_msg], dry_run=dry_run)

    # Create tag
    run_command(["git", "tag", new_version], dry_run=dry_run)

    if push:
        # Push commit and tag
        run_command(["git", "push"], dry_run=dry_run)
        run_command(["git", "push", "--tags"], dry_run=dry_run)
    else:
        print("\nTo push changes and tag:")
        print("  git push && git push --tags")


def check_git_clean() -> bool:
    """Check if the git working directory is clean."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )
    return len(result.stdout.strip()) == 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bump JoinMarket NG version and prepare release",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "version",
        help=(
            "Version bump type or explicit version. "
            "Use 'major', 'minor', 'patch' for automatic semver bump, "
            "or specify explicit version like '0.12.0'"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Don't push commit and tag to remote (push is default)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="(Deprecated) Push is now the default behavior",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip dirty working directory check",
    )
    parser.add_argument(
        "--allow-missing-trailers",
        action="store_true",
        help=(
            "Allow feat/fix commits without Changelog trailers while generating changelog "
            "(not recommended)"
        ),
    )

    args = parser.parse_args()

    # Get current version first (needed for resolving bump types)
    current_version = get_current_version()

    # Resolve version (handles major/minor/patch or explicit version)
    new_version = resolve_version(args.version, current_version)

    # Validate the resolved version format
    validate_version(new_version)

    print(f"Current version: {current_version}")
    if args.version in ("major", "minor", "patch"):
        print(f"Bump type: {args.version}")
    print(f"New version: {new_version}")
    print()

    # Check if new version is greater than current
    current_parts = validate_version(current_version)
    new_parts = validate_version(new_version)
    if new_parts <= current_parts:
        print(
            f"Warning: New version {new_version} is not greater than current {current_version}"
        )
        if not args.force:
            response = input("Continue anyway? [y/N] ")
            if response.lower() != "y":
                print("Aborted")
                sys.exit(1)

    # Check for clean working directory
    if not args.dry_run and not args.force:
        if not check_git_clean():
            print(
                "Error: Working directory is not clean. Commit or stash changes first."
            )
            print("       Use --force to skip this check.")
            sys.exit(1)

    # Determine push behavior (push is default, --no-push disables)
    should_push = not args.no_push

    # Update files
    print("Updating files...")
    generate_changelog_entries(
        current_version,
        dry_run=args.dry_run,
        allow_missing_trailers=args.allow_missing_trailers,
    )
    update_version_file(new_version, dry_run=args.dry_run)
    update_pyproject_files(new_version, dry_run=args.dry_run)
    update_install_script(new_version, dry_run=args.dry_run)
    update_changelog(new_version, current_version, dry_run=args.dry_run)
    open_editor_for_changelog(dry_run=args.dry_run)
    print()

    # Git operations
    print("Git operations...")
    git_commit_and_tag(new_version, dry_run=args.dry_run, push=should_push)

    if args.dry_run:
        print("\nThis was a dry run. No changes were made.")
    else:
        print(f"\nVersion bumped to {new_version}")
        if should_push:
            print("Changes and tag pushed to remote.")
        print("GitHub Actions will create the release when the tag is pushed.")


if __name__ == "__main__":
    main()
