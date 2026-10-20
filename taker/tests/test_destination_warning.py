"""
Tests for the destination script-type mismatch warning (issue #113).

When a taker sends a CoinJoin to a destination whose script type does not
match the wallet's native script type (jm-ng wallets are p2wpkh), the mixed
output types fingerprint the taker output and shrink the effective anonymity
set. The taker should warn (not abort) so non-interactive flows still work.
"""

from __future__ import annotations

from collections.abc import Iterator

import base58 as b58
import bech32 as bech32_lib
import pytest
from bitcointx.segwit_addr import encode
from loguru import logger

from taker.taker import warn_if_destination_script_mismatch


@pytest.fixture
def captured_warnings() -> Iterator[list[str]]:
    """Capture WARNING-level loguru messages emitted during the test."""
    messages: list[str] = []
    sink_id = logger.add(
        lambda msg: messages.append(msg.record["message"]),
        level="WARNING",
    )
    yield messages
    logger.remove(sink_id)


def _make_p2wpkh() -> str:
    addr = bech32_lib.encode("bcrt", 0, b"\xaa" * 20)
    assert addr is not None
    return addr


def _make_p2tr() -> str:
    addr = encode("bcrt", 1, b"\xcd" * 32)
    assert addr is not None
    return addr


def _make_p2pkh() -> str:
    return b58.b58encode_check(bytes([0x00]) + b"\xee" * 20).decode("ascii")


def _make_p2sh() -> str:
    return b58.b58encode_check(bytes([0x05]) + b"\xff" * 20).decode("ascii")


def test_p2wpkh_destination_emits_no_warning(captured_warnings: list[str]) -> None:
    """Matching script type: no warning, returns None."""
    result = warn_if_destination_script_mismatch(_make_p2wpkh())
    assert result is None
    assert captured_warnings == []


def test_p2tr_destination_warns(captured_warnings: list[str]) -> None:
    """Taproot destination from a p2wpkh wallet should warn."""
    addr = _make_p2tr()
    result = warn_if_destination_script_mismatch(addr)
    assert result == "p2tr"
    assert len(captured_warnings) == 1
    assert "p2tr" in captured_warnings[0]
    assert addr in captured_warnings[0]
    assert "fingerprint" in captured_warnings[0].lower()


def test_p2pkh_destination_warns(captured_warnings: list[str]) -> None:
    """Legacy p2pkh destination should warn."""
    addr = _make_p2pkh()
    result = warn_if_destination_script_mismatch(addr)
    assert result == "p2pkh"
    assert len(captured_warnings) == 1
    assert "p2pkh" in captured_warnings[0]


def test_p2sh_destination_warns(captured_warnings: list[str]) -> None:
    """p2sh (including p2sh-p2wpkh) destination should warn."""
    addr = _make_p2sh()
    result = warn_if_destination_script_mismatch(addr)
    assert result == "p2sh"
    assert len(captured_warnings) == 1
    assert "p2sh" in captured_warnings[0]


def test_invalid_address_is_silent(captured_warnings: list[str]) -> None:
    """Unparseable address: silent here; canonical error raised later in pipeline."""
    result = warn_if_destination_script_mismatch("not_a_valid_address")
    assert result is None
    assert captured_warnings == []
