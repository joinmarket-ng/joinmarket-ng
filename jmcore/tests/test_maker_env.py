from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jmcore.maker_env import parse_maker_env


@pytest.mark.parametrize(
    "value",
    ["", "\n", "\n\n", "\none", "one\ntwo", "one\n", "one\n\n", "one\r\n", '\\"\n', "\u0085\u2028"],
)
def test_parser_and_cli_preserve_exact_values(tmp_path: Path, value: str) -> None:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    text = f'BIP39_PASSPHRASE="{escaped}"\n'
    assert parse_maker_env(text) == {"BIP39_PASSPHRASE": value}
    stage = tmp_path / ".maker.env"
    stage.write_bytes(text.encode())
    result = subprocess.run(
        [sys.executable, "-m", "jmcore.maker_env", str(stage), "BIP39_PASSPHRASE"],
        capture_output=True,
    )
    assert result.returncode == 0
    assert result.stdout == value.encode()
    assert result.stderr == b""


@pytest.mark.parametrize(
    "text",
    [
        'BIP39_PASSPHRASE="unfinished\n',
        'BIP39_PASSPHRASE="one"\nBIP39_PASSPHRASE="two"\n',
        'EXPECTED_FINGERPRINT="11223344"\nEXPECTED_FINGERPRINT="55667788"\n',
        "export BIP39_PASSPHRASE=secret\n",
        'BIP39_PASSPHRASE="unsupported\\n"\n',
    ],
)
def test_invalid_records_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="maker staging"):
        parse_maker_env(text)


def test_marker_text_in_quoted_records_is_not_an_assignment() -> None:
    assert parse_maker_env(
        'UNKNOWN="one\nEXPECTED_FINGERPRINT=11223344\n"\n'
        'BIP39_PASSPHRASE="one\nEXPECTED_FINGERPRINT=55667788\n"\n'
    ) == {"BIP39_PASSPHRASE": "one\nEXPECTED_FINGERPRINT=55667788\n"}


@pytest.mark.parametrize("state", ["missing-file", "missing-key", "invalid", "invalid-utf8"])
def test_cli_distinguishes_absence_from_invalid_staging(tmp_path: Path, state: str) -> None:
    stage = tmp_path / ".maker.env"
    if state != "missing-file":
        stage.write_bytes(
            {
                "missing-key": b'MNEMONIC_PASSWORD=""\n',
                "invalid": b"malformed",
                "invalid-utf8": b"\xff",
            }[state]
        )
    result = subprocess.run(
        [sys.executable, "-m", "jmcore.maker_env", str(stage), "BIP39_PASSPHRASE"],
        capture_output=True,
    )
    assert result.returncode == (1 if state.startswith("missing") else 2)
    assert result.stdout == b""
