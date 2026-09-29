"""Tests for the lifecycle and polling of an explicitly enabled buyout service.

The runtime is where an operator's configuration turns into open files, open
sockets and money-moving decisions, so these tests are about what it refuses to
do: a disabled file must create nothing at all, an unverified node must never
reach the journal, and a session that this runtime did not create must be inert
no matter what it contains. What it does do is checked at the same boundary:
every new session carries the runtime binding, every bound session (including a
completed one, which a reorg can undo) is polled once, and a single expected
failure is reduced to an exception class name instead of starving the others.

The two nodes and the wire are replaced by fakes; the journal is real, because
the binding round trip through SQLite is exactly what is under test.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import scriptpubkey_to_address

from jmswap import buyout_runtime
from jmswap.buyout_chain import ChainError
from jmswap.buyout_config import BuyoutSettings
from jmswap.buyout_messages import (
    BuyoutCancel,
    BuyoutMessage,
    BuyoutPropose,
    BuyoutStatus,
    BuyoutSweep,
    Outpoint,
)
from jmswap.buyout_runtime import (
    ERROR_STATE_PREFIX,
    UNBOUND_STATE,
    BuyoutRuntime,
    BuyoutRuntimeError,
)
from jmswap.buyout_signing import BuyoutPolicy
from jmswap.buyout_store import BuyoutStore, StoredSession, UnknownSessionError
from jmswap.buyout_terms import ProtocolError
from jmswap.buyout_transport import PrivateTransportError
from jmswap.lnd_escrow import FrozenChannel
from jmswap.lnd_peer import LndPeerError, NodeInfo, PaymentRouteHop


def _pubkey(tag: str) -> str:
    return bytes(CKey(hashlib.sha256(tag.encode()).digest()).pub).hex()


def _p2tr_script(tag: str) -> str:
    key = CKey(hashlib.sha256(tag.encode()).digest())
    return (b"\x51\x20" + bytes(key.xonly_pub)).hex()


def _nonce(tag: str) -> str:
    return _pubkey(tag + "-r1") + _pubkey(tag + "-r2")


IDENTITY = _pubkey("runtime-identity")
PEER = _pubkey("runtime-peer")
STRANGER = _pubkey("runtime-stranger")
FINGERPRINT = "0123abcd"
SPLIT_SCRIPT = _p2tr_script("runtime-split")
PAYOUT_ADDRESS = scriptpubkey_to_address(bytes.fromhex(_p2tr_script("runtime-payout")), "regtest")
POINT = Outpoint(txid="33" * 32, vout=0)
SESSION_A = "a1" * 32
SESSION_B = "b2" * 32
SESSION_C = "c3" * 32
BINDING: dict[str, object] = {
    "network": "regtest",
    "lnd_identity": IDENTITY,
    "mixdepth": 0,
    "wallet_fingerprint": FINGERPRINT,
}
SYNCED_NODE = NodeInfo(
    identity_pubkey=IDENTITY, block_height=200, synced_to_chain=True, network="regtest"
)


def _settings(tmp_path: Path, **overrides: object) -> BuyoutSettings:
    for name in ("tls.cert", "peer.macaroon", "escrow.macaroon"):
        (tmp_path / name).write_bytes(b"credential")
    values: dict[str, object] = {
        "enabled": True,
        "network": "regtest",
        "journal": tmp_path / "journal" / "sessions.sqlite",
        "lnd_endpoint": "127.0.0.1:10009",
        "lnd_identity": IDENTITY,
        "lnd_tls_cert": tmp_path / "tls.cert",
        "lnd_peer_macaroon": tmp_path / "peer.macaroon",
        "lnd_escrow_macaroon": tmp_path / "escrow.macaroon",
        "bitcoin_rpc_url": "http://127.0.0.1:18443/",
        "bitcoin_rpc_user": "buyout",
        "bitcoin_rpc_password": "regtest-placeholder",
        "allowed_peers": (PEER,),
        "payout_addresses": [PAYOUT_ADDRESS],
        "mixdepth": 0,
        "wallet_fingerprint": FINGERPRINT,
        "poll_interval_seconds": 0.01,
    }
    values.update(overrides)
    return BuyoutSettings.model_validate(values)


@dataclass
class Harness:
    """Knobs for the faked resources, and every resource that was created."""

    settings: BuyoutSettings
    core_chain: str = "regtest"
    core_error: Exception | None = None
    node: NodeInfo = SYNCED_NODE
    node_error: Exception | None = None
    request_error: Exception | None = None
    poll_results: dict[str, str | Exception] = field(default_factory=dict)
    poll_hook: Any = None
    chains: list[Any] = field(default_factory=list)
    peers: list[Any] = field(default_factory=list)
    escrows: list[Any] = field(default_factory=list)
    transports: list[Any] = field(default_factory=list)
    signers: list[Any] = field(default_factory=list)
    settlements: list[Any] = field(default_factory=list)
    polled: list[str] = field(default_factory=list)
    routed: list[tuple[str, str, BuyoutMessage]] = field(default_factory=list)

    @property
    def journal(self) -> Path:
        assert self.settings.journal is not None
        return self.settings.journal


class FakeChain:
    def __init__(self, harness: Harness, endpoint: str, username: str, password: str) -> None:
        self.harness, self.endpoint, self.username, self.password = (
            harness,
            endpoint,
            username,
            password,
        )
        self.closed = False
        harness.chains.append(self)

    async def check_ready(self) -> str:
        if self.harness.core_error is not None:
            raise self.harness.core_error
        return self.harness.core_chain

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def height(self) -> int:
        return self.harness.node.block_height


class FakePeer:
    def __init__(
        self, harness: Harness, endpoint: str, certificate: bytes, macaroon: bytes
    ) -> None:
        self.harness, self.endpoint = harness, endpoint
        self.certificate, self.macaroon = certificate, macaroon
        self.closed = False
        harness.peers.append(self)

    async def __aenter__(self) -> FakePeer:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def node_info(self) -> NodeInfo:
        if self.harness.node_error is not None:
            raise self.harness.node_error
        return self.harness.node

    async def channels(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                point=POINT,
                peer_pubkey=PEER,
                active=True,
                private=True,
                pending_htlcs=0,
                commitment_type="TAPROOT",
            )
        ]


class FakeEscrow:
    def __init__(
        self, harness: Harness, endpoint: str, certificate: bytes, macaroon: bytes
    ) -> None:
        self.harness, self.endpoint, self.macaroon = harness, endpoint, macaroon
        self.closed = False
        harness.escrows.append(self)

    async def __aenter__(self) -> FakeEscrow:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def freeze(self, point: Outpoint, session_id: bytes) -> FrozenChannel:
        return FrozenChannel(
            point=point,
            capacity_sat=1_000_000,
            local_claim_sat=700_000,
            remote_claim_sat=300_000,
            peer_pubkey=bytes.fromhex(PEER),
            funding_script=bytes.fromhex(SPLIT_SCRIPT),
            local_funding_pubkey=bytes.fromhex(_pubkey("runtime-local-funding")),
            remote_funding_pubkey=bytes.fromhex(PEER),
        )


class FakeTransport:
    def __init__(
        self, harness: Harness, peer: Any, allowed_peers: frozenset[str], handler: Any = None
    ) -> None:
        self.harness, self.peer, self.allowed_peers, self.handler = (
            harness,
            peer,
            allowed_peers,
            handler,
        )
        self.closed = False
        self.alive = False
        harness.transports.append(self)

    async def __aenter__(self) -> FakeTransport:
        self.alive = True
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed, self.alive = True, False

    async def request(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        if self.harness.request_error is not None:
            raise self.harness.request_error
        raise PrivateTransportError("no reply was configured")


class FakeSigner:
    def __init__(self, harness: Harness, *args: Any, **kwargs: Any) -> None:
        self.harness, self.args, self.kwargs = harness, args, kwargs
        self.runtime_binding = kwargs.get("runtime_binding")
        harness.signers.append(self)

    async def handle(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        self.harness.routed.append(("signer", peer, message))
        return message


class FakeSettlement:
    def __init__(self, harness: Harness, *args: Any, **kwargs: Any) -> None:
        self.harness, self.args = harness, args
        harness.settlements.append(self)

    async def handle(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        self.harness.routed.append(("settlement", peer, message))
        return message

    async def poll(self, session_id: str) -> str:
        self.harness.polled.append(session_id)
        if self.harness.poll_hook is not None:
            self.harness.poll_hook(session_id)
        result = self.harness.poll_results.get(session_id, "PARENT_CONFIRMED")
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Harness:
    result = Harness(settings=_settings(tmp_path))
    for name, fake in (
        ("BuyoutChain", FakeChain),
        ("LndPeerClient", FakePeer),
        ("LndEscrowClient", FakeEscrow),
        ("BuyoutTransport", FakeTransport),
        ("CounterpartySigner", FakeSigner),
        ("BuyoutSettlement", FakeSettlement),
    ):

        def factory(*args: Any, _fake: Any = fake, **kwargs: Any) -> Any:
            return _fake(result, *args, **kwargs)

        monkeypatch.setattr(buyout_runtime, name, factory)
    return result


def _seed(
    harness: Harness,
    session_id: str,
    *,
    state: str = "PARENT_CONFIRMED",
    binding: dict[str, object] | None = None,
    authorized: bool = True,
    network: str = "regtest",
    role: str = "buyer",
) -> StoredSession:
    """Write one session into the real journal before the runtime opens it."""
    data: dict[str, object] = {
        "proposal": {"network": network},
        "settlement_authorized": authorized,
    }
    if binding is not None:
        data["runtime_binding"] = binding
    with BuyoutStore(harness.journal) as store:
        record = store.create(
            session_id,
            PEER,
            role,  # type: ignore[arg-type]
            (f"{session_id[:64]}:0",),
            data=data,
        )
        if state != record.state:
            record = store.update(record, state=state, data=record.data)
        return record


def _propose(session_id: str) -> BuyoutPropose:
    policy = BuyoutPolicy()
    return BuyoutPropose(
        v=1,
        type="buyout_propose",
        epoch_id=session_id,
        attempt=0,
        network="regtest",
        channel_points=[POINT],
        K_B=_pubkey("runtime-buyer-key"),
        K_B_claim=_pubkey("runtime-buyer-claim"),
        split_script_B=SPLIT_SCRIPT,
        csv_delay=policy.csv_delay,
        split_fee=policy.split_fee,
        split_fee_rate_sat_vb=policy.split_fee_rate_sat_vb,
        min_split_output=policy.min_split_output,
        sweep_fee_reserve=policy.sweep_fee_reserve,
        buyer_settlement_depth=policy.buyer_settlement_depth,
        cltv_limit=policy.cltv_limit,
        sweep_response_blocks=policy.sweep_response_blocks,
        max_buyout_fee=policy.buyout_fee,
        max_timeout_compensation=policy.timeout_compensation,
        freeze_ttl_blocks=policy.freeze_ttl_blocks,
        expiry=int(time.time()) + 120,
    )


def _status(session_id: str) -> BuyoutStatus:
    return BuyoutStatus(
        v=1,
        type="buyout_status",
        epoch_id=session_id,
        attempt=0,
        accept_hash="aa" * 32,
        stage="parent_signed",
        parent_hash="bb" * 32,
        txid="cc" * 32,
    )


def _sweep(session_id: str) -> BuyoutSweep:
    return BuyoutSweep(
        v=1,
        type="buyout_sweep",
        epoch_id=session_id,
        attempt=0,
        accept_hash="aa" * 32,
        unsigned_sweep_tx="0200000000",
        sweep_nonce_B=_nonce("runtime-sweep"),
    )


def _cancel(session_id: str) -> BuyoutCancel:
    return BuyoutCancel(
        v=1,
        type="buyout_cancel",
        epoch_id=session_id,
        attempt=0,
        reason_code="operator_cancel",
        proposal_hash="dd" * 32,
    )


class TestStartupAuthorization:
    def test_disabled_configuration_creates_no_resource(self, harness: Harness) -> None:
        disabled = BuyoutSettings()

        with pytest.raises(BuyoutRuntimeError, match="disabled"):
            BuyoutRuntime(disabled)

        assert harness.chains == harness.peers == harness.escrows == harness.transports == []
        assert not harness.journal.exists()
        assert not harness.journal.parent.exists()

    @pytest.mark.parametrize(
        ("knob", "value"),
        [
            ("core_chain", "main"),
            ("core_chain", "testnet4"),
            ("core_error", ChainError("node is not synchronized")),
        ],
    )
    async def test_an_unverified_core_never_reaches_the_journal(
        self, harness: Harness, knob: str, value: object
    ) -> None:
        setattr(harness, knob, value)

        with pytest.raises((BuyoutRuntimeError, ChainError)):
            async with BuyoutRuntime(harness.settings):
                pass

        assert [chain.closed for chain in harness.chains] == [True]
        assert harness.peers == []
        assert not harness.journal.exists()

    @pytest.mark.parametrize(
        "node",
        [
            NodeInfo(
                identity_pubkey=IDENTITY,
                block_height=200,
                synced_to_chain=False,
                network="regtest",
            ),
            NodeInfo(
                identity_pubkey=STRANGER,
                block_height=200,
                synced_to_chain=True,
                network="regtest",
            ),
            NodeInfo(
                identity_pubkey=IDENTITY, block_height=200, synced_to_chain=True, network="signet"
            ),
        ],
    )
    async def test_an_unverified_node_closes_resources_and_spares_the_journal(
        self, harness: Harness, node: NodeInfo
    ) -> None:
        harness.node = node

        with pytest.raises(BuyoutRuntimeError):
            async with BuyoutRuntime(harness.settings):
                pass

        assert [chain.closed for chain in harness.chains] == [True]
        assert [peer.closed for peer in harness.peers] == [True]
        assert harness.escrows == []
        assert harness.transports == []
        assert not harness.journal.exists()

    async def test_an_unreachable_node_propagates_and_releases_everything(
        self, harness: Harness
    ) -> None:
        harness.node_error = LndPeerError("node is unavailable")

        with pytest.raises(LndPeerError):
            async with BuyoutRuntime(harness.settings):
                pass

        assert [chain.closed for chain in harness.chains] == [True]
        assert [peer.closed for peer in harness.peers] == [True]
        assert not harness.journal.exists()

    async def test_a_runtime_cannot_be_entered_twice(self, harness: Harness) -> None:
        runtime = BuyoutRuntime(harness.settings)
        async with runtime:
            pass

        with pytest.raises(BuyoutRuntimeError, match="more than once"):
            async with runtime:
                pass

        assert len(harness.chains) == 1

    async def test_resources_are_exposed_only_while_open(self, harness: Harness) -> None:
        runtime = BuyoutRuntime(harness.settings)
        with pytest.raises(BuyoutRuntimeError, match="not open"):
            _ = runtime.store

        async with runtime:
            assert runtime.chain is harness.chains[0]
            assert runtime.peer is harness.peers[0]
            assert runtime.escrow is harness.escrows[0]
            assert runtime.transport is harness.transports[0]
            assert runtime.store.list() == []

        for name in ("store", "peer", "escrow", "chain", "buyer", "settlement", "transport"):
            with pytest.raises(BuyoutRuntimeError, match="not open"):
                getattr(runtime, name)
        assert all(transport.closed for transport in harness.transports)

    async def test_configured_route_hints_reach_the_peer_client(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        hints = (
            (
                PaymentRouteHop(
                    node_id=PEER,
                    chan_id=123456789,
                    fee_base_msat=1000,
                    fee_proportional_millionths=1,
                    cltv_expiry_delta=80,
                ),
            ),
        )
        captured: list[object] = []

        def hinted_peer(*args: Any, payment_route_hints: Any, **kwargs: Any) -> FakePeer:
            captured.append(payment_route_hints)
            return FakePeer(harness, *args, **kwargs)

        monkeypatch.setattr(buyout_runtime, "LndPeerClient", hinted_peer)
        harness.settings = _settings(tmp_path, payment_route_hints=hints)

        async with BuyoutRuntime(harness.settings):
            assert captured == [hints]

    async def test_an_unconfigured_runtime_builds_the_peer_without_hints(
        self, harness: Harness
    ) -> None:
        # FakePeer takes no route hint argument, so opening at all is the
        # evidence: an unconfigured runtime names nothing new.
        async with BuyoutRuntime(harness.settings) as runtime:
            assert runtime.peer is harness.peers[0]


class TestRoles:
    async def test_a_buyer_only_runtime_installs_no_handler(self, harness: Harness) -> None:
        async with BuyoutRuntime(harness.settings):
            assert harness.transports[0].handler is None
            assert harness.signers == []

    async def test_a_counterparty_routes_by_message_and_binding(self, harness: Harness) -> None:
        _seed(harness, SESSION_A, binding=BINDING, role="counterparty")
        async with BuyoutRuntime(harness.settings, counterparty=True) as runtime:
            handler = harness.transports[0].handler
            assert handler is not None
            assert harness.signers[0].runtime_binding == runtime.runtime_binding

            await handler(PEER, _propose(SESSION_B))
            await handler(PEER, _status(SESSION_A))
            await handler(PEER, _sweep(SESSION_A))
            await handler(PEER, _cancel(SESSION_A))

        assert [(role, message.type) for role, _, message in harness.routed] == [
            ("signer", "buyout_propose"),
            ("settlement", "buyout_status"),
            ("settlement", "buyout_sweep"),
            ("signer", "buyout_cancel"),
        ]

    async def test_a_counterparty_refuses_unknown_peers_and_unbound_sessions(
        self, harness: Harness
    ) -> None:
        _seed(harness, SESSION_A, binding=None, role="counterparty")
        async with BuyoutRuntime(harness.settings, counterparty=True):
            handler = harness.transports[0].handler

            with pytest.raises(ProtocolError, match="allowlist"):
                await handler(STRANGER, _propose(SESSION_B))
            with pytest.raises(ProtocolError, match="recovery required"):
                await handler(PEER, _status(SESSION_A))
            with pytest.raises(UnknownSessionError):
                await handler(PEER, _status(SESSION_B))

        assert harness.routed == []

    @pytest.mark.parametrize(
        "binding", [None, {**BINDING, "lnd_identity": STRANGER}, {**BINDING, "mixdepth": 1}]
    )
    @pytest.mark.parametrize("state", ["CREATED", "FREEZING", "ACCEPTED"])
    async def test_a_repeated_proposal_for_an_unbound_session_never_reaches_the_signer(
        self, harness: Harness, binding: dict[str, object] | None, state: str
    ) -> None:
        # A proposal that names a stored session belongs to whichever runtime
        # created that record, however early the record stopped.
        before = _seed(harness, SESSION_A, binding=binding, state=state, role="counterparty")

        async with BuyoutRuntime(harness.settings, counterparty=True):
            handler = harness.transports[0].handler
            with pytest.raises(ProtocolError, match="recovery required"):
                await handler(PEER, _propose(SESSION_A))

        assert harness.routed == []
        assert runtime_journal_sessions(harness) == [before]

    async def test_a_repeated_proposal_for_a_bound_session_reaches_the_signer(
        self, harness: Harness
    ) -> None:
        _seed(harness, SESSION_A, binding=BINDING, state="FREEZING", role="counterparty")

        async with BuyoutRuntime(harness.settings, counterparty=True):
            handler = harness.transports[0].handler
            await handler(PEER, _propose(SESSION_A))

        assert [(role, message.type) for role, _, message in harness.routed] == [
            ("signer", "buyout_propose")
        ]


class TestBinding:
    async def test_a_new_buyer_session_records_and_is_polled_under_the_binding(
        self, harness: Harness
    ) -> None:
        harness.request_error = PrivateTransportError("counterparty is unavailable")

        async with BuyoutRuntime(harness.settings) as runtime:
            with pytest.raises(ExceptionGroup):
                await runtime.prepare(PEER, [POINT])
            record = runtime.store.list()[0]
            assert record.data["runtime_binding"] == runtime.runtime_binding
            assert runtime.runtime_binding == BINDING
            assert await runtime.poll_once() == {record.session_id: "PARENT_CONFIRMED"}

        assert harness.polled == [record.session_id]

    async def test_prepare_refuses_a_peer_outside_the_allowlist(self, harness: Harness) -> None:
        async with BuyoutRuntime(harness.settings) as runtime:
            with pytest.raises(BuyoutRuntimeError, match="allowlist"):
                await runtime.prepare(STRANGER, [POINT])

        assert runtime_journal_sessions(harness) == []

    async def test_prepare_requires_a_configured_wallet_fingerprint(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        harness.settings = _settings(tmp_path, wallet_fingerprint=None)

        async with BuyoutRuntime(harness.settings) as runtime:
            assert "wallet_fingerprint" not in runtime.runtime_binding
            with pytest.raises(BuyoutRuntimeError, match="wallet_fingerprint"):
                await runtime.prepare(PEER, [POINT])

        assert runtime_journal_sessions(harness) == []


def runtime_journal_sessions(harness: Harness) -> list[StoredSession]:
    with BuyoutStore(harness.journal) as store:
        return store.list()


class TestPolling:
    @pytest.mark.parametrize(
        "binding",
        [
            None,
            {"network": "regtest", "lnd_identity": IDENTITY},
            {**BINDING, "lnd_identity": STRANGER},
            {**BINDING, "mixdepth": 1},
        ],
    )
    async def test_an_unbound_session_is_inert(
        self, harness: Harness, binding: dict[str, object] | None
    ) -> None:
        before = _seed(harness, SESSION_A, binding=binding)

        async with BuyoutRuntime(harness.settings) as runtime:
            assert await runtime.poll_once() == {SESSION_A: UNBOUND_STATE}

        assert harness.polled == []
        assert runtime_journal_sessions(harness) == [before]

    async def test_a_foreign_proposal_network_is_inert(self, harness: Harness) -> None:
        before = _seed(harness, SESSION_A, binding=BINDING, network="signet")

        async with BuyoutRuntime(harness.settings) as runtime:
            assert await runtime.poll_once() == {SESSION_A: UNBOUND_STATE}

        assert harness.polled == []
        assert runtime_journal_sessions(harness) == [before]

    async def test_an_unauthorized_session_is_reported_without_settling(
        self, harness: Harness
    ) -> None:
        before = _seed(harness, SESSION_A, binding=BINDING, authorized=False)

        async with BuyoutRuntime(harness.settings) as runtime:
            assert await runtime.poll_once() == {SESSION_A: before.state}

        assert harness.polled == []
        assert runtime_journal_sessions(harness) == [before]

    async def test_completed_sessions_are_polled_and_canceled_ones_are_skipped(
        self, harness: Harness
    ) -> None:
        _seed(harness, SESSION_A, binding=BINDING, state="COMPLETED")
        _seed(harness, SESSION_B, binding=BINDING, state="CANCELED")
        _seed(harness, SESSION_C, binding=BINDING, state="SETTLED")
        harness.poll_results = {SESSION_A: "COMPLETED", SESSION_C: "SPEND_BROADCAST"}

        async with BuyoutRuntime(harness.settings) as runtime:
            states = await runtime.poll_once()

        assert states == {SESSION_A: "COMPLETED", SESSION_C: "SPEND_BROADCAST"}
        assert harness.polled == [SESSION_A, SESSION_C]

    async def test_authorized_recovery_does_not_require_payment_authorization(
        self,
        harness: Harness,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        record = _seed(harness, SESSION_A, binding=BINDING, authorized=False, state="ACCEPTED")
        point = Outpoint(txid=SESSION_A, vout=0)
        proposal = _propose(SESSION_A).model_copy(update={"channel_points": [point]})
        with BuyoutStore(harness.journal) as store:
            store.update(
                record,
                state=record.state,
                data={
                    **record.data,
                    "proposal": proposal.model_dump(),
                    "recovery_authorized": True,
                    "freeze_height": harness.node.block_height - proposal.freeze_ttl_blocks,
                },
            )
        canceled = AsyncMock(return_value=True)
        async with BuyoutRuntime(harness.settings) as runtime:
            monkeypatch.setattr(runtime.escrow, "cancel", canceled, raising=False)
            assert await runtime.poll_once() == {SESSION_A: "CANCELED"}
        canceled.assert_awaited_once_with(point, bytes.fromhex(SESSION_A))
        assert harness.polled == []

    async def test_one_expected_failure_does_not_starve_the_other_sessions(
        self, harness: Harness
    ) -> None:
        _seed(harness, SESSION_A, binding=BINDING)
        _seed(harness, SESSION_B, binding=BINDING)
        harness.poll_results = {SESSION_A: ChainError("Bitcoin Core response was unavailable")}

        async with BuyoutRuntime(harness.settings) as runtime:
            states = await runtime.poll_once()

        assert states == {
            SESSION_A: f"{ERROR_STATE_PREFIX}ChainError",
            SESSION_B: "PARENT_CONFIRMED",
        }
        assert harness.polled == [SESSION_A, SESSION_B]

    async def test_a_programming_error_is_never_swallowed(self, harness: Harness) -> None:
        _seed(harness, SESSION_A, binding=BINDING)
        harness.poll_results = {SESSION_A: AttributeError("runtime bug")}

        async with BuyoutRuntime(harness.settings) as runtime:
            with pytest.raises(AttributeError):
                await runtime.poll_once()


class TestRunLoop:
    async def test_run_returns_when_the_stop_event_is_set(self, harness: Harness) -> None:
        _seed(harness, SESSION_A, binding=BINDING)
        stop = asyncio.Event()
        harness.poll_hook = lambda _session_id: stop.set()

        async with BuyoutRuntime(harness.settings) as runtime:
            await asyncio.wait_for(runtime.run(stop), 5)

        assert harness.polled == [SESSION_A]

    async def test_run_refuses_to_look_healthy_without_its_subscription(
        self, harness: Harness
    ) -> None:
        _seed(harness, SESSION_A, binding=BINDING)

        async with BuyoutRuntime(harness.settings) as runtime:
            harness.transports[0].alive = False
            with pytest.raises(BuyoutRuntimeError, match="subscription"):
                await runtime.run(asyncio.Event())

        assert harness.polled == []

    async def test_cancelling_run_releases_every_resource(self, harness: Harness) -> None:
        runtime = BuyoutRuntime(harness.settings)
        async with runtime:
            task = asyncio.create_task(runtime.run(asyncio.Event()))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert harness.chains[0].closed is True
        assert harness.peers[0].closed is True
        assert harness.escrows[0].closed is True
        assert harness.transports[0].closed is True
        with pytest.raises(BuyoutRuntimeError, match="not open"):
            _ = runtime.transport
