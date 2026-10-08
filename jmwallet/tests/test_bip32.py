"""
Tests for BIP32 HD key derivation.
"""

import pytest
from bitcointx.core.key import CKey, CPubKey

from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed


def test_mnemonic_to_seed(test_mnemonic):
    seed = mnemonic_to_seed(test_mnemonic)
    assert len(seed) == 64
    assert isinstance(seed, bytes)


def test_hdkey_from_seed(test_mnemonic):
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    assert master_key.depth == 0
    assert len(master_key.chain_code) == 32
    assert isinstance(master_key.private_key, CKey)
    assert isinstance(master_key.public_key, CPubKey)


def test_hdkey_derivation(test_mnemonic):
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    child = master_key.derive("m/84'/0'/0'/0/0")

    assert child.depth == 5
    assert isinstance(child.private_key, CKey)

    privkey_bytes = child.get_private_key_bytes()
    assert len(privkey_bytes) == 32

    pubkey_bytes = child.get_public_key_bytes(compressed=True)
    assert len(pubkey_bytes) == 33


def test_hardened_derivation(test_mnemonic):
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    hardened = master_key.derive("m/84'")
    assert hardened.depth == 1

    combined = master_key.derive("m/84'/0")
    assert combined.depth == 2


def test_address_generation(test_mnemonic):
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    key = master_key.derive("m/84'/0'/0'/0/0")
    address = key.get_address("regtest")

    assert address.startswith("bcrt1")
    assert len(address) > 20


def test_xpub_serialization(test_mnemonic):
    """Test that xpub serialization produces valid Base58Check output."""
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    # Derive to account level (m/84'/0'/0')
    account_key = master_key.derive("m/84'/0'/0'")

    # Test mainnet xpub
    xpub = account_key.get_xpub("mainnet")
    assert xpub.startswith("xpub")
    # BIP32 serialized keys are 78 bytes, which encodes to 111-112 Base58 chars
    assert len(xpub) >= 100

    # Test testnet tpub
    tpub = account_key.get_xpub("testnet")
    assert tpub.startswith("tpub")
    assert len(tpub) >= 100


def test_zpub_serialization(test_mnemonic):
    """Test that zpub serialization produces valid Base58Check output for BIP84."""
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    # Derive to account level (m/84'/0'/0')
    account_key = master_key.derive("m/84'/0'/0'")

    # Test mainnet zpub
    zpub = account_key.get_zpub("mainnet")
    assert zpub.startswith("zpub")
    # BIP32 serialized keys are 78 bytes, which encodes to 111-112 Base58 chars
    assert len(zpub) >= 100

    # Test testnet vpub
    vpub = account_key.get_zpub("testnet")
    assert vpub.startswith("vpub")
    assert len(vpub) >= 100


def test_xpub_zpub_difference(test_mnemonic):
    """Test that xpub and zpub are different for the same key."""
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    # Derive to account level
    account_key = master_key.derive("m/84'/0'/0'")

    xpub = account_key.get_xpub("mainnet")
    zpub = account_key.get_zpub("mainnet")

    # They should be different (different version bytes)
    assert xpub != zpub
    assert xpub.startswith("xpub")
    assert zpub.startswith("zpub")
    # But same length (both encode 78 bytes)
    assert len(xpub) == len(zpub)


def test_xprv_serialization(test_mnemonic):
    """Test that xprv serialization produces valid Base58Check output."""
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    # Derive to account level
    account_key = master_key.derive("m/84'/0'/0'")

    # Test mainnet xprv
    xprv = account_key.get_xprv("mainnet")
    assert xprv.startswith("xprv")
    assert len(xprv) >= 100

    # Test testnet tprv
    tprv = account_key.get_xprv("testnet")
    assert tprv.startswith("tprv")
    assert len(tprv) >= 100


def test_serialization_and_signature_compatibility_vector(test_mnemonic):
    """Serialized keys and deterministic signatures remain wire-compatible."""
    seed = mnemonic_to_seed(test_mnemonic)
    key = HDKey.from_seed(seed).derive("m/84'/0'/0'/0/0")

    assert key.get_xpub() == (
        "xpub6FrCS2gWHvogbAX8ipHuBmbPvckXLYs5SfEKq1Lp3tneESUXuNNUw67q6Q6r1xHhmoQtByXS7SXes78nuGckLXWEuRPWNfwBo8Cp5QQLPKy"
    )
    assert key.get_xprv() == (
        "xprvA2rr2X9cTZFPNgSfcnktpdefNav2w69E5SJj2cwCVZFfMe9PMq4EPHoMF7HfYYFwxDC3L98KUdGmhmamCAifxPbtSyLq9pFQJnf3AyC2JJD"
    )
    assert key.sign(b"HDKey signature compatibility vector") == bytes.fromhex(
        "30440220234c5591ad333a71d823cdcf3407c63115967766e45be991693459340a07d06e"
        "02203803fe92d04db1c02351083e6e65bda0a87532b662d360434cfa61daa2e09ad4"
    )


def test_fingerprint(test_mnemonic):
    """Test that fingerprints are calculated correctly."""
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    # Master key fingerprint
    fp = master_key.fingerprint
    assert len(fp) == 4
    assert isinstance(fp, bytes)

    # Child key should have parent's fingerprint
    child = master_key.derive("m/84'")
    assert child.parent_fingerprint == master_key.fingerprint


def test_fingerprint_bip32_vector() -> None:
    """BIP32 test vector 1 fixes the master and first hardened child fingerprints."""
    master_key = HDKey.from_seed(bytes(range(16)))
    child = master_key.derive("m/0'")

    assert master_key.fingerprint.hex() == "3442193e"
    assert child.fingerprint.hex() == "5c1bd648"
    assert child.parent_fingerprint == master_key.fingerprint


def test_child_number_tracking(test_mnemonic):
    """Test that child number is tracked correctly through derivation."""
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    # Master key has child_number 0
    assert master_key.child_number == 0

    # Hardened derivation at index 84 -> child_number = 84 + 0x80000000
    child = master_key.derive("m/84'")
    assert child.child_number == 84 + 0x80000000

    # Non-hardened derivation at index 0
    grandchild = child._derive_child(0)
    assert grandchild.child_number == 0


def test_derive_child_rejects_offset_out_of_range(test_mnemonic, monkeypatch):
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)

    import hmac

    class _FakeDigest:
        def digest(self) -> bytes:
            return (2**256 - 1).to_bytes(32, "big") + b"\x00" * 32

    def fake_new(*args, **kwargs):
        return _FakeDigest()

    monkeypatch.setattr(hmac, "new", fake_new)

    with pytest.raises(ValueError, match="Invalid child key"):
        master_key._derive_child(0)


def test_extended_key_serialization_rejects_depth_over_255(test_mnemonic):
    seed = mnemonic_to_seed(test_mnemonic)
    master_key = HDKey.from_seed(seed)
    master_key.depth = 256

    with pytest.raises(ValueError, match="depth > 255"):
        master_key.get_xpub("mainnet")

    with pytest.raises(ValueError, match="depth > 255"):
        master_key.get_zpub("mainnet")

    with pytest.raises(ValueError, match="depth > 255"):
        master_key.get_xprv("mainnet")
