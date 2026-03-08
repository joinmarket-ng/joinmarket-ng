import pytest

from jmcore.bitcoin import (
    create_p2tr_scriptpubkey,
    get_address_type,
    pubkey_to_p2tr_address,
    scriptpubkey_to_address,
    taproot_tweak_pubkey,
)


def test_taproot_tweak_and_address():
    # Test vector from BIP341 tests
    internal_pubkey = bytes.fromhex(
        "d6889cb081036e0faefa3a35157ad71086b123b2b144b649798b494c300a961d"
    )

    y_parity, tweaked_x_coord = taproot_tweak_pubkey(internal_pubkey)

    # Expected tweaked public key (x coord only)
    expected_tweaked_x_coord = bytes.fromhex(
        "53a1f6e454df1aa2776a2814a721372d6258050de330b3c6d10ee8f4e0dda343"
    )
    assert tweaked_x_coord == expected_tweaked_x_coord

    # Expected P2TR address
    expected_address = "bc1p2wsldez5mud2yam29q22wgfh9439spgduvct83k3pm50fcxa5dps59h4z5"

    # Direct encoding
    addr = pubkey_to_p2tr_address(tweaked_x_coord, "mainnet")
    assert addr == expected_address

    # Through scriptPubKey
    spk = create_p2tr_scriptpubkey(tweaked_x_coord)
    assert spk == bytes.fromhex(
        "512053a1f6e454df1aa2776a2814a721372d6258050de330b3c6d10ee8f4e0dda343"
    )

    addr_from_spk = scriptpubkey_to_address(spk, "mainnet")
    assert addr_from_spk == expected_address


def test_get_address_type_p2tr_mainnet():
    """get_address_type must recognise bech32m P2TR addresses."""
    addr = "bc1p2wsldez5mud2yam29q22wgfh9439spgduvct83k3pm50fcxa5dps59h4z5"
    assert get_address_type(addr) == "p2tr"


def test_get_address_type_p2tr_regtest():
    """get_address_type must recognise regtest P2TR (bcrt1p...) addresses."""
    # Derive a regtest P2TR address from the same test vector
    tweaked_x_coord = bytes.fromhex(
        "53a1f6e454df1aa2776a2814a721372d6258050de330b3c6d10ee8f4e0dda343"
    )
    addr = pubkey_to_p2tr_address(tweaked_x_coord, "regtest")
    assert addr.startswith("bcrt1p")
    assert get_address_type(addr) == "p2tr"


def test_get_address_type_p2tr_testnet():
    """get_address_type must recognise testnet P2TR (tb1p...) addresses."""
    tweaked_x_coord = bytes.fromhex(
        "53a1f6e454df1aa2776a2814a721372d6258050de330b3c6d10ee8f4e0dda343"
    )
    addr = pubkey_to_p2tr_address(tweaked_x_coord, "testnet")
    assert addr.startswith("tb1p")
    assert get_address_type(addr) == "p2tr"


def test_get_address_type_invalid_address():
    """get_address_type must raise ValueError for invalid addresses."""
    with pytest.raises(ValueError):
        get_address_type("bc1pinvalidaddress")
