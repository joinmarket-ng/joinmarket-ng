from __future__ import annotations

import base64
import hashlib
import json

import pytest
from coincurve import verify_signature as coincurve_verify
from pydantic import ValidationError

from jmcore.crypto import NickIdentity, bitcoin_message_hash_bytes
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
    validate_directory_id,
    verify_nick_auth_proof,
)
from jmcore.protocol import JM_VERSION

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
    challenge = NickAuthChallenge(challenge=CHALLENGE, directory_id=DIRECTORY_ID)
    assert challenge.to_payload() == {
        "challenge": CHALLENGE,
        "directory-id": DIRECTORY_ID,
    }
    assert NickAuthChallenge.parse(challenge.to_json()) == challenge

    proof_payload = proof.to_payload()
    assert set(proof_payload) == {"pubkey", "signature"}
    assert NickAuthProof.from_payload(json.dumps(proof_payload)) == proof

    result = NickAuthResult(code="ok", verified=True)
    assert NickAuthResult.from_payload(result.to_payload()) == result


@pytest.mark.parametrize(
    "payload",
    [
        '{"challenge":"'
        + CHALLENGE
        + '","challenge":"'
        + CHALLENGE
        + '","directory-id":"test:directory-a"}',
        '{"challenge":"' + CHALLENGE + '","directory-id":"test:directory-a","value":NaN}',
        '{"challenge":"' + CHALLENGE + '","directory-id":"test:directory-a","value":Infinity}',
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

    with pytest.raises(ValueError, match="wire name"):
        NickAuthChallenge.from_payload(
            {
                "challenge": CHALLENGE,
                "directory_id": "test:directory-a",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
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


@pytest.mark.parametrize(
    "directory_id",
    [DIRECTORY_ID, "test:directory-a", "directory.example:5222", "vpn_directory-1"],
)
def test_directory_ids_accept_spec_grammar(directory_id: str):
    assert validate_directory_id(directory_id) == directory_id


@pytest.mark.parametrize(
    "directory_id",
    [
        "",
        "UPPERCASE",
        ".directory",
        ":directory",
        "_directory",
        "-directory",
        "bad|id",
        "bad id",
        "non-ascii-\u00e9",
    ],
)
def test_directory_ids_reject_values_outside_spec_grammar(directory_id: str):
    with pytest.raises(ValueError, match="invalid directory-id"):
        validate_directory_id(directory_id)


@pytest.mark.parametrize(
    ("host", "port"),
    [
        ("short.onion", 5222),
        ("a" * 56 + ".onion", 0),
        ("bad host", 5222),
        ("localhost", 1234),
        ("127.0.0.1", 1234),
        ("directory-a", 5222),
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
        + b"nick-auth-v1"
    )


def test_create_and_verify_deterministic_proof(identity: NickIdentity, proof: NickAuthProof):
    assert proof.to_payload() == {
        "pubkey": "034f355bdcb7cc0af728ef3cceb9615d90684bb5b2ca5f859ab0f0b704075871aa",
        "signature": (
            "MEQCIDTAF2VwsN1hK3n+Hc2iGt2xkhURfTfCiJnM4myGM6T2"
            "AiAbXU8Kl5f16SwddktRYcl+gLHt5zS1nY7eLSi61e1g6w=="
        ),
    }
    assert proof.pubkey == "034f355bdcb7cc0af728ef3cceb9615d90684bb5b2ca5f859ab0f0b704075871aa"
    assert proof.signature == (
        "MEQCIDTAF2VwsN1hK3n+Hc2iGt2xkhURfTfCiJnM4myGM6T2"
        "AiAbXU8Kl5f16SwddktRYcl+gLHt5zS1nY7eLSi61e1g6w=="
    )
    assert verify_nick_auth_proof(
        proof,
        CHALLENGE,
        DIRECTORY_ID,
        HANDSHAKE_LINE,
        identity.nick,
        JM_VERSION,
    )


def test_proof_does_not_verify_in_message_signature_domain(
    identity: NickIdentity,
    proof: NickAuthProof,
):
    handshake_hash = handshake_line_sha256(HANDSHAKE_LINE)
    transcript = (
        f"nick-auth|{CHALLENGE}|{DIRECTORY_ID}|{handshake_hash}|"
        f"{identity.nick}|{identity.public_key_hex}"
    ).encode("ascii")
    message_hash = bitcoin_message_hash_bytes(transcript + b"onion-network")

    assert not coincurve_verify(
        base64.b64decode(proof.signature),
        message_hash,
        bytes.fromhex(proof.pubkey),
        hasher=None,
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
        JM_VERSION,
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
        JM_VERSION,
    )


def test_verification_uses_negotiated_version(identity: NickIdentity, proof: NickAuthProof):
    assert not verify_nick_auth_proof(
        proof,
        CHALLENGE,
        DIRECTORY_ID,
        HANDSHAKE_LINE,
        identity.nick,
        JM_VERSION + 1,
    )
