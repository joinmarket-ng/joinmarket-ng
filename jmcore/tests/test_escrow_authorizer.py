"""Escrow authorizer derivation and the digest contract shared with patched LND."""

from __future__ import annotations

import pytest
from bitcointx.core.key import XOnlyPubKey

from jmcore.crypto import mnemonic_to_seed
from jmcore.escrow_authorizer import (
    EscrowAuthorizer,
    EscrowAuthorizerError,
    authorization_digest,
)

SEED = bytes(range(64))
REGTEST_GENESIS = bytes.fromhex("0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206")[
    ::-1
]

# Same inputs and digest as TestAuthorizationDigestVector in
# lnd/patches/0003-feat-channelescrowrpc-require-an-enrolled-authorizer.patch.
VECTOR = {
    "chain_hash": REGTEST_GENESIS,
    "node_pubkey": b"\x02" + b"\x11" * 32,
    "funding_txid": b"\x33" * 32,
    "output_index": 1,
    "session_id": b"\x44" * 32,
    "parent_tx": b"exact parent transaction bytes",
    "prevouts": [(1_000, b"\x00\x14" + b"\x55" * 20), (2_000, b"\x51\x20" + b"\x66" * 32)],
    "channel_input_index": 1,
}
VECTOR_DIGEST = "bb38a59c038d846870187ec7a70c97dd8f676daff2cb857aa3bf748a573a7c29"


def test_digest_matches_the_lnd_vector() -> None:
    assert authorization_digest(**VECTOR).hex() == VECTOR_DIGEST  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("node_pubkey", b"\x03" + b"\x11" * 32),
        ("output_index", 0),
        ("parent_tx", b"other parent"),
        ("prevouts", [(2_000, b"\x51\x20" + b"\x66" * 32), (1_000, b"\x00\x14" + b"\x55" * 20)]),
        ("channel_input_index", 0),
        ("policy", 2),
    ],
)
def test_digest_commits_to_each_field(field: str, value: object) -> None:
    assert authorization_digest(**{**VECTOR, field: value}).hex() != VECTOR_DIGEST  # type: ignore[arg-type]


def test_digest_rejects_malformed_fields() -> None:
    with pytest.raises(EscrowAuthorizerError, match="node_pubkey"):
        authorization_digest(**{**VECTOR, "node_pubkey": b"\x02" * 32})  # type: ignore[arg-type]
    with pytest.raises(EscrowAuthorizerError, match="parent"):
        authorization_digest(**{**VECTOR, "prevouts": []})  # type: ignore[arg-type]
    with pytest.raises(EscrowAuthorizerError, match="range"):
        authorization_digest(**{**VECTOR, "output_index": -1})  # type: ignore[arg-type]


def test_key_is_deterministic_per_wallet_and_network() -> None:
    first = EscrowAuthorizer(SEED, "regtest")
    assert first.public_key == EscrowAuthorizer(SEED, "regtest").public_key
    assert len(first.public_key) == 32
    assert first.public_key != EscrowAuthorizer(SEED, "signet").public_key
    assert first.public_key != EscrowAuthorizer(b"\x01" * 64, "regtest").public_key

    mnemonic = "abandon " * 11 + "about"
    from_words = EscrowAuthorizer.from_mnemonic(mnemonic, "pass", "regtest")
    assert (
        from_words.public_key
        == EscrowAuthorizer(mnemonic_to_seed(mnemonic, "pass"), "regtest").public_key
    )


def test_signature_verifies_and_close_revokes() -> None:
    authorizer = EscrowAuthorizer(SEED, "regtest")
    digest = bytes.fromhex(VECTOR_DIGEST)
    signature = authorizer.sign(digest)
    assert len(signature) == 64
    assert XOnlyPubKey(authorizer.public_key).verify_schnorr(digest, signature)
    with pytest.raises(EscrowAuthorizerError):
        authorizer.sign(digest[:31])
    authorizer.close()
    with pytest.raises(EscrowAuthorizerError, match="closed"):
        authorizer.sign(digest)
