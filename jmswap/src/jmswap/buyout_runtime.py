"""Lifecycle of an explicitly enabled buyout service, and its settlement polling.

Everything a buyout endpoint owns at runtime (a journal, two LND connections, a
Bitcoin Core connection and one custom-message subscription) is acquired here, in
one order, and released in the reverse order on any startup error, on shutdown
and on cancellation. :class:`BuyoutRuntime` is the only object that knows that
order; the signing, settlement and transport modules keep owning their own rules.

Enabling is authorization, and starting is not
----------------------------------------------

A disabled configuration is refused before a single file is opened, a journal is
created or a socket is connected, so running this module against a file that
never said ``enabled = true`` cannot have a side effect.

An enabled configuration is still not permission to act on whatever the journal
happens to contain. Startup verifies, read-only and before the journal is
touched, that Bitcoin Core is synchronized on the configured network and that
LND is synchronized, is the configured node and is on the same network. Only
then is the journal opened and the subscription started.

Sessions are bound to the runtime that created them
---------------------------------------------------

Every session this runtime creates records a ``runtime_binding`` (network, node
identity, mixdepth, and the wallet fingerprint when one is configured) at
creation time. Polling and message handling act only on sessions whose stored
binding is exactly this runtime's, whose proposal names the configured network,
and (for settlement) whose ``settlement_authorized`` marker is affirmatively
true.

A session with a missing, partial or different binding is *inert*: it is
reported as :data:`UNBOUND_STATE` and logged, and nothing is written, paid,
signed, adopted or backfilled for it. An older record proves that some runtime
created it, not that this one may finish it, and silently adopting it is exactly
how an operator who moved a journal between nodes would pay from the wrong node.
Recovery of such a session is an explicit operator action that this increment
deliberately does not provide: there is no automatic resume, no nonce
reconstruction, no cancellation, no force close and no migration here.

Polling
-------

:meth:`BuyoutRuntime.poll_once` walks every stored session once. Resolved is not
finished: a ``COMPLETED`` session is polled again so that a reorganization of
the block that completed it is still noticed, while an affirmatively ``CANCELED``
session is skipped because nothing was ever signed for it. One session that
fails for an expected reason (protocol, chain, transport, node RPC or journal
conflict) must not starve the others, so its diagnostic is reduced to the
exception class name and the walk continues. A programming error and
cancellation are never swallowed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import NotRequired, Self, TypedDict, TypeVar

from pydantic import ValidationError

from jmswap.buyout_chain import BuyoutChain, ChainError
from jmswap.buyout_config import BuyoutConfigError, BuyoutSettings
from jmswap.buyout_messages import (
    BuyoutMessage,
    BuyoutMessageError,
    BuyoutPropose,
    BuyoutStatus,
    BuyoutSweep,
    Outpoint,
)
from jmswap.buyout_recovery import BuyoutRecovery
from jmswap.buyout_settlement import BuyoutSettlement
from jmswap.buyout_signing import BuyoutBuyer, CounterpartySigner, matches_runtime_binding
from jmswap.buyout_store import (
    STATE_CANCELED,
    BuyoutStore,
    BuyoutStoreError,
    StoredSession,
    UnknownSessionError,
)
from jmswap.buyout_terms import ProtocolError
from jmswap.buyout_transport import BuyoutTransport, PrivateTransportError
from jmswap.lnd_escrow import LndEscrowClient, LndEscrowError
from jmswap.lnd_peer import LndPeerClient, LndPeerError, PaymentRouteHop


class _RouteHintArgs(TypedDict):
    """The peer client argument an unconfigured deployment does not name."""

    payment_route_hints: NotRequired[tuple[tuple[PaymentRouteHop, ...], ...]]


_log = logging.getLogger(__name__)

_T = TypeVar("_T")

CORE_CHAIN_NAMES: dict[str, str] = {
    "main": "mainnet",
    "test": "testnet",
    "signet": "signet",
    "regtest": "regtest",
}
"""Bitcoin Core ``getblockchaininfo`` chain names, as configuration networks."""

UNBOUND_STATE = "UNBOUND_RECOVERY_REQUIRED"
"""Reported for a stored session this runtime is not allowed to act on."""

ERROR_STATE_PREFIX = "ERROR:"
"""Prefix of a polling diagnostic; the remainder is only an exception class name."""

_EXPECTED_SESSION_ERRORS: tuple[type[Exception], ...] = (
    ProtocolError,
    ChainError,
    PrivateTransportError,
    BuyoutStoreError,
    LndPeerError,
    LndEscrowError,
    BuyoutMessageError,
    ValidationError,
)


class BuyoutRuntimeError(Exception):
    """The runtime refused to start, is not open, or cannot keep running.

    The message names the rule that failed and never quotes a configured value,
    so it can be logged without logging a credential.
    """


def _required(value: _T | None, name: str) -> _T:
    if value is None:
        raise BuyoutRuntimeError(f"an enabled buyout service requires {name}")
    return value


def _read_file(path: Path, name: str) -> bytes:
    """Read one credential file, reporting only which setting named it."""
    try:
        data = path.read_bytes()
    except OSError:
        raise BuyoutRuntimeError(f"{name} could not be read") from None
    if not data:
        raise BuyoutRuntimeError(f"{name} is empty")
    return data


class BuyoutRuntime:
    """The resources of one enabled buyout endpoint, for as long as it is open.

    Use it as an async context manager; :attr:`store`, :attr:`peer`,
    :attr:`escrow`, :attr:`chain`, :attr:`buyer`, :attr:`settlement` and
    :attr:`transport` exist only inside the context and raise afterwards. A
    runtime is single use: leaving the context releases everything, and
    re-entering is refused rather than reconnecting with a stale journal view.

    ``counterparty`` decides whether this endpoint answers incoming requests. A
    buyer-only runtime installs no message handler at all, so it cannot accept a
    proposal or sign as a counterparty even if a peer asks it to.
    """

    def __init__(self, settings: BuyoutSettings, *, counterparty: bool = False) -> None:
        # Before any file, journal or socket: a disabled file authorizes nothing.
        if not settings.enabled:
            raise BuyoutRuntimeError("the buyout service is disabled; no runtime is authorized")
        self.settings = settings
        self.counterparty = counterparty
        self._network = _required(settings.network, "network")
        self._journal = _required(settings.journal, "journal")
        self._endpoint = _required(settings.lnd_endpoint, "lnd_endpoint")
        self._identity = _required(settings.lnd_identity, "lnd_identity")
        self._tls_cert = _required(settings.lnd_tls_cert, "lnd_tls_cert")
        self._peer_macaroon = _required(settings.lnd_peer_macaroon, "lnd_peer_macaroon")
        self._escrow_macaroon = _required(settings.lnd_escrow_macaroon, "lnd_escrow_macaroon")
        self._rpc_url = _required(settings.bitcoin_rpc_url, "bitcoin_rpc_url")
        self._rpc_user = _required(settings.bitcoin_rpc_user, "bitcoin_rpc_user")
        self._rpc_password = _required(settings.bitcoin_rpc_password, "bitcoin_rpc_password")
        self._mixdepth = _required(settings.mixdepth, "mixdepth")
        self._allowed_peers = frozenset(settings.allowed_peers)
        if not self._allowed_peers:
            raise BuyoutRuntimeError("an enabled buyout service requires at least one allowed peer")
        try:
            self._payout_script = settings.payout_script()
            self._policy = settings.build_signing_policy()
            self._settlement_policy = settings.build_settlement_policy()
        except (BuyoutConfigError, ValueError) as exc:
            raise BuyoutRuntimeError(str(exc)) from None
        self._binding: dict[str, str | int] = {
            "network": self._network,
            "lnd_identity": self._identity,
            "mixdepth": self._mixdepth,
        }
        if settings.wallet_fingerprint is not None:
            self._binding["wallet_fingerprint"] = settings.wallet_fingerprint
        self._stack: AsyncExitStack | None = None
        self._entered = False
        self._open = False
        self._reset()

    def _reset(self) -> None:
        self._store: BuyoutStore | None = None
        self._peer: LndPeerClient | None = None
        self._escrow: LndEscrowClient | None = None
        self._chain: BuyoutChain | None = None
        self._buyer: BuyoutBuyer | None = None
        self._signer: CounterpartySigner | None = None
        self._settlement: BuyoutSettlement | None = None
        self._transport: BuyoutTransport | None = None
        self._recovery: BuyoutRecovery | None = None

    @property
    def runtime_binding(self) -> dict[str, str | int]:
        """The binding written into every session this runtime creates."""
        return dict(self._binding)

    @property
    def store(self) -> BuyoutStore:
        return self._require(self._store)

    @property
    def peer(self) -> LndPeerClient:
        return self._require(self._peer)

    @property
    def escrow(self) -> LndEscrowClient:
        return self._require(self._escrow)

    @property
    def chain(self) -> BuyoutChain:
        return self._require(self._chain)

    @property
    def buyer(self) -> BuyoutBuyer:
        return self._require(self._buyer)

    @property
    def settlement(self) -> BuyoutSettlement:
        return self._require(self._settlement)

    @property
    def transport(self) -> BuyoutTransport:
        return self._require(self._transport)

    def _require(self, value: _T | None) -> _T:
        if not self._open or value is None:
            raise BuyoutRuntimeError("the buyout runtime is not open")
        return value

    async def __aenter__(self) -> Self:
        if self._entered:
            raise BuyoutRuntimeError("the buyout runtime cannot be entered more than once")
        self._entered = True
        stack = AsyncExitStack()
        try:
            await self._connect(stack)
        except BaseException:
            await stack.aclose()
            self._reset()
            raise
        self._stack = stack
        self._open = True
        return self

    async def __aexit__(self, *args: object) -> None:
        stack, self._stack = self._stack, None
        self._open = False
        try:
            if stack is not None:
                await stack.aclose()
        finally:
            self._reset()

    async def _connect(self, stack: AsyncExitStack) -> None:
        """Verify the two nodes read-only, then take the journal and the wire."""
        chain = BuyoutChain(
            str(self._rpc_url), self._rpc_user, self._rpc_password.get_secret_value()
        )
        # Registering the close without entering keeps the readiness check a
        # single RPC whose chain name is what binds Core to the configuration.
        stack.push_async_exit(chain)
        _check_core_network(await chain.check_ready(), self._network)
        certificate = _read_file(self._tls_cert, "lnd_tls_cert")
        # An unconfigured deployment builds the peer client exactly as before:
        # the route hint argument is only named when the operator supplied one.
        hints: _RouteHintArgs = (
            {"payment_route_hints": self.settings.payment_route_hints}
            if self.settings.payment_route_hints
            else {}
        )
        peer = await stack.enter_async_context(
            LndPeerClient(
                self._endpoint,
                certificate,
                _read_file(self._peer_macaroon, "lnd_peer_macaroon"),
                **hints,
            )
        )
        await self._check_node(peer)
        escrow = await stack.enter_async_context(
            LndEscrowClient(
                self._endpoint,
                certificate,
                _read_file(self._escrow_macaroon, "lnd_escrow_macaroon"),
            )
        )
        store = stack.enter_context(BuyoutStore(self._journal))
        self._chain, self._peer, self._escrow, self._store = chain, peer, escrow, store
        if self.counterparty:
            self._signer = CounterpartySigner(
                store,
                escrow,
                peer,
                chain.height,
                self._policy,
                self._payout_script,
                runtime_binding=self._binding,
                recovery_authorized=True,
                force_close_authorized=self.settings.automatic_force_close,
            )
        self._settlement = BuyoutSettlement(
            store,
            peer,
            chain,
            self._settlement_policy,
            self._request,
            runtime_binding=self._binding,
        )
        self._buyer = BuyoutBuyer(
            store,
            escrow,
            peer,
            self._request,
            chain.height,
            self._policy,
            runtime_binding=self._binding,
            recovery_authorized=True,
            force_close_authorized=self.settings.automatic_force_close,
        )
        self._recovery = BuyoutRecovery(store, escrow, peer, chain, runtime_binding=self._binding)
        self._transport = BuyoutTransport(
            peer, self._allowed_peers, self._dispatch if self.counterparty else None
        )
        await stack.enter_async_context(self._transport)

    async def _check_node(self, peer: LndPeerClient) -> None:
        info = await peer.node_info()
        if not info.synced_to_chain:
            raise BuyoutRuntimeError("the Lightning node is not synchronized to its chain")
        if info.identity_pubkey != self._identity:
            raise BuyoutRuntimeError("the Lightning node is not the configured lnd_identity")
        if info.network != self._network:
            raise BuyoutRuntimeError("the Lightning node is not on the configured network")

    async def _request(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        return await self.transport.request(peer, message)

    async def _dispatch(self, peer: str, message: BuyoutMessage) -> BuyoutMessage | None:
        """Route one authenticated incoming message to signing or settlement.

        Only a proposal for a session id this journal has never seen is new, and
        only a new session may be acted on without a binding: the signer writes
        this runtime's binding as it creates it. A proposal that names a stored
        session is a repeat, and a repeat is bound to whichever runtime created
        the record, however partially prepared that record is.
        """
        signer = self._signer
        if signer is None or not self._open:
            # A buyer-only runtime never signs for a counterparty.
            return None
        if peer not in self._allowed_peers:
            raise ProtocolError("peer is not in the operator allowlist")
        try:
            record = self.store.get(message.epoch_id)
        except UnknownSessionError:
            if not isinstance(message, BuyoutPropose):
                raise
            return await signer.handle(peer, message)
        if not matches_runtime_binding(record, self._binding):
            raise ProtocolError("session is not bound to this runtime; recovery required")
        if isinstance(message, BuyoutStatus | BuyoutSweep):
            return await self.settlement.handle(peer, message)
        return await signer.handle(peer, message)

    async def prepare(self, peer: str, points: Sequence[Outpoint]) -> str:
        """Propose a buyout of ``points`` to ``peer`` and return the session id."""
        buyer = self.buyer
        if peer not in self._allowed_peers:
            raise BuyoutRuntimeError("peer is not in the operator allowlist")
        if self.settings.wallet_fingerprint is None:
            raise BuyoutRuntimeError("a buyer session requires a configured wallet_fingerprint")
        return await buyer.prepare(peer, points, self._payout_script)

    def require_buyer_session(self, session_id: str) -> StoredSession:
        """Validate ownership before an operator resumes a buyer operation."""
        record = self.store.get(session_id)
        if (
            record.role != "buyer"
            or not matches_runtime_binding(record, self._binding)
            or self.settings.wallet_fingerprint is None
        ):
            raise BuyoutRuntimeError(
                "buyer session is not bound to this runtime; recovery required"
            )
        return record

    async def cancel(self, session_id: str) -> None:
        """Explicitly cancel this runtime's never-authorized buyer session."""
        self.require_buyer_session(session_id)
        await self.buyer.cancel(session_id)

    async def force_close(self, session_id: str) -> None:
        """Explicitly request unilateral recovery of an owned, unpaid session."""
        await self._require(self._recovery).force_close(session_id)

    async def poll_once(self) -> dict[str, str]:
        """Advance every session this runtime owns, and report what each one is.

        A session that is not bound to this runtime is reported as
        :data:`UNBOUND_STATE` without being touched, and an expected failure of
        one session is reported as ``ERROR:<class>`` without stopping the rest.
        """
        settlement = self.settlement
        states: dict[str, str] = {}
        for record in self.store.list():
            # An affirmative cancellation states nothing was signed; a resolved
            # session is still watched, because a reorg can undo its completion.
            if record.state == STATE_CANCELED:
                continue
            if not matches_runtime_binding(record, self._binding):
                _log.warning(
                    "buyout session %s is not bound to this runtime; recovery required",
                    record.session_id,
                )
                states[record.session_id] = UNBOUND_STATE
                continue
            try:
                if record.data.get("recovery_authorized") is True and await self._require(
                    self._recovery
                ).poll(record.session_id):
                    states[record.session_id] = self.store.get(record.session_id).state
                    continue
                if record.data.get("settlement_authorized") is not True:
                    states[record.session_id] = record.state
                    continue
                states[record.session_id] = await settlement.poll(record.session_id)
            except _EXPECTED_SESSION_ERRORS as exc:
                name = type(exc).__name__
                _log.warning("buyout session %s could not be polled: %s", record.session_id, name)
                states[record.session_id] = f"{ERROR_STATE_PREFIX}{name}"
        return states

    async def run(self, stop: asyncio.Event) -> None:
        """Poll every ``poll_interval_seconds`` until ``stop`` is set or cancelled.

        Cancellation propagates: leaving the runtime context is what releases the
        journal, the nodes and the subscription.
        """
        interval = self.settings.poll_interval_seconds
        while not stop.is_set():
            self._check_subscription()
            await self.poll_once()
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)

    def _check_subscription(self) -> None:
        """Stop rather than keep polling behind a subscription that already ended.

        A service whose receive loop is gone can no longer answer a peer, and
        polling on would leave it looking healthy while answering nobody.
        """
        if not self.transport.alive:
            raise BuyoutRuntimeError("the private message subscription ended; restart required")


def _check_core_network(chain_name: str, network: str) -> None:
    mapped = CORE_CHAIN_NAMES.get(chain_name)
    if mapped is None:
        raise BuyoutRuntimeError("Bitcoin Core reports an unsupported chain")
    if mapped != network:
        raise BuyoutRuntimeError("Bitcoin Core is not on the configured network")


__all__ = (
    "CORE_CHAIN_NAMES",
    "ERROR_STATE_PREFIX",
    "UNBOUND_STATE",
    "BuyoutRuntime",
    "BuyoutRuntimeError",
)
