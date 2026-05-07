"""Tests for taker.attestation_phase decoders."""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest
from coincurve import PrivateKey
from jmcore.attestation_wire import AttestPayload, encode_attest
from jmcore.clsag_attestation import RingMember

from taker.attestation_phase import (
    AttestDecodeResult,
    decode_attest_reply,
    index_ring_by_nick,
    log_decode_failures,
)


class _IdCrypto:
    """Round-trips strings through base64 (stand-in for NaCl session)."""

    def decrypt(self, ciphertext: str) -> str:
        return base64.b64decode(ciphertext).decode()


def _wrap(plaintext: str) -> str:
    return base64.b64encode(plaintext.encode()).decode()


def _signature_for_set(n: int) -> bytes:
    # Sized to (33 + 32 + 32 * n) so decode_attest's length check passes.
    return os.urandom(33 + 32 + 32 * n)


# ---- decode_attest_reply ----


def test_decode_attest_reply_happy_path() -> None:
    sig = _signature_for_set(5)
    payload = AttestPayload(run_id=os.urandom(32), round_no=2, signature=sig)
    encrypted = _wrap(encode_attest(payload))
    response_data = f"{encrypted} signing_pk_hex some_sig_b64"

    result = decode_attest_reply(
        nick="m1", response_data=response_data, crypto=_IdCrypto(), expected_set_size=5
    )
    assert result.ok
    assert result.payload == payload
    assert result.error is None
    assert result.nick == "m1"


def test_decode_attest_reply_empty() -> None:
    r = decode_attest_reply(nick="m1", response_data="   ", crypto=_IdCrypto(), expected_set_size=5)
    assert not r.ok
    assert r.error == "empty response"


def test_decode_attest_reply_wrong_set_size() -> None:
    payload = AttestPayload(run_id=os.urandom(32), round_no=0, signature=_signature_for_set(5))
    encrypted = _wrap(encode_attest(payload))
    r = decode_attest_reply(
        nick="m1", response_data=encrypted, crypto=_IdCrypto(), expected_set_size=6
    )
    assert not r.ok
    assert r.error is not None
    assert "wire decode failed" in r.error


def test_decode_attest_reply_garbage_plaintext() -> None:
    encrypted = _wrap("not a valid attest blob")
    r = decode_attest_reply(
        nick="m1", response_data=encrypted, crypto=_IdCrypto(), expected_set_size=5
    )
    assert not r.ok
    assert r.error is not None
    assert "wire decode failed" in r.error


# ---- index_ring_by_nick ----


def _ring(n: int) -> list[RingMember]:
    return [
        RingMember(
            pubkey_xonly=PrivateKey(os.urandom(32)).public_key.format(compressed=True)[1:],
            outpoint=os.urandom(36),
        )
        for _ in range(n)
    ]


def test_index_ring_by_nick_passes_through_valid_assignment() -> None:
    out = index_ring_by_nick(_ring(4), {"a": 0, "b": 2, "c": 3})
    assert out == {"a": 0, "b": 2, "c": 3}


def test_index_ring_by_nick_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="outside ring"):
        index_ring_by_nick(_ring(3), {"a": 0, "b": 5})


def test_index_ring_by_nick_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        index_ring_by_nick(_ring(4), {"a": 1, "b": 1})


# ---- log_decode_failures ----


def test_log_decode_failures_returns_only_failed_nicks() -> None:
    results = [
        AttestDecodeResult(nick="ok1", payload=AttestPayload(b"\x00" * 32, 0, b"x"), error=None),
        AttestDecodeResult(nick="bad1", payload=None, error="boom"),
        AttestDecodeResult(nick="bad2", payload=None, error="kaboom"),
    ]
    failed = log_decode_failures(results)
    assert failed == ["bad1", "bad2"]


def test_log_decode_failures_empty() -> None:
    assert log_decode_failures([]) == []


def test_protocol_compatibility_with_real_session_shape() -> None:
    """Sanity-check that anything with a `decrypt(str) -> str` method satisfies the protocol."""

    class WeirdCrypto:
        def decrypt(self, ciphertext: str) -> str:
            return "garbage"

        # Extra attributes the real CryptoSession has shouldn't matter.
        is_encrypted = True
        unrelated_state: dict[str, Any] = {}

    encrypted = "anything"
    r = decode_attest_reply(
        nick="m", response_data=encrypted, crypto=WeirdCrypto(), expected_set_size=5
    )
    assert not r.ok  # garbage plaintext won't decode
