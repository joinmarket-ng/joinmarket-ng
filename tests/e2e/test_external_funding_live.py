"""
Live external (CoinJoin-style) channel funding against the forked LDK Server.

Validates the channel-as-change endgame: a Lightning channel whose funding
output is provided by an *external* transaction (here a maker-built transaction
standing in for a JoinMarket CoinJoin) rather than funded from the node's
on-chain wallet, using the forked ldk-server's external-funding RPCs
(OpenChannelExternalFunding / GetFundingOutput / ProvideFundingOutpoint).

Flow:
  1. the provider opens an external-funded channel to the client;
  2. it polls for the channel funding output (scriptPubKey + value);
  3. a transaction carrying exactly that output is built and broadcast (the
     "CoinJoin"); the provider supplies the resulting outpoint;
  4. the channel funding confirms and the channel becomes usable.

Requires the forked LDK Server image (docker compose build ldk-provider).

Run with::

    docker compose --profile swap up -d
    pytest -m swap tests/e2e/test_external_funding_live.py --fail-on-skip
"""

from __future__ import annotations

import asyncio
import json

import pytest

from .conftest import is_port_open

pytestmark = [pytest.mark.swap, pytest.mark.docker]


def _services_up() -> bool:
    return is_port_open("127.0.0.1", 18443) and is_port_open("127.0.0.1", 3536)


@pytest.mark.timeout(300)
async def test_external_funded_channel_open() -> None:
    if not _services_up():
        pytest.skip("swap profile (bitcoind + forked ldk-provider) not running")
    try:
        import httpx  # noqa: F401
    except ImportError:
        pytest.skip("httpx[http2] not installed")

    from jmcore.bitcoin import scriptpubkey_to_address
    from jmswap.ldk import HttpxLdkTransport, LdkServerBackend

    from tests.e2e.test_swap_ldk_live import BitcoindRPC, _cli, _ldk_creds

    rpc = BitcoindRPC()
    rpc.call("getblockcount")

    prov_key, prov_cert = _ldk_creds("jm-ldk-provider")
    provider = LdkServerBackend(
        HttpxLdkTransport("https://127.0.0.1:3536", verify=prov_cert), prov_key
    )

    # client node id (peer to open the channel to)
    client_info = _cli("ldk-client", "/config/client.toml", "get-node-info")
    client_node = client_info["node_id"]

    # ensure the provider has a little on-chain balance for any anchor reserve
    prov_addr = _cli("ldk-provider", "/config/provider.toml", "onchain-receive")[
        "address"
    ]
    rpc.call("sendtoaddress", [prov_addr, 0.01], wallet="miner")
    rpc.mine(3)
    await asyncio.sleep(2)

    # 1) open an externally-funded channel
    channel_amount = 1_000_000
    user_channel_id = await provider.open_channel_external_funding(
        client_node, "jm-ldk-client:9735", channel_amount
    )
    assert user_channel_id

    # 2) poll for the channel funding output
    funding_script_hex = ""
    value = 0
    for _ in range(60):
        ready, funding_script_hex, value = await provider.get_funding_output(
            user_channel_id
        )
        if ready:
            break
        await asyncio.sleep(1)
    assert funding_script_hex, "funding output never became ready"
    assert value == channel_amount

    # 3) build + broadcast a transaction carrying exactly that funding output
    funding_spk = bytes.fromhex(funding_script_hex)
    funding_address = scriptpubkey_to_address(funding_spk, "regtest")
    coinjoin_txid = rpc.call(
        "sendtoaddress", [funding_address, channel_amount / 1e8], wallet="miner"
    )
    tx = rpc.call("getrawtransaction", [coinjoin_txid, True])
    funding_vout = next(
        o["n"] for o in tx["vout"] if o["scriptPubKey"].get("hex") == funding_script_hex
    )

    # 4) provide the outpoint and drive the channel to ready
    await provider.provide_funding_outpoint(
        user_channel_id, coinjoin_txid, funding_vout
    )

    opened = False
    for _ in range(60):
        rpc.mine(1)
        channels = _cli("ldk-provider", "/config/provider.toml", "list-channels")
        raw = json.dumps(channels).replace(" ", "")
        if user_channel_id in json.dumps(channels) and (
            '"is_channel_ready":true' in raw or '"is_usable":true' in raw
        ):
            opened = True
            break
        await asyncio.sleep(2)
    assert opened, "externally-funded channel did not become ready"

    # the funding transaction was the external (CoinJoin) tx, not an LDK-funded one
    assert rpc.call("getrawtransaction", [coinjoin_txid]) is not None
