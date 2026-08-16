"""Tests for wallet-derived channel escrow keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
from coincurve import PublicKeyXOnly
from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.service import WalletService
from pydantic import ValidationError

from maker.escrow_keys import (
    EscrowEndpointRole,
    EscrowKeyDerivation,
    EscrowKeyDerivationError,
    EscrowKeyPurpose,
    WalletEscrowKeyDeriver,
    escrow_derivation_path,
)

_TEST_SEED = bytes(range(64))
_SESSION_ID = "channel-session-001"


@dataclass(slots=True)
class FakeWallet:
    """Minimal WalletService-compatible value for deterministic key tests."""

    master_key: HDKey
    wallet_fingerprint: str
    root_path: str = "m/84'/1'"


def make_wallet() -> FakeWallet:
    master_key = HDKey.from_seed(_TEST_SEED)
    return FakeWallet(
        master_key=master_key,
        wallet_fingerprint=master_key.derive("m/0").fingerprint.hex(),
    )


def make_deriver(wallet: FakeWallet | None = None) -> WalletEscrowKeyDeriver:
    return WalletEscrowKeyDeriver(cast(WalletService, wallet or make_wallet()))


def test_canonical_derivation_path_fixed_vector() -> None:
    assert (
        escrow_derivation_path(
            "fixed-vector-session", EscrowEndpointRole.MAKER, EscrowKeyPurpose.ESCROW
        )
        == "m/1434376035'/1'/0'/0'/1449922420'/174632801'/1902264426'/909315835'"
    )


def test_derive_is_deterministic_and_outside_wallet_bip84_branch() -> None:
    wallet = make_wallet()
    deriver = make_deriver(wallet)
    first = deriver.derive(_SESSION_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.ESCROW)
    second = deriver.derive(_SESSION_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.ESCROW)

    assert first.metadata == second.metadata
    assert first.public_key_bytes() == second.public_key_bytes()
    assert not first.metadata.derivation_path.startswith(f"{wallet.root_path}/")


def test_all_role_and_purpose_combinations_are_distinct() -> None:
    deriver = make_deriver()
    handles = [
        deriver.derive(_SESSION_ID, role, purpose)
        for role in EscrowEndpointRole
        for purpose in EscrowKeyPurpose
    ]

    assert len({handle.metadata.derivation_path for handle in handles}) == 6
    assert len({handle.public_key_bytes() for handle in handles}) == 6


def test_metadata_json_and_handle_repr_exclude_private_data() -> None:
    handle = make_deriver().derive(_SESSION_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.CLAIM)
    private_key_hex = handle.private_key_bytes().hex()

    assert "private" not in handle.metadata.model_dump_json()
    assert private_key_hex not in handle.metadata.model_dump_json()
    assert "private" not in repr(handle).lower()
    assert private_key_hex not in repr(handle)
    assert not hasattr(handle, "model_dump")


def test_schnorr_signature_verifies_against_xonly_public_key() -> None:
    handle = make_deriver().derive(_SESSION_ID, EscrowEndpointRole.PEER, EscrowKeyPurpose.ESCROW)
    digest = bytes.fromhex("11" * 32)

    signature = handle.sign_digest(digest)

    assert len(signature) == 64
    assert PublicKeyXOnly(handle.public_key_bytes()[1:]).verify(signature, digest)


def test_restore_with_fresh_wallet_service_like_object() -> None:
    handle = make_deriver().derive(
        _SESSION_ID, EscrowEndpointRole.PEER, EscrowKeyPurpose.TIMEOUT_DESTINATION
    )

    restored = make_deriver(make_wallet()).restore(handle.metadata)

    assert restored.metadata == handle.metadata
    assert restored.public_key_bytes() == handle.public_key_bytes()
    assert restored.private_key_bytes() == handle.private_key_bytes()


@pytest.mark.parametrize(
    "field", ["wallet_fingerprint", "compressed_public_key", "derivation_path"]
)
def test_restore_rejects_tampered_metadata(field: str) -> None:
    deriver = make_deriver()
    metadata = deriver.derive(
        _SESSION_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.ESCROW
    ).metadata
    replacements = {
        "wallet_fingerprint": "deadbeef",
        "compressed_public_key": deriver.derive(
            _SESSION_ID, EscrowEndpointRole.PEER, EscrowKeyPurpose.ESCROW
        ).metadata.compressed_public_key,
        "derivation_path": "m/0'",
    }
    payload = metadata.model_dump()
    payload[field] = replacements[field]
    tampered = EscrowKeyDerivation.model_construct(**payload)

    with pytest.raises(EscrowKeyDerivationError):
        deriver.restore(tampered)


@pytest.mark.parametrize("digest", [b"", b"\x00" * 31, b"\x00" * 33])
def test_sign_digest_rejects_invalid_lengths(digest: bytes) -> None:
    handle = make_deriver().derive(_SESSION_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.ESCROW)

    with pytest.raises(EscrowKeyDerivationError, match="32-byte digest"):
        handle.sign_digest(digest)


def test_invalid_session_is_rejected() -> None:
    with pytest.raises(EscrowKeyDerivationError, match="non-empty"):
        make_deriver().derive("", EscrowEndpointRole.MAKER, EscrowKeyPurpose.ESCROW)
    with pytest.raises(ValidationError):
        EscrowKeyDerivation(
            session_id="",
            wallet_fingerprint=make_wallet().wallet_fingerprint,
            role=EscrowEndpointRole.MAKER,
            purpose=EscrowKeyPurpose.ESCROW,
            derivation_path="m/0'",
            compressed_public_key="02" + "11" * 32,
        )
