"""Tests for the cooperative (MuSig2 key-path) swap claim."""

from __future__ import annotations

from coincurve import PrivateKey, PublicKeyXOnly
from jmcore.bitcoin import create_p2tr_scriptpubkey, sha256
from jmcore.taproot import taproot_sighash

from jmswap.cooperative import cooperative_claim_client, server_cosign_claim
from jmswap.spend import SwapOutpoint
from jmswap.swap_tree import SwapTree

PREIMAGE = b"\x33" * 32
DEST_SPK = create_p2tr_scriptpubkey(b"\x07" * 32)


def _tree(server: PrivateKey, client: PrivateKey, is_reverse: bool) -> SwapTree:
    return SwapTree.build(
        is_reverse=is_reverse,
        preimage_hash=sha256(PREIMAGE),
        server_pubkey=server.public_key.format(compressed=True),
        client_pubkey=client.public_key.format(compressed=True),
        locktime=800_000,
    )


async def test_cooperative_claim_is_keypath_and_verifies() -> None:
    """The cooperative claim is a single key-path signature, valid for the output key.

    Crucially the witness carries NO preimage and NO tapscript, so it is
    indistinguishable from any other P2TR key-path spend.
    """
    server, client = PrivateKey(), PrivateKey()
    tree = _tree(server, client, is_reverse=True)  # swap-out: client claims
    outpoint = SwapOutpoint(
        txid="ab" * 32, vout=0, value=100_000, scriptpubkey=tree.funding_output_script()
    )

    async def cosign(client_pubnonce_hex: str) -> tuple[str, str]:
        pn, ps = server_cosign_claim(
            tree, outpoint, DEST_SPK, 200, server.secret, bytes.fromhex(client_pubnonce_hex)
        )
        return pn.hex(), ps.hex()

    tx = await cooperative_claim_client(tree, outpoint, DEST_SPK, 200, client.secret, cosign)
    # single 64-byte key-path signature, no preimage / script in the witness
    assert len(tx.witnesses[0]) == 1
    assert len(tx.witnesses[0][0]) == 64
    sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey])
    output_xonly = outpoint.scriptpubkey[2:]
    assert output_xonly == tree.tweaked_aggregate_xonly()
    assert PublicKeyXOnly(output_xonly).verify(tx.witnesses[0][0], sighash)


async def test_cooperative_claim_witness_has_no_preimage() -> None:
    """Regression: the cooperative claim must not leak the preimage on-chain."""
    server, client = PrivateKey(), PrivateKey()
    tree = _tree(server, client, is_reverse=False)
    outpoint = SwapOutpoint(
        txid="cd" * 32, vout=0, value=100_000, scriptpubkey=tree.funding_output_script()
    )

    async def cosign(client_pubnonce_hex: str) -> tuple[str, str]:
        pn, ps = server_cosign_claim(
            tree, outpoint, DEST_SPK, 200, server.secret, bytes.fromhex(client_pubnonce_hex)
        )
        return pn.hex(), ps.hex()

    tx = await cooperative_claim_client(tree, outpoint, DEST_SPK, 200, client.secret, cosign)
    for item in tx.witnesses[0]:
        assert item != PREIMAGE
        assert item != tree.claim_leaf  # no tapscript revealed
