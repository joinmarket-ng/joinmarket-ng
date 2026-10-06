"""Parse the systemd assignment subset emitted by the TUI without executing it."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

KEYS = {"MNEMONIC_PASSWORD", "BIP39_PASSPHRASE", "EXPECTED_FINGERPRINT"}


def parse_maker_env(text: str) -> dict[str, str]:
    """Return decoded credentials, preserving whitespace inside quoted values.

    Callers own file access policy. This parser neither imports environment
    variables nor treats a missing credential as an empty one.
    """
    values: dict[str, str] = {}
    lines = iter(text.split("\n"))
    for line in lines:
        line = line.lstrip()
        if not line or line.startswith(("#", ";")):
            continue
        assignment = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if assignment is None:
            raise ValueError("Malformed maker staging. Restage credentials.")
        name, value = assignment.groups()
        if value.startswith('"'):
            while not re.fullmatch(r'"(?:[^"\\]|\\["\\])*"', value.rstrip()):
                continuation = next(lines, None)
                if continuation is None:
                    raise ValueError("Ambiguous maker staging. Restage credentials.")
                value += "\n" + continuation
        value = value.rstrip()
        if name not in KEYS:
            continue
        if name in values or not re.fullmatch(r'"(?:[^"\\]|\\["\\])*"|[^\s"\\]*', value):
            raise ValueError("Ambiguous maker staging. Restage credentials.")
        if value.startswith('"'):
            value = re.sub(r'\\(["\\])', r"\1", value[1:-1])
        values[name] = value
    return values


def main() -> int:
    """Read one value for shell callers (exit 1 means absent, 2 means invalid)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("key", choices=sorted(KEYS))
    args = parser.parse_args()
    try:
        values = parse_maker_env(args.path.read_bytes().decode("utf-8"))
    except FileNotFoundError:
        return 1
    except (OSError, UnicodeError, ValueError):
        print("Cannot read maker staging. Restage credentials.", file=sys.stderr)
        return 2
    if args.key not in values:
        return 1
    sys.stdout.write(values[args.key])
    return 0


if __name__ == "__main__":
    sys.exit(main())
