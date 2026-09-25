from __future__ import annotations

from hashlib import sha256

import pytest

from taker._vendor.bolt11.models.signature import SECP256K1_ORDER, Signature, message

PRIVATE_KEY = "e126f68f7eafcc8b74f54d269fe206be715000f94dac067d1c04a8ca3b2db734"
PAYEE = "03e7156ae33b0a208d0744199163177e909e80176e55d97a2f221ede0f934dd9ad"
OTHER_PAYEE = "031b84c5567b126440995d3ed5aaba0565d71e1834604819ff9c17f5e9d5dd078f"
SIGNING_HRP = "lnbc1"
SIGNING_DATA = b"1234567890"
# BOLT11 example at bolts revision 14901bdcacee53d95b46dc276b0f09c85d7d71fd:
# https://github.com/lightning/bolts/blob/14901bdcacee53d95b46dc276b0f09c85d7d71fd/11-payment-encoding.md
BOLT11_SIGNING_DATA = bytes.fromhex(
    "0b25fe64500d04444444444444444444444444444444444444444444444444444444444444444021a"
    "00008101820283038404800081018202830384048000810182028303840480810343f506c6561736520"
    "636f6e736964657220737570706f7274696e6720746869732070726f6a6563740500e08000"
)
BOLT11_SIGNING_DIGEST = "6daf4d488be41ce7cbb487cab1ef2975e5efcea879b20d421f0ef86b07cbb987"


def _official_signature() -> Signature:
    return Signature.from_signature_data(
        hrp="lnbc",
        signing_data=BOLT11_SIGNING_DATA,
        signature_data=bytes.fromhex(
            "8d3ce9e28357337f62da0162d9454df827f83cfe499aeb1c1db349d4d8112742"
            "5e434ca29929406c23bba1ae8ac6ca32880b38d4bf6ff874024cac34ba9625f101"
        ),
    )


def test_validates_official_bolt11_signature_and_uses_its_signing_digest() -> None:
    signature = _official_signature()

    assert signature.recover_public_key() == PAYEE
    assert signature.verify(PAYEE)
    assert signature.signing_data == BOLT11_SIGNING_DATA
    assert sha256(message("lnbc", BOLT11_SIGNING_DATA)).hexdigest() == BOLT11_SIGNING_DIGEST


def test_signs_recovers_and_verifies_explicit_payee() -> None:
    signature = Signature.from_private_key(SIGNING_HRP, PRIVATE_KEY, SIGNING_DATA)

    assert len(signature.signature_data) == 65
    assert signature.recover_public_key() == PAYEE
    assert signature.verify(PAYEE)


def test_signing_matches_official_deterministic_signature() -> None:
    signature = Signature.from_private_key("lnbc", PRIVATE_KEY, BOLT11_SIGNING_DATA)

    assert signature.signature_data == _official_signature().signature_data


@pytest.mark.parametrize("payee", ["", "not hex", "02" + "00" * 32])
def test_rejects_malformed_payee(payee: str) -> None:
    with pytest.raises(ValueError):
        _official_signature().verify(payee)


@pytest.mark.parametrize("private_key", ["00" * 32, "01" * 31, "1"])
def test_rejects_malformed_private_key(private_key: str) -> None:
    with pytest.raises(ValueError):
        Signature.from_private_key(SIGNING_HRP, private_key, SIGNING_DATA)


def test_rejects_wrong_digest_and_payee() -> None:
    signature = _official_signature()
    wrong_digest = Signature.from_signature_data(
        hrp=signature.hrp,
        signing_data=signature.signing_data + b"wrong",
        signature_data=signature.signature_data,
    )

    assert wrong_digest.recover_public_key() != PAYEE
    with pytest.raises(ValueError, match="Invalid signature"):
        wrong_digest.verify(PAYEE)
    with pytest.raises(ValueError, match="Invalid signature"):
        signature.verify(OTHER_PAYEE)


def test_explicit_payee_verification_ignores_a_valid_recovery_flag() -> None:
    signature = _official_signature()
    signature_with_other_recovery_flag = Signature.from_signature_data(
        signature.hrp,
        signature.signature_data[:64] + bytes([signature.signature_data[64] ^ 1]),
        signature.signing_data,
    )

    assert signature_with_other_recovery_flag.verify(PAYEE)


@pytest.mark.parametrize(
    "signature_data",
    [
        b"\x01" * 64,
        b"\x01" * 66,
        b"\x01" * 64 + b"\x04",
    ],
)
def test_rejects_invalid_signature_length_and_recovery_flag(signature_data: bytes) -> None:
    signature = Signature.from_signature_data("lnbc", signature_data, BOLT11_SIGNING_DATA)

    with pytest.raises(ValueError, match="Invalid signature data"):
        signature.recover_public_key()
    with pytest.raises(ValueError, match="Invalid signature data"):
        signature.verify(PAYEE)


@pytest.mark.parametrize(
    "raw_signature",
    [
        b"\x00" * 32
        + bytes.fromhex("5e434ca29929406c23bba1ae8ac6ca32880b38d4bf6ff874024cac34ba9625f1"),
        bytes.fromhex("8d3ce9e28357337f62da0162d9454df827f83cfe499aeb1c1db349d4d8112742")
        + b"\x00" * 32,
        SECP256K1_ORDER.to_bytes(32, byteorder="big")
        + bytes.fromhex("5e434ca29929406c23bba1ae8ac6ca32880b38d4bf6ff874024cac34ba9625f1"),
        bytes.fromhex("8d3ce9e28357337f62da0162d9454df827f83cfe499aeb1c1db349d4d8112742")
        + SECP256K1_ORDER.to_bytes(32, byteorder="big"),
    ],
)
def test_explicit_payee_rejects_zero_and_overflow_scalars(raw_signature: bytes) -> None:
    signature = Signature.from_signature_data("lnbc", raw_signature + b"\x01", BOLT11_SIGNING_DATA)

    with pytest.raises(ValueError, match="Invalid signature"):
        signature.recover_public_key()
    with pytest.raises(ValueError, match="Invalid signature"):
        signature.verify(PAYEE)


def test_recovery_accepts_high_s_but_explicit_payee_rejects_it() -> None:
    low_s_signature = _official_signature()
    r = low_s_signature.signature_data[:32]
    s = int.from_bytes(low_s_signature.signature_data[32:64], byteorder="big")
    high_s = (SECP256K1_ORDER - s).to_bytes(32, byteorder="big")
    recovery_flag = low_s_signature.signature_data[64] ^ 1
    high_s_signature = Signature.from_signature_data(
        low_s_signature.hrp,
        r + high_s + bytes([recovery_flag]),
        low_s_signature.signing_data,
    )

    assert high_s_signature.recover_public_key() == PAYEE
    with pytest.raises(ValueError, match="Invalid signature"):
        high_s_signature.verify(PAYEE)
