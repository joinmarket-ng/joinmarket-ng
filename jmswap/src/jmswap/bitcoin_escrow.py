"""Deterministic Taproot primitives for one private channel-buyout escrow output.

The buyout escrow is a single Taproot output (the buyer's CoinJoin change) with
a role-ordered MuSig2 internal key over ``[K_B, K_C]`` and one tapscript leaf
that pays the buyer against a SHA256 preimage:

```text
internal_key = MuSig2.KeyAgg([K_B, K_C])       (buyer first, counterparty second)
leaf         = OP_SHA256 <payment_hash> OP_EQUALVERIFY <xonly(K_B_claim)> OP_CHECKSIG
```

Three spends exist and all of them are produced here byte for byte:

* the presigned **split**, a key-path spend with a BIP68 block-based relative
  lock that pays the counterparty if the buyer never settles,
* the unilateral **claim**, a script-path spend revealing the preimage, and
* the cooperative **sweep**, a key-path spend used after settlement.

This module contains no lifecycle, transport, or channel-backend logic. In
particular it does not enforce that the counterparty holds a complete signed
split before releasing its parent partial signature; that ordering rule belongs
to the protocol layer and is only made checkable here, by
:func:`verify_signed_split`, which validates a serialized split independently of
whatever mutable objects produced it.

Signed artifacts leave this module as serialized transaction bytes. Unsigned
artifacts are :class:`UnsignedKeyPathSpend` skeletons whose ``TaprootTx`` stays
mutable, so the skeleton is carried together with the sighash it was measured
at and that sighash is recomputed before any signature is trusted; every
verifier re-derives the expected transaction from its parameters and compares
the serialized bytes. A mutated skeleton therefore cannot be mistaken for an
agreed one. Verification here is cryptographic and structural only; it never
proves that a transaction would be accepted by consensus or by a node's policy
rules.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from jmcore.bitcoin import parse_transaction_bytes, tagged_hash
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
    xonly_from_privkey,
)

# BIP68 block-based relative lock bounds for the split (JMP-0011).
MIN_CSV_DELAY = 144
MAX_CSV_DELAY = 2016

# Either split output must remain able to pay for a child fee bump.
MIN_SPLIT_OUTPUT_SATS = 1000

# Claim and cooperative sweep signal BIP125 replaceability and disable the
# relative lock (bit 31 set), so they are spendable as soon as the parent
# confirms and can be fee-bumped until they do.
SWEEP_SEQUENCE = 0xFFFFFFFD

MAX_MONEY_SATS = 21_000_000 * 100_000_000

_P2TR_SCRIPT_LENGTH = 34


class ChannelBuyoutEscrowError(Exception):
    """A buyout escrow artifact is malformed or does not match the agreement."""


def _error(message: str) -> ChannelBuyoutEscrowError:
    return ChannelBuyoutEscrowError(message)


def _require_compressed_pubkey(pubkey: bytes, name: str) -> bytes:
    """Return the x-only form of a fully valid 33-byte compressed pubkey."""
    if type(pubkey) is not bytes or len(pubkey) != 33 or pubkey[0] not in (0x02, 0x03):
        raise _error(f"{name} must be a 33-byte compressed public key")
    try:
        return to_xonly(pubkey)
    except Exception as exc:  # noqa: BLE001 - primitive errors become escrow errors
        raise _error(f"{name} is not a valid public key") from exc


def _require_amount(value: int, name: str) -> int:
    if type(value) is not int or not 0 <= value <= MAX_MONEY_SATS:
        raise _error(f"{name} must be an integer between 0 and {MAX_MONEY_SATS} satoshis")
    return value


def _require_p2tr_script(script: bytes, name: str) -> bytes:
    if (
        type(script) is not bytes
        or len(script) != _P2TR_SCRIPT_LENGTH
        or script[0] != 0x51
        or script[1] != 0x20
    ):
        raise _error(f"{name} must be a native P2TR scriptPubKey")
    return script


def _require_csv_delay(csv_delay: int) -> int:
    if type(csv_delay) is not int or not MIN_CSV_DELAY <= csv_delay <= MAX_CSV_DELAY:
        raise _error(
            f"csv delay must be a block count from {MIN_CSV_DELAY} through {MAX_CSV_DELAY}"
        )
    return csv_delay


def _require_32_bytes(value: bytes, name: str) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise _error(f"{name} must be exactly 32 bytes")
    return value


@dataclass(frozen=True)
class EscrowOutpoint:
    """The confirmed escrow output: where it is, what it is worth, what it pays to."""

    txid: str
    vout: int
    value: int
    scriptpubkey: bytes

    def __post_init__(self) -> None:
        if type(self.txid) is not str or len(self.txid) != 64:
            raise _error("escrow txid must be 64 hexadecimal characters")
        if any(character not in "0123456789abcdef" for character in self.txid):
            raise _error(
                "escrow txid must be lowercase hexadecimal, without whitespace or a 0x prefix"
            )
        if type(self.vout) is not int or not 0 <= self.vout <= 0xFFFFFFFF:
            raise _error("escrow vout must be a 32-bit unsigned integer")
        if type(self.value) is not int or not 0 < self.value <= MAX_MONEY_SATS:
            raise _error("escrow value must be a positive satoshi amount")
        _require_p2tr_script(self.scriptpubkey, "escrow scriptPubKey")


@dataclass(frozen=True)
class BuyoutEscrow:
    """The single-leaf Taproot escrow agreed for one buyout attempt.

    Key aggregation is role ordered (buyer first, counterparty second) and the
    buyer's claim key is deliberately distinct from its escrow key, so revealing
    the preimage on chain never exposes a MuSig2 share.
    """

    buyer_escrow_pubkey: bytes
    counterparty_escrow_pubkey: bytes
    buyer_claim_pubkey: bytes
    payment_hash: bytes

    def __post_init__(self) -> None:
        # Distinctness is checked on the x-only forms: two compressed keys with
        # opposite parity share one x-only key, and both the claim leaf and the
        # Taproot output key only ever see the x-only form.
        xonly_keys = {
            _require_compressed_pubkey(self.buyer_escrow_pubkey, "buyer escrow key"),
            _require_compressed_pubkey(self.counterparty_escrow_pubkey, "counterparty escrow key"),
            _require_compressed_pubkey(self.buyer_claim_pubkey, "buyer claim key"),
        }
        if len(xonly_keys) != 3:
            raise _error("escrow and claim keys must be distinct, including in their x-only form")
        _require_32_bytes(self.payment_hash, "payment hash")

    @property
    def claim_leaf(self) -> bytes:
        """``OP_SHA256 <payment_hash> OP_EQUALVERIFY <xonly(K_B_claim)> OP_CHECKSIG``."""
        try:
            return construct_script(
                [
                    OP_SHA256,
                    self.payment_hash,
                    OP_EQUALVERIFY,
                    to_xonly(self.buyer_claim_pubkey),
                    OP_CHECKSIG,
                ]
            )
        except Exception as exc:  # noqa: BLE001
            raise _error("could not construct the escrow claim leaf") from exc

    @property
    def tap_tree(self) -> TapTreeNode:
        return (LEAF_VERSION_TAPSCRIPT, self.claim_leaf)

    def internal_xonly(self) -> bytes:
        """Role-ordered MuSig2 aggregate internal key, x-only."""
        try:
            return key_agg(
                [self.buyer_escrow_pubkey, self.counterparty_escrow_pubkey]
            ).aggregate_xonly()
        except Exception as exc:  # noqa: BLE001
            raise _error("could not aggregate the escrow keys") from exc

    def merkle_root(self) -> bytes:
        try:
            return taproot_merkle_root(self.tap_tree)
        except Exception as exc:  # noqa: BLE001
            raise _error("could not derive the escrow merkle root") from exc

    def taptweak_scalar(self) -> bytes:
        """BIP341 TapTweak committing to the sole claim leaf."""
        return tagged_hash("TapTweak", self.internal_xonly() + self.merkle_root())

    def keyagg_context(self) -> KeyAggContext:
        """MuSig2 context already tweaked to the Taproot output key."""
        try:
            return apply_xonly_tweak(
                key_agg([self.buyer_escrow_pubkey, self.counterparty_escrow_pubkey]),
                self.taptweak_scalar(),
            )
        except Exception as exc:  # noqa: BLE001
            raise _error("could not tweak the escrow aggregate key") from exc

    def output_xonly(self) -> bytes:
        """The Taproot output key that key-path spends must verify against."""
        return self.keyagg_context().aggregate_xonly()

    def output_script(self) -> bytes:
        """``OP_1 <xonly(output_key)>``: the buyer's CoinJoin change script."""
        try:
            return taproot_output_script(self.internal_xonly(), self.tap_tree)
        except Exception as exc:  # noqa: BLE001
            raise _error("could not derive the escrow output script") from exc

    def claim_control_block(self) -> tuple[bytes, bytes]:
        """Return the claim leaf and its single-leaf control block."""
        try:
            return control_block(self.internal_xonly(), self.tap_tree, 0)
        except Exception as exc:  # noqa: BLE001
            raise _error("could not derive the escrow control block") from exc


@dataclass(frozen=True)
class UnsignedKeyPathSpend:
    """An unsigned key-path child bound to the sighash it was measured at.

    ``tx`` stays mutable because witnesses are attached at finalization, so the
    sighash is carried alongside and re-checked before any signature is trusted.
    """

    tx: TaprootTx
    sighash: bytes
    outpoint: EscrowOutpoint


def _require_escrow_outpoint(escrow: BuyoutEscrow, outpoint: EscrowOutpoint) -> None:
    if not isinstance(outpoint, EscrowOutpoint):
        raise _error("escrow outpoint must be an EscrowOutpoint")
    if outpoint.scriptpubkey != escrow.output_script():
        raise _error("escrow outpoint does not pay to the agreed escrow script")


def _key_path_sighash(tx: TaprootTx, outpoint: EscrowOutpoint) -> bytes:
    try:
        return taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey], SIGHASH_DEFAULT)
    except Exception as exc:  # noqa: BLE001
        raise _error("could not compute the escrow key-path sighash") from exc


def _split_skeleton(
    escrow: BuyoutEscrow,
    outpoint: EscrowOutpoint,
    buyer_output_script: bytes,
    counterparty_output_script: bytes,
    counterparty_value_sats: int,
    fee_sats: int,
    csv_delay: int,
) -> TaprootTx:
    _require_escrow_outpoint(escrow, outpoint)
    _require_p2tr_script(buyer_output_script, "buyer split script")
    _require_p2tr_script(counterparty_output_script, "counterparty split script")
    _require_amount(counterparty_value_sats, "counterparty split value")
    _require_amount(fee_sats, "split fee")
    _require_csv_delay(csv_delay)

    buyer_value_sats = outpoint.value - counterparty_value_sats - fee_sats
    if buyer_value_sats < MIN_SPLIT_OUTPUT_SATS or counterparty_value_sats < MIN_SPLIT_OUTPUT_SATS:
        raise _error(f"both split outputs must be at least {MIN_SPLIT_OUTPUT_SATS} satoshis")
    if buyer_value_sats + counterparty_value_sats + fee_sats != outpoint.value:
        raise _error("split outputs and fee must sum to the escrow value")
    return TaprootTx(
        inputs=[TxIn(txid=outpoint.txid, vout=outpoint.vout, sequence=csv_delay)],
        outputs=[
            TxOut(value=buyer_value_sats, scriptpubkey=buyer_output_script),
            TxOut(value=counterparty_value_sats, scriptpubkey=counterparty_output_script),
        ],
        version=2,
        locktime=0,
    )


def _sweep_skeleton(
    escrow: BuyoutEscrow,
    outpoint: EscrowOutpoint,
    buyer_output_script: bytes,
    fee_sats: int,
) -> TaprootTx:
    _require_escrow_outpoint(escrow, outpoint)
    _require_p2tr_script(buyer_output_script, "buyer sweep script")
    _require_amount(fee_sats, "sweep fee")
    output_value = outpoint.value - fee_sats
    if output_value < MIN_SPLIT_OUTPUT_SATS:
        raise _error(f"sweep output must be at least {MIN_SPLIT_OUTPUT_SATS} satoshis")
    return TaprootTx(
        inputs=[TxIn(txid=outpoint.txid, vout=outpoint.vout, sequence=SWEEP_SEQUENCE)],
        outputs=[TxOut(value=output_value, scriptpubkey=buyer_output_script)],
        version=2,
        locktime=0,
    )


def build_split(
    escrow: BuyoutEscrow,
    outpoint: EscrowOutpoint,
    buyer_output_script: bytes,
    counterparty_output_script: bytes,
    counterparty_value_sats: int,
    fee_sats: int,
    csv_delay: int,
) -> UnsignedKeyPathSpend:
    """Build the exact unsigned split: buyer output first, counterparty second.

    The buyer value is derived as ``escrow value - counterparty value - fee``, so
    the split always conserves the escrow value exactly. The input carries the
    BIP68 block-based relative lock and nLockTime stays 0.
    """
    tx = _split_skeleton(
        escrow,
        outpoint,
        buyer_output_script,
        counterparty_output_script,
        counterparty_value_sats,
        fee_sats,
        csv_delay,
    )
    return UnsignedKeyPathSpend(tx=tx, sighash=_key_path_sighash(tx, outpoint), outpoint=outpoint)


def build_cooperative_sweep(
    escrow: BuyoutEscrow,
    outpoint: EscrowOutpoint,
    buyer_output_script: bytes,
    fee_sats: int,
) -> UnsignedKeyPathSpend:
    """Build the unsigned post-settlement key-path sweep of the whole escrow."""
    tx = _sweep_skeleton(escrow, outpoint, buyer_output_script, fee_sats)
    return UnsignedKeyPathSpend(tx=tx, sighash=_key_path_sighash(tx, outpoint), outpoint=outpoint)


def escrow_nonce(
    pubkey: bytes,
    rand: bytes | None = None,
    *,
    privkey: bytes,
    sighash: bytes,
) -> tuple[SecNonce, bytes]:
    """Generate one participant's single-use MuSig2 nonce pair for an escrow spend.

    The signing key and the message are mandatory so that the nonce is derived
    from them in addition to ``rand``, as BIP327 recommends: an accidental
    repetition of ``rand`` alone then still yields different nonces for
    different messages. ``privkey`` must belong to ``pubkey`` and ``sighash``
    must be the exact key-path sighash that will be signed.

    This is a defense in depth, not a licence to reuse randomness. ``rand`` must
    still be fresh secret randomness for every call, including calls that are
    later abandoned; reusing it for the same key and message repeats the nonce
    and leaks the private key. Omit it to use operating-system randomness.
    """
    _require_compressed_pubkey(pubkey, "escrow signer key")
    if rand is not None:
        _require_32_bytes(rand, "escrow nonce randomness")
    _require_32_bytes(privkey, "escrow signer private key")
    _require_32_bytes(sighash, "escrow sighash")
    try:
        return nonce_gen(pubkey, rand, privkey=privkey, msg32=sighash)
    except Exception as exc:  # noqa: BLE001
        raise _error(f"could not generate an escrow nonce: {exc}") from exc


def escrow_session(escrow: BuyoutEscrow, aggnonce: bytes, sighash: bytes) -> Session:
    """Derive the taptweaked, role-ordered MuSig2 session for a key-path sighash."""
    _require_32_bytes(sighash, "escrow sighash")
    try:
        return get_session(
            aggnonce,
            [escrow.buyer_escrow_pubkey, escrow.counterparty_escrow_pubkey],
            [escrow.taptweak_scalar()],
            sighash,
        )
    except Exception as exc:  # noqa: BLE001
        raise _error("could not derive the escrow MuSig2 session") from exc


def finalize_key_path_spend(
    escrow: BuyoutEscrow,
    spend: UnsignedKeyPathSpend,
    buyer_pubnonce: bytes,
    counterparty_pubnonce: bytes,
    buyer_partial_signature: bytes,
    counterparty_partial_signature: bytes,
) -> bytes:
    """Verify both role-bound partials and return the serialized signed spend.

    The skeleton's sighash is recomputed first, so a transaction mutated after
    the partial signatures were produced is rejected instead of signed over.
    """
    if not isinstance(spend, UnsignedKeyPathSpend):
        raise _error("spend must be an UnsignedKeyPathSpend")
    _require_escrow_outpoint(escrow, spend.outpoint)
    if _key_path_sighash(spend.tx, spend.outpoint) != spend.sighash:
        raise _error("transaction changed after its sighash was computed")
    try:
        aggnonce = nonce_agg([buyer_pubnonce, counterparty_pubnonce])
    except Exception as exc:  # noqa: BLE001
        raise _error("could not aggregate the escrow nonces") from exc
    session = escrow_session(escrow, aggnonce, spend.sighash)
    if not partial_sig_verify(
        buyer_partial_signature, buyer_pubnonce, escrow.buyer_escrow_pubkey, session
    ):
        raise _error("invalid buyer partial signature")
    if not partial_sig_verify(
        counterparty_partial_signature,
        counterparty_pubnonce,
        escrow.counterparty_escrow_pubkey,
        session,
    ):
        raise _error("invalid counterparty partial signature")
    try:
        signature = partial_sig_agg(
            [buyer_partial_signature, counterparty_partial_signature], session
        )
    except Exception as exc:  # noqa: BLE001
        raise _error("could not aggregate the escrow partial signatures") from exc
    if not verify_schnorr(escrow.output_xonly(), signature, spend.sighash):
        raise _error("aggregated signature does not verify against the escrow output key")
    signed = TaprootTx(
        inputs=list(spend.tx.inputs),
        outputs=list(spend.tx.outputs),
        version=spend.tx.version,
        locktime=spend.tx.locktime,
        witnesses=[[signature]],
    )
    return signed.serialize()


def build_claim(
    escrow: BuyoutEscrow,
    outpoint: EscrowOutpoint,
    preimage: bytes,
    buyer_claim_privkey: bytes,
    buyer_output_script: bytes,
    fee_sats: int,
) -> bytes:
    """Build the buyer's unilateral script-path claim, serialized and signed.

    The buyer needs no counterparty cooperation: it reveals the preimage and
    signs the claim leaf with its claim key.
    """
    tx = _sweep_skeleton(escrow, outpoint, buyer_output_script, fee_sats)
    _require_32_bytes(preimage, "claim preimage")
    if hashlib.sha256(preimage).digest() != escrow.payment_hash:
        raise _error("claim preimage does not match the payment hash")
    _require_32_bytes(buyer_claim_privkey, "buyer claim private key")
    try:
        claim_xonly = xonly_from_privkey(buyer_claim_privkey)
    except Exception as exc:  # noqa: BLE001
        raise _error("buyer claim private key is invalid") from exc
    if claim_xonly != to_xonly(escrow.buyer_claim_pubkey):
        raise _error("buyer claim private key does not match the escrow")
    leaf, control = escrow.claim_control_block()
    try:
        signature = sign_taproot_scriptpath(
            tx,
            0,
            [outpoint.value],
            [outpoint.scriptpubkey],
            leaf,
            buyer_claim_privkey,
            sighash_type=SIGHASH_DEFAULT,
        )
    except Exception as exc:  # noqa: BLE001
        raise _error("could not sign the escrow claim") from exc
    tx.witnesses = [[signature, preimage, leaf, control]]
    return tx.serialize()


def _parse_signed_spend(signed_tx: bytes, expected_outputs: int) -> tuple[TaprootTx, list[bytes]]:
    """Parse a serialized signed escrow child into a skeleton plus its witness."""
    if type(signed_tx) is not bytes or not signed_tx:
        raise _error("signed transaction must be non-empty bytes")
    try:
        parsed = parse_transaction_bytes(signed_tx)
    except Exception as exc:  # noqa: BLE001
        raise _error("signed transaction encoding is invalid") from exc
    if not parsed.has_witness or len(parsed.witnesses) != len(parsed.inputs):
        raise _error("signed transaction must use SegWit serialization")
    if len(parsed.inputs) != 1 or len(parsed.outputs) != expected_outputs:
        raise _error(f"signed transaction must have one input and {expected_outputs} output(s)")
    if parsed.inputs[0].scriptsig:
        raise _error("signed transaction input must have an empty scriptSig")
    tx = TaprootTx(
        inputs=[
            TxIn(
                txid=parsed.inputs[0].txid,
                vout=parsed.inputs[0].vout,
                sequence=parsed.inputs[0].sequence,
            )
        ],
        outputs=[TxOut(value=out.value, scriptpubkey=out.script) for out in parsed.outputs],
        version=parsed.version,
        locktime=parsed.locktime,
    )
    return tx, list(parsed.witnesses[0])


def _require_exact_serialization(
    expected: TaprootTx, witness: list[bytes], signed_tx: bytes, name: str
) -> None:
    """Reject anything that is not byte-for-byte the expected signed transaction."""
    rebuilt = TaprootTx(
        inputs=list(expected.inputs),
        outputs=list(expected.outputs),
        version=expected.version,
        locktime=expected.locktime,
        witnesses=[list(witness)],
    )
    if rebuilt.serialize() != signed_tx:
        raise _error(f"{name} does not match the agreed transaction byte for byte")


def _verify_key_path_witness(
    escrow: BuyoutEscrow,
    expected: TaprootTx,
    outpoint: EscrowOutpoint,
    witness: list[bytes],
    signed_tx: bytes,
    name: str,
) -> None:
    if len(witness) != 1 or len(witness[0]) != 64:
        raise _error(f"{name} witness must hold exactly one SIGHASH_DEFAULT signature")
    _require_exact_serialization(expected, witness, signed_tx, name)
    if not verify_schnorr(escrow.output_xonly(), witness[0], _key_path_sighash(expected, outpoint)):
        raise _error(f"{name} signature does not verify against the escrow output key")


def verify_signed_split(
    escrow: BuyoutEscrow,
    outpoint: EscrowOutpoint,
    buyer_output_script: bytes,
    counterparty_output_script: bytes,
    counterparty_value_sats: int,
    fee_sats: int,
    csv_delay: int,
    signed_tx: bytes,
) -> bool:
    """Independently validate a serialized signed split against the agreement.

    Everything is re-derived from the agreed parameters: outpoint, relative lock,
    output order, conserved values, and the aggregated key-path signature. This
    is the check the counterparty must pass before it releases anything that
    depends on the split existing.
    """
    expected = _split_skeleton(
        escrow,
        outpoint,
        buyer_output_script,
        counterparty_output_script,
        counterparty_value_sats,
        fee_sats,
        csv_delay,
    )
    tx, witness = _parse_signed_spend(signed_tx, expected_outputs=2)
    _compare_skeleton(expected, tx, "split")
    _verify_key_path_witness(escrow, expected, outpoint, witness, signed_tx, "split")
    return True


def verify_signed_cooperative_sweep(
    escrow: BuyoutEscrow,
    outpoint: EscrowOutpoint,
    buyer_output_script: bytes,
    fee_sats: int,
    signed_tx: bytes,
) -> bool:
    """Independently validate a serialized signed cooperative key-path sweep."""
    expected = _sweep_skeleton(escrow, outpoint, buyer_output_script, fee_sats)
    tx, witness = _parse_signed_spend(signed_tx, expected_outputs=1)
    _compare_skeleton(expected, tx, "cooperative sweep")
    _verify_key_path_witness(escrow, expected, outpoint, witness, signed_tx, "cooperative sweep")
    return True


def verify_signed_claim(
    escrow: BuyoutEscrow,
    outpoint: EscrowOutpoint,
    buyer_output_script: bytes,
    fee_sats: int,
    signed_tx: bytes,
) -> bool:
    """Independently validate a serialized signed script-path claim.

    The witness must reveal a preimage of the payment hash, use exactly the
    escrow's claim leaf and control block, and carry a claim-key signature over
    the BIP342 sighash of this exact transaction.
    """
    expected = _sweep_skeleton(escrow, outpoint, buyer_output_script, fee_sats)
    tx, witness = _parse_signed_spend(signed_tx, expected_outputs=1)
    _compare_skeleton(expected, tx, "claim")
    if len(witness) != 4:
        raise _error("claim witness must hold signature, preimage, leaf and control block")
    signature, preimage, leaf, control = witness
    if len(signature) != 64:
        raise _error("claim witness must use a SIGHASH_DEFAULT signature")
    if len(preimage) != 32 or hashlib.sha256(preimage).digest() != escrow.payment_hash:
        raise _error("claim witness preimage does not match the payment hash")
    expected_leaf, expected_control = escrow.claim_control_block()
    if leaf != expected_leaf or control != expected_control:
        raise _error("claim leaf or control block does not match the escrow")
    _require_exact_serialization(expected, witness, signed_tx, "claim")
    try:
        sighash = taproot_sighash(
            expected,
            0,
            [outpoint.value],
            [outpoint.scriptpubkey],
            SIGHASH_DEFAULT,
            leaf_hash=tapleaf_hash(expected_leaf),
        )
    except Exception as exc:  # noqa: BLE001
        raise _error("could not compute the escrow claim sighash") from exc
    if not verify_schnorr(to_xonly(escrow.buyer_claim_pubkey), signature, sighash):
        raise _error("claim signature does not verify against the buyer claim key")
    return True


def _compare_skeleton(expected: TaprootTx, actual: TaprootTx, name: str) -> None:
    """Report the first field-level difference before the byte-for-byte check."""
    if actual.version != expected.version or actual.locktime != expected.locktime:
        raise _error(f"{name} version or nLockTime does not match")
    expected_in, actual_in = expected.inputs[0], actual.inputs[0]
    if actual_in.txid != expected_in.txid or actual_in.vout != expected_in.vout:
        raise _error(f"{name} does not spend the exact escrow outpoint")
    if actual_in.sequence != expected_in.sequence:
        raise _error(f"{name} nSequence does not match")
    for index, (expected_out, actual_out) in enumerate(
        zip(expected.outputs, actual.outputs, strict=True)
    ):
        if actual_out.value != expected_out.value:
            raise _error(f"{name} output {index} value does not match")
        if actual_out.scriptpubkey != expected_out.scriptpubkey:
            raise _error(f"{name} output {index} script does not match")
