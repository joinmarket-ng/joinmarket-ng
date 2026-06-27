"""
Taproot swap trees (Boltz v2 compatible), for both swap directions.

A swap is locked in a single P2TR output whose:

* **internal key** is the MuSig2 aggregate of ``[server_pubkey, client_pubkey]``
  (server first, matching the Boltz convention). The cooperative key-path spend
  is an ordinary BIP340 signature, so a happy-path swap settlement is
  indistinguishable from any other P2TR key-path spend.
* **script tree** has two leaves: a *claim* leaf (spendable with the preimage by
  the party that ends up with the on-chain coin) and a *refund* leaf (spendable
  by the funder after an absolute locktime).

Direction mapping (the provider is always the "server"):

* **swap-in** (provider receives on-chain, pays Lightning) is a *submarine* swap:
  the client funds the output, the provider claims it with the preimage.
  ``claim_pubkey = server``, ``refund_pubkey = client``.
* **swap-out** (provider pays on-chain, receives Lightning) is a *reverse* swap:
  the provider funds the output, the client claims it with the preimage.
  ``claim_pubkey = client``, ``refund_pubkey = server``.

References:
    - https://github.com/BoltzExchange/boltz-core (SwapTree / ReverseSwapTree)
    - https://api.docs.boltz.exchange/
"""

from __future__ import annotations

from dataclasses import dataclass

from jmcore.bitcoin import tagged_hash
from jmcore.musig2 import KeyAggContext, apply_xonly_tweak, key_agg
from jmcore.taproot import (
    LEAF_VERSION_TAPSCRIPT,
    OP_CHECKLOCKTIMEVERIFY,
    OP_CHECKSIG,
    OP_CHECKSIGVERIFY,
    OP_EQUALVERIFY,
    OP_HASH160,
    OP_SIZE,
    TapTreeNode,
    construct_script,
    control_block,
    encode_script_num,
    push_script_num,
    ripemd160,
    taproot_address,
    taproot_merkle_root,
    taproot_output_script,
    to_xonly,
)

from jmswap.errors import SwapVerificationError

CLAIM_LEAF_INDEX = 0
REFUND_LEAF_INDEX = 1


def make_submarine_claim_leaf(preimage_hash: bytes, claim_pubkey: bytes) -> bytes:
    """Submarine claim leaf: ``OP_HASH160 <h> OP_EQUALVERIFY <claim> OP_CHECKSIG``."""
    if len(preimage_hash) != 32:
        raise ValueError("preimage_hash must be 32 bytes")
    return construct_script(
        [
            OP_HASH160,
            ripemd160(preimage_hash),
            OP_EQUALVERIFY,
            to_xonly(claim_pubkey),
            OP_CHECKSIG,
        ]
    )


def make_reverse_claim_leaf(preimage_hash: bytes, claim_pubkey: bytes) -> bytes:
    """Reverse claim leaf, with the ``OP_SIZE 32`` guard Boltz adds."""
    if len(preimage_hash) != 32:
        raise ValueError("preimage_hash must be 32 bytes")
    return construct_script(
        [
            OP_SIZE,
            encode_script_num(32),
            OP_EQUALVERIFY,
            OP_HASH160,
            ripemd160(preimage_hash),
            OP_EQUALVERIFY,
            to_xonly(claim_pubkey),
            OP_CHECKSIG,
        ]
    )


def make_refund_leaf(refund_pubkey: bytes, locktime: int) -> bytes:
    """Refund leaf (Boltz layout): ``<refund> OP_CHECKSIGVERIFY <locktime> OP_CLTV``.

    There is no trailing ``OP_DROP``: ``OP_CHECKLOCKTIMEVERIFY`` does not pop its
    argument, so the locktime is left on the stack (truthy) as the script's
    result, matching boltz-core byte for byte.
    """
    if locktime < 0 or locktime >= 500_000_000:
        raise ValueError("locktime must be a block height (< 500,000,000)")
    script = construct_script([to_xonly(refund_pubkey), OP_CHECKSIGVERIFY])
    script += push_script_num(locktime)
    script += bytes([OP_CHECKLOCKTIMEVERIFY])
    return script


@dataclass(frozen=True)
class SwapTree:
    """A two-leaf Taproot swap tree plus the aggregated funding key."""

    is_reverse: bool
    preimage_hash: bytes
    claim_pubkey: bytes  # 33-byte compressed
    refund_pubkey: bytes  # 33-byte compressed
    server_pubkey: bytes  # 33-byte compressed (first in MuSig2 agg)
    client_pubkey: bytes  # 33-byte compressed (second in MuSig2 agg)
    locktime: int
    claim_leaf: bytes
    refund_leaf: bytes

    @classmethod
    def build(
        cls,
        *,
        is_reverse: bool,
        preimage_hash: bytes,
        server_pubkey: bytes,
        client_pubkey: bytes,
        locktime: int,
    ) -> SwapTree:
        if is_reverse:
            claim_pubkey, refund_pubkey = client_pubkey, server_pubkey
            claim_leaf = make_reverse_claim_leaf(preimage_hash, claim_pubkey)
        else:
            claim_pubkey, refund_pubkey = server_pubkey, client_pubkey
            claim_leaf = make_submarine_claim_leaf(preimage_hash, claim_pubkey)
        refund_leaf = make_refund_leaf(refund_pubkey, locktime)
        return cls(
            is_reverse=is_reverse,
            preimage_hash=preimage_hash,
            claim_pubkey=claim_pubkey,
            refund_pubkey=refund_pubkey,
            server_pubkey=server_pubkey,
            client_pubkey=client_pubkey,
            locktime=locktime,
            claim_leaf=claim_leaf,
            refund_leaf=refund_leaf,
        )

    @property
    def tap_tree(self) -> TapTreeNode:
        return [
            (LEAF_VERSION_TAPSCRIPT, self.claim_leaf),
            (LEAF_VERSION_TAPSCRIPT, self.refund_leaf),
        ]

    def merkle_root(self) -> bytes:
        return taproot_merkle_root(self.tap_tree)

    def internal_xonly(self) -> bytes:
        """MuSig2 aggregate internal x-only key (server first, client second)."""
        return key_agg([self.server_pubkey, self.client_pubkey]).aggregate_xonly()

    def taptweak_scalar(self) -> bytes:
        """The BIP341 TapTweak scalar: tagged_hash('TapTweak', internal || root).

        This is the value to pass as a MuSig2 x-only tweak when cooperatively
        signing the key-path spend.
        """
        return tagged_hash("TapTweak", self.internal_xonly() + self.merkle_root())

    def keyagg_context(self) -> KeyAggContext:
        """The taptweaked MuSig2 key-aggregation context for cooperative signing."""
        ctx = key_agg([self.server_pubkey, self.client_pubkey])
        return apply_xonly_tweak(ctx, self.taptweak_scalar())

    def tweaked_aggregate_xonly(self) -> bytes:
        """Output x-only key for the cooperative key-path spend (taptweaked agg)."""
        return self.keyagg_context().aggregate_xonly()

    def funding_output_script(self) -> bytes:
        return taproot_output_script(self.internal_xonly(), self.tap_tree)

    def funding_address(self, network: str = "mainnet") -> str:
        return taproot_address(self.internal_xonly(), self.tap_tree, network)

    def claim_control_block(self) -> tuple[bytes, bytes]:
        return control_block(self.internal_xonly(), self.tap_tree, CLAIM_LEAF_INDEX)

    def refund_control_block(self) -> tuple[bytes, bytes]:
        return control_block(self.internal_xonly(), self.tap_tree, REFUND_LEAF_INDEX)

    def serialize(self) -> dict[str, dict[str, object]]:
        """Serialise to the Boltz v2 ``swapTree`` JSON shape."""
        return {
            "claimLeaf": {"version": LEAF_VERSION_TAPSCRIPT, "output": self.claim_leaf.hex()},
            "refundLeaf": {"version": LEAF_VERSION_TAPSCRIPT, "output": self.refund_leaf.hex()},
        }


def verify_swap_tree(
    *,
    is_reverse: bool,
    preimage_hash: bytes,
    server_pubkey: bytes,
    client_pubkey: bytes,
    locktime: int,
    expected_address: str,
    expected_swap_tree: dict[str, dict[str, object]] | None = None,
    network: str = "mainnet",
) -> SwapTree:
    """Recompute a swap tree from agreed parameters and verify the server's reply.

    Ensures the server cannot pin the funding address to leaves the client did
    not agree to (the v2 analogue of Boltz's ``_check_swap_scriptcode``).
    """
    tree = SwapTree.build(
        is_reverse=is_reverse,
        preimage_hash=preimage_hash,
        server_pubkey=server_pubkey,
        client_pubkey=client_pubkey,
        locktime=locktime,
    )
    if expected_swap_tree is not None and tree.serialize() != expected_swap_tree:
        raise SwapVerificationError("swap tree leaves do not match agreed parameters")
    computed = tree.funding_address(network)
    if computed != expected_address:
        raise SwapVerificationError(
            f"swap address mismatch: server={expected_address!r} computed={computed!r}"
        )
    return tree
