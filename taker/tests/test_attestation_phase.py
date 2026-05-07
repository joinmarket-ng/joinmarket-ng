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

    def encrypt(self, plaintext: str) -> str:
        return base64.b64encode(plaintext.encode()).decode()

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


# ---- run_attestation_round (orchestrator) ----

import asyncio  # noqa: E402

from jmcore.attestation_wire import (  # noqa: E402
    decode_attestreq,
)

from taker.attestation_phase import (  # noqa: E402
    AttestationRoundResult,
    MakerAttestParticipant,
    build_attestreq_for_maker,
    run_attestation_round,
)


class _StubDirectory:
    """Captures send_privmsg calls and returns canned wait_for_responses."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.sent: list[tuple[str, str, str, str]] = []  # (nick, command, msg, channel)
        self.responses = responses or {}
        self.fail_send_for: set[str] = set()

    async def send_privmsg(
        self,
        nick: str,
        command: str,
        message: str,
        *,
        log_routing: bool = False,
        force_channel: str = "",
    ) -> None:
        if nick in self.fail_send_for:
            raise RuntimeError(f"simulated send failure for {nick}")
        self.sent.append((nick, command, message, force_channel))

    async def wait_for_responses(
        self,
        expected_nicks: list[str],
        expected_command: str,
        timeout: float = 60.0,
    ) -> dict[str, dict[str, Any]]:
        return {n: self.responses[n] for n in expected_nicks if n in self.responses}


def _ciphertext_for(plaintext: str) -> str:
    return base64.b64encode(plaintext.encode()).decode()


def _make_participant(nick: str, signer_idx: int) -> MakerAttestParticipant:
    return MakerAttestParticipant(
        nick=nick,
        signer_idx=signer_idx,
        crypto=_IdCrypto(),
        comm_channel=f"directory:host:{6667 + signer_idx}",
    )


def _attest_response(plaintext: str) -> dict[str, Any]:
    return {"data": f"{_ciphertext_for(plaintext)} signing_pk_hex sig_b64", "error": False}


def test_build_attestreq_for_maker_round_trips_through_decoder() -> None:
    ring = _ring(5)
    run_id = os.urandom(32)
    wire = build_attestreq_for_maker(run_id=run_id, round_no=3, signer_idx=2, ring=ring)
    decoded = decode_attestreq(wire)
    assert decoded.run_id == run_id
    assert decoded.round_no == 3
    assert decoded.signer_idx == 2
    assert decoded.ring == ring


def test_run_attestation_round_happy_path() -> None:
    ring = _ring(5)
    run_id = os.urandom(32)
    sigs = {n: _signature_for_set(5) for n in ("m1", "m2", "m3")}
    responses = {
        n: _attest_response(
            encode_attest(AttestPayload(run_id=run_id, round_no=2, signature=sigs[n]))
        )
        for n in sigs
    }
    directory = _StubDirectory(responses=responses)
    participants = [
        _make_participant("m1", 0),
        _make_participant("m2", 2),
        _make_participant("m3", 4),
    ]

    result = asyncio.run(
        run_attestation_round(
            directory_client=directory,
            run_id=run_id,
            round_no=2,
            ring=ring,
            participants=participants,
            timeout=1.0,
        )
    )

    assert isinstance(result, AttestationRoundResult)
    assert set(result.decoded.keys()) == {"m1", "m2", "m3"}
    assert result.failed_makers == []
    assert len(directory.sent) == 3
    # Each send carries the maker's signer_idx encoded in the body.
    for nick, command, ciphertext, channel in directory.sent:
        assert command == "attestreq"
        assert channel.startswith("directory:")
        # Decode through the stub's identity decryptor.
        plaintext = base64.b64decode(ciphertext).decode()
        req = decode_attestreq(plaintext)
        expected_idx = next(p.signer_idx for p in participants if p.nick == nick)
        assert req.signer_idx == expected_idx
        assert req.ring == ring


def test_run_attestation_round_missing_response_marks_failure() -> None:
    ring = _ring(5)
    run_id = os.urandom(32)
    # Only m1 replies; m2 silently times out.
    responses = {
        "m1": _attest_response(
            encode_attest(AttestPayload(run_id=run_id, round_no=0, signature=_signature_for_set(5)))
        )
    }
    directory = _StubDirectory(responses=responses)
    participants = [_make_participant("m1", 0), _make_participant("m2", 1)]

    result = asyncio.run(
        run_attestation_round(
            directory_client=directory,
            run_id=run_id,
            round_no=0,
            ring=ring,
            participants=participants,
            timeout=0.1,
        )
    )

    assert set(result.decoded.keys()) == {"m1"}
    assert result.failed_makers == ["m2"]


def test_run_attestation_round_send_failure_excludes_from_wait() -> None:
    ring = _ring(5)
    run_id = os.urandom(32)
    directory = _StubDirectory()
    directory.fail_send_for = {"m2"}
    # m1 will get a response; if m2 had been waited on we'd see it in failed.
    directory.responses = {
        "m1": _attest_response(
            encode_attest(AttestPayload(run_id=run_id, round_no=0, signature=_signature_for_set(5)))
        )
    }
    participants = [_make_participant("m1", 0), _make_participant("m2", 1)]

    result = asyncio.run(
        run_attestation_round(
            directory_client=directory,
            run_id=run_id,
            round_no=0,
            ring=ring,
            participants=participants,
            timeout=0.1,
        )
    )

    assert set(result.decoded.keys()) == {"m1"}
    assert result.failed_makers == ["m2"]
    # m2 was never sent to.
    assert {n for n, *_ in directory.sent} == {"m1"}


def test_run_attestation_round_run_id_mismatch_surfaces_as_failure() -> None:
    ring = _ring(5)
    run_id = os.urandom(32)
    wrong_run_id = os.urandom(32)
    responses = {
        "m1": _attest_response(
            encode_attest(
                AttestPayload(run_id=wrong_run_id, round_no=0, signature=_signature_for_set(5))
            )
        ),
    }
    directory = _StubDirectory(responses=responses)
    participants = [_make_participant("m1", 0)]

    result = asyncio.run(
        run_attestation_round(
            directory_client=directory,
            run_id=run_id,
            round_no=0,
            ring=ring,
            participants=participants,
            timeout=0.1,
        )
    )
    assert result.decoded == {}
    # Mismatch is logged and the maker stays out of decoded; we don't
    # re-add to failed_makers from the mismatch path itself (it
    # decoded fine), so the assertion is that decoded stays empty.


def test_run_attestation_round_maker_error_response() -> None:
    ring = _ring(5)
    run_id = os.urandom(32)
    responses = {"m1": {"data": "blacklisted", "error": True}}
    directory = _StubDirectory(responses=responses)
    participants = [_make_participant("m1", 0)]

    result = asyncio.run(
        run_attestation_round(
            directory_client=directory,
            run_id=run_id,
            round_no=0,
            ring=ring,
            participants=participants,
            timeout=0.1,
        )
    )
    assert result.decoded == {}
    assert result.failed_makers == ["m1"]


def test_run_attestation_round_empty_participants_returns_empty() -> None:
    directory = _StubDirectory()
    result = asyncio.run(
        run_attestation_round(
            directory_client=directory,
            run_id=os.urandom(32),
            round_no=0,
            ring=_ring(5),
            participants=[],
            timeout=0.1,
        )
    )
    assert result.decoded == {}
    assert result.failed_makers == []
    assert directory.sent == []
