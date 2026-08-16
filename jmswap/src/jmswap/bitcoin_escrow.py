"""Bounded Taproot transaction primitives for channel-input escrow.

This module deliberately contains only the deterministic on-chain artifacts for
one escrow output. Lifecycle coordination and backend capabilities remain in
``channel_escrow`` and are not coupled to these cryptographic checks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from coincurve import PrivateKey, PublicKey
from jmcore.bitcoin import parse_transaction, tagged_hash
from jmcore.musig2 import (
    KeyAggContext,
    SecNonce,
    Session,
    apply_xonly_tweak,
    get_session,
    key_agg,
    nonce_agg,
    nonce_gen,
    partial_sig_agg,
    partial_sig_verify,
)
from jmcore.taproot import (
    LEAF_VERSION_TAPSCRIPT,
    OP_CHECKSIG,
    OP_EQUALVERIFY,
    OP_SHA256,
    SIGHASH_DEFAULT,
    TaprootTx,
    TapTreeNode,
    TxIn,
    TxOut,
    construct_script,
    control_block,
    sign_taproot_scriptpath,
    tapleaf_hash,
    taproot_merkle_root,
    taproot_output_script,
    taproot_sighash,
    to_xonly,
    verify_schnorr,
)

from jmswap.errors import ChannelInputEscrowError
from jmswap.spend import SwapOutpoint

_MAX_HEIGHT_LOCKTIME = 500_000_000
_TIMEOUT_SEQUENCE = 0xFFFFFFFE
_SUCCESS_SEQUENCE = 0xFFFFFFFD


def _escrow_error(message: str) -> ChannelInputEscrowError:
    return ChannelInputEscrowError(message)


def _require_compressed_pubkey(pubkey: bytes, name: str) -> None:
    if not isinstance(pubkey, bytes) or len(pubkey) != 33 or pubkey[0] not in (0x02, 0x03):
        raise ChannelInputEscrowError(f"{name} must be a 33-byte compressed public key")
    try:
        PublicKey(pubkey)
    except Exception as exc:  # noqa: BLE001 - convert primitive validation errors
        raise _escrow_error(f"{name} is not a valid public key") from exc


def _require_height(height: int) -> None:
    if not isinstance(height, int) or not 0 <= height < _MAX_HEIGHT_LOCKTIME:
        raise ChannelInputEscrowError("timeout height must be a block height below 500000000")


def _require_nonnegative(value: int, name: str) -> None:
    if not isinstance(value, int) or value < 0:
        raise ChannelInputEscrowError(f"{name} must be nonnegative")


def _require_script(script: bytes, name: str) -> None:
    if not isinstance(script, bytes) or not script:
        raise ChannelInputEscrowError(f"{name} must be a non-empty script")
    if len(script) > 10_000:
        raise ChannelInputEscrowError(f"{name} exceeds the maximum script size")

    offset = 0
    while offset < len(script):
        opcode = script[offset]
        offset += 1
        if 1 <= opcode <= 75:
            push_length = opcode
        elif opcode == 0x4C:
            if offset >= len(script):
                raise ChannelInputEscrowError(f"{name} has a truncated PUSHDATA1")
            push_length = script[offset]
            offset += 1
        elif opcode == 0x4D:
            if offset + 2 > len(script):
                raise ChannelInputEscrowError(f"{name} has a truncated PUSHDATA2")
            push_length = int.from_bytes(script[offset : offset + 2], "little")
            offset += 2
        elif opcode == 0x4E:
            if offset + 4 > len(script):
                raise ChannelInputEscrowError(f"{name} has a truncated PUSHDATA4")
            push_length = int.from_bytes(script[offset : offset + 4], "little")
            offset += 4
        else:
            continue
        if offset + push_length > len(script):
            raise ChannelInputEscrowError(f"{name} has a truncated data push")
        offset += push_length


def _require_outpoint(outpoint: SwapOutpoint, tree: ChannelInputEscrowTree) -> None:
    if not isinstance(outpoint, SwapOutpoint):
        raise ChannelInputEscrowError("escrow outpoint must be a SwapOutpoint")
    try:
        txid = bytes.fromhex(outpoint.txid)
    except (TypeError, ValueError) as exc:
        raise _escrow_error("escrow outpoint txid must be hexadecimal") from exc
    if (
        len(txid) != 32
        or not isinstance(outpoint.vout, int)
        or not 0 <= outpoint.vout <= 0xFFFFFFFF
    ):
        raise ChannelInputEscrowError("escrow outpoint is invalid")
    if not isinstance(outpoint.value, int) or outpoint.value <= 0:
        raise ChannelInputEscrowError("escrow value must be positive")
    _require_script(outpoint.scriptpubkey, "escrow funding script")
    if outpoint.scriptpubkey != tree.funding_output_script():
        raise ChannelInputEscrowError("escrow outpoint does not use the agreed funding script")


@dataclass(frozen=True)
class ChannelInputEscrowTree:
    """The single-leaf Taproot tree for one channel-input escrow output.

    Escrow aggregation is role ordered: maker first, peer second. The claim key
    is intentionally separate from the fresh MuSig2 funding keys.
    """

    maker_escrow_pubkey: bytes
    peer_escrow_pubkey: bytes
    maker_claim_pubkey: bytes
    payment_hash: bytes

    def __post_init__(self) -> None:
        _require_compressed_pubkey(self.maker_escrow_pubkey, "maker escrow key")
        _require_compressed_pubkey(self.peer_escrow_pubkey, "peer escrow key")
        _require_compressed_pubkey(self.maker_claim_pubkey, "maker claim key")
        if (
            len(
                {
                    self.maker_escrow_pubkey,
                    self.peer_escrow_pubkey,
                    self.maker_claim_pubkey,
                }
            )
            != 3
        ):
            raise ChannelInputEscrowError("escrow and claim keys must be distinct")
        if not isinstance(self.payment_hash, bytes) or len(self.payment_hash) != 32:
            raise ChannelInputEscrowError("payment hash must be exactly 32 bytes")

    @property
    def success_leaf(self) -> bytes:
        """``OP_SHA256 <payment_hash> OP_EQUALVERIFY <maker_claim> OP_CHECKSIG``."""
        try:
            return construct_script(
                [
                    OP_SHA256,
                    self.payment_hash,
                    OP_EQUALVERIFY,
                    to_xonly(self.maker_claim_pubkey),
                    OP_CHECKSIG,
                ]
            )
        except Exception as exc:  # noqa: BLE001 - primitive errors become protocol errors
            raise _escrow_error("could not construct escrow success leaf") from exc

    @property
    def tap_tree(self) -> TapTreeNode:
        return (LEAF_VERSION_TAPSCRIPT, self.success_leaf)

    def internal_xonly(self) -> bytes:
        """Role-ordered MuSig2 aggregate internal x-only key."""
        try:
            return key_agg([self.maker_escrow_pubkey, self.peer_escrow_pubkey]).aggregate_xonly()
        except Exception as exc:  # noqa: BLE001
            raise _escrow_error("could not aggregate escrow keys") from exc

    def merkle_root(self) -> bytes:
        try:
            return taproot_merkle_root(self.tap_tree)
        except Exception as exc:  # noqa: BLE001
            raise _escrow_error("could not derive escrow merkle root") from exc

    def taptweak_scalar(self) -> bytes:
        """BIP341 TapTweak committing to the sole success leaf."""
        return tagged_hash("TapTweak", self.internal_xonly() + self.merkle_root())

    def keyagg_context(self) -> KeyAggContext:
        try:
            return apply_xonly_tweak(
                key_agg([self.maker_escrow_pubkey, self.peer_escrow_pubkey]), self.taptweak_scalar()
            )
        except Exception as exc:  # noqa: BLE001
            raise _escrow_error("could not tweak escrow aggregate key") from exc

    def tweaked_aggregate_xonly(self) -> bytes:
        """The P2TR output key for a timeout key-path signature."""
        return self.keyagg_context().aggregate_xonly()

    def funding_output_script(self) -> bytes:
        try:
            return taproot_output_script(self.internal_xonly(), self.tap_tree)
        except Exception as exc:  # noqa: BLE001
            raise _escrow_error("could not derive escrow funding script") from exc

    def success_control_block(self) -> tuple[bytes, bytes]:
        """Return the fixed success leaf and its single-leaf control block."""
        try:
            return control_block(self.internal_xonly(), self.tap_tree, 0)
        except Exception as exc:  # noqa: BLE001
            raise _escrow_error("could not derive escrow control block") from exc


@dataclass(frozen=True)
class UnsignedTimeoutSpend:
    """Deterministic timeout skeleton and its BIP341 default sighash."""

    tx: TaprootTx
    sighash: bytes
    outpoint: SwapOutpoint


def parse_escrow_transaction(tx_hex: str) -> TaprootTx:
    """Parse serialized escrow transaction bytes into the shared Taproot model."""
    try:
        parsed = parse_transaction(tx_hex)
    except (TypeError, ValueError) as exc:
        raise _escrow_error("escrow transaction encoding is invalid") from exc
    if not parsed.has_witness or len(parsed.witnesses) != len(parsed.inputs):
        raise ChannelInputEscrowError("escrow transaction must use SegWit serialization")
    if any(tx_input.scriptsig for tx_input in parsed.inputs):
        raise ChannelInputEscrowError("escrow transaction inputs must have empty scriptSigs")
    return TaprootTx(
        inputs=[
            TxIn(
                txid=tx_input.txid,
                vout=tx_input.vout,
                sequence=tx_input.sequence,
            )
            for tx_input in parsed.inputs
        ],
        outputs=[
            TxOut(value=tx_output.value, scriptpubkey=tx_output.script)
            for tx_output in parsed.outputs
        ],
        version=parsed.version,
        locktime=parsed.locktime,
        witnesses=parsed.witnesses,
    )


def build_timeout_spend(
    tree: ChannelInputEscrowTree,
    outpoint: SwapOutpoint,
    maker_output_script: bytes,
    peer_output_script: bytes,
    peer_claim_sats: int,
    timeout_compensation_sats: int,
    fee_sats: int,
    timeout_height: int,
) -> UnsignedTimeoutSpend:
    """Build the exact unsigned timeout child, with maker output first."""
    _require_outpoint(outpoint, tree)
    _require_script(maker_output_script, "maker output script")
    _require_script(peer_output_script, "peer output script")
    _require_nonnegative(peer_claim_sats, "peer claim")
    _require_nonnegative(timeout_compensation_sats, "timeout compensation")
    _require_nonnegative(fee_sats, "timeout fee")
    _require_height(timeout_height)

    peer_value = peer_claim_sats + timeout_compensation_sats
    maker_value = outpoint.value - peer_value - fee_sats
    if peer_value <= 0 or maker_value <= 0:
        raise ChannelInputEscrowError("timeout outputs must both be positive")
    tx = TaprootTx(
        inputs=[TxIn(txid=outpoint.txid, vout=outpoint.vout, sequence=_TIMEOUT_SEQUENCE)],
        outputs=[
            TxOut(value=maker_value, scriptpubkey=maker_output_script),
            TxOut(value=peer_value, scriptpubkey=peer_output_script),
        ],
        version=2,
        locktime=timeout_height,
    )
    try:
        sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey], SIGHASH_DEFAULT)
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not compute timeout sighash") from exc
    return UnsignedTimeoutSpend(tx=tx, sighash=sighash, outpoint=outpoint)


def timeout_nonce(pubkey: bytes, rand: bytes | None = None) -> tuple[SecNonce, bytes]:
    """Generate a fresh MuSig2 nonce pair for one timeout participant."""
    _require_compressed_pubkey(pubkey, "timeout signer key")
    if rand is not None and (not isinstance(rand, bytes) or len(rand) != 32):
        raise ChannelInputEscrowError("timeout nonce randomness must be exactly 32 bytes")
    try:
        return nonce_gen(pubkey, rand)
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not generate timeout nonce") from exc


def timeout_session(tree: ChannelInputEscrowTree, aggnonce: bytes, sighash: bytes) -> Session:
    """Derive the shared taptweaked MuSig2 session for a timeout signature."""
    if not isinstance(sighash, bytes) or len(sighash) != 32:
        raise ChannelInputEscrowError("timeout sighash must be exactly 32 bytes")
    try:
        return get_session(
            aggnonce,
            [tree.maker_escrow_pubkey, tree.peer_escrow_pubkey],
            [tree.taptweak_scalar()],
            sighash,
        )
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not derive timeout MuSig2 session") from exc


def finalize_timeout_spend(
    tree: ChannelInputEscrowTree,
    spend: UnsignedTimeoutSpend,
    maker_pubnonce: bytes,
    peer_pubnonce: bytes,
    maker_partial_signature: bytes,
    peer_partial_signature: bytes,
) -> TaprootTx:
    """Verify both role-bound partial signatures and attach the final key-path witness."""
    if not isinstance(spend, UnsignedTimeoutSpend):
        raise ChannelInputEscrowError("timeout spend must be an UnsignedTimeoutSpend")
    _require_outpoint(spend.outpoint, tree)
    try:
        current_sighash = taproot_sighash(
            spend.tx,
            0,
            [spend.outpoint.value],
            [spend.outpoint.scriptpubkey],
            SIGHASH_DEFAULT,
        )
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not recompute timeout sighash") from exc
    if current_sighash != spend.sighash:
        raise ChannelInputEscrowError("timeout transaction changed after its sighash was computed")
    try:
        aggnonce = nonce_agg([maker_pubnonce, peer_pubnonce])
        session = timeout_session(tree, aggnonce, spend.sighash)
        maker_valid = partial_sig_verify(
            maker_partial_signature, maker_pubnonce, tree.maker_escrow_pubkey, session
        )
        peer_valid = partial_sig_verify(
            peer_partial_signature, peer_pubnonce, tree.peer_escrow_pubkey, session
        )
    except ChannelInputEscrowError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not verify timeout partial signatures") from exc
    if not maker_valid:
        raise ChannelInputEscrowError("invalid maker timeout partial signature")
    if not peer_valid:
        raise ChannelInputEscrowError("invalid peer timeout partial signature")
    try:
        signature = partial_sig_agg([maker_partial_signature, peer_partial_signature], session)
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not aggregate timeout partial signatures") from exc
    if len(signature) != 64 or not verify_schnorr(
        tree.tweaked_aggregate_xonly(), signature, spend.sighash
    ):
        raise ChannelInputEscrowError("invalid final timeout signature")
    spend.tx.witnesses = [[signature]]
    return spend.tx


def verify_timeout(
    tree: ChannelInputEscrowTree,
    outpoint: SwapOutpoint,
    maker_output_script: bytes,
    peer_output_script: bytes,
    peer_claim_sats: int,
    timeout_compensation_sats: int,
    fee_sats: int,
    timeout_height: int,
    tx: TaprootTx,
) -> bool:
    """Independently verify every deterministic field and signature of a timeout child."""
    _require_outpoint(outpoint, tree)
    _require_script(maker_output_script, "maker output script")
    _require_script(peer_output_script, "peer output script")
    _require_nonnegative(peer_claim_sats, "peer claim")
    _require_nonnegative(timeout_compensation_sats, "timeout compensation")
    _require_nonnegative(fee_sats, "timeout fee")
    _require_height(timeout_height)
    if not isinstance(tx, TaprootTx):
        raise ChannelInputEscrowError("timeout transaction must be a TaprootTx")
    if tx.version != 2 or tx.locktime != timeout_height:
        raise ChannelInputEscrowError("timeout transaction version or locktime does not match")
    if len(tx.inputs) != 1 or len(tx.outputs) != 2:
        raise ChannelInputEscrowError("timeout transaction must have one input and two outputs")
    txin = tx.inputs[0]
    if (
        txin.txid != outpoint.txid
        or txin.vout != outpoint.vout
        or txin.sequence != _TIMEOUT_SEQUENCE
    ):
        raise ChannelInputEscrowError(
            "timeout transaction does not spend the exact escrow outpoint"
        )
    peer_value = peer_claim_sats + timeout_compensation_sats
    maker_value = outpoint.value - peer_value - fee_sats
    if peer_value <= 0 or maker_value <= 0:
        raise ChannelInputEscrowError("timeout outputs must both be positive")
    maker_output, peer_output = tx.outputs
    if maker_output.value != maker_value or maker_output.scriptpubkey != maker_output_script:
        raise ChannelInputEscrowError("timeout maker output does not match")
    if peer_output.value != peer_value or peer_output.scriptpubkey != peer_output_script:
        raise ChannelInputEscrowError("timeout peer output does not match")
    if len(tx.witnesses) != 1 or len(tx.witnesses[0]) != 1 or len(tx.witnesses[0][0]) != 64:
        raise ChannelInputEscrowError("timeout witness must contain one default Schnorr signature")
    try:
        sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey], SIGHASH_DEFAULT)
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not recompute timeout sighash") from exc
    if not verify_schnorr(tree.tweaked_aggregate_xonly(), tx.witnesses[0][0], sighash):
        raise ChannelInputEscrowError("timeout Schnorr signature does not verify")
    return True


def build_success_spend(
    tree: ChannelInputEscrowTree,
    outpoint: SwapOutpoint,
    preimage: bytes,
    maker_claim_privkey: bytes,
    maker_output_script: bytes,
    fee_sats: int,
) -> TaprootTx:
    """Build a script-path success self-sweep controlled by the maker claim key."""
    _require_outpoint(outpoint, tree)
    _require_script(maker_output_script, "maker output script")
    _require_nonnegative(fee_sats, "success fee")
    if not isinstance(preimage, bytes) or len(preimage) != 32:
        raise ChannelInputEscrowError("success preimage must be exactly 32 bytes")
    if hashlib.sha256(preimage).digest() != tree.payment_hash:
        raise ChannelInputEscrowError("success preimage does not match payment hash")
    try:
        claim_key = PrivateKey(maker_claim_privkey).public_key.format(compressed=True)
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("maker claim private key is invalid") from exc
    if claim_key != tree.maker_claim_pubkey:
        raise ChannelInputEscrowError("maker claim private key does not match the escrow tree")
    output_value = outpoint.value - fee_sats
    if output_value <= 0:
        raise ChannelInputEscrowError("success output must be positive")
    tx = TaprootTx(
        inputs=[TxIn(txid=outpoint.txid, vout=outpoint.vout, sequence=_SUCCESS_SEQUENCE)],
        outputs=[TxOut(value=output_value, scriptpubkey=maker_output_script)],
        version=2,
        locktime=0,
    )
    leaf, control = tree.success_control_block()
    try:
        signature = sign_taproot_scriptpath(
            tx,
            0,
            [outpoint.value],
            [outpoint.scriptpubkey],
            leaf,
            maker_claim_privkey,
            sighash_type=SIGHASH_DEFAULT,
        )
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not sign success spend") from exc
    tx.witnesses = [[signature, preimage, leaf, control]]
    return tx


def verify_success_spend(
    tree: ChannelInputEscrowTree,
    outpoint: SwapOutpoint,
    maker_output_script: bytes,
    fee_sats: int,
    tx: TaprootTx,
) -> bool:
    """Independently verify the exact success script-path spend and its signature."""
    _require_outpoint(outpoint, tree)
    _require_script(maker_output_script, "maker output script")
    _require_nonnegative(fee_sats, "success fee")
    if not isinstance(tx, TaprootTx):
        raise ChannelInputEscrowError("success transaction must be a TaprootTx")
    if tx.version != 2 or tx.locktime != 0:
        raise ChannelInputEscrowError("success transaction version or locktime does not match")
    if len(tx.inputs) != 1 or len(tx.outputs) != 1:
        raise ChannelInputEscrowError("success transaction must have one input and one output")
    txin = tx.inputs[0]
    if (
        txin.txid != outpoint.txid
        or txin.vout != outpoint.vout
        or txin.sequence != _SUCCESS_SEQUENCE
    ):
        raise ChannelInputEscrowError(
            "success transaction does not spend the exact escrow outpoint"
        )
    output_value = outpoint.value - fee_sats
    if output_value <= 0:
        raise ChannelInputEscrowError("success output must be positive")
    output = tx.outputs[0]
    if output.value != output_value or output.scriptpubkey != maker_output_script:
        raise ChannelInputEscrowError("success output does not match")
    if len(tx.witnesses) != 1 or len(tx.witnesses[0]) != 4:
        raise ChannelInputEscrowError("success witness has the wrong shape")
    signature, preimage, leaf, control = tx.witnesses[0]
    expected_leaf, expected_control = tree.success_control_block()
    if len(signature) != 64:
        raise ChannelInputEscrowError("success witness must use SIGHASH_DEFAULT")
    if not isinstance(preimage, bytes) or len(preimage) != 32:
        raise ChannelInputEscrowError("success witness preimage must be exactly 32 bytes")
    if hashlib.sha256(preimage).digest() != tree.payment_hash:
        raise ChannelInputEscrowError("success preimage does not match payment hash")
    if leaf != expected_leaf or control != expected_control:
        raise ChannelInputEscrowError("success leaf or control block does not match escrow tree")
    try:
        sighash = taproot_sighash(
            tx,
            0,
            [outpoint.value],
            [outpoint.scriptpubkey],
            SIGHASH_DEFAULT,
            leaf_hash=tapleaf_hash(expected_leaf),
        )
    except Exception as exc:  # noqa: BLE001
        raise _escrow_error("could not recompute success sighash") from exc
    if not verify_schnorr(to_xonly(tree.maker_claim_pubkey), signature, sighash):
        raise ChannelInputEscrowError("success maker claim signature does not verify")
    return True
