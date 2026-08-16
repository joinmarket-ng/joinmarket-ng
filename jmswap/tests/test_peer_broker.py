"""Tests for explicit durable application delivery over escrow peer transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from jmswap.channel_escrow import EscrowLifecycleState
from jmswap.errors import ChannelInputEscrowError
from jmswap.peer_transport import (
    ESCROW_PEER_CUSTOM_MESSAGE_TYPE,
    EscrowPeerBinding,
    EscrowPeerBroker,
    EscrowPeerEnvelope,
    EscrowPeerJournal,
    EscrowPeerTransport,
    EscrowProtocolRole,
    EscrowPublicKeysPayload,
    ParentMuSig2NoncePayload,
    RawReceivedCustomMessage,
    RecoveryStatusPayload,
)
from jmswap.ring_authorization import (
    RingChannelPoint,
    RingEdgeAuthorization,
    RingEdgeAuthorizationStore,
)

LOCAL_NODE = "02" + "11" * 32
PEER_NODE = "03" + "22" * 32
EDGE_ID = "33" * 32
CHANNEL_TXID = "44" * 32


class QueueCustomMessageBackend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, int]] = []
        self.received: asyncio.Queue[RawReceivedCustomMessage | Exception] = asyncio.Queue()
        self.subscription_started = asyncio.Event()
        self.subscription_cancelled = False

    async def send_custom_message(
        self, peer_node_id: str, data: bytes, message_type: int = 0
    ) -> None:
        self.sent.append((peer_node_id, data, message_type))

    async def _messages(self) -> AsyncIterator[RawReceivedCustomMessage]:
        self.subscription_started.set()
        try:
            while True:
                item = await self.received.get()
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.subscription_cancelled = True

    def subscribe_custom_messages(
        self, message_type: int = 0
    ) -> AsyncIterator[RawReceivedCustomMessage]:
        assert message_type == ESCROW_PEER_CUSTOM_MESSAGE_TYPE
        return self._messages()


def _status(*, parent_txid: str = "aa" * 32) -> RecoveryStatusPayload:
    return RecoveryStatusPayload(state=EscrowLifecycleState.PARENT_SIGNED, parent_txid=parent_txid)


def _transport(
    tmp_path: Path, backend: QueueCustomMessageBackend | None = None
) -> tuple[EscrowPeerTransport, QueueCustomMessageBackend]:
    authorization = RingEdgeAuthorization(
        edge_id=EDGE_ID,
        coinjoin_context="55" * 32,
        taker_nick="taker",
        taker_session_pubkey="66" * 32,
        funder_nick="funder",
        receiver_nick="receiver",
        funder_session_pubkey="77" * 32,
        receiver_session_pubkey="88" * 32,
        funder_ln_node=f"{LOCAL_NODE}@local.example:9735",
        receiver_ln_node=f"{PEER_NODE}@peer.example:9735",
        coinjoin_amount_sats=100_000,
        capacity_sats=150_000,
        funder_contribution_sats=80_000,
        receiver_contribution_sats=70_000,
        expires_at_unix=2_000_000_000,
    )
    authorization_store = RingEdgeAuthorizationStore(tmp_path / "authorizations")
    authorization_store.authorize(authorization, local_nick="funder", now_unix=1)
    authorization_store.bind_funding(
        EDGE_ID,
        authorization.authorization_id,
        "bcrt1pfunding",
        "5120" + "99" * 32,
    )
    authorization_store.bind_channel_point(
        EDGE_ID,
        authorization.authorization_id,
        RingChannelPoint(txid=CHANNEL_TXID, vout=1),
    )
    active_backend = QueueCustomMessageBackend() if backend is None else backend
    return (
        EscrowPeerTransport(
            active_backend,
            authorization_store,
            EscrowPeerJournal(tmp_path / "journal"),
            LOCAL_NODE,
        ),
        active_backend,
    )


def _inbound(binding: EscrowPeerBinding, payload: RecoveryStatusPayload) -> EscrowPeerEnvelope:
    reverse_binding = EscrowPeerBinding(
        edge_id=binding.edge_id,
        authorization_id=binding.authorization_id,
        session_id=binding.session_id,
        channel_point=binding.channel_point,
        local_role=binding.local_role,
        local_node_id=binding.peer_node_id,
        peer_node_id=binding.local_node_id,
    )
    return EscrowPeerEnvelope.create(
        binding=reverse_binding,
        sequence=1,
        previous_message_id=None,
        payload=payload,
    )


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def test_broker_send_reuses_the_exact_durable_envelope(tmp_path: Path) -> None:
    transport, backend = _transport(tmp_path)
    broker = EscrowPeerBroker(transport)

    first = await broker.send(EDGE_ID, _status())
    retry = await broker.send(EDGE_ID, _status())

    assert retry == first
    assert retry.sequence == first.sequence
    assert retry.message_id == first.message_id
    assert transport._journal.outbound(transport.binding(EDGE_ID)) == (first,)
    assert [item[1] for item in backend.sent] == [first.encode(), first.encode()]


async def test_broker_rejects_conflicting_same_kind_retry_without_sending(tmp_path: Path) -> None:
    transport, backend = _transport(tmp_path)
    broker = EscrowPeerBroker(transport)
    first = EscrowPublicKeysPayload(
        role=EscrowProtocolRole.PEER,
        escrow_pubkey=PEER_NODE,
        timeout_destination_scriptpubkey="5120" + "aa" * 32,
    )
    conflicting = first.model_copy(update={"escrow_pubkey": "02" + "ab" * 32})

    await broker.send(EDGE_ID, first)
    with pytest.raises(ChannelInputEscrowError, match="conflicting"):
        await broker.send(EDGE_ID, conflicting)

    assert len(backend.sent) == 1
    assert len(transport._journal.outbound(transport.binding(EDGE_ID))) == 1


async def test_broker_allows_distinct_recovery_and_parent_round_messages(tmp_path: Path) -> None:
    transport, _backend = _transport(tmp_path)
    broker = EscrowPeerBroker(transport)
    parent_nonce = ParentMuSig2NoncePayload(
        round_id="55" * 32,
        parent_txid="aa" * 32,
        public_nonce="bb" * 66,
    )
    next_parent_nonce = parent_nonce.model_copy(update={"round_id": "66" * 32})

    first = await broker.send(EDGE_ID, _status())
    second = await broker.send(EDGE_ID, _status(parent_txid="bb" * 32))
    third = await broker.send(EDGE_ID, parent_nonce)
    fourth = await broker.send(EDGE_ID, next_parent_nonce)

    assert [envelope.sequence for envelope in (first, second, third, fourth)] == [1, 2, 3, 4]


async def test_broker_rejects_conflicting_parent_message_in_same_round(tmp_path: Path) -> None:
    transport, backend = _transport(tmp_path)
    broker = EscrowPeerBroker(transport)
    parent_nonce = ParentMuSig2NoncePayload(
        round_id="55" * 32,
        parent_txid="aa" * 32,
        public_nonce="bb" * 66,
    )
    conflicting = parent_nonce.model_copy(update={"public_nonce": "cc" * 66})

    await broker.send(EDGE_ID, parent_nonce)
    with pytest.raises(ChannelInputEscrowError, match="conflicting"):
        await broker.send(EDGE_ID, conflicting)

    assert len(backend.sent) == 1


async def test_broker_receive_retains_then_explicitly_completes_across_restart(
    tmp_path: Path,
) -> None:
    transport, backend = _transport(tmp_path)
    broker = EscrowPeerBroker(transport)
    inbound = _inbound(transport.binding(EDGE_ID), _status())
    broker.start()
    await backend.subscription_started.wait()

    receive_task = asyncio.create_task(broker.receive(EDGE_ID, RecoveryStatusPayload))
    await backend.received.put(
        RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode())
    )
    assert await receive_task == inbound
    assert transport.pending(EDGE_ID) == (inbound,)
    await broker.close()

    restarted_transport, _ = _transport(tmp_path, backend)
    restarted = EscrowPeerBroker(restarted_transport)
    redelivered = await restarted.receive(EDGE_ID, RecoveryStatusPayload)
    assert redelivered == inbound
    restarted.complete(redelivered)
    assert restarted_transport.pending(EDGE_ID) == ()
    await restarted.close()


async def test_duplicate_delivery_wakes_a_waiting_broker_receiver(tmp_path: Path) -> None:
    transport, backend = _transport(tmp_path)
    broker = EscrowPeerBroker(transport)
    inbound = _inbound(transport.binding(EDGE_ID), _status())
    broker.start()
    await backend.subscription_started.wait()
    receive_task = asyncio.create_task(broker.receive(EDGE_ID, RecoveryStatusPayload))
    await _wait_until(
        lambda: EDGE_ID in broker._conditions and bool(broker._conditions[EDGE_ID]._waiters)  # noqa: SLF001 - synchronize the receiver test
    )

    await transport.accept(RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode()))
    assert not receive_task.done()
    await backend.received.put(
        RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode())
    )

    assert await receive_task == inbound
    await broker.close()


async def test_receive_timeout_retains_the_unmatched_durable_message(tmp_path: Path) -> None:
    transport, _backend = _transport(tmp_path)
    inbound = _inbound(transport.binding(EDGE_ID), _status())
    await transport.accept(RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode()))
    broker = EscrowPeerBroker(transport)

    with pytest.raises(ChannelInputEscrowError, match="timed out"):
        await broker.receive(EDGE_ID, EscrowPublicKeysPayload, timeout=0.01)

    assert transport.pending(EDGE_ID) == (inbound,)
    await broker.close()


async def test_subscription_failure_is_reported_to_receivers(tmp_path: Path) -> None:
    transport, backend = _transport(tmp_path)
    broker = EscrowPeerBroker(transport)
    failure = RuntimeError("custom-message stream failed")
    broker.start()
    await backend.subscription_started.wait()
    await backend.received.put(failure)
    await _wait_until(lambda: broker._task is not None and broker._task.done())

    with pytest.raises(ChannelInputEscrowError, match="subscription failed") as exc_info:
        await broker.receive(EDGE_ID, RecoveryStatusPayload)

    assert exc_info.value.__cause__ is failure
    await broker.close()


async def test_broker_close_cancels_the_subscription(tmp_path: Path) -> None:
    transport, backend = _transport(tmp_path)
    broker = EscrowPeerBroker(transport)
    broker.start()
    await backend.subscription_started.wait()

    await broker.close()

    assert backend.subscription_cancelled
