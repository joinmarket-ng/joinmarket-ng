"""Wire models and codec for the draft JMP-0011 private channel-buyout messages.

JMP-0011 (`channel_buyout_v1`) is a **draft**. This module implements only its
Transport/Encoding and Messages sections: the byte-level payload codec and the
strict schema of the thirteen message types exchanged between the buyer ``B``
and its channel counterparty ``C``. Those payloads never reach the CoinJoin
round; they travel as BOLT 1 custom messages of type
:data:`BUYOUT_CUSTOM_MESSAGE_TYPE` over the authenticated BOLT 8 connection
between the two channel endpoints, so the counterparty stays absent from the
CoinJoin and the round peers observe nothing defined here.

Nothing in this module is runtime: there is no state machine, no signing, no
Lightning adapter, no fee policy and no defaults for economic parameters. Cross
message rules (an ``accept_hash`` that matches a known attempt, a nonce count
that matches the counterpart's, a stage transition that is legal, feasibility of
the accounting inequalities, chain observation) require attempt state and belong
to the runtime layer. Only constraints that are checkable from a single payload
are enforced here.

Bounded interpretations of the draft
------------------------------------

The draft leaves several ranges open. A wire codec cannot leave them open, so
each one below is a deliberate, documented interpretation of this module and not
a settled part of the specification. Widening any of them is a wire-compatible
change; narrowing is not.

* ``channel_points``, ``channel_input_indices``, ``parent_nonces_*`` and
  ``parent_partials_C`` hold 1 through :data:`MAX_CHANNEL_POINTS` entries. The
  draft bounds neither, only the payload size.
* ``prevouts`` holds 1 through :data:`MAX_PARENT_PREVOUTS` entries, which keeps
  the parent's prevout list inside the payload budget.
* Outpoints are structured objects ``{"txid": hex32, "vout": uint32}``. The
  draft writes ``txid:vout`` prose; a JSON string form would need its own
  canonicality rules, so the structured form is used.
* ``script_pubkey``, ``split_script_B`` and ``split_script_C`` must be exactly a
  P2TR script (``5120`` followed by a 32-byte x-only key). The draft caps script
  length at 68 hex characters and says "P2TR only".
* ``buyer_settlement_depth`` is 3 through 144 and ``cltv_limit`` is 1 through
  :data:`MAX_CSV_DELAY`; the draft states only the lower bound of the first and
  the settlement inequality that constrains the second.
* ``sweep_response_blocks`` is 0 through 6; the draft states only the upper
  bound. ``split_fee_rate_sat_vb`` and ``min_parent_fee_rate_sat_vb`` are at
  least 1 because a zero fee rate cannot satisfy the minimum relay rate.
* ``expiry`` and ``freeze_ttl_blocks`` are bounded as ordinary counters; the
  draft does not say whether ``expiry`` is an absolute time or a duration, and
  this module does not decide.
* ``bolt11`` is a lowercase ``ln``-prefixed bech32 string of at most
  :data:`MAX_BOLT11_CHARS` characters. The draft gives no bound. The invoice is
  not decoded here; amount, payment hash and expiry are runtime checks.
* :class:`BuyoutStatus` omits parent identifiers before a parent exists. The
  identifiers must occur together; states after parent receipt require them.
  Canceled or superseded attempts may have reached parent receipt or may not.
* :class:`BuyoutCancel` carries exactly one of ``accept_hash`` and
  ``proposal_hash``. The draft writes "``accept_hash`` or ``proposal_hash``";
  neither, or both, is rejected.
* Booleans are rejected everywhere in the payload, since no field of the message
  table is a boolean.

Codec
-----

:func:`decode_buyout_payload` rejects, before any nested validation, a payload
that is oversized, not valid UTF-8, nested deeper than :data:`MAX_JSON_DEPTH`,
contains duplicate object keys, a float, a non-finite constant, ``null``, a
boolean, or bytes that are not already the RFC 8785 canonical form of their own
parsed value. :func:`encode_buyout_payload` emits that same canonical form with
no null fields and decodes it again before returning it, so a mutated list
inside a frozen model or a ``model_construct`` bypass cannot leave this module
as bytes.

Every rejection raises :class:`BuyoutMessageError` carrying a fixed reason and,
for schema failures, a bounded set of pydantic error codes. Field locations,
validator messages and inputs are withheld because an attacker chooses the key
names, the discriminator value and the field values, and the exception chain is
suppressed because a ``UnicodeDecodeError``, a ``JSONDecodeError`` or a
canonicalizer error would otherwise carry payload bytes into a traceback. A
caller can therefore log any failure of this module without logging the message.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal, Union

import rfc8785
from bitcointx.core.key import CPubKey
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

# The split's relative-lock and dust bounds are invariants of the escrow itself,
# so the wire schema reuses them instead of restating them.
from jmswap.bitcoin_escrow import MAX_CSV_DELAY, MIN_CSV_DELAY, MIN_SPLIT_OUTPUT_SATS

# BOLT 1 custom message type carrying every payload defined here.
BUYOUT_CUSTOM_MESSAGE_TYPE = 53357

# Value of the mandatory ``v`` field.
BUYOUT_PROTOCOL_VERSION = 1

# Transport bounds fixed by the draft.
MAX_PAYLOAD_BYTES = 65_000
MAX_MONEY = 2_100_000_000_000_000
MAX_UINT32 = 4_294_967_295
MAX_TX_HEX_CHARS = 120_000
MAX_SETTLEMENT_DEPTH = 144
MIN_PARENT_WAIT_BLOCKS = 6
MIN_MAX_FREEZE_BLOCKS = 144
MAX_SWEEP_RESPONSE_BLOCKS = 6

# Bounds this module fixes for the draft; see the module docstring.
MAX_CHANNEL_POINTS = 32
MAX_PARENT_PREVOUTS = 252
MAX_BOLT11_CHARS = 2_048
MAX_JSON_DEPTH = 3

# A schema rejection reports at most this many pydantic error codes.
MAX_REPORTED_ERROR_CODES = 8
_ERROR_CODE_SHAPE = re.compile(r"[a-z0-9_]{1,48}")

# Domain separation for object hashes: SHA256("JMP0011/" + kind + "/v1\0" || json).
HASH_DOMAIN_PREFIX = "JMP0011/"
HASH_DOMAIN_SUFFIX = "/v1\u0000"


class BuyoutMessageError(Exception):
    """A payload is not a well-formed JMP-0011 message.

    The message text describes the failure and, for schema failures, the field
    locations that failed. It never contains payload content.
    """


def _validate_compressed_pubkey(value: str) -> str:
    if not CPubKey(bytes.fromhex(value)).is_fullyvalid():
        raise PydanticCustomError(
            "not_a_secp256k1_point", "value is not a valid compressed secp256k1 point"
        )
    return value


def _validate_pubnonce(value: str) -> str:
    raw = bytes.fromhex(value)
    for offset in (0, 33):
        if not CPubKey(raw[offset : offset + 33]).is_fullyvalid():
            raise PydanticCustomError(
                "not_a_musig2_pubnonce", "value is not two valid compressed secp256k1 points"
            )
    return value


Hex32 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
"""A 32-byte hash, preimage or ID as 64 lowercase hex characters."""

CompressedPubKey = Annotated[
    str,
    Field(pattern=r"^0[23][0-9a-f]{64}$"),
    AfterValidator(_validate_compressed_pubkey),
]
"""A compressed secp256k1 public key, parsed natively to reject off-curve keys."""

PubNonce = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{132}$"),
    AfterValidator(_validate_pubnonce),
]
"""A MuSig2 public nonce: two compressed points, both parsed natively."""

PartialSig = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
"""A MuSig2 partial signature as 64 lowercase hex characters (scalar range is a
runtime concern of the MuSig2 session, not of the wire codec)."""

P2trScript = Annotated[str, Field(pattern=r"^5120[0-9a-f]{64}$")]
"""A P2TR scriptPubKey, the only script shape the draft allows on the wire."""

TxHex = Annotated[
    str, Field(pattern=r"^(?:[0-9a-f]{2})+$", min_length=2, max_length=MAX_TX_HEX_CHARS)
]
"""A consensus-serialized transaction as lowercase hex of even length. The bytes
are not parsed here; transaction structure is a runtime check."""

ReasonCode = Annotated[str, Field(pattern=r"^[a-z0-9_]{1,64}$")]
Bolt11 = Annotated[str, Field(pattern=r"^ln[0-9a-z]+$", max_length=MAX_BOLT11_CHARS)]

Satoshis = Annotated[int, Field(ge=0, le=MAX_MONEY)]
Uint32 = Annotated[int, Field(ge=0, le=MAX_UINT32)]
FeeRateSatVb = Annotated[int, Field(ge=1, le=MAX_UINT32)]


class _StrictModel(BaseModel):
    """Frozen, strict, closed model: no coercion, no extra keys, no mutation."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Outpoint(_StrictModel):
    """A funding outpoint of an eligible channel."""

    txid: Hex32
    vout: Uint32


class Prevout(_StrictModel):
    """The spent output of one parent input, in parent input order."""

    value: Satoshis
    script_pubkey: P2trScript


class _BuyoutPayload(_StrictModel):
    """Fields every JMP-0011 payload carries."""

    v: Literal[1]
    epoch_id: Hex32
    attempt: Uint32


class BuyoutPropose(_BuyoutPayload):
    """``B`` to ``C``: the terms of one buyout attempt."""

    type: Literal["buyout_propose"]
    network: Literal["mainnet", "testnet", "signet", "regtest"]
    channel_points: Annotated[list[Outpoint], Field(min_length=1, max_length=MAX_CHANNEL_POINTS)]
    K_B: CompressedPubKey  # noqa: N815
    K_B_claim: CompressedPubKey  # noqa: N815
    split_script_B: P2trScript  # noqa: N815
    csv_delay: Annotated[int, Field(ge=MIN_CSV_DELAY, le=MAX_CSV_DELAY)]
    split_fee_rate_sat_vb: FeeRateSatVb
    split_fee: Satoshis
    min_split_output: Annotated[int, Field(ge=MIN_SPLIT_OUTPUT_SATS, le=MAX_MONEY)]
    sweep_fee_reserve: Satoshis
    buyer_settlement_depth: Annotated[int, Field(ge=3, le=MAX_SETTLEMENT_DEPTH)]
    cltv_limit: Annotated[int, Field(ge=1, le=MAX_CSV_DELAY)]
    sweep_response_blocks: Annotated[int, Field(ge=0, le=MAX_SWEEP_RESPONSE_BLOCKS)]
    max_buyout_fee: Satoshis
    max_timeout_compensation: Satoshis
    freeze_ttl_blocks: Annotated[int, Field(ge=1, le=MAX_UINT32)]
    expiry: Uint32

    @model_validator(mode="after")
    def _distinct_channel_points(self) -> BuyoutPropose:
        seen = {(point.txid, point.vout) for point in self.channel_points}
        if len(seen) != len(self.channel_points):
            raise PydanticCustomError(
                "duplicate_channel_point", "channel_points contains a duplicate outpoint"
            )
        return self


class BuyoutAccept(_BuyoutPayload):
    """``C`` to ``B``: acceptance with fresh escrow material and frozen terms."""

    type: Literal["buyout_accept"]
    proposal_hash: Hex32
    K_C: CompressedPubKey  # noqa: N815
    payment_hash: Hex32
    split_script_C: P2trScript  # noqa: N815
    entitlement_C: Satoshis  # noqa: N815
    buyout_fee: Satoshis
    timeout_compensation: Satoshis
    min_parent_fee_rate_sat_vb: FeeRateSatVb
    parent_wait_blocks: Annotated[int, Field(ge=MIN_PARENT_WAIT_BLOCKS, le=MAX_UINT32)]
    settlement_depth: Annotated[int, Field(ge=1, le=MAX_SETTLEMENT_DEPTH)]
    max_freeze_blocks: Annotated[int, Field(ge=MIN_MAX_FREEZE_BLOCKS, le=MAX_UINT32)]
    frozen_state_hash: Hex32


class BuyoutReject(_BuyoutPayload):
    """``C`` to ``B``: the proposal is refused."""

    type: Literal["buyout_reject"]
    proposal_hash: Hex32
    reason_code: ReasonCode


class BuyoutParent(_BuyoutPayload):
    """``B`` to ``C``: the exact unsigned CoinJoin plus ``B``'s nonces."""

    type: Literal["buyout_parent"]
    accept_hash: Hex32
    unsigned_parent_tx: TxHex
    prevouts: Annotated[list[Prevout], Field(min_length=1, max_length=MAX_PARENT_PREVOUTS)]
    escrow_output_index: Uint32
    channel_input_indices: Annotated[
        list[Uint32], Field(min_length=1, max_length=MAX_CHANNEL_POINTS)
    ]
    split_nonce_B: PubNonce  # noqa: N815
    parent_nonces_B: Annotated[  # noqa: N815
        list[PubNonce], Field(min_length=1, max_length=MAX_CHANNEL_POINTS)
    ]

    @model_validator(mode="after")
    def _indices_match_prevouts_and_nonces(self) -> BuyoutParent:
        if len(set(self.channel_input_indices)) != len(self.channel_input_indices):
            raise PydanticCustomError(
                "duplicate_channel_input_index",
                "channel_input_indices contains a duplicate index",
            )
        if any(index >= len(self.prevouts) for index in self.channel_input_indices):
            raise PydanticCustomError(
                "channel_input_index_without_prevout",
                "channel_input_indices refers to an input without a prevout",
            )
        if len(self.parent_nonces_B) != len(self.channel_input_indices):
            raise PydanticCustomError(
                "parent_nonce_count_mismatch",
                "parent_nonces_B length does not match channel_input_indices",
            )
        return self


class BuyoutNonces(_BuyoutPayload):
    """``C`` to ``B``: the counterparty's nonces for the accepted parent.

    The nonce count is checked against ``C``'s own record of
    ``channel_input_indices`` by the runtime, which is the only holder of the
    attempt state; the payload alone cannot carry that relation.
    """

    type: Literal["buyout_nonces"]
    accept_hash: Hex32
    parent_hash: Hex32
    split_nonce_C: PubNonce  # noqa: N815
    parent_nonces_C: Annotated[  # noqa: N815
        list[PubNonce], Field(min_length=1, max_length=MAX_CHANNEL_POINTS)
    ]


class BuyoutSplitPartial(_BuyoutPayload):
    """``B`` to ``C``: the buyer's partial signature over the split."""

    type: Literal["buyout_split_partial"]
    accept_hash: Hex32
    parent_hash: Hex32
    split_partial_B: PartialSig  # noqa: N815


class BuyoutParentPartials(_BuyoutPayload):
    """``C`` to ``B``: the counterparty's point of no return for the epoch.

    As for :class:`BuyoutNonces`, the runtime checks ``parent_partials_C``
    against the attempt's channel input count.
    """

    type: Literal["buyout_parent_partials"]
    accept_hash: Hex32
    parent_hash: Hex32
    split_partial_C: PartialSig  # noqa: N815
    parent_partials_C: Annotated[  # noqa: N815
        list[PartialSig], Field(min_length=1, max_length=MAX_CHANNEL_POINTS)
    ]


class BuyoutInvoice(_BuyoutPayload):
    """``C`` to ``B``: the BOLT 11 invoice for the claim."""

    type: Literal["buyout_invoice"]
    accept_hash: Hex32
    txid: Hex32
    bolt11: Bolt11


class BuyoutSweep(_BuyoutPayload):
    """``B`` to ``C``: a cooperative key-path sweep of the settled escrow."""

    type: Literal["buyout_sweep"]
    accept_hash: Hex32
    unsigned_sweep_tx: TxHex
    sweep_nonce_B: PubNonce  # noqa: N815


class BuyoutSweepPartial(_BuyoutPayload):
    """``C`` to ``B``: the counterparty's half of the cooperative sweep."""

    type: Literal["buyout_sweep_partial"]
    accept_hash: Hex32
    sweep_hash: Hex32
    sweep_nonce_C: PubNonce  # noqa: N815
    sweep_partial_C: PartialSig  # noqa: N815


class BuyoutCancel(_BuyoutPayload):
    """Either direction: abandon an attempt while cancellation is still allowed.

    Exactly one of ``accept_hash`` and ``proposal_hash`` identifies the attempt.
    Whether cancellation is still permitted (the draft forbids it at or after
    ``parent_signed``) is a runtime decision.
    """

    type: Literal["buyout_cancel"]
    accept_hash: Hex32 | None = None
    proposal_hash: Hex32 | None = None
    reason_code: ReasonCode

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> BuyoutCancel:
        if (self.accept_hash is None) == (self.proposal_hash is None):
            raise PydanticCustomError(
                "cancel_identifier_not_exactly_one",
                "exactly one of accept_hash and proposal_hash is required",
            )
        return self


class BuyoutClose(_BuyoutPayload):
    """Either direction: end the epoch by a cooperative close."""

    type: Literal["buyout_close"]
    reason_code: ReasonCode


class BuyoutStatus(_BuyoutPayload):
    """Either direction: resynchronize message state after a restart.

    Parent identifiers are absent until parent receipt. Peer claims about chain
    state are not evidence, so a receiver
    treats this payload as message state only.
    """

    type: Literal["buyout_status"]
    accept_hash: Hex32
    stage: Literal[
        "accepted",
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
        "canceled",
    ]
    parent_hash: Hex32 | None = None
    txid: Hex32 | None = None

    @model_validator(mode="after")
    def validate_parent_identifiers(self) -> BuyoutStatus:
        if (self.parent_hash is None) != (self.txid is None):
            raise PydanticCustomError("status_parent_pair_required", "parent pair required")
        has_parent = self.parent_hash is not None
        if self.stage == "accepted" and has_parent:
            raise PydanticCustomError("status_parent_not_expected", "parent not expected")
        if self.stage not in {"accepted", "canceled", "superseded"} and not has_parent:
            raise PydanticCustomError("status_parent_required", "parent required")
        return self


BuyoutMessage = Annotated[
    Union[  # noqa: UP007
        BuyoutPropose,
        BuyoutAccept,
        BuyoutReject,
        BuyoutParent,
        BuyoutNonces,
        BuyoutSplitPartial,
        BuyoutParentPartials,
        BuyoutInvoice,
        BuyoutSweep,
        BuyoutSweepPartial,
        BuyoutCancel,
        BuyoutClose,
        BuyoutStatus,
    ],
    Field(discriminator="type"),
]

_MESSAGE_ADAPTER: TypeAdapter[BuyoutMessage] = TypeAdapter(BuyoutMessage)


def _reject_float(_value: str) -> float:
    raise BuyoutMessageError("payload contains a non-integer number")


def _reject_constant(_value: str) -> Any:
    raise BuyoutMessageError("payload contains a non-finite number")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BuyoutMessageError("payload contains a duplicate object key")
        result[key] = value
    return result


def _check_depth(text: str) -> None:
    """Bound nesting lexically, before any recursive parse allocates a tree."""
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise BuyoutMessageError(f"payload is nested deeper than {MAX_JSON_DEPTH} levels")
        elif char in "}]":
            depth -= 1


def _check_values(node: Any) -> None:
    """Reject ``null`` and booleans anywhere in an already depth-bounded tree."""
    if node is None:
        raise BuyoutMessageError("payload contains null")
    if isinstance(node, bool):
        raise BuyoutMessageError("payload contains a boolean")
    if isinstance(node, dict):
        for value in node.values():
            _check_values(value)
    elif isinstance(node, list):
        for value in node:
            _check_values(value)


def _schema_error_codes(error: ValidationError) -> str:
    """Reduce a validation failure to pydantic error codes.

    Field locations, messages, contexts and inputs are all attacker-controlled
    in part: an unknown key becomes a location, a bogus ``type`` becomes a
    discriminator message, and a custom validator message can quote its value.
    Only ``detail["type"]`` is taken, it is checked against a conservative
    identifier shape, and the result is deduplicated, sorted and truncated so a
    rejected payload cannot steer the size or the content of what a caller logs.
    """
    codes = set()
    for detail in error.errors(include_url=False, include_input=False, include_context=False):
        code = detail["type"]
        codes.add(code if _ERROR_CODE_SHAPE.fullmatch(code) else "invalid")
    return ",".join(sorted(codes)[:MAX_REPORTED_ERROR_CODES]) or "invalid"


def decode_buyout_payload(payload: bytes) -> BuyoutMessage:
    """Parse one custom-message payload into its :data:`BuyoutMessage` model.

    Raises :class:`BuyoutMessageError` for every rejection. The raised error
    never contains payload content, and every rejection suppresses its exception
    chain (``from None``): a ``UnicodeDecodeError``, a ``JSONDecodeError`` or a
    canonicalizer error would otherwise carry payload bytes into a caller's
    traceback.
    """
    if not isinstance(payload, bytes | bytearray):
        raise BuyoutMessageError("payload is not bytes") from None
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise BuyoutMessageError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes") from None
    if not payload:
        raise BuyoutMessageError("payload is empty") from None
    raw = bytes(payload)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BuyoutMessageError("payload is not valid UTF-8") from None

    _check_depth(text)

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except BuyoutMessageError as exc:
        raise BuyoutMessageError(str(exc)) from None
    except ValueError:
        raise BuyoutMessageError("payload is not valid JSON") from None

    if not isinstance(parsed, dict):
        raise BuyoutMessageError("payload is not a JSON object") from None
    _check_values(parsed)

    try:
        canonical = rfc8785.dumps(parsed)
    except Exception:  # noqa: BLE001 - rfc8785 raises its own error tree
        raise BuyoutMessageError("payload is not canonicalizable") from None
    if canonical != raw:
        raise BuyoutMessageError("payload is not RFC 8785 canonical JSON") from None

    try:
        return _MESSAGE_ADAPTER.validate_python(parsed)
    except ValidationError as exc:
        codes = _schema_error_codes(exc)
        raise BuyoutMessageError(f"payload does not match the JMP-0011 schema ({codes})") from None


def encode_buyout_payload(message: BuyoutMessage) -> bytes:
    """Serialize a message model to its canonical payload, omitting null fields.

    The result is decoded again before it is returned. Freezing a model stops
    attribute assignment but not mutation of a list it holds, and
    ``model_construct`` skips validation entirely, so an in-memory model is not
    by itself evidence that its bytes are a valid JMP-0011 message. Re-decoding
    makes the encoder emit only payloads its own parser accepts.
    """
    try:
        body = _MESSAGE_ADAPTER.dump_python(
            message, mode="json", exclude_none=True, warnings="error"
        )
        payload: bytes = rfc8785.dumps(body)
    except BuyoutMessageError:
        raise
    except Exception:  # noqa: BLE001 - a bypassed model can hold anything
        raise BuyoutMessageError("message is not serializable") from None
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise BuyoutMessageError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes") from None
    decode_buyout_payload(payload)
    return payload


def object_hash(kind: str, message: BuyoutMessage) -> str:
    """``H(kind, object) = SHA256(UTF8(domain) || canonical_json(object))``."""
    domain = (HASH_DOMAIN_PREFIX + kind + HASH_DOMAIN_SUFFIX).encode("utf-8")
    return hashlib.sha256(domain + encode_buyout_payload(message)).hexdigest()


def proposal_hash(message: BuyoutPropose) -> str:
    """``proposal_hash = H("proposal", buyout_propose)``."""
    return object_hash("proposal", message)


def accept_hash(message: BuyoutAccept) -> str:
    """``accept_hash = H("accept", buyout_accept)``."""
    return object_hash("accept", message)


__all__ = (
    "BUYOUT_CUSTOM_MESSAGE_TYPE",
    "BUYOUT_PROTOCOL_VERSION",
    "MAX_BOLT11_CHARS",
    "MAX_CHANNEL_POINTS",
    "MAX_CSV_DELAY",
    "MAX_JSON_DEPTH",
    "MAX_MONEY",
    "MAX_PARENT_PREVOUTS",
    "MAX_PAYLOAD_BYTES",
    "MAX_TX_HEX_CHARS",
    "MAX_UINT32",
    "BuyoutAccept",
    "BuyoutCancel",
    "BuyoutClose",
    "BuyoutInvoice",
    "BuyoutMessage",
    "BuyoutMessageError",
    "BuyoutNonces",
    "BuyoutParent",
    "BuyoutParentPartials",
    "BuyoutPropose",
    "BuyoutReject",
    "BuyoutSplitPartial",
    "BuyoutStatus",
    "BuyoutSweep",
    "BuyoutSweepPartial",
    "Outpoint",
    "Prevout",
    "accept_hash",
    "decode_buyout_payload",
    "encode_buyout_payload",
    "object_hash",
    "proposal_hash",
)
