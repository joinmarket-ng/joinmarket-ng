"""Tests for the standalone signed bond PSBT finalizer."""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from finalize_bond_psbt import (  # noqa: E402
    _encode_varint,
    _extract_freeze_script_pubkey,
    _read_psbt_pair,
    _read_varint,
    finalize_bond_psbt,
    parse_signed_bond_psbt,
)

SIGNED_SPECTER_PSBT_B64 = (
    "cHNidP8BAFICAAAAATE7hixB8V/y7oXB1Ckyveg+WIPrW0RlxtFNPdv3o6BUAQAAAAD+////"
    "AdBBDwAAAAAAFgAUr8yGDmmY3JWBeIXYdqhZO4fpB4MAuVVpAAEBK0BCDwAAAAAAIgAg6WSb"
    "rbiM8Zvs3u3QwM5l56zhJdTDwpQnSzvGG3PlqeEiAgIJ014SVJgJ37nyNaD9IVJ7d042rAku"
    "z0/0N22Q2dWMBEcwRAIgWYcBBSQzdRvnuXhSFzSa+oczdHEGFS2Vbe3e+YHrNrQCIFe8omA0"
    "wStZo2SmHImP0gzc/zVrqM0zUSDns6rY04klAQEDBAEAAAABBSoEALlVabF1IQIJ014SVJgJ"
    "37nyNaD9IVJ7d042rAkuz0/0N22Q2dWMBKwAAA=="
)

WITNESS_SCRIPT_HEX = (
    "0400b95569b175210209d35e12549809dfb9f235a0fd21527b774e36ac092ecf4ff4376"
    "d90d9d58c04ac"
)


def _remove_partial_signature(psbt_b64: str) -> str:
    raw = base64.b64decode(psbt_b64)
    pos = 5

    # Copy global map through separator.
    while True:
        key, _, next_pos = _read_psbt_pair(raw, pos)
        pos = next_pos
        if key is None:
            break

    output = bytearray(raw[:pos])

    # Copy input map, skipping partial signature pairs.
    while True:
        pair_start = pos
        key, _, next_pos = _read_psbt_pair(raw, pos)
        pos = next_pos
        if key is None:
            output.append(0)
            break
        if key[0] != 0x02:
            output.extend(raw[pair_start:next_pos])

    output.extend(raw[pos:])
    return base64.b64encode(output).decode()


def _replace_partial_signature_pubkey(psbt_b64: str, pubkey: bytes) -> str:
    raw = base64.b64decode(psbt_b64)
    pos = 5

    while True:
        key, _, next_pos = _read_psbt_pair(raw, pos)
        pos = next_pos
        if key is None:
            break

    output = bytearray(raw[:pos])

    while True:
        pair_start = pos
        key, value, next_pos = _read_psbt_pair(raw, pos)
        pos = next_pos
        if key is None:
            output.append(0)
            break
        if key[0] == 0x02:
            assert value is not None
            new_key = b"\x02" + pubkey
            output.extend(_encode_varint(len(new_key)) + new_key)
            output.extend(_encode_varint(len(value)) + value)
        else:
            output.extend(raw[pair_start:next_pos])

    output.extend(raw[pos:])
    return base64.b64encode(output).decode()


def _remove_first_separator(psbt_b64: str) -> str:
    raw = base64.b64decode(psbt_b64)
    pos = 5

    while True:
        key, _, next_pos = _read_psbt_pair(raw, pos)
        pos = next_pos
        if key is None:
            return base64.b64encode(raw[: pos - 1]).decode()


class TestParseSignedBondPSBT:
    def test_extracts_signed_bond_fields(self) -> None:
        result = parse_signed_bond_psbt(SIGNED_SPECTER_PSBT_B64)

        assert result["witness_script"].hex() == WITNESS_SCRIPT_HEX
        assert result["signature"][-1] == 0x01
        assert result["unsigned_tx"].hex().startswith("02000000")

    def test_unsigned_psbt_raises(self) -> None:
        with pytest.raises(ValueError, match="partial signature"):
            parse_signed_bond_psbt(_remove_partial_signature(SIGNED_SPECTER_PSBT_B64))

    def test_witness_script_mismatch_raises(self) -> None:
        raw = bytearray(base64.b64decode(SIGNED_SPECTER_PSBT_B64))
        needle = hashlib.sha256(bytes.fromhex(WITNESS_SCRIPT_HEX)).digest()
        offset = bytes(raw).index(needle)
        raw[offset] ^= 0x01

        with pytest.raises(ValueError, match="does not match"):
            parse_signed_bond_psbt(base64.b64encode(raw).decode())

    def test_partial_signature_pubkey_mismatch_raises(self) -> None:
        wrong_pubkey = bytes.fromhex("03" + "11" * 32)
        psbt = _replace_partial_signature_pubkey(SIGNED_SPECTER_PSBT_B64, wrong_pubkey)

        with pytest.raises(ValueError, match="pubkey does not match"):
            parse_signed_bond_psbt(psbt)

    def test_truncated_psbt_raises_value_error(self) -> None:
        raw = base64.b64decode(SIGNED_SPECTER_PSBT_B64)
        truncated = base64.b64encode(raw[:-7]).decode()

        with pytest.raises(ValueError, match="truncated|Truncated|Unexpected end"):
            parse_signed_bond_psbt(truncated)

    def test_missing_global_separator_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="global map is truncated"):
            parse_signed_bond_psbt(_remove_first_separator(SIGNED_SPECTER_PSBT_B64))

    def test_truncated_witness_script_raises_value_error(self) -> None:
        truncated_script = bytes.fromhex(WITNESS_SCRIPT_HEX)[:7]

        with pytest.raises(ValueError, match="truncated|missing"):
            _extract_freeze_script_pubkey(truncated_script)

    def test_line_wrapped_psbt_is_accepted(self) -> None:
        wrapped = "\n".join(
            SIGNED_SPECTER_PSBT_B64[i : i + 64]
            for i in range(0, len(SIGNED_SPECTER_PSBT_B64), 64)
        )

        assert parse_signed_bond_psbt(wrapped)["witness_script"].hex() == WITNESS_SCRIPT_HEX


class TestFinalizeBondPSBT:
    def test_finalize_outputs_witness_transaction(self) -> None:
        signed_hex = finalize_bond_psbt(SIGNED_SPECTER_PSBT_B64)
        signed = bytes.fromhex(signed_hex)

        assert signed[4] == 0x00
        assert signed[5] == 0x01

        offset = 6
        input_count, offset = _read_varint(signed, offset)
        assert input_count == 1
        offset += 32 + 4
        script_len, offset = _read_varint(signed, offset)
        offset += script_len + 4

        output_count, offset = _read_varint(signed, offset)
        assert output_count == 1
        offset += 8
        output_script_len, offset = _read_varint(signed, offset)
        offset += output_script_len

        witness_items, offset = _read_varint(signed, offset)
        assert witness_items == 2

        sig_len, offset = _read_varint(signed, offset)
        signature = signed[offset : offset + sig_len]
        offset += sig_len
        assert signature[-1] == 0x01

        script_len, offset = _read_varint(signed, offset)
        witness_script = signed[offset : offset + script_len]
        assert witness_script.hex() == WITNESS_SCRIPT_HEX

    def test_finalized_tx_has_expected_hex(self) -> None:
        assert finalize_bond_psbt(SIGNED_SPECTER_PSBT_B64) == (
            "02000000000101313b862c41f15ff2ee85c1d42932bde83e5883eb5b4465c6d14d3d"
            "dbf7a3a0540100000000feffffff01d0410f0000000000160014afcc860e6998dc95"
            "817885d876a8593b87e90783024730440220598701052433751be7b9785217349afa"
            "8733747106152d956deddef981eb36b4022057bca26034c12b59a364a61c898fd20"
            "cdcff356ba8cd335120e7b3aad8d38925012a0400b95569b175210209d35e125498"
            "09dfb9f235a0fd21527b774e36ac092ecf4ff4376d90d9d58c04ac00b95569"
        )
