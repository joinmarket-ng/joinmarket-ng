"""Validation of one agreed buyout attempt and of the parent that carries it.

Two payloads (a ``buyout_propose`` and the ``buyout_accept`` that answers it) plus
the frozen channel snapshots are individually well formed long before they agree
with each other. :class:`BuyoutTerms` is the object that decides they do: it
binds the two payloads to the same attempt, re-derives the escrow they describe,
checks every economic bound one side could have chosen against the other side's
limit, and proves that both endpoints are looking at the same frozen channels.
:func:`validate_parent` then decides whether a concrete unsigned CoinJoin is the
parent those terms allow to be signed.

Everything here is pure: no state machine, no persistence, no Lightning call, no
signing, no fee policy of its own. Every threshold that could be a policy choice
(fee rates, reserves, minimum outputs, settlement depths) comes from the two
payloads or from an explicit argument; the module only checks that the agreed
numbers are consistent and safe for the side running the check. Both endpoints
run exactly the same checks on mirrored inputs and must reach the same verdict,
which is why nothing here depends on who is local.

Role normalization
------------------

A frozen channel is reported from the point of view of the node that reported
it: ``local`` is the reporting node, which is the buyer on ``B``'s side and the
counterparty on ``C``'s side. ``buyer_is_local`` is the only place that
asymmetry enters. :func:`frozen_state_hash` normalizes the snapshot to buyer and
counterparty roles before hashing, so ``B`` and ``C`` hash mirrored snapshots to
the same digest and ``C`` can commit to the state it froze in its acceptance.

The hash covers the outpoint, the capacity, the two role-normalized claims, the
two role-normalized funding keys and the funding script. It deliberately does
not cover the peer node id: that identity is the one thing the buyout protocol
exists to keep out of the CoinJoin round, and a commitment to it would let
anyone who guesses a candidate node id confirm the guess. The claims are the
cooperative-close balances the channel backend computed for the snapshot, not
raw commitment-transaction data, so the digest commits to the economically
relevant state rather than to a serialization the backend does not expose.

Errors
------

Every rejection raises :class:`ProtocolError` with a fixed message naming the
rule that failed. Amounts, keys, hashes, outpoints and transaction bytes are
never quoted, and the exception chain of an underlying primitive error is
suppressed, so a caller can log any failure of this module without logging the
private terms of an attempt.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import rfc8785
from jmcore.bitcoin import (
    ParsedTransaction,
    encode_varint,
    hash256,
    parse_transaction_bytes,
    serialize_transaction,
)
from jmcore.taproot import TaprootTx, TxIn, TxOut

from jmswap.bitcoin_escrow import (
    SWEEP_SEQUENCE,
    BuyoutEscrow,
    ChannelBuyoutEscrowError,
    EscrowOutpoint,
    UnsignedKeyPathSpend,
    build_split,
)
from jmswap.buyout_messages import (
    HASH_DOMAIN_PREFIX,
    HASH_DOMAIN_SUFFIX,
    MAX_MONEY,
    MAX_TX_HEX_CHARS,
    BuyoutAccept,
    BuyoutMessageError,
    BuyoutPropose,
    Prevout,
    proposal_hash,
)
from jmswap.lnd_escrow import FrozenChannel

# The split must stay unspendable until the buyer has had its whole settlement
# window plus the longest HTLC it may have to resolve, and then some: this
# margin absorbs the blocks lost to a reorg, to a stalled parent and to the
# claim's own confirmation.
SPLIT_SAFETY_MARGIN_BLOCKS = 36

# A parent may commit to a height-based nLockTime no further ahead than this, so
# that a parent agreed now is broadcastable now rather than at a future height.
MAX_LOCKTIME_LOOKAHEAD_BLOCKS = 12

# Height-based nLockTime range (BIP65): at or above this, nLockTime is a time.
LOCKTIME_THRESHOLD = 500_000_000

# The parent is pre-signature exchange, so every input signals BIP125
# replaceability; a parent with no nLockTime may leave nSequence final instead.
PARENT_SEQUENCE = 0xFFFFFFFE
PARENT_SEQUENCE_FINAL = 0xFFFFFFFF

# The reserve the buyer keeps for its unilateral claim must pay for that claim at
# a fee rate high enough to confirm inside the settlement window even if the
# market moved after the terms were agreed.
CLAIM_SWEEP_RESERVE_RATE_SAT_VB = 50

# A SIGHASH_DEFAULT Schnorr signature, the whole witness of a key-path spend.
TAPROOT_SIGNATURE_SIZE = 64

_P2TR_SCRIPT_LENGTH = 34
_MAX_PARENT_BYTES = MAX_TX_HEX_CHARS // 2
_FROZEN_STATE_DOMAIN = (HASH_DOMAIN_PREFIX + "frozen-state" + HASH_DOMAIN_SUFFIX).encode("utf-8")


class ProtocolError(Exception):
    """Agreed terms, frozen channels or a parent transaction violate JMP-0011.

    The message names the rule that failed and never carries a value from the
    attempt.
    """


def _error(message: str) -> ProtocolError:
    return ProtocolError(message)


def _require_int(value: int, name: str) -> int:
    """Reject anything that is not exactly an ``int`` (``bool`` included)."""
    if type(value) is not int or value < 0:
        raise _error(f"{name} must be a non-negative integer")
    return value


def _is_p2tr(script: bytes) -> bool:
    return len(script) == _P2TR_SCRIPT_LENGTH and script[0] == 0x51 and script[1] == 0x20


def _witness_section_size(witnesses: Sequence[Sequence[bytes]]) -> int:
    """Size of the SegWit marker, flag and witness stacks of a serialized tx."""
    size = 2
    for stack in witnesses:
        size += len(encode_varint(len(stack)))
        for item in stack:
            size += len(encode_varint(len(item))) + len(item)
    return size


def _vsize_from_sizes(base_size: int, witness_size: int) -> int:
    """BIP141 virtual size from the non-witness and witness byte counts."""
    weight = base_size * 4 + witness_size
    return -(-weight // 4)


def _measured_vsize(tx: TaprootTx) -> int:
    """Virtual size of a transaction whose witnesses are already the real shape."""
    total = len(tx.serialize())
    witness_size = _witness_section_size(tx.witnesses)
    return _vsize_from_sizes(total - witness_size, witness_size)


def frozen_state_hash(channels: Sequence[FrozenChannel], buyer_is_local: bool) -> str:
    """``SHA256("JMP0011/frozen-state/v1\\0" || canonical_json(normalized))``.

    ``normalized`` is the list of frozen channels in the given order, each one
    reduced to its outpoint, capacity, role-normalized claims, role-normalized
    funding keys and funding script. Mirrored snapshots of the same channels at
    the buyer and at the counterparty hash to the same digest. The peer node id
    is never hashed.
    """
    if type(buyer_is_local) is not bool:
        raise _error("buyer_is_local must be a bool")
    if isinstance(channels, str | bytes) or not isinstance(channels, Sequence) or not channels:
        raise _error("channels must be a non-empty sequence of FrozenChannel")
    records: list[dict[str, Any]] = []
    for channel in channels:
        if not isinstance(channel, FrozenChannel):
            raise _error("every channel must be a FrozenChannel")
        buyer_claim, counterparty_claim = (
            (channel.local_claim_sat, channel.remote_claim_sat)
            if buyer_is_local
            else (channel.remote_claim_sat, channel.local_claim_sat)
        )
        buyer_key, counterparty_key = (
            (channel.local_funding_pubkey, channel.remote_funding_pubkey)
            if buyer_is_local
            else (channel.remote_funding_pubkey, channel.local_funding_pubkey)
        )
        records.append(
            {
                "point": {"txid": channel.point.txid, "vout": channel.point.vout},
                "capacity_sat": channel.capacity_sat,
                "buyer_claim_sat": buyer_claim,
                "counterparty_claim_sat": counterparty_claim,
                "buyer_funding_pubkey": buyer_key.hex(),
                "counterparty_funding_pubkey": counterparty_key.hex(),
                "funding_script": channel.funding_script.hex(),
            }
        )
    try:
        canonical = rfc8785.dumps(records)
    except Exception:  # noqa: BLE001 - rfc8785 raises its own error tree
        raise _error("frozen channel state is not canonicalizable") from None
    return hashlib.sha256(_FROZEN_STATE_DOMAIN + canonical).hexdigest()


@dataclass(frozen=True)
class BuyoutTerms:
    """One proposal, the acceptance that answers it, and the channels they buy.

    Constructing this object is the validation: an instance only exists if the
    two payloads belong to the same attempt, the acceptance commits to this
    exact proposal and to this exact frozen state, every amount the counterparty
    chose is inside the limits the buyer proposed, and the timelocks leave the
    buyer a settlement window that ends before the split becomes spendable.
    """

    proposal: BuyoutPropose
    acceptance: BuyoutAccept
    channels: tuple[FrozenChannel, ...]
    buyer_is_local: bool
    _escrow: BuyoutEscrow = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.proposal, BuyoutPropose):
            raise _error("proposal must be a buyout_propose message")
        if not isinstance(self.acceptance, BuyoutAccept):
            raise _error("acceptance must be a buyout_accept message")
        if type(self.buyer_is_local) is not bool:
            raise _error("buyer_is_local must be a bool")
        if type(self.channels) is not tuple or not self.channels:
            raise _error("channels must be a non-empty tuple of FrozenChannel")
        if any(not isinstance(channel, FrozenChannel) for channel in self.channels):
            raise _error("every channel must be a FrozenChannel")
        self._check_attempt_binding()
        self._check_economics()
        self._check_channels()
        object.__setattr__(self, "_escrow", self._build_escrow())

    @property
    def escrow(self) -> BuyoutEscrow:
        """The single-leaf Taproot escrow these terms agree on."""
        return self._escrow

    @property
    def claim_sat(self) -> int:
        """What the buyer owes over Lightning: the entitlement plus the fee."""
        return self.acceptance.entitlement_C + self.acceptance.buyout_fee

    @property
    def counterparty_split_sat(self) -> int:
        """What the split pays the counterparty if the buyer never settles."""
        return self.claim_sat + self.acceptance.timeout_compensation

    def _check_attempt_binding(self) -> None:
        if self.acceptance.epoch_id != self.proposal.epoch_id:
            raise _error("acceptance is for a different epoch")
        if self.acceptance.attempt != self.proposal.attempt:
            raise _error("acceptance is for a different attempt")
        try:
            expected = proposal_hash(self.proposal)
        except BuyoutMessageError:
            raise _error("proposal cannot be hashed") from None
        if self.acceptance.proposal_hash != expected:
            raise _error("acceptance does not commit to this proposal")

    def _check_economics(self) -> None:
        proposal, acceptance = self.proposal, self.acceptance
        if acceptance.buyout_fee > proposal.max_buyout_fee:
            raise _error("buyout fee exceeds the proposed maximum")
        if acceptance.timeout_compensation > proposal.max_timeout_compensation:
            raise _error("timeout compensation exceeds the proposed maximum")
        if acceptance.settlement_depth > proposal.buyer_settlement_depth:
            raise _error("settlement depth exceeds the buyer's settlement window")
        required_csv = (
            proposal.buyer_settlement_depth + proposal.cltv_limit + SPLIT_SAFETY_MARGIN_BLOCKS
        )
        if proposal.csv_delay < required_csv:
            raise _error("csv delay does not cover the settlement window and the cltv limit")
        if self.counterparty_split_sat < proposal.min_split_output:
            raise _error("counterparty split output is below the agreed minimum")
        total = (
            self.counterparty_split_sat
            + proposal.min_split_output
            + proposal.split_fee
            + proposal.sweep_fee_reserve
        )
        if total > MAX_MONEY:
            raise _error("agreed amounts exceed the total money supply")

    def _check_channels(self) -> None:
        points = self.proposal.channel_points
        if len(self.channels) != len(points):
            raise _error("frozen channel count does not match the proposed channel points")
        peer = self.channels[0].peer_pubkey
        entitlement = 0
        capacity = 0
        for channel, point in zip(self.channels, points, strict=True):
            if channel.point != point:
                raise _error("frozen channels do not match the proposed channel points in order")
            if channel.peer_pubkey != peer:
                raise _error("frozen channels are not all with the same peer")
            if channel.local_claim_sat + channel.remote_claim_sat != channel.capacity_sat:
                raise _error("frozen channel claims do not account for its capacity exactly")
            entitlement += (
                channel.remote_claim_sat if self.buyer_is_local else channel.local_claim_sat
            )
            capacity += channel.capacity_sat
        if capacity > MAX_MONEY:
            raise _error("frozen channel capacities exceed the total money supply")
        if entitlement != self.acceptance.entitlement_C:
            raise _error("entitlement does not match the frozen counterparty claims")
        digest = frozen_state_hash(self.channels, self.buyer_is_local)
        if digest != self.acceptance.frozen_state_hash:
            raise _error("acceptance does not commit to this frozen channel state")

    def _build_escrow(self) -> BuyoutEscrow:
        try:
            return BuyoutEscrow(
                buyer_escrow_pubkey=bytes.fromhex(self.proposal.K_B),
                counterparty_escrow_pubkey=bytes.fromhex(self.acceptance.K_C),
                buyer_claim_pubkey=bytes.fromhex(self.proposal.K_B_claim),
                payment_hash=bytes.fromhex(self.acceptance.payment_hash),
            )
        except ChannelBuyoutEscrowError:
            raise _error("escrow keys and payment hash do not form a valid escrow") from None
        except ValueError:
            raise _error("escrow material is not valid hexadecimal") from None


@dataclass(frozen=True)
class ValidatedParent:
    """A parent transaction that :func:`validate_parent` accepted.

    ``vsize`` and ``fee_sat`` are the values the fee-rate check used: the vsize
    assumes the key-path witness every input will carry once signed. ``split``
    is the exact presigned split these terms require for this escrow output, so
    a caller signs what was validated rather than rebuilding it.
    """

    raw: bytes
    txid: str
    parent_hash: str
    escrow_outpoint: EscrowOutpoint
    split: UnsignedKeyPathSpend
    fee_sat: int
    vsize: int


def validate_parent(
    terms: BuyoutTerms,
    parent: bytes,
    prevouts: Sequence[Prevout],
    channel_input_indices: Sequence[int],
    escrow_output_index: int,
    tip_height: int,
    min_fee_rate_sat_vb: int = 1,
) -> ValidatedParent:
    """Decide whether ``parent`` is the CoinJoin ``terms`` allow to be signed.

    ``parent`` must be the canonical unsigned serialization of a version 2
    transaction: no witness, no scriptSig, no repeated outpoint, a height-based
    nLockTime no further than :data:`MAX_LOCKTIME_LOOKAHEAD_BLOCKS` ahead of
    ``tip_height``, and one uniform nSequence. ``prevouts`` are the spent outputs
    of every input in input order; ``channel_input_indices`` selects the inputs
    that spend the frozen channels, in the proposal's channel order; and
    ``escrow_output_index`` selects the output that must pay the escrow.

    Structure is not enough: the escrow output must also be large enough that
    both split outputs clear the agreed minimum, that the buyer's share still
    covers its sweep reserve, and that the reserve itself pays for a real
    unilateral claim at :data:`CLAIM_SWEEP_RESERVE_RATE_SAT_VB`. The claim's cost
    is measured from a claim witness of the true shape rather than estimated.

    Acceptance here is arithmetic and structural. It does not prove that a node
    would relay or mine the transaction.
    """
    if not isinstance(terms, BuyoutTerms):
        raise _error("terms must be a BuyoutTerms")
    terms.__post_init__()
    indices = _checked_indices(channel_input_indices, len(terms.channels))
    _require_int(escrow_output_index, "escrow_output_index")
    _require_int(tip_height, "tip_height")
    if type(min_fee_rate_sat_vb) is not int or min_fee_rate_sat_vb < 1:
        raise _error("min_fee_rate_sat_vb must be a positive integer")
    prevout_list = _checked_prevouts(prevouts)

    parsed = _parse_canonical_parent(parent)
    _check_parent_shape(parsed, prevout_list, tip_height)
    _check_channel_inputs(terms, parsed, prevout_list, indices)
    escrow_value = _check_escrow_output(terms, parsed, escrow_output_index)
    fee_sat, vsize = _check_parent_fee(
        terms, parsed, prevout_list, len(parent), min_fee_rate_sat_vb
    )

    outpoint = _escrow_outpoint(terms, parent, escrow_output_index, escrow_value)
    split = _check_settlement_economics(terms, outpoint)
    return ValidatedParent(
        raw=bytes(parent),
        txid=outpoint.txid,
        parent_hash=hashlib.sha256(parent).hexdigest(),
        escrow_outpoint=outpoint,
        split=split,
        fee_sat=fee_sat,
        vsize=vsize,
    )


def _checked_indices(channel_input_indices: Sequence[int], channel_count: int) -> tuple[int, ...]:
    if (
        isinstance(channel_input_indices, str | bytes)
        or not isinstance(channel_input_indices, Sequence)
        or len(channel_input_indices) != channel_count
    ):
        raise _error("channel_input_indices must select one parent input per frozen channel")
    indices = tuple(_require_int(index, "channel input index") for index in channel_input_indices)
    if len(set(indices)) != len(indices):
        raise _error("channel_input_indices must be distinct parent input indices")
    return indices


def _checked_prevouts(prevouts: Sequence[Prevout]) -> tuple[Prevout, ...]:
    if isinstance(prevouts, str | bytes) or not isinstance(prevouts, Sequence) or not prevouts:
        raise _error("prevouts must be a non-empty sequence of Prevout")
    if any(not isinstance(prevout, Prevout) for prevout in prevouts):
        raise _error("every prevout must be a Prevout")
    return tuple(prevouts)


def _parse_canonical_parent(parent: bytes) -> ParsedTransaction:
    if type(parent) is not bytes or not parent:
        raise _error("parent must be non-empty serialized transaction bytes")
    if len(parent) > _MAX_PARENT_BYTES:
        raise _error("parent transaction is larger than the protocol allows")
    try:
        parsed = parse_transaction_bytes(parent)
    except Exception:  # noqa: BLE001 - parser raises ValueError subtypes
        raise _error("parent transaction encoding is invalid") from None
    if parsed.has_witness or parsed.witnesses:
        raise _error("parent must be unsigned and carry no witness")
    if not parsed.inputs or not parsed.outputs:
        raise _error("parent must have at least one input and one output")
    if any(inp.scriptsig for inp in parsed.inputs):
        raise _error("parent inputs must have an empty scriptSig")
    canonical = serialize_transaction(
        parsed.version, parsed.inputs, parsed.outputs, parsed.locktime
    )
    if canonical != parent:
        raise _error("parent is not the canonical unsigned serialization of its contents")
    return parsed


def _check_parent_shape(
    parsed: ParsedTransaction, prevouts: tuple[Prevout, ...], tip_height: int
) -> None:
    if parsed.version != 2:
        raise _error("parent version must be 2")
    if parsed.locktime >= LOCKTIME_THRESHOLD:
        raise _error("parent nLockTime must be a block height")
    if parsed.locktime > tip_height + MAX_LOCKTIME_LOOKAHEAD_BLOCKS:
        raise _error("parent nLockTime is too far ahead of the chain tip")
    allowed = (
        {PARENT_SEQUENCE, PARENT_SEQUENCE_FINAL} if parsed.locktime == 0 else {PARENT_SEQUENCE}
    )
    sequences = {inp.sequence for inp in parsed.inputs}
    if len(sequences) != 1 or not sequences <= allowed:
        raise _error("parent inputs must all carry the same allowed nSequence")
    outpoints = {(inp.txid_le, inp.vout) for inp in parsed.inputs}
    if len(outpoints) != len(parsed.inputs):
        raise _error("parent must not spend the same outpoint twice")
    if len(prevouts) != len(parsed.inputs):
        raise _error("prevout count does not match the parent input count")
    if any(not _is_p2tr(out.script) for out in parsed.outputs):
        raise _error("every parent output must be a native P2TR output")
    if any(out.value <= 0 for out in parsed.outputs):
        raise _error("every parent output must pay a positive amount")


def _check_channel_inputs(
    terms: BuyoutTerms,
    parsed: ParsedTransaction,
    prevouts: tuple[Prevout, ...],
    indices: tuple[int, ...],
) -> None:
    for channel, index in zip(terms.channels, indices, strict=True):
        if index >= len(parsed.inputs):
            raise _error("a channel input index does not select a parent input")
        spent = parsed.inputs[index]
        if spent.txid != channel.point.txid or spent.vout != channel.point.vout:
            raise _error("a channel input does not spend the agreed channel outpoint")
        prevout = prevouts[index]
        if prevout.value != channel.capacity_sat:
            raise _error("a channel prevout value does not match the frozen capacity")
        if bytes.fromhex(prevout.script_pubkey) != channel.funding_script:
            raise _error("a channel prevout script does not match the frozen funding output")


def _check_escrow_output(
    terms: BuyoutTerms, parsed: ParsedTransaction, escrow_output_index: int
) -> int:
    if escrow_output_index >= len(parsed.outputs):
        raise _error("escrow_output_index does not select a parent output")
    script = terms.escrow.output_script()
    if sum(1 for out in parsed.outputs if out.script == script) != 1:
        raise _error("parent must pay the escrow script exactly once")
    if parsed.outputs[escrow_output_index].script != script:
        raise _error("the output at escrow_output_index does not pay the agreed escrow")
    value: int = parsed.outputs[escrow_output_index].value
    return value


def _check_parent_fee(
    terms: BuyoutTerms,
    parsed: ParsedTransaction,
    prevouts: tuple[Prevout, ...],
    base_size: int,
    min_fee_rate_sat_vb: int,
) -> tuple[int, int]:
    total_in = sum(prevout.value for prevout in prevouts)
    total_out = sum(out.value for out in parsed.outputs)
    if total_in > MAX_MONEY or total_out > MAX_MONEY:
        raise _error("parent totals exceed the total money supply")
    if total_out > total_in:
        raise _error("parent outputs exceed its inputs")
    fee_sat = total_in - total_out
    witness_size = _witness_section_size([[b"\x00" * TAPROOT_SIGNATURE_SIZE]] * len(parsed.inputs))
    vsize = _vsize_from_sizes(base_size, witness_size)
    required_rate = max(terms.acceptance.min_parent_fee_rate_sat_vb, min_fee_rate_sat_vb)
    if fee_sat < vsize * required_rate:
        raise _error("parent fee is below the agreed minimum fee rate")
    return fee_sat, vsize


def _escrow_outpoint(
    terms: BuyoutTerms, parent: bytes, escrow_output_index: int, escrow_value: int
) -> EscrowOutpoint:
    try:
        return EscrowOutpoint(
            txid=hash256(parent)[::-1].hex(),
            vout=escrow_output_index,
            value=escrow_value,
            scriptpubkey=terms.escrow.output_script(),
        )
    except ChannelBuyoutEscrowError:
        raise _error("parent does not define a usable escrow outpoint") from None


def _check_settlement_economics(
    terms: BuyoutTerms, outpoint: EscrowOutpoint
) -> UnsignedKeyPathSpend:
    proposal = terms.proposal
    counterparty_value = terms.counterparty_split_sat
    buyer_value = outpoint.value - counterparty_value - proposal.split_fee
    if buyer_value < proposal.min_split_output:
        raise _error("escrow value leaves the buyer split output below the agreed minimum")
    if outpoint.value - counterparty_value < proposal.sweep_fee_reserve + proposal.min_split_output:
        raise _error("escrow value does not cover the buyer share and its sweep reserve")
    claim_vsize = _claim_vsize(terms, outpoint)
    if proposal.sweep_fee_reserve < claim_vsize * CLAIM_SWEEP_RESERVE_RATE_SAT_VB:
        raise _error("sweep fee reserve does not pay for a claim at the required reserve rate")
    split = _build_agreed_split(terms, outpoint, counterparty_value)
    measured = TaprootTx(
        inputs=list(split.tx.inputs),
        outputs=list(split.tx.outputs),
        version=split.tx.version,
        locktime=split.tx.locktime,
        witnesses=[[b"\x00" * TAPROOT_SIGNATURE_SIZE]],
    )
    split_vsize = _measured_vsize(measured)
    # The split is one fixed-size key-path spend, so its fee is not a range to
    # negotiate: both sides derive the same exact value from the agreed rate.
    if proposal.split_fee != split_vsize * proposal.split_fee_rate_sat_vb:
        raise _error("split fee does not match the agreed split fee rate")
    return split


def _build_agreed_split(
    terms: BuyoutTerms, outpoint: EscrowOutpoint, counterparty_value: int
) -> UnsignedKeyPathSpend:
    try:
        return build_split(
            terms.escrow,
            outpoint,
            bytes.fromhex(terms.proposal.split_script_B),
            bytes.fromhex(terms.acceptance.split_script_C),
            counterparty_value,
            terms.proposal.split_fee,
            terms.proposal.csv_delay,
        )
    except ChannelBuyoutEscrowError:
        raise _error("the agreed split is not constructible from these terms") from None


def _claim_vsize(terms: BuyoutTerms, outpoint: EscrowOutpoint) -> int:
    """Measure the buyer's unilateral claim from a witness of the true shape.

    The witness is the real claim leaf and its real control block, with a dummy
    signature and preimage of the exact sizes the spend will carry, so the
    reserve is checked against a measured cost and never against a guess.
    """
    leaf, control = terms.escrow.claim_control_block()
    tx = TaprootTx(
        inputs=[TxIn(txid=outpoint.txid, vout=outpoint.vout, sequence=SWEEP_SEQUENCE)],
        outputs=[
            TxOut(value=outpoint.value, scriptpubkey=bytes.fromhex(terms.proposal.split_script_B))
        ],
        version=2,
        locktime=0,
        witnesses=[[b"\x00" * TAPROOT_SIGNATURE_SIZE, b"\x00" * 32, leaf, control]],
    )
    return _measured_vsize(tx)


__all__ = (
    "CLAIM_SWEEP_RESERVE_RATE_SAT_VB",
    "MAX_LOCKTIME_LOOKAHEAD_BLOCKS",
    "PARENT_SEQUENCE",
    "PARENT_SEQUENCE_FINAL",
    "SPLIT_SAFETY_MARGIN_BLOCKS",
    "BuyoutTerms",
    "ProtocolError",
    "ValidatedParent",
    "frozen_state_hash",
    "validate_parent",
)
