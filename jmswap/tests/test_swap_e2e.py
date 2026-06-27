"""
End-to-end swap protocol tests, fully in-process.

A provider and a client run both swap directions against fake (but faithful)
backends: a shared :class:`FakeChainBackend`, a :class:`FakeLightningNetwork`
with one node each, and :class:`FakeSwapWallet`s. The on-chain transactions are
real, BIP341-correct Taproot spends; only the consensus engine and the relays
are mocked. The RPC layer is exercised both directly and over the Nostr
LoopbackRelay.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from coincurve import PrivateKey

from jmswap.chain import FakeChainBackend
from jmswap.client import SwapClient
from jmswap.errors import SwapProtocolError
from jmswap.lightning import FakeLightningBackend, FakeLightningNetwork
from jmswap.models import ProviderTerms, SwapDirection, SwapState
from jmswap.nostr import LoopbackRelay, SwapNostrNode
from jmswap.server import SwapServer
from jmswap.wallet import FakeSwapWallet

PROVIDER_SWAP_PUBKEY = PrivateKey().public_key.format(compressed=True).hex()


def _build_world(
    *, coop_claim_window_polls: int = 40
) -> tuple[SwapServer, SwapClient, ProviderTerms, FakeChainBackend]:
    chain = FakeChainBackend(height=500)
    net = FakeLightningNetwork()
    provider_ln = FakeLightningBackend(net, "aa" * 33)
    client_ln = FakeLightningBackend(net, "bb" * 33)
    provider_wallet = FakeSwapWallet(chain)
    client_wallet = FakeSwapWallet(chain)
    terms = ProviderTerms(
        provider_id="provider-1",
        provider_pubkey=PROVIDER_SWAP_PUBKEY,
        network="regtest",
        min_amount=10_000,
        max_amount=10_000_000,
        percentage_fee=Decimal("0.5"),
        base_fee=300,
        cltv_delta=144,
        accepts_whole_coin=True,
    )
    server = SwapServer(
        terms,
        provider_ln,
        chain,
        provider_wallet,
        poll_interval=0.01,
        coop_claim_window_polls=coop_claim_window_polls,
    )
    client = SwapClient(client_ln, chain, client_wallet, poll_interval=0.01)
    return server, client, terms, chain


def _direct_rpc(server: SwapServer):
    async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await server.rpc_handler(method, params, "client")

    return rpc


async def test_swap_in_end_to_end() -> None:
    """Client funds on-chain, provider pays Lightning and claims the HTLC.

    The happy path now settles via the cooperative key-path: the client co-signs
    and broadcasts an ordinary P2TR spend to the provider, revealing no preimage
    or script on-chain.
    """
    from jmswap.spend import extract_preimage_from_claim

    server, client, terms, chain = _build_world()
    rpc = _direct_rpc(server)
    contract = await asyncio.wait_for(client.swap_in(rpc, terms, amount=200_000), timeout=10)
    assert contract.direction == SwapDirection.SWAP_IN
    assert contract.state == SwapState.CLAIMED
    server_contract = next(iter(server.contracts.values()))
    await _wait_state(server_contract, SwapState.CLAIMED)
    # The provider learned the preimage off-chain (paying the invoice), but the
    # on-chain spend is a key-path spend that reveals neither preimage nor script.
    assert server_contract.preimage is not None
    raw = await chain.find_spending_tx(server_contract.lockup_txid, server_contract.lockup_vout)
    assert raw is not None
    assert extract_preimage_from_claim(raw) is None  # cooperative key-path, no preimage


async def test_swap_in_script_path_fallback() -> None:
    """With no cooperative window, the provider claims via the script path."""
    from jmswap.spend import extract_preimage_from_claim

    server, client, terms, chain = _build_world(coop_claim_window_polls=0)
    rpc = _direct_rpc(server)
    contract = await asyncio.wait_for(client.swap_in(rpc, terms, amount=200_000), timeout=10)
    assert contract.state == SwapState.CLAIMED
    server_contract = next(iter(server.contracts.values()))
    await _wait_state(server_contract, SwapState.CLAIMED)
    # The provider swept on-chain itself, revealing the preimage in the witness.
    assert server_contract.spend_txid is not None
    raw = await chain.get_transaction(server_contract.spend_txid)
    assert raw is not None
    assert extract_preimage_from_claim(raw) is not None


async def test_swap_out_end_to_end() -> None:
    """Client pays Lightning, provider funds on-chain, client claims it."""
    server, client, terms, _chain = _build_world()
    rpc = _direct_rpc(server)
    contract = await asyncio.wait_for(client.swap_out(rpc, terms, amount=150_000), timeout=10)
    assert contract.direction == SwapDirection.SWAP_OUT
    assert contract.state == SwapState.CLAIMED
    assert contract.spend_txid is not None
    server_contract = next(iter(server.contracts.values()))
    await _wait_state(server_contract, SwapState.CLAIMED)
    # the provider learned the preimage from the on-chain claim and settled LN
    assert server_contract.preimage == contract.preimage


async def test_whole_coin_swap_in() -> None:
    """Third-role style: a whole-coin swap-in charges only the base fee."""
    server, client, terms, _chain = _build_world()
    rpc = _direct_rpc(server)
    contract = await asyncio.wait_for(
        client.swap_in(rpc, terms, amount=500_000, whole_coin=True), timeout=10
    )
    assert contract.state == SwapState.CLAIMED
    # base fee only -> onchain amount is amount + base_fee
    assert contract.onchain_amount == 500_000 + terms.base_fee


async def test_swap_rejects_out_of_limits() -> None:
    server, client, terms, _chain = _build_world()
    rpc = _direct_rpc(server)
    with pytest.raises(SwapProtocolError):
        await client.swap_in(rpc, terms, amount=5_000)  # below min_amount


async def test_swap_in_over_nostr() -> None:
    """The full advertise/discover/RPC path over the in-process Nostr relay."""
    server, client, terms, _chain = _build_world()
    relay = LoopbackRelay()
    provider_node = SwapNostrNode(PrivateKey().secret, relay)
    client_node = SwapNostrNode(PrivateKey().secret, relay)
    await provider_node.advertise(terms)

    serve_task = asyncio.create_task(provider_node.serve(server.rpc_handler))
    try:
        adverts = await client_node.discover(timeout=0.3)
        assert adverts and adverts[0].terms.network == "regtest"

        async def rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
            return await client_node.call(provider_node.pubkey, method, params, timeout=8)

        contract = await asyncio.wait_for(client.swap_in(rpc, terms, amount=120_000), timeout=12)
        assert contract.state == SwapState.CLAIMED
    finally:
        serve_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve_task


async def _wait_state(contract: Any, state: SwapState, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if contract.state == state or contract.state.is_terminal:
            return
        await asyncio.sleep(0.01)
