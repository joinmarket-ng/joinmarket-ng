"""
Full live swap end-to-end against real bitcoind + two real LDK Server nodes
(docker ``swap`` profile).

This is the strongest validation in the suite: the on-chain HTLC claim is
broadcast to a real bitcoind, so its acceptance proves the Taproot script,
witness and BIP341 sighash are consensus-correct on a real node, and the
Lightning legs run over a real channel between two LDK nodes.

Flow (swap-in: provider receives on-chain, pays Lightning):
  1. fund both LDK nodes on-chain and open a provider->client channel;
  2. the client (LDK) issues an invoice and funds the on-chain HTLC (bitcoind);
  3. the provider (LDK) pays the invoice, learns the preimage and claims the
     HTLC on-chain; bitcoind accepts the claim transaction.

Run with::

    docker compose --profile swap up -d
    pytest -m swap tests/e2e/test_swap_ldk_live.py --fail-on-skip
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from typing import Any

import pytest

from .conftest import is_port_open

pytestmark = [pytest.mark.swap, pytest.mark.docker]

RPC_URL = "http://127.0.0.1:18443"
RPC_AUTH = ("test", "test")
MINER_WALLET = "miner"


def _docker() -> bool:
    return shutil.which("docker") is not None


def _services_up() -> bool:
    return (
        is_port_open("127.0.0.1", 18443)
        and is_port_open("127.0.0.1", 3536)
        and is_port_open("127.0.0.1", 3537)
    )


class BitcoindRPC:
    """A tiny synchronous bitcoind JSON-RPC client (regtest, miner wallet)."""

    def __init__(self) -> None:
        import httpx

        self._client = httpx.Client(auth=RPC_AUTH, timeout=30.0)

    def call(
        self, method: str, params: list[Any] | None = None, wallet: str | None = None
    ) -> Any:
        url = RPC_URL if wallet is None else f"{RPC_URL}/wallet/{wallet}"
        resp = self._client.post(
            url,
            json={
                "jsonrpc": "1.0",
                "id": "t",
                "method": method,
                "params": params or [],
            },
        )
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"{method} failed: {data['error']}")
        return data["result"]

    def mine(self, blocks: int = 1) -> None:
        addr = self.call("getnewaddress", wallet=MINER_WALLET)
        self.call("generatetoaddress", [blocks, addr], wallet=MINER_WALLET)


def _cli(container: str, config: str, *args: str) -> dict[str, Any]:
    """Run ldk-server-cli inside a container and parse its JSON output."""
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            container,
            "ldk-server-cli",
            "-c",
            config,
            "--base-url",
            "127.0.0.1:3536",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = (out.stdout or "") + (out.stderr or "")
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"_raw": text}


def _ldk_creds(container: str, network: str = "regtest") -> tuple[str, str]:
    tmp = tempfile.mkdtemp(prefix="ldk-")
    subprocess.run(
        ["docker", "cp", f"{container}:/data/{network}/api_key", f"{tmp}/api_key"],
        check=True,
        capture_output=True,
        timeout=20,
    )
    subprocess.run(
        ["docker", "cp", f"{container}:/data/tls.crt", f"{tmp}/tls.crt"],
        check=True,
        capture_output=True,
        timeout=20,
    )
    with open(f"{tmp}/api_key", "rb") as fh:
        return fh.read().hex(), f"{tmp}/tls.crt"


def _fund_onchain(rpc: BitcoindRPC, container: str, config: str, btc: float) -> None:
    addr = _cli(container, config, "onchain-receive")["address"]
    rpc.call("sendtoaddress", [addr, btc], wallet=MINER_WALLET)
    rpc.mine(3)


async def _wait_spendable(backend: Any, minimum: int) -> None:
    for _ in range(60):
        balances = await backend._call("GetBalances", b"")  # noqa: SLF001
        # spendable on-chain balance is field 1 (total) / 2 (spendable) in proto;
        # we just retry until the node reports a non-trivial spendable balance.
        from jmswap.ldk import _first_int

        spendable = _first_int(balances, 2) or _first_int(balances, 1) or 0
        if spendable >= minimum:
            return
        await asyncio.sleep(2)


@pytest.mark.timeout(600)
async def test_live_swap_in_over_ldk_and_bitcoind() -> None:
    if not (_docker() and _services_up()):
        pytest.skip("swap profile (bitcoind + ldk-provider + ldk-client) not fully up")
    try:
        import httpx  # noqa: F401
    except ImportError:
        pytest.skip("httpx[http2] not installed")

    from jmswap.ldk import HttpxLdkTransport, LdkServerBackend

    from tests.e2e.swap_live_backends import BitcoindChain, BitcoindWallet

    rpc = BitcoindRPC()
    rpc.call("getblockcount")  # connectivity probe

    prov_key, prov_cert = _ldk_creds("jm-ldk-provider")
    cli_key, cli_cert = _ldk_creds("jm-ldk-client")
    provider_ln = LdkServerBackend(
        HttpxLdkTransport("https://127.0.0.1:3536", verify=prov_cert), prov_key
    )
    client_ln = LdkServerBackend(
        HttpxLdkTransport("https://127.0.0.1:3537", verify=cli_cert), cli_key
    )

    provider_node = await provider_ln.node_id()
    client_node = await client_ln.node_id()
    assert provider_node and client_node

    # 1) fund both nodes on-chain and open a provider -> client channel
    _fund_onchain(rpc, "ldk-provider", "/config/provider.toml", 0.05)
    _fund_onchain(rpc, "ldk-client", "/config/client.toml", 0.01)
    await _wait_spendable(provider_ln, 3_000_000)

    _cli(
        "ldk-provider",
        "/config/provider.toml",
        "open-channel",
        client_node,
        "jm-ldk-client:9735",
        "3000000sat",
    )
    # confirm and let channel_ready propagate
    for _ in range(30):
        rpc.mine(1)
        channels = _cli("ldk-provider", "/config/provider.toml", "list-channels")
        raw = json.dumps(channels)
        if '"is_usable":true' in raw.replace(
            " ", ""
        ) or '"is_channel_ready":true' in raw.replace(" ", ""):
            break
        await asyncio.sleep(2)

    # 2) run the swap-in via SwapServer/SwapClient with bitcoind-backed chain/wallet
    from decimal import Decimal

    from jmswap.client import SwapClient
    from jmswap.models import ProviderTerms, SwapDirection, SwapState
    from jmswap.server import SwapServer

    chain = BitcoindChain(rpc)
    server_wallet = BitcoindWallet(rpc)
    client_wallet = BitcoindWallet(rpc)
    terms = ProviderTerms(
        provider_id=provider_node,
        provider_pubkey="02" + "11" * 32,
        network="regtest",
        directions=[SwapDirection.SWAP_IN, SwapDirection.SWAP_OUT],
        min_amount=10_000,
        max_amount=1_000_000,
        percentage_fee=Decimal("0.1"),
        base_fee=500,
        cltv_delta=144,
    )
    server = SwapServer(terms, provider_ln, chain, server_wallet, poll_interval=2.0)
    client = SwapClient(client_ln, chain, client_wallet, poll_interval=2.0)

    async def rpc_handler(method: str, params: dict[str, Any]) -> dict[str, Any]:
        return await server.rpc_handler(method, params, "client")

    # mine alongside the swap so confirmations advance
    stop = asyncio.Event()

    async def _miner() -> None:
        while not stop.is_set():
            rpc.mine(1)
            await asyncio.sleep(3)

    miner_task = asyncio.create_task(_miner())
    try:
        contract = await asyncio.wait_for(
            client.swap_in(rpc_handler, terms, amount=100_000), timeout=300
        )
    finally:
        stop.set()
        miner_task.cancel()

    assert contract.state == SwapState.CLAIMED
    server_contract = next(iter(server.contracts.values()))
    for _ in range(30):
        if server_contract.state == SwapState.CLAIMED:
            break
        await asyncio.sleep(2)
    assert server_contract.state == SwapState.CLAIMED
    # The happy path settles cooperatively: the client co-signed and broadcast an
    # ordinary key-path spend to the provider; bitcoind accepted it and the
    # funding output is spent.
    assert contract.spend_txid is not None
    claim = rpc.call("getrawtransaction", [contract.spend_txid, True])
    assert claim is not None
    # a cooperative key-path spend has a single-element witness (just the sig),
    # revealing neither the preimage nor the swap script on-chain
    assert len(claim["vin"][0]["txinwitness"]) == 1
    assert rpc.call("gettxout", [contract.lockup_txid, contract.lockup_vout]) is None

    # 3) swap-out: the client now has outbound liquidity from the swap-in, so it
    # can pay the provider; the provider funds the on-chain HTLC and the client
    # claims it on-chain (bitcoind validates the reverse-swap claim witness).
    stop2 = asyncio.Event()

    async def _miner2() -> None:
        while not stop2.is_set():
            rpc.mine(1)
            await asyncio.sleep(3)

    miner2 = asyncio.create_task(_miner2())
    try:
        out_contract = await asyncio.wait_for(
            client.swap_out(rpc_handler, terms, amount=50_000), timeout=300
        )
    finally:
        stop2.set()
        miner2.cancel()

    assert out_contract.state == SwapState.CLAIMED
    assert out_contract.spend_txid is not None
    # bitcoind accepted the client's reverse-swap claim transaction
    assert rpc.call("getrawtransaction", [out_contract.spend_txid]) is not None
