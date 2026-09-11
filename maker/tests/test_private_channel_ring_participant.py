from __future__ import annotations

import time
from pathlib import Path
from typing import cast

import pytest
from jmcore.channel_ring_store import RingLifecycleState
from jmcore.cofunded_ring import (
    CoordinatorParticipant,
    LocalContribution,
    PolicyBounds,
    PrivateEdgePlan,
    PrivateParticipant,
    RingContactDeliveryPayload,
    RingContactsPayload,
    RingOpenPayload,
    RingPlanAckPayload,
    RingPrivateHelloPayload,
    RingPrivateInvitePayload,
    RingPrivatePlanPayload,
    RingPrivatePreparedPayload,
    RingSignPayload,
    canonical_json,
    contact_public_key,
    cycle_context_hash,
    decrypt_peer_contact,
    encrypt_peer_contact,
    manifest_hash,
    sign_payload,
)
from test_channel_ring_participant import (
    LOCAL_NODE,
    Harness,
    _key,
    _limits,
    _node,
    _participant,
    _policy,
    _secret,
)

from maker.channel_ring import MakerRingError

PREDECESSOR_CONTACT_SECRET = f"{101:064x}"
SUCCESSOR_CONTACT_SECRET = f"{102:064x}"
NON_NEIGHBOR_CONTACT_SECRET = f"{103:064x}"


def _private_harness(tmp_path: Path) -> Harness:
    harness = Harness(tmp_path)
    harness.config = harness.config.model_copy(update={"private_coordination": True})
    harness.machine.config = harness.config
    return harness


def _private_invite(harness: Harness) -> RingPrivateInvitePayload:
    return harness.signed(
        RingPrivateInvitePayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=harness.taker_key,
            network="regtest",
            offer_type="tr0absoffer",
            expiry=int(time.time()) + 60,
            policy_bounds=PolicyBounds(
                min_csv_delay=144,
                max_csv_delay=144,
                min_depth=3,
                max_depth=3,
                min_reserve=10_000,
                max_reserve=10_000,
            ),
        )
    )


def _coordinator(key: str, contact_secret: str) -> CoordinatorParticipant:
    return CoordinatorParticipant(
        participant_key=key,
        encryption_key=contact_public_key(contact_secret),
        backend_limits=_limits(),
    )


def _private_hello(
    *,
    key: str,
    secret: bytes,
    contact_secret: str,
) -> RingPrivateHelloPayload:
    return cast(
        RingPrivateHelloPayload,
        sign_payload(
            RingPrivateHelloPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=key,
                participant=_coordinator(key, contact_secret),
            ),
            secret,
        ),
    )


def _private_plan(
    harness: Harness,
    local_hello: RingPrivateHelloPayload,
) -> RingPrivatePlanPayload:
    local = harness.record().ring_public_key
    predecessor = _coordinator(harness.taker_key, PREDECESSOR_CONTACT_SECRET)
    successor = _coordinator(_key(12), SUCCESSOR_CONTACT_SECRET)
    return harness.signed(
        RingPrivatePlanPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=harness.taker_key,
            network="regtest",
            offer_type="tr0absoffer",
            cycle_keys=[harness.taker_key, local, _key(12), _key(13)],
            position=1,
            contribution=LocalContribution(
                participant_key=local,
                residual=1_000_000,
                outgoing=600_000,
                incoming=400_000,
            ),
            predecessor=predecessor,
            successor=successor,
            incoming_edge=PrivateEdgePlan(
                edge_id="21" * 32,
                pending_channel_id="11" * 32,
                opener_key=harness.taker_key,
                acceptor_key=local,
                capacity=1_000_000,
                push_amount=400_000,
                policy=_policy(),
            ),
            outgoing_edge=PrivateEdgePlan(
                edge_id="22" * 32,
                pending_channel_id="12" * 32,
                opener_key=local,
                acceptor_key=_key(12),
                capacity=900_000,
                push_amount=300_000,
                policy=_policy(),
            ),
            neighbor_hellos=[
                local_hello,
                _private_hello(
                    key=harness.taker_key,
                    secret=harness.taker_secret,
                    contact_secret=PREDECESSOR_CONTACT_SECRET,
                ),
                _private_hello(
                    key=_key(12),
                    secret=_secret(12),
                    contact_secret=SUCCESSOR_CONTACT_SECRET,
                ),
            ],
        )
    )


def _delivery(harness: Harness, local: CoordinatorParticipant) -> RingContactDeliveryPayload:
    record = harness.record()
    assert isinstance(record.plan, RingPrivatePlanPayload)
    context = cycle_context_hash(
        record.plan.network,
        record.round_nonce,
        record.revision,
        record.plan.cycle_keys,
    )
    predecessor = _participant(harness.taker_key, _node(20))
    successor = _participant(_key(12), _node(22))
    return harness.signed(
        RingContactDeliveryPayload(
            v=2,
            round_nonce=record.round_nonce,
            revision=record.revision,
            signer_key=harness.taker_key,
            envelopes=[
                encrypt_peer_contact(PREDECESSOR_CONTACT_SECRET, local, predecessor, context),
                encrypt_peer_contact(SUCCESSOR_CONTACT_SECRET, local, successor, context),
            ],
        )
    )


async def test_private_participant_contacts_then_opens_without_contact_disclosure(
    tmp_path: Path,
) -> None:
    harness = _private_harness(tmp_path)
    hello_response = await harness.machine.handle(_private_invite(harness))
    local_hello = hello_response[0]
    assert isinstance(local_hello, RingPrivateHelloPayload)
    assert "node_id" not in local_hello.model_dump()["participant"]
    assert "onion_endpoint" not in local_hello.model_dump()["participant"]

    plan = _private_plan(harness, local_hello)
    contacts_response = await harness.machine.handle(plan)
    contacts = cast(RingContactsPayload, contacts_response[0])
    assert contacts.v == 2
    assert harness.record().state is RingLifecycleState.PLANNED
    assert harness.record().incoming_edge is None
    assert harness.record().outgoing_edge is None
    assert harness.machine.acceptor_task is None
    assert b"node_id" not in canonical_json(plan)
    assert b"onion_endpoint" not in canonical_json(plan)
    assert b"node_id" not in canonical_json(contacts)
    assert b"onion_endpoint" not in canonical_json(contacts)

    record = harness.record()
    assert isinstance(record.plan, RingPrivatePlanPayload)
    context = cycle_context_hash(
        record.plan.network,
        record.round_nonce,
        record.revision,
        record.plan.cycle_keys,
    )
    by_recipient = {envelope.recipient_key: envelope for envelope in contacts.envelopes}
    for recipient, secret in (
        (harness.taker_key, PREDECESSOR_CONTACT_SECRET),
        (_key(12), SUCCESSOR_CONTACT_SECRET),
    ):
        decrypted = decrypt_peer_contact(
            secret,
            local_hello.participant,
            by_recipient[recipient],
            recipient,
            context,
        )
        assert decrypted.node_id == LOCAL_NODE
        assert decrypted.onion_endpoint == harness.initialized.onion_endpoint

    assert await harness.machine.handle(plan) == contacts_response
    delivery = _delivery(harness, local_hello.participant)
    acknowledgments = await harness.machine.handle(delivery)
    acknowledgment = cast(RingPlanAckPayload, acknowledgments[0])
    assert acknowledgment.v == 2
    assert harness.record().state is RingLifecycleState.ACCEPTOR_ARMED
    assert harness.record().backend_plan().predecessor.node_id == _node(20)
    assert harness.record().backend_plan().successor.node_id == _node(22)
    assert await harness.machine.handle(delivery) == acknowledgments

    prepared = await harness.machine.handle(
        harness.signed(
            RingOpenPayload(
                v=2,
                round_nonce="aa" * 32,
                revision=0,
                signer_key=harness.taker_key,
                plan_hash=acknowledgment.plan_hash,
            )
        )
    )
    assert isinstance(prepared[0], RingPrivatePreparedPayload)
    assert "opener_node_id" not in prepared[0].incoming.model_dump()
    assert b"opener_node_id" not in canonical_json(prepared[0])
    assert harness.record().prepared_incoming is not None
    assert harness.record().prepared_incoming.opener_node_id == _node(20)

    unsigned = await harness.unsigned()
    unsigned = harness.signed(unsigned.model_copy(update={"v": 2, "sig": "00" * 64}))
    ready = await harness.machine.handle(unsigned)
    assert [item.v for item in ready] == [2, 2]
    ready_set = harness.ready_set(unsigned.manifest)
    ready_set = harness.signed(ready_set.model_copy(update={"v": 2, "sig": "00" * 64}))
    ready_ack = await harness.machine.handle(ready_set)
    assert ready_ack[0].v == 2
    sign_ack = await harness.machine.handle(
        harness.signed(
            RingSignPayload(
                v=2,
                round_nonce="aa" * 32,
                revision=0,
                signer_key=harness.taker_key,
                manifest_hash=manifest_hash(unsigned.manifest).hex(),
                unsigned_tx_hash=unsigned.manifest.unsigned_tx_hash,
            )
        )
    )
    assert sign_ack[0].v == 2


async def test_private_participant_rejects_invalid_descriptor_delivery_and_order(
    tmp_path: Path,
) -> None:
    invalid = _private_harness(tmp_path / "invalid")
    hello = await invalid.machine.handle(_private_invite(invalid))
    assert isinstance(hello[0], RingPrivateHelloPayload)
    bad_local = hello[0].model_copy(
        update={
            "participant": hello[0].participant.model_copy(
                update={"encryption_key": contact_public_key(NON_NEIGHBOR_CONTACT_SECRET)}
            )
        }
    )
    bad_local = cast(
        RingPrivateHelloPayload,
        sign_payload(bad_local, bytes.fromhex(invalid.record().ring_secret)),
    )
    invalid_plan = _private_plan(invalid, bad_local)
    with pytest.raises(MakerRingError, match="local contact descriptor"):
        await invalid.machine.handle(invalid_plan)

    harness = _private_harness(tmp_path / "delivery")
    hello = await harness.machine.handle(_private_invite(harness))
    assert isinstance(hello[0], RingPrivateHelloPayload)
    plan = _private_plan(harness, hello[0])
    await harness.machine.handle(plan)
    with pytest.raises(MakerRingError, match="ring_open is out of order"):
        await harness.machine.handle(
            harness.signed(
                RingOpenPayload(
                    v=2,
                    round_nonce="aa" * 32,
                    revision=0,
                    signer_key=harness.taker_key,
                    plan_hash=harness.record().plan_hash,
                )
            )
        )

    local = hello[0].participant
    record = harness.record()
    assert isinstance(record.plan, RingPrivatePlanPayload)
    context = cycle_context_hash(
        record.plan.network,
        record.round_nonce,
        record.revision,
        record.plan.cycle_keys,
    )
    invalid_delivery = harness.signed(
        RingContactDeliveryPayload(
            v=2,
            round_nonce=record.round_nonce,
            revision=record.revision,
            signer_key=harness.taker_key,
            envelopes=[
                encrypt_peer_contact(
                    PREDECESSOR_CONTACT_SECRET,
                    local,
                    _participant(harness.taker_key, _node(20)),
                    context,
                ),
                encrypt_peer_contact(
                    NON_NEIGHBOR_CONTACT_SECRET,
                    local,
                    PrivateParticipant(
                        participant_key=_key(13),
                        node_id=_node(23),
                        onion_endpoint="a" * 56 + ".onion:9735",
                        backend_limits=_limits(),
                    ),
                    context,
                ),
            ],
        )
    )
    with pytest.raises(MakerRingError, match="contact delivery"):
        await harness.machine.handle(invalid_delivery)
    assert harness.record().state is RingLifecycleState.PLANNED

    with pytest.raises(MakerRingError, match="version"):
        await harness.machine.handle(
            harness.signed(
                RingOpenPayload(
                    round_nonce="aa" * 32,
                    revision=0,
                    signer_key=harness.taker_key,
                    plan_hash=harness.record().plan_hash,
                )
            )
        )


async def test_private_mode_refuses_new_v1_but_retains_v1_record_recovery(tmp_path: Path) -> None:
    refused = _private_harness(tmp_path / "refused")
    with pytest.raises(MakerRingError, match="legacy ring invitations"):
        await refused.machine.handle(refused.invite())

    retained = Harness(tmp_path / "retained")
    await retained.machine.handle(retained.invite())
    acknowledgment = cast(RingPlanAckPayload, (await retained.machine.handle(retained.plan()))[0])
    retained.config = retained.config.model_copy(update={"private_coordination": True})
    retained.machine.config = retained.config
    prepared = await retained.machine.handle(
        retained.signed(
            RingOpenPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=retained.taker_key,
                plan_hash=acknowledgment.plan_hash,
            )
        )
    )
    assert prepared[0].v == 1


async def test_private_invites_require_private_mode_but_replay_after_disable(
    tmp_path: Path,
) -> None:
    refused = Harness(tmp_path / "refused")
    with pytest.raises(
        MakerRingError, match="private ring invitations require private coordination"
    ):
        await refused.machine.handle(_private_invite(refused))
    assert not refused.store.load_all().records

    retained = _private_harness(tmp_path / "retained")
    invite = _private_invite(retained)
    accepted = await retained.machine.handle(invite)
    assert isinstance(accepted[0], RingPrivateHelloPayload)
    retained.config = retained.config.model_copy(update={"private_coordination": False})
    retained.machine.config = retained.config

    assert await retained.machine.handle(invite) == accepted
