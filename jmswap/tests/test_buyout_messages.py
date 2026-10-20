"""Wire-level tests for the draft JMP-0011 channel-buyout message codec.

These tests pin the payload contract only: canonical bytes in, frozen models
out, and a rejection for every malformed, non-canonical or out-of-bounds
payload. Nothing here exercises protocol state, signing or chain rules.
"""

from __future__ import annotations

import hashlib
import json
import traceback
from typing import Any, TypeVar

import pytest
import rfc8785
from pydantic import ValidationError

from jmswap import bitcoin_escrow as escrow
from jmswap.buyout_messages import (
    BUYOUT_CUSTOM_MESSAGE_TYPE,
    BUYOUT_PROTOCOL_VERSION,
    MAX_CHANNEL_POINTS,
    MAX_CSV_DELAY,
    MAX_JSON_DEPTH,
    MAX_MONEY,
    MAX_PARENT_PREVOUTS,
    MAX_PAYLOAD_BYTES,
    MAX_UINT32,
    MIN_CSV_DELAY,
    MIN_SPLIT_OUTPUT_SATS,
    BuyoutAccept,
    BuyoutCancel,
    BuyoutClose,
    BuyoutInvoice,
    BuyoutMessageError,
    BuyoutNonces,
    BuyoutParent,
    BuyoutParentPartials,
    BuyoutPropose,
    BuyoutReject,
    BuyoutSplitPartial,
    BuyoutStatus,
    BuyoutSweep,
    BuyoutSweepPartial,
    Outpoint,
    Prevout,
    accept_hash,
    decode_buyout_payload,
    encode_buyout_payload,
    object_hash,
    proposal_hash,
)

T = TypeVar("T")

PUBKEY_1 = "031b84c5567b126440995d3ed5aaba0565d71e1834604819ff9c17f5e9d5dd078f"
PUBKEY_2 = "024d4b6cd1361032ca9bd2aeb9d900aa4d45d9ead80ac9423374c451a7254d0766"
PUBKEY_3 = "02531fe6068134503d2723133227c867ac8fa6c83c537e9a44c3c5bdbdcb1fe337"
PUBKEY_4 = "03462779ad4aad39514614751a71085f2f10e1c7a593e4e030efb5b8721ce55b0b"
OFF_CURVE_PUBKEY = "02" + "00" * 31 + "07"

NONCE_1 = PUBKEY_1 + PUBKEY_2
NONCE_2 = PUBKEY_3 + PUBKEY_4

EPOCH_ID = "ab" * 32
HASH_A = "11" * 32
HASH_B = "22" * 32
HASH_C = "33" * 32
P2TR_B = "5120" + "44" * 32
P2TR_C = "5120" + "55" * 32
PARTIAL_1 = "66" * 32
PARTIAL_2 = "77" * 32
TX_HEX = "0200000001" + "00" * 40


def propose(**overrides: Any) -> BuyoutPropose:
    fields: dict[str, Any] = {
        "v": 1,
        "type": "buyout_propose",
        "epoch_id": EPOCH_ID,
        "attempt": 0,
        "network": "regtest",
        "channel_points": [Outpoint(txid=HASH_A, vout=1)],
        "K_B": PUBKEY_1,
        "K_B_claim": PUBKEY_2,
        "split_script_B": P2TR_B,
        "csv_delay": 432,
        "split_fee_rate_sat_vb": 2,
        "split_fee": 500,
        "min_split_output": 1000,
        "sweep_fee_reserve": 8000,
        "buyer_settlement_depth": 6,
        "cltv_limit": 144,
        "sweep_response_blocks": 6,
        "max_buyout_fee": 10_000,
        "max_timeout_compensation": 5_000,
        "freeze_ttl_blocks": 144,
        "expiry": 600,
    }
    fields.update(overrides)
    return BuyoutPropose(**fields)


def accept(**overrides: Any) -> BuyoutAccept:
    fields: dict[str, Any] = {
        "v": 1,
        "type": "buyout_accept",
        "epoch_id": EPOCH_ID,
        "attempt": 0,
        "proposal_hash": HASH_B,
        "K_C": PUBKEY_3,
        "payment_hash": HASH_C,
        "split_script_C": P2TR_C,
        "entitlement_C": 250_000,
        "buyout_fee": 1_000,
        "timeout_compensation": 2_000,
        "min_parent_fee_rate_sat_vb": 3,
        "parent_wait_blocks": 6,
        "settlement_depth": 3,
        "max_freeze_blocks": 144,
        "frozen_state_hash": HASH_A,
    }
    fields.update(overrides)
    return BuyoutAccept(**fields)


def parent(**overrides: Any) -> BuyoutParent:
    fields: dict[str, Any] = {
        "v": 1,
        "type": "buyout_parent",
        "epoch_id": EPOCH_ID,
        "attempt": 1,
        "accept_hash": HASH_B,
        "unsigned_parent_tx": TX_HEX,
        "prevouts": [
            Prevout(value=500_000, script_pubkey=P2TR_B),
            Prevout(value=600_000, script_pubkey=P2TR_C),
        ],
        "escrow_output_index": 2,
        "channel_input_indices": [0],
        "split_nonce_B": NONCE_1,
        "parent_nonces_B": [NONCE_2],
    }
    fields.update(overrides)
    return BuyoutParent(**fields)


REJECT = BuyoutReject(
    v=1,
    type="buyout_reject",
    epoch_id=EPOCH_ID,
    attempt=0,
    proposal_hash=HASH_B,
    reason_code="channel_not_eligible",
)
NONCES = BuyoutNonces(
    v=1,
    type="buyout_nonces",
    epoch_id=EPOCH_ID,
    attempt=1,
    accept_hash=HASH_B,
    parent_hash=HASH_C,
    split_nonce_C=NONCE_1,
    parent_nonces_C=[NONCE_2],
)
SPLIT_PARTIAL = BuyoutSplitPartial(
    v=1,
    type="buyout_split_partial",
    epoch_id=EPOCH_ID,
    attempt=1,
    accept_hash=HASH_B,
    parent_hash=HASH_C,
    split_partial_B=PARTIAL_1,
)
PARENT_PARTIALS = BuyoutParentPartials(
    v=1,
    type="buyout_parent_partials",
    epoch_id=EPOCH_ID,
    attempt=1,
    accept_hash=HASH_B,
    parent_hash=HASH_C,
    split_partial_C=PARTIAL_1,
    parent_partials_C=[PARTIAL_2],
)
INVOICE = BuyoutInvoice(
    v=1,
    type="buyout_invoice",
    epoch_id=EPOCH_ID,
    attempt=1,
    accept_hash=HASH_B,
    txid=HASH_A,
    bolt11="lnbcrt2500u1pvjluezpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfq",
)
SWEEP = BuyoutSweep(
    v=1,
    type="buyout_sweep",
    epoch_id=EPOCH_ID,
    attempt=1,
    accept_hash=HASH_B,
    unsigned_sweep_tx=TX_HEX,
    sweep_nonce_B=NONCE_1,
)
SWEEP_PARTIAL = BuyoutSweepPartial(
    v=1,
    type="buyout_sweep_partial",
    epoch_id=EPOCH_ID,
    attempt=1,
    accept_hash=HASH_B,
    sweep_hash=HASH_C,
    sweep_nonce_C=NONCE_2,
    sweep_partial_C=PARTIAL_2,
)
CANCEL = BuyoutCancel(
    v=1,
    type="buyout_cancel",
    epoch_id=EPOCH_ID,
    attempt=1,
    accept_hash=HASH_B,
    reason_code="round_abandoned",
)
CLOSE = BuyoutClose(
    v=1,
    type="buyout_close",
    epoch_id=EPOCH_ID,
    attempt=1,
    reason_code="max_freeze_blocks_elapsed",
)
STATUS = BuyoutStatus(
    v=1,
    type="buyout_status",
    epoch_id=EPOCH_ID,
    attempt=1,
    accept_hash=HASH_B,
    stage="dormant",
    parent_hash=HASH_C,
    txid=HASH_A,
)

ALL_MESSAGES: list[Any] = [
    propose(),
    accept(),
    REJECT,
    parent(),
    NONCES,
    SPLIT_PARTIAL,
    PARENT_PARTIALS,
    INVOICE,
    SWEEP,
    SWEEP_PARTIAL,
    CANCEL,
    CLOSE,
    STATUS,
]


def decode_as(model: type[T], payload: bytes) -> T:
    """Decode and assert the discriminator routed the payload to ``model``."""
    message = decode_buyout_payload(payload)
    assert isinstance(message, model)
    return message


def payload_of(message: Any, **overrides: Any) -> bytes:
    """Canonical payload for a message with raw JSON-level overrides applied."""
    body: dict[str, Any] = json.loads(encode_buyout_payload(message))
    for key, value in overrides.items():
        if value is ...:
            body.pop(key, None)
        else:
            body[key] = value
    return rfc8785.dumps(body)


class TestRoundTrip:
    @pytest.mark.parametrize("message", ALL_MESSAGES, ids=lambda m: str(m.type))
    def test_round_trip_is_stable(self, message: Any) -> None:
        payload = encode_buyout_payload(message)
        decoded = decode_buyout_payload(payload)
        assert decoded == message
        assert type(decoded) is type(message)
        assert encode_buyout_payload(decoded) == payload

    def test_every_message_type_of_the_table_is_covered(self) -> None:
        assert {message.type for message in ALL_MESSAGES} == {
            "buyout_propose",
            "buyout_accept",
            "buyout_reject",
            "buyout_parent",
            "buyout_nonces",
            "buyout_split_partial",
            "buyout_parent_partials",
            "buyout_invoice",
            "buyout_sweep",
            "buyout_sweep_partial",
            "buyout_cancel",
            "buyout_close",
            "buyout_status",
        }

    def test_transport_constants(self) -> None:
        assert BUYOUT_CUSTOM_MESSAGE_TYPE == 53357
        assert BUYOUT_PROTOCOL_VERSION == 1
        assert MAX_PAYLOAD_BYTES == 65_000

    def test_models_are_frozen(self) -> None:
        with pytest.raises(ValidationError):
            propose().attempt = 2  # type: ignore[misc]


class TestCanonicalEncoding:
    def test_keys_are_sorted_and_separators_minimal(self) -> None:
        payload = encode_buyout_payload(
            BuyoutClose(
                v=1,
                type="buyout_close",
                epoch_id=EPOCH_ID,
                attempt=7,
                reason_code="force_close",
            )
        )
        assert payload == (
            b'{"attempt":7,"epoch_id":"' + EPOCH_ID.encode() + b'",'
            b'"reason_code":"force_close","type":"buyout_close","v":1}'
        )

    def test_absent_optional_field_is_omitted_not_null(self) -> None:
        payload = encode_buyout_payload(
            BuyoutCancel(
                v=1,
                type="buyout_cancel",
                epoch_id=EPOCH_ID,
                attempt=0,
                proposal_hash=HASH_B,
                reason_code="expired",
            )
        )
        assert b"null" not in payload
        assert b"accept_hash" not in payload
        assert decode_as(BuyoutCancel, payload).proposal_hash == HASH_B

    def test_object_hash_matches_the_specified_domain(self) -> None:
        message = propose()
        domain = b"JMP0011/proposal/v1\x00"
        assert domain.endswith(b"\x00")
        expected = hashlib.sha256(domain + encode_buyout_payload(message)).hexdigest()
        assert proposal_hash(message) == expected
        assert object_hash("proposal", message) == expected

    def test_proposal_and_accept_hashes_use_distinct_domains(self) -> None:
        message = accept()
        assert accept_hash(message) == object_hash("accept", message)
        assert accept_hash(message) != object_hash("proposal", message)

    def test_hash_vectors_are_stable(self) -> None:
        assert (
            proposal_hash(propose())
            == "984d40bbd814f2748d57aa9a2d07c852aa6f6b453a0d22e629f51d7f9a8f513c"
        )
        assert (
            accept_hash(accept())
            == "e389484d8b99800a060ee05177edeace37f027d10bca3b3cb4cba56190760f75"
        )


class TestPayloadRejection:
    def test_oversized_payload(self) -> None:
        with pytest.raises(BuyoutMessageError, match="exceeds"):
            decode_buyout_payload(b"{" + b"a" * MAX_PAYLOAD_BYTES)

    def test_empty_payload(self) -> None:
        with pytest.raises(BuyoutMessageError, match="empty"):
            decode_buyout_payload(b"")

    def test_invalid_utf8(self) -> None:
        with pytest.raises(BuyoutMessageError, match="UTF-8"):
            decode_buyout_payload(b'{"v":1,"epoch_id":"\xff\xfe"}')

    def test_not_json(self) -> None:
        with pytest.raises(BuyoutMessageError, match="valid JSON"):
            decode_buyout_payload(b'{"v":1,')

    def test_top_level_array(self) -> None:
        with pytest.raises(BuyoutMessageError, match="JSON object"):
            decode_buyout_payload(b"[1,2]")

    def test_duplicate_keys(self) -> None:
        with pytest.raises(BuyoutMessageError, match="duplicate object key"):
            decode_buyout_payload(b'{"attempt":0,"attempt":1}')

    @pytest.mark.parametrize("literal", [b"1.0", b"1e3", b"0.5"])
    def test_floats(self, literal: bytes) -> None:
        with pytest.raises(BuyoutMessageError, match="non-integer number"):
            decode_buyout_payload(b'{"attempt":' + literal + b"}")

    @pytest.mark.parametrize("literal", [b"NaN", b"Infinity", b"-Infinity"])
    def test_non_finite_numbers(self, literal: bytes) -> None:
        with pytest.raises(BuyoutMessageError, match="non-finite number"):
            decode_buyout_payload(b'{"attempt":' + literal + b"}")

    def test_null_value(self) -> None:
        with pytest.raises(BuyoutMessageError, match="null"):
            decode_buyout_payload(b'{"attempt":null}')

    def test_nested_null_value(self) -> None:
        with pytest.raises(BuyoutMessageError, match="null"):
            decode_buyout_payload(b'{"prevouts":[{"value":null}]}')

    def test_boolean_value(self) -> None:
        with pytest.raises(BuyoutMessageError, match="boolean"):
            decode_buyout_payload(payload_of(propose(), attempt=True))

    @pytest.mark.parametrize(
        "payload",
        [
            b'{"attempt": 0}',
            b'{"v":1,"attempt":0}',
            b'{"attempt":0}\n',
            b'{"attempt":0,"epoch_id":"AB"}',
        ],
        ids=["whitespace", "unsorted", "trailing-newline", "uppercase-hex-is-canonical-json"],
    )
    def test_non_canonical_bytes(self, payload: bytes) -> None:
        with pytest.raises(BuyoutMessageError):
            decode_buyout_payload(payload)

    def test_canonical_rejection_keeps_uppercase_hex_out_of_the_schema(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), epoch_id=EPOCH_ID.upper()))

    def test_deep_nesting_is_rejected_before_parsing(self) -> None:
        payload = b"[" * 10_000 + b"]" * 10_000
        with pytest.raises(BuyoutMessageError, match="nested deeper"):
            decode_buyout_payload(payload)

    def test_nesting_at_the_bound_is_accepted(self) -> None:
        assert MAX_JSON_DEPTH == 3
        assert decode_buyout_payload(encode_buyout_payload(parent())) == parent()

    def test_one_level_too_deep_is_rejected(self) -> None:
        with pytest.raises(BuyoutMessageError, match="nested deeper"):
            decode_buyout_payload(payload_of(parent(), prevouts=[[{"value": 1}]]))

    def test_braces_inside_strings_do_not_count_as_nesting(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), network="{{{{[[[["))

    def test_error_never_echoes_the_payload(self) -> None:
        secret = "deadbeef" * 8 + "ff"
        with pytest.raises(BuyoutMessageError) as raised:
            decode_buyout_payload(payload_of(propose(), epoch_id=secret))
        assert secret not in str(raised.value)
        assert PUBKEY_1 not in str(raised.value)


class TestSchemaRejection:
    def test_unknown_type(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), type="buyout_unknown"))

    def test_missing_type(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), type=...))

    def test_wrong_version(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), v=2))

    def test_extra_field(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), buyout_fee=1))

    def test_missing_required_field(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), csv_delay=...))

    def test_integer_as_string(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), attempt="0"))

    def test_unknown_network(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), network="liquid"))

    @pytest.mark.parametrize("network", ["mainnet", "testnet", "signet", "regtest"])
    def test_known_networks(self, network: str) -> None:
        message = decode_as(BuyoutPropose, payload_of(propose(), network=network))
        assert message.network == network

    @pytest.mark.parametrize(
        "value",
        ["", "ab", HASH_A + "00", EPOCH_ID.upper(), "zz" * 32, "0x" + "11" * 31],
        ids=["empty", "short", "long", "uppercase", "non-hex", "prefixed"],
    )
    def test_bad_hash32(self, value: str) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), epoch_id=value))

    @pytest.mark.parametrize(
        "value",
        [OFF_CURVE_PUBKEY, "04" + PUBKEY_1[2:], PUBKEY_1[:-2], "00" * 33],
        ids=["off-curve", "uncompressed-prefix", "short", "zero"],
    )
    def test_bad_pubkey(self, value: str) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), K_B=value))

    @pytest.mark.parametrize(
        "value",
        [PUBKEY_1 + OFF_CURVE_PUBKEY, OFF_CURVE_PUBKEY + PUBKEY_1, PUBKEY_1, "00" * 66],
        ids=["bad-second-point", "bad-first-point", "half-length", "zero"],
    )
    def test_bad_pubnonce(self, value: str) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(parent(), split_nonce_B=value))

    @pytest.mark.parametrize(
        "value",
        ["0014" + "44" * 20, "5120" + "44" * 31, "", "6120" + "44" * 32],
        ids=["p2wpkh", "short", "empty", "wrong-witness-version"],
    )
    def test_non_p2tr_script(self, value: str) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), split_script_B=value))

    @pytest.mark.parametrize("value", [-1, MAX_MONEY + 1], ids=["negative", "above-max-money"])
    def test_amount_bounds(self, value: int) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(accept(), entitlement_C=value))

    def test_max_money_is_accepted(self) -> None:
        decoded = decode_as(BuyoutAccept, payload_of(accept(), entitlement_C=MAX_MONEY))
        assert decoded.entitlement_C == MAX_MONEY

    @pytest.mark.parametrize("value", [-1, MAX_UINT32 + 1], ids=["negative", "above-uint32"])
    def test_attempt_counter_bounds(self, value: int) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), attempt=value))

    def test_max_uint32_attempt_is_accepted(self) -> None:
        message = decode_as(BuyoutPropose, payload_of(propose(), attempt=MAX_UINT32))
        assert message.attempt == MAX_UINT32

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("csv_delay", 143),
            ("csv_delay", 2017),
            ("min_split_output", 999),
            ("buyer_settlement_depth", 2),
            ("buyer_settlement_depth", 145),
            ("cltv_limit", 0),
            ("sweep_response_blocks", 7),
            ("split_fee_rate_sat_vb", 0),
            ("freeze_ttl_blocks", 0),
        ],
    )
    def test_proposal_parameter_bounds(self, field: str, value: int) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), **{field: value}))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("parent_wait_blocks", 5),
            ("settlement_depth", 0),
            ("settlement_depth", 145),
            ("max_freeze_blocks", 143),
            ("min_parent_fee_rate_sat_vb", 0),
        ],
    )
    def test_acceptance_parameter_bounds(self, field: str, value: int) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(accept(), **{field: value}))

    @pytest.mark.parametrize("value", ["", "Timeout", "a" * 65, "bad code", "bad-code"])
    def test_bad_reason_code(self, value: str) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(CLOSE, reason_code=value))

    @pytest.mark.parametrize("value", ["", "abc", "LNBC1", "lnbc 1"])
    def test_bad_bolt11(self, value: str) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(INVOICE, bolt11=value))

    @pytest.mark.parametrize("value", ["", "0x0200", "0200000001ABCD", "02000000001"])
    def test_bad_transaction_hex(self, value: str) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(parent(), unsigned_parent_tx=value))

    def test_oversized_transaction_hex(self) -> None:
        with pytest.raises(BuyoutMessageError):
            decode_buyout_payload(payload_of(parent(), unsigned_parent_tx="00" * 61_000))


class TestListBounds:
    def test_channel_points_are_bounded(self) -> None:
        points = [{"txid": f"{index:064x}", "vout": 0} for index in range(MAX_CHANNEL_POINTS)]
        assert (
            len(
                decode_as(
                    BuyoutPropose, payload_of(propose(), channel_points=points)
                ).channel_points
            )
            == MAX_CHANNEL_POINTS
        )
        points.append({"txid": f"{MAX_CHANNEL_POINTS:064x}", "vout": 0})
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), channel_points=points))

    def test_channel_points_cannot_be_empty(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), channel_points=[]))

    def test_duplicate_channel_point(self) -> None:
        duplicate = [{"txid": HASH_A, "vout": 1}, {"txid": HASH_A, "vout": 1}]
        with pytest.raises(BuyoutMessageError, match="duplicate_channel_point"):
            decode_buyout_payload(payload_of(propose(), channel_points=duplicate))

    def test_same_txid_different_vout_is_allowed(self) -> None:
        points = [{"txid": HASH_A, "vout": 1}, {"txid": HASH_A, "vout": 2}]
        assert (
            len(
                decode_as(
                    BuyoutPropose, payload_of(propose(), channel_points=points)
                ).channel_points
            )
            == 2
        )

    def test_outpoint_requires_both_fields(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), channel_points=[{"txid": HASH_A}]))

    def test_outpoint_forbids_extra_fields(self) -> None:
        bad = [{"txid": HASH_A, "vout": 1, "amount": 1}]
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(propose(), channel_points=bad))

    def test_prevouts_are_bounded(self) -> None:
        prevouts = [{"value": 1000, "script_pubkey": P2TR_B}] * (MAX_PARENT_PREVOUTS + 1)
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(parent(), prevouts=prevouts))

    def test_prevouts_cannot_be_empty(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(parent(), prevouts=[], channel_input_indices=[0]))


class TestParentConsistency:
    def test_duplicate_channel_input_index(self) -> None:
        with pytest.raises(BuyoutMessageError, match="duplicate_channel_input_index"):
            decode_buyout_payload(
                payload_of(
                    parent(),
                    channel_input_indices=[1, 1],
                    parent_nonces_B=[NONCE_1, NONCE_2],
                )
            )

    def test_index_without_a_prevout(self) -> None:
        with pytest.raises(BuyoutMessageError, match="channel_input_index_without_prevout"):
            decode_buyout_payload(payload_of(parent(), channel_input_indices=[2]))

    def test_last_valid_index_is_accepted(self) -> None:
        decoded = decode_as(BuyoutParent, payload_of(parent(), channel_input_indices=[1]))
        assert decoded.channel_input_indices == [1]

    @pytest.mark.parametrize(
        ("indices", "nonces"),
        [([0], [NONCE_1, NONCE_2]), ([0, 1], [NONCE_1])],
        ids=["too-many-nonces", "too-few-nonces"],
    )
    def test_nonce_count_must_match_index_count(
        self, indices: list[int], nonces: list[str]
    ) -> None:
        with pytest.raises(BuyoutMessageError, match="parent_nonce_count_mismatch"):
            decode_buyout_payload(
                payload_of(parent(), channel_input_indices=indices, parent_nonces_B=nonces)
            )

    def test_matching_counts_are_accepted(self) -> None:
        decoded = decode_as(
            BuyoutParent,
            payload_of(parent(), channel_input_indices=[0, 1], parent_nonces_B=[NONCE_1, NONCE_2]),
        )
        assert len(decoded.parent_nonces_B) == 2

    def test_counterpart_nonce_count_is_not_a_wire_rule(self) -> None:
        """``buyout_nonces`` carries no index list, so its count is a runtime check."""
        message = decode_as(
            BuyoutNonces, payload_of(NONCES, parent_nonces_C=[NONCE_1, NONCE_2, NONCE_1])
        )
        assert len(message.parent_nonces_C) == 3


class TestCancelAndStatus:
    def test_cancel_with_accept_hash(self) -> None:
        message = decode_as(BuyoutCancel, payload_of(CANCEL))
        assert message.accept_hash == HASH_B
        assert message.proposal_hash is None

    def test_cancel_with_both_identifiers(self) -> None:
        with pytest.raises(BuyoutMessageError, match="cancel_identifier_not_exactly_one"):
            decode_buyout_payload(payload_of(CANCEL, proposal_hash=HASH_C))

    def test_cancel_with_neither_identifier(self) -> None:
        with pytest.raises(BuyoutMessageError, match="cancel_identifier_not_exactly_one"):
            decode_buyout_payload(payload_of(CANCEL, accept_hash=...))

    @pytest.mark.parametrize(
        "stage",
        [
            "parent_received",
            "split_signed",
            "parent_signed",
            "dormant",
            "confirmed",
            "invoice_sent",
            "settled",
            "swept",
            "split_broadcast",
            "superseded",
        ],
    )
    def test_accepted_stages(self, stage: str) -> None:
        assert decode_as(BuyoutStatus, payload_of(STATUS, stage=stage)).stage == stage

    @pytest.mark.parametrize("stage", ["accepted", "unknown"])
    def test_invalid_stage_or_unexpected_parent(self, stage: str) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(STATUS, stage=stage))

    @pytest.mark.parametrize("stage", ["accepted", "canceled", "superseded"])
    def test_status_before_parent_receipt(self, stage: str) -> None:
        payload = payload_of(STATUS, stage=stage, parent_hash=..., txid=...)
        message = decode_as(BuyoutStatus, payload)
        assert message.parent_hash is None
        assert encode_buyout_payload(message) == payload

    def test_status_requires_parent_hash_and_txid(self) -> None:
        with pytest.raises(BuyoutMessageError, match="schema"):
            decode_buyout_payload(payload_of(STATUS, txid=...))


def sentinel(label: str) -> str:
    """A value a caller must never find in a log line or a traceback."""
    return f"s{label}nt1nel{label}"


def rendered_failure(payload: bytes) -> str:
    """Decode ``payload`` and render everything a caller could log about it."""
    with pytest.raises(BuyoutMessageError) as raised:
        decode_buyout_payload(payload)
    error = raised.value
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    return rendered + repr(error) + str(error)


class TestRejectionLeaksNothing:
    def test_schema_error_reports_only_bounded_codes(self) -> None:
        with pytest.raises(BuyoutMessageError) as raised:
            decode_buyout_payload(payload_of(propose(), csv_delay=1, network="liquid"))
        message = str(raised.value)
        codes = message.removeprefix("payload does not match the JMP-0011 schema (").rstrip(")")
        assert set(codes.split(",")) == {"greater_than_equal", "literal_error"}

    def test_unknown_field_name_is_not_echoed(self) -> None:
        secret = sentinel("field")
        rendered = rendered_failure(payload_of(propose(), **{secret: 1}))
        assert secret not in rendered
        assert "extra_forbidden" in rendered

    def test_unknown_discriminator_value_is_not_echoed(self) -> None:
        secret = sentinel("type")
        rendered = rendered_failure(payload_of(propose(), type=secret))
        assert secret not in rendered
        assert "union_tag_invalid" in rendered

    def test_invalid_field_value_is_not_echoed(self) -> None:
        secret = sentinel("value")
        rendered = rendered_failure(payload_of(propose(), epoch_id=secret))
        assert secret not in rendered

    def test_malformed_utf8_is_not_echoed(self) -> None:
        secret = sentinel("utf8")
        rendered = rendered_failure(b'{"epoch_id":"' + secret.encode() + b'\xff\xfe"}')
        assert secret not in rendered
        assert "UTF-8" in rendered

    def test_malformed_json_is_not_echoed(self) -> None:
        secret = sentinel("json")
        rendered = rendered_failure(b'{"epoch_id":"' + secret.encode() + b'",')
        assert secret not in rendered
        assert "valid JSON" in rendered

    def test_duplicate_key_rejection_is_not_echoed(self) -> None:
        secret = sentinel("dup")
        raw = b'{"a":"' + secret.encode() + b'","a":"' + secret.encode() + b'"}'
        rendered = rendered_failure(raw)
        assert secret not in rendered

    def test_non_canonical_payload_is_not_echoed(self) -> None:
        secret = sentinel("canon")
        rendered = rendered_failure(b'{"epoch_id": "' + secret.encode() + b'"}')
        assert secret not in rendered

    def test_rejections_carry_no_exception_chain(self) -> None:
        payloads = [
            b'{"epoch_id":"' + sentinel("chain").encode() + b'\xff"}',
            b'{"epoch_id":',
            b'{"attempt":1.5}',
            payload_of(propose(), attempt="0"),
        ]
        for raw in payloads:
            with pytest.raises(BuyoutMessageError) as raised:
                decode_buyout_payload(raw)
            assert raised.value.__cause__ is None
            assert raised.value.__context__ is None or raised.value.__suppress_context__


class TestEncodeGuards:
    def test_encode_rejects_an_oversized_payload(self) -> None:
        huge = parent(unsigned_parent_tx="ab" * 40_000)
        with pytest.raises(BuyoutMessageError, match="exceeds"):
            encode_buyout_payload(huge)

    def test_encode_rejects_a_mutated_list_field(self) -> None:
        """Freezing a model does not freeze the lists it holds."""
        message = propose()
        message.channel_points.append(Outpoint(txid=HASH_A, vout=1))
        with pytest.raises(BuyoutMessageError, match="duplicate_channel_point"):
            encode_buyout_payload(message)

    def test_encode_rejects_a_mutated_nested_list_beyond_its_bound(self) -> None:
        message = propose()
        for index in range(MAX_CHANNEL_POINTS):
            message.channel_points.append(Outpoint(txid=f"{index:064x}", vout=0))
        with pytest.raises(BuyoutMessageError, match="too_long"):
            encode_buyout_payload(message)

    def test_encode_rejects_a_mutation_that_breaks_a_cross_field_rule(self) -> None:
        message = parent()
        message.channel_input_indices.append(1)
        with pytest.raises(BuyoutMessageError, match="parent_nonce_count_mismatch"):
            encode_buyout_payload(message)

    def test_encode_accepts_a_valid_mutation(self) -> None:
        message = propose()
        message.channel_points.append(Outpoint(txid=HASH_C, vout=0))
        assert len(decode_as(BuyoutPropose, encode_buyout_payload(message)).channel_points) == 2

    def test_encode_rejects_a_model_construct_bypass(self) -> None:
        bypassed = BuyoutPropose.model_construct(**propose().__dict__ | {"csv_delay": 1})
        with pytest.raises(BuyoutMessageError, match="greater_than_equal"):
            encode_buyout_payload(bypassed)

    def test_encode_rejects_a_model_copy_bypass(self) -> None:
        bypassed = accept().model_copy(update={"payment_hash": "not-a-hash"})
        with pytest.raises(BuyoutMessageError, match="string_pattern_mismatch"):
            encode_buyout_payload(bypassed)

    def test_encode_rejects_a_bypassed_nested_model(self) -> None:
        message = parent()
        message.prevouts.append(Prevout.model_construct(value=-1, script_pubkey="00"))
        with pytest.raises(BuyoutMessageError, match="greater_than_equal"):
            encode_buyout_payload(message)

    def test_encode_rejects_an_unserializable_bypass(self) -> None:
        bypassed = BuyoutClose.model_construct(**CLOSE.__dict__ | {"reason_code": object()})
        with pytest.raises(BuyoutMessageError, match="not serializable"):
            encode_buyout_payload(bypassed)

    def test_encode_never_leaks_a_bypassed_value(self) -> None:
        secret = sentinel("encode")
        bypassed = accept().model_copy(update={"payment_hash": secret})
        with pytest.raises(BuyoutMessageError) as raised:
            encode_buyout_payload(bypassed)
        error = raised.value
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        assert secret not in rendered + str(error)


class TestSharedInvariants:
    def test_split_bounds_come_from_the_escrow_module(self) -> None:
        """The wire schema must not drift from the escrow's own split bounds."""
        assert (MIN_CSV_DELAY, MAX_CSV_DELAY) == (escrow.MIN_CSV_DELAY, escrow.MAX_CSV_DELAY)
        assert MIN_SPLIT_OUTPUT_SATS == escrow.MIN_SPLIT_OUTPUT_SATS
        decode_buyout_payload(payload_of(propose(), csv_delay=escrow.MIN_CSV_DELAY))
        decode_buyout_payload(payload_of(propose(), csv_delay=escrow.MAX_CSV_DELAY))
        with pytest.raises(BuyoutMessageError, match="less_than_equal"):
            decode_buyout_payload(payload_of(propose(), csv_delay=escrow.MAX_CSV_DELAY + 1))
