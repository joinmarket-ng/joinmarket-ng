"""SLIP-0021 vectors and wallet market capability contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Literal

import pytest
from bitcointx.core.key import CKey
from nacl.public import PublicKey, SealedBox
from pydantic import ValidationError

from jmcore import market_keys
from jmcore.constants import SECP256K1_N
from jmcore.credential_market import (
    MAX_MARKET_BYTES,
    BondReference,
    MarketAuthorization,
    MarketError,
    MarketModel,
)
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_keys import (
    BoundMarketKeys,
    MarketKeyError,
    MarketKeysClosedError,
    MarketKeyScope,
    WalletMarketKeys,
)

# Public SLIP-0021 vector: mnemonic "all" twelve times, empty passphrase.
SEED = bytes.fromhex(
    "c76c4ac4f4e4a00d6b274d5c39c700bb4a7ddc04fbc6f78e85ca75007b5b495f74a9"
    "043eeb77bdd53aa6fc3a0e31462270316fa04b8c19114c8798706cd02ac8"
)


class Message(MarketModel):
    version: Literal[1] = 1
    payload: str = "synthetic market message"


@pytest.fixture
def scope() -> MarketKeyScope:
    return MarketKeyScope(network="regtest", chain_hash="11" * 32, role="buyer", period=4)


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ((), "dbf12b44133eaab506a740f6565cc117228cbf1dd70635cfa8ddfdc9af734756"),
        ((b"SLIP-0021",), "1d065e3ac1bbe5c7fad32cf2305f7d709dc070d672044a19e610c77cdf33de0d"),
        (
            (b"SLIP-0021", b"Master encryption key"),
            "ea163130e35bbafdf5ddee97a17b39cef2be4b4f390180d65b54cf05c6a82fde",
        ),
        (
            (b"SLIP-0021", b"Authentication key"),
            "47194e938ab24cc82bfa25f6486ed54bebe79c40ae2a5a32ea6db294d81861a6",
        ),
    ],
)
def test_upstream_slip21_vectors(labels: tuple[bytes, ...], expected: str) -> None:
    node = market_keys._master_node(SEED)
    for label in labels:
        node = market_keys._child_node(node, label)
    assert node[32:].hex() == expected


def test_seed_recovery_and_existing_wire_formats(scope: MarketKeyScope) -> None:
    first = WalletMarketKeys(SEED, "regtest")
    restored = WalletMarketKeys(SEED, "regtest")
    public_key = first.signing_public_key(scope)
    assert restored.signing_public_key(scope) == public_key
    assert first.sign_document(scope, Message()).verified(Message, public_key.hex()) == Message()
    recipient = first.encryption_public_key(scope)
    ciphertext = SealedBox(PublicKey(recipient)).encrypt(b"synthetic delivery")
    assert restored.encryption_public_key(scope) == recipient
    assert restored.decrypt_message(scope, ciphertext) == b"synthetic delivery"
    assert first._node(
        scope, b"document-signing", b"secp256k1-ecdsa-bitcoin-message"
    ) != first._node(scope, b"message-encryption", b"x25519-xsalsa20-poly1305-sealedbox")


def test_version_one_derivation_profile_vector(scope: MarketKeyScope) -> None:
    keys = WalletMarketKeys(SEED, "regtest")
    assert keys.signing_public_key(scope).hex() == (
        "03a9cbd24b5cf7d1eba3e9b82cb2af5a6a7bda25b49f238475b6ad859ad481bbfd"
    )
    assert keys.encryption_public_key(scope).hex() == (
        "4827536a26038dea66e51f7f988b33b4dc3cacf1a275eb60477283617a2f5a18"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"network": "signet"},
        {"chain_hash": "22" * 32},
        {"role": "seller"},
        {"period": 5},
        {"bond": {"txid": "33" * 32, "vout": 0}},
        {"bond": {"txid": "33" * 32, "vout": 1}},
        {"trade_id": "44" * 32},
    ],
)
def test_context_separation(scope: MarketKeyScope, change: dict[str, object]) -> None:
    changed = MarketKeyScope.model_validate(scope.model_dump() | change)
    first = WalletMarketKeys(SEED, scope.network)
    second = WalletMarketKeys(SEED, changed.network)
    assert first.signing_public_key(scope) != second.signing_public_key(changed)
    assert first.encryption_public_key(scope) != second.encryption_public_key(changed)
    signed = first.sign_document(scope, Message())
    with pytest.raises(MarketError):
        signed.verified(Message, second.signing_public_key(changed).hex())
    ciphertext = SealedBox(PublicKey(first.encryption_public_key(scope))).encrypt(b"delivery")
    with pytest.raises(MarketKeyError, match="decrypt"):
        second.decrypt_message(changed, ciphertext)


def test_bond_outpoint_components_are_separate(scope: MarketKeyScope) -> None:
    keys = WalletMarketKeys(SEED, "regtest")
    points = [("33" * 32, 0), ("33" * 32, 1), ("44" * 32, 0)]
    public_keys = {
        keys.signing_public_key(
            MarketKeyScope.model_validate(
                scope.model_dump() | {"bond": {"txid": txid, "vout": vout}}
            )
        )
        for txid, vout in points
    }
    assert len(public_keys) == 3


def test_seed_and_wallet_network_separation(scope: MarketKeyScope) -> None:
    first = WalletMarketKeys(SEED, "regtest")
    other = WalletMarketKeys(b"\x01" * 64, "regtest")
    assert first.signing_public_key(scope) != other.signing_public_key(scope)
    assert first.encryption_public_key(scope) != other.encryption_public_key(scope)
    with pytest.raises(MarketKeyError, match="network"):
        WalletMarketKeys(SEED, "signet").signing_public_key(scope)


def test_private_ledger_identity_is_wallet_wide() -> None:
    first = WalletMarketKeys(SEED, "regtest")
    restored = WalletMarketKeys(SEED, "signet")
    other = WalletMarketKeys(b"\x01" * 64, "regtest")
    assert len(first.ledger_identity()) == 64
    assert first.ledger_identity() == restored.ledger_identity()
    assert first.ledger_identity() != other.ledger_identity()
    first.close()
    with pytest.raises(MarketKeysClosedError):
        first.ledger_identity()


def test_bound_keys_delegate_operations_and_snapshot_scope() -> None:
    parent = WalletMarketKeys(SEED, "regtest")
    original = MarketKeyScope(
        network="regtest",
        chain_hash="11" * 32,
        role="seller",
        period=4,
        bond=ExternalPoDLEOutpoint(txid="22" * 32, vout=1),
    )
    bound = parent.bind(original)

    assert isinstance(bound, BoundMarketKeys)
    assert bound.wallet_id == parent.ledger_identity()
    assert bound.signing_public_key() == parent.signing_public_key(original)
    assert bound.encryption_public_key() == parent.encryption_public_key(original)
    assert bound.sign_document(Message()) == parent.sign_document(original, Message())
    ciphertext = SealedBox(PublicKey(bound.encryption_public_key())).encrypt(b"bound delivery")
    assert bound.decrypt_message(ciphertext) == b"bound delivery"

    object.__setattr__(original, "period", 5)
    copied_scope = bound.scope
    object.__setattr__(copied_scope, "period", 6)
    assert bound.scope.period == 4
    assert bound.signing_public_key() != parent.signing_public_key(original)
    with pytest.raises(MarketKeyError, match="network"):
        parent.bind(original.model_copy(update={"network": "signet"}))


@pytest.mark.parametrize(
    "scope_change",
    (
        {"role": "buyer"},
        {"network": "signet"},
        {"period": 5},
        {"bond": {"txid": "33" * 32, "vout": 1}},
        {"trade_id": "44" * 32},
    ),
)
def test_bound_seller_scope_must_match_authorization(scope_change: dict[str, object]) -> None:
    bond = BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid="22" * 32, vout=1),
        pubkey=bytes(CKey(bytes([0x11]) * 32).pub).hex(),
        locktime=600_000_000,
    )
    base_scope = MarketKeyScope(
        network="regtest",
        chain_hash="11" * 32,
        role="seller",
        period=4,
        bond=bond.outpoint,
    )
    scope = MarketKeyScope.model_validate(base_scope.model_dump() | scope_change)
    parent = WalletMarketKeys(SEED, scope.network)
    bound = parent.bind(scope)
    authority = MarketAuthorization(
        bond=bond,
        period=4,
        seller_pubkey=bound.signing_public_key().hex(),
    )

    with pytest.raises(MarketKeyError):
        bound.validate_seller_authorization(authority)


def test_bound_keys_are_revoked_with_their_parent(scope: MarketKeyScope) -> None:
    parent = WalletMarketKeys(SEED, "regtest")
    bound = parent.bind(scope)
    parent.close()

    for operation in (
        lambda: bound.wallet_id,
        lambda: bound.signing_public_key(),
        lambda: bound.encryption_public_key(),
        lambda: bound.sign_document(Message()),
        lambda: bound.decrypt_message(b"x" * 48),
        lambda: parent.bind(scope),
    ):
        with pytest.raises(MarketKeysClosedError):
            operation()


def test_revocation_applies_to_all_operations(scope: MarketKeyScope) -> None:
    keys = WalletMarketKeys(SEED, "regtest")
    retained = keys
    keys.close()
    keys.close()
    for operation in (
        lambda: retained.signing_public_key(scope),
        lambda: retained.sign_document(scope, Message()),
        lambda: retained.encryption_public_key(scope),
        lambda: retained.decrypt_message(scope, b"x" * 48),
    ):
        with pytest.raises(MarketKeysClosedError):
            operation()
    assert SEED.hex() not in repr(keys)


def test_invalid_scalars_are_rejected_not_reduced(
    scope: MarketKeyScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = WalletMarketKeys(SEED, "regtest")
    original = market_keys._child_node

    def child(node: bytes, label: bytes) -> bytes:
        if label in (b"0", b"1", b"2"):
            scalar = {b"0": 0, b"1": SECP256K1_N, b"2": 2}[label]
            return b"\x00" * 32 + scalar.to_bytes(32, "big")
        return original(node, label)

    monkeypatch.setattr(market_keys, "_child_node", child)
    assert keys.signing_public_key(scope) == bytes(CKey.from_secret_bytes((2).to_bytes(32)).pub)


@pytest.mark.parametrize("ciphertext", [b"", b"x" * 47, b"x" * (MAX_MARKET_BYTES + 49)])
def test_message_size_limit(scope: MarketKeyScope, ciphertext: bytes) -> None:
    with pytest.raises(MarketKeyError, match="length"):
        WalletMarketKeys(SEED, "regtest").decrypt_message(scope, ciphertext)


def test_scope_validation_cannot_be_bypassed_by_model_copy(scope: MarketKeyScope) -> None:
    invalid = scope.model_copy(update={"period": True})
    with pytest.raises(ValidationError):
        WalletMarketKeys(SEED, "regtest").signing_public_key(invalid)


@pytest.mark.parametrize("ciphertext", [None, "x" * 48, bytearray(b"x" * 48)])
def test_nonbytes_message_rejected(scope: MarketKeyScope, ciphertext: object) -> None:
    with pytest.raises(MarketKeyError, match="length"):
        WalletMarketKeys(SEED, "regtest").decrypt_message(scope, ciphertext)  # type: ignore[arg-type]


def test_close_waits_for_in_flight_operation(
    scope: MarketKeyScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = WalletMarketKeys(SEED, "regtest")
    entered, release, closing, closed = Event(), Event(), Event(), Event()
    original = market_keys._child_node

    def child(node: bytes, label: bytes) -> bytes:
        if label == b"v1":
            entered.set()
            assert release.wait(5)
        return original(node, label)

    def close() -> None:
        closing.set()
        keys.close()
        closed.set()

    monkeypatch.setattr(market_keys, "_child_node", child)
    with ThreadPoolExecutor(max_workers=2) as pool:
        operation = pool.submit(keys.signing_public_key, scope)
        try:
            assert entered.wait(5)
            revocation = pool.submit(close)
            assert closing.wait(5)
            assert not closed.wait(0.05)
        finally:
            release.set()
        assert len(operation.result(timeout=5)) == 33
        revocation.result(timeout=5)
    with pytest.raises(MarketKeysClosedError):
        keys.signing_public_key(scope)


@pytest.mark.parametrize("seed", [b"", b"x" * 32, b"x" * 63, b"x" * 65])
def test_requires_binary_bip39_seed(seed: bytes) -> None:
    with pytest.raises(ValueError, match="64-byte"):
        WalletMarketKeys(seed, "regtest")
