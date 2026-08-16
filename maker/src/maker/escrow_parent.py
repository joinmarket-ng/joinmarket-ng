"""Independent validation of a fully signed channel-input escrow parent."""

from __future__ import annotations

from jmcore.bitcoin import (
    ParsedTransaction,
    create_p2wpkh_script_code,
    get_txid,
    hash160,
    parse_transaction,
    serialize_transaction,
)
from jmcore.taproot import SIGHASH_DEFAULT, TaprootTx, TxIn, TxOut, taproot_sighash, verify_schnorr
from jmswap.channel_escrow import (
    ChannelInputEscrowRecord,
    EscrowLifecycleState,
    FinalizedEscrowParent,
    ParentAuthorization,
    PreparedChannelInputEscrow,
)
from jmswap.errors import ChannelInputEscrowError
from jmwallet.wallet.signing import verify_p2wpkh_signature


def validate_finalized_escrow_parent(
    record_or_prepared: ChannelInputEscrowRecord | PreparedChannelInputEscrow,
    signed_parent_tx_hex: str,
    authorization: ParentAuthorization | None = None,
) -> FinalizedEscrowParent:
    """Verify every witness of the exact parent authorized for an escrow session.

    ``record_or_prepared`` is either the durable record in its signature-released
    state, or the released record's prepared and authorization artifacts.
    """
    try:
        prepared, released_authorization = _resolve_artifacts(record_or_prepared, authorization)
        parent = prepared.parent_evidence
        _validate_artifact_bindings(prepared, released_authorization)
        signed = _parse_exact_signed_parent(
            signed_parent_tx_hex, parent.parent_tx_hex, parent.parent_txid
        )
        _validate_channel_input(signed, prepared, released_authorization)
        _validate_ordinary_inputs(signed, prepared, parent.channel_input_index)
        return FinalizedEscrowParent(
            session_id=prepared.terms.snapshot.session_id,
            parent_txid=parent.parent_txid,
            signed_parent_tx_hex=signed_parent_tx_hex,
            channel_funding_outpoint=parent.channel_funding_outpoint,
            channel_input_index=parent.channel_input_index,
            all_inputs_signed=True,
            all_signatures_verified=True,
            signed_parent_txid_verified=True,
        )
    except ChannelInputEscrowError:
        raise
    except Exception:  # noqa: BLE001 - normalize untrusted transaction data
        raise ChannelInputEscrowError("invalid finalized escrow parent") from None


def _resolve_artifacts(
    record_or_prepared: ChannelInputEscrowRecord | PreparedChannelInputEscrow,
    authorization: ParentAuthorization | None,
) -> tuple[PreparedChannelInputEscrow, ParentAuthorization]:
    if isinstance(record_or_prepared, ChannelInputEscrowRecord):
        if authorization is not None:
            raise ChannelInputEscrowError("invalid finalized escrow parent")
        if (
            record_or_prepared.state is not EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED
            or record_or_prepared.prepared is None
            or record_or_prepared.authorization is None
        ):
            raise ChannelInputEscrowError("invalid finalized escrow parent")
        return record_or_prepared.prepared, record_or_prepared.authorization
    if isinstance(record_or_prepared, PreparedChannelInputEscrow) and isinstance(
        authorization, ParentAuthorization
    ):
        return record_or_prepared, authorization
    raise ChannelInputEscrowError("invalid finalized escrow parent")


def _validate_artifact_bindings(
    prepared: PreparedChannelInputEscrow, authorization: ParentAuthorization
) -> None:
    parent = prepared.parent_evidence
    terms = prepared.terms
    if (
        not parent.all_inputs_nonmalleable
        or not parent.exact_transaction_verified
        or not prepared.timeout_evidence.fully_signed
        or not prepared.timeout_evidence.signature_verified
        or authorization.session_id != terms.snapshot.session_id
        or authorization.parent_txid != parent.parent_txid
        or authorization.prepared_parent_tx_hex != parent.parent_tx_hex
        or authorization.timeout_txid != prepared.timeout_evidence.timeout_txid
        or authorization.channel_funding_outpoint != terms.snapshot.funding_outpoint
        or authorization.channel_funding_outpoint != parent.channel_funding_outpoint
        or authorization.channel_input_index != parent.channel_input_index
        or authorization.sighash != "default"
        or not authorization.signature_verified
        or not authorization.channel_witness_txid_verified
        or not authorization.commits_all_inputs
        or not authorization.commits_all_outputs
        or len(authorization.channel_input_witness) != 1
        or len(authorization.channel_input_witness[0]) != 64
        or len(parent.ordered_prevouts) <= parent.channel_input_index
    ):
        raise ChannelInputEscrowError("invalid finalized escrow parent")
    channel_prevout = parent.ordered_prevouts[parent.channel_input_index]
    if (
        channel_prevout.value_sats != terms.snapshot.capacity_sats
        or channel_prevout.scriptpubkey != terms.snapshot.funding_scriptpubkey
    ):
        raise ChannelInputEscrowError("invalid finalized escrow parent")


def _parse_exact_signed_parent(
    signed_parent_tx_hex: str, expected_parent_tx_hex: str, expected_txid: str
) -> ParsedTransaction:
    signed = parse_transaction(signed_parent_tx_hex)
    if not signed.has_witness or len(signed.witnesses) != len(signed.inputs):
        raise ChannelInputEscrowError("invalid finalized escrow parent")
    if any(tx_input.scriptsig for tx_input in signed.inputs):
        raise ChannelInputEscrowError("invalid finalized escrow parent")
    stripped = serialize_transaction(
        signed.version, signed.inputs, signed.outputs, signed.locktime
    ).hex()
    if stripped != expected_parent_tx_hex or get_txid(signed_parent_tx_hex) != expected_txid:
        raise ChannelInputEscrowError("invalid finalized escrow parent")
    return signed


def _validate_channel_input(
    signed: ParsedTransaction,
    prepared: PreparedChannelInputEscrow,
    authorization: ParentAuthorization,
) -> None:
    parent = prepared.parent_evidence
    index = parent.channel_input_index
    if index >= len(signed.inputs):
        raise ChannelInputEscrowError("invalid finalized escrow parent")
    channel_input = signed.inputs[index]
    if (
        channel_input.txid != parent.channel_funding_outpoint.txid
        or channel_input.vout != parent.channel_funding_outpoint.vout
        or tuple(signed.witnesses[index]) != authorization.channel_input_witness
    ):
        raise ChannelInputEscrowError("invalid finalized escrow parent")

    prevout_values = [prevout.value_sats for prevout in parent.ordered_prevouts]
    prevout_scripts = [bytes.fromhex(prevout.scriptpubkey) for prevout in parent.ordered_prevouts]
    funding_script = bytes.fromhex(prepared.terms.snapshot.funding_scriptpubkey)
    if len(funding_script) != 34 or not funding_script.startswith(b"\x51\x20"):
        raise ChannelInputEscrowError("invalid finalized escrow parent")
    taproot_tx = TaprootTx(
        inputs=[
            TxIn(txid=tx_input.txid, vout=tx_input.vout, sequence=tx_input.sequence)
            for tx_input in signed.inputs
        ],
        outputs=[
            TxOut(value=tx_output.value, scriptpubkey=tx_output.script)
            for tx_output in signed.outputs
        ],
        version=signed.version,
        locktime=signed.locktime,
    )
    sighash = taproot_sighash(taproot_tx, index, prevout_values, prevout_scripts, SIGHASH_DEFAULT)
    if not verify_schnorr(funding_script[2:], authorization.channel_input_witness[0], sighash):
        raise ChannelInputEscrowError("invalid finalized escrow parent")


def _validate_ordinary_inputs(
    signed: ParsedTransaction, prepared: PreparedChannelInputEscrow, channel_input_index: int
) -> None:
    prevouts = prepared.parent_evidence.ordered_prevouts
    if len(signed.inputs) != len(prevouts):
        raise ChannelInputEscrowError("invalid finalized escrow parent")
    for index, (prevout, witness) in enumerate(zip(prevouts, signed.witnesses, strict=True)):
        if index == channel_input_index:
            continue
        scriptpubkey = bytes.fromhex(prevout.scriptpubkey)
        if len(scriptpubkey) != 22 or not scriptpubkey.startswith(b"\x00\x14") or len(witness) != 2:
            raise ChannelInputEscrowError("invalid finalized escrow parent")
        signature, pubkey = witness
        if (
            len(pubkey) != 33
            or pubkey[0] not in (2, 3)
            or hash160(pubkey) != scriptpubkey[2:]
            or len(signature) < 2
            or signature[-1] != 1
            or not verify_p2wpkh_signature(
                signed,
                index,
                create_p2wpkh_script_code(pubkey),
                prevout.value_sats,
                signature,
                pubkey,
            )
        ):
            raise ChannelInputEscrowError("invalid finalized escrow parent")
