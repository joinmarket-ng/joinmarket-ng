"""Tests for the FakeChainBackend and txid computation."""

from __future__ import annotations

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import create_p2tr_scriptpubkey, sha256

from jmswap.chain import FakeChainBackend
from jmswap.errors import ChainError
from jmswap.spend import SwapOutpoint, build_claim_tx
from jmswap.swap_tree import SwapTree


async def test_fund_mine_and_utxos() -> None:
    chain = FakeChainBackend(height=200)
    spk = create_p2tr_scriptpubkey(b"\x01" * 32)
    addr = "bcrt1pexample"
    txid, vout = chain.fund_address(addr, 50_000, spk, confirmations=0)
    utxos = await chain.get_address_utxos(addr)
    assert len(utxos) == 1
    assert utxos[0].txid == txid and utxos[0].vout == vout
    chain.mine(3)
    assert await chain.block_height() == 203
    utxos = await chain.get_address_utxos(addr)
    assert utxos[0].confirmations == 3


async def test_broadcast_txid_matches_taproottx() -> None:
    chain = FakeChainBackend()
    server, client = PrivateKey(), PrivateKey()
    preimage = b"\x05" * 32
    tree = SwapTree.build(
        is_reverse=False,
        preimage_hash=sha256(preimage),
        server_pubkey=server.public_key.format(compressed=True),
        client_pubkey=client.public_key.format(compressed=True),
        locktime=800_000,
    )
    outpoint = SwapOutpoint(
        txid="cd" * 32, vout=0, value=100_000, scriptpubkey=tree.funding_output_script()
    )
    dest = create_p2tr_scriptpubkey(b"\x09" * 32)
    tx = build_claim_tx(tree, outpoint, preimage, server.secret, dest, fee=200)
    txid = await chain.broadcast(tx.serialize().hex())
    # The backend's witness-stripped txid must match the tx's own txid.
    assert txid == tx.txid()
    assert await chain.get_transaction(txid) == tx.serialize().hex()


async def test_double_broadcast_raises() -> None:
    chain = FakeChainBackend()
    raw = "02000000000100000000000000000000000000"  # arbitrary non-segwit-ish bytes
    txid = await chain.broadcast(raw)
    assert isinstance(txid, str)
    with pytest.raises(ChainError):
        await chain.broadcast(raw)


async def test_broadcast_invalid_hex() -> None:
    chain = FakeChainBackend()
    with pytest.raises(ChainError):
        await chain.broadcast("nothex")
