"""Tests for the pure terms and parent validation of the private buyout.

The scenario below is one complete, internally consistent attempt: two frozen
channels with the same peer, a proposal and the acceptance that answers it, and
an unsigned parent that pays the agreed escrow. Every other test mutates exactly
one thing in it and asserts the mutation is rejected, so a passing suite means
the accepted case is accepted for the stated reasons and not by accident.

The expected virtual sizes are written out as numbers rather than recomputed
with the module's own formula: restating the implementation would test nothing.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    hash256,
    serialize_transaction,
    taproot_tweak_pubkey,
)

from jmswap.bitcoin_escrow import MIN_SPLIT_OUTPUT_SATS
from jmswap.buyout_messages import (
    BuyoutAccept,
    BuyoutPropose,
    Outpoint,
    Prevout,
    proposal_hash,
)
from jmswap.buyout_terms import (
    CLAIM_SWEEP_RESERVE_RATE_SAT_VB,
    MAX_LOCKTIME_LOOKAHEAD_BLOCKS,
    PARENT_SEQUENCE,
    PARENT_SEQUENCE_FINAL,
    SPLIT_SAFETY_MARGIN_BLOCKS,
    BuyoutTerms,
    ProtocolError,
    frozen_state_hash,
    validate_parent,
)
from jmswap.lnd_escrow import FrozenChannel


def _pubkey(secret: bytes) -> bytes:
    return bytes(CKey(secret).pub)


def _p2tr(secret: bytes) -> bytes:
    _, output_key = taproot_tweak_pubkey(bytes(CKey(secret).xonly_pub))
    return bytes([0x51, 0x20]) + output_key


K_B = _pubkey(b"\x11" * 32)
K_C = _pubkey(b"\x22" * 32)
K_B_CLAIM = _pubkey(b"\x33" * 32)
PREIMAGE = b"\x44" * 32
PAYMENT_HASH = hashlib.sha256(PREIMAGE).digest()

PEER_AT_BUYER = _pubkey(b"\x55" * 32)  # C's node id, as B sees it
PEER_AT_COUNTERPARTY = _pubkey(b"\x66" * 32)  # B's node id, as C sees it

SPLIT_SCRIPT_B = _p2tr(b"\x71" * 32)
SPLIT_SCRIPT_C = _p2tr(b"\x72" * 32)
OTHER_SCRIPT = _p2tr(b"\x73" * 32)

EPOCH_ID = "ab" * 32
ATTEMPT = 3
TIP_HEIGHT = 800_000

# Channel data from the buyer's point of view: point, capacity, buyer claim,
# funding script, buyer funding key, counterparty funding key.
CHANNELS: tuple[tuple[Outpoint, int, int, bytes, bytes, bytes], ...] = (
    (
        Outpoint(txid="1a" * 32, vout=0),
        500_000,
        400_000,
        _p2tr(b"\x81" * 32),
        _pubkey(b"\x91" * 32),
        _pubkey(b"\x92" * 32),
    ),
    (
        Outpoint(txid="2b" * 32, vout=2),
        300_000,
        250_000,
        _p2tr(b"\x82" * 32),
        _pubkey(b"\x93" * 32),
        _pubkey(b"\x94" * 32),
    ),
)

ENTITLEMENT_C = 150_000  # (500_000 - 400_000) + (300_000 - 250_000)
BUYOUT_FEE = 5_000
TIMEOUT_COMPENSATION = 2_000
MIN_SPLIT_OUTPUT = 10_000

# One input, two P2TR outputs, one 64-byte key-path witness.
SPLIT_VSIZE = 154
SPLIT_FEE_RATE = 2
SPLIT_FEE = SPLIT_VSIZE * SPLIT_FEE_RATE

# One input, one P2TR output, witness of signature, preimage, leaf and control.
CLAIM_VSIZE = 146
SWEEP_FEE_RESERVE = 8_000

# Three inputs, three P2TR outputs, one 64-byte witness per input.
PARENT_VSIZE = 312
PARENT_FEE_RATE = 2

ESCROW_VALUE = 400_000
EXTRA_INPUT_VALUE = 200_000
PARENT_FEE = 1_000

BUYER_SETTLEMENT_DEPTH = 6
CLTV_LIMIT = 200
CSV_DELAY = BUYER_SETTLEMENT_DEPTH + CLTV_LIMIT + SPLIT_SAFETY_MARGIN_BLOCKS + 200


def make_channels(buyer_is_local: bool = True) -> tuple[FrozenChannel, ...]:
    """Build the frozen snapshots as the buyer or as the counterparty sees them."""
    peer = PEER_AT_BUYER if buyer_is_local else PEER_AT_COUNTERPARTY
    channels = []
    for point, capacity, buyer_claim, script, buyer_key, counterparty_key in CHANNELS:
        counterparty_claim = capacity - buyer_claim
        local, remote = (
            (buyer_claim, counterparty_claim)
            if buyer_is_local
            else (counterparty_claim, buyer_claim)
        )
        local_key, remote_key = (
            (buyer_key, counterparty_key) if buyer_is_local else (counterparty_key, buyer_key)
        )
        channels.append(
            FrozenChannel(
                point=point,
                capacity_sat=capacity,
                local_claim_sat=local,
                remote_claim_sat=remote,
                peer_pubkey=peer,
                funding_script=script,
                local_funding_pubkey=local_key,
                remote_funding_pubkey=remote_key,
            )
        )
    return tuple(channels)


def make_proposal(**overrides: Any) -> BuyoutPropose:
    fields: dict[str, Any] = {
        "v": 1,
        "type": "buyout_propose",
        "epoch_id": EPOCH_ID,
        "attempt": ATTEMPT,
        "network": "regtest",
        "channel_points": [point for point, *_ in CHANNELS],
        "K_B": K_B.hex(),
        "K_B_claim": K_B_CLAIM.hex(),
        "split_script_B": SPLIT_SCRIPT_B.hex(),
        "csv_delay": CSV_DELAY,
        "split_fee_rate_sat_vb": SPLIT_FEE_RATE,
        "split_fee": SPLIT_FEE,
        "min_split_output": MIN_SPLIT_OUTPUT,
        "sweep_fee_reserve": SWEEP_FEE_RESERVE,
        "buyer_settlement_depth": BUYER_SETTLEMENT_DEPTH,
        "cltv_limit": CLTV_LIMIT,
        "sweep_response_blocks": 2,
        "max_buyout_fee": 10_000,
        "max_timeout_compensation": 5_000,
        "freeze_ttl_blocks": 288,
        "expiry": 1_000,
    }
    fields.update(overrides)
    return BuyoutPropose(**fields)


def make_acceptance(proposal: BuyoutPropose, **overrides: Any) -> BuyoutAccept:
    fields: dict[str, Any] = {
        "v": 1,
        "type": "buyout_accept",
        "epoch_id": proposal.epoch_id,
        "attempt": proposal.attempt,
        "proposal_hash": proposal_hash(proposal),
        "K_C": K_C.hex(),
        "payment_hash": PAYMENT_HASH.hex(),
        "split_script_C": SPLIT_SCRIPT_C.hex(),
        "entitlement_C": ENTITLEMENT_C,
        "buyout_fee": BUYOUT_FEE,
        "timeout_compensation": TIMEOUT_COMPENSATION,
        "min_parent_fee_rate_sat_vb": PARENT_FEE_RATE,
        "parent_wait_blocks": 12,
        "settlement_depth": BUYER_SETTLEMENT_DEPTH,
        "max_freeze_blocks": 288,
        "frozen_state_hash": frozen_state_hash(make_channels(True), True),
    }
    fields.update(overrides)
    return BuyoutAccept(**fields)


def make_terms(buyer_is_local: bool = True, **acceptance_overrides: Any) -> BuyoutTerms:
    proposal = make_proposal()
    return BuyoutTerms(
        proposal=proposal,
        acceptance=make_acceptance(proposal, **acceptance_overrides),
        channels=make_channels(buyer_is_local),
        buyer_is_local=buyer_is_local,
    )


@pytest.fixture
def terms() -> BuyoutTerms:
    return make_terms()


def make_parent(
    terms: BuyoutTerms,
    *,
    inputs: list[TxInput] | None = None,
    outputs: list[TxOutput] | None = None,
    version: int = 2,
    locktime: int = 0,
    witnesses: list[list[bytes]] | None = None,
) -> bytes:
    return serialize_transaction(
        version,
        inputs if inputs is not None else default_inputs(),
        outputs if outputs is not None else default_outputs(terms),
        locktime,
        witnesses,
    )


def default_inputs(sequence: int = PARENT_SEQUENCE) -> list[TxInput]:
    channel_inputs = [
        TxInput(txid_le=bytes.fromhex(point.txid)[::-1], vout=point.vout, sequence=sequence)
        for point, *_ in CHANNELS
    ]
    channel_inputs.append(TxInput(txid_le=b"\xcc" * 32, vout=7, sequence=sequence))
    return channel_inputs


def default_outputs(terms: BuyoutTerms) -> list[TxOutput]:
    total_in = sum(capacity for _, capacity, *_ in CHANNELS) + EXTRA_INPUT_VALUE
    remainder = total_in - ESCROW_VALUE - PARENT_FEE
    return [
        TxOutput(value=ESCROW_VALUE, script=terms.escrow.output_script()),
        TxOutput(value=remainder - 99_000, script=OTHER_SCRIPT),
        TxOutput(value=99_000, script=SPLIT_SCRIPT_B),
    ]


def default_prevouts() -> list[Prevout]:
    prevouts = [
        Prevout(value=capacity, script_pubkey=script.hex())
        for _, capacity, _, script, *_ in CHANNELS
    ]
    prevouts.append(Prevout(value=EXTRA_INPUT_VALUE, script_pubkey=OTHER_SCRIPT.hex()))
    return prevouts


def validate(terms: BuyoutTerms, parent: bytes | None = None, **overrides: Any) -> Any:
    arguments: dict[str, Any] = {
        "prevouts": default_prevouts(),
        "channel_input_indices": [0, 1],
        "escrow_output_index": 0,
        "tip_height": TIP_HEIGHT,
    }
    arguments.update(overrides)
    return validate_parent(terms, parent if parent is not None else make_parent(terms), **arguments)


# --------------------------------------------------------------------------
# The accepted case
# --------------------------------------------------------------------------


def test_valid_terms_expose_the_escrow_and_the_claim(terms: BuyoutTerms) -> None:
    assert terms.claim_sat == ENTITLEMENT_C + BUYOUT_FEE
    assert terms.counterparty_split_sat == ENTITLEMENT_C + BUYOUT_FEE + TIMEOUT_COMPENSATION
    assert terms.escrow.buyer_escrow_pubkey == K_B
    assert terms.escrow.counterparty_escrow_pubkey == K_C
    assert terms.escrow.payment_hash == PAYMENT_HASH
    assert len(terms.escrow.output_script()) == 34


def test_valid_parent_is_accepted(terms: BuyoutTerms) -> None:
    parent = make_parent(terms)
    validated = validate(terms, parent)

    assert validated.raw == parent
    assert validated.txid == hash256(parent)[::-1].hex()
    assert validated.parent_hash == hashlib.sha256(parent).hexdigest()
    assert validated.vsize == PARENT_VSIZE
    assert validated.fee_sat == PARENT_FEE
    assert validated.escrow_outpoint.vout == 0
    assert validated.escrow_outpoint.value == ESCROW_VALUE
    assert validated.escrow_outpoint.scriptpubkey == terms.escrow.output_script()
    assert validated.escrow_outpoint.txid == validated.txid


def test_split_pays_the_counterparty_its_claim_and_compensation(terms: BuyoutTerms) -> None:
    split = validate(terms).split

    assert split.tx.inputs[0].sequence == CSV_DELAY
    assert split.tx.outputs[0].scriptpubkey == SPLIT_SCRIPT_B
    assert split.tx.outputs[1].scriptpubkey == SPLIT_SCRIPT_C
    assert split.tx.outputs[1].value == terms.counterparty_split_sat
    assert split.tx.outputs[0].value == ESCROW_VALUE - terms.counterparty_split_sat - SPLIT_FEE
    assert split.tx.outputs[0].value >= MIN_SPLIT_OUTPUT_SATS


def test_mirrored_terms_agree_on_everything_that_is_signed() -> None:
    at_buyer = make_terms(True)
    at_counterparty = make_terms(False)

    assert at_counterparty.acceptance.frozen_state_hash == at_buyer.acceptance.frozen_state_hash
    assert at_counterparty.escrow.output_script() == at_buyer.escrow.output_script()
    assert at_counterparty.claim_sat == at_buyer.claim_sat

    parent = make_parent(at_buyer)
    buyer_view = validate(at_buyer, parent)
    counterparty_view = validate(at_counterparty, parent)

    assert counterparty_view == buyer_view
    assert counterparty_view.split.sighash == buyer_view.split.sighash


def test_a_locktime_within_the_lookahead_is_accepted(terms: BuyoutTerms) -> None:
    parent = make_parent(terms, locktime=TIP_HEIGHT + MAX_LOCKTIME_LOOKAHEAD_BLOCKS)
    assert validate(terms, parent).txid == hash256(parent)[::-1].hex()


def test_final_sequences_are_accepted_only_without_a_locktime(terms: BuyoutTerms) -> None:
    final = make_parent(terms, inputs=default_inputs(PARENT_SEQUENCE_FINAL))
    assert validate(terms, final).fee_sat == PARENT_FEE

    with_locktime = make_parent(
        terms, inputs=default_inputs(PARENT_SEQUENCE_FINAL), locktime=TIP_HEIGHT
    )
    with pytest.raises(ProtocolError, match="nSequence"):
        validate(terms, with_locktime)


# --------------------------------------------------------------------------
# The frozen state hash
# --------------------------------------------------------------------------


def test_frozen_state_hash_is_mirrored_and_ignores_the_peer_identity() -> None:
    assert frozen_state_hash(make_channels(True), True) == frozen_state_hash(
        make_channels(False), False
    )


def test_frozen_state_hash_binds_the_role_of_each_claim() -> None:
    # The same snapshot read with the wrong role is a different frozen state.
    assert frozen_state_hash(make_channels(True), True) != frozen_state_hash(
        make_channels(True), False
    )


def test_frozen_state_hash_covers_the_claims_and_the_channel_order() -> None:
    channels = make_channels(True)
    baseline = frozen_state_hash(channels, True)

    moved = FrozenChannel(
        point=channels[0].point,
        capacity_sat=channels[0].capacity_sat,
        local_claim_sat=channels[0].local_claim_sat - 1,
        remote_claim_sat=channels[0].remote_claim_sat + 1,
        peer_pubkey=channels[0].peer_pubkey,
        funding_script=channels[0].funding_script,
        local_funding_pubkey=channels[0].local_funding_pubkey,
        remote_funding_pubkey=channels[0].remote_funding_pubkey,
    )
    assert frozen_state_hash((moved, channels[1]), True) != baseline
    assert frozen_state_hash((channels[1], channels[0]), True) != baseline


def test_frozen_state_hash_rejects_bad_arguments() -> None:
    channels = make_channels(True)
    with pytest.raises(ProtocolError, match="bool"):
        frozen_state_hash(channels, 1)  # type: ignore[arg-type]
    with pytest.raises(ProtocolError, match="non-empty sequence"):
        frozen_state_hash((), True)
    with pytest.raises(ProtocolError, match="FrozenChannel"):
        frozen_state_hash((channels[0], "not a channel"), True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Terms: binding, economics, channels
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"epoch_id": "cd" * 32}, "different epoch"),
        ({"attempt": ATTEMPT + 1}, "different attempt"),
        ({"proposal_hash": "ef" * 32}, "does not commit to this proposal"),
        ({"buyout_fee": 10_001}, "buyout fee exceeds"),
        ({"timeout_compensation": 5_001}, "timeout compensation exceeds"),
        ({"settlement_depth": BUYER_SETTLEMENT_DEPTH + 1}, "settlement depth exceeds"),
        ({"entitlement_C": ENTITLEMENT_C + 1}, "entitlement does not match"),
        ({"frozen_state_hash": "ff" * 32}, "frozen channel state"),
        ({"K_C": K_B.hex()}, "valid escrow"),
    ],
)
def test_acceptance_mutations_are_rejected(overrides: dict[str, Any], match: str) -> None:
    proposal = make_proposal()
    acceptance = make_acceptance(proposal, **overrides)
    with pytest.raises(ProtocolError, match=match):
        BuyoutTerms(
            proposal=proposal,
            acceptance=acceptance,
            channels=make_channels(True),
            buyer_is_local=True,
        )


def test_a_proposal_the_acceptance_never_saw_is_rejected() -> None:
    proposal = make_proposal()
    acceptance = make_acceptance(proposal)
    with pytest.raises(ProtocolError, match="does not commit to this proposal"):
        BuyoutTerms(
            proposal=make_proposal(expiry=1_001),
            acceptance=acceptance,
            channels=make_channels(True),
            buyer_is_local=True,
        )


def test_a_csv_delay_inside_the_settlement_window_is_rejected() -> None:
    proposal = make_proposal(
        csv_delay=BUYER_SETTLEMENT_DEPTH + CLTV_LIMIT + SPLIT_SAFETY_MARGIN_BLOCKS - 1
    )
    with pytest.raises(ProtocolError, match="csv delay"):
        BuyoutTerms(
            proposal=proposal,
            acceptance=make_acceptance(proposal),
            channels=make_channels(True),
            buyer_is_local=True,
        )


def test_a_counterparty_share_below_the_minimum_output_is_rejected() -> None:
    proposal = make_proposal(min_split_output=ENTITLEMENT_C + BUYOUT_FEE + TIMEOUT_COMPENSATION + 1)
    with pytest.raises(ProtocolError, match="counterparty split output"):
        BuyoutTerms(
            proposal=proposal,
            acceptance=make_acceptance(proposal),
            channels=make_channels(True),
            buyer_is_local=True,
        )


def test_channels_must_match_the_proposed_points_exactly() -> None:
    proposal = make_proposal()
    acceptance = make_acceptance(proposal)
    channels = make_channels(True)

    with pytest.raises(ProtocolError, match="channel count"):
        BuyoutTerms(
            proposal=proposal,
            acceptance=acceptance,
            channels=(channels[0],),
            buyer_is_local=True,
        )
    with pytest.raises(ProtocolError, match="in order"):
        BuyoutTerms(
            proposal=proposal,
            acceptance=acceptance,
            channels=(channels[1], channels[0]),
            buyer_is_local=True,
        )


def test_channels_with_different_peers_are_rejected() -> None:
    proposal = make_proposal()
    channels = make_channels(True)
    other_peer = FrozenChannel(
        point=channels[1].point,
        capacity_sat=channels[1].capacity_sat,
        local_claim_sat=channels[1].local_claim_sat,
        remote_claim_sat=channels[1].remote_claim_sat,
        peer_pubkey=PEER_AT_COUNTERPARTY,
        funding_script=channels[1].funding_script,
        local_funding_pubkey=channels[1].local_funding_pubkey,
        remote_funding_pubkey=channels[1].remote_funding_pubkey,
    )
    with pytest.raises(ProtocolError, match="same peer"):
        BuyoutTerms(
            proposal=proposal,
            acceptance=make_acceptance(proposal),
            channels=(channels[0], other_peer),
            buyer_is_local=True,
        )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"buyer_is_local": 1}, "bool"),
        ({"channels": list(make_channels(True))}, "tuple"),
        ({"channels": ()}, "tuple"),
        ({"channels": ("not a channel",)}, "FrozenChannel"),
        ({"proposal": "not a proposal"}, "buyout_propose"),
        ({"acceptance": "not an acceptance"}, "buyout_accept"),
    ],
)
def test_terms_reject_wrong_argument_types(kwargs: dict[str, Any], match: str) -> None:
    proposal = make_proposal()
    arguments: dict[str, Any] = {
        "proposal": proposal,
        "acceptance": make_acceptance(proposal),
        "channels": make_channels(True),
        "buyer_is_local": True,
    }
    arguments.update(kwargs)
    with pytest.raises(ProtocolError, match=match):
        BuyoutTerms(**arguments)


# --------------------------------------------------------------------------
# Parent structure
# --------------------------------------------------------------------------


def test_parent_must_be_version_two(terms: BuyoutTerms) -> None:
    with pytest.raises(ProtocolError, match="version"):
        validate(terms, make_parent(terms, version=1))


def test_parent_locktime_must_be_a_near_block_height(terms: BuyoutTerms) -> None:
    with pytest.raises(ProtocolError, match="block height"):
        validate(terms, make_parent(terms, locktime=500_000_000))
    with pytest.raises(ProtocolError, match="ahead of the chain tip"):
        validate(terms, make_parent(terms, locktime=TIP_HEIGHT + MAX_LOCKTIME_LOOKAHEAD_BLOCKS + 1))


def test_parent_sequences_must_be_uniform_and_replaceable(terms: BuyoutTerms) -> None:
    mixed = default_inputs()
    mixed[1] = TxInput(txid_le=mixed[1].txid_le, vout=mixed[1].vout, sequence=PARENT_SEQUENCE_FINAL)
    with pytest.raises(ProtocolError, match="nSequence"):
        validate(terms, make_parent(terms, inputs=mixed))

    with pytest.raises(ProtocolError, match="nSequence"):
        validate(terms, make_parent(terms, inputs=default_inputs(0xFFFFFFFD)))


def test_parent_must_not_spend_an_outpoint_twice(terms: BuyoutTerms) -> None:
    inputs = default_inputs()
    inputs[2] = TxInput(txid_le=inputs[0].txid_le, vout=inputs[0].vout, sequence=inputs[0].sequence)
    with pytest.raises(ProtocolError, match="same outpoint twice"):
        validate(terms, make_parent(terms, inputs=inputs))


def test_parent_must_carry_no_signature(terms: BuyoutTerms) -> None:
    signed = make_parent(terms, witnesses=[[b"\x00" * 64]] * 3)
    with pytest.raises(ProtocolError, match="no witness"):
        validate(terms, signed)

    inputs = default_inputs()
    inputs[0] = TxInput(
        txid_le=inputs[0].txid_le,
        vout=inputs[0].vout,
        scriptsig=b"\x51",
        sequence=inputs[0].sequence,
    )
    with pytest.raises(ProtocolError, match="scriptSig"):
        validate(terms, make_parent(terms, inputs=inputs))


def test_parent_must_be_canonically_serialized(terms: BuyoutTerms) -> None:
    parent = make_parent(terms)
    # A non-minimal varint for the input count parses but is not canonical.
    padded = parent[:4] + b"\xfd\x03\x00" + parent[5:]
    with pytest.raises(ProtocolError, match="canonical"):
        validate(terms, padded)

    with pytest.raises(ProtocolError, match="encoding is invalid"):
        validate(terms, parent + b"\x00")

    with pytest.raises(ProtocolError, match="non-empty serialized transaction"):
        validate(terms, b"")


def test_parent_outputs_must_be_p2tr_and_non_zero(terms: BuyoutTerms) -> None:
    outputs = default_outputs(terms)

    not_taproot = list(outputs)
    not_taproot[1] = TxOutput(value=outputs[1].value, script=b"\x00\x14" + b"\x11" * 20)
    with pytest.raises(ProtocolError, match="P2TR"):
        validate(terms, make_parent(terms, outputs=not_taproot))

    zero = list(outputs)
    zero[1] = TxOutput(value=0, script=outputs[1].script)
    with pytest.raises(ProtocolError, match="positive amount"):
        validate(terms, make_parent(terms, outputs=zero))


def test_prevouts_must_correspond_to_the_inputs(terms: BuyoutTerms) -> None:
    with pytest.raises(ProtocolError, match="prevout count"):
        validate(terms, prevouts=default_prevouts()[:2])
    with pytest.raises(ProtocolError, match="non-empty sequence of Prevout"):
        validate(terms, prevouts=[])
    with pytest.raises(ProtocolError, match="must be a Prevout"):
        validate(terms, prevouts=[*default_prevouts()[:2], "not a prevout"])


# --------------------------------------------------------------------------
# Parent bindings to the frozen channels and to the escrow
# --------------------------------------------------------------------------


def test_channel_inputs_must_spend_the_frozen_outpoints_in_order(terms: BuyoutTerms) -> None:
    with pytest.raises(ProtocolError, match="agreed channel outpoint"):
        validate(terms, channel_input_indices=[1, 0])
    with pytest.raises(ProtocolError, match="agreed channel outpoint"):
        validate(terms, channel_input_indices=[0, 2])
    with pytest.raises(ProtocolError, match="distinct"):
        validate(terms, channel_input_indices=[0, 0])
    with pytest.raises(ProtocolError, match="one parent input per frozen channel"):
        validate(terms, channel_input_indices=[0])
    with pytest.raises(ProtocolError, match="does not select a parent input"):
        validate(terms, channel_input_indices=[0, 9])


def test_channel_prevouts_must_match_the_frozen_funding_output(terms: BuyoutTerms) -> None:
    prevouts = default_prevouts()
    prevouts[0] = Prevout(value=CHANNELS[0][1] - 1, script_pubkey=prevouts[0].script_pubkey)
    with pytest.raises(ProtocolError, match="value does not match the frozen capacity"):
        validate(terms, prevouts=prevouts)

    prevouts = default_prevouts()
    prevouts[1] = Prevout(value=prevouts[1].value, script_pubkey=OTHER_SCRIPT.hex())
    with pytest.raises(ProtocolError, match="script does not match the frozen funding"):
        validate(terms, prevouts=prevouts)


def test_the_escrow_output_must_be_present_exactly_once(terms: BuyoutTerms) -> None:
    outputs = default_outputs(terms)

    missing = list(outputs)
    missing[0] = TxOutput(value=outputs[0].value, script=OTHER_SCRIPT)
    with pytest.raises(ProtocolError, match="exactly once"):
        validate(terms, make_parent(terms, outputs=missing))

    twice = [*outputs, TxOutput(value=1_000, script=terms.escrow.output_script())]
    with pytest.raises(ProtocolError, match="exactly once"):
        validate(terms, make_parent(terms, outputs=twice))

    with pytest.raises(ProtocolError, match="does not pay the agreed escrow"):
        validate(terms, escrow_output_index=1)
    with pytest.raises(ProtocolError, match="does not select a parent output"):
        validate(terms, escrow_output_index=3)


# --------------------------------------------------------------------------
# Fees, reserves and feasibility
# --------------------------------------------------------------------------


def test_parent_fee_must_reach_the_higher_of_the_two_rates(terms: BuyoutTerms) -> None:
    outputs = default_outputs(terms)
    starved = list(outputs)
    # Leave exactly one satoshi less than the agreed rate requires.
    starved[1] = TxOutput(
        value=outputs[1].value + (PARENT_FEE - PARENT_VSIZE * PARENT_FEE_RATE) + 1,
        script=outputs[1].script,
    )
    with pytest.raises(ProtocolError, match="below the agreed minimum fee rate"):
        validate(terms, make_parent(terms, outputs=starved))

    # The caller's own floor applies even when the acceptance asked for less.
    with pytest.raises(ProtocolError, match="below the agreed minimum fee rate"):
        validate(terms, min_fee_rate_sat_vb=10)


def test_parent_outputs_must_not_exceed_its_inputs(terms: BuyoutTerms) -> None:
    outputs = default_outputs(terms)
    inflated = list(outputs)
    inflated[1] = TxOutput(value=outputs[1].value + PARENT_FEE + 1, script=outputs[1].script)
    with pytest.raises(ProtocolError, match="exceed its inputs"):
        validate(terms, make_parent(terms, outputs=inflated))


def test_an_escrow_too_small_for_the_split_is_rejected(terms: BuyoutTerms) -> None:
    floor = terms.counterparty_split_sat + SWEEP_FEE_RESERVE + MIN_SPLIT_OUTPUT
    outputs = default_outputs(terms)
    shrunk = list(outputs)
    shrunk[0] = TxOutput(value=floor - 1, script=outputs[0].script)
    shrunk[1] = TxOutput(
        value=outputs[1].value + outputs[0].value - floor + 1, script=outputs[1].script
    )
    with pytest.raises(ProtocolError, match="sweep reserve"):
        validate(terms, make_parent(terms, outputs=shrunk))


def test_an_escrow_that_cannot_fund_the_buyer_split_is_rejected() -> None:
    # A reserve of zero removes the sweep-reserve floor, so the buyer's split
    # output becomes the binding constraint.
    proposal = make_proposal(sweep_fee_reserve=0)
    terms = BuyoutTerms(
        proposal=proposal,
        acceptance=make_acceptance(proposal),
        channels=make_channels(True),
        buyer_is_local=True,
    )
    floor = terms.counterparty_split_sat + SPLIT_FEE + MIN_SPLIT_OUTPUT
    outputs = default_outputs(terms)
    shrunk = list(outputs)
    shrunk[0] = TxOutput(value=floor - 1, script=outputs[0].script)
    shrunk[1] = TxOutput(
        value=outputs[1].value + outputs[0].value - floor + 1, script=outputs[1].script
    )
    with pytest.raises(ProtocolError, match="buyer split output below"):
        validate(terms, make_parent(terms, outputs=shrunk))


def test_the_sweep_reserve_must_pay_for_a_measured_claim() -> None:
    required = CLAIM_VSIZE * CLAIM_SWEEP_RESERVE_RATE_SAT_VB

    for reserve, expectation in ((required, True), (required - 1, False)):
        proposal = make_proposal(sweep_fee_reserve=reserve)
        terms = BuyoutTerms(
            proposal=proposal,
            acceptance=make_acceptance(proposal),
            channels=make_channels(True),
            buyer_is_local=True,
        )
        if expectation:
            assert validate(terms).fee_sat == PARENT_FEE
        else:
            with pytest.raises(ProtocolError, match="required reserve rate"):
                validate(terms)


def test_the_split_fee_must_equal_the_agreed_rate_times_the_split_vsize() -> None:
    for delta in (-1, 1):
        proposal = make_proposal(split_fee=SPLIT_FEE + delta)
        terms = BuyoutTerms(
            proposal=proposal,
            acceptance=make_acceptance(proposal),
            channels=make_channels(True),
            buyer_is_local=True,
        )
        with pytest.raises(ProtocolError, match="split fee does not match"):
            validate(terms)


# --------------------------------------------------------------------------
# Argument typing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"escrow_output_index": True}, "escrow_output_index"),
        ({"tip_height": True}, "tip_height"),
        ({"channel_input_indices": [True, 1]}, "channel input index"),
        ({"min_fee_rate_sat_vb": True}, "min_fee_rate_sat_vb"),
        ({"escrow_output_index": -1}, "escrow_output_index"),
        ({"tip_height": -1}, "tip_height"),
        ({"min_fee_rate_sat_vb": 0}, "min_fee_rate_sat_vb"),
        ({"channel_input_indices": "01"}, "one parent input per frozen channel"),
    ],
)
def test_arguments_are_not_coerced(
    terms: BuyoutTerms, overrides: dict[str, Any], match: str
) -> None:
    with pytest.raises(ProtocolError, match=match):
        validate(terms, **overrides)


def test_validate_parent_requires_validated_terms() -> None:
    with pytest.raises(ProtocolError, match="BuyoutTerms"):
        validate_parent(
            "not terms",  # type: ignore[arg-type]
            b"\x02",
            default_prevouts(),
            [0, 1],
            0,
            TIP_HEIGHT,
        )
