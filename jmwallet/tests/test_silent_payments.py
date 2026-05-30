"""Tests for BIP352 silent payment wallet integration."""

from __future__ import annotations

import pytest
from _jmwallet_test_helpers import TEST_MNEMONIC
from coincurve import PublicKey
from jmcore.silentpayments import (
    SilentPaymentAddress,
    SilentPaymentInput,
    create_labeled_address,
    create_outputs,
)

from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.silent_payments import CHANGE_LABEL, SilentPaymentWallet


def _sp_wallet(network: str = "mainnet") -> SilentPaymentWallet:
    master = HDKey.from_seed(mnemonic_to_seed(TEST_MNEMONIC, ""))
    return SilentPaymentWallet(master, network=network)


def test_derivation_is_deterministic_and_distinct() -> None:
    a = _sp_wallet()
    b = _sp_wallet()
    assert a.scan_privkey == b.scan_privkey
    assert a.spend_privkey == b.spend_privkey
    # Scan and spend keys must differ (different derivation branches).
    assert a.scan_privkey != a.spend_privkey
    assert a.scan_pubkey != a.spend_pubkey


def test_address_encoding_matches_network() -> None:
    assert _sp_wallet("mainnet").get_address().startswith("sp1")
    assert _sp_wallet("signet").get_address().startswith("tsp1")

    addr = _sp_wallet().get_address()
    decoded, hrp = SilentPaymentAddress.decode(addr)
    assert hrp == "sp"
    assert decoded.scan_pubkey == _sp_wallet().scan_pubkey
    assert decoded.spend_pubkey == _sp_wallet().spend_pubkey


def test_change_label_cannot_be_published() -> None:
    with pytest.raises(ValueError, match="m=0 change label"):
        _sp_wallet().get_address(label=CHANGE_LABEL)


def test_labeled_address_differs_from_base() -> None:
    wallet = _sp_wallet()
    base = wallet.get_address()
    labeled = wallet.get_address(label=1)
    assert base != labeled
    base_decoded, _ = SilentPaymentAddress.decode(base)
    labeled_decoded, _ = SilentPaymentAddress.decode(labeled)
    # Same scan key, different spend (tweaked) key.
    assert base_decoded.scan_pubkey == labeled_decoded.scan_pubkey
    assert base_decoded.spend_pubkey != labeled_decoded.spend_pubkey


def _p2wpkh_input(privkey: int, txid: str, vout: int) -> SilentPaymentInput:
    pub = PublicKey.from_secret(privkey.to_bytes(32, "big")).format(compressed=True)
    import hashlib

    pubkey_hash = hashlib.new("ripemd160", hashlib.sha256(pub).digest()).digest()
    return SilentPaymentInput(
        txid=txid,
        vout=vout,
        scriptpubkey=bytes([0x00, 0x14]) + pubkey_hash,
        witness=[b"\x00" * 71, pub],
        private_key=privkey,
    )


def test_send_then_scan_round_trip() -> None:
    """A payment sent to the wallet's SP address must be detected and spendable."""
    wallet = _sp_wallet()
    address = wallet.get_address()

    sender_priv = 0xA1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F6A1B2C3D4E5F60001
    vin = _p2wpkh_input(sender_priv, "11" * 32, 0)

    recipient = SilentPaymentAddress.decode(address)[0]
    outputs = create_outputs([(sender_priv, False)], [vin.outpoint()], [recipient])
    assert len(outputs) == 1

    found = wallet.scan([vin], outputs)
    assert len(found) == 1
    assert found[0].pubkey_xonly == outputs[0]

    # Recovered private key must reproduce the taproot output key.
    d = wallet.output_private_key(found[0])
    derived = PublicKey.from_secret(d.to_bytes(32, "big")).format(compressed=True)[1:]
    assert derived == outputs[0]

    # And the output address is a valid taproot (bech32m) address.
    assert wallet.output_address(found[0]).startswith("bc1p")


def test_scan_change_label_round_trip() -> None:
    """A payment to the reserved change label must be detected via scanning."""
    wallet = _sp_wallet()
    # Build the m=0 labeled address directly (never published, only scanned).
    change_address = SilentPaymentAddress.decode(
        create_labeled_address(wallet.scan_privkey, wallet.spend_pubkey, CHANGE_LABEL, "mainnet")
    )[0]

    sender_priv = 0x0BADC0DE0BADC0DE0BADC0DE0BADC0DE0BADC0DE0BADC0DE0BADC0DE0BADC0001
    vin = _p2wpkh_input(sender_priv, "22" * 32, 1)
    outputs = create_outputs([(sender_priv, False)], [vin.outpoint()], [change_address])

    found = wallet.scan([vin], outputs)
    assert len(found) == 1
    d = wallet.output_private_key(found[0])
    derived = PublicKey.from_secret(d.to_bytes(32, "big")).format(compressed=True)[1:]
    assert derived == outputs[0]
