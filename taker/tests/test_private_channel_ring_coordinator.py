from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from jmcore.channel_ring_store import RingLifecycleState, RingParticipantRecord, RingParticipantRole
from jmcore.cofunded_ring import (
    CoordinatorParticipant,
    PreparedOutgoingState,
    PrivateParticipant,
    PublicPreparedIncomingState,
    RingContactDeliveryPayload,
    RingContactsPayload,
    RingPayloadType,
    RingPlanAckPayload,
    RingPrivateHelloPayload,
    RingPrivatePlanPayload,
    RingPrivatePreparedPayload,
    RingReadyPayload,
    RingReadySetAckPayload,
    RingReadySetPayload,
    RingSignAckPayload,
    RingSignPayload,
    RingUnsignedPayload,
    canonical_json,
    contact_public_key,
    cycle_context_hash,
    encode_ring_message,
    encrypt_peer_contact,
    ring_hash,
    sign_payload,
)
from test_channel_ring_coordinator import (
    FakeLnd,
    MockCoordinator,
    _address,
    _harness,
    _limits,
    _node,
    _ring_key,
)

from taker.channel_ring import TakerRingCoordinator, TakerRingError, reconcile_taker_ring_records


class PrivateMockCoordinator(MockCoordinator):
    def __init__(self, *args: Any, malformed_contacts: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.contact_secrets = {f"maker-{index}": f"{100 + index:064x}" for index in range(1, 4)}
        self.malformed_contacts = malformed_contacts
        self.transcript: list[tuple[str, RingPayloadType]] = []
        self.plan_ack_hashes: dict[str, str] = {}
        self.deliveries: dict[str, RingContactDeliveryPayload] = {}

    def _private_participant(self, nick: str, key: str) -> PrivateParticipant:
        index = int(nick[-1])
        return PrivateParticipant(
            participant_key=key,
            node_id=_node(index),
            onion_endpoint="a" * 56 + ".onion:9735",
            backend_limits=_limits(),
        )

    def _private_hello(self, nick: str, record: RingParticipantRecord) -> RingPrivateHelloPayload:
        key = _ring_key(int(nick[-1]))
        return cast(
            RingPrivateHelloPayload,
            self._peer_sign(
                nick,
                RingPrivateHelloPayload(
                    v=2,
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=key,
                    participant=CoordinatorParticipant(
                        participant_key=key,
                        encryption_key=contact_public_key(self.contact_secrets[nick]),
                        backend_limits=_limits(),
                    ),
                ),
            ),
        )

    def _contacts(self, nick: str, plan: RingPrivatePlanPayload) -> RingContactsPayload:
        key = plan.contribution.participant_key
        sender = self._private_participant(nick, key)
        context = cycle_context_hash(plan.network, plan.round_nonce, plan.revision, plan.cycle_keys)
        recipients = [plan.predecessor, plan.successor]
        if self.malformed_contacts and nick == "maker-1":
            replacement = next(
                item
                for item in self._record().coordinator_participants
                if item.participant_key
                not in {key, plan.predecessor.participant_key, plan.successor.participant_key}
            )
            assert isinstance(replacement, CoordinatorParticipant)
            recipients[1] = replacement
        return cast(
            RingContactsPayload,
            self._peer_sign(
                nick,
                RingContactsPayload(
                    v=2,
                    round_nonce=plan.round_nonce,
                    revision=plan.revision,
                    signer_key=key,
                    envelopes=[
                        encrypt_peer_contact(self.contact_secrets[nick], recipient, sender, context)
                        for recipient in recipients
                    ],
                ),
            ),
        )

    async def _send(self, nick: str, payload: RingPayloadType) -> None:
        self.transcript.append((nick, payload))

    async def _receive(
        self,
        expected_type: type[Any],
        *,
        expected_counts: int = 1,
        timeout: float | None = None,
    ) -> dict[str, list[RingPayloadType]]:
        del expected_counts, timeout
        if expected_type is not RingPrivatePreparedPayload:
            raise AssertionError(f"unexpected receive phase {expected_type}")
        record = self._record()
        result: dict[str, list[RingPayloadType]] = {}
        for key, nick in self._peer_by_key.items():
            plan = record.coordinator_plans[key]
            assert isinstance(plan, RingPrivatePlanPayload)
            outgoing = plan.outgoing_edge
            incoming = plan.incoming_edge
            result[nick] = [
                self._peer_sign(
                    nick,
                    RingPrivatePreparedPayload(
                        v=2,
                        round_nonce=record.round_nonce,
                        revision=record.revision,
                        signer_key=key,
                        outgoing=PreparedOutgoingState(
                            pending_channel_id=outgoing.pending_channel_id,
                            opener_key=outgoing.opener_key,
                            acceptor_key=outgoing.acceptor_key,
                            script_pubkey="5120" + outgoing.pending_channel_id,
                            capacity=outgoing.capacity,
                            push_amount=outgoing.push_amount,
                            policy=outgoing.policy,
                        ),
                        incoming=PublicPreparedIncomingState(
                            pending_channel_id=incoming.pending_channel_id,
                            opener_key=incoming.opener_key,
                            acceptor_key=incoming.acceptor_key,
                            capacity=incoming.capacity,
                            push_amount=incoming.push_amount,
                            policy=incoming.policy,
                        ),
                    ),
                )
            ]
        return result

    async def _exchange(
        self,
        payloads: Any,
        expected_type: type[Any],
        *,
        expected_counts: int = 1,
        timeout: float | None = None,
    ) -> dict[str, list[RingPayloadType]]:
        del expected_counts, timeout
        self.transcript.extend(payloads.items())
        record = self._record()
        if expected_type is RingPrivateHelloPayload:
            return {nick: [self._private_hello(nick, record)] for nick in payloads}
        if expected_type is RingContactsPayload:
            return {
                nick: [self._contacts(nick, cast(RingPrivatePlanPayload, plan))]
                for nick, plan in payloads.items()
            }
        if expected_type is RingPlanAckPayload:
            result: dict[str, list[RingPayloadType]] = {}
            for nick, delivery in payloads.items():
                assert isinstance(delivery, RingContactDeliveryPayload)
                key = next(key for key, peer in self._peer_by_key.items() if peer == nick)
                self.deliveries[key] = delivery
                plan_hash = ring_hash("plan", record.coordinator_plans[key]).hex()
                self.plan_ack_hashes[key] = plan_hash
                result[nick] = [
                    self._peer_sign(
                        nick,
                        RingPlanAckPayload(
                            v=2,
                            round_nonce=record.round_nonce,
                            revision=record.revision,
                            signer_key=key,
                            plan_hash=plan_hash,
                        ),
                    )
                ]
            return result
        if expected_type is RingReadyPayload:
            cast(FakeLnd, self.lnd).unsigned_distributed.set()
            return {
                nick: [
                    self._peer_sign(
                        nick,
                        item.model_copy(update={"v": 2, "sig": "00" * 64}),
                    )
                    for item in self._ready(nick, cast(RingUnsignedPayload, unsigned))
                ]
                for nick, unsigned in payloads.items()
            }
        if expected_type is RingReadySetAckPayload:
            return {
                nick: [
                    self._peer_sign(
                        nick,
                        RingReadySetAckPayload(
                            v=2,
                            round_nonce=record.round_nonce,
                            revision=record.revision,
                            signer_key=key,
                            readiness_hash=ring_hash(
                                "ready-set", cast(RingReadySetPayload, ready_set).attestations
                            ).hex(),
                        ),
                    )
                ]
                for nick, ready_set in payloads.items()
                for key in (next(key for key, peer in self._peer_by_key.items() if peer == nick),)
            }
        if expected_type is RingSignAckPayload:
            return {
                nick: [
                    self._peer_sign(
                        nick,
                        RingSignAckPayload(
                            v=2,
                            round_nonce=record.round_nonce,
                            revision=record.revision,
                            signer_key=key,
                            manifest_hash=cast(RingSignPayload, sign).manifest_hash,
                            unsigned_tx_hash=cast(RingSignPayload, sign).unsigned_tx_hash,
                        ),
                    )
                ]
                for nick, sign in payloads.items()
                for key in (next(key for key, peer in self._peer_by_key.items() if peer == nick),)
            }
        raise AssertionError(f"unexpected exchange phase {expected_type}")


def _private_harness(tmp_path: Path, *, malformed_contacts: bool = False) -> PrivateMockCoordinator:
    legacy = _harness(tmp_path)
    config = legacy.config.model_copy(update={"private_coordination": True})
    for maker in legacy.session.maker_sessions.values():
        maker.offer = maker.offer.model_copy(
            update={"features": {"cofunded_channel_ring_v2": True}}
        )
    return PrivateMockCoordinator(
        legacy.session,
        config=config,
        store=legacy.store,
        initialized_backend=legacy.initialized_backend,
        chain_backend=legacy.chain_backend,
        session_identity="private-test-session",
        malformed_contacts=malformed_contacts,
    )


async def test_private_coordinator_full_transcript_keeps_contacts_local(tmp_path: Path) -> None:
    coordinator = _private_harness(tmp_path)

    assert await coordinator.prepare(_address(300), 0)

    record = coordinator._record()
    assert record.state is RingLifecycleState.SIGNING
    assert record.contact_secret is not None
    assert isinstance(record.plan, RingPrivatePlanPayload)
    assert record.manifest is not None
    assert all(isinstance(item, CoordinatorParticipant) for item in record.coordinator_participants)
    assert all(
        isinstance(item, RingPrivatePlanPayload) for item in record.coordinator_plans.values()
    )
    assert all(item.v == 2 for _, item in coordinator.transcript)
    assert all(
        b"node_id" not in canonical_json(payload)
        and b"onion_endpoint" not in canonical_json(payload)
        for _, payload in coordinator.transcript
    )
    assert set(record.local_contacts) == {
        record.plan.predecessor.participant_key,
        record.plan.successor.participant_key,
    }
    backend_plan = record.backend_plan()
    assert (
        backend_plan.predecessor == record.local_contacts[backend_plan.predecessor.participant_key]
    )
    assert backend_plan.successor == record.local_contacts[backend_plan.successor.participant_key]
    assert record.incoming_edge is not None and record.outgoing_edge is not None
    assert record.incoming_edge.funding_outpoint is not None
    assert record.incoming_edge.peer == backend_plan.predecessor
    assert record.outgoing_edge.peer == backend_plan.successor
    assert all(
        coordinator.plan_ack_hashes[key] == ring_hash("plan", plan).hex()
        for key, plan in record.coordinator_plans.items()
        if key != record.ring_public_key
    )
    for key, delivery in coordinator.deliveries.items():
        assert {item.recipient_key for item in delivery.envelopes} == {key}
        assert {item.sender_key for item in delivery.envelopes} == {
            record.coordinator_plans[key].predecessor.participant_key,
            record.coordinator_plans[key].successor.participant_key,
        }

    await reconcile_taker_ring_records(
        coordinator.store,
        coordinator.initialized_backend,
        cast(Any, coordinator.chain_backend),
        active_session_identities=frozenset({record.taker_session_identity}),
    )
    lnd = cast(FakeLnd, coordinator.lnd)
    assert lnd.resumed_incoming_points == [
        f"{record.manifest.unsigned_txid}:{record.incoming_edge.funding_outpoint.vout}"
    ]
    assert lnd.resumed_verified[0][0] is not None
    assert lnd.resumed_verified[0][0].pending_channel_id == bytes.fromhex(
        backend_plan.outgoing_edge.pending_channel_id
    )


async def test_private_coordinator_rejects_wrong_contact_recipient(tmp_path: Path) -> None:
    coordinator = _private_harness(tmp_path, malformed_contacts=True)
    coordinator._preconditions()
    tx_fee, residuals = coordinator._finalize_fees_and_inputs(0)
    await coordinator._invite(tx_fee, residuals)

    with pytest.raises(TakerRingError, match="recipients do not match"):
        await coordinator._plan()


async def test_stranded_private_planned_ring_escalates_before_contact_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _private_harness(tmp_path)
    coordinator._preconditions()
    tx_fee, residuals = coordinator._finalize_fees_and_inputs(0)
    await coordinator._invite(tx_fee, residuals)

    async def lost_driving_session(*args: Any, **kwargs: Any) -> dict[str, list[RingPayloadType]]:
        del args, kwargs
        raise RuntimeError("driving session lost")

    monkeypatch.setattr(coordinator, "_exchange", lost_driving_session)
    with pytest.raises(RuntimeError, match="driving session lost"):
        await coordinator._plan()

    record = coordinator._record()
    assert record.state is RingLifecycleState.PLANNED
    assert isinstance(record.plan, RingPrivatePlanPayload)
    assert record.local_contacts == {}
    reserved_inputs = set(coordinator.session.reserved_inputs)
    locked_inputs = set(coordinator.session.wallet.locked)

    await reconcile_taker_ring_records(
        coordinator.store,
        coordinator.initialized_backend,
        cast(Any, coordinator.chain_backend),
        active_session_identities=frozenset({record.taker_session_identity}),
    )
    assert coordinator._record().state is RingLifecycleState.PLANNED

    await reconcile_taker_ring_records(
        coordinator.store, coordinator.initialized_backend, cast(Any, coordinator.chain_backend)
    )

    escalated = coordinator._record()
    lnd = cast(FakeLnd, coordinator.lnd)
    assert escalated.state is RingLifecycleState.RECOVERY_REQUIRED
    assert escalated.retry is not None
    assert escalated.retry.last_error is not None
    assert "cannot be resumed" in escalated.retry.last_error
    assert coordinator.session.reserved_inputs == reserved_inputs
    assert coordinator.session.wallet.locked == locked_inputs
    assert lnd.incoming is None
    assert lnd.canceled == []
    assert lnd.retired == []
    assert lnd.resumed_incoming_points == []
    assert lnd.resumed_verified == []


class _TokenCrypto:
    def __init__(self, messages: dict[str, str]) -> None:
        self.messages = messages

    def decrypt(self, value: str) -> str:
        return self.messages[value]


class _ResponseDirectory:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    async def wait_for_responses(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return self.responses


def _wire_coordinator(
    tmp_path: Path, *, duplicate: bool = False, wrong_version: bool = False
) -> TakerRingCoordinator:
    source = _private_harness(tmp_path)
    coordinator = TakerRingCoordinator(
        source.session,
        config=source.config,
        store=source.store,
        initialized_backend=source.initialized_backend,
        chain_backend=source.chain_backend,
        session_identity="wire-test-session",
    )
    record = RingParticipantRecord.fresh(
        round_nonce="aa" * 32,
        revision=0,
        taker_session_identity="wire-test-session",
        local_role=RingParticipantRole.TAKER,
        local_position=0,
        private_coordination=True,
    )
    coordinator.store.save(record)
    coordinator.record_key = record.key
    messages: dict[str, str] = {}
    responses: dict[str, Any] = {}
    for index, nick in enumerate(coordinator.session.maker_sessions, start=1):
        key = _ring_key(index)
        coordinator._peer_by_key[key] = nick
        payload = RingPlanAckPayload(
            v=1 if wrong_version else 2,
            round_nonce=record.round_nonce,
            revision=record.revision,
            signer_key=key,
            plan_hash="11" * 32,
        )
        signed_payload = sign_payload(payload, index.to_bytes(32, "big"))
        token = f"response-{index}"
        messages[token] = encode_ring_message(signed_payload)
        coordinator.session.maker_sessions[nick].crypto = cast(Any, _TokenCrypto(messages))
        values = [token, token] if duplicate else token
        responses[nick] = {"data": values}
    coordinator.session._taker.directory_client = _ResponseDirectory(responses)
    return coordinator


async def test_private_coordinator_rejects_replayed_or_wrong_version_response(
    tmp_path: Path,
) -> None:
    wrong_version = _wire_coordinator(tmp_path / "version", wrong_version=True)
    with pytest.raises(TakerRingError, match="wrong protocol version"):
        await wrong_version._receive(RingPlanAckPayload)

    replay = _wire_coordinator(tmp_path / "replay", duplicate=True)
    with pytest.raises(TakerRingError, match="2 responses, expected 1"):
        await replay._receive(RingPlanAckPayload)
