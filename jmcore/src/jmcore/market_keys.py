"""Revocable wallet-native market keys, independent of the BIP32 spending tree.

This is an in-process capability boundary, not a sandbox or guaranteed memory
zeroization. Only the application-specific SLIP-0021 node is retained. Callers
receive public keys, signatures, or plaintext, never private key material.
"""

from __future__ import annotations

import hashlib
import hmac
from threading import RLock
from typing import Literal

from bitcointx.core.key import CKey
from nacl.bindings import crypto_box_SEALBYTES
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, SealedBox
from pydantic import Field

from jmcore.constants import SECP256K1_N
from jmcore.credential_market import (
    MAX_MARKET_BYTES,
    Hex32,
    MarketAuthorization,
    MarketModel,
    Network,
    SignedDocument,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLEOutpoint

_APPLICATION = b"JoinMarket NG credential market"


class MarketKeyError(Exception):
    """A market key operation cannot be completed safely."""


class MarketKeysClosedError(MarketKeyError):
    """The owning wallet session has revoked its market capability."""


class MarketKeyScope(MarketModel):
    """Public, canonical derivation inputs, not proof of chain or bond ownership.

    ``chain_hash`` is the chain's genesis block hash, not its moving tip. Distinct
    optional bond/trade labels support both period identities and trade keys.
    """

    network: Network
    chain_hash: Hex32
    role: Literal["buyer", "seller"]
    period: int = Field(ge=0, le=65534)
    bond: ExternalPoDLEOutpoint | None = None
    trade_id: Hex32 | None = None


def _master_node(seed: bytes) -> bytes:
    return hmac.digest(b"Symmetric key seed", seed, "sha512")


def _child_node(node: bytes, label: bytes) -> bytes:
    return hmac.digest(node[:32], b"\x00" + label, "sha512")


class WalletMarketKeys:
    """Wallet-owned signing/decryption operations; ``close`` revokes all callers.

    Construction must occur at the unlocked wallet's binary BIP39 seed boundary.
    No market operation authorizes issuance or proves that a ledger is complete.
    Network validation is deferred until use to leave ordinary wallet startup
    behavior unchanged for callers using unsupported network names.
    """

    __slots__ = ("_lock", "_network", "_root")

    def __init__(self, seed: bytes, network: str) -> None:
        if not isinstance(seed, bytes) or len(seed) != 64:
            raise ValueError("Market keys require a 64-byte binary BIP39 seed")
        self._root: bytes | None = _child_node(_master_node(seed), _APPLICATION)
        self._network = network
        self._lock = RLock()

    def close(self) -> None:
        """Revoke future operations, including through retained references."""
        with self._lock:
            self._root = None

    def ledger_identity(self) -> str:
        """Return a wallet-wide storage identifier, never a public market identity."""
        with self._lock:
            if self._root is None:
                raise MarketKeysClosedError("Wallet market keys are closed")
            node = _child_node(self._root, b"wallet-ledger-identity-v1")
            return hashlib.sha256(node[32:]).hexdigest()

    def bind(self, scope: MarketKeyScope) -> BoundMarketKeys:
        """Restrict later market operations to one independently copied scope."""
        snapshot = _snapshot_scope(scope)
        # This also validates the network and rejects a closed parent. The derived
        # public key is intentionally not retained by the bound capability.
        self.signing_public_key(snapshot)
        return BoundMarketKeys(self, snapshot)

    def signing_public_key(self, scope: MarketKeyScope) -> bytes:
        with self._lock:
            return bytes(self._signing_key(scope).pub)

    def sign_document(self, scope: MarketKeyScope, body: MarketModel) -> SignedDocument:
        """Sign an existing canonical market document, without exporting a key."""
        with self._lock:
            return sign_document(body, self._signing_key(scope))

    def encryption_public_key(self, scope: MarketKeyScope) -> bytes:
        with self._lock:
            return bytes(self._encryption_key(scope).public_key)

    def decrypt_message(self, scope: MarketKeyScope, ciphertext: bytes) -> bytes:
        """Open a bounded NaCl sealed box; callers must validate its plaintext."""
        with self._lock:
            key = self._encryption_key(scope)
            if not isinstance(ciphertext, bytes) or not (
                crypto_box_SEALBYTES <= len(ciphertext) <= MAX_MARKET_BYTES + crypto_box_SEALBYTES
            ):
                raise MarketKeyError("Invalid sealed market message length")
            try:
                return SealedBox(key).decrypt(ciphertext)
            except CryptoError as exc:
                raise MarketKeyError("Could not decrypt sealed market message") from exc

    def _node(self, scope: MarketKeyScope, purpose: bytes, algorithm: bytes) -> bytes:
        if self._root is None:
            raise MarketKeysClosedError("Wallet market keys are closed")
        # Revalidate even model_construct/model_copy inputs before deriving keys.
        scope = MarketKeyScope.model_validate(scope.model_dump())
        if scope.network != self._network:
            raise MarketKeyError("Market key network does not match the wallet")
        labels = [
            b"v1",
            b"network",
            scope.network.encode("ascii"),
            b"chain",
            scope.chain_hash.encode("ascii"),
            b"role",
            scope.role.encode("ascii"),
            b"period",
            str(scope.period).encode("ascii"),
            b"bond",
        ]
        if scope.bond is None:
            labels.append(b"none")
        else:
            labels.extend(
                (b"outpoint", scope.bond.txid.encode("ascii"), str(scope.bond.vout).encode("ascii"))
            )
        labels.append(b"trade")
        labels.extend(
            (b"none",) if scope.trade_id is None else (b"id", scope.trade_id.encode("ascii"))
        )
        labels.extend((b"purpose", purpose, b"algorithm", algorithm))
        node = self._root
        for label in labels:
            node = _child_node(node, label)
        return node

    def _signing_key(self, scope: MarketKeyScope) -> CKey:
        node = self._node(scope, b"document-signing", b"secp256k1-ecdsa-bitcoin-message")
        # Rejection sampling preserves scalar uniformity; never reduce modulo n.
        for counter in range(256):
            secret = _child_node(node, str(counter).encode("ascii"))[32:]
            if 0 < int.from_bytes(secret, "big") < SECP256K1_N:
                return CKey.from_secret_bytes(secret, compressed=True)
        raise MarketKeyError("Could not derive a valid market signing scalar")

    def _encryption_key(self, scope: MarketKeyScope) -> PrivateKey:
        node = self._node(scope, b"message-encryption", b"x25519-xsalsa20-poly1305-sealedbox")
        return PrivateKey(node[32:])


class BoundMarketKeys:
    """A wallet-owned market capability restricted to one seller or buyer scope."""

    __slots__ = ("_parent", "_scope")

    def __init__(self, parent: WalletMarketKeys, scope: MarketKeyScope) -> None:
        self._parent = parent
        self._scope = _snapshot_scope(scope)

    @property
    def scope(self) -> MarketKeyScope:
        """Return an independent copy of the capability's immutable scope."""
        return _snapshot_scope(self._scope)

    @property
    def wallet_id(self) -> str:
        """Return the owning wallet's storage identity while it remains unlocked."""
        return self._parent.ledger_identity()

    def signing_public_key(self) -> bytes:
        return self._parent.signing_public_key(_snapshot_scope(self._scope))

    def encryption_public_key(self) -> bytes:
        return self._parent.encryption_public_key(_snapshot_scope(self._scope))

    def sign_document(self, body: MarketModel) -> SignedDocument:
        return self._parent.sign_document(_snapshot_scope(self._scope), body)

    def decrypt_message(self, ciphertext: bytes) -> bytes:
        return self._parent.decrypt_message(_snapshot_scope(self._scope), ciphertext)

    def validate_seller_authorization(self, authority: MarketAuthorization) -> None:
        """Require a period/bond seller authority that exactly matches this scope."""
        scope = _snapshot_scope(self._scope)
        try:
            validated_authority = MarketAuthorization.model_validate(authority.model_dump())
        except (AttributeError, ValueError) as exc:
            raise MarketKeyError("Invalid seller authorization") from exc
        if scope.role != "seller":
            raise MarketKeyError("Market key scope is not a seller capability")
        if scope.network != validated_authority.bond.network:
            raise MarketKeyError("Market key scope network does not match authorization")
        if scope.period != validated_authority.period:
            raise MarketKeyError("Market key scope period does not match authorization")
        if scope.bond != validated_authority.bond.outpoint:
            raise MarketKeyError("Market key scope bond does not match authorization")
        if scope.trade_id is not None:
            raise MarketKeyError("Seller market key scope must not be trade-specific")


def _snapshot_scope(scope: MarketKeyScope) -> MarketKeyScope:
    """Revalidate and deep-copy potentially model-constructed scope data."""
    if not isinstance(scope, MarketKeyScope):
        raise MarketKeyError("Market key scope is invalid")
    try:
        return MarketKeyScope.model_validate(scope.model_dump())
    except ValueError as exc:
        raise MarketKeyError("Market key scope is invalid") from exc
