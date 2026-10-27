"""Seed-derived key material for managed Lightning nodes.

Each managed LND node is initialized from an extended master key derived from
the wallet seed with SLIP-0021, under a label path that names the network, a
random installation epoch, the mixdepth, and a rotation generation. The epoch
makes every allocation fresh: two installations restored from the same seed
never derive the same node identity. See docs/technical/standard-taproot-setup.md.

Like ``WalletMarketKeys``, this is an in-process capability boundary: only the
application SLIP-0021 node is retained, never the seed. The node master key is
returned because LND's ``InitWallet`` needs it; callers must pass it only to that
RPC.
"""

from __future__ import annotations

import hmac
import re
import secrets
from dataclasses import dataclass
from threading import RLock

import base58

from jmcore.constants import SECP256K1_N

NETWORKS = frozenset({"mainnet", "testnet", "signet", "regtest"})
MAX_MIXDEPTHS = 10
MAX_GENERATIONS = 1000

_APPLICATION = b"JoinMarket NG"
_NODE_LABEL = b"lightning node v1"
_UNLOCK_LABEL = b"lightning unlock v1"
_BACKUP_LABEL = b"lightning backup v1"
_EPOCH = re.compile(r"^[0-9a-f]{16}$")
# BIP32 serialization versions; LND accepts tprv for testnet, signet and regtest.
_XPRV_VERSION = bytes.fromhex("0488ade4")
_TPRV_VERSION = bytes.fromhex("04358394")


class LightningKeyError(Exception):
    """A managed node key cannot be derived safely."""


def new_epoch() -> str:
    """A fresh installation epoch: 64 random bits, lowercase hex."""
    return secrets.token_hex(8)


@dataclass(frozen=True)
class NodeKeyScope:
    """The public derivation inputs of one managed node identity."""

    network: str
    epoch: str
    mixdepth: int
    generation: int

    def __post_init__(self) -> None:
        if self.network not in NETWORKS:
            raise LightningKeyError(f"unsupported network {self.network!r}")
        if not isinstance(self.epoch, str) or not _EPOCH.fullmatch(self.epoch):
            raise LightningKeyError("epoch must be 16 lowercase hex characters")
        for name, value, bound in (
            ("mixdepth", self.mixdepth, MAX_MIXDEPTHS),
            ("generation", self.generation, MAX_GENERATIONS),
        ):
            if type(value) is not int or not 0 <= value < bound:
                raise LightningKeyError(f"{name} must be an integer in [0, {bound})")

    def labels(self) -> tuple[bytes, ...]:
        return (
            self.network.encode(),
            self.epoch.encode(),
            f"mixdepth {self.mixdepth}".encode(),
            f"generation {self.generation}".encode(),
        )


def slip21_node(seed: bytes, labels: tuple[bytes, ...]) -> bytes:
    """The 64-byte SLIP-0021 node at ``labels``; the key is its second half."""
    node = hmac.digest(b"Symmetric key seed", seed, "sha512")
    for label in labels:
        node = hmac.digest(node[:32], b"\x00" + label, "sha512")
    return node


def bip32_master_key(entropy: bytes, network: str) -> str:
    """Serialize the BIP32 master private key of ``entropy`` (depth 0)."""
    digest = hmac.digest(b"Bitcoin seed", entropy, "sha512")
    key, chain_code = digest[:32], digest[32:]
    if not 0 < int.from_bytes(key, "big") < SECP256K1_N:
        raise LightningKeyError("derived master key is invalid; this is astronomically rare")
    version = _XPRV_VERSION if network == "mainnet" else _TPRV_VERSION
    payload = version + b"\x00" + b"\x00" * 4 + b"\x00" * 4 + chain_code + b"\x00" + key
    return base58.b58encode_check(payload).decode("ascii")


class WalletLightningKeys:
    """Derive managed node keys and unlock passwords; ``close`` revokes callers."""

    __slots__ = ("_lock", "_network", "_root")

    def __init__(self, seed: bytes, network: str) -> None:
        if not isinstance(seed, bytes) or len(seed) != 64:
            raise ValueError("Lightning keys require a 64-byte binary BIP39 seed")
        if network not in NETWORKS:
            raise LightningKeyError(f"unsupported network {network!r}")
        self._root: bytes | None = slip21_node(seed, (_APPLICATION,))
        self._network = network
        self._lock = RLock()

    @property
    def network(self) -> str:
        return self._network

    def close(self) -> None:
        with self._lock:
            self._root = None

    def node_master_key(self, scope: NodeKeyScope) -> str:
        """The extended master key for LND ``InitWallet``; never log or store it."""
        return bip32_master_key(self._key(_NODE_LABEL, scope), self._network)

    def unlock_password(self, scope: NodeKeyScope) -> bytes:
        """The node's LND wallet password, for the unlock RPCs only."""
        return self._key(_UNLOCK_LABEL, scope).hex().encode("ascii")

    def backup_key(self) -> bytes:
        """A 32-byte key encrypting this wallet's node backups on this network."""
        return self._derive((_BACKUP_LABEL, self._network.encode()))

    def _key(self, purpose: bytes, scope: NodeKeyScope) -> bytes:
        if not isinstance(scope, NodeKeyScope):
            raise TypeError("scope must be a NodeKeyScope")
        if scope.network != self._network:
            raise LightningKeyError("node network does not match the wallet")
        return self._derive((purpose, *scope.labels()))

    def _derive(self, labels: tuple[bytes, ...]) -> bytes:
        with self._lock:
            if self._root is None:
                raise LightningKeyError("wallet Lightning keys are closed")
            node = self._root
            for label in labels:
                node = hmac.digest(node[:32], b"\x00" + label, "sha512")
            return node[32:]
