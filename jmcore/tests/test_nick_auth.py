from __future__ import annotations

import base64
import hashlib
import json

import pytest
from pydantic import ValidationError

from jmcore.crypto import NickIdentity
from jmcore.nick_auth import (
    NickAuthChallenge,
    NickAuthMode,
    NickAuthProof,
    NickAuthResult,
    build_nick_auth_signed_message,
    create_nick_auth_proof,
    directory_id_for_endpoint,
    handshake_line_sha256,
    parse_strict_json_object,
    verify_nick_auth_proof,
)

PRIVATE_KEY = bytes.fromhex("11" * 32)
CHALLENGE = "22" * 32
DIRECTORY_HOST = "nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion"
DIRECTORY_ID = f"{DIRECTORY_HOST}:5222"
HANDSHAKE_LINE = '{"nick":"J5example","features":{"nick_auth":true}}'


@pytest.fixture
def identity() -> NickIdentity:
    return NickIdentity(private_key_bytes=PRIVATE_KEY)


@pytest.fixture
def proof(identity: NickIdentity) -> NickAuthProof:
    return create_nick_auth_proof(identity, CHALLENGE, DIRECTORY_ID, HANDSHAKE_LINE)


def test_modes_are_stable_string_values():
    assert {mode.value for mode in NickAuthMode} == {
        "prefer_verified",
        "require_verified",
        "disabled",
    }


def test_payload_models_roundtrip_wire_aliases(proof: NickAuthProof):
    challenge = NickAuthChallenge(kind="challenge", challenge=CHALLENGE, directory_id=DIRECTORY_ID)
    assert challenge.to_payload() == {
        "kind": "challenge",
        "challenge": CHALLENGE,
        "directory-id": DIRECTORY_ID,
    }
    assert NickAuthChallenge.parse(challenge.to_json()) == challenge

    proof_payload = proof.to_payload()
    assert "handshake-sha256" in proof_payload
    assert "handshake_sha256" not in proof_payload
    assert NickAuthProof.from_payload(json.dumps(proof_payload)) == proof

    result = NickAuthResult(code="ok", verified=True)
    assert NickAuthResult.from_payload(result.to_payload()) == result


@pytest.mark.parametrize(
    "payload",
    [
        '{"kind":"challenge","challenge":"'
        + CHALLENGE
        + '","challenge":"'
        + CHALLENGE
        + '","directory-id":"test:directory-a"}',
        '{"kind":"challenge","challenge":"'
        + CHALLENGE
        + '","directory-id":"test:directory-a","value":NaN}',
        '{"kind":"challenge","challenge":"'
        + CHALLENGE
        + '","directory-id":"test:directory-a","value":Infinity}',
        "[]",
    ],
)
def test_strict_json_rejects_duplicates_non_finite_and_non_objects(payload: str):
    with pytest.raises(ValueError):
        parse_strict_json_object(payload)


def test_payload_models_reject_extra_fields_and_type_coercion():
    with pytest.raises(ValidationError):
        NickAuthChallenge.from_payload(
            {
                "kind": "challenge",
                "challenge": CHALLENGE,
                "directory-id": "test:directory-a",
                "extra": "rejected",
            }
        )
    with pytest.raises(ValidationError):
        NickAuthResult(code="ok", verified=1)
    with pytest.raises(ValidationError):
        NickAuthResult(code="invalid", verified=True)
    with pytest.raises(ValidationError):
        NickAuthResult(code="unknown", verified=False)

    with pytest.raises(ValueError, match="missing required field"):
        NickAuthChallenge.from_payload({"challenge": CHALLENGE, "directory-id": "test:directory-a"})
    with pytest.raises(ValueError, match="wire name"):
        NickAuthChallenge.from_payload(
            {
                "kind": "challenge",
                "challenge": CHALLENGE,
                "directory_id": "test:directory-a",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("challenge", "AA" * 32),
        ("handshake-sha256", "0" * 63),
        ("pubkey", "02" + "AA" * 32),
        ("pubkey", "04" + "11" * 32),
        ("signature", "not base64"),
        ("signature", base64.b64encode(b"\x30\x00").decode("ascii")),
    ],
)
def test_proof_rejects_noncanonical_encodings(proof: NickAuthProof, field: str, value: str):
    payload = proof.to_payload()
    payload[field] = value
    with pytest.raises(ValidationError):
        NickAuthProof.from_payload(payload)


def test_handshake_hashes_exact_decoded_line_utf8_bytes():
    line = '{"motd":"caf\u00e9","escaped":"\\n"}'
    assert handshake_line_sha256(line) == hashlib.sha256(line.encode("utf-8")).hexdigest()
    assert handshake_line_sha256(line) != handshake_line_sha256(line + "\r\n")
    assert handshake_line_sha256(line) != handshake_line_sha256(json.dumps(line))


def test_directory_endpoint_ids_are_canonical():
    assert directory_id_for_endpoint(DIRECTORY_HOST.upper(), 5222) == DIRECTORY_ID
    assert directory_id_for_endpoint("LOCALHOST", 1234) == "test:localhost-1234"
    assert directory_id_for_endpoint("127.0.0.1", 1234) == "test:127.0.0.1-1234"
    assert directory_id_for_endpoint("directory-a", 5222) == "test:directory-a-5222"


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("short.onion", 5222),
        ("a" * 56 + ".onion", 0),
        ("bad host", 5222),
        ("example.com", 5222),
        ("8.8.8.8", 5222),
    ],
)
def test_directory_endpoint_ids_reject_malformed_or_public_values(host: str, port: int):
    with pytest.raises(ValueError):
        directory_id_for_endpoint(host, port)


def test_signed_message_is_exact_ascii_transcript(identity: NickIdentity):
    handshake_hash = handshake_line_sha256(HANDSHAKE_LINE)
    message = build_nick_auth_signed_message(
        CHALLENGE,
        DIRECTORY_ID,
        handshake_hash,
        identity.nick,
        identity.public_key_hex,
    )
    assert (
        message
        == (
            f"nick-auth|{CHALLENGE}|{DIRECTORY_ID}|{handshake_hash}|"
            f"{identity.nick}|{identity.public_key_hex}"
        ).encode("ascii")
        + b"onion-network"
    )


def test_create_and_verify_deterministic_proof(identity: NickIdentity, proof: NickAuthProof):
    assert proof.challenge == CHALLENGE
    assert (
        proof.handshake_sha256 == "ac72075bdc7009683bd8a563dfd09c5ad7ef8c42e8040db0717a5fb3c19b7666"
    )
    assert proof.pubkey == "034f355bdcb7cc0af728ef3cceb9615d90684bb5b2ca5f859ab0f0b704075871aa"
    assert proof.signature == (
        "MEUCIQCy/qOvvS9DrnU8eOp09bftEqTvFmuDay7kXQtJw03a"
        "RAIgR+WY4/hlsW8gC+NmGJIqM6ijM0r5oXmrhOIVn1ZlHAQ="
    )
    assert verify_nick_auth_proof(
        proof,
        CHALLENGE,
        DIRECTORY_ID,
        HANDSHAKE_LINE,
        identity.nick,
    )


def test_signature_is_deterministic(identity: NickIdentity):
    first = create_nick_auth_proof(identity, CHALLENGE, DIRECTORY_ID, HANDSHAKE_LINE)
    second = create_nick_auth_proof(identity, CHALLENGE, DIRECTORY_ID, HANDSHAKE_LINE)
    assert first == second


@pytest.mark.parametrize(
    ("challenge", "directory_id", "handshake_line", "nick"),
    [
        ("33" * 32, DIRECTORY_ID, HANDSHAKE_LINE, None),
        (CHALLENGE, "test:other-directory", HANDSHAKE_LINE, None),
        (CHALLENGE, DIRECTORY_ID, HANDSHAKE_LINE + " ", None),
        (CHALLENGE, DIRECTORY_ID, HANDSHAKE_LINE, "J5wrongnick"),
    ],
)
def test_verification_rejects_replay_binding_mismatches(
    identity: NickIdentity,
    proof: NickAuthProof,
    challenge: str,
    directory_id: str,
    handshake_line: str,
    nick: str | None,
):
    assert not verify_nick_auth_proof(
        proof,
        challenge,
        directory_id,
        handshake_line,
        nick or identity.nick,
    )


def test_verification_rejects_tampered_signature(proof: NickAuthProof, identity: NickIdentity):
    signature = bytearray(base64.b64decode(proof.signature))
    signature[-1] ^= 1
    tampered = NickAuthProof.from_payload(
        {**proof.to_payload(), "signature": base64.b64encode(signature).decode("ascii")}
    )
    assert not verify_nick_auth_proof(
        tampered,
        CHALLENGE,
        DIRECTORY_ID,
        HANDSHAKE_LINE,
        identity.nick,
    )
