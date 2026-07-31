"""Live Electrum-protocol maker swap provider test over Tor-routed Nostr."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import address_to_scriptpubkey, parse_transaction, sha256
from jmcore.crypto import NickIdentity
from jmcore.network import connect_via_tor
from jmcore.protocol import JM_VERSION, MessageType, create_handshake_request
from jmswap.client import SwapClient
from jmswap.direct import DirectOnionClientAdapter
from jmswap.fees import forward_onchain_amount, reverse_onchain_amount
from jmswap.lnd_lightning import LndRestLightningBackend
from jmswap.models import FundingOutput, InvoiceState, ProviderSwapState
from jmswap.nostr import (
    DISCOVERY_D_TAG,
    SWAP_DISCOVERY_KIND,
    ElectrumNostrAdapter,
    RelayEventTransport,
)
from jmswap.nip04 import verify_event

pytestmark = [pytest.mark.docker, pytest.mark.swap_provider_e2e]

DATA_DIR = Path(os.getenv("RING_E2E_DATA_DIR", "/ring-data"))
BITCOIN_RPC_URL = os.getenv("RING_E2E_BITCOIN_RPC_URL", "http://ring-bitcoin:18443")
TOR_HOST = os.getenv("RING_E2E_TOR_SOCKS_HOST", "ring-tor")
TOR_PORT = int(os.getenv("RING_E2E_TOR_SOCKS_PORT", "29050"))
CLIENT_REST_URL = os.getenv(
    "SWAP_PROVIDER_E2E_CLIENT_REST_URL", "https://lnd-taker:8080"
)
RESTART_REQUEST = DATA_DIR / "restart-provider.request"
RESTART_COMPLETE = DATA_DIR / "restart-provider.complete"


def lncli(service: str, *args: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "lncli",
            f"--rpcserver={service}:10009",
            "--network=regtest",
            f"--tlscertpath={DATA_DIR / service / 'tls.cert'}",
            f"--macaroonpath={DATA_DIR / service / 'admin.macaroon'}",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return json.loads(result.stdout)


async def restart_provider() -> None:
    RESTART_REQUEST.write_text("restart\n")
    deadline = asyncio.get_running_loop().time() + 120
    while not RESTART_COMPLETE.is_file():
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("timed out waiting for provider restart")
        await asyncio.sleep(0.25)


def onion_hostname(service: str) -> str:
    return (DATA_DIR / "tor" / service / "hostname").read_text().strip()


def provider_state(swap_id: str) -> dict[str, Any] | None:
    path = DATA_DIR / "maker1" / "swap-provider" / f"{swap_id}.json"
    return json.loads(path.read_text()) if path.is_file() else None


async def bitcoin_rpc(
    method: str, params: list[Any] | None = None, *, wallet: str | None = None
) -> Any:
    suffix = f"/wallet/{wallet}" if wallet else ""
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{BITCOIN_RPC_URL}{suffix}",
            auth=("test", "test"),
            json={
                "jsonrpc": "2.0",
                "id": "swap-e2e",
                "method": method,
                "params": params or [],
            },
        )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload["result"]


async def mine(blocks: int = 1) -> None:
    address = await bitcoin_rpc("getnewaddress", ["", "bech32m"], wallet="ring-funder")
    await bitcoin_rpc("generatetoaddress", [blocks, address])


async def wait_until(predicate: Any, *, timeout: float = 90.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = await predicate()
        if result:
            return result
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("timed out waiting for swap-provider state")
        await asyncio.sleep(0.25)


async def wait_for_bitcoin_rpc(*, timeout: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            await bitcoin_rpc("getblockchaininfo")
        except httpx.HTTPError:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("timed out waiting for Bitcoin RPC") from None
            await asyncio.sleep(0.25)
        else:
            return


async def setup_bidirectional_liquidity() -> None:
    if lncli("lnd-maker1", "listchannels").get("channels"):
        return
    maker_address = lncli("lnd-maker1", "newaddress", "p2tr")["address"]
    await bitcoin_rpc("sendtoaddress", [maker_address, 0.05], wallet="ring-funder")
    await mine(2)
    taker_id = lncli("lnd-taker", "getinfo")["identity_pubkey"]
    taker_onion = onion_hostname("lnd-taker")

    async def connect_taker() -> bool:
        peers = lncli("lnd-maker1", "listpeers").get("peers", [])
        if any(peer.get("pub_key") == taker_id for peer in peers):
            return True
        try:
            lncli(
                "lnd-maker1",
                "connect",
                f"{taker_id}@{taker_onion}:9735",
                "--timeout=30s",
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        return True

    async def open_channel() -> bool:
        pending = lncli("lnd-maker1", "pendingchannels")
        if pending.get("pending_open_channels"):
            return True
        if not await connect_taker():
            return False
        try:
            lncli(
                "lnd-maker1",
                "openchannel",
                "--private",
                "--min_confs=1",
                "--sat_per_vbyte=1",
                taker_id,
                "3000000",
                "1500000",
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        return True

    await wait_until(open_channel, timeout=180)
    await mine(6)

    async def active() -> bool:
        return bool(lncli("lnd-maker1", "listchannels").get("channels"))

    await wait_until(active)


async def discover_provider() -> tuple[
    ElectrumNostrAdapter, Any, str, RelayEventTransport
]:
    relay_host = onion_hostname("swap-nostr")
    transport = RelayEventTransport(
        (f"ws://{relay_host}",),
        socks_host=TOR_HOST,
        socks_port=TOR_PORT,
        stream_isolation=True,
    )
    adapter = ElectrumNostrAdapter(PrivateKey.from_int(101).secret, transport)

    async def found() -> Any:
        offers = await adapter.discover(network="regtest", timeout=10.0)
        return offers[0] if offers else None

    pubkey, offer = await wait_until(found, timeout=180)
    adapter.select_provider(pubkey)
    return adapter, offer, pubkey, transport


def provider_offer_filters(provider_pubkey: str) -> list[dict[str, Any]]:
    return [
        {
            "kinds": [SWAP_DISCOVERY_KIND],
            "authors": [provider_pubkey],
            "#d": [DISCOVERY_D_TAG],
            "#r": ["net:regtest"],
        }
    ]


async def provider_offer_ids(
    transport: RelayEventTransport, provider_pubkey: str
) -> set[str]:
    ready = asyncio.Event()
    identifiers: set[str] = set()

    async def collect() -> None:
        async for event in transport.subscribe_ready(
            provider_offer_filters(provider_pubkey), ready
        ):
            if verify_event(event) and isinstance(event.get("id"), str):
                identifiers.add(event["id"])

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(ready.wait(), 30)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return identifiers


async def wait_for_provider_republish(
    transport: RelayEventTransport, provider_pubkey: str, *, previous_ids: set[str]
) -> None:
    async def wait() -> None:
        async for event in transport.subscribe(provider_offer_filters(provider_pubkey)):
            if (
                verify_event(event)
                and isinstance(event.get("id"), str)
                and event["id"] not in previous_ids
            ):
                return

    try:
        await asyncio.wait_for(wait(), 90)
    except TimeoutError as exc:
        raise RuntimeError("provider did not republish after restart") from exc


async def find_output(address: str) -> FundingOutput | None:
    result = await bitcoin_rpc("scantxoutset", ["start", [f"addr({address})"]])
    for item in result.get("unspents", []):
        txout = await bitcoin_rpc("gettxout", [item["txid"], item["vout"], True])
        if txout is None:
            continue
        return FundingOutput(
            txid=item["txid"],
            vout=item["vout"],
            value=round(float(txout["value"]) * 100_000_000),
            script_pubkey=txout["scriptPubKey"]["hex"],
            confirmations=txout["confirmations"],
        )
    return None


async def find_spender(txid: str, vout: int) -> str | None:
    for candidate in await bitcoin_rpc("getrawmempool"):
        raw = await bitcoin_rpc("getrawtransaction", [candidate, False])
        if any(
            item.txid == txid and item.vout == vout
            for item in parse_transaction(raw).inputs
        ):
            return raw
    return None


async def direct_forward_swap(offer: Any, client_lnd: LndRestLightningBackend) -> None:
    capability = offer.direct_onion
    assert capability is not None
    host, _, port = capability.endpoint.rpartition(":")
    connection = await connect_via_tor(
        host,
        int(port),
        socks_host=TOR_HOST,
        socks_port=TOR_PORT,
        socks_username="swap-e2e-direct",
        socks_password=os.urandom(16).hex(),
    )
    identity = NickIdentity(JM_VERSION)
    handshake = create_handshake_request(
        nick=identity.nick,
        location="NOT-SERVING-ONION",
        network="regtest",
        directory=False,
    )
    await connection.send(
        json.dumps(
            {"type": MessageType.HANDSHAKE.value, "line": json.dumps(handshake)}
        ).encode()
    )
    await connection.receive()

    async def exchange(event: dict[str, Any]) -> dict[str, Any]:
        await connection.send(
            json.dumps(
                {"nick": identity.nick, "cmd": "swaprpc", "data": json.dumps(event)},
                separators=(",", ":"),
            ).encode()
        )
        response = json.loads((await connection.receive()).decode())
        return json.loads(response["data"])

    adapter = DirectOnionClientAdapter(
        PrivateKey.from_int(102).secret, capability.transport_pubkey, exchange
    )
    try:
        client = SwapClient(adapter, network="regtest", lightning=client_lnd)
        amount = 20_000
        record = await client.createnormalswap(
            lightning_amount=amount,
            expected_onchain_amount=forward_onchain_amount(
                amount, offer.percentage_fee, offer.mining_fee
            ),
            current_height=await bitcoin_rpc("getblockcount"),
            user_seckey=PrivateKey.from_int(103).secret,
        )
        invoice = await client_lnd.create_hold_invoice(amount, record.id)
        await client.addswapinvoice(record, invoice)
        await bitcoin_rpc(
            "sendtoaddress",
            [record.address, record.onchain_amount / 100_000_000],
            wallet="ring-funder",
        )
        await mine(1)
        funding = await wait_until(lambda: find_output(record.address))
        raw_claim = await wait_until(lambda: find_spender(funding.txid, funding.vout))
        preimage = next(
            item
            for witness in parse_transaction(raw_claim).witnesses
            for item in witness
            if len(item) == 32 and sha256(item).hex() == record.id
        )

        async def invoice_held() -> bool:
            return await client_lnd.invoice_status(record.id) is InvoiceState.HELD

        await wait_until(invoice_held)
        await client_lnd.settle_hold_invoice(preimage.hex())
    finally:
        await connection.close()


async def test_live_maker_swap_provider_both_directions_and_restart() -> None:
    if os.getenv("SWAP_PROVIDER_E2E") != "1":
        pytest.skip(
            "start the swap-provider-e2e Compose profile and set SWAP_PROVIDER_E2E=1"
        )

    await wait_for_bitcoin_rpc()
    await setup_bidirectional_liquidity()
    adapter, offer, provider_pubkey, transport = await discover_provider()
    client_lnd = LndRestLightningBackend(
        CLIENT_REST_URL,
        tls_cert_path=DATA_DIR / "lnd-taker" / "tls.cert",
        macaroon_path=DATA_DIR / "lnd-taker" / "admin.macaroon",
    )
    await direct_forward_swap(offer, client_lnd)
    client = SwapClient(adapter, network="regtest", lightning=client_lnd)
    initial_balance = lncli("lnd-maker1", "channelbalance")["local_balance"]["sat"]

    forward_amount = 100_000
    forward = await client.createnormalswap(
        lightning_amount=forward_amount,
        expected_onchain_amount=forward_onchain_amount(
            forward_amount, offer.percentage_fee, offer.mining_fee
        ),
        current_height=await bitcoin_rpc("getblockcount"),
        user_seckey=PrivateKey.from_int(104).secret,
    )
    invoice = await client_lnd.create_hold_invoice(forward_amount, forward.id)
    await client.addswapinvoice(forward, invoice)
    await bitcoin_rpc(
        "sendtoaddress",
        [forward.address, forward.onchain_amount / 100_000_000],
        wallet="ring-funder",
    )
    await mine(1)

    funding = await wait_until(lambda: find_output(forward.address))
    raw_claim = await wait_until(lambda: find_spender(funding.txid, funding.vout))
    preimage = next(
        item
        for witness in parse_transaction(raw_claim).witnesses
        for item in witness
        if len(item) == 32 and sha256(item).hex() == forward.id
    )

    async def forward_invoice_held() -> bool:
        return await client_lnd.invoice_status(forward.id) is InvoiceState.HELD

    await wait_until(forward_invoice_held)
    await client_lnd.settle_hold_invoice(preimage.hex())

    reverse_amount = 101_000
    reverse = await client.createswap(
        lightning_amount=reverse_amount,
        expected_onchain_amount=reverse_onchain_amount(
            reverse_amount, offer.percentage_fee, offer.mining_fee
        ),
        maximum_miner_fee=offer.mining_fee * 2,
        current_height=await bitcoin_rpc("getblockcount"),
        user_seckey=PrivateKey.from_int(105).secret,
        preimage=os.urandom(32),
    )
    assert reverse.miner_fee_invoice is not None and reverse.invoice is not None
    await client_lnd.pay_invoice(reverse.miner_fee_invoice)
    main_payment = asyncio.create_task(client_lnd.pay_invoice(reverse.invoice))

    async def persisted_funding() -> FundingOutput | None:
        payload = provider_state(reverse.id)
        if payload is None:
            return None
        funding = payload.get("funding")
        return FundingOutput.model_validate(funding) if funding else None

    reverse_funding = await wait_until(persisted_funding)
    await mine(1)
    confirmed = await wait_until(lambda: find_output(reverse.address))
    assert confirmed.confirmations >= 1

    published_offer_ids = await provider_offer_ids(transport, provider_pubkey)
    await restart_provider()
    await wait_for_provider_republish(
        transport, provider_pubkey, previous_ids=published_offer_ids
    )
    destination = await bitcoin_rpc(
        "getnewaddress", ["", "bech32m"], wallet="ring-funder"
    )
    raw_cooperative = await client.claimtaprootswap(
        reverse,
        confirmed,
        destination_script=address_to_scriptpubkey(destination),
        output_value=confirmed.value - 500,
        locktime=await bitcoin_rpc("getblockcount"),
    )
    await bitcoin_rpc("sendrawtransaction", [raw_cooperative])
    payment = await asyncio.wait_for(main_payment, 30)
    assert payment.state.value == "succeeded"

    async def settled() -> bool:
        payload = provider_state(reverse.id)
        return (
            payload is not None and payload["state"] == ProviderSwapState.SETTLED.value
        )

    await wait_until(settled)
    final_balance = lncli("lnd-maker1", "channelbalance")["local_balance"]["sat"]
    assert final_balance != initial_balance
    assert reverse_funding.txid == confirmed.txid
