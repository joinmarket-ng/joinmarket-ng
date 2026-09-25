from __future__ import annotations

import base64
import hashlib
import json

import pytest
from bitcointx.core.key import CKey
from pydantic import ValidationError

from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    encode_varint,
    hash256,
    parse_transaction_bytes,
    serialize_transaction,
)
from jmcore.cofunded_ring import (
    MAX_RING_PAYLOAD_BYTES,
    BackendLimits,
    ChannelPolicy,
    EndpointRole,
    LocalContribution,
    ManifestOutput,
    PolicyBounds,
    PreparedIncomingState,
    PreparedOutgoingState,
    PrivateEdgePlan,
    PrivateParticipant,
    PublicParticipant,
    ReadinessAttestation,
    ReadinessState,
    RingCancelPayload,
    RingEdge,
    RingHelloPayload,
    RingInvitePayload,
    RingKeyPair,
    RingManifest,
    RingOpenPayload,
    RingPlanPayload,
    RingReadyPayload,
    RingReadySetPayload,
    RingUnsignedPayload,
    RingValidationError,
    canonical_json,
    decode_ring_message,
    encode_ring_message,
    manifest_hash,
    payload_hash,
    ring_hash,
    sign_attestation,
    sign_payload,
    validate_hello_for_invite,
    validate_plan_for_invite,
    verify_attestation,
    verify_payload,
)
from jmcore.constants import MAX_MONEY


def _secret(index: int) -> bytes:
    return index.to_bytes(32, "big")


def _ring_key(index: int) -> str:
    return RingKeyPair.from_secret(_secret(index)).public_key


def _node_id(index: int) -> str:
    return bytes(CKey(_secret(index)).pub).hex()


def _policy() -> ChannelPolicy:
    return ChannelPolicy(
        fundee_csv_delay=144,
        fundee_reserve=10_000,
        min_depth=3,
        opener_csv_delay=144,
        opener_reserve=10_000,
    )


def _bounds() -> PolicyBounds:
    return PolicyBounds(
        min_csv_delay=1,
        max_csv_delay=2016,
        min_depth=1,
        max_depth=144,
        min_reserve=0,
        max_reserve=1_000_000,
    )


def _limits(network: str = "regtest", offer_type: str = "tr0absoffer") -> BackendLimits:
    return BackendLimits(
        network=network,
        offer_type=offer_type,
        min_channel_capacity=20_000,
        max_channel_capacity=10_000_000,
        max_push_amount=5_000_000,
        dust_limit=354,
        max_reserve=1_000_000,
        max_commitment_fee=100_000,
        max_pending_channels=8,
    )


def _participant(index: int, *, network: str = "regtest") -> PrivateParticipant:
    return PrivateParticipant(
        participant_key=_ring_key(index),
        node_id=_node_id(index + 20),
        onion_endpoint="a" * 56 + ".onion:9735",
        backend_limits=_limits(network),
    )


def _manifest() -> RingManifest:
    keys = [_ring_key(index) for index in range(1, 5)]
    edges = [
        RingEdge(
            opener_key=keys[index],
            acceptor_key=keys[(index + 1) % 4],
            pending_channel_id=f"{index + 50:064x}",
            capacity=200_000 + index,
            output_index=index + 4,
            script_pubkey="5120" + f"{index + 101:064x}",
            policy=_policy(),
        )
        for index in range(4)
    ]
    outputs = [
        ManifestOutput(
            index=index,
            amount=100_000,
            script_pubkey="5120" + f"{index + 1:064x}",
        )
        for index in range(4)
    ] + [
        ManifestOutput(
            index=edge.output_index,
            amount=edge.capacity,
            script_pubkey=edge.script_pubkey,
        )
        for edge in edges
    ]
    return RingManifest(
        network="regtest",
        round_nonce="11" * 32,
        revision=2,
        unsigned_tx_hash="22" * 32,
        unsigned_txid="33" * 32,
        participant_keys=keys,
        edges=edges,
        equal_output_indices=[0, 1, 2, 3],
        outputs=outputs,
    )


def _psbt(unsigned_tx: bytes, input_count: int, output_count: int) -> bytes:
    return (
        b"psbt\xff"
        + b"\x01\x00"
        + encode_varint(len(unsigned_tx))
        + unsigned_tx
        + b"\x00"
        + b"\x00" * (input_count + output_count)
    )


def _unsigned_payload() -> tuple[RingUnsignedPayload, bytes]:
    manifest = _manifest()
    inputs = [TxInput.from_hex(f"{index + 200:064x}", index) for index in range(4)]
    outputs = [
        TxOutput.from_hex(output.script_pubkey, output.amount) for output in manifest.outputs
    ]
    unsigned_tx = serialize_transaction(2, inputs, outputs, 0)
    manifest = manifest.model_copy(
        update={
            "unsigned_tx_hash": hashlib.sha256(unsigned_tx).hexdigest(),
            "unsigned_txid": hash256(unsigned_tx)[::-1].hex(),
        }
    )
    payload = RingUnsignedPayload(
        round_nonce=manifest.round_nonce,
        revision=manifest.revision,
        signer_key=manifest.participant_keys[0],
        unsigned_tx=unsigned_tx.hex(),
        psbt=base64.b64encode(_psbt(unsigned_tx, len(inputs), len(outputs))).decode(),
        manifest=manifest,
    )
    return payload, unsigned_tx


def _unsigned_payload_data(payload: RingUnsignedPayload, unsigned_tx: bytes) -> dict[str, object]:
    transaction = parse_transaction_bytes(unsigned_tx)
    manifest = payload.manifest.model_copy(
        update={
            "unsigned_tx_hash": hashlib.sha256(unsigned_tx).hexdigest(),
            "unsigned_txid": hash256(unsigned_tx)[::-1].hex(),
        }
    )
    return {
        **payload.model_dump(),
        "unsigned_tx": unsigned_tx.hex(),
        "psbt": base64.b64encode(
            _psbt(unsigned_tx, len(transaction.inputs), len(transaction.outputs))
        ).decode(),
        "manifest": manifest,
    }


def _invite() -> RingInvitePayload:
    return RingInvitePayload(
        round_nonce="00" * 32,
        revision=0,
        signer_key=_ring_key(1),
        network="regtest",
        offer_type="tr0absoffer",
        expiry=1,
        policy_bounds=PolicyBounds(
            min_csv_delay=1,
            max_csv_delay=2,
            min_depth=1,
            max_depth=2,
            min_reserve=0,
            max_reserve=1,
        ),
    )


def test_canonical_bip340_vector() -> None:
    invite = _invite()
    expected_json = (
        b'{"expiry":1,"network":"regtest","offer_type":"tr0absoffer",'
        b'"policy_bounds":{"max_csv_delay":2,"max_depth":2,"max_reserve":1,'
        b'"min_csv_delay":1,"min_depth":1,"min_reserve":0},"revision":0,'
        b'"round_nonce":"' + b"0" * 64 + b'","signer_key":'
        b'"79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",'
        b'"type":"ring_invite","v":1}'
    )
    assert canonical_json(invite, exclude_signature=True) == expected_json
    assert payload_hash(invite).hex() == (
        "8952fea4ff5244ae7de0c8f6f9933bb7fb88fb8d6ab6e4cbafc3dd2811b39e37"
    )

    signed = sign_payload(invite, _secret(1), aux_randomness=b"\x00" * 32)
    assert signed.sig == (
        "6ae1af64178a2573ca81e8df3f459732fdaa827f0ad40c8ff6bcb5c701c976b1"
        "bdf4983e8645f6ffeea818e9db3f343af674e4e2e44ba66ebec5aae3139ee998"
    )
    assert verify_payload(signed)
    assert decode_ring_message(encode_ring_message(signed)) == signed


def test_payload_signature_rejects_mutation_and_wrong_key() -> None:
    signed = sign_payload(_invite(), _secret(1), aux_randomness=b"\x01" * 32)
    assert not verify_payload(signed.model_copy(update={"expiry": 2}))
    with pytest.raises(RingValidationError, match="does not match secret"):
        sign_payload(_invite(), _secret(2))


@pytest.mark.parametrize(
    "message,match",
    [
        ("!ring unknown e30", "unknown ring message type"),
        # ring_status is not part of this revision: participants reconcile chain
        # state from their own backend rather than trusting a peer's report.
        ("!ring ring_status e30", "unknown ring message type"),
        ("!ring ring_invite e30=", "unpadded base64url"),
        ("!ring ring_invite", "invalid ring envelope"),
        ("!ring ring_invite !!!", "unpadded base64url"),
    ],
)
def test_envelope_rejects_unknown_or_malformed_data(message: str, match: str) -> None:
    with pytest.raises(RingValidationError, match=match):
        decode_ring_message(message)


def test_envelope_rejects_duplicate_keys_unknown_version_type_and_extra_fields() -> None:
    signed = sign_payload(_invite(), _secret(1), aux_randomness=b"\x00" * 32)
    body = canonical_json(signed).decode()

    duplicate = body[:-1] + ',"v":1}'
    encoded = base64.urlsafe_b64encode(duplicate.encode()).rstrip(b"=").decode()
    with pytest.raises(RingValidationError, match="duplicate JSON key"):
        decode_ring_message(f"!ring ring_invite {encoded}")

    for replacement, match in (
        ('"v":2', "unknown ring payload version"),
        ('"type":"ring_hello"', "message types differ"),
    ):
        changed = (
            body.replace('"v":1', replacement, 1)
            if "v" in replacement
            else body.replace('"type":"ring_invite"', replacement, 1)
        )
        encoded = base64.urlsafe_b64encode(changed.encode()).rstrip(b"=").decode()
        with pytest.raises(RingValidationError, match=match):
            decode_ring_message(f"!ring ring_invite {encoded}")

    data = json.loads(body)
    data["unexpected"] = True
    encoded = base64.urlsafe_b64encode(canonical_json(data)).rstrip(b"=").decode()
    with pytest.raises(RingValidationError, match="extra_forbidden"):
        decode_ring_message(f"!ring ring_invite {encoded}")

    reversed_data = dict(reversed(list(json.loads(body).items())))
    noncanonical = json.dumps(reversed_data, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(noncanonical.encode()).rstrip(b"=").decode()
    with pytest.raises(RingValidationError, match="not canonical"):
        decode_ring_message(f"!ring ring_invite {encoded}")


def test_envelope_rejects_deep_and_oversized_json_before_model_validation() -> None:
    deep = b"[" * 11 + b"]" * 11
    encoded = base64.urlsafe_b64encode(deep).rstrip(b"=").decode()
    with pytest.raises(RingValidationError, match="maximum depth"):
        decode_ring_message(f"!ring ring_invite {encoded}")

    oversized = "!ring ring_invite " + "a" * (4 * MAX_RING_PAYLOAD_BYTES // 3 + 129)
    with pytest.raises(RingValidationError, match="maximum encoded size"):
        decode_ring_message(oversized)


def test_manifest_validates_cycle_counts_and_public_data() -> None:
    manifest = _manifest()
    assert len(manifest.participant_keys) == len(manifest.edges) == 4
    encoded = canonical_json(manifest)
    assert b"node_id" not in encoded
    assert b"onion" not in encoded
    assert b"residual" not in encoded
    assert b"outgoing" not in encoded
    assert b"incoming" not in encoded

    bad_edges = manifest.edges.copy()
    bad_edges[0] = bad_edges[0].model_copy(update={"acceptor_key": manifest.participant_keys[2]})
    with pytest.raises(ValidationError, match="directed cycle"):
        RingManifest(**{**manifest.model_dump(), "edges": bad_edges})


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("edges", "duplicate-script", "duplicate channel script_pubkey"),
        ("edges", "duplicate-pending", "duplicate pending_channel_id"),
        ("equal_output_indices", [0, 0, 2, 3], "duplicate equal output_index"),
    ],
)
def test_manifest_rejects_duplicate_public_identifiers(
    field: str, value: object, match: str
) -> None:
    manifest = _manifest()
    data = manifest.model_dump()
    if value == "duplicate-script":
        edges = manifest.edges.copy()
        edges[1] = edges[1].model_copy(update={"script_pubkey": edges[0].script_pubkey})
        data[field] = edges
    elif value == "duplicate-pending":
        edges = manifest.edges.copy()
        edges[1] = edges[1].model_copy(update={"pending_channel_id": edges[0].pending_channel_id})
        data[field] = edges
    else:
        data[field] = value
    with pytest.raises(ValidationError, match=match):
        RingManifest(**data)


def test_models_reject_bad_hex_money_and_private_manifest_fields() -> None:
    data = _manifest().model_dump()
    data["round_nonce"] = "AA" * 32
    with pytest.raises(ValidationError, match="lowercase hex"):
        RingManifest(**data)
    output = data["outputs"][0]
    with pytest.raises(ValidationError):
        ManifestOutput(**{**output, "amount": MAX_MONEY + 1})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        PublicParticipant(participant_key=_ring_key(1), node_id=_node_id(20))


def test_private_hello_enforces_network_and_tr0_round_matching() -> None:
    invite = RingInvitePayload(
        round_nonce="10" * 32,
        revision=3,
        signer_key=_ring_key(1),
        network="regtest",
        offer_type="tr0absoffer",
        expiry=100,
        policy_bounds=_bounds(),
    )
    hello = RingHelloPayload(
        round_nonce=invite.round_nonce,
        revision=invite.revision,
        signer_key=_ring_key(2),
        participant=_participant(2),
    )
    validate_hello_for_invite(invite, hello)

    wrong_network = hello.model_copy(update={"participant": _participant(2, network="signet")})
    with pytest.raises(RingValidationError, match="network"):
        validate_hello_for_invite(invite, wrong_network)
    with pytest.raises(ValidationError):
        BackendLimits(**{**_limits().model_dump(), "offer_type": "sw0absoffer"})


def test_taker_signs_plan_for_a_distinct_participant() -> None:
    cycle = [_ring_key(index) for index in range(1, 5)]
    payload = RingPlanPayload(
        round_nonce="10" * 32,
        revision=3,
        signer_key=cycle[0],
        network="regtest",
        offer_type="tr0absoffer",
        cycle_keys=cycle,
        position=1,
        contribution=LocalContribution(
            participant_key=cycle[1], residual=300_000, outgoing=180_000, incoming=120_000
        ),
        predecessor=_participant(1),
        successor=_participant(3),
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
    )
    assert payload.signer_key == cycle[0]
    assert payload.contribution.participant_key == cycle[1]
    invite = RingInvitePayload(
        round_nonce=payload.round_nonce,
        revision=payload.revision,
        signer_key=cycle[0],
        network=payload.network,
        offer_type=payload.offer_type,
        expiry=100,
        policy_bounds=_bounds(),
    )
    validate_plan_for_invite(invite, payload)
    with pytest.raises(RingValidationError, match="inviting taker"):
        validate_plan_for_invite(invite, payload.model_copy(update={"signer_key": cycle[3]}))


def test_ring_open_binds_plan_and_prepared_fundee_does_not_claim_script() -> None:
    opener = _ring_key(2)
    acceptor = _ring_key(3)
    opened = RingOpenPayload(
        round_nonce="10" * 32,
        revision=3,
        signer_key=_ring_key(1),
        plan_hash="20" * 32,
    )
    assert decode_ring_message(encode_ring_message(opened)) == opened
    outgoing = PreparedOutgoingState(
        pending_channel_id="21" * 32,
        opener_key=opener,
        acceptor_key=acceptor,
        script_pubkey="5120" + "22" * 32,
        capacity=200_000,
        push_amount=80_000,
        policy=_policy(),
    )
    incoming = PreparedIncomingState(
        pending_channel_id="23" * 32,
        opener_key=acceptor,
        acceptor_key=opener,
        opener_node_id=_node_id(20),
        capacity=210_000,
        push_amount=90_000,
        policy=_policy(),
    )
    assert "script_pubkey" not in incoming.model_dump()
    assert outgoing.script_pubkey.startswith("5120")


def test_unsigned_payload_binds_canonical_transaction_and_psbt() -> None:
    payload, unsigned_tx = _unsigned_payload()
    assert payload.unsigned_tx == unsigned_tx.hex()

    different_tx = bytearray(unsigned_tx)
    different_tx[-4] = 1
    unrelated_psbt = _psbt(bytes(different_tx), 4, len(payload.manifest.outputs))
    with pytest.raises(ValidationError, match="does not match unsigned_tx"):
        RingUnsignedPayload(
            **{
                **payload.model_dump(),
                "psbt": base64.b64encode(unrelated_psbt).decode(),
            }
        )

    parsed = parse_transaction_bytes(unsigned_tx)
    witnessed = serialize_transaction(
        parsed.version,
        parsed.inputs,
        parsed.outputs,
        parsed.locktime,
        [[b"signature"], [], [], []],
    )
    with pytest.raises(ValidationError, match="no signatures or witness"):
        RingUnsignedPayload(**{**payload.model_dump(), "unsigned_tx": witnessed.hex()})


@pytest.mark.parametrize(
    ("version", "locktime", "sequence"),
    [
        (1, 0, 0xFFFFFFFF),
        (2, 500_000_000, 0xFFFFFFFE),
        (2, 0, 0xFFFFFFFE),
    ],
)
def test_unsigned_payload_rejects_nonfinal_transaction_policy(
    version: int, locktime: int, sequence: int
) -> None:
    payload, unsigned_tx = _unsigned_payload()
    transaction = parse_transaction_bytes(unsigned_tx)
    inputs = [
        TxInput.from_hex(item.txid, item.vout, sequence=sequence) for item in transaction.inputs
    ]
    changed = serialize_transaction(version, inputs, transaction.outputs, locktime)

    with pytest.raises(ValidationError, match="version 2, zero locktime, and final"):
        RingUnsignedPayload(**_unsigned_payload_data(payload, changed))


def test_unsigned_payload_rejects_duplicate_input_outpoints() -> None:
    payload, unsigned_tx = _unsigned_payload()
    transaction = parse_transaction_bytes(unsigned_tx)
    duplicate_inputs = transaction.inputs.copy()
    duplicate_inputs[1] = duplicate_inputs[0]
    changed = serialize_transaction(2, duplicate_inputs, transaction.outputs, 0)

    with pytest.raises(ValidationError, match="duplicate input outpoints"):
        RingUnsignedPayload(**_unsigned_payload_data(payload, changed))


def test_readiness_state_hash_and_endpoint_signature() -> None:
    manifest = _manifest()
    edge = manifest.edges[0]
    state = ReadinessState(
        pending_channel_id=edge.pending_channel_id,
        opener_key=edge.opener_key,
        acceptor_key=edge.acceptor_key,
        script_pubkey=edge.script_pubkey,
        capacity=edge.capacity,
        opener_balance=edge.capacity - 70_000 - 1_000 - 660,
        fundee_balance=70_000,
        push_amount=70_000,
        opener_reserve=10_000,
        fundee_reserve=10_000,
        commitment_fee=1_000,
        commitment_overhead=660,
        policy=edge.policy,
        role=EndpointRole.OPENER,
        signer_key=edge.opener_key,
        round_nonce=manifest.round_nonce,
        revision=manifest.revision,
        manifest_hash=manifest_hash(manifest).hex(),
        unsigned_tx_hash=manifest.unsigned_tx_hash,
        unsigned_txid=manifest.unsigned_txid,
        output_index=edge.output_index,
        state_salt="44" * 32,
    )
    attestation = ReadinessAttestation(
        edge=edge.pending_channel_id,
        manifest_hash=state.manifest_hash,
        revision=state.revision,
        role=state.role,
        round_nonce=state.round_nonce,
        signer_key=state.signer_key,
        state_hash=ring_hash("ready-state", state).hex(),
    )
    signed = sign_attestation(attestation, _secret(1), aux_randomness=b"\x02" * 32)
    assert verify_attestation(signed)
    assert not verify_attestation(
        signed.model_copy(
            update={"attestation": attestation.model_copy(update={"state_hash": "55" * 32})}
        )
    )

    ready = RingReadyPayload(
        round_nonce=state.round_nonce,
        revision=state.revision,
        signer_key=state.signer_key,
        state=state,
        attestation=attestation,
        endpoint_signature=signed.signature,
    )
    with pytest.raises(ValidationError, match="envelope"):
        RingReadyPayload(**{**ready.model_dump(), "revision": state.revision + 1})


def test_readiness_set_rejects_cross_revision_attestation() -> None:
    manifest = _manifest()
    signed = []
    for index, edge in enumerate(manifest.edges):
        for role, signer_key, secret_index in (
            (EndpointRole.OPENER, edge.opener_key, index + 1),
            (EndpointRole.FUNDEE, edge.acceptor_key, (index + 1) % 4 + 1),
        ):
            attestation = ReadinessAttestation(
                edge=edge.pending_channel_id,
                manifest_hash=manifest_hash(manifest).hex(),
                revision=manifest.revision,
                role=role,
                round_nonce=manifest.round_nonce,
                signer_key=signer_key,
                state_hash="55" * 32,
            )
            signed.append(sign_attestation(attestation, _secret(secret_index)))
    RingReadySetPayload(
        round_nonce=manifest.round_nonce,
        revision=manifest.revision,
        signer_key=manifest.participant_keys[0],
        manifest=manifest,
        attestations=signed,
    )

    wrong = signed.copy()
    changed = wrong[0].attestation.model_copy(update={"revision": manifest.revision + 1})
    wrong[0] = sign_attestation(changed, _secret(1))
    with pytest.raises(ValidationError, match="another ring revision"):
        RingReadySetPayload(
            round_nonce=manifest.round_nonce,
            revision=manifest.revision,
            signer_key=manifest.participant_keys[0],
            manifest=manifest,
            attestations=wrong,
        )


def test_cancel_payload_contains_only_canceled_backend_attempts() -> None:
    canceled = "66" * 32
    payload = RingCancelPayload(
        round_nonce="11" * 32,
        revision=4,
        signer_key=_ring_key(1),
        reason_code="peer_timeout",
        canceled_pending_ids=[canceled],
    )
    assert payload.canceled_pending_ids == [canceled]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RingCancelPayload(
            **payload.model_dump(),
            retained_pending_ids=["77" * 32],
        )
