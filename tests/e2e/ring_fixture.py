"""Market-independent fixture for private channel-ring integration tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import httpx
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig
from jmcore.models import NetworkType, OfferType
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.service import WalletService
from pydantic import SecretStr

from taker.config import BroadcastPolicy, MaxCjFee, TakerConfig
from taker.taker import Taker

ROOT = Path(__file__).resolve().parents[2]


COMPOSE_FILE = ROOT / "jmswap" / "docker-compose.ring-e2e.yml"


HOST_COMPOSE_FILE = ROOT / "jmswap" / "docker-compose.ring-e2e.host.yml"


DATA_DIR = Path(os.getenv("RING_E2E_DATA_DIR", ROOT / "tmp" / "ring-e2e"))


PROJECT = os.getenv("RING_E2E_PROJECT", "jm-ring-e2e")


RPC_URL = f"http://127.0.0.1:{os.getenv('RING_E2E_BITCOIN_PORT', '20443')}"


DIRECTORY = f"127.0.0.1:{os.getenv('RING_E2E_DIRECTORY_PORT', '25222')}"


TOR_SOCKS_PORT = int(os.getenv("RING_E2E_TOR_SOCKS_PORT", "29050"))


TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)


RING_LND_SERVICES = ("lnd-maker1", "lnd-maker2", "lnd-maker3", "lnd-taker")


LND_SERVICES = (*RING_LND_SERVICES, "lnd-maker4")


LND_PORTS = {
    "lnd-maker1": os.getenv("RING_E2E_MAKER1_GRPC_PORT", "21009"),
    "lnd-maker2": os.getenv("RING_E2E_MAKER2_GRPC_PORT", "22009"),
    "lnd-maker3": os.getenv("RING_E2E_MAKER3_GRPC_PORT", "23009"),
    "lnd-maker4": os.getenv("RING_E2E_MAKER4_GRPC_PORT", "25009"),
    "lnd-taker": os.getenv("RING_E2E_TAKER_GRPC_PORT", "24009"),
}


MINE_BATCH_BLOCKS = 10


async def bitcoin_rpc(method: str, params: list[Any] | None = None) -> Any:
    deadline = asyncio.get_running_loop().time() + 60
    delay = 1.0
    while True:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    RPC_URL,
                    auth=("test", "test"),
                    json={
                        "jsonrpc": "2.0",
                        "id": "ring-e2e",
                        "method": method,
                        "params": params or [],
                    },
                )
            break
        except httpx.ConnectError:
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    f"Bitcoin RPC {method} did not become reachable"
                ) from None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload["result"]


def compose(
    *args: str, environment: dict[str, str] | None = None, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if environment:
        env.update(environment)
    compose_files = ["-f", str(COMPOSE_FILE), "-f", str(HOST_COMPOSE_FILE)]
    if ipam_file := env.get("RING_E2E_IPAM_FILE"):
        compose_files.extend(("-f", ipam_file))
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            *compose_files,
            "--profile",
            "ring-e2e",
            *args,
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def lncli(service: str, *args: str, timeout: float = 180) -> dict[str, Any]:
    result = compose(
        "exec",
        "-T",
        service,
        "lncli",
        "--network=regtest",
        "--tlscertpath=/root/.lnd/tls.cert",
        "--macaroonpath=/root/.lnd/admin.macaroon",
        *args,
        timeout=timeout,
    )
    return json.loads(result.stdout)


def lnd_path(service: str, filename: str) -> Path:
    return DATA_DIR / service / filename


def onion_endpoint(service: str) -> str:
    info = lncli(service, "getinfo")
    endpoints = [str(uri).partition("@")[2] for uri in info["uris"] if "@" in str(uri)]
    if len(endpoints) != 1:
        raise RuntimeError(
            f"{service} did not advertise exactly one endpoint: {endpoints}"
        )
    return endpoints[0]


def channel_ring_config(
    data_dir: Path, *, journal_dir: Path | None = None, with_taker_node: bool = True
) -> ChannelRingConfig:
    nodes = (
        {
            "taker": ChannelRingNodeConfig(
                lnd_grpc_url=f"https://127.0.0.1:{LND_PORTS['lnd-taker']}",
                lnd_tls_cert_path=lnd_path("lnd-taker", "tls.cert"),
                lnd_macaroon_path=lnd_path("lnd-taker", "admin.macaroon"),
                onion_endpoint=onion_endpoint("lnd-taker"),
            )
        }
        if with_taker_node
        else {}
    )
    return ChannelRingConfig(
        enabled=True,
        nodes=nodes,
        mixdepth_nodes={0: "taker"} if with_taker_node else {},
        # The maker registry is mounted into non-root containers. Host-side
        # taker enrollment must not atomically replace it with a root-owned file.
        node_binding_directory=data_dir / "node-bindings",
        minimum_makers=3,
        min_channel_capacity=1_000_000,
        max_channel_capacity=20_000_000,
        max_push=500_000,
        opener_reserve=10_000,
        fundee_reserve=10_000,
        maximum_commitment_fee=100_000,
        spendable_margin=100_000,
        confirmation_depth=3,
        allowed_csv_delays=(144,),
        minimum_csv_delay=144,
        maximum_csv_delay=144,
        phase_timeout_seconds=300.0,
        open_timeout_seconds=300.0,
        readiness_timeout_seconds=120.0,
        setup_timeout_seconds=600.0,
        hold_safety_margin_seconds=30.0,
        maker_setup_hold_seconds=660.0,
        max_active_sessions=4,
        max_verified_sessions=2,
        persistence_directory=(journal_dir or data_dir) / "rings",
    )


def taker_config(
    data_dir: Path, *, journal_dir: Path | None = None, with_taker_node: bool = True
) -> TakerConfig:
    return TakerConfig(
        mnemonic=SecretStr(TAKER_MNEMONIC),
        network=NetworkType.REGTEST,
        bitcoin_network=NetworkType.REGTEST,
        data_dir=data_dir,
        backend_type="descriptor_wallet",
        backend_config={"rpc_url": RPC_URL, "rpc_user": "test", "rpc_password": "test"},
        directory_servers=[DIRECTORY],
        socks_host="127.0.0.1",
        socks_port=TOR_SOCKS_PORT,
        address_type="p2tr",
        counterparty_count=4,
        minimum_makers=3,
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        channel_ring=channel_ring_config(
            data_dir, journal_dir=journal_dir, with_taker_node=with_taker_node
        ),
        max_cj_fee=MaxCjFee(abs_fee=3_000, rel_fee="0.01"),
        bondless_makers_allowance=1.0,
        bondless_makers_allowance_require_zero_fee=False,
        max_maker_replacement_attempts=0,
        taker_utxo_age=5,
        maker_timeout_sec=120,
        order_wait_time=45.0,
        orderbook_min_wait=5.0,
        orderbook_quiet_period=3.0,
        # Let the full-node estimator respect the current mempool floor;
        # retained regtest stacks can raise it above a fixed 2 sat/vB.
        fee_rate=None,
        tx_fee_factor=0.0,
        tx_broadcast=BroadcastPolicy.SELF,
    )


async def make_taker(
    wallet: WalletService, backend: DescriptorWalletBackend, config: TakerConfig
) -> Taker:
    if config.channel_ring.enabled:
        from jmswap.channel_ring_nodes import (
            channel_ring_wallet_identity,
            enroll_configured_ring_nodes,
        )

        await enroll_configured_ring_nodes(
            config.channel_ring,
            network="regtest",
            offer_type=config.preferred_offer_type.value,
            wallet_identity=channel_ring_wallet_identity(
                wallet.master_key.get_public_key_bytes()
            ),
            mixdepth_count=wallet.mixdepth_count,
            expected_node_ids={
                "taker": lncli("lnd-taker", "getinfo")["identity_pubkey"]
            },
            acknowledge_prior_use=True,
        )
    taker = Taker(wallet, backend, config)
    await taker.start()
    return taker


async def wait_for_ring_channels(
    timeout: float = 120,
) -> dict[str, list[dict[str, Any]]]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        channels = {
            service: list(
                (await asyncio.to_thread(lncli, service, "listchannels"))["channels"]
            )
            for service in RING_LND_SERVICES
        }
        if all(
            len(items) == 2 and all(item["active"] for item in items)
            for items in channels.values()
        ):
            return channels
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"ring channels did not become active: {channels}")
        await asyncio.sleep(2)


async def exercise_maker_channels(record: Any, mining_address: str) -> None:
    """Prove each created maker edge carries a payment and closes cooperatively.

    This is an ordinary channel lifecycle, not an escrow buyout or timeout split.
    Never reset payment history or stop the makers while an outcome is unknown.
    """
    services = ("lnd-maker1", "lnd-maker2", "lnd-maker3")

    async def rpc(service: str, *args: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(lncli, service, *args)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Command arguments can contain invoices and preimages.
            raise RuntimeError(
                f"{service} lifecycle {args[0]} failed; retain LND history"
            ) from None

    by_node = {
        (await rpc(service, "getinfo"))["identity_pubkey"]: service
        for service in services
    }
    by_key = {peer.participant_key: peer.node_id for peer in record.participants}
    assert record.manifest is not None
    points: dict[str, tuple[str, str]] = {}

    async def channel(service: str, point: str) -> dict[str, Any]:
        items = (await rpc(service, "listchannels"))["channels"]
        matches = [item for item in items if item["channel_point"] == point]
        assert len(matches) == 1, f"{service} does not have the expected channel"
        return matches[0]

    for ordinal, edge in enumerate(record.manifest.edges):
        opener, fundee = (
            by_node[by_key[edge.opener_key]],
            by_node[by_key[edge.acceptor_key]],
        )
        point = f"{record.manifest.unsigned_txid}:{edge.output_index}"
        points[point] = (opener, fundee)
        before = await channel(opener, point)
        received_before = await channel(fundee, point)
        for state, peer in (
            (before, edge.acceptor_key),
            (received_before, edge.opener_key),
        ):
            assert state["remote_pubkey"] == by_key[peer]
            assert state["private"] and state["active"]
            assert (
                int(state["capacity"]) == edge.capacity and not state["pending_htlcs"]
            )
        amount = 1_000
        invoice = await rpc(
            fundee, "addinvoice", f"--amt={amount}", f"--memo=maker-edge-{ordinal}"
        )
        decoded = await rpc(fundee, "decodepayreq", invoice["payment_request"])
        assert decoded["route_hints"] == []
        assert decoded["destination"] == by_key[edge.acceptor_key]
        assert decoded["payment_hash"] == invoice["r_hash"]
        assert int(decoded["num_satoshis"]) == amount
        # Derive the public SCID from the actual funding block. Private/alias
        # channel IDs reported by ListChannels are not always usable by BuildRoute.
        funding = await bitcoin_rpc(
            "getrawtransaction", [record.manifest.unsigned_txid, True]
        )
        block = await bitcoin_rpc("getblock", [funding["blockhash"], 1])
        scid = (
            (int(block["height"]) << 40)
            | (block["tx"].index(record.manifest.unsigned_txid) << 16)
            | edge.output_index
        )
        built = await rpc(
            opener,
            "buildroute",
            f"--amt={amount}",
            f"--final_cltv_delta={decoded['cltv_expiry']}",
            f"--hops={decoded['destination']}",
            f"--outgoing_chan_id={scid}",
        )
        hops = built["route"]["hops"]
        assert len(hops) == 1 and int(hops[0]["chan_id"]) == scid
        assert hops[0]["pub_key"] == decoded["destination"]
        assert int(hops[0]["amt_to_forward_msat"]) == amount * 1_000
        assert int(hops[0]["fee_msat"]) == 0
        hops[0]["mpp_record"] = {
            "payment_addr": decoded["payment_addr"],
            "total_amt_msat": str(amount * 1_000),
        }
        hops[0]["tlv_payload"] = True
        sent = await rpc(
            opener,
            "sendtoroute",
            f"--payment_hash={invoice['r_hash']}",
            json.dumps(built),
        )
        assert sent["status"] == "SUCCEEDED" and not sent.get("failure")
        assert (
            hashlib.sha256(bytes.fromhex(sent["preimage"])).hexdigest()
            == invoice["r_hash"]
        )
        assert [int(hop["chan_id"]) for hop in sent["route"]["hops"]] == [scid]
        settled = await rpc(fundee, "lookupinvoice", invoice["r_hash"])
        assert (
            settled["state"] == "SETTLED"
            and int(settled["amt_paid_msat"]) == amount * 1_000
        )
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            after, received_after = (
                await channel(opener, point),
                await channel(fundee, point),
            )
            if (
                int(after["total_satoshis_sent"])
                == int(before["total_satoshis_sent"]) + amount
                and int(received_after["total_satoshis_received"])
                == int(received_before["total_satoshis_received"]) + amount
                and not after["pending_htlcs"]
                and not received_after["pending_htlcs"]
            ):
                break
            assert asyncio.get_running_loop().time() < deadline, (
                f"edge {ordinal} payment incomplete"
            )
            await asyncio.sleep(1)
        assert int(after["local_balance"]) < int(before["local_balance"])
        assert int(received_after["local_balance"]) > int(
            received_before["local_balance"]
        )

    # Close only after every payment is terminal; never force close as a shortcut.
    for point, (opener, _) in points.items():
        txid, vout = point.split(":")
        await rpc(
            opener, "closechannel", f"--funding_txid={txid}", f"--output_index={vout}"
        )
    deadline = asyncio.get_running_loop().time() + 120
    while True:
        complete = True
        confirmations = 1
        for service in services:
            closed = {
                item["channel_point"]: item
                for item in (await rpc(service, "closedchannels"))["channels"]
            }
            for point, endpoints in points.items():
                if service not in endpoints:
                    continue
                if point not in closed:
                    complete = False
                else:
                    summary = closed[point]
                    assert summary["close_type"] == "COOPERATIVE_CLOSE"
                    assert int(summary["close_height"]) > 0
                    assert int(summary["time_locked_balance"]) == 0
            assert not any(
                item.get("pending_htlcs")
                for item in (await rpc(service, "listchannels"))["channels"]
            )
            pending = await rpc(service, "pendingchannels")
            for group in (
                "pending_open_channels",
                "pending_closing_channels",
                "pending_force_closing_channels",
                "waiting_close_channels",
            ):
                for item in pending.get(group, []):
                    if item["channel"]["channel_point"] in points:
                        complete = False
                        confirmations = max(
                            confirmations,
                            int(item.get("blocks_til_close_confirmed", 1)),
                        )
        if complete:
            break
        assert asyncio.get_running_loop().time() < deadline, (
            "maker channel cooperative closure incomplete"
        )
        await mine_and_sync(
            min(10, max(1, confirmations)), mining_address, services=services
        )
        await asyncio.sleep(1)
    for service in services:
        assert (await rpc(service, "listchannels"))["channels"] == []


async def mine_and_sync(
    count: int,
    address: str,
    timeout: float = 120,
    services: tuple[str, ...] = LND_SERVICES,
) -> None:
    """Mine ``count`` blocks and wait for every ring node to catch up."""
    # One call for a whole CSV delay can outlast the RPC read timeout on a busy
    # host, and a timed-out generatetoaddress cannot be retried safely.
    for mined in range(0, count, MINE_BATCH_BLOCKS):
        await bitcoin_rpc(
            "generatetoaddress", [min(MINE_BATCH_BLOCKS, count - mined), address]
        )
    height = int(await bitcoin_rpc("getblockcount"))
    deadline = asyncio.get_running_loop().time() + timeout
    for service in services:
        while True:
            info = await asyncio.to_thread(lncli, service, "getinfo")
            if info.get("synced_to_chain") and int(info["block_height"]) >= height:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"{service} did not sync to block {height}")
            await asyncio.sleep(1)
