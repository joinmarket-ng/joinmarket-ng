"""Tests for swap hardening: RBF, fee bumping, reorg conf-gating, sybil bonds."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from coincurve import PrivateKey
from jmcore.bitcoin import create_p2tr_scriptpubkey, sha256

from jmswap.chain import FakeChainBackend
from jmswap.client import SwapClient
from jmswap.lightning import FakeLightningBackend, FakeLightningNetwork
from jmswap.models import ProviderTerms, SwapDirection
from jmswap.nostr import LoopbackRelay, SwapNostrNode
from jmswap.server import SwapServer
from jmswap.spend import RBF_SEQUENCE, SwapOutpoint, build_claim_tx, build_refund_tx, bump_fee
from jmswap.swap_tree import SwapTree
from jmswap.wallet import FakeSwapWallet

PREIMAGE = b"\x66" * 32
PUBKEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


def _tree(server: PrivateKey, client: PrivateKey, is_reverse: bool) -> SwapTree:
    return SwapTree.build(
        is_reverse=is_reverse,
        preimage_hash=sha256(PREIMAGE),
        server_pubkey=server.public_key.format(compressed=True),
        client_pubkey=client.public_key.format(compressed=True),
        locktime=800_000,
    )


def test_claim_signals_rbf_by_default() -> None:
    s, c = PrivateKey(), PrivateKey()
    tree = _tree(s, c, is_reverse=False)
    outpoint = SwapOutpoint(
        txid="ab" * 32, vout=0, value=100_000, scriptpubkey=tree.funding_output_script()
    )
    dest = create_p2tr_scriptpubkey(b"\x07" * 32)
    rbf = build_claim_tx(tree, outpoint, PREIMAGE, s.secret, dest, 200)
    assert rbf.inputs[0].sequence == RBF_SEQUENCE
    final = build_claim_tx(tree, outpoint, PREIMAGE, s.secret, dest, 200, enable_rbf=False)
    assert final.inputs[0].sequence == 0xFFFFFFFF


def test_refund_signals_rbf_and_locktime() -> None:
    s, c = PrivateKey(), PrivateKey()
    tree = _tree(s, c, is_reverse=False)
    outpoint = SwapOutpoint(
        txid="ab" * 32, vout=0, value=100_000, scriptpubkey=tree.funding_output_script()
    )
    dest = create_p2tr_scriptpubkey(b"\x07" * 32)
    refund = build_refund_tx(tree, outpoint, c.secret, dest, 200)
    assert refund.inputs[0].sequence == RBF_SEQUENCE  # < 0xffffffff enables CLTV + RBF
    assert refund.locktime == 800_000


def test_bump_fee_increases_absolute_fee() -> None:
    assert bump_fee(300, vbytes=150, min_increment_sat_vb=1.0) >= 300 + 150
    assert bump_fee(0) > 0
    # a bumped claim is a valid replacement (same outpoint, higher fee)
    s, c = PrivateKey(), PrivateKey()
    tree = _tree(s, c, is_reverse=False)
    outpoint = SwapOutpoint(
        txid="ab" * 32, vout=0, value=100_000, scriptpubkey=tree.funding_output_script()
    )
    dest = create_p2tr_scriptpubkey(b"\x07" * 32)
    original = build_claim_tx(tree, outpoint, PREIMAGE, s.secret, dest, 200)
    bumped = build_claim_tx(tree, outpoint, PREIMAGE, s.secret, dest, bump_fee(200))
    assert original.inputs[0].outpoint == bumped.inputs[0].outpoint
    assert bumped.outputs[0].value < original.outputs[0].value  # higher fee


async def test_min_confirmations_gates_funding() -> None:
    chain = FakeChainBackend(height=500)
    net = FakeLightningNetwork()
    terms = ProviderTerms(
        provider_id="p",
        provider_pubkey=PUBKEY,
        network="regtest",
        directions=[SwapDirection.SWAP_IN],
        min_amount=10_000,
        max_amount=1_000_000,
        percentage_fee=Decimal("0.1"),
    )
    server = SwapServer(
        terms,
        FakeLightningBackend(net),
        chain,
        FakeSwapWallet(chain),
        poll_interval=0.01,
        min_confirmations=3,
    )
    client = SwapClient(
        FakeLightningBackend(net),
        chain,
        FakeSwapWallet(chain),
        poll_interval=0.01,
        min_confirmations=3,
    )

    async def rpc(method: str, params: dict) -> dict:  # type: ignore[type-arg]
        return await server.rpc_handler(method, params, "client")

    # FakeSwapWallet funds with 1 confirmation; with min_confirmations=3 the swap
    # cannot complete until more blocks are mined, so it times out quickly here.
    task = asyncio.create_task(client.swap_in(rpc, terms, amount=100_000))
    await asyncio.sleep(0.2)
    assert not task.done()  # still waiting for 3 confirmations
    chain.mine(3)  # now enough confirmations
    await asyncio.wait_for(task, timeout=10)


async def test_discover_filters_by_min_bond() -> None:
    relay = LoopbackRelay()
    low = SwapNostrNode(PrivateKey().secret, relay)
    high = SwapNostrNode(PrivateKey().secret, relay)
    client = SwapNostrNode(PrivateKey().secret, relay)
    await low.advertise(
        ProviderTerms(
            provider_id=low.pubkey, provider_pubkey=PUBKEY, network="regtest", bond_value=0
        )
    )
    await high.advertise(
        ProviderTerms(
            provider_id=high.pubkey, provider_pubkey=PUBKEY, network="regtest", bond_value=5_000_000
        )
    )
    all_adverts = await client.discover(timeout=0.2)
    assert len(all_adverts) == 2
    bonded = await client.discover(timeout=0.2, min_bond_value=1_000_000)
    assert [a.nostr_pubkey for a in bonded] == [high.pubkey]
