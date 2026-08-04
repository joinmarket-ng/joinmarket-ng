"""Shared JMP-0005 nick ownership authentication primitives."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar, Literal, Self, cast

from coincurve import PublicKey
from coincurve import verify_signature as coincurve_verify
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jmcore.crypto import NickIdentity, bitcoin_message_hash_bytes, nick_from_pubkey_hex

_LOWER_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_COMPRESSED_PUBKEY_RE = re.compile(r"0[23][0-9a-f]{64}")
_ONION_HOST_RE = re.compile(r"[a-z2-7]{56}\.onion")
_NICK_RE = re.compile(r"J[0-9][1-9A-HJ-NP-Za-km-zO]{14}")
_DIRECTORY_ID_RE = re.compile(r"[a-z0-9][a-z0-9.:_-]*")
_SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_NICK_AUTH_DOMAIN = b"nick-auth-v1"


class NickAuthMode(StrEnum):
    PREFER_VERIFIED = "prefer_verified"
    REQUIRE_VERIFIED = "require_verified"
    DISABLED = "disabled"


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_strict_json_object(payload: str | bytes) -> dict[str, Any]:
    """Parse a JSON object while rejecting duplicate keys and non-finite numbers."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if not isinstance(payload, str):
        raise TypeError("JSON payload must be str or bytes")
    parsed = json.loads(
        payload,
        object_pairs_hook=_object_without_duplicate_keys,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(parsed, dict):
        raise ValueError("nick authentication payload must be a JSON object")
    return cast(dict[str, Any], parsed)


def _validate_lower_hex_64(value: str, field_name: str) -> str:
    if _LOWER_HEX_64_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be exactly 64 lowercase hexadecimal characters")
    return value


def validate_directory_id(value: str) -> str:
    """Validate and return a JMP-0005 directory identity."""
    if not isinstance(value, str) or _DIRECTORY_ID_RE.fullmatch(value) is None:
        raise ValueError("invalid directory-id")
    return value


def validate_directory_endpoint(value: str) -> str:
    """Validate an exact host:port key used to select an expected directory identity."""
    if not isinstance(value, str) or value != value.strip():
        raise ValueError("invalid directory endpoint")
    try:
        host, port_text = value.rsplit(":", 1)
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("directory endpoint must use host:port") from exc
    if (
        not host
        or ":" in host
        or any(char.isspace() for char in host)
        or not 1 <= port <= 65535
        or value != f"{host}:{port}"
    ):
        raise ValueError("directory endpoint must use canonical host:port")
    return value


def _is_strict_der_signature(signature: bytes) -> bool:
    if not 8 <= len(signature) <= 72:
        return False
    if signature[0] != 0x30 or signature[1] != len(signature) - 2:
        return False
    if signature[2] != 0x02:
        return False

    r_length = signature[3]
    if r_length == 0 or 5 + r_length >= len(signature):
        return False
    r_start = 4
    if signature[r_start] & 0x80:
        return False
    if r_length > 1 and signature[r_start] == 0 and not signature[r_start + 1] & 0x80:
        return False
    r_value = int.from_bytes(signature[r_start : r_start + r_length], "big")
    if not 1 <= r_value < _SECP256K1_ORDER:
        return False

    s_type_index = r_start + r_length
    if signature[s_type_index] != 0x02:
        return False
    s_length = signature[s_type_index + 1]
    s_start = s_type_index + 2
    if s_length == 0 or s_start + s_length != len(signature):
        return False
    if signature[s_start] & 0x80:
        return False
    if s_length > 1 and signature[s_start] == 0 and not signature[s_start + 1] & 0x80:
        return False
    s_value = int.from_bytes(signature[s_start : s_start + s_length], "big")
    return 1 <= s_value < _SECP256K1_ORDER


def _decode_canonical_der_signature(value: str) -> bytes:
    try:
        encoded = value.encode("ascii")
        signature = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("signature must use strict Base64") from exc
    if base64.b64encode(signature) != encoded:
        raise ValueError("signature Base64 encoding is not canonical")
    if not _is_strict_der_signature(signature):
        raise ValueError("signature is not strict DER")
    return signature


class _NickAuthPayload(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        strict=True,
        populate_by_name=True,
    )

    @classmethod
    def parse(cls, payload: str | bytes) -> Self:
        return cls.from_payload(parse_strict_json_object(payload))

    @classmethod
    def from_payload(cls, payload: Mapping[str, object] | str | bytes) -> Self:
        if isinstance(payload, (str, bytes)):
            return cls.parse(payload)
        data = dict(payload)
        for field_name, field in cls.model_fields.items():
            if field.alias is not None and field.alias != field_name and field_name in data:
                raise ValueError(f"payload field must use wire name: {field.alias}")
        return cls.model_validate(data, strict=True)

    def to_payload(self) -> dict[str, object]:
        return cast(dict[str, object], self.model_dump(mode="json", by_alias=True))

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), allow_nan=False, separators=(",", ":"))


class NickAuthChallenge(_NickAuthPayload):
    challenge: str
    directory_id: str = Field(alias="directory-id")

    @field_validator("challenge")
    @classmethod
    def validate_challenge(cls, value: str) -> str:
        return _validate_lower_hex_64(value, "challenge")

    @field_validator("directory_id")
    @classmethod
    def validate_directory_id(cls, value: str) -> str:
        return validate_directory_id(value)


class NickAuthProof(_NickAuthPayload):
    pubkey: str
    signature: str

    @field_validator("pubkey")
    @classmethod
    def validate_pubkey(cls, value: str) -> str:
        if _COMPRESSED_PUBKEY_RE.fullmatch(value) is None:
            raise ValueError("pubkey must be a lowercase compressed secp256k1 public key")
        try:
            PublicKey(bytes.fromhex(value))
        except ValueError as exc:
            raise ValueError("pubkey is not a valid secp256k1 public key") from exc
        return value

    @field_validator("signature")
    @classmethod
    def validate_signature(cls, value: str) -> str:
        _decode_canonical_der_signature(value)
        return value


class NickAuthResult(_NickAuthPayload):
    code: Literal["ok", "malformed", "expired", "invalid", "policy"]
    verified: bool

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.verified != (self.code == "ok"):
            raise ValueError("only code 'ok' may have verified=true")
        return self


def handshake_line_sha256(line: str) -> str:
    if not isinstance(line, str):
        raise TypeError("handshake line must be str")
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def directory_id_for_endpoint(host: str, port: int) -> str:
    """Derive a JMP-0005 identity from a selected Tor v3 endpoint."""
    if not isinstance(host, str) or isinstance(port, bool) or not isinstance(port, int):
        raise ValueError("host and port have invalid types")
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not host or host != host.strip() or any(char.isspace() for char in host):
        raise ValueError("invalid host")

    normalized_host = host.lower()
    if normalized_host.endswith("."):
        normalized_host = normalized_host[:-1]
    if normalized_host.endswith(".onion"):
        if _ONION_HOST_RE.fullmatch(normalized_host) is None:
            raise ValueError("onion endpoint must use a 56-character v3 hostname")
        return f"{normalized_host}:{port}"
    raise ValueError("non-onion endpoint requires an explicitly configured directory-id")


def build_nick_auth_signed_message(
    challenge: str,
    directory_id: str,
    handshake_sha256: str,
    nick: str,
    pubkey: str,
) -> bytes:
    challenge = _validate_lower_hex_64(challenge, "challenge")
    directory_id = validate_directory_id(directory_id)
    handshake_sha256 = _validate_lower_hex_64(handshake_sha256, "handshake-sha256")
    if _NICK_RE.fullmatch(nick) is None:
        raise ValueError("nick must use the JMP-0001 nick format")
    NickAuthProof.validate_pubkey(pubkey)
    transcript = f"nick-auth|{challenge}|{directory_id}|{handshake_sha256}|{nick}|{pubkey}"
    return transcript.encode("ascii") + _NICK_AUTH_DOMAIN


def create_nick_auth_proof(
    identity: NickIdentity,
    challenge: str,
    directory_id: str,
    handshake_line: str,
) -> NickAuthProof:
    handshake_sha256 = handshake_line_sha256(handshake_line)
    message = build_nick_auth_signed_message(
        challenge,
        directory_id,
        handshake_sha256,
        identity.nick,
        identity.public_key_hex,
    )
    return NickAuthProof.from_payload(
        {
            "pubkey": identity.public_key_hex,
            "signature": identity.sign_bytes(message),
        }
    )


def verify_nick_auth_proof(
    proof: NickAuthProof,
    expected_challenge: str,
    expected_directory_id: str,
    handshake_line: str,
    nick: str,
    protocol_version: int,
) -> bool:
    try:
        handshake_sha256 = handshake_line_sha256(handshake_line)
        if nick_from_pubkey_hex(proof.pubkey, protocol_version) != nick:
            return False

        signature = _decode_canonical_der_signature(proof.signature)
        message = build_nick_auth_signed_message(
            expected_challenge,
            expected_directory_id,
            handshake_sha256,
            nick,
            proof.pubkey,
        )
        message_hash = bitcoin_message_hash_bytes(message)
        return coincurve_verify(signature, message_hash, bytes.fromhex(proof.pubkey), hasher=None)
    except (TypeError, ValueError):
        return False
