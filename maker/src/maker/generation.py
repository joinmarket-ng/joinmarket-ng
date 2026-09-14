"""Runtime resources owned by one maker identity generation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer
from jmcore.network import HiddenServiceListener, TCPConnection
from jmcore.tor_control import EphemeralHiddenService, TorControlClient
from loguru import logger

from maker.direct_connection import DirectConnectionState
from maker.directory_pool import MakerDirectoryPool
from maker.offers import OfferManager


class GenerationState(StrEnum):
    """Whether a generation may accept new work."""

    ACCEPTING = "accepting"
    GRACE = "grace"
    CLOSED = "closed"


@dataclass(slots=True)
class OfferDelivery:
    """Public offer messages each directory of one generation still owes.

    ``pending[node_id][oid]`` holds what that directory has not accepted yet:
    the exact formatted announcement, or ``None`` meaning the offer must be
    withdrawn. Keying by offer id coalesces superseded updates, so a directory
    that fails for several refreshes accumulates at most one entry per
    advertised offer instead of an unbounded queue. Payloads are stored already
    formatted, so a retry repeats the identical wire message instead of
    re-randomizing advertised terms.
    """

    pending: dict[str, dict[int, str | None]] = field(default_factory=dict)
    # Serializes sends so a reconnect flush cannot interleave with a refresh
    # flush and reorder or duplicate one directory's messages.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def queue(self, node_ids: Iterable[str], desired: Mapping[int, str | None]) -> None:
        """Record the latest desired state for the given offer ids."""
        if not desired:
            return
        for node_id in node_ids:
            self.pending.setdefault(node_id, {}).update(desired)

    def prune(self, known_node_ids: Iterable[str]) -> None:
        """Forget directories this generation can no longer reach at all.

        Callers must pass every directory that may still come back, not only
        the connected ones: a withdrawal owed to a directory that is merely
        down has to survive the outage and go out on reconnect.
        """
        known = set(known_node_ids)
        for node_id in [node_id for node_id in self.pending if node_id not in known]:
            del self.pending[node_id]

    async def flush(self, clients: Mapping[str, DirectoryClient]) -> None:
        """Send each directory its outstanding payloads, cancellations first.

        A directory stops at its first failure, so an announcement can never
        overtake an undelivered cancellation; whatever is left stays queued for
        the next flush. Directories are independent: one failure never resends
        payloads another directory already accepted.

        A send can take arbitrarily long, and a refresh may supersede this
        generation's desired state meanwhile. Both the client map and the
        message order are therefore snapshotted, while the payload itself is
        re-read immediately before each send, so a stale message can never be
        published after the update that replaced it.
        """
        async with self.send_lock:
            for node_id, client in list(clients.items()):
                entry = self.pending.get(node_id)
                if not entry:
                    continue
                # Cancellations (payload None) sort before announcements.
                ordered = sorted(entry.items(), key=lambda item: (item[1] is not None, item[0]))
                for oid, _snapshot in ordered:
                    if oid not in entry:
                        continue
                    payload = entry[oid]
                    try:
                        await client.send_public_message(
                            f"cancel {oid}" if payload is None else payload
                        )
                    except Exception as exc:
                        logger.warning(
                            f"Directory {node_id} did not accept offer update {oid}, "
                            f"will retry the same message: {exc}"
                        )
                        break
                    if entry.get(oid, payload) == payload:
                        entry.pop(oid, None)
                if not entry:
                    self.pending.pop(node_id, None)


@dataclass(slots=True)
class MakerGeneration:
    """All identity-bound maker resources.

    The bot intentionally keeps current-generation aliases for compatibility,
    but protocol routing always resolves one of these records explicitly.
    """

    generation_id: int
    nick_identity: NickIdentity
    offer_manager: OfferManager
    directory_pool: MakerDirectoryPool
    directory_clients: dict[str, DirectoryClient] = field(default_factory=dict)
    current_offers: list[Offer] = field(default_factory=list)
    hidden_service_listener: HiddenServiceListener | None = None
    tor_control: TorControlClient | None = None
    ephemeral_hidden_service: EphemeralHiddenService | None = None
    onion_host: str | None = None
    listener_port: int | None = None
    direct_connections: dict[str, TCPConnection] = field(default_factory=dict)
    direct_connection_states: dict[TCPConnection, DirectConnectionState] = field(
        default_factory=dict
    )
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    reconnect_attempts: dict[str, int] = field(default_factory=dict)
    state: GenerationState = GenerationState.ACCEPTING
    grace_deadline: float | None = None
    # Serializes offer refreshes for this identity: only one rebuild may be
    # between creation and adoption at a time.
    refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    offer_delivery: OfferDelivery = field(default_factory=OfferDelivery)
