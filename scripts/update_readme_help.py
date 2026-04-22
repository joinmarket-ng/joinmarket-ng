#!/usr/bin/env python3
"""Update CLI help sections in documentation files.

This script extracts help text from CLI commands and inserts them into
auto-generated sections in both module READMEs and docs pages.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def get_command_help(command: list[str]) -> str:
    """
    Run a command to get its help text.

    Args:
        command: Command to run (e.g., ["jm-wallet", "--help"])

    Returns:
        Help text output from the command
    """
    try:
        # Force a fixed terminal width to ensure consistent output
        env = dict(os.environ)
        env["COLUMNS"] = "80"

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=env,
        )
        # Return stdout or stderr (typer outputs to stdout)
        return result.stdout if result.stdout else result.stderr
    except (
        subprocess.TimeoutExpired,
        subprocess.CalledProcessError,
        FileNotFoundError,
    ) as e:
        print(f"Warning: Failed to run {' '.join(command)}: {e}", file=sys.stderr)
        return ""


def strip_ansi(text: str) -> str:
    """
    Remove ANSI escape codes from text.

    Args:
        text: Text with ANSI codes

    Returns:
        Text without ANSI codes
    """
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", text)


def discover_subcommands(base_command: str) -> list[str]:
    """
    Discover subcommands for a given base command.

    Args:
        base_command: Base command (e.g., "jm-wallet")

    Returns:
        List of subcommand names
    """
    help_text = get_command_help([base_command, "--help"])
    if not help_text:
        return []

    # Strip ANSI codes for easier parsing
    clean_text = strip_ansi(help_text)

    subcommands = []
    in_commands_section = False

    for line in clean_text.split("\n"):
        # Check for Commands section (Typer format or argparse positional arguments)
        if ("Commands" in line and ("─" in line or "Commands:" in line)) or (
            "positional arguments:" in line
        ):
            in_commands_section = True
            continue

        if in_commands_section:
            # Stop at next section (options, etc.) or end of box
            if ("╰" in line) or (
                ("options:" in line.lower() or "optional arguments:" in line.lower())
                and not any(
                    c.isalnum() or c == "-"
                    for c in line.replace("options", "").replace(
                        "optional arguments", ""
                    )
                )
            ):
                break

            # Extract command name (first word after whitespace, before description)
            # Typer format: "│ command-name  Description text │"
            # Argparse format: "  command-name  Description text"
            # Argparse choices format: "  {status,health}  Available commands"
            stripped = line.strip()

            # Skip argparse choices line (e.g., "{status,health}  Available commands")
            if stripped.startswith("{") and "}" in stripped:
                continue

            # Remove box drawing characters (Typer)
            stripped = stripped.replace("│", "").strip()

            if stripped and not stripped.startswith("─"):
                # Split on multiple spaces to separate command from description
                parts = re.split(r"\s{2,}", stripped)
                if parts and parts[0]:
                    # Command name is the first part
                    cmd_name = parts[0].strip()
                    # Validate it looks like a command (alphanumeric with hyphens)
                    if re.match(r"^[a-zA-Z][a-zA-Z0-9\-]*$", cmd_name):
                        subcommands.append(cmd_name)

    return subcommands


def create_help_section(command_name: str, help_text: str) -> str:
    """
    Create a collapsed details section for command help.

    Args:
        command_name: Full command (e.g., "jm-wallet --help")
        help_text: Help text to include

    Returns:
        Markdown details section
    """
    if not help_text:
        return ""

    # Strip ANSI codes for clean markdown rendering
    clean_text = strip_ansi(help_text)

    # Strip trailing whitespace from each line to avoid conflicts with pre-commit hooks
    lines = clean_text.splitlines()
    cleaned_lines = [line.rstrip() for line in lines]
    cleaned_help = "\n".join(cleaned_lines)

    # Escape any existing backticks in help text to avoid breaking markdown
    escaped_help = cleaned_help.replace("```", "\\`\\`\\`")

    return f"""<details>
<summary><code>{command_name}</code></summary>

```
{escaped_help.rstrip()}
```

</details>
"""


def generate_all_help_sections(base_command: str) -> str:
    """
    Generate all help sections for a command and its subcommands.

    Args:
        base_command: Base command (e.g., "jm-wallet")

    Returns:
        Markdown with all help sections
    """
    sections = []

    # Main command help
    main_help = get_command_help([base_command, "--help"])
    if main_help:
        sections.append(create_help_section(f"{base_command} --help", main_help))

    # Subcommand help
    subcommands = discover_subcommands(base_command)
    for subcmd in subcommands:
        subcmd_help = get_command_help([base_command, subcmd, "--help"])
        if subcmd_help:
            sections.append(
                create_help_section(f"{base_command} {subcmd} --help", subcmd_help)
            )

    return "\n".join(sections)


def update_readme_help(readme_path: Path, command: str) -> bool:
    """
    Update README file with command help sections.

    Args:
        readme_path: Path to README.md file
        command: Base command name (e.g., "jm-wallet")

    Returns:
        True if file was modified, False otherwise
    """
    if not readme_path.exists():
        print(f"Warning: {readme_path} not found", file=sys.stderr)
        return False

    # Read current README
    content = readme_path.read_text()

    # Generate new help sections
    help_sections = generate_all_help_sections(command)
    if not help_sections:
        print(f"Warning: No help sections generated for {command}", file=sys.stderr)
        return False

    # Define markers for auto-generated section
    start_marker = f"<!-- AUTO-GENERATED HELP START: {command} -->"
    end_marker = f"<!-- AUTO-GENERATED HELP END: {command} -->"

    # Check if markers exist
    if start_marker in content and end_marker in content:
        # Replace existing section
        pattern = re.compile(
            rf"{re.escape(start_marker)}.*?{re.escape(end_marker)}", re.DOTALL
        )
        new_section = f"{start_marker}\n\n{help_sections}\n\n{end_marker}"
        new_content = pattern.sub(new_section, content)
    else:
        # Markers don't exist - add them at the end of file
        new_section = f"\n\n{start_marker}\n\n{help_sections}\n\n{end_marker}\n"
        new_content = content.rstrip() + new_section

    # Check if content changed
    if content == new_content:
        return False

    # Write updated README
    readme_path.write_text(new_content)
    print(f"Updated {readme_path}")
    return True


def main() -> int:
    """Main entry point."""
    # Find project root (directory containing this script's parent)
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    # Define commands and all docs files that should contain their help sections.
    commands_to_update: list[tuple[str, list[Path]]] = [
        (
            "jm-wallet",
            [
                project_root / "jmwallet" / "README.md",
                project_root / "docs" / "README-jmwallet.md",
            ],
        ),
        (
            "jm-maker",
            [
                project_root / "maker" / "README.md",
                project_root / "docs" / "README-maker.md",
            ],
        ),
        (
            "jm-taker",
            [
                project_root / "taker" / "README.md",
                project_root / "docs" / "README-taker.md",
            ],
        ),
        (
            "jm-directory-ctl",
            [
                project_root / "directory_server" / "README.md",
                project_root / "docs" / "README-directory-server.md",
            ],
        ),
        (
            "jm-tumbler",
            [
                project_root / "jmtumbler" / "README.md",
                project_root / "docs" / "README-jmtumbler.md",
            ],
        ),
    ]

    modified = False

    for command, readmes in commands_to_update:
        print(f"Processing {command}...", file=sys.stderr)

        # Check if command is available
        help_text = get_command_help([command, "--help"])
        if not help_text:
            print(
                f"Warning: Command '{command}' not available. "
                f"Make sure the package is installed.",
                file=sys.stderr,
            )
            continue

        # Update all target docs for this command.
        for readme in readmes:
            if update_readme_help(readme, command):
                modified = True

    # Return exit code for pre-commit
    # 0 = no changes, 1 = changes made (pre-commit will fail and show diff)
    return 1 if modified else 0


if __name__ == "__main__":
    sys.exit(main())
