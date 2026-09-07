"""Narrow TOML config access for the shell TUI.

The TUI only needs to read, set, and remove string values in named top-level
tables. Keeping that surface here avoids shell parsing and preserves comments
and unrelated configuration through TOMLKit.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import tomlkit
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from jmcore.secure_files import atomic_write_sensitive_file, read_sensitive_file


class ConfigFileError(Exception):
    """Raised when a config file cannot be safely handled by the TUI."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(kind: str, value: str) -> None:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ConfigFileError(f"invalid {kind}")


def _read_document(path: Path) -> TOMLDocument:
    if not path.exists():
        return tomlkit.document()

    try:
        text = read_sensitive_file(path).decode("utf-8")
        return tomlkit.parse(text)
    except (OSError, UnicodeDecodeError, tomlkit.exceptions.ParseError) as exc:
        raise ConfigFileError("cannot read valid TOML config") from exc


def _get_table(document: TOMLDocument, section: str) -> Table | None:
    if section not in document:
        return None

    table_value = document[section]
    if not isinstance(table_value, Table):
        raise ConfigFileError("config section is not a table")
    return table_value


def get_config_value(path: Path, section: str, key: str) -> str | None:
    """Return a string config value, or ``None`` when the file or key is absent."""
    _validate_identifier("section", section)
    _validate_identifier("key", key)
    table = _get_table(_read_document(path), section)
    if table is None or key not in table:
        return None

    value = table[key]
    if not isinstance(value, str):
        raise ConfigFileError("config value is not a string")
    return value


def set_config_value(path: Path, section: str, key: str, value: str) -> None:
    """Set a string config value without rewriting the file for a no-op."""
    _validate_identifier("section", section)
    _validate_identifier("key", key)
    document = _read_document(path)
    table = _get_table(document, section)
    if table is None:
        table = tomlkit.table()
        document[section] = table
    elif key in table:
        current_value = table[key]
        if not isinstance(current_value, str):
            raise ConfigFileError("config value is not a string")
        if current_value == value:
            return

    table[key] = value
    try:
        atomic_write_sensitive_file(path, tomlkit.dumps(document).encode("utf-8"))
    except OSError as exc:
        raise ConfigFileError("cannot write config") from exc


def remove_config_value(path: Path, section: str, key: str) -> None:
    """Remove a string config value, doing nothing when the file or key is absent."""
    _validate_identifier("section", section)
    _validate_identifier("key", key)
    if not path.exists():
        return

    document = _read_document(path)
    table = _get_table(document, section)
    if table is None or key not in table:
        return

    value = table[key]
    if not isinstance(value, str):
        raise ConfigFileError("config value is not a string")
    del table[key]
    try:
        atomic_write_sensitive_file(path, tomlkit.dumps(document).encode("utf-8"))
    except OSError as exc:
        raise ConfigFileError("cannot write config") from exc


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read and update TUI TOML config values")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("get", "set", "remove"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--config", type=Path, required=True)
        command_parser.add_argument("--section", required=True)
        command_parser.add_argument("--key", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the TUI config helper command."""
    args = _parse_args(argv)
    try:
        if args.command == "get":
            value = get_config_value(args.config, args.section, args.key)
            if value is not None:
                sys.stdout.write(value)
        elif args.command == "set":
            try:
                value = sys.stdin.buffer.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConfigFileError("config value must be UTF-8 text") from exc
            set_config_value(args.config, args.section, args.key, value)
        else:
            remove_config_value(args.config, args.section, args.key)
    except ConfigFileError as exc:
        print(f"config file error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
