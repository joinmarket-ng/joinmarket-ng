#!/usr/bin/env python3
"""Generate configuration-template changes for release notes."""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"
CONFIG_HEADING = "### Configuration Changes"
CONFIG_TEMPLATE_PATHS = (
    "jmcore/src/jmcore/data/config.toml.template",
    "config.toml.template",
)
VERSION_HEADING_PATTERN = re.compile(
    r"^## \[(?P<version>[^]]+)](?: - \d{4}-\d{2}-\d{2})?\s*$", re.MULTILINE
)
COMPARE_LINK_PATTERN = re.compile(
    r"^\[(?P<version>[^]]+)]: \S*/compare/(?P<from_ref>\S+)\.\.\.(?P<to_ref>\S+)\s*$",
    re.MULTILINE,
)


class ConfigChangelogError(Exception):
    """Raised when a configuration changelog cannot be generated."""


@dataclass(frozen=True)
class TemplateSnapshot:
    ref: str
    path: str
    content: str


def run_git(
    args: list[str], project_root: Path = PROJECT_ROOT, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=check,
    )


def validate_ref(ref: str, project_root: Path = PROJECT_ROOT) -> None:
    result = run_git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        project_root=project_root,
        check=False,
    )
    if result.returncode != 0:
        raise ConfigChangelogError(
            f"Git ref {ref!r} is unavailable. Fetch tags and full history before retrying."
        )


def read_template_snapshot(
    ref: str, project_root: Path = PROJECT_ROOT
) -> TemplateSnapshot | None:
    validate_ref(ref, project_root)
    for path in CONFIG_TEMPLATE_PATHS:
        result = run_git(
            ["show", f"{ref}:{path}"], project_root=project_root, check=False
        )
        if result.returncode == 0:
            return TemplateSnapshot(ref=ref, path=path, content=result.stdout)
    return None


def render_template_diff(
    from_snapshot: TemplateSnapshot | None,
    to_snapshot: TemplateSnapshot | None,
    from_label: str,
    to_label: str,
) -> str:
    from_lines = from_snapshot.content.splitlines() if from_snapshot else []
    to_lines = to_snapshot.content.splitlines() if to_snapshot else []
    if from_lines == to_lines:
        return ""

    diff_lines = difflib.unified_diff(
        from_lines,
        to_lines,
        fromfile=f"config.toml.template ({from_label})",
        tofile=f"config.toml.template ({to_label})",
        lineterm="",
    )
    return "\n".join("" if line == " " else line for line in diff_lines)


def generate_config_changes_section(
    from_ref: str,
    to_ref: str,
    *,
    from_label: str | None = None,
    to_label: str | None = None,
    project_root: Path = PROJECT_ROOT,
) -> str:
    """Build the release-note section for template changes between two refs."""
    from_snapshot = read_template_snapshot(from_ref, project_root)
    to_snapshot = read_template_snapshot(to_ref, project_root)
    diff = render_template_diff(
        from_snapshot,
        to_snapshot,
        from_label or from_ref,
        to_label or to_ref,
    )

    if not diff:
        return (
            f"{CONFIG_HEADING}\n\n"
            "This release did not change the bundled `config.toml.template`."
        )

    return (
        f"{CONFIG_HEADING}\n\n"
        "Existing `config.toml` files are not updated automatically. Review the bundled "
        "template changes below and apply the relevant options manually.\n\n"
        "````diff\n"
        f"{diff}\n"
        "````"
    )


def replace_config_section(section: str, config_section: str) -> str:
    """Replace or append the generated configuration subsection."""
    pattern = re.compile(
        rf"(?:^|\n+){re.escape(CONFIG_HEADING)}\n.*?(?=\n### |\Z)",
        re.DOTALL,
    )
    cleaned = pattern.sub("", section.rstrip()).strip("\n")
    if not cleaned:
        return f"{config_section}\n\n"
    return f"{cleaned}\n\n{config_section}\n\n"


def backfill_changelog(
    content: str, project_root: Path = PROJECT_ROOT
) -> tuple[str, list[str]]:
    """Add configuration sections to every comparable released version."""
    compare_refs = {
        match.group("version"): (match.group("from_ref"), match.group("to_ref"))
        for match in COMPARE_LINK_PATTERN.finditer(content)
    }
    headings = list(VERSION_HEADING_PATTERN.finditer(content))
    replacements: list[tuple[int, int, str]] = []
    updated_versions: list[str] = []

    for index, heading in enumerate(headings):
        version = heading.group("version")
        if version == "Unreleased" or version not in compare_refs:
            continue

        section_start = heading.end()
        section_end = (
            headings[index + 1].start() if index + 1 < len(headings) else len(content)
        )
        from_ref, to_ref = compare_refs[version]
        config_section = generate_config_changes_section(
            from_ref,
            to_ref,
            from_label=from_ref,
            to_label=version,
            project_root=project_root,
        )
        current_section = content[section_start:section_end]
        replacement = "\n" + replace_config_section(
            current_section.strip("\n"), config_section
        )
        replacements.append((section_start, section_end, replacement))
        updated_versions.append(version)

    new_content = content
    for start, end, replacement in reversed(replacements):
        new_content = new_content[:start] + replacement + new_content[end:]
    return new_content, updated_versions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-ref", help="Earlier Git ref to compare")
    parser.add_argument(
        "--to-ref", default="HEAD", help="Later Git ref (default: HEAD)"
    )
    parser.add_argument("--to-label", help="Label used for the later diff header")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Generate sections for all comparable versions in CHANGELOG.md",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Write backfilled sections to CHANGELOG.md (preview is the default)",
    )
    args = parser.parse_args()

    try:
        if args.backfill:
            content = CHANGELOG.read_text()
            new_content, versions = backfill_changelog(content)
            if args.update:
                CHANGELOG.write_text(new_content)
                print(f"Updated {CHANGELOG} for {len(versions)} releases")
            else:
                print(new_content)
            return

        if not args.from_ref:
            parser.error("--from-ref is required unless --backfill is used")
        print(
            generate_config_changes_section(
                args.from_ref,
                args.to_ref,
                to_label=args.to_label,
            )
        )
    except ConfigChangelogError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
