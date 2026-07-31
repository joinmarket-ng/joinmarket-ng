from __future__ import annotations

from dataclasses import dataclass

from coincurve import PrivateKey, PublicKeyXOnly
from jmcore.bitcoin import (
    NetworkType,
    TxInput,
    TxOutput,
    get_txid,
    parse_transaction,
    scriptpubkey_to_address,
    serialize_transaction,
)
from jmcore.taproot import (
    TaprootTx,
    TxIn,
    TxOut,
    sign_taproot_scriptpath,
    taproot_sighash,
)

from jmswap.contract import NLOCKTIME_BLOCKHEIGHT_MAX, SwapLeaf, TaprootSwapContract
from jmswap.fees import DUST_THRESHOLD, cooperative_fee_bounds
from jmswap.models import FundingOutput

COOPERATIVE_SEQUENCE = 0xFFFFFFFF - 2


@dataclass(frozen=True)
class ValidatedCooperativeTransaction:
    raw_unsigned: str
    tx: TaprootTx
    message: bytes
    output_address: str
    output_value: int


def validate_cooperative_transaction(
    raw_transaction: object,
    *,
    funding: FundingOutput,
    network: str | NetworkType,
    expected_destination: str | None = None,
    expected_value: int | None = None,
    max_locktime: int | None = None,
) -> ValidatedCooperativeTransaction:
    if (
        not isinstance(raw_transaction, str)
        or not raw_transaction
        or len(raw_transaction) > 200_000
        or len(raw_transaction) % 2
    ):
        raise ValueError("invalid cooperative transaction")
    try:
        parsed = parse_transaction(raw_transaction)
    except (ValueError, IndexError) as exc:
        raise ValueError("invalid cooperative transaction") from exc
    if parsed.version != 2 or len(parsed.inputs) != 1 or len(parsed.outputs) != 1:
        raise ValueError("cooperative transaction shape is not canonical")
    if parsed.locktime > NLOCKTIME_BLOCKHEIGHT_MAX or (
        max_locktime is not None and parsed.locktime > max_locktime
    ):
        raise ValueError("cooperative transaction locktime is not final")
    txin = parsed.inputs[0]
    canonical = serialize_transaction(
        parsed.version, parsed.inputs, parsed.outputs, parsed.locktime
    ).hex()
    if (
        txin.scriptsig
        or parsed.has_witness
        or parsed.witnesses
        or txin.sequence != COOPERATIVE_SEQUENCE
        or canonical != raw_transaction
    ):
        raise ValueError("cooperative transaction input is not unsigned and canonical")
    if txin.txid != funding.txid or txin.vout != funding.vout:
        raise ValueError("cooperative input does not match funding output")
    output = parsed.outputs[0]
    try:
        destination = scriptpubkey_to_address(output.script, network)
    except ValueError as exc:
        raise ValueError("invalid cooperative destination") from exc
    minimum_fee, maximum_fee = cooperative_fee_bounds()
    fee = funding.value - output.value
    if (
        output.value < DUST_THRESHOLD
        or (expected_destination is not None and destination != expected_destination)
        or (expected_value is not None and output.value != expected_value)
        or not minimum_fee <= fee <= maximum_fee
    ):
        raise ValueError("invalid cooperative transaction destination, amount, or fee")
    tx = TaprootTx(
        inputs=[TxIn(txid=txin.txid, vout=txin.vout, sequence=txin.sequence)],
        outputs=[TxOut(value=output.value, scriptpubkey=output.script)],
        version=parsed.version,
        locktime=parsed.locktime,
    )
    message = taproot_sighash(tx, 0, [funding.value], [bytes.fromhex(funding.script_pubkey)])
    return ValidatedCooperativeTransaction(raw_transaction, tx, message, destination, output.value)


def build_unsigned_cooperative_transaction(
    funding: FundingOutput,
    *,
    destination_script: bytes,
    value: int,
    locktime: int = 0,
) -> str:
    tx = TaprootTx(
        inputs=[TxIn(funding.txid, funding.vout, COOPERATIVE_SEQUENCE)],
        outputs=[TxOut(value, destination_script)],
        locktime=locktime,
    )
    return serialize_transaction(
        tx.version,
        [TxInput.from_hex(funding.txid, funding.vout, sequence=COOPERATIVE_SEQUENCE)],
        [TxOutput(value, destination_script)],
        tx.locktime,
    ).hex()


def add_keypath_witness(raw_unsigned: str, signature: bytes) -> str:
    parsed = parse_transaction(raw_unsigned)
    return serialize_transaction(
        parsed.version, parsed.inputs, parsed.outputs, parsed.locktime, [[signature]]
    ).hex()


def validate_final_cooperative_transaction(
    raw_signed: str,
    *,
    raw_unsigned: str,
    contract: TaprootSwapContract,
    message: bytes,
    expected_signature: bytes | None = None,
) -> str:
    parsed = parse_transaction(raw_signed)
    identity = serialize_transaction(
        parsed.version, parsed.inputs, parsed.outputs, parsed.locktime
    ).hex()
    if identity != raw_unsigned or len(parsed.witnesses) != 1 or len(parsed.witnesses[0]) != 1:
        raise ValueError("cooperative final transaction identity or witness is invalid")
    signature = parsed.witnesses[0][0]
    if len(signature) != 64 or (expected_signature is not None and signature != expected_signature):
        raise ValueError("cooperative final signature is invalid")
    if not PublicKeyXOnly(contract.output_script[2:]).verify(signature, message):
        raise ValueError("cooperative final signature is invalid")
    return get_txid(raw_signed)


def build_script_path_spend(
    contract: TaprootSwapContract,
    funding: FundingOutput,
    *,
    destination_script: bytes,
    output_value: int,
    local_seckey: bytes,
    leaf: SwapLeaf,
    preimage: bytes | None = None,
) -> str:
    """Build a unilateral claim/refund solely from validated contract data."""
    script, control = contract.script_path(leaf)
    local_pubkey = PrivateKey(local_seckey).public_key.format(compressed=True)
    expected_pubkey = contract.claim_pubkey if leaf is SwapLeaf.CLAIM else contract.refund_pubkey
    if local_pubkey[1:] != expected_pubkey[1:]:
        raise ValueError("local key does not own the selected swap leaf")
    if not DUST_THRESHOLD <= output_value < funding.value:
        raise ValueError("fallback output value or fee is invalid")
    locktime = contract.locktime if leaf is SwapLeaf.REFUND else 0
    sequence = COOPERATIVE_SEQUENCE if leaf is SwapLeaf.REFUND else 1
    tx = TaprootTx(
        inputs=[TxIn(funding.txid, funding.vout, sequence)],
        outputs=[TxOut(output_value, destination_script)],
        locktime=locktime,
    )
    signature = sign_taproot_scriptpath(
        tx,
        0,
        [funding.value],
        [bytes.fromhex(funding.script_pubkey)],
        script,
        local_seckey,
    )
    witness = [signature]
    if leaf is SwapLeaf.CLAIM:
        if preimage is None or len(preimage) != 32:
            raise ValueError("claim requires the 32-byte preimage")
        from jmcore.bitcoin import sha256

        if sha256(preimage) != contract.payment_hash:
            raise ValueError("claim preimage does not match payment hash")
        witness.append(preimage)
    witness.extend((script, control))
    tx.witnesses = [witness]
    return tx.serialize().hex()
