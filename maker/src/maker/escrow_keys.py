"""Wallet-derived keys for channel-input escrow protocol roles."""

from __future__ import annotations

import hashlib
import hmac
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from jmwallet.wallet.bip32 import HDKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from jmwallet.wallet.service import WalletService


class EscrowKeyDerivationError(Exception):
    """Raised when an escrow key cannot be safely derived or restored."""


class EscrowEndpointRole(StrEnum):
    """Endpoint identity for an escrow key."""

    MAKER = "maker"
    PEER = "peer"


class EscrowKeyPurpose(StrEnum):
    """Protocol purpose for an escrow key."""

    ESCROW = "escrow"
    CLAIM = "claim"
    TIMEOUT_DESTINATION = "timeout_destination"
    NONCE_SEED = "nonce_seed"


_APPLICATION_INDEX = (
    int.from_bytes(hashlib.sha256(b"joinmarket-ng/channel-input-escrow").digest()[:4], "big")
    & 0x7FFFFFFF
)
_SESSION_DOMAIN = b"joinmarket-ng/channel-input-escrow/v1/session\x00"
_ROLE_INDICES = {
    EscrowEndpointRole.MAKER: 0,
    EscrowEndpointRole.PEER: 1,
}
_PURPOSE_INDICES = {
    EscrowKeyPurpose.ESCROW: 0,
    EscrowKeyPurpose.CLAIM: 1,
    EscrowKeyPurpose.TIMEOUT_DESTINATION: 2,
    EscrowKeyPurpose.NONCE_SEED: 3,
}
_TIMEOUT_NONCE_DOMAIN = b"joinmarket-ng/channel-input-escrow/v1/timeout-nonce\x00"
_INVOICE_PREIMAGE_DOMAIN = b"joinmarket-ng/channel-input-escrow/v1/invoice-preimage\x00"


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id:
        raise EscrowKeyDerivationError("session ID must be a non-empty string")


def escrow_derivation_path(
    session_id: str, role: EscrowEndpointRole, purpose: EscrowKeyPurpose
) -> str:
    """Return the canonical hardened derivation path for an escrow key."""
    _validate_session_id(session_id)
    if not isinstance(role, EscrowEndpointRole) or not isinstance(purpose, EscrowKeyPurpose):
        raise EscrowKeyDerivationError("invalid escrow endpoint role or key purpose")

    session_hash = hashlib.sha256(_SESSION_DOMAIN + session_id.encode("utf-8")).digest()
    session_indices = [
        int.from_bytes(session_hash[offset : offset + 4], "big") & 0x7FFFFFFF
        for offset in range(0, 16, 4)
    ]
    path_indices = [
        _APPLICATION_INDEX,
        1,
        _ROLE_INDICES[role],
        _PURPOSE_INDICES[purpose],
        *session_indices,
    ]
    return "m/" + "/".join(f"{index}'" for index in path_indices)


def _normalize_32_byte_hex(value: str, label: str) -> bytes:
    if not isinstance(value, str) or len(value) != 64:
        raise EscrowKeyDerivationError(f"{label} must be a 32-byte hexadecimal string")
    try:
        normalized = bytes.fromhex(value)
    except ValueError as exc:
        raise EscrowKeyDerivationError(f"{label} must be a 32-byte hexadecimal string") from exc
    if len(normalized) != 32:
        raise EscrowKeyDerivationError(f"{label} must be a 32-byte hexadecimal string")
    return normalized


class EscrowKeyDerivation(BaseModel):
    """Public metadata needed to recover a wallet-derived escrow key."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    version: Literal[1] = 1
    session_id: str = Field(min_length=1)
    wallet_fingerprint: str = Field(min_length=8, max_length=8, pattern=r"^[0-9a-f]{8}$")
    role: EscrowEndpointRole
    purpose: EscrowKeyPurpose
    derivation_path: str
    compressed_public_key: str = Field(min_length=66, max_length=66, pattern=r"^[0-9a-f]{66}$")

    @field_validator("compressed_public_key")
    @classmethod
    def _validate_compressed_public_key(cls, value: str) -> str:
        try:
            public_key = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("compressed public key must be hexadecimal") from exc
        if len(public_key) != 33 or public_key[0] not in (0x02, 0x03):
            raise ValueError("compressed public key must be a 33-byte compressed key")
        return value

    @model_validator(mode="after")
    def _validate_derivation_path(self) -> EscrowKeyDerivation:
        expected_path = escrow_derivation_path(self.session_id, self.role, self.purpose)
        if self.derivation_path != expected_path:
            raise ValueError("derivation path does not match the escrow key metadata")
        return self


class EscrowKeyHandle:
    """In-memory escrow key and its public recovery metadata."""

    __slots__ = ("metadata", "_key")

    def __init__(self, metadata: EscrowKeyDerivation, key: HDKey) -> None:
        self.metadata = metadata
        self._key = key

    def __repr__(self) -> str:
        return f"EscrowKeyHandle(metadata={self.metadata!r})"

    def public_key_bytes(self) -> bytes:
        """Return the compressed public key."""
        return self._key.get_public_key_bytes(compressed=True)

    def private_key_bytes(self) -> bytes:
        """Return the 32-byte private key for protocol signing operations."""
        return self._key.get_private_key_bytes()

    def sign_digest(self, digest: bytes) -> bytes:
        """Create a BIP340 Schnorr signature over a 32-byte Taproot digest."""
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise EscrowKeyDerivationError("Schnorr signing requires a 32-byte digest")
        try:
            return self._key.private_key.sign_schnorr(digest)
        except Exception as exc:  # noqa: BLE001 - normalize key library failures
            raise EscrowKeyDerivationError("unable to sign escrow digest") from exc


def derive_timeout_nonce_randomness(
    nonce_seed: EscrowKeyHandle,
    *,
    edge_id: str,
    timeout_txid: str,
    timeout_sighash: bytes,
    role: EscrowEndpointRole,
) -> bytes:
    """Derive deterministic, role-bound entropy for a timeout MuSig2 nonce."""
    if not isinstance(nonce_seed, EscrowKeyHandle):
        raise EscrowKeyDerivationError("invalid timeout nonce seed")
    if not isinstance(role, EscrowEndpointRole):
        raise EscrowKeyDerivationError("invalid escrow endpoint role")

    edge_id_bytes = _normalize_32_byte_hex(edge_id, "edge ID")
    timeout_txid_bytes = _normalize_32_byte_hex(timeout_txid, "timeout transaction ID")
    if not isinstance(timeout_sighash, bytes) or len(timeout_sighash) != 32:
        raise EscrowKeyDerivationError("timeout sighash must be exactly 32 bytes")

    metadata = nonce_seed.metadata
    if metadata.purpose is not EscrowKeyPurpose.NONCE_SEED:
        raise EscrowKeyDerivationError("timeout nonce seed has an invalid purpose")
    if metadata.role is not role:
        raise EscrowKeyDerivationError("timeout nonce seed role does not match")
    if metadata.session_id != edge_id_bytes.hex():
        raise EscrowKeyDerivationError("timeout nonce seed session does not match edge ID")

    transcript = (
        _TIMEOUT_NONCE_DOMAIN
        + edge_id_bytes
        + timeout_txid_bytes
        + timeout_sighash
        + role.value.encode("ascii")
    )
    return hmac.digest(nonce_seed.private_key_bytes(), transcript, "sha256")


def derive_invoice_preimage(
    nonce_seed: EscrowKeyHandle, *, edge_id: str, role: EscrowEndpointRole
) -> bytes:
    """Derive a deterministic invoice preimage without adding it to durable state.

    The endpoint's nonce-seed key is wallet-derived and restored after a restart, so
    the economic peer can recreate an identical invoice request without retaining a
    sensitive preimage in an application journal.
    """
    if not isinstance(nonce_seed, EscrowKeyHandle):
        raise EscrowKeyDerivationError("invalid invoice nonce seed")
    if not isinstance(role, EscrowEndpointRole):
        raise EscrowKeyDerivationError("invalid escrow endpoint role")
    edge_id_bytes = _normalize_32_byte_hex(edge_id, "edge ID")
    metadata = nonce_seed.metadata
    if metadata.purpose is not EscrowKeyPurpose.NONCE_SEED:
        raise EscrowKeyDerivationError("invoice nonce seed has an invalid purpose")
    if metadata.role is not role:
        raise EscrowKeyDerivationError("invoice nonce seed role does not match")
    if metadata.session_id != edge_id_bytes.hex():
        raise EscrowKeyDerivationError("invoice nonce seed session does not match edge ID")
    return hmac.digest(
        nonce_seed.private_key_bytes(),
        _INVOICE_PREIMAGE_DOMAIN + edge_id_bytes + role.value.encode("ascii"),
        "sha256",
    )


class WalletEscrowKeyDeriver:
    """Derive and restore channel escrow keys from a JoinMarket wallet seed."""

    def __init__(self, wallet: WalletService) -> None:
        self._wallet = wallet

    def derive(
        self, session_id: str, role: EscrowEndpointRole, purpose: EscrowKeyPurpose
    ) -> EscrowKeyHandle:
        """Derive an escrow key and its public recovery metadata."""
        try:
            wallet_fingerprint = self._wallet_fingerprint()
            path = escrow_derivation_path(session_id, role, purpose)
            key = self._wallet.master_key.derive(path)
            public_key = self._validated_public_key(key)
            metadata = EscrowKeyDerivation(
                session_id=session_id,
                wallet_fingerprint=wallet_fingerprint,
                role=role,
                purpose=purpose,
                derivation_path=path,
                compressed_public_key=public_key.hex(),
            )
            return EscrowKeyHandle(metadata, key)
        except EscrowKeyDerivationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize wallet/key failures
            raise EscrowKeyDerivationError("unable to derive escrow key") from exc

    def restore(self, metadata: EscrowKeyDerivation) -> EscrowKeyHandle:
        """Re-derive and verify an escrow key from public recovery metadata."""
        try:
            if not isinstance(metadata, EscrowKeyDerivation):
                raise EscrowKeyDerivationError("invalid escrow key metadata")
            if metadata.wallet_fingerprint != self._wallet_fingerprint():
                raise EscrowKeyDerivationError("escrow key metadata belongs to another wallet")

            expected_path = escrow_derivation_path(
                metadata.session_id, metadata.role, metadata.purpose
            )
            if metadata.derivation_path != expected_path:
                raise EscrowKeyDerivationError("escrow key metadata has an invalid derivation path")

            key = self._wallet.master_key.derive(expected_path)
            if self._validated_public_key(key).hex() != metadata.compressed_public_key:
                raise EscrowKeyDerivationError(
                    "escrow key metadata public key does not match wallet"
                )
            return EscrowKeyHandle(metadata, key)
        except EscrowKeyDerivationError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize wallet/key failures
            raise EscrowKeyDerivationError("unable to restore escrow key") from exc

    def _wallet_fingerprint(self) -> str:
        fingerprint = self._wallet.wallet_fingerprint
        if not isinstance(fingerprint, str) or len(fingerprint) != 8:
            raise EscrowKeyDerivationError("wallet fingerprint is invalid")
        try:
            fingerprint_bytes = bytes.fromhex(fingerprint)
        except ValueError as exc:
            raise EscrowKeyDerivationError("wallet fingerprint is invalid") from exc
        if fingerprint != fingerprint.lower() or len(fingerprint_bytes) != 4:
            raise EscrowKeyDerivationError("wallet fingerprint is invalid")

        expected_fingerprint = self._wallet.master_key.derive("m/0").fingerprint.hex()
        if fingerprint != expected_fingerprint:
            raise EscrowKeyDerivationError("wallet fingerprint does not match wallet key")
        return fingerprint

    @staticmethod
    def _validated_public_key(key: HDKey) -> bytes:
        public_key = key.get_public_key_bytes(compressed=True)
        if len(public_key) != 33 or public_key[0] not in (0x02, 0x03):
            raise EscrowKeyDerivationError("derived escrow public key is invalid")
        return public_key
