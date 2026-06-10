"""
Tests for the dual-type wallet capability.

A WalletService has a primary ``address_type`` (P2WPKH or P2TR) but can also
derive, resolve and sign coins of the *other* family. This lets a maker serve
and later spend a CoinJoin output of whichever equal-output type the taker
requested, regardless of the maker's primary wallet type.
"""

from unittest.mock import AsyncMock

import pytest
from jmcore.bitcoin import create_p2tr_scriptpubkey

from jmwallet.wallet.service import WalletService


def _wallet(test_mnemonic, address_type):
    backend = AsyncMock()
    return WalletService(test_mnemonic, backend, network="regtest", address_type=address_type)


def test_secondary_type_is_the_other_family(test_mnemonic):
    p2wpkh = _wallet(test_mnemonic, "p2wpkh")
    p2tr = _wallet(test_mnemonic, "p2tr")
    assert p2wpkh.address_type == "p2wpkh"
    assert p2wpkh.secondary_address_type == "p2tr"
    assert p2tr.address_type == "p2tr"
    assert p2tr.secondary_address_type == "p2wpkh"


def test_get_address_primary_unchanged(test_mnemonic):
    """Omitting script_type yields the primary-type address as before."""
    wallet = _wallet(test_mnemonic, "p2wpkh")
    default = wallet.get_address(0, 0, 0)
    primary = wallet.get_address(0, 0, 0, "p2wpkh")
    assert default == primary
    assert default.startswith("bcrt1q")


def test_get_address_secondary_type(test_mnemonic):
    """A P2WPKH wallet can derive a P2TR address at the same path."""
    wallet = _wallet(test_mnemonic, "p2wpkh")
    p2wpkh_addr = wallet.get_address(0, 1, 3, "p2wpkh")
    p2tr_addr = wallet.get_address(0, 1, 3, "p2tr")
    assert p2wpkh_addr.startswith("bcrt1q")
    assert p2tr_addr.startswith("bcrt1p")
    assert p2wpkh_addr != p2tr_addr
    # Both are recorded with their respective script types.
    assert wallet._address_script_type[p2wpkh_addr] == "p2wpkh"
    assert wallet._address_script_type[p2tr_addr] == "p2tr"


def test_get_change_address_accepts_script_type(test_mnemonic):
    wallet = _wallet(test_mnemonic, "p2wpkh")
    addr = wallet.get_change_address(2, 5, "p2tr")
    assert addr.startswith("bcrt1p")
    assert wallet._address_script_type[addr] == "p2tr"


def test_key_resolution_uses_correct_purpose_root(test_mnemonic):
    """get_key_for_address derives a secondary P2TR coin under m/86'."""
    wallet = _wallet(test_mnemonic, "p2wpkh")
    p2tr_addr = wallet.get_address(0, 0, 0, "p2tr")
    key = wallet.get_key_for_address(p2tr_addr)
    assert key is not None
    # The derived key's BIP86 output key must produce the same P2TR address.
    output_xonly = key.get_p2tr_output_xonly()
    assert create_p2tr_scriptpubkey(output_xonly) == bytes([0x51, 0x20]) + output_xonly
    assert key.get_p2tr_address(wallet.network) == p2tr_addr

    # And the P2WPKH address at the same path resolves under m/84'.
    p2wpkh_addr = wallet.get_address(0, 0, 0, "p2wpkh")
    key_w = wallet.get_key_for_address(p2wpkh_addr)
    assert key_w is not None
    assert key_w.get_address(wallet.network) == p2wpkh_addr


def test_resolve_p2tr_signing_key_for_secondary_coin(test_mnemonic):
    """A P2WPKH wallet can resolve the BIP86 key for a secondary P2TR output."""
    wallet = _wallet(test_mnemonic, "p2wpkh")
    p2tr_addr = wallet.get_address(0, 1, 7, "p2tr")
    resolved = wallet.resolve_p2tr_signing_key(p2tr_addr)
    assert resolved is not None
    private_key, output_xonly = resolved
    # The signing key's public key is the tweaked taproot output key.
    assert private_key.public_key.format(compressed=True)[1:] == output_xonly
    assert len(output_xonly) == 32


def test_scan_descriptors_dual_family(test_mnemonic):
    """Dual-type scan descriptors include both wpkh() and tr() families."""
    wallet = _wallet(test_mnemonic, "p2wpkh")
    descriptors = wallet.get_scan_descriptors(scan_range=10)
    descs = [d["desc"] for d in descriptors]
    assert any(d.startswith("wpkh(") for d in descs)
    assert any(d.startswith("tr(") for d in descs)
    # Two families x two chains x mixdepth_count
    assert len(descriptors) == wallet.mixdepth_count * 2 * 2


def test_scan_descriptors_single_family_when_disabled(test_mnemonic):
    wallet = _wallet(test_mnemonic, "p2tr")
    descriptors = wallet.get_scan_descriptors(scan_range=10, dual_type=False)
    descs = [d["desc"] for d in descriptors]
    assert all(d.startswith("tr(") for d in descs)
    assert len(descriptors) == wallet.mixdepth_count * 2


def test_account_xpub_differs_by_type(test_mnemonic):
    wallet = _wallet(test_mnemonic, "p2wpkh")
    assert wallet.get_account_xpub(0, "p2wpkh") != wallet.get_account_xpub(0, "p2tr")
    # Default matches primary.
    assert wallet.get_account_xpub(0) == wallet.get_account_xpub(0, "p2wpkh")


def test_get_address_rejects_unknown_script_type(test_mnemonic):
    wallet = _wallet(test_mnemonic, "p2wpkh")
    with pytest.raises(ValueError, match="Unsupported script_type"):
        wallet.get_address(0, 0, 0, "p2sh")
