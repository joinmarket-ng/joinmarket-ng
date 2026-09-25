"""Private two-party signing, with the recovery split durable before authorization.

The transport authenticates the Lightning peer; this module never talks to a
JoinMarket coordinator. Native secret nonces live only in memory. After a crash
we reuse durable signatures, never reconstruct or reuse a secret nonce. Missing
nonce state before a durable signature requires recovery, not a new parent.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, cast

from bitcointx.core.key import CKey
from jmcore.bitcoin import parse_transaction_bytes, serialize_transaction
from jmcore.musig2 import SecNonce, nonce_agg, sign_partial
from jmcore.taproot import TaprootTx, TxIn, TxOut, taproot_sighash, verify_schnorr

from jmswap.bitcoin_escrow import escrow_nonce, escrow_session, finalize_key_path_spend
from jmswap.buyout_messages import (
    BuyoutAccept,
    BuyoutCancel,
    BuyoutMessage,
    BuyoutNonces,
    BuyoutParent,
    BuyoutParentPartials,
    BuyoutPropose,
    BuyoutSplitPartial,
    BuyoutStatus,
    Outpoint,
    Prevout,
    accept_hash,
    decode_buyout_payload,
    encode_buyout_payload,
    proposal_hash,
)
from jmswap.buyout_store import PAYOUT_SCRIPT_KEY, BuyoutStore, StoredSession
from jmswap.buyout_terms import (
    BuyoutTerms,
    ProtocolError,
    ValidatedParent,
    frozen_state_hash,
    validate_parent,
)
from jmswap.lnd_escrow import FrozenChannel, LndEscrowClient, SigningAttempt
from jmswap.lnd_peer import LndPeerClient

Request = Callable[[str, BuyoutMessage], Awaitable[BuyoutMessage]]
Height = Callable[[], Awaitable[int]]


@dataclass(frozen=True)
class BuyoutPolicy:
    network: Literal["regtest", "signet", "testnet", "mainnet"] = "regtest"
    csv_delay: int = 432
    split_fee: int = 154
    split_fee_rate_sat_vb: int = 1
    min_split_output: int = 1_000
    sweep_fee_reserve: int = 10_000
    buyer_settlement_depth: int = 6
    cltv_limit: int = 360
    sweep_response_blocks: int = 1
    buyout_fee: int = 0
    timeout_compensation: int = 0
    settlement_depth: int = 3
    parent_wait_blocks: int = 6
    max_freeze_blocks: int = 144
    freeze_ttl_blocks: int = 6
    proposal_lifetime_seconds: int = 120
    settlement_enabled: bool = False


def _secret() -> str:
    return bytes(CKey(secrets.token_bytes(32))).hex()


def _pub(secret: str) -> str:
    return bytes(CKey(bytes.fromhex(secret)).pub).hex()


def _channel_data(channel: FrozenChannel) -> dict[str, Any]:
    data = asdict(channel)
    data["point"] = channel.point.model_dump()
    return {key: value.hex() if isinstance(value, bytes) else value for key, value in data.items()}


def _channels(data: dict[str, Any]) -> tuple[FrozenChannel, ...]:
    result = []
    for item in data["channels"]:
        values = dict(item)
        values["point"] = Outpoint.model_validate(values["point"])
        for key in (
            "peer_pubkey",
            "funding_script",
            "local_funding_pubkey",
            "remote_funding_pubkey",
        ):
            values[key] = bytes.fromhex(values[key])
        result.append(FrozenChannel(**values))
    return tuple(result)


def session_terms(record: StoredSession) -> BuyoutTerms:
    data = cast(dict[str, Any], record.data)
    return BuyoutTerms(
        BuyoutPropose.model_validate(data["proposal"]),
        BuyoutAccept.model_validate(data["acceptance"]),
        _channels(data),
        record.role == "buyer",
    )


def matches_runtime_binding(record: StoredSession, binding: dict[str, str | int]) -> bool:
    """Match persisted ownership with exact types (JSON booleans are not mixdepths)."""
    stored = record.data.get("runtime_binding")
    proposal = record.data.get("proposal")
    return (
        bool(binding)
        and isinstance(stored, dict)
        and stored.keys() == binding.keys()
        and all(
            type(stored[key]) is type(value) and stored[key] == value
            for key, value in binding.items()
        )
        and isinstance(proposal, dict)
        and proposal.get("network") == binding.get("network")
    )


def require_runtime_binding(record: StoredSession, binding: dict[str, str | int]) -> None:
    """Keep configured runtimes from acting on another deployment's journal rows.

    Low-level callers without a configured binding retain their explicit API.
    A configured runtime never adopts an absent, partial, or different binding.
    """
    if binding and not matches_runtime_binding(record, binding):
        raise ProtocolError("session is not bound to this runtime; recovery required")


def _save(
    store: BuyoutStore, session_id: str, state: str, *, irreversible: bool = False, **changes: Any
) -> StoredSession:
    record = store.get(session_id)
    data = {**record.data, **changes}
    return store.update(
        record,
        state=state,
        data=data,
        parent_signing_started=record.parent_signing_started or irreversible,
    )


def _attempts(record: StoredSession) -> list[SigningAttempt]:
    return [
        SigningAttempt(bytes.fromhex(item["attempt_id"]), bytes.fromhex(item["public_nonce"]))
        for item in cast(dict[str, Any], record.data)["attempts"]
    ]


def _attempt_data(attempts: Sequence[SigningAttempt]) -> list[dict[str, str]]:
    return [
        {"attempt_id": item.attempt_id.hex(), "public_nonce": item.public_nonce.hex()}
        for item in attempts
    ]


def _payout_candidates(scripts: Sequence[str]) -> tuple[str, ...]:
    if isinstance(scripts, str) or not scripts:
        raise ValueError("payout scripts must be a non-empty sequence")
    return tuple(scripts)


async def _eligible(
    peer: LndPeerClient,
    points: Sequence[Outpoint],
    other: str,
    network: str,
    *,
    allow_quiescent: bool = False,
) -> None:
    info = await peer.node_info()
    if not info.synced_to_chain or info.network != network:
        raise ProtocolError("Lightning node is not synchronized to the requested network")
    available = {(item.point.txid, item.point.vout): item for item in await peer.channels()}
    for point in points:
        channel = available.get((point.txid, point.vout))
        if (
            channel is None
            or channel.peer_pubkey != other
            or (not channel.active and not allow_quiescent)
            or not channel.private
            or channel.pending_htlcs
            or channel.commitment_type != "TAPROOT"
        ):
            raise ProtocolError("channel is not eligible for private buyout")


async def _freeze(backend: LndEscrowClient, proposal: BuyoutPropose) -> tuple[FrozenChannel, ...]:
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(backend.freeze(point, bytes.fromhex(proposal.epoch_id)))
            for point in proposal.channel_points
        ]
    return tuple(task.result() for task in tasks)


async def _prepare(
    backend: LndEscrowClient, message: BuyoutParent, terms: BuyoutTerms, parent: ValidatedParent
) -> list[SigningAttempt]:
    session = bytes.fromhex(message.epoch_id)
    attempts = []
    for point, index in zip(
        terms.proposal.channel_points, message.channel_input_indices, strict=True
    ):
        txid = await backend.prepare(point, session, parent.raw, message.prevouts, index)
        if txid != parent.txid:
            raise ProtocolError("backend prepared a different parent")
        attempts.append(await backend.begin(point, session))
    return attempts


async def _funding_partial(
    backend: LndEscrowClient,
    point: Outpoint,
    session: bytes,
    attempt: SigningAttempt,
    remote_nonce: bytes,
) -> bytes:
    status = await backend.status(point, session)
    if status.durable_local_partial:
        if (
            status.durable_local_nonce != attempt.public_nonce
            or status.durable_remote_nonce != remote_nonce
        ):
            raise ProtocolError("durable signing evidence does not match the attempt")
        return status.durable_local_partial
    if status.active_attempt is None or status.active_attempt.attempt_id != attempt.attempt_id:
        raise ProtocolError("native signing nonce unavailable; recovery required")
    return await backend.sign(point, session, attempt, remote_nonce)


def channel_signature(
    parent: bytes, prevouts: Sequence[Prevout], index: int, finalized: bytes
) -> bytes:
    parsed = parse_transaction_bytes(finalized)
    raw = serialize_transaction(parsed.version, parsed.inputs, parsed.outputs, parsed.locktime)
    if raw != parent or len(parsed.witnesses) != len(parsed.inputs):
        raise ProtocolError("backend returned a different signed parent")
    if any(witness for n, witness in enumerate(parsed.witnesses) if n != index):
        raise ProtocolError("backend signed an unexpected input")
    witness = parsed.witnesses[index]
    if len(witness) != 1 or len(witness[0]) != 64:
        raise ProtocolError("backend returned an invalid key-path witness")
    tx = TaprootTx(
        inputs=[TxIn(item.txid, item.vout, item.sequence) for item in parsed.inputs],
        outputs=[TxOut(item.value, item.script) for item in parsed.outputs],
        version=parsed.version,
        locktime=parsed.locktime,
    )
    scripts = [bytes.fromhex(item.script_pubkey) for item in prevouts]
    digest = taproot_sighash(tx, index, [item.value for item in prevouts], scripts)
    if not verify_schnorr(scripts[index][2:], witness[0], digest):
        raise ProtocolError("backend channel signature is invalid")
    return witness[0]


class CounterpartySigner:
    """Single-parent counterparty endpoint; caller supplies authenticated peer IDs."""

    def __init__(
        self,
        store: BuyoutStore,
        escrow: LndEscrowClient,
        peer: LndPeerClient,
        height: Height,
        policy: BuyoutPolicy,
        payout_scripts: Sequence[str],
        *,
        runtime_binding: dict[str, str | int] | None = None,
        recovery_authorized: bool = False,
        force_close_authorized: bool = False,
    ) -> None:
        self.store, self.escrow, self.peer = store, escrow, peer
        self.height, self.policy = height, policy
        # One fresh script per session: the journal never reuses a payout.
        self.payout_scripts = _payout_candidates(payout_scripts)
        self.runtime_binding = dict(runtime_binding or {})
        self.recovery_authorized = recovery_authorized
        self.force_close_authorized = force_close_authorized
        self._locks: dict[str, asyncio.Lock] = {}
        self._nonces: dict[str, SecNonce] = {}

    async def handle(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        with self.store.exclusive_operation():
            return await self._handle(peer, message)

    async def _handle(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        async with self._locks.setdefault(message.epoch_id, asyncio.Lock()):
            if isinstance(message, BuyoutPropose):
                return await self._propose(peer, message)
            record = self.store.get(message.epoch_id)
            require_runtime_binding(record, self.runtime_binding)
            if record.peer_pubkey != peer or record.role != "counterparty":
                raise ProtocolError("message is not bound to the authenticated session")
            if isinstance(message, BuyoutCancel):
                return await self._cancel(record, message)
            if record.state in {"CANCELING", "CANCELED"}:
                raise ProtocolError("session is canceled or cancellation is pending")
            terms = session_terms(record)
            if (
                record.peer_pubkey != peer
                or record.role != "counterparty"
                or message.attempt != terms.proposal.attempt
                or getattr(message, "accept_hash", None) != accept_hash(terms.acceptance)
            ):
                raise ProtocolError("message is not bound to the authenticated session")
            cached = self._cached(record, message)
            if cached is not None:
                return cached
            if isinstance(message, BuyoutParent):
                return await self._parent(record, terms, message)
            if isinstance(message, BuyoutSplitPartial):
                return await self._split(record, terms, message)
            raise ProtocolError("message is not a signing request")

    async def _cancel(self, record: StoredSession, message: BuyoutCancel) -> BuyoutMessage:
        proposal = BuyoutPropose.model_validate(record.data["proposal"])
        if message.attempt != proposal.attempt or record.parent_signing_started:
            raise ProtocolError("parent authorization cannot be canceled")
        if message.proposal_hash is not None:
            bound = message.proposal_hash == proposal_hash(proposal)
        else:
            acceptance = record.data.get("acceptance")
            bound = acceptance is not None and message.accept_hash == accept_hash(
                BuyoutAccept.model_validate(acceptance)
            )
        if not bound:
            raise ProtocolError("cancellation is not bound to the session")
        cached = self._cached(record, message)
        if cached is not None:
            return cached
        _save(self.store, record.session_id, "CANCELING")
        for point in proposal.channel_points:
            await self.escrow.cancel(point, bytes.fromhex(record.session_id))
        self._nonces.pop(record.session_id, None)
        # Before acceptance there is no accept_hash for a status message. The
        # matching cancellation in the opposite direction acknowledges release.
        response: BuyoutMessage = message
        if message.accept_hash is not None:
            response = BuyoutStatus(
                v=1,
                type="buyout_status",
                epoch_id=record.session_id,
                attempt=message.attempt,
                accept_hash=message.accept_hash,
                stage="canceled",
            )
        self._reply(message, response, "CANCELED")
        return response

    def _cached(self, record: StoredSession, message: BuyoutMessage) -> BuyoutMessage | None:
        previous = cast(dict[str, Any], record.data).get("requests", {}).get(message.type)
        if previous is None:
            return None
        if previous != encode_buyout_payload(message).hex():
            raise ProtocolError("conflicting repeated request")
        response = cast(dict[str, Any], record.data).get("responses", {}).get(message.type)
        return decode_buyout_payload(bytes.fromhex(response)) if response else None

    def _reply(self, request: BuyoutMessage, response: BuyoutMessage, state: str) -> None:
        record = self.store.get(request.epoch_id)
        data = cast(dict[str, Any], record.data)
        _save(
            self.store,
            request.epoch_id,
            state,
            requests={
                **data.get("requests", {}),
                request.type: encode_buyout_payload(request).hex(),
            },
            responses={
                **data.get("responses", {}),
                request.type: encode_buyout_payload(response).hex(),
            },
        )

    async def _propose(self, peer: str, proposal: BuyoutPropose) -> BuyoutAccept:
        existing = next(
            (item for item in self.store.list() if item.session_id == proposal.epoch_id), None
        )
        if existing is not None:
            require_runtime_binding(existing, self.runtime_binding)
            if existing.peer_pubkey != peer or existing.role != "counterparty":
                raise ProtocolError("proposal belongs to another peer")
            if existing.data.get("proposal") != proposal.model_dump():
                raise ProtocolError("conflicting repeated proposal")
            if existing.state in {"CANCELING", "CANCELED"}:
                raise ProtocolError("session is canceled or cancellation is pending")
            cached = self._cached(existing, proposal)
            if isinstance(cached, BuyoutAccept):
                return cached
            if existing.data.get("acceptance") is not None:
                acceptance = BuyoutAccept.model_validate(existing.data["acceptance"])
                self._reply(proposal, acceptance, existing.state)
                return acceptance
            if existing.state not in {"CREATED", "FREEZING"}:
                raise ProtocolError("incomplete proposal requires reconciliation")
            return await self._accept(proposal)
        if (
            proposal.network != self.policy.network
            or proposal.attempt != 0
            or not time.time() < proposal.expiry <= time.time() + 600
            or proposal.max_buyout_fee < self.policy.buyout_fee
            or proposal.max_timeout_compensation < self.policy.timeout_compensation
            or proposal.freeze_ttl_blocks > self.policy.freeze_ttl_blocks
            or proposal.csv_delay > self.policy.csv_delay
        ):
            raise ProtocolError("proposal is outside counterparty policy")
        # B may already have initiated STFU, which makes C's channel inactive.
        # The authenticated peer and FreezeChannel still enforce quiescence.
        await _eligible(
            self.peer, proposal.channel_points, peer, self.policy.network, allow_quiescent=True
        )
        self.store.create(
            proposal.epoch_id,
            peer,
            "counterparty",
            tuple(f"{p.txid}:{p.vout}" for p in proposal.channel_points),
            data={
                "proposal": proposal.model_dump(),
                PAYOUT_SCRIPT_KEY: self.store.next_payout_script(self.payout_scripts),
                "escrow_secret": _secret(),
                "preimage": secrets.token_hex(32),
                "settlement_authorized": self.policy.settlement_enabled,
                "runtime_binding": dict(self.runtime_binding),
                "recovery_authorized": self.recovery_authorized,
                "force_close_authorized": self.force_close_authorized,
                "freeze_height": await self.height(),
            },
        )
        _save(self.store, proposal.epoch_id, "FREEZING")
        return await self._accept(proposal)

    async def _accept(self, proposal: BuyoutPropose) -> BuyoutAccept:
        record = self.store.get(proposal.epoch_id)
        if not isinstance(record.data.get(PAYOUT_SCRIPT_KEY), str):
            # Recorded before per-session payouts; refuse before freezing anything.
            raise ProtocolError("session has no recorded payout script; it cannot be accepted")
        channels = await _freeze(self.escrow, proposal)
        record = self.store.get(proposal.epoch_id)
        data = cast(dict[str, Any], record.data)
        acceptance = BuyoutAccept(
            v=1,
            type="buyout_accept",
            epoch_id=proposal.epoch_id,
            attempt=0,
            proposal_hash=proposal_hash(proposal),
            K_C=_pub(data["escrow_secret"]),
            payment_hash=hashlib.sha256(bytes.fromhex(data["preimage"])).hexdigest(),
            split_script_C=data[PAYOUT_SCRIPT_KEY],
            entitlement_C=sum(c.local_claim_sat for c in channels),
            buyout_fee=self.policy.buyout_fee,
            timeout_compensation=self.policy.timeout_compensation,
            min_parent_fee_rate_sat_vb=1,
            parent_wait_blocks=self.policy.parent_wait_blocks,
            settlement_depth=self.policy.settlement_depth,
            max_freeze_blocks=self.policy.max_freeze_blocks,
            frozen_state_hash=frozen_state_hash(channels, False),
        )
        BuyoutTerms(proposal, acceptance, channels, False)
        _save(
            self.store,
            proposal.epoch_id,
            "ACCEPTED",
            acceptance=acceptance.model_dump(),
            channels=[_channel_data(item) for item in channels],
        )
        self._reply(proposal, acceptance, "ACCEPTED")
        return acceptance

    async def _parent(
        self, record: StoredSession, terms: BuyoutTerms, message: BuyoutParent
    ) -> BuyoutNonces:
        if record.state != "ACCEPTED":
            raise ProtocolError("parent is not expected in this state")
        parent = validate_parent(
            terms,
            bytes.fromhex(message.unsigned_parent_tx),
            message.prevouts,
            message.channel_input_indices,
            message.escrow_output_index,
            await self.height(),
        )
        _save(self.store, record.session_id, "PREPARING", parent=message.model_dump())
        attempts = await _prepare(self.escrow, message, terms, parent)
        secret = bytes.fromhex(cast(dict[str, Any], record.data)["escrow_secret"])
        nonce, public = escrow_nonce(
            bytes(CKey(secret).pub), privkey=secret, sighash=parent.split.sighash
        )
        self._nonces[record.session_id] = nonce
        response = BuyoutNonces(
            v=1,
            type="buyout_nonces",
            epoch_id=record.session_id,
            attempt=message.attempt,
            accept_hash=message.accept_hash,
            parent_hash=parent.parent_hash,
            split_nonce_C=public.hex(),
            parent_nonces_C=[item.public_nonce.hex() for item in attempts],
        )
        _save(
            self.store,
            record.session_id,
            "NONCES",
            attempts=_attempt_data(attempts),
            nonces=response.model_dump(),
        )
        self._reply(message, response, "NONCES")
        return response

    async def _split(
        self, record: StoredSession, terms: BuyoutTerms, message: BuyoutSplitPartial
    ) -> BuyoutParentPartials:
        data = cast(dict[str, Any], record.data)
        request = BuyoutParent.model_validate(data["parent"])
        nonces = BuyoutNonces.model_validate(data["nonces"])
        if message.parent_hash != nonces.parent_hash:
            raise ProtocolError("split partial is for another parent")
        parent = validate_parent(
            terms,
            bytes.fromhex(request.unsigned_parent_tx),
            request.prevouts,
            request.channel_input_indices,
            request.escrow_output_index,
            await self.height(),
        )
        if not data.get("split_tx"):
            if record.state != "NONCES" or record.session_id not in self._nonces:
                raise ProtocolError("split nonce unavailable; recovery required")
            public_b, public_c = (
                bytes.fromhex(request.split_nonce_B),
                bytes.fromhex(nonces.split_nonce_C),
            )
            session = escrow_session(
                terms.escrow, nonce_agg([public_b, public_c]), parent.split.sighash
            )
            partial_c = sign_partial(
                self._nonces.pop(record.session_id), bytes.fromhex(data["escrow_secret"]), session
            )
            signed = finalize_key_path_spend(
                terms.escrow,
                parent.split,
                public_b,
                public_c,
                bytes.fromhex(message.split_partial_B),
                partial_c,
            )
            # This write must complete before any channel-input signing RPC.
            record = _save(
                self.store,
                record.session_id,
                "SPLIT_SIGNED",
                split_tx=signed.hex(),
                split_partial_c=partial_c.hex(),
                split_partial_b=message.split_partial_B,
            )
        elif data["split_partial_b"] != message.split_partial_B:
            raise ProtocolError("conflicting split partial")
        record = _save(self.store, record.session_id, "PARENT_SIGNING", irreversible=True)
        partials = []
        for point, attempt, funding_nonce_b in zip(
            terms.proposal.channel_points, _attempts(record), request.parent_nonces_B, strict=True
        ):
            partials.append(
                (
                    await _funding_partial(
                        self.escrow,
                        point,
                        bytes.fromhex(record.session_id),
                        attempt,
                        bytes.fromhex(funding_nonce_b),
                    )
                ).hex()
            )
        response = BuyoutParentPartials(
            v=1,
            type="buyout_parent_partials",
            epoch_id=record.session_id,
            attempt=message.attempt,
            accept_hash=message.accept_hash,
            parent_hash=message.parent_hash,
            split_partial_C=cast(str, record.data["split_partial_c"]),
            parent_partials_C=partials,
        )
        self._reply(message, response, "PARENT_SIGNED")
        return response


class BuyoutBuyer:
    """Owns a buyer attempt without exposing the channel relationship to round peers."""

    def __init__(
        self,
        store: BuyoutStore,
        escrow: LndEscrowClient,
        peer: LndPeerClient,
        request: Request,
        height: Height,
        policy: BuyoutPolicy,
        *,
        runtime_binding: dict[str, str | int] | None = None,
        recovery_authorized: bool = False,
        force_close_authorized: bool = False,
    ) -> None:
        self.store, self.escrow, self.peer = store, escrow, peer
        self.request, self.height, self.policy = request, height, policy
        self.runtime_binding = dict(runtime_binding or {})
        self.recovery_authorized = recovery_authorized
        self.force_close_authorized = force_close_authorized
        self._locks: dict[str, asyncio.Lock] = {}

    async def prepare(
        self, peer: str, points: Sequence[Outpoint], payout_scripts: Sequence[str]
    ) -> str:
        """Propose a buyout paying out to the first unused of ``payout_scripts``."""
        candidates = _payout_candidates(payout_scripts)
        with self.store.exclusive_operation():
            return await self._prepare_session(peer, points, candidates)

    async def _prepare_session(
        self, peer: str, points: Sequence[Outpoint], payout_scripts: tuple[str, ...]
    ) -> str:
        await _eligible(self.peer, points, peer, self.policy.network)
        split_script = self.store.next_payout_script(payout_scripts)
        secret, claim = _secret(), _secret()
        sid = secrets.token_hex(32)
        p = self.policy
        proposal = BuyoutPropose(
            v=1,
            type="buyout_propose",
            epoch_id=sid,
            attempt=0,
            network=p.network,
            channel_points=list(points),
            K_B=_pub(secret),
            K_B_claim=_pub(claim),
            split_script_B=split_script,
            csv_delay=p.csv_delay,
            split_fee=p.split_fee,
            split_fee_rate_sat_vb=p.split_fee_rate_sat_vb,
            min_split_output=p.min_split_output,
            sweep_fee_reserve=p.sweep_fee_reserve,
            buyer_settlement_depth=p.buyer_settlement_depth,
            cltv_limit=p.cltv_limit,
            sweep_response_blocks=p.sweep_response_blocks,
            max_buyout_fee=p.buyout_fee,
            max_timeout_compensation=p.timeout_compensation,
            freeze_ttl_blocks=p.freeze_ttl_blocks,
            expiry=int(time.time()) + p.proposal_lifetime_seconds,
        )
        self.store.create(
            sid,
            peer,
            "buyer",
            tuple(f"{item.txid}:{item.vout}" for item in points),
            data={
                "proposal": proposal.model_dump(),
                PAYOUT_SCRIPT_KEY: split_script,
                "escrow_secret": secret,
                "claim_secret": claim,
                "settlement_authorized": p.settlement_enabled,
                "runtime_binding": dict(self.runtime_binding),
                "recovery_authorized": self.recovery_authorized,
                "force_close_authorized": self.force_close_authorized,
                "freeze_height": await self.height(),
            },
        )
        _save(self.store, sid, "FREEZING")
        return await self._finish_prepare(self.store.get(sid))

    async def resume_prepare(self, session_id: str) -> str:
        """Explicitly reconcile the same recorded proposal after interruption."""
        with self.store.exclusive_operation():
            record = self.store.get(session_id)
            require_runtime_binding(record, self.runtime_binding)
            if record.role != "buyer":
                raise ProtocolError("session is not a buyer")
            if record.state == "ACCEPTED":
                session_terms(record)
                return session_id
            if record.state not in {"CREATED", "FREEZING"}:
                raise ProtocolError("proposal cannot resume in this state")
            return await self._finish_prepare(record)

    async def _finish_prepare(self, record: StoredSession) -> str:
        proposal = BuyoutPropose.model_validate(record.data["proposal"])
        sid = record.session_id

        async def negotiate() -> BuyoutMessage:
            return await self.request(record.peer_pubkey, proposal)

        async with asyncio.TaskGroup() as group:
            freezing = group.create_task(_freeze(self.escrow, proposal))
            negotiating = group.create_task(negotiate())
        channels, response = freezing.result(), negotiating.result()
        if not isinstance(response, BuyoutAccept):
            raise ProtocolError("counterparty did not accept buyout")
        BuyoutTerms(proposal, response, channels, True)
        _save(
            self.store,
            sid,
            "ACCEPTED",
            channels=[_channel_data(item) for item in channels],
            acceptance=response.model_dump(),
        )
        return sid

    async def cancel(self, session_id: str) -> None:
        """Release only an explicitly requested, never-authorized attempt.

        Missing or partial journal state does not start recovery automatically.
        A lost acknowledgement retains CANCELING so the exact request can retry.
        """
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            with self.store.exclusive_operation():
                record = self.store.get(session_id)
                require_runtime_binding(record, self.runtime_binding)
                if record.role != "buyer" or record.parent_signing_started:
                    raise ProtocolError("parent authorization cannot be canceled")
                if record.state == "CANCELED":
                    return
                proposal = BuyoutPropose.model_validate(record.data["proposal"])
                acceptance = record.data.get("acceptance")
                message = BuyoutCancel(
                    v=1,
                    type="buyout_cancel",
                    epoch_id=session_id,
                    attempt=proposal.attempt,
                    reason_code="operator_cancel",
                    accept_hash=accept_hash(BuyoutAccept.model_validate(acceptance))
                    if acceptance is not None
                    else None,
                    proposal_hash=proposal_hash(proposal) if acceptance is None else None,
                )
                _save(self.store, session_id, "CANCELING")
                response = await self.request(record.peer_pubkey, message)
                acknowledged = (message.proposal_hash is not None and response == message) or (
                    isinstance(response, BuyoutStatus)
                    and response.epoch_id == session_id
                    and response.attempt == proposal.attempt
                    and response.stage == "canceled"
                    and response.accept_hash == message.accept_hash
                )
                if not acknowledged:
                    raise ProtocolError("cancellation acknowledgement does not match")
                for point in proposal.channel_points:
                    await self.escrow.cancel(point, bytes.fromhex(session_id))
                _save(self.store, session_id, "CANCELED")

    def terms(self, session_id: str) -> BuyoutTerms:
        record = self.store.get(session_id)
        require_runtime_binding(record, self.runtime_binding)
        return session_terms(record)

    def reserve_coinjoin(self, session_id: str) -> None:
        """Bind an accepted buyout to one caller before any round negotiation."""
        with self.store.exclusive_operation():
            record = self.store.get(session_id)
            require_runtime_binding(record, self.runtime_binding)
            if record.role != "buyer" or record.state != "ACCEPTED":
                raise ProtocolError("buyout is already reserved or no longer accepted")
            _save(self.store, session_id, "ROUND_RESERVED")

    async def sign_parent(
        self,
        session_id: str,
        raw: bytes,
        prevouts: Sequence[Prevout],
        indices: Sequence[int],
        escrow_index: int,
    ) -> dict[tuple[str, int], bytes]:
        async with self._locks.setdefault(session_id, asyncio.Lock()):
            with self.store.exclusive_operation():
                return await self._sign_parent(session_id, raw, prevouts, indices, escrow_index)

    async def _sign_parent(
        self,
        session_id: str,
        raw: bytes,
        prevouts: Sequence[Prevout],
        indices: Sequence[int],
        escrow_index: int,
    ) -> dict[tuple[str, int], bytes]:
        record = self.store.get(session_id)
        require_runtime_binding(record, self.runtime_binding)
        terms = session_terms(record)
        parent = validate_parent(terms, raw, prevouts, indices, escrow_index, await self.height())
        if record.role != "buyer":
            raise ProtocolError("session is not a buyer")
        if record.state not in {"ACCEPTED", "ROUND_RESERVED"}:
            return await self._resume_parent(record, parent)
        session = bytes.fromhex(session_id)
        _save(self.store, session_id, "PREPARING", raw_parent=raw.hex())
        attempts = []
        for point, index in zip(terms.proposal.channel_points, indices, strict=True):
            if await self.escrow.prepare(point, session, raw, prevouts, index) != parent.txid:
                raise ProtocolError("backend prepared a different parent")
            attempts.append(await self.escrow.begin(point, session))
        secret = bytes.fromhex(cast(str, record.data["escrow_secret"]))
        nonce, public = escrow_nonce(
            bytes(CKey(secret).pub), privkey=secret, sighash=parent.split.sighash
        )
        message = BuyoutParent(
            v=1,
            type="buyout_parent",
            epoch_id=session_id,
            attempt=0,
            accept_hash=accept_hash(terms.acceptance),
            unsigned_parent_tx=raw.hex(),
            prevouts=list(prevouts),
            escrow_output_index=escrow_index,
            channel_input_indices=list(indices),
            split_nonce_B=public.hex(),
            parent_nonces_B=[a.public_nonce.hex() for a in attempts],
        )
        _save(
            self.store,
            session_id,
            "NONCES",
            parent=message.model_dump(),
            attempts=_attempt_data(attempts),
        )
        response = await self.request(record.peer_pubkey, message)
        if (
            not isinstance(response, BuyoutNonces)
            or response.epoch_id != session_id
            or response.attempt != 0
            or response.accept_hash != message.accept_hash
            or response.parent_hash != parent.parent_hash
            or len(response.parent_nonces_C) != len(attempts)
        ):
            raise ProtocolError("counterparty nonce response does not match parent")
        split_session = escrow_session(
            terms.escrow,
            nonce_agg([public, bytes.fromhex(response.split_nonce_C)]),
            parent.split.sighash,
        )
        partial = sign_partial(nonce, secret, split_session)
        # Sending this authorizes C to sign. A lost reply cannot make cancellation safe.
        record = _save(
            self.store,
            session_id,
            "PARENT_SIGNING",
            irreversible=True,
            nonces=response.model_dump(),
            split_partial_b=partial.hex(),
        )
        return await self._resume_parent(record, parent)

    async def _resume_parent(
        self, record: StoredSession, parent: ValidatedParent
    ) -> dict[tuple[str, int], bytes]:
        data = cast(dict[str, Any], record.data)
        if data.get("raw_parent") != parent.raw.hex() or not data.get("split_partial_b"):
            raise ProtocolError("attempt cannot resume with this parent; recovery required")
        terms = session_terms(record)
        message = BuyoutParent.model_validate(data["parent"])
        nonces = BuyoutNonces.model_validate(data["nonces"])
        request = BuyoutSplitPartial(
            v=1,
            type="buyout_split_partial",
            epoch_id=record.session_id,
            attempt=0,
            accept_hash=message.accept_hash,
            parent_hash=parent.parent_hash,
            split_partial_B=data["split_partial_b"],
        )
        saved = data.get("counterparty_partials")
        response = (
            BuyoutParentPartials.model_validate(saved)
            if saved is not None
            else await self.request(record.peer_pubkey, request)
        )
        if (
            not isinstance(response, BuyoutParentPartials)
            or response.epoch_id != record.session_id
            or response.attempt != 0
            or response.accept_hash != message.accept_hash
            or response.parent_hash != parent.parent_hash
            or len(response.parent_partials_C) != len(terms.channels)
        ):
            raise ProtocolError("counterparty signature response does not match parent")
        split = finalize_key_path_spend(
            terms.escrow,
            parent.split,
            bytes.fromhex(message.split_nonce_B),
            bytes.fromhex(nonces.split_nonce_C),
            bytes.fromhex(request.split_partial_B),
            bytes.fromhex(response.split_partial_C),
        )
        if data.get("channel_signatures") is not None:
            parsed = parse_transaction_bytes(parent.raw)
            signatures = {}
            for point, index in zip(
                terms.proposal.channel_points, message.channel_input_indices, strict=True
            ):
                signature = bytes.fromhex(data["channel_signatures"][f"{point.txid}:{point.vout}"])
                witnesses: list[list[bytes]] = [[] for _ in parsed.inputs]
                witnesses[index] = [signature]
                finalized = serialize_transaction(
                    parsed.version,
                    parsed.inputs,
                    parsed.outputs,
                    parsed.locktime,
                    witnesses=witnesses,
                )
                signatures[(point.txid, point.vout)] = channel_signature(
                    parent.raw, message.prevouts, index, finalized
                )
            return signatures
        _save(
            self.store,
            record.session_id,
            "PARENT_SIGNING",
            irreversible=True,
            split_tx=split.hex(),
            counterparty_partials=response.model_dump(),
        )
        signatures = {}
        for point, index, attempt, nonce_c, partial_c in zip(
            terms.proposal.channel_points,
            message.channel_input_indices,
            _attempts(record),
            nonces.parent_nonces_C,
            response.parent_partials_C,
            strict=True,
        ):
            await _funding_partial(
                self.escrow,
                point,
                bytes.fromhex(record.session_id),
                attempt,
                bytes.fromhex(nonce_c),
            )
            final = await self.escrow.finalize(
                point, bytes.fromhex(record.session_id), attempt, bytes.fromhex(partial_c)
            )
            signatures[(point.txid, point.vout)] = channel_signature(
                parent.raw, message.prevouts, index, final
            )
        _save(
            self.store,
            record.session_id,
            "PARENT_SIGNED",
            irreversible=True,
            channel_signatures={
                f"{txid}:{vout}": sig.hex() for (txid, vout), sig in signatures.items()
            },
        )
        return signatures
