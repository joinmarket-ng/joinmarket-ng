"""Test P2TR change output script generation.

Validates the integration of taproot_tweak_pubkey with create_p2tr_scriptpubkey
for generating P2TR change outputs, which is the code path used in send.py.
"""

from __future__ import annotations

from coincurve import PrivateKey
from jmcore.bitcoin import (
    create_p2tr_scriptpubkey,
    is_p2tr_address,
    scriptpubkey_to_address,
    taproot_tweak_pubkey,
)


def test_p2tr_change_output_script():
    """Verify that unpacking taproot_tweak_pubkey and passing to
    create_p2tr_scriptpubkey produces a valid P2TR scriptpubkey.

    This replicates the code path in send.py where the
    change output is generated for P2TR wallets.
    """
    # Simulate what send.py does: get compressed pubkey, strip prefix
    priv = PrivateKey(
        bytes.fromhex("0101010101010101010101010101010101010101010101010101010101010101")
    )
    compressed_pubkey = priv.public_key.format(compressed=True)
    x_only = compressed_pubkey[1:]  # Strip 02/03 prefix

    # This must return a tuple (y_parity, tweaked_x_only)
    result = taproot_tweak_pubkey(x_only)
    assert isinstance(result, tuple), "taproot_tweak_pubkey must return a tuple"
    assert len(result) == 2, "taproot_tweak_pubkey must return (y_parity, tweaked_x_only)"

    y_parity, tweaked_x_only = result
    assert isinstance(y_parity, int), "y_parity must be an int"
    assert isinstance(tweaked_x_only, bytes), "tweaked_x_only must be bytes"
    assert len(tweaked_x_only) == 32, "tweaked_x_only must be 32 bytes"

    change_script = create_p2tr_scriptpubkey(tweaked_x_only)

    # Validate it's a proper P2TR scriptPubKey (OP_1 PUSH32 <32-byte key>)
    assert len(change_script) == 34, "P2TR scriptPubKey must be 34 bytes"
    assert change_script[0] == 0x51, "P2TR scriptPubKey must start with OP_1"
    assert change_script[1] == 0x20, "P2TR scriptPubKey PUSH32"
    assert change_script[2:] == tweaked_x_only

    # Verify it resolves to a valid P2TR address
    address = scriptpubkey_to_address(change_script, "mainnet")
    assert is_p2tr_address(address), f"Expected P2TR address, got: {address}"
    assert address.startswith("bc1p"), f"Expected bc1p prefix, got: {address}"


def test_p2tr_change_output_hex_vs_bytes():
    """Verify create_p2tr_scriptpubkey accepts both hex string and bytes."""
    priv = PrivateKey(
        bytes.fromhex("0202020202020202020202020202020202020202020202020202020202020202")
    )
    x_only = priv.public_key.format(compressed=True)[1:]
    _, tweaked_x_only = taproot_tweak_pubkey(x_only)

    # Both should produce the same scriptPubKey
    spk_from_bytes = create_p2tr_scriptpubkey(tweaked_x_only)
    spk_from_hex = create_p2tr_scriptpubkey(tweaked_x_only.hex())
    assert spk_from_bytes == spk_from_hex
