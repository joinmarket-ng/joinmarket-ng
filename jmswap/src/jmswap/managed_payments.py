"""Wallet- and mixdepth-bound admission for managed payments and their history.

New payments use the current ACTIVE node. Historical lookups use the original
pubkey, including retiring/recovering nodes, and never require a replacement.
Validated endpoint snapshots survive registry read failures in this process;
they are never replaced with a newer node's credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from jmswap.channel_ring_nodes import channel_ring_wallet_identity
from jmswap.managed_nodes import (
    ManagedNode,
    ManagedNodeError,
    ManagedNodePaths,
    NodeRegistry,
    RegistryStore,
)

if TYPE_CHECKING:
    from jmswap.lnd_peer import LndPeerClient


class ManagedPaymentRefusedError(ManagedNodeError):
    """No managed node is affirmatively authorized for a new payment."""


@dataclass(frozen=True)
class ManagedPaymentEndpoint:
    """One explicit identity binding, not an endpoint inferred from generic config."""

    identity_pubkey: str
    network: str
    rpc_port: int
    tls_cert: Path
    macaroon: Path

    def client(self) -> LndPeerClient:
        from jmswap.lnd_peer import LndPeerClient

        return LndPeerClient(
            f"127.0.0.1:{self.rpc_port}", self.tls_cert.read_bytes(), self.macaroon.read_bytes()
        )


@dataclass(frozen=True)
class ManagedPaymentSource:
    store: RegistryStore
    wallet_identity: str
    mixdepth: int
    _known: dict[str, ManagedPaymentEndpoint] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def for_wallet(
        cls, data_dir: Path, network: str, wallet_public_key: bytes, mixdepth: int
    ) -> ManagedPaymentSource:
        identity = channel_ring_wallet_identity(wallet_public_key)
        paths = ManagedNodePaths.for_wallet(data_dir, network, identity)
        return cls(RegistryStore(paths), identity, mixdepth)

    def _registry(self) -> NodeRegistry:
        registry = self.store.load()
        if registry is None:
            raise ManagedPaymentRefusedError("managed payment registry is unavailable")
        if registry.wallet_identity != self.wallet_identity:
            raise ManagedPaymentRefusedError("managed payment registry belongs to another wallet")
        return registry

    def _remember(self, node: ManagedNode) -> ManagedPaymentEndpoint:
        if node.identity_pubkey is None or node.mixdepth != self.mixdepth:
            raise ManagedPaymentRefusedError("managed payment node identity is unavailable")
        endpoint = ManagedPaymentEndpoint(
            node.identity_pubkey,
            self.store.paths.network,
            node.rpc_port,
            self.store.paths.tls_cert(node),
            self.store.paths.macaroon(node),
        )
        known = self._known.get(node.identity_pubkey)
        if known is not None and known != endpoint:
            raise ManagedPaymentRefusedError("managed payment identity binding changed")
        self._known[node.identity_pubkey] = endpoint
        return endpoint

    def active_endpoint(self) -> ManagedPaymentEndpoint:
        """Resolve one current source from a single authoritative registry snapshot."""
        registry = self._registry()
        node = registry.active(self.mixdepth)
        if node is None or node.identity_pubkey is None:
            raise ManagedPaymentRefusedError("no active managed payment node for this mixdepth")
        # A copied identity would link mixdepths or defeat rotation. It cannot
        # authorize a new payment, even if only one entry is currently ACTIVE.
        matches = [item for item in registry.nodes if item.identity_pubkey == node.identity_pubkey]
        if len(matches) != 1:
            raise ManagedPaymentRefusedError("managed payment identity is not unique")
        return self._remember(node)

    def historical_endpoint(self, payer: str) -> ManagedPaymentEndpoint:
        """The original same-mixdepth endpoint, never a current-node fallback.

        A previously validated snapshot stays useful under metadata loss. A
        fresh process needs an authoritative, unique registry binding instead.
        Neither path establishes whether a missing payment ever settled.
        """
        known = self._known.get(payer)
        if known is not None:
            return known
        registry = self._registry()
        matches = [
            node
            for node in registry.nodes
            if node.mixdepth == self.mixdepth and node.identity_pubkey == payer
        ]
        if len(matches) != 1:
            raise ManagedPaymentRefusedError("original payment node is unavailable or ambiguous")
        return self._remember(matches[0])
