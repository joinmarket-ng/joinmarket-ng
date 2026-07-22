#!/usr/bin/env python3
"""Generate static shell completion scripts for JoinMarket CLI tools.

Introspects Typer/Click command structures and emits native bash/zsh
completion scripts that require zero Python execution at tab-press time.

Usage::

    python scripts/generate_completions.py              # writes to completions/
    python scripts/generate_completions.py --out-dir /tmp/comps

The generated files can be sourced directly in the shell or placed in
the appropriate completion directories (e.g. ``/usr/share/zsh/site-functions/``).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import typer


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class ParamInfo:
    """Metadata for a single CLI option/argument."""

    opts: list[str]  # e.g. ['--network', '-n']
    is_flag: bool
    help: str
    choices: list[str] = field(default_factory=list)
    is_path: bool = False
    is_dir: bool = False
    secondary_opts: list[str] = field(default_factory=list)


@dataclass
class CommandInfo:
    """Metadata for a single CLI (sub)command."""

    name: str
    help: str
    params: list[ParamInfo] = field(default_factory=list)


@dataclass
class CLIInfo:
    """Top-level CLI descriptor."""

    name: str  # console_script name, e.g. 'jm-maker'
    help: str
    commands: list[CommandInfo]  # empty if single-command app
    params: list[ParamInfo]  # top-level params (single-command) or group-level


# ---------------------------------------------------------------------------
# Introspection
# ---------------------------------------------------------------------------

# Map console_script names to their Typer app import paths
CLI_APPS: dict[str, str] = {
    "jm-maker": "maker.cli:app",
    "jm-taker": "taker.cli:app",
    "jm-tumbler": "tumbler.cli:app",
    "jm-wallet": "jmwallet.cli:app",
    "jmwalletd": "jmwalletd.cli:app",
}


def _import_app(import_path: str) -> typer.Typer:
    """Import a Typer app from 'module.path:attribute' string."""
    module_path, attr = import_path.rsplit(":", 1)
    mod = __import__(module_path, fromlist=[attr])
    return getattr(mod, attr)


def _extract_params(params: list) -> list[ParamInfo]:
    """Extract parameter metadata from Click parameter list.

    Note: typer >= 0.16 vendors click as ``typer._click``, so ``isinstance``
    checks against the installed ``click`` package fail. Use the stable
    ``param_type_name`` attribute instead (present in both click flavors).
    """
    result: list[ParamInfo] = []
    for p in params:
        if getattr(p, "param_type_name", "") != "option":
            continue
        # Skip typer's built-in completion options
        if p.name in ("install_completion", "show_completion"):
            continue

        choices: list[str] = []
        if hasattr(p.type, "choices"):
            choices = list(p.type.choices)

        is_path = (
            "Path" in type(p.type).__name__ or "path" in type(p.type).__name__.lower()
        )
        is_dir = is_path and getattr(p.type, "file_okay", True) is False

        help_text = getattr(p, "help", "") or ""
        # Truncate multi-line help to first line for completion descriptions
        help_text = help_text.split("\n")[0].strip()

        result.append(
            ParamInfo(
                opts=list(p.opts),
                is_flag=p.is_flag,
                help=help_text,
                choices=choices,
                is_path=is_path,
                is_dir=is_dir,
                secondary_opts=list(p.secondary_opts) if p.secondary_opts else [],
            )
        )
    return result


def introspect_cli(name: str, import_path: str) -> CLIInfo:
    """Build CLIInfo by introspecting the Typer/Click app."""
    typer_app = _import_app(import_path)
    click_app = typer.main.get_command(typer_app)

    app_help = click_app.help or ""

    if hasattr(click_app, "list_commands"):
        # Group with subcommands (use the app's own context class: the click
        # flavor may be typer's vendored copy rather than the installed click)
        ctx = click_app.context_class(click_app)
        commands: list[CommandInfo] = []
        for cmd_name in click_app.list_commands(ctx):
            cmd = click_app.get_command(ctx, cmd_name)
            if cmd is None:
                continue
            commands.append(
                CommandInfo(
                    name=cmd_name,
                    help=(cmd.help or cmd.short_help or "").split("\n")[0].strip(),
                    params=_extract_params(cmd.params),
                )
            )
        return CLIInfo(
            name=name,
            help=app_help,
            commands=commands,
            params=_extract_params(click_app.params),
        )
    else:
        # Single command (e.g. jmwalletd serve is the only command)
        return CLIInfo(
            name=name,
            help=app_help,
            commands=[],
            params=_extract_params(click_app.params),
        )


# ---------------------------------------------------------------------------
# ZSH generation
# ---------------------------------------------------------------------------


def _zsh_escape(s: str) -> str:
    """Escape special characters for zsh completion descriptions."""
    return s.replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:")


def _zsh_option_spec(param: ParamInfo) -> str:
    """Build a single _arguments option spec for a param.

    Format: ``'--opt=[description]:message:action'``
    """
    primary = param.opts[0]
    desc = _zsh_escape(param.help)

    # Determine the action (what appears after the option)
    if param.is_flag:
        action = ""
    elif param.choices:
        choices_str = " ".join(param.choices)
        action = f":choice:({choices_str})"
    elif param.is_dir:
        action = ":dir:_directories"
    elif param.is_path:
        action = ":file:_files"
    else:
        action = ": :"

    if param.is_flag:
        spec = f"'{primary}[{desc}]'"
    else:
        spec = f"'{primary}=[{desc}]{action}'"

    return spec


def _generate_zsh_subcommand_case(cmd: CommandInfo, indent: str = "        ") -> str:
    """Generate the case clause for a single subcommand."""
    lines: list[str] = []
    lines.append(f"{indent}{cmd.name})")
    lines.append(f"{indent}  _arguments \\")
    for param in cmd.params:
        spec = _zsh_option_spec(param)
        lines.append(f"{indent}    {spec} \\")
    lines.append(f"{indent}    '--help[Show this message and exit]'")
    lines.append(f"{indent}  ;;")
    return "\n".join(lines)


def generate_zsh(cli: CLIInfo) -> str:
    """Generate a complete zsh completion script."""
    func_name = cli.name.replace("-", "_")

    if not cli.commands:
        # Single-command app
        lines = [
            f"#compdef {cli.name}",
            "",
            f"# Static completion for {cli.name}",
            "# Generated by scripts/generate_completions.py",
            "",
            f"_{func_name}() {{",
            "  _arguments \\",
        ]
        for param in cli.params:
            spec = _zsh_option_spec(param)
            lines.append(f"    {spec} \\")
        lines.append("    '--help[Show this message and exit]'")
        lines.append("}")
        lines.append("")
        # compdef for direct sourcing; #compdef header handles fpath loading
        lines.append(f"compdef _{func_name} {cli.name}")
        lines.append("")
        return "\n".join(lines)

    # Multi-command app
    lines = [
        f"#compdef {cli.name}",
        "",
        f"# Static completion for {cli.name}",
        "# Generated by scripts/generate_completions.py",
        "",
        f"_{func_name}() {{",
        "  local -a commands",
        "  commands=(",
    ]
    for cmd in cli.commands:
        lines.append(f"    '{cmd.name}:{_zsh_escape(cmd.help)}'")
    lines.extend(
        [
            "  )",
            "",
            "  _arguments -C \\",
            "    '1:command:->command' \\",
            "    '*::arg:->args'",
            "",
            "  case $state in",
            "    command)",
            f"      _describe -t commands '{cli.name} commands' commands",
            "      ;;",
            "    args)",
            "      case $words[1] in",
        ]
    )
    for cmd in cli.commands:
        lines.append(_generate_zsh_subcommand_case(cmd))
    lines.extend(
        [
            "      esac",
            "      ;;",
            "  esac",
            "}",
            "",
            # compdef for direct sourcing; #compdef header handles fpath loading
            f"compdef _{func_name} {cli.name}",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Bash generation
# ---------------------------------------------------------------------------


def _generate_bash_subcommand_case(cmd: CommandInfo, indent: str = "      ") -> str:
    """Generate the bash case clause for a single subcommand."""
    all_opts = ["--help"]
    for param in cmd.params:
        all_opts.extend(param.opts)
    opts_str = " ".join(all_opts)
    lines = [
        f"{indent}{cmd.name})",
        f'{indent}  COMPREPLY=( $(compgen -W "{opts_str}" -- "$cur") )',
        f"{indent}  ;;",
    ]
    return "\n".join(lines)


def generate_bash(cli: CLIInfo) -> str:
    """Generate a complete bash completion script."""
    func_name = cli.name.replace("-", "_")

    if not cli.commands:
        # Single-command app
        all_opts = ["--help"]
        for param in cli.params:
            all_opts.extend(param.opts)
        opts_str = " ".join(all_opts)

        lines = [
            f"# Static completion for {cli.name}",
            "# Generated by scripts/generate_completions.py",
            "",
            f"_{func_name}_completion() {{",
            "    local cur prev",
            "    COMPREPLY=()",
            '    cur="${COMP_WORDS[COMP_CWORD]}"',
            '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
            "",
            f'    COMPREPLY=( $(compgen -W "{opts_str}" -- "$cur") )',
            "    return 0",
            "}",
            "",
            f"complete -o default -F _{func_name}_completion {cli.name}",
            "",
        ]
        return "\n".join(lines)

    # Multi-command app
    subcmds = " ".join(cmd.name for cmd in cli.commands)

    lines = [
        f"# Static completion for {cli.name}",
        "# Generated by scripts/generate_completions.py",
        "",
        f"_{func_name}_completion() {{",
        "    local cur prev subcmd",
        "    COMPREPLY=()",
        '    cur="${COMP_WORDS[COMP_CWORD]}"',
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
        "",
        '    if [ "$COMP_CWORD" -eq 1 ]; then',
        f'        COMPREPLY=( $(compgen -W "{subcmds}" -- "$cur") )',
        "        return 0",
        "    fi",
        "",
        '    subcmd="${COMP_WORDS[1]}"',
        '    case "$subcmd" in',
    ]
    for cmd in cli.commands:
        lines.append(_generate_bash_subcommand_case(cmd))
    lines.extend(
        [
            "    esac",
            "    return 0",
            "}",
            "",
            f"complete -o default -F _{func_name}_completion {cli.name}",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate static shell completions for JoinMarket CLIs",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "completions",
        help="Output directory (default: <repo>/completions/)",
    )
    parser.add_argument(
        "--shells",
        nargs="+",
        default=["bash", "zsh"],
        choices=["bash", "zsh"],
        help="Which shells to generate for",
    )
    args = parser.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    generators = {
        "bash": (generate_bash, ".bash"),
        "zsh": (generate_zsh, ".zsh"),
    }

    for cli_name, import_path in CLI_APPS.items():
        print(f"Introspecting {cli_name} ({import_path})...")
        try:
            cli = introspect_cli(cli_name, import_path)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            continue

        cmd_count = len(cli.commands) if cli.commands else 1
        param_count = (
            sum(len(c.params) for c in cli.commands)
            if cli.commands
            else len(cli.params)
        )
        print(f"  Found {cmd_count} command(s), {param_count} option(s)")

        for shell in args.shells:
            gen_func, ext = generators[shell]
            content = gen_func(cli)
            out_file = out_dir / f"{cli_name}{ext}"
            out_file.write_text(content)
            print(f"  Wrote {out_file}")

    print(f"\nDone. Files written to {out_dir}/")
    print("Source these files in your shell config, or install via:")
    print("  source <(cat completions/*.bash)  # bash")
    print("  fpath=(completions $fpath); autoload -Uz compinit && compinit  # zsh")


if __name__ == "__main__":
    main()
