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
from tomlkit.items import Comment, Item, Table, Whitespace
from tomlkit.toml_document import TOMLDocument

from jmcore.secure_files import atomic_write_sensitive_file, read_sensitive_file


class ConfigFileError(Exception):
    """Raised when a config file cannot be safely handled by the TUI."""


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BANNER_RE = re.compile(r"^#\s*=+\s*$")


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


def _chapter_banner_cut(table: Table, next_section: str | None) -> int | None:
    """Return the body index where the next chapter's banner comments start.

    Template chapters end with a ``# ====`` separator banner that TOMLKit
    attaches to the preceding table as trailing trivia. Keys must be inserted
    before that banner so they stay inside their own chapter. ``None`` means
    there is no banner (for example the last chapter) and appending is fine.
    """
    body = table.value.body
    if next_section is None:
        return None
    banner_index: int | None = None
    for index, (item_key, item) in enumerate(body):
        if (
            item_key is None
            and isinstance(item, Comment)
            and _BANNER_RE.match(item.trivia.comment.strip())
            and index + 2 < len(body)
        ):
            heading, closing = body[index + 1][1], body[index + 2][1]
            if not (
                isinstance(heading, Comment)
                and isinstance(closing, Comment)
                and re.fullmatch(
                    rf"#\s*{re.escape(next_section)}\s+(?:Settings|Configuration)\s*",
                    heading.trivia.comment,
                    flags=re.IGNORECASE,
                )
                and _BANNER_RE.match(closing.trivia.comment.strip())
            ):
                continue
            banner_index = index
            break
    if banner_index is None:
        return None
    cut = banner_index
    while cut > 0 and body[cut - 1][0] is None and isinstance(body[cut - 1][1], Whitespace):
        cut -= 1
    return cut


def _table_key_index(table: Table, key: str) -> int | None:
    """Return the body index of an existing key, or ``None`` when absent."""
    for index, (item_key, _item) in enumerate(table.value.body):
        if item_key is not None and item_key.key == key:
            return index
    return None


def _next_table_section(document: TOMLDocument, section: str) -> str | None:
    seen = False
    for name, value in document.items():
        if seen and isinstance(value, Table):
            return name
        seen = name == section or seen
    return None


def _insert_table_value(
    table: Table, key: str, value: str | bool | Item, next_section: str | None
) -> None:
    """Append a new key, keeping the next chapter's banner comments last."""
    cut = _chapter_banner_cut(table, next_section)
    if cut is None:
        table[key] = value
        return
    body = table.value.body
    banner_trivia = body[cut:]
    del body[cut:]
    table[key] = value
    body.extend(banner_trivia)


def _write_document(path: Path, document: TOMLDocument) -> None:
    try:
        atomic_write_sensitive_file(path, tomlkit.dumps(document).encode("utf-8"))
    except OSError as exc:
        raise ConfigFileError("cannot write config") from exc


def get_config_value(path: Path, section: str, key: str) -> str | None:
    """Return a config value as text, or ``None`` when the file or key is absent.

    Boolean values are rendered as ``"true"`` / ``"false"`` so shell callers
    can compare against a stable string.
    """
    _validate_identifier("section", section)
    _validate_identifier("key", key)
    table = _get_table(_read_document(path), section)
    if table is None or key not in table:
        return None

    value = table[key]
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str):
        raise ConfigFileError("config value is not a string or boolean")
    return value


def set_config_value(
    path: Path, section: str, key: str, value: str, *, as_bool: bool = False
) -> None:
    """Set a string (or boolean with ``as_bool``) config value without rewriting
    the file for a no-op.

    New keys are inserted before the next chapter's banner comments so that
    template-generated files keep every key inside its own chapter. Keys that
    were previously written after such a banner are moved back on update.

    The value type is stable per key: an existing string value can only be
    updated with a string, an existing boolean only with ``as_bool``. With
    ``as_bool`` the value must be exactly ``"true"`` or ``"false"`` and is
    written as a TOML boolean.
    """
    _validate_identifier("section", section)
    _validate_identifier("key", key)
    typed_value: str | bool = value
    if as_bool:
        # Tolerate a trailing newline so both `printf` and `echo` work as
        # stdin producers.
        stripped = value.strip()
        if stripped not in ("true", "false"):
            raise ConfigFileError("boolean config value must be 'true' or 'false'")
        typed_value = stripped == "true"
    document = _read_document(path)
    next_section = _next_table_section(document, section)
    insertion_value: str | bool | Item = typed_value
    table = _get_table(document, section)
    if table is None:
        table = tomlkit.table()
        document[section] = table
    elif key in table:
        current_value = table[key]
        if isinstance(current_value, bool):
            if not as_bool:
                raise ConfigFileError("config value is a boolean, not a string")
        elif isinstance(current_value, str):
            if as_bool:
                raise ConfigFileError("config value is a string, not a boolean")
        else:
            raise ConfigFileError("config value is not a string or boolean")
        key_index = _table_key_index(table, key)
        cut = _chapter_banner_cut(table, next_section)
        if cut is not None and key_index is not None and key_index > cut:
            # The key sits after the next chapter's banner comments, which
            # makes it look like it belongs to that chapter. Move it back.
            table[key] = typed_value
            insertion_value = table.item(key)
            table.remove(key)
        elif current_value == typed_value:
            return
        else:
            table[key] = typed_value
            _write_document(path, document)
            return

    _insert_table_value(table, key, insertion_value, next_section)
    _write_document(path, document)


def remove_config_value(path: Path, section: str, key: str) -> None:
    """Remove a scalar config value, doing nothing when the file or key is absent."""
    _validate_identifier("section", section)
    _validate_identifier("key", key)
    if not path.exists():
        return

    document = _read_document(path)
    table = _get_table(document, section)
    if table is None or key not in table:
        return

    value = table[key]
    if not isinstance(value, (str, bool)):
        raise ConfigFileError("config value is not a string or boolean")
    del table[key]
    _write_document(path, document)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read and update TUI TOML config values")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("get", "set", "set-bool", "remove"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--config", type=Path, required=True)
        command_parser.add_argument("--section", required=True)
        command_parser.add_argument("--key", required=True)
        if command == "set":
            command_parser.add_argument(
                "--bool",
                action="store_true",
                help="write the stdin value ('true' or 'false') as a TOML boolean",
            )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the TUI config helper command."""
    args = _parse_args(argv)
    try:
        if args.command == "get":
            value = get_config_value(args.config, args.section, args.key)
            if value is not None:
                sys.stdout.write(value)
        elif args.command in {"set", "set-bool"}:
            try:
                value = sys.stdin.buffer.read().decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConfigFileError("config value must be UTF-8 text") from exc
            set_config_value(
                args.config,
                args.section,
                args.key,
                value,
                as_bool=args.command == "set-bool" or args.bool,
            )
        else:
            remove_config_value(args.config, args.section, args.key)
    except ConfigFileError as exc:
        print(f"config file error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
