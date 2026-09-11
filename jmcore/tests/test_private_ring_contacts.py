from __future__ import annotations

import base64

import pytest
from bitcointx.core.key import CKey
from nacl import public
from pydantic import ValidationError

from jmcore.cofunded_ring import (
    CONTACT_PADDED_PLAINTEXT_BYTES,
    BackendLimits,
    ChannelPolicy,
    ContactEnvelope,
    CoordinatorParticipant,
    LocalContribution,
    PolicyBounds,
    PreparedOutgoingState,
    PrivateEdgePlan,
    PrivateParticipant,
    PublicPreparedIncomingState,
    RingContactDeliveryPayload,
    RingContactsPayload,
    RingKeyPair,
    RingNetwork,
    RingPlanAckPayload,
    RingPrivateHelloPayload,
    RingPrivateInvitePayload,
    RingPrivatePlanPayload,
    RingPrivatePreparedPayload,
    RingValidationError,
    canonical_json,
    contact_public_key,
    cycle_context_hash,
    decode_ring_message,
    decrypt_peer_contact,
    encode_ring_message,
    encrypt_peer_contact,
    sign_payload,
    validate_private_plan_for_invite,
)


def _secret(index: int) -> bytes:
    return index.to_bytes(32, "big")


def _ring_key(index: int) -> str:
    return RingKeyPair.from_secret(_secret(index)).public_key


def _contact_secret(index: int) -> str:
    return f"{index + 100:064x}"


def _node_id(index: int) -> str:
    return bytes(CKey.from_secret_bytes(_secret(index)).pub).hex()


def _policy() -> ChannelPolicy:
    return ChannelPolicy(
        fundee_csv_delay=144,
        fundee_reserve=10_000,
        min_depth=3,
        opener_csv_delay=144,
        opener_reserve=10_000,
    )


def _limits(network: RingNetwork = "regtest") -> BackendLimits:
    return BackendLimits(
        network=network,
        offer_type="tr0absoffer",
        min_channel_capacity=20_000,
        max_channel_capacity=10_000_000,
        max_push_amount=5_000_000,
        dust_limit=354,
        max_reserve=1_000_000,
        max_commitment_fee=100_000,
        max_pending_channels=8,
    )


def _coordinator(index: int, *, network: RingNetwork = "regtest") -> CoordinatorParticipant:
    return CoordinatorParticipant(
        participant_key=_ring_key(index),
        encryption_key=contact_public_key(_contact_secret(index)),
        backend_limits=_limits(network),
    )


def _private(index: int, *, network: RingNetwork = "regtest") -> PrivateParticipant:
    return PrivateParticipant(
        participant_key=_ring_key(index),
        node_id=_node_id(index + 20),
        onion_endpoint="a" * 56 + ".onion:9735",
        backend_limits=_limits(network),
    )


def _private_hello(
    index: int, *, revision: int = 3, network: RingNetwork = "regtest"
) -> RingPrivateHelloPayload:
    hello = RingPrivateHelloPayload(
        round_nonce="10" * 32,
        revision=revision,
        signer_key=_ring_key(index),
        participant=_coordinator(index, network=network),
    )
    signed = sign_payload(hello, _secret(index), aux_randomness=bytes([index]) * 32)
    assert isinstance(signed, RingPrivateHelloPayload)
    return signed


def _private_plan() -> tuple[RingPrivateInvitePayload, RingPrivatePlanPayload]:
    cycle = [_ring_key(index) for index in range(1, 5)]
    hellos = [_private_hello(index) for index in (2, 1, 3)]
    invite = RingPrivateInvitePayload(
        round_nonce="10" * 32,
        revision=3,
        signer_key=cycle[0],
        network="regtest",
        offer_type="tr0absoffer",
        expiry=100,
        policy_bounds=PolicyBounds(
            min_csv_delay=1,
            max_csv_delay=2016,
            min_depth=1,
            max_depth=144,
            min_reserve=0,
            max_reserve=1_000_000,
        ),
    )
    plan = RingPrivatePlanPayload(
        round_nonce=invite.round_nonce,
        revision=invite.revision,
        signer_key=cycle[0],
        network=invite.network,
        offer_type=invite.offer_type,
        cycle_keys=cycle,
        position=1,
        contribution=LocalContribution(
            participant_key=cycle[1], residual=300_000, outgoing=180_000, incoming=120_000
        ),
        predecessor=_coordinator(1),
        successor=_coordinator(3),
        incoming_edge=PrivateEdgePlan(
            edge_id="20" * 32,
            pending_channel_id="21" * 32,
            opener_key=cycle[0],
            acceptor_key=cycle[1],
            capacity=250_000,
            push_amount=120_000,
            policy=_policy(),
        ),
        outgoing_edge=PrivateEdgePlan(
            edge_id="22" * 32,
            pending_channel_id="23" * 32,
            opener_key=cycle[1],
            acceptor_key=cycle[2],
            capacity=300_000,
            push_amount=100_000,
            policy=_policy(),
        ),
        neighbor_hellos=hellos,
    )
    return invite, plan


def test_private_descriptors_and_prepared_state_exclude_persistent_contacts() -> None:
    invite, plan = _private_plan()
    validate_private_plan_for_invite(invite, plan)
    encoded = canonical_json(plan)
    assert b"node_id" not in encoded
    assert b"onion" not in encoded
    assert decode_ring_message(encode_ring_message(plan)) == plan

    outgoing = PreparedOutgoingState(
        pending_channel_id="31" * 32,
        opener_key=_ring_key(2),
        acceptor_key=_ring_key(3),
        script_pubkey="5120" + "32" * 32,
        capacity=200_000,
        push_amount=80_000,
        policy=_policy(),
    )
    incoming = PublicPreparedIncomingState(
        pending_channel_id="33" * 32,
        opener_key=_ring_key(1),
        acceptor_key=_ring_key(2),
        capacity=210_000,
        push_amount=90_000,
        policy=_policy(),
    )
    prepared = RingPrivatePreparedPayload(
        round_nonce="10" * 32,
        revision=3,
        signer_key=_ring_key(2),
        outgoing=outgoing,
        incoming=incoming,
    )
    assert "opener_node_id" not in prepared.model_dump()["incoming"]
    assert decode_ring_message(encode_ring_message(prepared)) == prepared

    with pytest.raises(ValidationError, match="Input should be 2"):
        RingPrivateInvitePayload.model_validate({**invite.model_dump(), "v": 1})
    with pytest.raises(ValidationError, match="Input should be 1 or 2"):
        RingPlanAckPayload.model_validate(
            {
                "v": 3,
                "round_nonce": "10" * 32,
                "revision": 3,
                "signer_key": _ring_key(1),
                "plan_hash": "44" * 32,
            }
        )
    shared = RingPlanAckPayload(
        v=2,
        round_nonce="10" * 32,
        revision=3,
        signer_key=_ring_key(1),
        plan_hash="44" * 32,
    )
    assert decode_ring_message(encode_ring_message(shared)) == shared


def test_private_plan_rejects_invalid_signed_hello_bindings() -> None:
    invite, plan = _private_plan()
    bad_signature = plan.neighbor_hellos.copy()
    bad_signature[0] = bad_signature[0].model_copy(update={"sig": "00" * 64})
    with pytest.raises(RingValidationError, match="signed hello"):
        validate_private_plan_for_invite(
            invite, plan.model_copy(update={"neighbor_hellos": bad_signature})
        )

    substituted = _coordinator(1).model_copy(
        update={"encryption_key": contact_public_key(_contact_secret(9))}
    )
    with pytest.raises(RingValidationError, match="signed hello"):
        validate_private_plan_for_invite(
            invite, plan.model_copy(update={"predecessor": substituted})
        )

    for update in (
        {"revision": plan.revision + 1},
        {"network": "signet"},
        {
            "cycle_keys": [
                plan.cycle_keys[0],
                plan.cycle_keys[2],
                plan.cycle_keys[1],
                plan.cycle_keys[3],
            ]
        },
    ):
        with pytest.raises(RingValidationError, match="signed hello"):
            validate_private_plan_for_invite(invite, plan.model_copy(update=update))


def test_authenticated_contact_round_trip_and_payload_cardinality() -> None:
    cycle = [_ring_key(index) for index in range(1, 5)]
    context = cycle_context_hash("regtest", "10" * 32, 3, cycle)
    sender = _private(2)
    envelope = encrypt_peer_contact(_contact_secret(2), _coordinator(1), sender, context)

    recovered = decrypt_peer_contact(
        _contact_secret(1), _coordinator(2), envelope, _ring_key(1), context
    )
    assert recovered == sender

    second = encrypt_peer_contact(_contact_secret(2), _coordinator(3), sender, context)
    contacts = RingContactsPayload(
        round_nonce="10" * 32,
        revision=3,
        signer_key=sender.participant_key,
        envelopes=[envelope, second],
    )
    assert decode_ring_message(encode_ring_message(contacts)) == contacts

    incoming = encrypt_peer_contact(_contact_secret(3), _coordinator(1), _private(3), context)
    delivery = RingContactDeliveryPayload(
        round_nonce="10" * 32,
        revision=3,
        signer_key=_ring_key(1),
        envelopes=[envelope, incoming],
    )
    assert decode_ring_message(encode_ring_message(delivery)) == delivery
    with pytest.raises(ValidationError, match="duplicate contact sender"):
        RingContactDeliveryPayload(**{**delivery.model_dump(), "envelopes": [envelope, envelope]})
    with pytest.raises(ValidationError, match="contact envelope sender"):
        RingContactsPayload(**{**contacts.model_dump(), "signer_key": _ring_key(4)})


def test_authenticated_contact_rejects_wrong_bindings_ciphertext_and_key_substitution() -> None:
    context = cycle_context_hash(
        "regtest", "10" * 32, 3, [_ring_key(index) for index in range(1, 5)]
    )
    envelope = encrypt_peer_contact(_contact_secret(2), _coordinator(1), _private(2), context)

    cases = (
        (_contact_secret(1), _coordinator(3), envelope, _ring_key(1), context),
        (_contact_secret(1), _coordinator(2), envelope, _ring_key(3), context),
        (_contact_secret(1), _coordinator(2), envelope, _ring_key(1), "aa" * 32),
        (_contact_secret(4), _coordinator(2), envelope, _ring_key(1), context),
        (
            _contact_secret(1),
            _coordinator(2).model_copy(
                update={"encryption_key": contact_public_key(_contact_secret(9))}
            ),
            envelope,
            _ring_key(1),
            context,
        ),
    )
    for args in cases:
        with pytest.raises(RingValidationError):
            decrypt_peer_contact(*args)

    altered_first_char = "B" if envelope.ciphertext[0] != "B" else "C"
    altered = envelope.model_copy(
        update={"ciphertext": altered_first_char + envelope.ciphertext[1:]}
    )
    with pytest.raises(RingValidationError, match="authentication"):
        decrypt_peer_contact(_contact_secret(1), _coordinator(2), altered, _ring_key(1), context)


def test_authenticated_contact_rejects_malformed_padded_plaintext_and_low_order_key() -> None:
    context = cycle_context_hash(
        "regtest", "10" * 32, 3, [_ring_key(index) for index in range(1, 5)]
    )
    malformed_plaintext = b"\x00\x02{}\x01" + b"\x00" * (CONTACT_PADDED_PLAINTEXT_BYTES - 5)
    ciphertext = bytes(
        public.Box(
            public.PrivateKey(bytes.fromhex(_contact_secret(2))),
            public.PublicKey(bytes.fromhex(contact_public_key(_contact_secret(1)))),
        ).encrypt(malformed_plaintext)
    )
    envelope = ContactEnvelope(
        sender_key=_ring_key(2),
        recipient_key=_ring_key(1),
        context_hash=context,
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
    )
    with pytest.raises(RingValidationError, match="padding"):
        decrypt_peer_contact(_contact_secret(1), _coordinator(2), envelope, _ring_key(1), context)
    with pytest.raises(ValidationError, match="low-order"):
        CoordinatorParticipant(
            participant_key=_ring_key(2),
            encryption_key="00" * 32,
            backend_limits=_limits(),
        )
