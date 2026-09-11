"""Live four-party co-funded channel-ring test against stock LND nodes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from jmcore.bitcoin import parse_transaction
from jmcore.channel_maintenance import ChannelMaintenanceConfig
from jmcore.channel_ring import ChannelRingConfig
from jmcore.channel_ring_store import RingLifecycleState
from jmcore.cofunded_ring import (
    CoordinatorParticipant,
    PrivateParticipant,
    RingPrivatePlanPayload,
)
from jmcore.models import NetworkType, OfferType
from jmswap.lnd import LndBackend
from jmswap.maintenance import MaintenanceService, maintenance_status
from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    get_mnemonic_fingerprint,
)
from jmwallet.wallet.service import WalletService
from pydantic import SecretStr

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
DIRECTED_PAYMENT_AMOUNTS = (1_111, 2_222, 3_333, 4_444)
LND_PORTS = {
    "lnd-maker1": os.getenv("RING_E2E_MAKER1_GRPC_PORT", "21009"),
    "lnd-maker2": os.getenv("RING_E2E_MAKER2_GRPC_PORT", "22009"),
    "lnd-maker3": os.getenv("RING_E2E_MAKER3_GRPC_PORT", "23009"),
    "lnd-taker": os.getenv("RING_E2E_TAKER_GRPC_PORT", "24009"),
}


async def bitcoin_rpc(
    method: str, params: list[Any] | None = None, *, wallet: str | None = None
) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            RPC_URL if wallet is None else f"{RPC_URL}/wallet/{quote(wallet, safe='')}",
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
        channels = await list_channels()
        if all(
            len(items) == 2 and all(item["active"] for item in items)
            for items in channels.values()
        ):
            return channels
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"ring channels did not become active: {channels}")
        await asyncio.sleep(2)


async def list_channels() -> dict[str, list[dict[str, Any]]]:
    responses = await asyncio.gather(
        *(asyncio.to_thread(lncli, service, "listchannels") for service in LND_SERVICES)
    )
    return {
        service: list(response["channels"])
        for service, response in zip(LND_SERVICES, responses, strict=True)
    }


async def assert_no_pending_channels() -> None:
    for service in LND_SERVICES:
        pending = await asyncio.to_thread(lncli, service, "pendingchannels")
        assert not pending["pending_open_channels"]
        assert not pending["pending_closing_channels"]
        assert not pending["pending_force_closing_channels"]
        assert not pending["waiting_close_channels"]


def endpoint_balance_snapshot(
    channels: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, int]]:
    snapshot: dict[str, dict[str, int]] = {}
    for service, items in channels.items():
        balances: dict[str, int] = {}
        for channel in items:
            channel_point = str(channel["channel_point"])
            assert channel_point not in balances
            balances[channel_point] = int(channel["local_balance"])
        snapshot[service] = balances
    return snapshot


async def wait_for_balance_deltas(
    before: dict[str, dict[str, int]],
    expected_deltas: dict[str, dict[str, int]],
    timeout: float = 30,
) -> dict[str, list[dict[str, Any]]]:
    deadline = asyncio.get_running_loop().time() + timeout
    observed: dict[str, dict[str, int]] | None = None
    while True:
        channels = await list_channels()
        after = endpoint_balance_snapshot(channels)
        if set(after) == set(before) and all(
            set(after[service]) == set(before[service]) for service in before
        ):
            observed = {
                service: {
                    channel_point: after[service][channel_point]
                    - before[service][channel_point]
                    for channel_point in before[service]
                }
                for service in before
            }
            if observed == expected_deltas:
                return channels
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                "directed ring payments did not produce the expected endpoint balance "
                f"deltas: expected={expected_deltas}, observed={observed}"
            )
        await asyncio.sleep(0.5)


def bitcoin_value_sats(value: Any) -> int:
    return int(Decimal(str(value)) * 100_000_000)


def close_channel(service: str, channel_point: str) -> str:
    result = compose(
        "exec",
        "-T",
        service,
        "lncli",
        "--network=regtest",
        "--tlscertpath=/root/.lnd/tls.cert",
        "--macaroonpath=/root/.lnd/admin.macaroon",
        "closechannel",
        f"--chan_point={channel_point}",
    )
    decoder = json.JSONDecoder()
    remaining = result.stdout.lstrip()
    updates: list[dict[str, Any]] = []
    while remaining:
        update, offset = decoder.raw_decode(remaining)
        if not isinstance(update, dict):
            raise RuntimeError(f"unexpected closechannel response: {update!r}")
        updates.append(update)
        remaining = remaining[offset:].lstrip()
    for update in updates:
        pending = update.get("close_pending")
        candidates = (
            update.get("closing_txid"),
            update.get("txid"),
            pending.get("txid") if isinstance(pending, dict) else None,
            pending.get("closing_txid") if isinstance(pending, dict) else None,
        )
        if txid := next((str(item) for item in candidates if item), None):
            return txid
    raise RuntimeError(f"closechannel did not report a closing transaction: {updates}")


async def wait_for_mempool_transaction(txid: str, timeout: float = 30) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if txid in await bitcoin_rpc("getrawmempool"):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"cooperative close was not broadcast: {txid}")
        await asyncio.sleep(0.5)


async def wait_for_closed_channel(channel_point: str, timeout: float = 60) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        channels = await list_channels()
        matching_channels = [
            channel
            for items in channels.values()
            for channel in items
            if str(channel["channel_point"]) == channel_point
        ]
        if not matching_channels or all(
            not channel["active"] for channel in matching_channels
        ):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"confirmed channel close remained active: {channel_point}"
            )
        await asyncio.sleep(0.5)


async def wait_for_no_ring_channels(timeout: float = 60) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        channels = await list_channels()
        pending = await asyncio.gather(
            *(
                asyncio.to_thread(lncli, service, "pendingchannels")
                for service in LND_SERVICES
            )
        )
        has_pending = any(
            state[key]
            for state in pending
            for key in (
                "pending_open_channels",
                "pending_closing_channels",
                "pending_force_closing_channels",
                "waiting_close_channels",
            )
        )
        has_active = any(
            channel["active"] for items in channels.values() for channel in items
        )
        if not has_active and not has_pending:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"ring channels did not fully close: channels={channels}, pending={pending}"
            )
        await asyncio.sleep(0.5)


async def assert_confirmed_cooperative_close(
    closing_txid: str, channel_point: str
) -> None:
    funding_txid, separator, output_index_text = channel_point.partition(":")
    assert separator
    funding_output_index = int(output_index_text)
    funding_transaction = await bitcoin_rpc("getrawtransaction", [funding_txid, True])
    funding_output = funding_transaction["vout"][funding_output_index]
    assert str(funding_output["scriptPubKey"]["hex"]).startswith("5120")
    funding_value = bitcoin_value_sats(funding_output["value"])

    closing_transaction = await bitcoin_rpc("getrawtransaction", [closing_txid, True])
    assert int(closing_transaction["confirmations"]) == 1
    assert len(closing_transaction["vin"]) == 1
    closing_input = closing_transaction["vin"][0]
    assert (str(closing_input["txid"]), int(closing_input["vout"])) == (
        funding_txid,
        funding_output_index,
    )
    witness = [str(item) for item in closing_input["txinwitness"]]
    assert len(witness) == 1
    assert len(bytes.fromhex(witness[0])) in {64, 65}

    closing_output_values = [
        bitcoin_value_sats(output["value"]) for output in closing_transaction["vout"]
    ]
    assert closing_output_values and all(value > 0 for value in closing_output_values)
    closing_fee = funding_value - sum(closing_output_values)
    assert closing_fee > 0
    assert funding_value == sum(closing_output_values) + closing_fee


async def pay_directed_edges(
    node_service: dict[str, str],
    record: Any,
    channels: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, int]]:
    participant_node = {
        participant.participant_key: participant.node_id
        for participant in record.coordinator_participants
    }
    assert record.manifest is not None
    assert len(record.manifest.edges) == len(DIRECTED_PAYMENT_AMOUNTS)
    expected_deltas = {
        service: {channel_point: 0 for channel_point in balances}
        for service, balances in endpoint_balance_snapshot(channels).items()
    }
    for index, edge in enumerate(record.manifest.edges):
        opener = node_service[participant_node[edge.opener_key]]
        fundee = node_service[participant_node[edge.acceptor_key]]
        channel_point = f"{record.manifest.unsigned_txid}:{edge.output_index}"
        opener_channels = [
            channel
            for channel in channels[opener]
            if str(channel["channel_point"]) == channel_point
        ]
        assert len(opener_channels) == 1
        opener_channel = opener_channels[0]
        assert (
            str(opener_channel["remote_pubkey"]) == participant_node[edge.acceptor_key]
        )
        amount = DIRECTED_PAYMENT_AMOUNTS[index]
        invoice = await asyncio.to_thread(
            lncli, fundee, "addinvoice", f"--amt={amount}", f"--memo=ring-edge-{index}"
        )
        payment = await asyncio.to_thread(
            lncli,
            opener,
            "payinvoice",
            "--json",
            "--force",
            "--fee_limit=100",
            f"--outgoing_chan_id={opener_channel['scid']}",
            str(invoice["payment_request"]),
        )
        assert not payment.get("payment_error")
        assert payment.get("payment_preimage")
        successful_attempts = [
            attempt for attempt in payment["htlcs"] if attempt["status"] == "SUCCEEDED"
        ]
        assert len(successful_attempts) == 1
        assert [
            str(hop["chan_id"]) for hop in successful_attempts[0]["route"]["hops"]
        ] == [str(opener_channel["scid"])]
        expected_deltas[opener][channel_point] -= amount
        expected_deltas[fundee][channel_point] += amount
    return expected_deltas


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
        taker = await make_taker(wallet, backend, config)
        destination = wallet.get_receive_address(1, 0)
        rejected_txid = await taker.do_coinjoin(
            amount=1_000_000, destination=destination, mixdepth=0, counterparty_count=3
        )
        assert rejected_txid is None
        assert taker.last_failure_reason == "Strict channel-ring negotiation aborted"
        assert taker._channel_ring_store is not None
        rejected_records = taker._channel_ring_store.load_all().records
        assert len(rejected_records) == 1
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
            input_value += bitcoin_value_sats(previous["vout"][tx_input.vout]["value"])
        actual_fee = input_value - sum(output.value for output in parsed.outputs)
        maker_txfees = sum(
            session.offer.txfee for session in taker._session.maker_sessions.values()
        )
        assert record.coordinator_tx_fee is not None
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
        before_payment_balances = endpoint_balance_snapshot(channels)
        expected_payment_deltas = await pay_directed_edges(
            node_service, record, channels
        )
        await wait_for_balance_deltas(before_payment_balances, expected_payment_deltas)

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

        participant_node = {}
        for participant in record.coordinator_participants:
            assert isinstance(participant, PrivateParticipant)
            participant_node[participant.participant_key] = participant.node_id
        channel_points = [f"{txid}:{edge.output_index}" for edge in manifest.edges]
        assert len(channel_points) == len(set(channel_points)) == 4
        closing_txids: set[str] = set()
        for edge, channel_point in zip(manifest.edges, channel_points, strict=True):
            closer = node_service[participant_node[edge.opener_key]]
            block_height = int(await bitcoin_rpc("getblockcount"))
            closing_txid = await asyncio.to_thread(close_channel, closer, channel_point)
            assert closing_txid not in closing_txids
            closing_txids.add(closing_txid)
            await wait_for_mempool_transaction(closing_txid)
            mined_blocks = await bitcoin_rpc("generatetoaddress", [1, destination])
            assert len(mined_blocks) == 1
            assert int(await bitcoin_rpc("getblockcount")) == block_height + 1
            await assert_confirmed_cooperative_close(closing_txid, channel_point)
            await wait_for_closed_channel(channel_point)
        assert len(closing_txids) == 4
        # LND retains cooperative closes until its additional close-confirmation depth.
        await bitcoin_rpc("generatetoaddress", [6, destination])
        await wait_for_no_ring_channels()
        await assert_no_pending_channels()
    finally:
        cleanup_failures: list[BaseException] = []
        for active_taker in (restart_taker, taker):
            if active_taker is None:
                continue
            try:
                await active_taker.stop(close_wallet=False)
            except BaseException as exc:
                cleanup_failures.append(exc)
        cleanup_results = await asyncio.gather(
            *(item.close() for item in lnd_backends), return_exceptions=True
        )
        cleanup_failures.extend(
            result for result in cleanup_results if isinstance(result, BaseException)
        )
        try:
            await wallet.close()
        except BaseException as exc:
            cleanup_failures.append(exc)
        try:
            await recreate_maker3(10_000)
        except BaseException as exc:
            cleanup_failures.append(exc)
        if sys.exc_info()[0] is None and cleanup_failures:
            raise cleanup_failures[0]


def private_taker_config(data_dir: Path) -> TakerConfig:
    config = taker_config(data_dir)
    return config.model_copy(
        update={
            "channel_ring": config.channel_ring.model_copy(
                update={
                    "private_coordination": True,
                    "maintenance_directory": data_dir / "maintenance",
                }
            )
        }
    )


def private_ring_endpoints(
    channels: dict[str, list[dict[str, Any]]], service_node_id: dict[str, str]
) -> dict[str, tuple[str, str]]:
    by_point: dict[str, list[tuple[str, str, bool]]] = {}
    for service, items in channels.items():
        local_node_id = service_node_id[service]
        for channel in items:
            by_point.setdefault(str(channel["channel_point"]), []).append(
                (
                    local_node_id,
                    str(channel["remote_pubkey"]),
                    bool(channel["initiator"]),
                )
            )

    endpoints: dict[str, tuple[str, str]] = {}
    for channel_point, sides in by_point.items():
        assert len(sides) == 2
        assert {side[0] for side in sides} == {side[1] for side in sides}
        initiators = [side[0] for side in sides if side[2]]
        assert len(initiators) == 1
        opener = initiators[0]
        fundee = next(side[0] for side in sides if side[0] != opener)
        endpoints[channel_point] = (opener, fundee)
    return endpoints


async def pay_directed_private_edges(
    node_service: dict[str, str],
    manifest: Any,
    channels: dict[str, list[dict[str, Any]]],
    endpoints: dict[str, tuple[str, str]],
) -> dict[str, dict[str, int]]:
    expected_deltas = {
        service: {channel_point: 0 for channel_point in balances}
        for service, balances in endpoint_balance_snapshot(channels).items()
    }
    for index, edge in enumerate(manifest.edges):
        channel_point = f"{manifest.unsigned_txid}:{edge.output_index}"
        opener_node_id, fundee_node_id = endpoints[channel_point]
        opener = node_service[opener_node_id]
        fundee = node_service[fundee_node_id]
        opener_channels = [
            channel
            for channel in channels[opener]
            if str(channel["channel_point"]) == channel_point
        ]
        assert len(opener_channels) == 1
        opener_channel = opener_channels[0]
        assert str(opener_channel["remote_pubkey"]) == fundee_node_id
        amount = DIRECTED_PAYMENT_AMOUNTS[index]
        invoice = await asyncio.to_thread(
            lncli, fundee, "addinvoice", f"--amt={amount}", f"--memo=ring-edge-{index}"
        )
        payment = await asyncio.to_thread(
            lncli,
            opener,
            "payinvoice",
            "--json",
            "--force",
            "--fee_limit=100",
            f"--outgoing_chan_id={opener_channel['scid']}",
            str(invoice["payment_request"]),
        )
        assert not payment.get("payment_error")
        assert payment.get("payment_preimage")
        successful_attempts = [
            attempt for attempt in payment["htlcs"] if attempt["status"] == "SUCCEEDED"
        ]
        assert len(successful_attempts) == 1
        assert [
            str(hop["chan_id"]) for hop in successful_attempts[0]["route"]["hops"]
        ] == [str(opener_channel["scid"])]
        expected_deltas[opener][channel_point] -= amount
        expected_deltas[fundee][channel_point] += amount
    return expected_deltas


def assert_private_coordinator_record(
    record: Any, nonneighbor_node_ids: set[str]
) -> None:
    assert isinstance(record.plan, RingPrivatePlanPayload)
    assert all(
        isinstance(item, CoordinatorParticipant)
        for item in record.coordinator_participants
    )
    assert all(
        "node_id" not in item.model_dump(mode="json")
        and "onion_endpoint" not in item.model_dump(mode="json")
        for item in record.coordinator_participants
    )
    assert all(
        isinstance(item, RingPrivatePlanPayload)
        for item in record.coordinator_plans.values()
    )
    assert set(record.local_contacts) == {
        record.plan.predecessor.participant_key,
        record.plan.successor.participant_key,
    }
    serialized_plans = [
        json.dumps(plan.model_dump(mode="json"), sort_keys=True)
        for plan in record.coordinator_plans.values()
    ]
    assert all(
        "node_id" not in plan and "onion_endpoint" not in plan
        for plan in serialized_plans
    )
    serialized_record = json.dumps(record.model_dump(mode="json"), sort_keys=True)
    assert all(node_id not in serialized_record for node_id in nonneighbor_node_ids)
    assert all(
        all(node_id not in plan for plan in serialized_plans)
        for node_id in nonneighbor_node_ids
    )


def assert_maintenance_enrollments(
    data_dir: Path,
    record: Any,
    taker_node_id: str,
    endpoints: dict[str, tuple[str, str]],
) -> None:
    assert record.maintenance_enrollment_recorded
    assert record.manifest is not None
    assert record.incoming_edge is not None
    assert record.outgoing_edge is not None
    state = json.loads((data_dir / "maintenance" / "maintenance-v1.json").read_text())
    enrollment_points = {
        str(record.incoming_edge.funding_outpoint),
        str(record.outgoing_edge.funding_outpoint),
    }
    assert set(state["enrollments"]) == enrollment_points
    for channel_point, enrollment in state["enrollments"].items():
        opener, fundee = endpoints[channel_point]
        assert enrollment == {
            "node_id": taker_node_id,
            "peer_node_id": fundee if opener == taker_node_id else opener,
            "channel_point": channel_point,
            "origin_id": record.manifest.unsigned_txid,
            "status": "pending_validation",
        }


@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_private_coordination_complete_four_party_ring(tmp_path: Path) -> None:
    if os.getenv("RING_E2E") != "1":
        pytest.skip("start the ring-e2e Compose profile and set RING_E2E=1")
    if os.getenv("RING_E2E_PRIVATE_COORDINATION") != "true":
        pytest.skip(
            "start the ring-e2e Compose profile with private coordination enabled"
        )

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
    config = private_taker_config(tmp_path)
    taker: Taker | None = None
    restart_taker: Taker | None = None
    lnd_backends: list[LndBackend] = []
    try:
        taker = await make_taker(wallet, backend, config)
        destination = wallet.get_receive_address(1, 0)
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
        assert {
            (output.index, output.amount, output.script_pubkey)
            for output in manifest.outputs
        } == {
            (index, output.value, output.script.hex())
            for index, output in enumerate(parsed.outputs)
        }

        input_value = 0
        for tx_input in parsed.inputs:
            previous = await bitcoin_rpc("getrawtransaction", [tx_input.txid, True])
            input_value += bitcoin_value_sats(previous["vout"][tx_input.vout]["value"])
        actual_fee = input_value - sum(output.value for output in parsed.outputs)
        maker_txfees = sum(
            session.offer.txfee for session in taker._session.maker_sessions.values()
        )
        assert record.coordinator_tx_fee is not None
        assert actual_fee == record.coordinator_tx_fee + maker_txfees

        await bitcoin_rpc("generatetoaddress", [3, destination])
        channels = await wait_for_channels()
        expected_points = {f"{txid}:{edge.output_index}" for edge in manifest.edges}
        assert {
            str(channel["channel_point"])
            for items in channels.values()
            for channel in items
        } == expected_points
        assert sum(len(items) for items in channels.values()) == 8
        for items in channels.values():
            assert len({str(item["remote_pubkey"]) for item in items}) == 2
            assert all(item["private"] for item in items)
            assert all(item["alias_scids"] for item in items)
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
        service_node_id = {
            service: info.identity_pubkey
            for service, info in zip(LND_SERVICES, node_infos, strict=True)
        }
        node_service = {
            node_id: service for service, node_id in service_node_id.items()
        }
        endpoints = private_ring_endpoints(channels, service_node_id)
        assert set(endpoints) == expected_points
        taker_node_id = service_node_id["lnd-taker"]
        nonneighbor_node_ids = (
            set(node_service)
            - {taker_node_id}
            - {contact.node_id for contact in record.local_contacts.values()}
        )
        assert len(nonneighbor_node_ids) == 1
        assert_private_coordinator_record(record, nonneighbor_node_ids)

        before_payment_balances = endpoint_balance_snapshot(channels)
        expected_payment_deltas = await pay_directed_private_edges(
            node_service, manifest, channels, endpoints
        )
        await wait_for_balance_deltas(before_payment_balances, expected_payment_deltas)

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
        restarted_record = successful[0]
        assert restarted_record.state is RingLifecycleState.CONFIRMED_OPEN
        assert restarted_record.final_tx == raw_tx
        assert_maintenance_enrollments(
            tmp_path, restarted_record, taker_node_id, endpoints
        )
        maintenance_backend = lnd_backends[LND_SERVICES.index("lnd-taker")]
        maintenance = MaintenanceService(
            maintenance_backend,
            ChannelMaintenanceConfig(),
            tmp_path / "maintenance",
            taker_node_id,
        )
        await maintenance.tick()
        status = maintenance_status(tmp_path / "maintenance", taker_node_id)
        assert status["enrollments"] == {"enrolled": 2}
        assert status["payment_phase"] is None
        maintained = await maintenance_backend.list_maintenance_channels()
        assert len(maintained) == 2
        assert all(
            channel.local_base_fee_msat == 0 and channel.local_fee_ppm == 0
            for channel in maintained
        )
        changed_point = maintained[0].channel_point
        await asyncio.to_thread(
            lncli,
            "lnd-taker",
            "updatechanpolicy",
            f"--chan_point={changed_point}",
            "--base_fee_msat=7",
            "--fee_rate_ppm=0",
            f"--time_lock_delta={maintained[0].local_cltv_delta}",
        )
        await maintenance.tick()
        after_policy_change = await maintenance_backend.list_maintenance_channels()
        assert (
            next(
                item
                for item in after_policy_change
                if item.channel_point == changed_point
            ).local_base_fee_msat
            == 7
        )

        channel_points = [f"{txid}:{edge.output_index}" for edge in manifest.edges]
        assert len(channel_points) == len(set(channel_points)) == 4
        closing_txids: set[str] = set()
        for channel_point in channel_points:
            closer = node_service[endpoints[channel_point][0]]
            block_height = int(await bitcoin_rpc("getblockcount"))
            closing_txid = await asyncio.to_thread(close_channel, closer, channel_point)
            assert closing_txid not in closing_txids
            closing_txids.add(closing_txid)
            await wait_for_mempool_transaction(closing_txid)
            mined_blocks = await bitcoin_rpc("generatetoaddress", [1, destination])
            assert len(mined_blocks) == 1
            assert int(await bitcoin_rpc("getblockcount")) == block_height + 1
            await assert_confirmed_cooperative_close(closing_txid, channel_point)
            await wait_for_closed_channel(channel_point)
        assert len(closing_txids) == 4
        await bitcoin_rpc("generatetoaddress", [6, destination])
        await wait_for_no_ring_channels()
        await assert_no_pending_channels()
    finally:
        cleanup_failures: list[BaseException] = []
        for active_taker in (restart_taker, taker):
            if active_taker is None:
                continue
            try:
                await active_taker.stop(close_wallet=False)
            except BaseException as exc:
                cleanup_failures.append(exc)
        cleanup_results = await asyncio.gather(
            *(item.close() for item in lnd_backends), return_exceptions=True
        )
        cleanup_failures.extend(
            result for result in cleanup_results if isinstance(result, BaseException)
        )
        try:
            await wallet.close()
        except BaseException as exc:
            cleanup_failures.append(exc)
        if sys.exc_info()[0] is None and cleanup_failures:
            raise cleanup_failures[0]
