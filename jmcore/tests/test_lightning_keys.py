"""Managed Lightning node key derivation (a persisted-identity contract)."""

from __future__ import annotations

import base58
import pytest
from mnemonic import Mnemonic

from jmcore.lightning_keys import (
    LightningKeyError,
    NodeKeyScope,
    WalletLightningKeys,
    bip32_master_key,
    new_epoch,
    slip21_node,
)

# SLIP-0021 test vectors use the "all all ... all" mnemonic without passphrase.
SEED = Mnemonic.to_seed(" ".join(["all"] * 12), "")
EPOCH = "0123456789abcdef"


def test_slip21_matches_the_specification_vectors() -> None:
    assert slip21_node(SEED, ())[32:].hex() == (
        "dbf12b44133eaab506a740f6565cc117228cbf1dd70635cfa8ddfdc9af734756"
    )
    assert slip21_node(SEED, (b"SLIP-0021", b"Master encryption key"))[32:].hex() == (
        "ea163130e35bbafdf5ddee97a17b39cef2be4b4f390180d65b54cf05c6a82fde"
    )


def test_bip32_master_key_matches_bip32_test_vector_1() -> None:
    assert bip32_master_key(bytes.fromhex("000102030405060708090a0b0c0d0e0f"), "mainnet") == (
        "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi"
    )


def test_node_derivation_is_pinned() -> None:
    """Changing these values would change the identity of every existing node."""
    keys = WalletLightningKeys(SEED, "signet")
    scope = NodeKeyScope("signet", EPOCH, 2, 1)

    assert keys.node_master_key(scope) == (
        "tprv8ZgxMBicQKsPesm6pDGUALm4k8BPJrXiMAAZNsWhdn7kESC9xWsCdJHNCK7Nigegm27rtUoiDmS5yrhkCDq4G3jJyLM1tBNHQqE4Lx4taCx"
    )
    assert keys.unlock_password(scope) == (
        b"401e1dc62584ebd6a2a7f78366a13f685cb2043bdbbd93cff6c5da410d0d5349"
    )
    assert keys.backup_key().hex() == (
        "327b6858302d1846201ba6716a1839bc047eb7660380615d35078d407a73c734"
    )


def test_every_scope_field_separates_identities() -> None:
    signet = WalletLightningKeys(SEED, "signet")
    regtest = WalletLightningKeys(SEED, "regtest")
    base = NodeKeyScope("signet", EPOCH, 0, 0)
    variants = [
        NodeKeyScope("signet", new_epoch(), 0, 0),
        NodeKeyScope("signet", EPOCH, 1, 0),
        NodeKeyScope("signet", EPOCH, 0, 1),
    ]
    keys = {signet.node_master_key(scope) for scope in [base, *variants]}
    keys.add(regtest.node_master_key(NodeKeyScope("regtest", EPOCH, 0, 0)))

    assert len(keys) == 5
    assert signet.unlock_password(base).decode() not in keys


def test_master_key_is_a_depth_zero_testnet_key_off_mainnet() -> None:
    payload = base58.b58decode_check(
        WalletLightningKeys(SEED, "regtest").node_master_key(NodeKeyScope("regtest", EPOCH, 0, 0))
    )

    assert payload[:4] == bytes.fromhex("04358394")
    assert payload[4:13] == b"\x00" * 9


@pytest.mark.parametrize(
    ("epoch", "mixdepth", "generation"),
    [("0123", 0, 0), ("0123456789ABCDEF", 0, 0), (EPOCH, 10, 0), (EPOCH, 0, 1000), (EPOCH, -1, 0)],
)
def test_out_of_range_scopes_are_rejected(epoch: str, mixdepth: int, generation: int) -> None:
    with pytest.raises(LightningKeyError):
        NodeKeyScope("signet", epoch, mixdepth, generation)


def test_network_mismatch_and_close_refuse_derivation() -> None:
    keys = WalletLightningKeys(SEED, "signet")
    with pytest.raises(LightningKeyError, match="network"):
        keys.node_master_key(NodeKeyScope("mainnet", EPOCH, 0, 0))

    keys.close()
    with pytest.raises(LightningKeyError, match="closed"):
        keys.unlock_password(NodeKeyScope("signet", EPOCH, 0, 0))
