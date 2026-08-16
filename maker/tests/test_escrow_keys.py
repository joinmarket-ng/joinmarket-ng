"""Tests for wallet-derived channel escrow keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
from coincurve import PublicKeyXOnly
from jmswap.bitcoin_escrow import timeout_nonce
from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.service import WalletService
from pydantic import ValidationError

from maker.escrow_keys import (
    EscrowEndpointRole,
    EscrowKeyDerivation,
    EscrowKeyDerivationError,
    EscrowKeyPurpose,
    WalletEscrowKeyDeriver,
    derive_timeout_nonce_randomness,
    escrow_derivation_path,
)

_TEST_SEED = bytes(range(64))
_SESSION_ID = "channel-session-001"
_EDGE_ID = "a1" * 32
_OTHER_EDGE_ID = "b2" * 32
_TIMEOUT_TXID = "c3" * 32
_TIMEOUT_SIGHASH = bytes.fromhex("d4" * 32)


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


@pytest.mark.parametrize(
    ("purpose", "expected_path"),
    [
        (
            EscrowKeyPurpose.ESCROW,
            "m/1434376035'/1'/0'/0'/1449922420'/174632801'/1902264426'/909315835'",
        ),
        (
            EscrowKeyPurpose.CLAIM,
            "m/1434376035'/1'/0'/1'/1449922420'/174632801'/1902264426'/909315835'",
        ),
        (
            EscrowKeyPurpose.TIMEOUT_DESTINATION,
            "m/1434376035'/1'/0'/2'/1449922420'/174632801'/1902264426'/909315835'",
        ),
        (
            EscrowKeyPurpose.NONCE_SEED,
            "m/1434376035'/1'/0'/3'/1449922420'/174632801'/1902264426'/909315835'",
        ),
    ],
)
def test_canonical_derivation_path_fixed_vector(
    purpose: EscrowKeyPurpose, expected_path: str
) -> None:
    assert (
        escrow_derivation_path("fixed-vector-session", EscrowEndpointRole.MAKER, purpose)
        == expected_path
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

    assert len({handle.metadata.derivation_path for handle in handles}) == 8
    assert len({handle.public_key_bytes() for handle in handles}) == 8


def test_metadata_json_and_handle_repr_exclude_private_data() -> None:
    handle = make_deriver().derive(_SESSION_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.CLAIM)
    private_key_hex = handle.private_key_bytes().hex()

    assert "private" not in handle.metadata.model_dump_json()
    assert private_key_hex not in handle.metadata.model_dump_json()
    assert "randomness" not in handle.metadata.model_dump_json()
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


def test_timeout_nonce_randomness_is_deterministic_and_restorable() -> None:
    deriver = make_deriver()
    nonce_seed = deriver.derive(_EDGE_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.NONCE_SEED)
    escrow = deriver.derive(_EDGE_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.ESCROW)

    randomness = derive_timeout_nonce_randomness(
        nonce_seed,
        edge_id=_EDGE_ID.upper(),
        timeout_txid=_TIMEOUT_TXID.upper(),
        timeout_sighash=_TIMEOUT_SIGHASH,
        role=EscrowEndpointRole.MAKER,
    )
    repeated_randomness = derive_timeout_nonce_randomness(
        nonce_seed,
        edge_id=_EDGE_ID,
        timeout_txid=_TIMEOUT_TXID,
        timeout_sighash=_TIMEOUT_SIGHASH,
        role=EscrowEndpointRole.MAKER,
    )
    _, public_nonce = timeout_nonce(escrow.public_key_bytes(), randomness)
    restored = make_deriver(make_wallet()).restore(nonce_seed.metadata)
    restored_escrow = make_deriver(make_wallet()).restore(escrow.metadata)
    restored_randomness = derive_timeout_nonce_randomness(
        restored,
        edge_id=_EDGE_ID,
        timeout_txid=_TIMEOUT_TXID,
        timeout_sighash=_TIMEOUT_SIGHASH,
        role=EscrowEndpointRole.MAKER,
    )
    _, restored_public_nonce = timeout_nonce(
        restored_escrow.public_key_bytes(), restored_randomness
    )

    assert len(randomness) == 32
    assert randomness == repeated_randomness == restored_randomness
    assert public_nonce == restored_public_nonce
    serialized_metadata = nonce_seed.metadata.model_dump_json()
    assert "private" not in serialized_metadata
    assert "randomness" not in serialized_metadata


def test_timeout_nonce_randomness_changes_with_transcript_or_key() -> None:
    deriver = make_deriver()
    maker_nonce_seed = deriver.derive(
        _EDGE_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.NONCE_SEED
    )
    baseline = derive_timeout_nonce_randomness(
        maker_nonce_seed,
        edge_id=_EDGE_ID,
        timeout_txid=_TIMEOUT_TXID,
        timeout_sighash=_TIMEOUT_SIGHASH,
        role=EscrowEndpointRole.MAKER,
    )
    changed_txid = derive_timeout_nonce_randomness(
        maker_nonce_seed,
        edge_id=_EDGE_ID,
        timeout_txid="e5" * 32,
        timeout_sighash=_TIMEOUT_SIGHASH,
        role=EscrowEndpointRole.MAKER,
    )
    changed_sighash = derive_timeout_nonce_randomness(
        maker_nonce_seed,
        edge_id=_EDGE_ID,
        timeout_txid=_TIMEOUT_TXID,
        timeout_sighash=bytes.fromhex("f6" * 32),
        role=EscrowEndpointRole.MAKER,
    )
    peer_nonce_seed = deriver.derive(_EDGE_ID, EscrowEndpointRole.PEER, EscrowKeyPurpose.NONCE_SEED)
    changed_role = derive_timeout_nonce_randomness(
        peer_nonce_seed,
        edge_id=_EDGE_ID,
        timeout_txid=_TIMEOUT_TXID,
        timeout_sighash=_TIMEOUT_SIGHASH,
        role=EscrowEndpointRole.PEER,
    )
    changed_session_seed = deriver.derive(
        _OTHER_EDGE_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.NONCE_SEED
    )
    changed_session = derive_timeout_nonce_randomness(
        changed_session_seed,
        edge_id=_OTHER_EDGE_ID,
        timeout_txid=_TIMEOUT_TXID,
        timeout_sighash=_TIMEOUT_SIGHASH,
        role=EscrowEndpointRole.MAKER,
    )

    assert baseline not in {changed_txid, changed_sighash, changed_role, changed_session}


def test_timeout_nonce_randomness_rejects_wrong_nonce_seed_metadata() -> None:
    deriver = make_deriver()
    nonce_seed = deriver.derive(_EDGE_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.NONCE_SEED)
    wrong_purpose = deriver.derive(_EDGE_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.CLAIM)
    wrong_role = deriver.derive(_EDGE_ID, EscrowEndpointRole.PEER, EscrowKeyPurpose.NONCE_SEED)
    wrong_session = deriver.derive(
        _OTHER_EDGE_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.NONCE_SEED
    )
    arguments = {
        "edge_id": _EDGE_ID,
        "timeout_txid": _TIMEOUT_TXID,
        "timeout_sighash": _TIMEOUT_SIGHASH,
        "role": EscrowEndpointRole.MAKER,
    }

    with pytest.raises(EscrowKeyDerivationError, match="invalid purpose"):
        derive_timeout_nonce_randomness(wrong_purpose, **arguments)
    with pytest.raises(EscrowKeyDerivationError, match="role does not match"):
        derive_timeout_nonce_randomness(wrong_role, **arguments)
    with pytest.raises(EscrowKeyDerivationError, match="session does not match"):
        derive_timeout_nonce_randomness(wrong_session, **arguments)
    assert nonce_seed.metadata.purpose is EscrowKeyPurpose.NONCE_SEED


@pytest.mark.parametrize(
    ("edge_id", "timeout_txid", "timeout_sighash", "role"),
    [
        ("", _TIMEOUT_TXID, _TIMEOUT_SIGHASH, EscrowEndpointRole.MAKER),
        ("00" * 31, _TIMEOUT_TXID, _TIMEOUT_SIGHASH, EscrowEndpointRole.MAKER),
        ("gg" * 32, _TIMEOUT_TXID, _TIMEOUT_SIGHASH, EscrowEndpointRole.MAKER),
        (_EDGE_ID, "", _TIMEOUT_SIGHASH, EscrowEndpointRole.MAKER),
        (_EDGE_ID, "00" * 33, _TIMEOUT_SIGHASH, EscrowEndpointRole.MAKER),
        (_EDGE_ID, "gg" * 32, _TIMEOUT_SIGHASH, EscrowEndpointRole.MAKER),
        (_EDGE_ID, _TIMEOUT_TXID, b"", EscrowEndpointRole.MAKER),
        (_EDGE_ID, _TIMEOUT_TXID, b"\x00" * 31, EscrowEndpointRole.MAKER),
        (_EDGE_ID, _TIMEOUT_TXID, b"\x00" * 33, EscrowEndpointRole.MAKER),
        (_EDGE_ID, _TIMEOUT_TXID, _TIMEOUT_SIGHASH, "maker"),
    ],
)
def test_timeout_nonce_randomness_rejects_malformed_inputs(
    edge_id: str, timeout_txid: str, timeout_sighash: bytes, role: EscrowEndpointRole | str
) -> None:
    nonce_seed = make_deriver().derive(
        _EDGE_ID, EscrowEndpointRole.MAKER, EscrowKeyPurpose.NONCE_SEED
    )

    with pytest.raises(EscrowKeyDerivationError):
        derive_timeout_nonce_randomness(
            nonce_seed,
            edge_id=edge_id,
            timeout_txid=timeout_txid,
            timeout_sighash=timeout_sighash,
            role=role,  # type: ignore[arg-type]
        )
