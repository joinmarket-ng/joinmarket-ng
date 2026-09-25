"""BIP173/BIP350 checksum and case rules at both public address boundaries."""

from __future__ import annotations

import bech32
import pytest
from bitcointx.segwit_addr import encode

from jmcore.bitcoin import address_to_scriptpubkey, bech32m_encode, get_address_type


@pytest.mark.parametrize("hrp", ["tb", "bcrt"])
@pytest.mark.parametrize(
    ("version", "length", "kind"), [(0, 20, "p2wpkh"), (0, 32, "p2wsh"), (1, 32, "p2tr")]
)
@pytest.mark.parametrize("uppercase", [False, True])
def test_valid_segwit_address(
    hrp: str, version: int, length: int, kind: str, uppercase: bool
) -> None:
    program = bytes(range(length))
    address = encode(hrp, version, program)
    assert address is not None
    if uppercase:
        address = address.upper()
    opcode = 0 if version == 0 else 0x51
    assert address_to_scriptpubkey(address) == bytes([opcode, length]) + program
    assert get_address_type(address) == kind


@pytest.mark.parametrize("hrp", ["tb", "bcrt"])
@pytest.mark.parametrize(("version", "length"), [(0, 20), (0, 32), (1, 32)])
def test_wrong_checksum_variant_is_rejected(hrp: str, version: int, length: int) -> None:
    program = bytes(range(length))
    address = (
        bech32m_encode(hrp, version, program)
        if version == 0
        else bech32.encode(hrp, version, program)
    )
    assert address is not None
    for parser in (address_to_scriptpubkey, get_address_type):
        with pytest.raises(ValueError):
            parser(address)


@pytest.mark.parametrize("hrp", ["tb", "bcrt"])
@pytest.mark.parametrize(("version", "length"), [(0, 20), (0, 32), (1, 32)])
def test_mixed_case_is_rejected(hrp: str, version: int, length: int) -> None:
    address = encode(hrp, version, bytes(range(length)))
    assert address is not None
    # The HRP stays lowercase while the data is uppercase. Its checksum would
    # still match if a decoder silently lowercased the entire string.
    address = hrp + "1" + address[len(hrp) + 1 :].upper()
    assert address != address.lower()
    for parser in (address_to_scriptpubkey, get_address_type):
        with pytest.raises(ValueError):
            parser(address)
