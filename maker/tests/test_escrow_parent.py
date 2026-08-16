"""Adversarial tests for maker-side final escrow parent validation."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    get_txid,
    hash160,
    parse_transaction,
    serialize_transaction,
)
from jmcore.taproot import TaprootTx, TxIn, TxOut, sign_taproot_keypath
from jmswap.channel_escrow import (
    ChannelInputEscrowRecord,
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowLifecycleState,
    EscrowOutpoint,
    EscrowParentEvidence,
    EscrowParentPrevout,
    EscrowTimeoutEvidence,
    ParentAuthorization,
    PreparedChannelInputEscrow,
)
from jmswap.errors import ChannelInputEscrowError
from jmwallet.wallet.signing import sign_p2wpkh_input

from maker.escrow_parent import validate_finalized_escrow_parent

WALLET_TXID = "11" * 32
FUNDING_TXID = "22" * 32


def _terms(funding_script: bytes) -> ChannelInputEscrowTerms:
    return ChannelInputEscrowTerms(
        snapshot=ChannelSnapshot(
            session_id="escrow-session",
            channel_id="channel-id",
            funding_outpoint=EscrowOutpoint(txid=FUNDING_TXID, vout=1),
            funding_scriptpubkey=funding_script.hex(),
            local_funding_pubkey="02" + "46" * 32,
            remote_funding_pubkey="03" + "47" * 32,
            capacity_sats=100_000,
            maker_claim_sats=70_000,
            peer_claim_sats=30_000,
            quiescent=True,
            active_htlcs=0,
        ),
        authorizer_pubkey="02" + "66" * 32,
        payment_hash="01" * 32,
        invoice="lnbcrt1privateinvoice",
        invoice_payee_pubkey="02" + "34" * 32,
        invoice_payment_secret="35" * 32,
        invoice_amount_msat=32_000_000,
        invoice_expiry_unix=2_000_000_000,
        invoice_min_final_cltv_delta=18,
        invoice_verified=True,
        premium_sats=2_000,
        timeout_compensation_sats=500,
        escrow_value_sats=80_000,
        timeout_fee_sats=500,
        timeout_height=1_000,
        peer_settle_deadline=970,
        success_broadcast_deadline=980,
        parent_confirmation_depth=1,
        success_confirmation_depth=1,
        success_confirmation_budget_blocks=10,
        reorg_margin_blocks=5,
        safety_delta_blocks=3,
        preimage_budget_blocks=2,
    )


def _build_artifacts() -> tuple[
    ChannelInputEscrowRecord, PreparedChannelInputEscrow, ParentAuthorization, str
]:
    wallet_key = PrivateKey(b"\x01" * 32)
    channel_key = PrivateKey(b"\x02" * 32)
    escrow_key = PrivateKey(b"\x03" * 32)
    wallet_pubkey = wallet_key.public_key.format(compressed=True)
    wallet_script = b"\x00\x14" + hash160(wallet_pubkey)
    funding_script = b"\x51\x20" + channel_key.public_key.format(compressed=True)[1:]
    escrow_script = b"\x51\x20" + escrow_key.public_key.format(compressed=True)[1:]
    terms = _terms(funding_script)
    inputs = [TxInput.from_hex(WALLET_TXID, 0), TxInput.from_hex(FUNDING_TXID, 1)]
    outputs = [TxOutput(value=80_000, script=escrow_script)]
    parent_tx_hex = serialize_transaction(2, inputs, outputs, 0).hex()
    parent_txid = get_txid(parent_tx_hex)
    unsigned = parse_transaction(parent_tx_hex)
    ordinary_signature = sign_p2wpkh_input(
        unsigned,
        0,
        create_p2wpkh_script_code(wallet_pubkey),
        60_000,
        wallet_key,
    )
    taproot_tx = TaprootTx(
        inputs=[
            TxIn(txid=value.txid, vout=value.vout, sequence=value.sequence) for value in inputs
        ],
        outputs=[TxOut(value=value.value, scriptpubkey=value.script) for value in outputs],
        version=2,
        locktime=0,
    )
    channel_signature = sign_taproot_keypath(
        taproot_tx,
        1,
        [60_000, 100_000],
        [wallet_script, funding_script],
        channel_key.secret,
    )
    signed_parent_tx_hex = serialize_transaction(
        2,
        inputs,
        outputs,
        0,
        witnesses=[[ordinary_signature, wallet_pubkey], [channel_signature]],
    ).hex()
    evidence = EscrowParentEvidence(
        parent_tx_hex=parent_tx_hex,
        parent_txid=parent_txid,
        channel_funding_outpoint=terms.snapshot.funding_outpoint,
        channel_input_index=1,
        ordered_prevouts=(
            EscrowParentPrevout(value_sats=60_000, scriptpubkey=wallet_script.hex()),
            EscrowParentPrevout(value_sats=100_000, scriptpubkey=funding_script.hex()),
        ),
        escrow_outpoint=EscrowOutpoint(txid=parent_txid, vout=0),
        escrow_value_sats=80_000,
        escrow_scriptpubkey=escrow_script.hex(),
        all_inputs_nonmalleable=True,
        exact_transaction_verified=True,
    )
    prepared = PreparedChannelInputEscrow(
        terms=terms,
        parent_evidence=evidence,
        timeout_evidence=EscrowTimeoutEvidence(
            timeout_tx_hex="0200",
            timeout_txid="aa" * 32,
            spent_escrow_outpoint=evidence.escrow_outpoint,
            locktime=terms.timeout_height,
            maker_output_sats=49_000,
            peer_output_sats=30_500,
            fee_sats=500,
            fully_signed=True,
            signature_verified=True,
        ),
    )
    authorization = ParentAuthorization(
        session_id=terms.snapshot.session_id,
        parent_txid=parent_txid,
        prepared_parent_tx_hex=parent_tx_hex,
        timeout_txid=prepared.timeout_evidence.timeout_txid,
        channel_funding_outpoint=terms.snapshot.funding_outpoint,
        channel_input_index=1,
        channel_input_witness=(channel_signature,),
        sighash="default",
        signature_verified=True,
        channel_witness_txid_verified=True,
        commits_all_inputs=True,
        commits_all_outputs=True,
    )
    record = ChannelInputEscrowRecord(
        terms=terms,
        negotiated_parent_evidence=evidence,
        state=EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED,
        prepared=prepared,
        authorization=authorization,
    )
    return record, prepared, authorization, signed_parent_tx_hex


def _with_witnesses(tx_hex: str, witnesses: list[list[bytes]]) -> str:
    tx = parse_transaction(tx_hex)
    return serialize_transaction(
        tx.version, tx.inputs, tx.outputs, tx.locktime, witnesses=witnesses
    ).hex()


def _wrong_channel_witness(signed_parent_tx_hex: str) -> str:
    tx = parse_transaction(signed_parent_tx_hex)
    return _with_witnesses(signed_parent_tx_hex, [tx.witnesses[0], [b"\x00" * 64]])


def _wrong_ordinary_sighash(signed_parent_tx_hex: str) -> str:
    tx = parse_transaction(signed_parent_tx_hex)
    signature, pubkey = tx.witnesses[0]
    return _with_witnesses(
        signed_parent_tx_hex,
        [[signature[:-1] + b"\x02", pubkey], tx.witnesses[1]],
    )


def _wrong_ordinary_signature(signed_parent_tx_hex: str) -> str:
    tx = parse_transaction(signed_parent_tx_hex)
    return _with_witnesses(signed_parent_tx_hex, [[b"\x01", tx.witnesses[0][1]], tx.witnesses[1]])


def _wrong_ordinary_pubkey(signed_parent_tx_hex: str) -> str:
    tx = parse_transaction(signed_parent_tx_hex)
    wrong_pubkey = PrivateKey(b"\x04" * 32).public_key.format(compressed=True)
    return _with_witnesses(
        signed_parent_tx_hex,
        [[tx.witnesses[0][0], wrong_pubkey], tx.witnesses[1]],
    )


def _incomplete_ordinary_witness(signed_parent_tx_hex: str) -> str:
    tx = parse_transaction(signed_parent_tx_hex)
    return _with_witnesses(signed_parent_tx_hex, [[tx.witnesses[0][0]], tx.witnesses[1]])


def test_validate_finalized_escrow_parent_accepts_released_record_and_artifacts() -> None:
    record, prepared, authorization, signed_parent_tx_hex = _build_artifacts()

    finalized = validate_finalized_escrow_parent(record, signed_parent_tx_hex)
    direct = validate_finalized_escrow_parent(prepared, signed_parent_tx_hex, authorization)

    assert finalized == direct
    assert finalized.signed_parent_tx_hex == signed_parent_tx_hex
    assert finalized.all_inputs_signed
    assert finalized.all_signatures_verified
    assert finalized.signed_parent_txid_verified


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record, prepared, authorization, signed: (
            record.model_copy(update={"state": EscrowLifecycleState.TIMEOUT_SIGNED}),
            signed,
        ),
        lambda record, prepared, authorization, signed: (
            record.model_copy(update={"authorization": None}),
            signed,
        ),
        lambda record, prepared, authorization, signed: (
            record,
            _wrong_channel_witness(signed),
        ),
        lambda record, prepared, authorization, signed: (
            record.model_copy(
                update={
                    "authorization": authorization.model_copy(
                        update={"channel_input_witness": (b"\x00" * 64,)}
                    )
                }
            ),
            _wrong_channel_witness(signed),
        ),
        lambda record, prepared, authorization, signed: (
            record,
            _wrong_ordinary_sighash(signed),
        ),
        lambda record, prepared, authorization, signed: (
            record,
            _wrong_ordinary_signature(signed),
        ),
        lambda record, prepared, authorization, signed: (
            record,
            _wrong_ordinary_pubkey(signed),
        ),
        lambda record, prepared, authorization, signed: (
            record,
            _incomplete_ordinary_witness(signed),
        ),
    ],
)
def test_validate_finalized_escrow_parent_rejects_mutations(
    mutation: Callable[
        [ChannelInputEscrowRecord, PreparedChannelInputEscrow, ParentAuthorization, str],
        tuple[ChannelInputEscrowRecord, str],
    ],
) -> None:
    record, prepared, authorization, signed_parent_tx_hex = _build_artifacts()
    mutated_record, mutated_signed_parent_tx_hex = mutation(
        record, prepared, authorization, signed_parent_tx_hex
    )

    with pytest.raises(ChannelInputEscrowError, match="invalid finalized escrow parent"):
        validate_finalized_escrow_parent(mutated_record, mutated_signed_parent_tx_hex)


def test_validate_finalized_escrow_parent_rejects_changed_parent_bytes_and_prevout_evidence() -> (
    None
):
    record, prepared, authorization, signed_parent_tx_hex = _build_artifacts()
    tx = parse_transaction(signed_parent_tx_hex)
    changed_inputs = [*tx.inputs]
    changed_inputs[0].sequence -= 1
    changed_parent = serialize_transaction(
        tx.version, changed_inputs, tx.outputs, tx.locktime, witnesses=tx.witnesses
    ).hex()
    altered_prevout = prepared.parent_evidence.ordered_prevouts[0].model_copy(
        update={"value_sats": 60_001}
    )
    altered_prepared = prepared.model_copy(
        update={
            "parent_evidence": prepared.parent_evidence.model_copy(
                update={
                    "ordered_prevouts": (
                        altered_prevout,
                        *prepared.parent_evidence.ordered_prevouts[1:],
                    )
                }
            )
        }
    )

    with pytest.raises(ChannelInputEscrowError, match="invalid finalized escrow parent"):
        validate_finalized_escrow_parent(record, changed_parent)
    with pytest.raises(ChannelInputEscrowError, match="invalid finalized escrow parent"):
        validate_finalized_escrow_parent(altered_prepared, signed_parent_tx_hex, authorization)
