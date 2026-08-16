"""Tests for durable authenticated maker-ring escrow peer transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from jmswap.channel_escrow import EscrowLifecycleState
from jmswap.errors import ChannelInputEscrowError
from jmswap.peer_transport import (
    ESCROW_PEER_CUSTOM_MESSAGE_TYPE,
    MAX_ESCROW_PEER_ENVELOPE_BYTES,
    MAX_JOURNAL_MESSAGES_PER_DIRECTION,
    AcknowledgmentPayload,
    EscrowPeerBinding,
    EscrowPeerEnvelope,
    EscrowPeerJournal,
    EscrowPeerTransport,
    EscrowProtocolRole,
    EscrowPublicKeysPayload,
    RawReceivedCustomMessage,
    RecoveryStatusPayload,
    TimeoutMuSig2NoncePayload,
)
from jmswap.ring_authorization import (
    RingChannelPoint,
    RingEdgeAuthorization,
    RingEdgeAuthorizationRecord,
)

LOCAL_NODE = "02" + "11" * 32
PEER_NODE = "03" + "22" * 32
EDGE_ID = "33" * 32
CHANNEL_TXID = "44" * 32


class FakeRingStore:
    def __init__(self, record: RingEdgeAuthorizationRecord | None) -> None:
        self.record = record

    def load(self, edge_id: str) -> RingEdgeAuthorizationRecord | None:
        if self.record is None or edge_id != self.record.authorization.edge_id:
            return None
        return self.record


class FakeBackend:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, int]] = []
        self.fail_send = False
        self.received: list[RawReceivedCustomMessage] = []

    async def send_custom_message(
        self, peer_node_id: str, data: bytes, message_type: int = 0
    ) -> None:
        self.sent.append((peer_node_id, data, message_type))
        if self.fail_send:
            raise RuntimeError("network unavailable")

    async def _messages(self) -> AsyncIterator[RawReceivedCustomMessage]:
        while self.received:
            yield self.received.pop(0)

    def subscribe_custom_messages(
        self, message_type: int = 0
    ) -> AsyncIterator[RawReceivedCustomMessage]:
        assert message_type == ESCROW_PEER_CUSTOM_MESSAGE_TYPE
        return self._messages()


def _record(*, channel_point: bool = True) -> RingEdgeAuthorizationRecord:
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
    return RingEdgeAuthorizationRecord(
        authorization=authorization,
        local_nick="funder",
        funding_address="bcrt1pfunding",
        funding_scriptpubkey="5120" + "99" * 32,
        channel_point=RingChannelPoint(txid=CHANNEL_TXID, vout=1) if channel_point else None,
    )


def _binding() -> EscrowPeerBinding:
    return EscrowPeerBinding.from_authorization_record(_record(), LOCAL_NODE)


def _payload() -> RecoveryStatusPayload:
    return RecoveryStatusPayload(state=EscrowLifecycleState.PARENT_SIGNED, parent_txid="aa" * 32)


def _inbound(
    binding: EscrowPeerBinding,
    *,
    sequence: int = 1,
    previous: str | None = None,
    payload: RecoveryStatusPayload | None = None,
) -> EscrowPeerEnvelope:
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
        sequence=sequence,
        previous_message_id=previous,
        payload=_payload() if payload is None else payload,
    )


def _recommit(values: dict[str, object]) -> EscrowPeerEnvelope:
    values["channel_point"] = RingChannelPoint.model_validate(values["channel_point"])
    values["payload"] = RecoveryStatusPayload.model_validate(values["payload"])
    unvalidated = EscrowPeerEnvelope.model_construct(**values)
    values["message_id"] = unvalidated.computed_message_id()
    return EscrowPeerEnvelope.model_validate(values)


def _transport(tmp_path: Path) -> tuple[EscrowPeerTransport, FakeBackend, EscrowPeerJournal]:
    backend = FakeBackend()
    journal = EscrowPeerJournal(tmp_path / "journal")
    return (
        EscrowPeerTransport(backend, FakeRingStore(_record()), journal, LOCAL_NODE),
        backend,
        journal,
    )


def test_envelope_has_deterministic_canonical_id_and_rejects_tampering() -> None:
    binding = _binding()
    envelope = EscrowPeerEnvelope.create(
        binding=binding, sequence=1, previous_message_id=None, payload=_payload()
    )

    assert EscrowPeerEnvelope.decode(envelope.encode()) == envelope
    assert envelope.message_id == envelope.computed_message_id()
    tampered = envelope.model_dump()
    tampered["sequence"] = 2
    with pytest.raises(ValidationError, match="previous_message_id|message ID"):
        EscrowPeerEnvelope.model_validate(tampered)
    noncanonical = envelope.model_dump_json().encode("ascii")
    assert noncanonical != envelope.encode()
    with pytest.raises(ChannelInputEscrowError, match="not canonical"):
        EscrowPeerEnvelope.decode(noncanonical)


def test_key_and_timeout_payloads_are_role_and_transaction_bound() -> None:
    maker_keys = EscrowPublicKeysPayload(
        role=EscrowProtocolRole.MAKER,
        escrow_pubkey=LOCAL_NODE,
        maker_claim_pubkey="03" + "ab" * 32,
        timeout_destination_scriptpubkey="5120" + "cd" * 32,
    )
    assert maker_keys.maker_claim_pubkey == "03" + "ab" * 32
    with pytest.raises(ValidationError, match="requires the maker claim key"):
        EscrowPublicKeysPayload(
            role=EscrowProtocolRole.MAKER,
            escrow_pubkey=LOCAL_NODE,
            timeout_destination_scriptpubkey="5120" + "cd" * 32,
        )
    with pytest.raises(ValidationError, match="cannot declare"):
        EscrowPublicKeysPayload(
            role=EscrowProtocolRole.PEER,
            escrow_pubkey=PEER_NODE,
            maker_claim_pubkey="03" + "ab" * 32,
            timeout_destination_scriptpubkey="5120" + "ef" * 32,
        )
    with pytest.raises(ValidationError, match="timeout transaction ID"):
        TimeoutMuSig2NoncePayload(timeout_txid="aa", public_nonce="bb" * 66)


def test_binding_requires_finalized_channel_and_matching_local_node() -> None:
    with pytest.raises(ChannelInputEscrowError, match="channel point"):
        EscrowPeerBinding.from_authorization_record(_record(channel_point=False), LOCAL_NODE)
    with pytest.raises(ChannelInputEscrowError, match="does not match"):
        EscrowPeerBinding.from_authorization_record(_record(), PEER_NODE)


async def test_outbound_is_persisted_before_send_failure_and_replayed_exactly(
    tmp_path: Path,
) -> None:
    transport, backend, journal = _transport(tmp_path)
    backend.fail_send = True
    with pytest.raises(RuntimeError, match="network"):
        await transport.send(EDGE_ID, _payload())
    persisted = journal.outbound(_binding())
    assert len(persisted) == 1

    backend.fail_send = False
    restarted = EscrowPeerTransport(
        backend, FakeRingStore(_record()), EscrowPeerJournal(tmp_path / "journal"), LOCAL_NODE
    )
    replayed = await restarted.replay(EDGE_ID)
    assert replayed == persisted
    assert backend.sent[-1][1] == persisted[0].encode()


async def test_valid_receive_is_dispatched_and_acknowledged(tmp_path: Path) -> None:
    transport, backend, journal = _transport(tmp_path)
    inbound = _inbound(_binding())

    accepted = await transport.accept(
        RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode())
    )

    assert accepted == inbound
    outbound = journal.outbound(_binding())
    assert isinstance(outbound[0].payload, AcknowledgmentPayload)
    assert outbound[0].payload.acknowledged_message_id == inbound.message_id
    assert backend.sent[0][2] == ESCROW_PEER_CUSTOM_MESSAGE_TYPE


async def test_duplicate_receive_is_not_redispatched_but_acknowledged_again(tmp_path: Path) -> None:
    transport, backend, journal = _transport(tmp_path)
    inbound = _inbound(_binding())
    raw = RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode())

    assert await transport.accept(raw) == inbound
    assert await transport.accept(raw) is None
    assert len(journal.outbound(_binding())) == 1
    assert len(backend.sent) == 2
    assert backend.sent[0][1] == backend.sent[1][1]


async def test_ack_send_failure_does_not_suppress_durable_delivery(tmp_path: Path) -> None:
    transport, backend, journal = _transport(tmp_path)
    backend.fail_send = True
    inbound = _inbound(_binding())

    accepted = await transport.accept(
        RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode())
    )

    assert accepted == inbound
    assert journal.pending_inbound(_binding()) == (inbound,)


@pytest.mark.parametrize(
    "variant", ["sequence", "gap", "previous", "peer", "recipient", "authorization", "channel"]
)
async def test_receive_rejects_conflicts_and_binding_mismatches(
    tmp_path: Path, variant: str
) -> None:
    transport, _backend, _journal = _transport(tmp_path)
    binding = _binding()
    first = _inbound(binding)
    await transport.accept(RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=first.encode()))
    raw_peer = PEER_NODE
    envelope = first
    if variant == "sequence":
        envelope = _inbound(
            binding,
            payload=RecoveryStatusPayload(
                state=EscrowLifecycleState.PARENT_SIGNED, parent_txid="bb" * 32
            ),
        )
    elif variant == "gap":
        envelope = _inbound(binding, sequence=3, previous=first.message_id)
    elif variant == "previous":
        envelope = _inbound(binding, sequence=2, previous="00" * 32)
    elif variant == "peer":
        raw_peer = "02" + "ab" * 32
    elif variant == "recipient":
        values = first.model_dump()
        values["recipient_node_id"] = "02" + "cd" * 32
        envelope = _recommit(values)
    elif variant == "authorization":
        values = first.model_dump()
        values["authorization_id"] = "ee" * 32
        envelope = _recommit(values)
    elif variant == "channel":
        values = first.model_dump()
        values["channel_point"] = {"txid": "ee" * 32, "vout": 1}
        envelope = _recommit(values)
    with pytest.raises(ChannelInputEscrowError):
        await transport.accept(
            RawReceivedCustomMessage(peer_node_id=raw_peer, data=envelope.encode())
        )


def test_decode_rejects_malformed_and_oversized_payloads() -> None:
    with pytest.raises(ChannelInputEscrowError, match="malformed"):
        EscrowPeerEnvelope.decode(b"not-json")
    with pytest.raises(ChannelInputEscrowError, match="size"):
        EscrowPeerEnvelope.decode(b" " * (MAX_ESCROW_PEER_ENVELOPE_BYTES + 1))


async def test_acknowledges_known_outbound_and_rejects_unknown_ids(tmp_path: Path) -> None:
    transport, _backend, journal = _transport(tmp_path)
    outbound = await transport.send(EDGE_ID, _payload())
    binding = _binding()
    reverse = EscrowPeerBinding(
        edge_id=binding.edge_id,
        authorization_id=binding.authorization_id,
        session_id=binding.session_id,
        channel_point=binding.channel_point,
        local_role=binding.local_role,
        local_node_id=binding.peer_node_id,
        peer_node_id=binding.local_node_id,
    )
    acknowledgment = EscrowPeerEnvelope.create(
        binding=reverse,
        sequence=1,
        previous_message_id=None,
        payload=AcknowledgmentPayload(acknowledged_message_id=outbound.message_id),
    )
    assert (
        await transport.accept(
            RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=acknowledgment.encode())
        )
        is None
    )
    journal_record = journal.load(binding)
    assert journal_record is not None
    assert outbound.message_id in journal_record.acknowledged_outbound_ids

    unknown = EscrowPeerEnvelope.create(
        binding=reverse,
        sequence=2,
        previous_message_id=acknowledgment.message_id,
        payload=AcknowledgmentPayload(acknowledged_message_id="ff" * 32),
    )
    with pytest.raises(ChannelInputEscrowError, match="unknown outbound"):
        await transport.accept(
            RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=unknown.encode())
        )
    unchanged = journal.load(binding)
    assert unchanged is not None
    assert unknown not in unchanged.inbound


async def test_pending_messages_are_redelivered_after_restart_until_consumer_resumes(
    tmp_path: Path,
) -> None:
    transport, backend, journal = _transport(tmp_path)
    inbound = _inbound(_binding())
    await transport.accept(RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode()))
    restarted = EscrowPeerTransport(
        backend, FakeRingStore(_record()), EscrowPeerJournal(tmp_path / "journal"), LOCAL_NODE
    )

    pending = restarted.pending_messages(EDGE_ID)
    assert await anext(pending) == inbound
    await anext(pending, None)
    assert journal.pending_inbound(_binding()) == ()


async def test_subscription_marks_message_dispatched_after_consumer_resumes(tmp_path: Path) -> None:
    transport, backend, journal = _transport(tmp_path)
    inbound = _inbound(_binding())
    backend.received.append(RawReceivedCustomMessage(peer_node_id=PEER_NODE, data=inbound.encode()))

    received = [message async for message in transport.messages()]

    assert received == [inbound]
    assert journal.pending_inbound(_binding()) == ()


def test_journal_rejects_corrupt_persisted_record(tmp_path: Path) -> None:
    journal = EscrowPeerJournal(tmp_path)
    binding = _binding()
    journal.prepare_outbound(binding, _payload())
    [record_path] = tmp_path.glob("*.json")
    record_path.write_text("not-json")

    with pytest.raises(ChannelInputEscrowError, match="journal is malformed"):
        journal.load(binding)


def test_journal_caps_each_direction(tmp_path: Path) -> None:
    journal = EscrowPeerJournal(tmp_path)
    binding = _binding()
    for _ in range(MAX_JOURNAL_MESSAGES_PER_DIRECTION):
        journal.prepare_outbound(binding, _payload())
    with pytest.raises(ChannelInputEscrowError, match="limit"):
        journal.prepare_outbound(binding, _payload())
