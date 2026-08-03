"""
Peer registry for tracking active peers and their metadata.

Implements Single Responsibility Principle: only manages peer state.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from jmcore.models import NetworkType, PeerInfo, PeerStatus
from jmcore.protocol import FeatureSet
from loguru import logger


class PeerNotFoundError(Exception):
    pass


class PeerOwnershipConflictError(Exception):
    """Raised when a live peer already owns a nick."""


@dataclass(frozen=True)
class PeerOwner:
    connection_id: str
    verified_pubkey: bytes | str | None = None

    @property
    def verified(self) -> bool:
        return self.verified_pubkey is not None


@dataclass(frozen=True)
class DisplacedPeer:
    peer_key: str
    peer: PeerInfo
    connection_id: str


@dataclass(frozen=True)
class RegistrationResult:
    peer_key: str
    connection_id: str
    displaced: tuple[DisplacedPeer, ...] = ()


class PeerRegistry:
    def __init__(self, max_peers: int = 1000):
        self.max_peers = max_peers
        self._peers: dict[str, PeerInfo] = {}
        self._owners: dict[str, PeerOwner] = {}

    def register(
        self,
        peer: PeerInfo,
        connection_id: str | None = None,
        *,
        verified_pubkey: bytes | str | None = None,
    ) -> RegistrationResult:
        """Atomically reserve a peer's nick for one connection.

        Legacy registrations use first-live-writer ownership. A verified registration may
        replace legacy owners, while replacing a verified owner requires the same full pubkey.
        The self-declared location remains metadata and is never an ownership key.
        """
        connection_id = connection_id or uuid4().hex
        location = peer.location_string
        key = peer.nick

        displaced: list[DisplacedPeer] = []
        existing_peer = self._peers.get(key)
        existing_owner = self._owners.get(key)
        if existing_peer is not None and existing_owner is not None:
            if verified_pubkey is None:
                raise PeerOwnershipConflictError(f"Peer nick already registered: {peer.nick}")
            if existing_owner.verified and existing_owner.verified_pubkey != verified_pubkey:
                raise PeerOwnershipConflictError(f"Verified nick already registered: {peer.nick}")
            displaced.append(
                DisplacedPeer(
                    peer_key=key,
                    peer=existing_peer,
                    connection_id=existing_owner.connection_id,
                )
            )

        if len(self._peers) - len(displaced) >= self.max_peers:
            raise ValueError(f"Maximum peers reached: {self.max_peers}")

        for old in displaced:
            self._remove(old.peer_key)

        self._peers[key] = peer
        self._owners[key] = PeerOwner(
            connection_id=connection_id,
            verified_pubkey=verified_pubkey,
        )

        peer.last_seen = datetime.now(UTC)
        logger.info(f"Registered peer: {peer.nick} at {location}")
        return RegistrationResult(
            peer_key=key,
            connection_id=connection_id,
            displaced=tuple(displaced),
        )

    def unregister(self, key: str, expected_connection_id: str | None = None) -> bool:
        if not self.is_current_owner(key, expected_connection_id):
            return False

        peer = self._remove(key)
        if peer is None:
            return False
        logger.info(f"Unregistered peer: {peer.nick} at {peer.location_string}")
        return True

    def _remove(self, key: str) -> PeerInfo | None:
        peer = self._peers.pop(key, None)
        if peer is None:
            return None
        self._owners.pop(key, None)
        return peer

    def is_current_owner(self, key: str, expected_connection_id: str | None) -> bool:
        owner = self._owners.get(key)
        if owner is None:
            return False
        return expected_connection_id is None or owner.connection_id == expected_connection_id

    def get_owner(self, key: str) -> PeerOwner | None:
        return self._owners.get(key)

    def get_connection_id(self, key: str) -> str | None:
        owner = self.get_owner(key)
        return owner.connection_id if owner else None

    def get_by_key(self, key: str) -> PeerInfo | None:
        return self._peers.get(key)

    def get_by_nick(self, nick: str) -> PeerInfo | None:
        return self._peers.get(nick)

    def get_key_by_nick(self, nick: str) -> str | None:
        """Return the routing key currently reserved for ``nick``."""
        return nick if nick in self._peers else None

    def update_status(
        self,
        key: str,
        status: PeerStatus,
        expected_connection_id: str | None = None,
    ) -> bool:
        if not self.is_current_owner(key, expected_connection_id):
            return False
        peer = self.get_by_key(key)
        if peer:
            peer.status = status
            if status in (PeerStatus.CONNECTED, PeerStatus.HANDSHAKED):
                peer.last_seen = datetime.now(UTC)
            return True
        return False

    def update_last_seen(self, key: str, expected_connection_id: str | None = None) -> bool:
        """Update the last_seen timestamp for a peer.

        Called on every received message to track peer liveness for heartbeat.
        """
        if not self.is_current_owner(key, expected_connection_id):
            return False
        peer = self.get_by_key(key)
        if peer:
            peer.last_seen = datetime.now(UTC)
            return True
        return False

    def _iter_connected(self, network: NetworkType | None = None) -> Iterator[PeerInfo]:
        """Iterator over connected peers.

        Creates a snapshot of peers to avoid RuntimeError if dict is modified during iteration.
        """
        for p in list(self._peers.values()):
            if (
                p.status == PeerStatus.HANDSHAKED
                and not p.is_directory
                and (network is None or p.network == network)
            ):
                yield p

    def iter_connected(self, network: NetworkType | None = None) -> Iterator[PeerInfo]:
        """Public memory-efficient iterator over connected peers."""
        return self._iter_connected(network)

    def iter_connected_owners(
        self, network: NetworkType | None = None
    ) -> Iterator[tuple[str, PeerInfo, str]]:
        """Iterate over handshaked peers with their current ownership generation."""
        for key, peer in list(self._peers.items()):
            owner = self._owners.get(key)
            if (
                owner is not None
                and peer.status == PeerStatus.HANDSHAKED
                and not peer.is_directory
                and (network is None or peer.network == network)
            ):
                yield key, peer, owner.connection_id

    def get_all_connected(self, network: NetworkType | None = None) -> list[PeerInfo]:
        return list(self._iter_connected(network))

    def get_peerlist_for_network(self, network: NetworkType) -> list[tuple[str, str]]:
        # Use generator to avoid intermediate list
        # Include all connected peers, even NOT-SERVING-ONION
        # While they can't be directly connected to, they are reachable via the directory
        # for private messages, so this information is useful
        return [(peer.nick, peer.location_string) for peer in self._iter_connected(network)]

    def get_peerlist_with_features(self, network: NetworkType) -> list[tuple[str, str, FeatureSet]]:
        """
        Get peerlist with features for peers on a network.

        Returns list of (nick, location, features) tuples for connected peers.
        Includes all peers, even NOT-SERVING-ONION, as they are still reachable
        via the directory for private messaging.
        """
        result = []
        for peer in self._iter_connected(network):
            # Build FeatureSet from peer.features dict
            features = FeatureSet(features={k for k, v in peer.features.items() if v is True})
            # Debug: Log when features are extracted for peerlist
            if peer.features and not features.features:
                logger.warning(
                    f"Peer {peer.nick} has features dict {peer.features} but "
                    f"FeatureSet is empty after 'v is True' filter"
                )
            result.append((peer.nick, peer.location_string, features))
        return result

    def count(self) -> int:
        return len(self._peers)

    def clear(self) -> None:
        self._peers.clear()
        self._owners.clear()

    def get_passive_peers(self, network: NetworkType | None = None) -> list[PeerInfo]:
        """
        Get passive peers (NOT-SERVING-ONION).

        These are typically orderbook watchers/takers that don't host their own
        onion service but connect to the directory to watch offers.
        """
        return [p for p in self._iter_connected(network) if p.onion_address == "NOT-SERVING-ONION"]

    def get_active_peers(self, network: NetworkType | None = None) -> list[PeerInfo]:
        """
        Get active peers (serving onion address).

        These are typically makers that host their own onion service and
        publish offers to the orderbook.
        """
        return [p for p in self._iter_connected(network) if p.onion_address != "NOT-SERVING-ONION"]

    def get_stats(self) -> dict[str, int]:
        connected = 0
        passive = 0
        active = 0
        neutrino_compat = 0
        peerlist_features = 0
        push_encrypted = 0

        for p in list(self._peers.values()):
            if p.status == PeerStatus.HANDSHAKED and not p.is_directory:
                connected += 1
                if p.onion_address == "NOT-SERVING-ONION":
                    passive += 1
                else:
                    active += 1
                # Count feature support from features dict
                features = p.features
                if features.get("neutrino_compat"):
                    neutrino_compat += 1
                if features.get("peerlist_features"):
                    peerlist_features += 1
                if features.get("push_encrypted"):
                    push_encrypted += 1

        return {
            "total_peers": len(self._peers),
            "connected_peers": connected,
            "passive_peers": passive,
            "active_peers": active,
            "neutrino_compat_peers": neutrino_compat,
            "peerlist_features_peers": peerlist_features,
            "push_encrypted_peers": push_encrypted,
        }

    def get_neutrino_compat_peers(self, network: NetworkType | None = None) -> list[PeerInfo]:
        """
        Get peers that support neutrino_compat feature.

        These peers advertise extended UTXO metadata (scriptpubkey, blockheight)
        which is required for Neutrino backend verification.
        """
        return [p for p in self._iter_connected(network) if p.neutrino_compat]

    def get_peers_idle_since(self, cutoff: datetime) -> list[tuple[str, PeerInfo, str]]:
        """Get connected peers whose last_seen is older than cutoff.

        Returns list of (peer_key, peer_info, connection_id) tuples.
        """
        result: list[tuple[str, PeerInfo, str]] = []
        for key, peer in list(self._peers.items()):
            owner = self._owners.get(key)
            if (
                owner is not None
                and peer.status == PeerStatus.HANDSHAKED
                and not peer.is_directory
                and peer.last_seen is not None
                and peer.last_seen < cutoff
            ):
                result.append((key, peer, owner.connection_id))
        return result

    def supports_ping(self, key: str, expected_connection_id: str | None = None) -> bool:
        """Check if a peer supports PING/PONG heartbeat."""
        if not self.is_current_owner(key, expected_connection_id):
            return False
        peer = self.get_by_key(key)
        if peer is None:
            return False
        return peer.features.get("ping", False) is True

    def is_maker(self, key: str, expected_connection_id: str | None = None) -> bool:
        """Check if a peer is a maker (serves an onion address)."""
        if not self.is_current_owner(key, expected_connection_id):
            return False
        peer = self.get_by_key(key)
        if peer is None:
            return False
        return peer.onion_address != "NOT-SERVING-ONION"
