"""Tests for swap persistence and timelock refund recovery."""

from __future__ import annotations

from pathlib import Path

from coincurve import PrivateKey
from jmcore.bitcoin import create_p2tr_scriptpubkey, sha256

from jmswap.chain import FakeChainBackend
from jmswap.models import SwapContract, SwapDirection, SwapState
from jmswap.persistence import SwapStore
from jmswap.recovery import is_refundable, refund
from jmswap.spend import build_claim_tx
from jmswap.swap_tree import SwapTree

PREIMAGE = b"\x44" * 32
PREIMAGE_HASH = sha256(PREIMAGE).hex()


def _contract(state: SwapState = SwapState.CREATED) -> SwapContract:
    return SwapContract(
        swap_id="swap-1",
        direction=SwapDirection.SWAP_OUT,
        network="regtest",
        server_pubkey="02" + "11" * 32,
        client_pubkey="03" + "22" * 32,
        preimage_hash=PREIMAGE_HASH,
        locktime=900_000,
        onchain_amount=100_000,
        funding_address="bcrt1pexample",
        state=state,
        lockup_txid="ab" * 32,
        lockup_vout=0,
    )


def test_store_save_load_list_delete(tmp_path: Path) -> None:
    store = SwapStore(tmp_path)
    contract = _contract(SwapState.LOCKUP_CONFIRMED)
    store.save(contract)
    loaded = store.load("swap-1")
    assert loaded is not None
    assert loaded.swap_id == "swap-1"
    assert loaded.state == SwapState.LOCKUP_CONFIRMED
    assert [c.swap_id for c in store.list()] == ["swap-1"]
    store.delete("swap-1")
    assert store.load("swap-1") is None
    assert store.list() == []


def test_store_overwrite_is_atomic(tmp_path: Path) -> None:
    store = SwapStore(tmp_path)
    contract = _contract(SwapState.AWAITING_LOCKUP)
    store.save(contract)
    contract.advance(SwapState.LOCKUP_SEEN)
    store.save(contract)
    assert store.load("swap-1").state == SwapState.LOCKUP_SEEN
    # only one record file persists (atomic replace, no temp leftovers)
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_is_refundable_predicate() -> None:
    contract = _contract(SwapState.LOCKUP_CONFIRMED)
    assert not is_refundable(contract, 899_999)  # locktime not reached
    assert is_refundable(contract, 900_000)  # at locktime
    contract.state = SwapState.CLAIMED
    assert not is_refundable(contract, 900_001)  # already claimed
    contract.state = SwapState.LOCKUP_CONFIRMED
    contract.lockup_txid = None
    assert not is_refundable(contract, 900_001)  # nothing funded


async def test_refund_broadcasts_when_unclaimed() -> None:
    server, client = PrivateKey(), PrivateKey()
    tree = SwapTree.build(
        is_reverse=True,
        preimage_hash=bytes.fromhex(PREIMAGE_HASH),
        server_pubkey=server.public_key.format(compressed=True),
        client_pubkey=client.public_key.format(compressed=True),
        locktime=900_000,
    )
    chain = FakeChainBackend(height=900_010)
    funding_txid, vout = chain.fund_address(
        tree.funding_address("regtest"), 100_000, tree.funding_output_script()
    )
    contract = SwapContract(
        swap_id="swap-2",
        direction=SwapDirection.SWAP_OUT,
        network="regtest",
        server_pubkey=server.public_key.format(compressed=True).hex(),
        client_pubkey=client.public_key.format(compressed=True).hex(),
        preimage_hash=PREIMAGE_HASH,
        locktime=900_000,
        onchain_amount=100_000,
        funding_address=tree.funding_address("regtest"),
        state=SwapState.LOCKUP_CONFIRMED,
        lockup_txid=funding_txid,
        lockup_vout=vout,
    )
    dest = create_p2tr_scriptpubkey(b"\x09" * 32)
    # swap-out refund key is the provider (server)
    txid = await refund(contract, tree, server.secret, dest, chain)
    assert txid is not None
    assert contract.state == SwapState.REFUNDED
    assert await chain.get_transaction(txid) is not None


async def test_refund_skips_when_already_claimed() -> None:
    server, client = PrivateKey(), PrivateKey()
    tree = SwapTree.build(
        is_reverse=True,
        preimage_hash=bytes.fromhex(PREIMAGE_HASH),
        server_pubkey=server.public_key.format(compressed=True),
        client_pubkey=client.public_key.format(compressed=True),
        locktime=900_000,
    )
    chain = FakeChainBackend(height=900_010)
    funding_txid, vout = chain.fund_address(
        tree.funding_address("regtest"), 100_000, tree.funding_output_script()
    )
    from jmswap.spend import SwapOutpoint

    outpoint = SwapOutpoint(
        txid=funding_txid, vout=vout, value=100_000, scriptpubkey=tree.funding_output_script()
    )
    # the client claims the HTLC first (reverse swap: client claims with preimage)
    claim = build_claim_tx(
        tree, outpoint, PREIMAGE, client.secret, create_p2tr_scriptpubkey(b"\x01" * 32), fee=200
    )
    await chain.broadcast(claim.serialize().hex())

    contract = SwapContract(
        swap_id="swap-3",
        direction=SwapDirection.SWAP_OUT,
        network="regtest",
        server_pubkey=server.public_key.format(compressed=True).hex(),
        client_pubkey=client.public_key.format(compressed=True).hex(),
        preimage_hash=PREIMAGE_HASH,
        locktime=900_000,
        onchain_amount=100_000,
        funding_address=tree.funding_address("regtest"),
        state=SwapState.LOCKUP_CONFIRMED,
        lockup_txid=funding_txid,
        lockup_vout=vout,
    )
    txid = await refund(
        contract, tree, server.secret, create_p2tr_scriptpubkey(b"\x09" * 32), chain
    )
    assert txid is None  # already claimed -> no refund
    assert contract.state == SwapState.LOCKUP_CONFIRMED


async def test_server_refund_expired_and_persistence(tmp_path: Path) -> None:
    """SwapServer refunds an expired swap-out and persists the contract."""
    from decimal import Decimal

    from jmswap.lightning import FakeLightningBackend, FakeLightningNetwork
    from jmswap.models import ProviderTerms
    from jmswap.server import SwapServer
    from jmswap.wallet import FakeSwapWallet

    server_key, client_key = PrivateKey(), PrivateKey()
    tree = SwapTree.build(
        is_reverse=True,
        preimage_hash=bytes.fromhex(PREIMAGE_HASH),
        server_pubkey=server_key.public_key.format(compressed=True),
        client_pubkey=client_key.public_key.format(compressed=True),
        locktime=900_000,
    )
    chain = FakeChainBackend(height=900_010)
    funding_txid, vout = chain.fund_address(
        tree.funding_address("regtest"), 100_000, tree.funding_output_script()
    )
    store = SwapStore(tmp_path)
    terms = ProviderTerms(
        provider_id="p",
        provider_pubkey="02" + "11" * 32,
        network="regtest",
        percentage_fee=Decimal("0.1"),
    )
    swap_server = SwapServer(
        terms,
        FakeLightningBackend(FakeLightningNetwork()),
        chain,
        FakeSwapWallet(chain),
        store=store,
    )
    contract = SwapContract(
        swap_id="swap-9",
        direction=SwapDirection.SWAP_OUT,
        network="regtest",
        server_pubkey=server_key.public_key.format(compressed=True).hex(),
        client_pubkey=client_key.public_key.format(compressed=True).hex(),
        preimage_hash=PREIMAGE_HASH,
        locktime=900_000,
        onchain_amount=100_000,
        funding_address=tree.funding_address("regtest"),
        state=SwapState.LOCKUP_CONFIRMED,
        lockup_txid=funding_txid,
        lockup_vout=vout,
    )
    swap_server.contracts["swap-9"] = contract
    swap_server._trees["swap-9"] = tree
    swap_server._privkeys["swap-9"] = server_key.secret

    refunded = await swap_server.refund_expired()
    assert len(refunded) == 1
    assert contract.state == SwapState.REFUNDED
    # the refund was persisted
    assert store.load("swap-9").state == SwapState.REFUNDED
