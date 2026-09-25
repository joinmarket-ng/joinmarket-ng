"""Live four-party co-funded channel-ring test against stock LND nodes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from jmcore.bitcoin import parse_transaction
from jmcore.channel_ring import ChannelRingConfig
from jmcore.channel_ring_store import RingLifecycleState
from jmcore.models import NetworkType, OfferType
from jmswap.lnd import LndBackend
from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    get_mnemonic_fingerprint,
)
from jmwallet.wallet.service import WalletService

from taker.channel_ring import reconcile_taker_ring_records
from taker.config import BroadcastPolicy, MaxCjFee, TakerConfig
from taker.taker import Taker

pytestmark = [pytest.mark.docker, pytest.mark.ring_e2e]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "jmswap" / "docker-compose.ring-e2e.yml"
DATA_DIR = Path(os.getenv("RING_E2E_DATA_DIR", ROOT / "tmp" / "ring-e2e"))
PROJECT = os.getenv("RING_E2E_PROJECT", "jm-ring-e2e")
RPC_URL = f"http://127.0.0.1:{os.getenv('RING_E2E_BITCOIN_PORT', '20443')}"
DIRECTORY = f"127.0.0.1:{os.getenv('RING_E2E_DIRECTORY_PORT', '25222')}"
TOR_SOCKS_PORT = int(os.getenv("RING_E2E_TOR_SOCKS_PORT", "29050"))
TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)

LND_SERVICES = ("lnd-maker1", "lnd-maker2", "lnd-maker3", "lnd-taker")
LND_PORTS = {
    "lnd-maker1": os.getenv("RING_E2E_MAKER1_GRPC_PORT", "21009"),
    "lnd-maker2": os.getenv("RING_E2E_MAKER2_GRPC_PORT", "22009"),
    "lnd-maker3": os.getenv("RING_E2E_MAKER3_GRPC_PORT", "23009"),
    "lnd-taker": os.getenv("RING_E2E_TAKER_GRPC_PORT", "24009"),
}


async def bitcoin_rpc(method: str, params: list[Any] | None = None) -> Any:
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
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload["result"]


def compose(
    *args: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if environment:
        env.update(environment)
    return subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(COMPOSE_FILE),
            "--profile",
            "ring-e2e",
            *args,
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


def lncli(service: str, *args: str) -> dict[str, Any]:
    result = compose(
        "exec",
        "-T",
        service,
        "lncli",
        "--network=regtest",
        "--tlscertpath=/root/.lnd/tls.cert",
        "--macaroonpath=/root/.lnd/admin.macaroon",
        *args,
    )
    return json.loads(result.stdout)


async def wait_for_file(path: Path, timeout: float = 90) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.is_file() or path.stat().st_size == 0:
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"credential was not created: {path}")
        await asyncio.sleep(0.25)


async def wait_for_maker3(timeout: float = 90) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        logs = await asyncio.to_thread(compose, "logs", "--no-color", "ring-maker3")
        if "Maker bot started. Listening for takers" in logs.stdout:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("ring-maker3 did not become ready")
        await asyncio.sleep(1)


async def recreate_maker3(fundee_reserve: int) -> None:
    await asyncio.to_thread(
        compose,
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "ring-maker3",
        environment={"RING_E2E_MAKER3_FUNDEE_RESERVE": str(fundee_reserve)},
    )
    await wait_for_maker3()


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


def channel_ring_config(data_dir: Path) -> ChannelRingConfig:
    return ChannelRingConfig(
        enabled=True,
        lnd_grpc_url=f"https://127.0.0.1:{LND_PORTS['lnd-taker']}",
        lnd_tls_cert_path=lnd_path("lnd-taker", "tls.cert"),
        lnd_macaroon_path=lnd_path("lnd-taker", "admin.macaroon"),
        onion_endpoint=onion_endpoint("lnd-taker"),
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
        max_active_sessions=4,
        max_verified_sessions=2,
        persistence_directory=data_dir / "rings",
    )


def taker_config(data_dir: Path) -> TakerConfig:
    return TakerConfig(
        mnemonic=TAKER_MNEMONIC,
        network=NetworkType.REGTEST,
        bitcoin_network=NetworkType.REGTEST,
        data_dir=data_dir,
        backend_type="descriptor_wallet",
        backend_config={"rpc_url": RPC_URL, "rpc_user": "test", "rpc_password": "test"},
        directory_servers=[DIRECTORY],
        socks_host="127.0.0.1",
        socks_port=TOR_SOCKS_PORT,
        address_type="p2tr",
        counterparty_count=3,
        minimum_makers=3,
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        channel_ring=channel_ring_config(data_dir),
        max_cj_fee=MaxCjFee(abs_fee=3_000, rel_fee="0.01"),
        bondless_makers_allowance=1.0,
        bondless_makers_allowance_require_zero_fee=False,
        max_maker_replacement_attempts=0,
        taker_utxo_age=5,
        maker_timeout_sec=120,
        order_wait_time=45.0,
        orderbook_min_wait=5.0,
        orderbook_quiet_period=3.0,
        fee_rate=2.0,
        tx_fee_factor=0.0,
        tx_broadcast=BroadcastPolicy.SELF,
    )


async def make_taker(
    wallet: WalletService, backend: DescriptorWalletBackend, config: TakerConfig
) -> Taker:
    taker = Taker(wallet, backend, config)
    await taker.start()
    return taker


async def wait_for_channels(timeout: float = 120) -> dict[str, list[dict[str, Any]]]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        channels = {
            service: list(
                (await asyncio.to_thread(lncli, service, "listchannels"))["channels"]
            )
            for service in LND_SERVICES
        }
        if all(
            len(items) == 2 and all(item["active"] for item in items)
            for items in channels.values()
        ):
            return channels
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"ring channels did not become active: {channels}")
        await asyncio.sleep(2)


async def assert_no_pending_channels() -> None:
    for service in LND_SERVICES:
        pending = await asyncio.to_thread(lncli, service, "pendingchannels")
        assert not pending["pending_open_channels"]
        assert not pending["pending_closing_channels"]
        assert not pending["pending_force_closing_channels"]
        assert not pending["waiting_close_channels"]


async def pay_directed_edges(node_service: dict[str, str], record: Any) -> None:
    participant_node = {
        participant.participant_key: participant.node_id
        for participant in record.coordinator_participants
    }
    assert record.manifest is not None
    for index, edge in enumerate(record.manifest.edges):
        opener = node_service[participant_node[edge.opener_key]]
        fundee = node_service[participant_node[edge.acceptor_key]]
        invoice = await asyncio.to_thread(
            lncli, fundee, "addinvoice", "--amt=1000", f"--memo=ring-edge-{index}"
        )
        payment = await asyncio.to_thread(
            lncli,
            opener,
            "payinvoice",
            "--json",
            "--force",
            "--fee_limit=100",
            str(invoice["payment_request"]),
        )
        assert not payment.get("payment_error")
        assert payment.get("payment_preimage")


@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_rejected_revision_then_complete_four_party_ring(tmp_path: Path) -> None:
    if os.getenv("RING_E2E") != "1":
        pytest.skip("start the ring-e2e Compose profile and set RING_E2E=1")

    credential_paths = [
        path
        for service in LND_SERVICES
        for path in (lnd_path(service, "tls.cert"), lnd_path(service, "admin.macaroon"))
    ]
    await asyncio.gather(*(wait_for_file(path) for path in credential_paths))

    backend = DescriptorWalletBackend(
        rpc_url=RPC_URL,
        rpc_user="test",
        rpc_password="test",
        wallet_name=f"jm_{get_mnemonic_fingerprint(TAKER_MNEMONIC)}_ring_e2e_regtest",
    )
    wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=backend,
        network="regtest",
        mixdepth_count=5,
        address_type="p2tr",
        data_dir=tmp_path,
    )
    config = taker_config(tmp_path)
    taker: Taker | None = None
    restart_taker: Taker | None = None
    lnd_backends: list[LndBackend] = []
    try:
        await recreate_maker3(15_000)
        # The rejected plan cannot progress to channel opens; bound this wait
        # separately from the full Tor/LND negotiation budget used below.
        rejected_config = config.model_copy(
            update={
                "channel_ring": config.channel_ring.model_copy(
                    update={"phase_timeout_seconds": 60.0}
                )
            }
        )
        taker = await make_taker(wallet, backend, rejected_config)
        destination = wallet.get_receive_address(1, 0)
        rejected_txid = await taker.do_coinjoin(
            amount=1_000_000, destination=destination, mixdepth=0, counterparty_count=3
        )
        assert rejected_txid is None
        assert taker.last_failure_reason == "Strict channel-ring negotiation aborted"
        assert taker._channel_ring_store is not None
        rejected_records = taker._channel_ring_store.load_all().records
        assert len(rejected_records) == 1
        assert len(rejected_records[0].coordinator_plans) == 4
        assert rejected_records[0].state is RingLifecycleState.RETIRED
        await assert_no_pending_channels()

        await taker.stop(close_wallet=False)
        taker = None
        await recreate_maker3(10_000)

        taker = await make_taker(wallet, backend, config)
        destination = wallet.get_receive_address(1, 1)
        txid = await taker.do_coinjoin(
            amount=1_000_000, destination=destination, mixdepth=0, counterparty_count=3
        )
        assert txid is not None
        coordinator = taker._session.ring_coordinator
        assert coordinator is not None
        record = coordinator._record()
        assert record.state is RingLifecycleState.BROADCAST
        assert record.manifest is not None
        manifest = record.manifest
        assert manifest.unsigned_txid == txid
        assert len(manifest.participant_keys) == 4
        assert len(manifest.edges) == 4
        assert len(manifest.equal_output_indices) == 4
        assert len(manifest.outputs) == 8

        raw_tx = str(await bitcoin_rpc("getrawtransaction", [txid]))
        parsed = parse_transaction(raw_tx)
        assert len(parsed.outputs) == 8
        assert sum(output.value == 1_000_000 for output in parsed.outputs) == 4
        assert all(output.script.startswith(b"\x51\x20") for output in parsed.outputs)
        manifest_outputs = {
            (output.index, output.amount, output.script_pubkey)
            for output in manifest.outputs
        }
        assert manifest_outputs == {
            (index, output.value, output.script.hex())
            for index, output in enumerate(parsed.outputs)
        }

        input_value = 0
        for tx_input in parsed.inputs:
            previous = await bitcoin_rpc("getrawtransaction", [tx_input.txid, True])
            input_value += int(
                Decimal(str(previous["vout"][tx_input.vout]["value"])) * 100_000_000
            )
        actual_fee = input_value - sum(output.value for output in parsed.outputs)
        maker_txfees = sum(
            session.offer.txfee for session in taker._session.maker_sessions.values()
        )
        assert actual_fee == record.coordinator_tx_fee + maker_txfees

        await bitcoin_rpc("generatetoaddress", [3, destination])
        channels = await wait_for_channels()
        all_points = {
            str(channel["channel_point"])
            for items in channels.values()
            for channel in items
        }
        expected_points = {f"{txid}:{edge.output_index}" for edge in manifest.edges}
        assert all_points == expected_points
        assert sum(len(items) for items in channels.values()) == 8
        for items in channels.values():
            assert len({str(item["remote_pubkey"]) for item in items}) == 2
            assert all(item["private"] for item in items)
            assert all(
                item["commitment_type"] in {7, "7", "SIMPLE_TAPROOT", "TAPROOT"}
                for item in items
            )
            assert all(
                int(item["local_balance"]) + int(item["remote_balance"])
                < int(item["capacity"])
                for item in items
            )

        for service in LND_SERVICES:
            lnd_backends.append(
                LndBackend.from_paths(
                    f"127.0.0.1:{LND_PORTS[service]}",
                    lnd_path(service, "tls.cert"),
                    lnd_path(service, "admin.macaroon"),
                )
            )
        node_infos = await asyncio.gather(*(item.node_info() for item in lnd_backends))
        node_service = {
            info.identity_pubkey: service
            for info, service in zip(node_infos, LND_SERVICES, strict=True)
        }
        await pay_directed_edges(node_service, record)

        assert taker._channel_ring_store is not None
        assert taker._channel_ring_backend is not None
        await reconcile_taker_ring_records(
            taker._channel_ring_store, taker._channel_ring_backend, backend
        )
        assert coordinator._record().state is RingLifecycleState.CONFIRMED_OPEN

        await taker.stop(close_wallet=False)
        taker = None
        restart_taker = await make_taker(wallet, backend, config)
        stored = restart_taker._channel_ring_store
        assert stored is not None
        successful = [
            item for item in stored.load_all().records if item.manifest is not None
        ]
        assert len(successful) == 1
        assert successful[0].state is RingLifecycleState.CONFIRMED_OPEN
        assert successful[0].final_tx == raw_tx
    finally:
        if restart_taker is not None:
            await restart_taker.stop(close_wallet=False)
        if taker is not None:
            await taker.stop(close_wallet=False)
        await asyncio.gather(
            *(item.close() for item in lnd_backends), return_exceptions=True
        )
        await wallet.close()
        await recreate_maker3(10_000)
