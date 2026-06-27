"""
Spending Taproot swap outputs: claim, refund and cooperative (key-path) spends.

* **claim** (script path): the preimage holder sweeps the HTLC, revealing the
  preimage in the witness. Witness ``[sig, preimage, claim_leaf, control_block]``.
* **refund** (script path): the funder reclaims after the absolute locktime.
  Witness ``[sig, refund_leaf, control_block]`` with ``nLockTime`` set.
* **cooperative** (key path): a 2-of-2 MuSig2 spend producing a single 64-byte
  BIP340 signature, indistinguishable from any other P2TR key-path spend. This
  is the happy path; the script paths are the trustless fallbacks.

All builders return a fully serialised, signed :class:`TaprootTx`; the
cooperative path additionally exposes the interactive MuSig2 primitives the
two peers use to co-sign without either learning the other's key.
"""

from __future__ import annotations

from dataclasses import dataclass

from jmcore.musig2 import (
    SecNonce,
    Session,
    get_session,
    nonce_agg,
    nonce_gen,
    partial_sig_agg,
    partial_sig_verify,
    sign_partial,
)
from jmcore.taproot import (
    TaprootTx,
    TxIn,
    TxOut,
    sign_taproot_scriptpath,
    tapleaf_hash,
    taproot_sighash,
)

from jmswap.swap_tree import SwapTree

# Conservative vbyte estimates for fee calculation.
CLAIM_TX_VBYTES = 150
REFUND_TX_VBYTES = 150
COOPERATIVE_TX_VBYTES = 110


@dataclass(frozen=True)
class SwapOutpoint:
    """The on-chain swap funding output being spent."""

    txid: str
    vout: int
    value: int  # sats locked in the output
    scriptpubkey: bytes  # the P2TR funding scriptPubKey


def estimate_fee(vbytes: int, feerate_sat_vb: float) -> int:
    """Round a vbyte estimate * feerate up to whole sats."""
    return int(vbytes * feerate_sat_vb + 0.999)


def bump_fee(
    current_fee: int, vbytes: int = CLAIM_TX_VBYTES, min_increment_sat_vb: float = 1.0
) -> int:
    """Return a higher fee for a BIP125 replacement (raises the rate by >= 1 sat/vB).

    A claim/refund built with ``enable_rbf`` can be replaced by rebuilding it with
    this fee; the replacement satisfies BIP125's higher-absolute-fee rule.
    """
    return max(current_fee + int(vbytes * min_increment_sat_vb + 0.999), current_fee + 1)


# BIP125 opt-in replace-by-fee signalling sequence (also enables nLockTime/CLTV).
RBF_SEQUENCE = 0xFFFFFFFD


def _build_spend_tx(
    outpoint: SwapOutpoint,
    destination_spk: bytes,
    fee: int,
    *,
    locktime: int = 0,
    sequence: int = 0xFFFFFFFF,
) -> TaprootTx:
    value = outpoint.value - fee
    if value <= 0:
        raise ValueError("fee exceeds swap output value")
    return TaprootTx(
        inputs=[TxIn(txid=outpoint.txid, vout=outpoint.vout, sequence=sequence)],
        outputs=[TxOut(value=value, scriptpubkey=destination_spk)],
        locktime=locktime,
    )


# --- Claim (script path, reveals preimage) ----------------------------------


def build_claim_tx(
    tree: SwapTree,
    outpoint: SwapOutpoint,
    preimage: bytes,
    claim_privkey: bytes,
    destination_spk: bytes,
    fee: int,
    *,
    enable_rbf: bool = True,
) -> TaprootTx:
    """Build and sign a script-path claim transaction (reveals ``preimage``).

    With ``enable_rbf`` the input signals BIP125 opt-in RBF so a stuck claim can
    be fee-bumped by rebuilding with a higher fee (see :func:`bump_fee`).
    """
    if len(preimage) != 32:
        raise ValueError("preimage must be 32 bytes")
    sequence = RBF_SEQUENCE if enable_rbf else 0xFFFFFFFF
    tx = _build_spend_tx(outpoint, destination_spk, fee, sequence=sequence)
    script, cb = tree.claim_control_block()
    sig = sign_taproot_scriptpath(
        tx, 0, [outpoint.value], [outpoint.scriptpubkey], script, claim_privkey
    )
    tx.witnesses = [[sig, preimage, script, cb]]
    return tx


# --- Refund (script path, after locktime) -----------------------------------


def build_refund_tx(
    tree: SwapTree,
    outpoint: SwapOutpoint,
    refund_privkey: bytes,
    destination_spk: bytes,
    fee: int,
) -> TaprootTx:
    """Build and sign a script-path refund transaction (requires locktime)."""
    tx = _build_spend_tx(
        outpoint,
        destination_spk,
        fee,
        locktime=tree.locktime,
        sequence=RBF_SEQUENCE,  # enables nLockTime/CLTV and signals BIP125 RBF
    )
    script, cb = tree.refund_control_block()
    sig = sign_taproot_scriptpath(
        tx, 0, [outpoint.value], [outpoint.scriptpubkey], script, refund_privkey
    )
    tx.witnesses = [[sig, script, cb]]
    return tx


# --- Cooperative (key path, MuSig2 2-of-2) ----------------------------------


@dataclass
class CooperativeSpend:
    """An unsigned cooperative-spend transaction plus its sighash and session.

    Both peers build the *same* object (deterministic given the inputs) and use
    the MuSig2 primitives below to co-sign it.
    """

    tx: TaprootTx
    sighash: bytes


def build_cooperative_spend(
    tree: SwapTree,
    outpoint: SwapOutpoint,
    destination_spk: bytes,
    fee: int,
) -> CooperativeSpend:
    """Build the unsigned key-path spend skeleton and its sighash."""
    tx = _build_spend_tx(outpoint, destination_spk, fee)
    sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey])
    return CooperativeSpend(tx=tx, sighash=sighash)


def cooperative_session(tree: SwapTree, aggnonce: bytes, sighash: bytes) -> Session:
    """Derive the shared MuSig2 session for a cooperative spend."""
    return get_session(
        aggnonce,
        [tree.server_pubkey, tree.client_pubkey],
        [tree.taptweak_scalar()],
        sighash,
    )


def cooperative_nonce(pubkey: bytes) -> tuple[SecNonce, bytes]:
    """Generate this peer's (secret, public) MuSig2 nonce for a cooperative spend."""
    return nonce_gen(pubkey)


def finalize_cooperative(
    tree: SwapTree,
    spend: CooperativeSpend,
    server_pubnonce: bytes,
    client_pubnonce: bytes,
    server_psig: bytes,
    client_psig: bytes,
) -> TaprootTx:
    """Aggregate two partial signatures into the final key-path witness."""
    aggnonce = nonce_agg([server_pubnonce, client_pubnonce])
    session = cooperative_session(tree, aggnonce, spend.sighash)
    if not partial_sig_verify(server_psig, server_pubnonce, tree.server_pubkey, session):
        raise ValueError("invalid server partial signature")
    if not partial_sig_verify(client_psig, client_pubnonce, tree.client_pubkey, session):
        raise ValueError("invalid client partial signature")
    sig = partial_sig_agg([server_psig, client_psig], session)
    spend.tx.witnesses = [[sig]]
    return spend.tx


def cooperative_spend_local(
    tree: SwapTree,
    outpoint: SwapOutpoint,
    destination_spk: bytes,
    fee: int,
    server_privkey: bytes,
    client_privkey: bytes,
) -> TaprootTx:
    """Run the whole cooperative spend locally (both keys held; for tests/tools).

    The interactive flow is the same primitives split across two peers:
    :func:`cooperative_nonce`, :func:`cooperative_session`,
    :func:`jmcore.musig2.sign_partial`, :func:`finalize_cooperative`.
    """
    spend = build_cooperative_spend(tree, outpoint, destination_spk, fee)
    sn_s, pn_s = nonce_gen(tree.server_pubkey)
    sn_c, pn_c = nonce_gen(tree.client_pubkey)
    aggnonce = nonce_agg([pn_s, pn_c])
    session = cooperative_session(tree, aggnonce, spend.sighash)
    ps_s = sign_partial(sn_s, server_privkey, session)
    ps_c = sign_partial(sn_c, client_privkey, session)
    return finalize_cooperative(tree, spend, pn_s, pn_c, ps_s, ps_c)


def claim_leaf_hash(tree: SwapTree) -> bytes:
    """Tapleaf hash of the claim leaf (handy for tests/inspection)."""
    return tapleaf_hash(tree.claim_leaf)


def parse_witness_stacks(raw: bytes) -> list[list[bytes]]:
    """Return the per-input witness stacks of a serialized SegWit transaction."""
    if len(raw) < 6 or raw[4] != 0x00 or raw[5] != 0x01:
        raise ValueError("not a SegWit transaction")
    offset = 6
    n_in, offset = _read_varint(raw, offset)
    inputs = n_in
    for _ in range(n_in):
        offset += 36
        sl, offset = _read_varint(raw, offset)
        offset += sl + 4
    n_out, offset = _read_varint(raw, offset)
    for _ in range(n_out):
        offset += 8
        sl, offset = _read_varint(raw, offset)
        offset += sl
    stacks: list[list[bytes]] = []
    for _ in range(inputs):
        n_items, offset = _read_varint(raw, offset)
        stack: list[bytes] = []
        for _ in range(n_items):
            il, offset = _read_varint(raw, offset)
            stack.append(raw[offset : offset + il])
            offset += il
        stacks.append(stack)
    return stacks


def extract_preimage_from_claim(raw_hex: str) -> bytes | None:
    """Extract the 32-byte preimage from a swap claim transaction's witness.

    A claim witness is ``[sig, preimage, leaf_script, control_block]``; the
    preimage is the second item. Returns ``None`` if no 32-byte candidate is
    present (e.g. a cooperative or refund spend).
    """
    try:
        stacks = parse_witness_stacks(bytes.fromhex(raw_hex))
    except ValueError:
        return None
    for stack in stacks:
        if len(stack) >= 2 and len(stack[1]) == 32:
            return stack[1]
    return None


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    n = data[offset]
    offset += 1
    if n < 0xFD:
        return n, offset
    if n == 0xFD:
        return int.from_bytes(data[offset : offset + 2], "little"), offset + 2
    if n == 0xFE:
        return int.from_bytes(data[offset : offset + 4], "little"), offset + 4
    return int.from_bytes(data[offset : offset + 8], "little"), offset + 8
