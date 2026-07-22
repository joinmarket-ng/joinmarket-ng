"""
Alphabetically sorted ``--help`` output for Typer-based CLIs.

Typer preserves the registration order of subcommands and the function
signature order of options, which makes long help screens hard to scan.
This module provides drop-in replacements that sort:

- Subcommands alphabetically in group help.
- Options alphabetically (by their first long name) in command help.
- Positional arguments keep their declared order, since it is meaningful.

Usage::

    from jmcore.cli_help import SortedTyper

    app = SortedTyper(name="jm-example", no_args_is_help=True)

Implementation note: typer >= 0.16 vendors click as ``typer._click``, so the
parameters seen at help-rendering time are not instances of the installed
``click`` package's classes. Classification therefore relies on the stable
``Parameter.param_type_name`` attribute ("option" vs "argument"), which both
real and vendored click provide.

Note: unlike :mod:`jmcore.cli_common`, this module imports ``typer`` at
module level. It must only be imported by components that depend on typer
(maker, taker, tumbler, jmwallet, jmwalletd); jmcore itself does not
declare a typer dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import typer
from typer.core import TyperCommand, TyperGroup

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "SortedHelpCommand",
    "SortedHelpGroup",
    "SortedTyper",
    "find_unsorted_help",
    "sort_params_for_help",
]


def _is_option(param: Any) -> bool:
    """True when ``param`` is an option (works for real and vendored click)."""
    return bool(getattr(param, "param_type_name", "") == "option")


def _option_sort_key(option: Any) -> str:
    """Sort key for an option: its first long name (fallback: first name)."""
    opts: list[str] = list(option.opts)
    long_opts = [opt for opt in opts if opt.startswith("--")]
    name = long_opts[0] if long_opts else opts[0]
    return name.lstrip("-").casefold()


def sort_params_for_help(params: list[Any]) -> list[Any]:
    """Return params with arguments first (declared order), then sorted options.

    Positional arguments keep their declaration order because it defines the
    call syntax; options are listed alphabetically. Parsing is unaffected:
    click matches options by name and the relative argument order is kept.
    """
    arguments = [p for p in params if not _is_option(p)]
    options = sorted((p for p in params if _is_option(p)), key=_option_sort_key)
    return [*arguments, *options]


class SortedHelpCommand(TyperCommand):
    """Typer command that lists its options alphabetically in ``--help``."""

    def get_params(self, ctx: Any) -> list[Any]:
        return sort_params_for_help(super().get_params(ctx))


class SortedHelpGroup(TyperGroup):
    """Typer group that sorts subcommands and its own options in ``--help``."""

    def get_params(self, ctx: Any) -> list[Any]:
        return sort_params_for_help(super().get_params(ctx))

    def list_commands(self, ctx: Any) -> list[str]:
        return sorted(self.commands)


class SortedTyper(typer.Typer):
    """:class:`typer.Typer` with alphabetically sorted help output.

    Defaults the group class to :class:`SortedHelpGroup` and every registered
    command to :class:`SortedHelpCommand`. Sub-apps attached with
    ``add_typer`` should themselves be ``SortedTyper`` instances.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("cls", SortedHelpGroup)
        super().__init__(*args, **kwargs)

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        kwargs.setdefault("cls", SortedHelpCommand)
        return super().command(*args, **kwargs)


def find_unsorted_help(app: typer.Typer) -> list[str]:
    """Return help-order violations for ``app`` (empty list means sorted).

    Walks the resolved click command tree and reports every group whose
    subcommands are not alphabetical and every command whose options are not
    alphabetical. Intended for tests guarding the sorted-help behavior.
    """
    violations: list[str] = []
    root = typer.main.get_command(app)
    _check_command(root, root.name or "<root>", violations)
    return violations


def _check_command(command: Any, path: str, violations: list[str]) -> None:
    ctx = command.context_class(command)
    option_names = [_option_sort_key(p) for p in command.get_params(ctx) if _is_option(p)]
    if option_names != sorted(option_names):
        violations.append(f"{path}: options not sorted: {option_names}")

    if hasattr(command, "list_commands"):
        subcommand_names = list(command.list_commands(ctx))
        if subcommand_names != sorted(subcommand_names):
            violations.append(f"{path}: subcommands not sorted: {subcommand_names}")
        for name in subcommand_names:
            subcommand = command.get_command(ctx, name)
            if subcommand is not None:
                _check_command(subcommand, f"{path} {name}", violations)
