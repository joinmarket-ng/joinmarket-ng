"""Live three-maker channel-input v1 CoinJoin against patched LND nodes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest
from jmcore.bitcoin import parse_transaction
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.models import NetworkType, OfferType
from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    get_mnemonic_fingerprint,
)
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import verify_p2tr_signature

from taker.channel_input_parent import (
    ChannelInputParentCoordinator,
    ChannelInputParentState,
)
from taker.config import BroadcastPolicy, MaxCjFee, TakerConfig
from taker.taker import Taker

pytestmark = [pytest.mark.docker, pytest.mark.e2e]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    ROOT / "jmswap" / "docker-compose.ring-e2e.yml",
    ROOT / "jmswap" / "docker-compose.channel-input-e2e.yml",
    ROOT / "jmswap" / "docker-compose.channel-input-e2e.host.yml",
)
DATA_DIR = Path(
    os.getenv("CHANNEL_INPUT_E2E_DATA_DIR", ROOT / "tmp" / "channel-input-e2e")
)
PROJECT = os.getenv("CHANNEL_INPUT_E2E_PROJECT", "jm-channel-input-e2e")
RPC_URL = f"http://127.0.0.1:{os.getenv('CHANNEL_INPUT_E2E_BITCOIN_PORT', '30443')}"
DIRECTORY = f"127.0.0.1:{os.getenv('CHANNEL_INPUT_E2E_DIRECTORY_PORT', '30322')}"
TOR_SOCKS_PORT = int(os.getenv("CHANNEL_INPUT_E2E_TOR_SOCKS_PORT", "30950"))
TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)
MAKER_SERVICES = ("ring-maker1", "ring-maker2", "ring-maker3")
LND_MAKERS = ("lnd-maker1", "lnd-maker2", "lnd-maker3")
LND_SERVICES = (*LND_MAKERS, "lnd-taker")
LND_PORTS = {
    "lnd-maker1": os.getenv("CHANNEL_INPUT_E2E_MAKER1_GRPC_PORT", "31009"),
    "lnd-maker2": os.getenv("CHANNEL_INPUT_E2E_MAKER2_GRPC_PORT", "32009"),
    "lnd-maker3": os.getenv("CHANNEL_INPUT_E2E_MAKER3_GRPC_PORT", "31309"),
    "lnd-taker": os.getenv("CHANNEL_INPUT_E2E_TAKER_GRPC_PORT", "30009"),
}
CHANNEL_CAPACITY = 1_500_000
CHANNEL_PUSH = 600_000
COINJOIN_AMOUNT = 1_000_000
MAKER_TXFEE = 500


async def bitcoin_rpc(
    method: str, params: list[Any] | None = None, *, wallet: str | None = None
) -> Any:
    url = RPC_URL if wallet is None else f"{RPC_URL}/wallet/{wallet}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            auth=("test", "test"),
            json={
                "jsonrpc": "2.0",
                "id": "channel-input-e2e",
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
    command = ["docker", "compose", "-p", PROJECT]
    for compose_file in COMPOSE_FILES:
        command.extend(("-f", str(compose_file)))
    command.extend(("--profile", "ring-e2e", *args))
    try:
        return subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"docker compose {' '.join(args)} failed: {exc.stderr.strip()}"
        ) from exc


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
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected lncli response from {service}: {payload!r}")
    return payload


def lnd_path(service: str, relative: str) -> Path:
    return DATA_DIR / service / relative


async def wait_for_file(path: Path, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size:
            return
        await asyncio.sleep(0.25)
    raise RuntimeError(f"LND credential was not created: {path}")


async def wait_for_wallet_balance(
    service: str, minimum_sat: int, timeout: float = 180
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        balance = await asyncio.to_thread(lncli, service, "walletbalance")
        confirmed = int(balance.get("confirmed_balance", 0))
        if confirmed >= minimum_sat:
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"{service} did not receive {minimum_sat} confirmed sats")


async def wait_for_spendable_lnd_funds(
    service: str, minimum_sat: int, minimum_confirmations: int = 6, timeout: float = 180
) -> None:
    deadline = time.monotonic() + timeout
    last_utxos: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        response = await asyncio.to_thread(
            lncli, service, "listunspent", f"--min_confs={minimum_confirmations}"
        )
        last_utxos = list(response.get("utxos", []))
        available = sum(int(utxo["amount_sat"]) for utxo in last_utxos)
        if available >= minimum_sat:
            return
        await asyncio.sleep(1)
    raise RuntimeError(
        f"{service} has no {minimum_confirmations}-confirmation, spendable "
        f"{minimum_sat}-sat LND UTXO: {last_utxos}"
    )


async def wait_for_lnd_sync(service: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = await asyncio.to_thread(lncli, service, "getinfo")
        if info.get("synced_to_chain") and info.get("wallet_synced"):
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"{service} did not synchronize after mining")


async def wait_for_seed_channels(timeout: float = 120) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        taker_channels = (await asyncio.to_thread(lncli, "lnd-taker", "listchannels"))[
            "channels"
        ]
        maker_channels = await asyncio.gather(
            *(
                asyncio.to_thread(lncli, service, "listchannels")
                for service in LND_MAKERS
            )
        )
        if (
            len(taker_channels) == 3
            and all(item["active"] and item["private"] for item in taker_channels)
            and all(len(response["channels"]) == 1 for response in maker_channels)
            and all(response["channels"][0]["active"] for response in maker_channels)
        ):
            points: dict[str, str] = {}
            for service, response in zip(LND_MAKERS, maker_channels, strict=True):
                channel = response["channels"][0]
                assert channel["private"]
                assert channel["commitment_type"] in {
                    7,
                    "7",
                    "TAPROOT",
                    "SIMPLE_TAPROOT",
                }
                points[service] = str(channel["channel_point"])
            assert set(points.values()) == {
                str(channel["channel_point"]) for channel in taker_channels
            }
            return points
        await asyncio.sleep(1)
    raise RuntimeError("private final-Taproot seed channels did not become active")


async def wait_for_configured_makers(timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    logs: list[subprocess.CompletedProcess[str]] = []
    while time.monotonic() < deadline:
        logs = await asyncio.gather(
            *(
                asyncio.to_thread(compose, "logs", "--no-color", service)
                for service in MAKER_SERVICES
            )
        )
        if all(
            "Validated channel-input v1 LND backend" in result.stdout
            and "Maker bot started. Listening for takers" in result.stdout
            for result in logs
        ):
            return
        await asyncio.sleep(1)
    raise RuntimeError(
        "channel-input maker did not become ready:\n"
        + "\n".join(result.stdout + result.stderr for result in logs)
    )


async def assert_makers_running() -> None:
    running = await asyncio.to_thread(
        compose, "ps", "--services", "--status", "running"
    )
    assert set(MAKER_SERVICES).issubset(set(running.stdout.split()))


async def core_wallet() -> str:
    wallet = "channel-input-e2e-funder"
    try:
        await bitcoin_rpc("createwallet", [wallet, False, False, "", False, True])
    except RuntimeError as exc:
        if "already exists" not in str(exc) and "already loaded" not in str(exc):
            raise
        try:
            await bitcoin_rpc("loadwallet", [wallet])
        except RuntimeError as load_exc:
            if "already loaded" not in str(load_exc):
                raise
    return wallet


async def mine(wallet: str, blocks: int) -> None:
    address = await bitcoin_rpc("getnewaddress", ["", "bech32m"], wallet=wallet)
    await bitcoin_rpc("generatetoaddress", [blocks, address])


async def fund_lnd_wallets(wallet: str) -> None:
    taker_addresses, maker_addresses = await asyncio.gather(
        asyncio.gather(
            *(
                asyncio.to_thread(lncli, "lnd-taker", "newaddress", "p2tr")
                for _ in range(3)
            )
        ),
        asyncio.gather(
            *(
                asyncio.to_thread(lncli, service, "newaddress", "p2tr")
                for service in LND_MAKERS
            )
        ),
    )
    for address in taker_addresses:
        await bitcoin_rpc("sendtoaddress", [address["address"], 0.025], wallet=wallet)
    for address in maker_addresses:
        amount = 0.01
        await bitcoin_rpc("sendtoaddress", [address["address"], amount], wallet=wallet)
    await mine(wallet, 6)
    await asyncio.gather(
        *(wait_for_lnd_sync(service) for service in LND_SERVICES),
        wait_for_wallet_balance("lnd-taker", 7_000_000),
        wait_for_spendable_lnd_funds("lnd-taker", 7_000_000),
        *(wait_for_wallet_balance(service, 900_000) for service in LND_MAKERS),
        *(wait_for_spendable_lnd_funds(service, 900_000) for service in LND_MAKERS),
    )


def onion_endpoint(service: str) -> str:
    info = lncli(service, "getinfo")
    endpoints = [str(uri).partition("@")[2] for uri in info["uris"] if "@" in str(uri)]
    if len(endpoints) != 1:
        raise RuntimeError(
            f"{service} did not advertise exactly one onion endpoint: {endpoints}"
        )
    return endpoints[0]


async def seed_private_channels(wallet: str) -> dict[str, str]:
    for service in LND_MAKERS:
        node_id = (await asyncio.to_thread(lncli, service, "getinfo"))[
            "identity_pubkey"
        ]
        await connect_over_tor(node_id, onion_endpoint(service))
        await wait_for_peer(node_id)
        await asyncio.to_thread(
            lncli,
            "lnd-taker",
            "openchannel",
            f"--node_key={node_id}",
            f"--local_amt={CHANNEL_CAPACITY}",
            f"--push_amt={CHANNEL_PUSH}",
            "--private",
            "--channel_type=taproot",
            "--sat_per_vbyte=2",
        )
    await mine(wallet, 6)
    return await wait_for_seed_channels()


async def connect_over_tor(node_id: str, endpoint: str, attempts: int = 4) -> None:
    last_error: RuntimeError | None = None
    for attempt in range(attempts):
        try:
            await asyncio.to_thread(
                lncli, "lnd-taker", "connect", f"{node_id}@{endpoint}"
            )
            return
        except RuntimeError as exc:
            last_error = exc
            if "already connected" in str(exc).lower():
                return
            if attempt + 1 < attempts:
                await asyncio.sleep(5)
    assert last_error is not None
    raise last_error


async def wait_for_peer(node_id: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        peers = (await asyncio.to_thread(lncli, "lnd-taker", "listpeers"))["peers"]
        if any(peer.get("pub_key") == node_id for peer in peers):
            return
        await asyncio.sleep(1)
    raise RuntimeError(f"lnd-taker did not connect to {node_id}")


async def enable_channel_input_makers(points: dict[str, str]) -> None:
    environment: dict[str, str] = {}
    for index, service in enumerate(LND_MAKERS, start=1):
        environment[f"CHANNEL_INPUT_E2E_MAKER{index}_ENABLED"] = "true"
        environment[f"CHANNEL_INPUT_E2E_MAKER{index}_CHANNEL_POINTS"] = json.dumps(
            [points[service]]
        )
    await asyncio.to_thread(
        compose,
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        *MAKER_SERVICES,
        environment=environment,
    )
    await wait_for_configured_makers()


def taker_config(data_dir: Path, channel_points: dict[str, str]) -> TakerConfig:
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
        channel_input=ChannelInputConfig(
            enabled=True,
            lnd_grpc_url=f"https://127.0.0.1:{LND_PORTS['lnd-taker']}",
            lnd_tls_cert_path=lnd_path("lnd-taker", "tls.cert"),
            lnd_macaroon_path=lnd_path("lnd-taker", "admin.macaroon"),
            lnd_escrow_macaroon_path=lnd_path(
                "lnd-taker", "channelescrow.macaroon"
            ),
            allowed_channel_points=tuple(sorted(channel_points.values())),
            minimum_makers=3,
            min_channel_capacity_sat=1_000_000,
            max_channel_capacity_sat=2_000_000,
            max_claim_adjustment_sat=10_000,
            max_miner_fee_share_sat=10_000,
            max_parent_fee_sat=100_000,
            max_fee_rate_sat_vb=100,
            recovery_delay_blocks=6,
            phase_timeout_seconds=120.0,
            max_active_sessions=4,
            persistence_directory=data_dir / "channel-input",
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


@pytest.mark.asyncio
@pytest.mark.timeout(1200)
async def test_three_maker_channel_input_coinjoin_finalizes_and_recovers(
    tmp_path: Path,
) -> None:
    if os.getenv("CHANNEL_INPUT_E2E") != "1":
        pytest.skip("run scripts/run-channel-input-e2e.sh to start the isolated stack")

    credentials = [
        lnd_path(service, relative)
        for service in LND_SERVICES
        for relative in (
            "tls.cert",
            "admin.macaroon",
            "channelescrow.macaroon",
        )
    ]
    await asyncio.gather(*(wait_for_file(path) for path in credentials))

    funder = await core_wallet()
    await mine(funder, 110)
    await fund_lnd_wallets(funder)
    channel_points = await seed_private_channels(funder)
    await enable_channel_input_makers(channel_points)

    backend = DescriptorWalletBackend(
        rpc_url=RPC_URL,
        rpc_user="test",
        rpc_password="test",
        wallet_name=f"jm_{get_mnemonic_fingerprint(TAKER_MNEMONIC)}_channel_input_e2e_regtest",
    )
    wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=backend,
        network="regtest",
        mixdepth_count=5,
        address_type="p2tr",
        data_dir=tmp_path,
    )
    config = taker_config(tmp_path, channel_points)
    taker: Taker | None = None
    restarted_taker: Taker | None = None
    try:
        taker = await make_taker(wallet, backend, config)
        destination = wallet.get_receive_address(1, 0)
        txid = await taker.do_coinjoin(
            amount=COINJOIN_AMOUNT,
            destination=destination,
            mixdepth=0,
            counterparty_count=3,
        )
        assert txid is not None, taker.last_failure_reason
        assert (
            set(taker._session.maker_sessions)
            and len(taker._session.maker_sessions) == 3
        )
        assert len(taker._session.ordinary_witnesses) == 4
        assert all(
            int(session.offer.cjfee) > 0 and session.offer.txfee == MAKER_TXFEE
            for session in taker._session.maker_sessions.values()
        )
        await assert_makers_running()

        coordinator = taker._session.channel_input_coordinator
        assert coordinator is not None
        parent = coordinator.parent_coordinator
        durable = parent.load(coordinator.session_id)
        assert durable is not None
        assert durable.state is ChannelInputParentState.BROADCAST_ATTEMPTED
        assert durable.final_txid == txid
        assert durable.final_tx_hex is not None
        assert len(durable.plan.channels) == len(durable.channels) == 3
        assert len(durable.plan.ordinary_witnesses) == 4
        assert all(channel.witness is not None for channel in durable.channels)

        raw_transaction = str(await bitcoin_rpc("getrawtransaction", [txid]))
        transaction = parse_transaction(raw_transaction)
        input_points = {(item.txid, item.vout) for item in transaction.inputs}
        expected_channels = {
            tuple(channel_point.rsplit(":", 1))
            for channel_point in channel_points.values()
        }
        expected_channel_inputs = {
            (txid_text, int(vout)) for txid_text, vout in expected_channels
        }
        assert expected_channel_inputs.issubset(input_points)
        assert len(transaction.inputs) == 7
        assert len(input_points - expected_channel_inputs) == 4
        assert len(transaction.witnesses) == 7
        assert all(
            len(witness) == 1 and len(witness[0]) in {64, 65}
            for witness in transaction.witnesses
        )
        assert all(
            output.script.startswith(b"\x51\x20") for output in transaction.outputs
        )
        # The legacy equal-plus-change CoinJoin heuristic does not classify a
        # channel-input parent: its six settlement outputs break the ordinary
        # n-equal/n-change shape. Assert the equal-output structure directly.
        value_counts = Counter(output.value for output in transaction.outputs)
        assert value_counts[COINJOIN_AMOUNT] == 4
        assert max(value_counts.values()) == 4
        cj_scripts = {
            output.script
            for output in transaction.outputs
            if output.value == COINJOIN_AMOUNT
        }
        assert len(cj_scripts) == 4
        assert len(transaction.outputs) == 14

        channel_indexes = {channel.channel_input_index for channel in durable.channels}
        assert {
            index
            for index, point in enumerate(transaction.inputs)
            if (point.txid, point.vout) in expected_channel_inputs
        } == channel_indexes
        prevout_values = [item.value_sat for item in durable.plan.prevouts]
        prevout_scripts = [
            bytes.fromhex(item.script_pubkey) for item in durable.plan.prevouts
        ]
        for channel in durable.channels:
            witness = transaction.witnesses[channel.channel_input_index][0]
            assert verify_p2tr_signature(
                transaction,
                channel.channel_input_index,
                prevout_values,
                prevout_scripts,
                witness,
                bytes.fromhex(channel.first_snapshot.funding_script_pubkey)[2:],
            )

        metadata = taker._session.tx_metadata
        input_owners = metadata["input_owners"]
        output_owners = metadata["output_owners"]
        input_values = metadata["input_values"]
        assert len(input_owners) == len(input_values) == len(transaction.inputs)
        assert len(output_owners) == len(transaction.outputs)
        paid_fees = taker._session.maker_fee_plan()
        for nick, plan_channel, persisted_channel in zip(
            taker._session.maker_sessions,
            durable.plan.channels,
            durable.channels,
            strict=True,
        ):
            ordinary_input = sum(
                value
                for owner, value in zip(input_owners, input_values, strict=True)
                if owner == nick
            )
            equal_output = sum(
                output.value
                for owner, output in zip(
                    output_owners, transaction.outputs, strict=True
                )
                if owner == (nick, "cj")
            )
            assert equal_output == COINJOIN_AMOUNT
            ordinary_output = sum(
                output.value
                for owner, output in zip(
                    output_owners, transaction.outputs, strict=True
                )
                if owner == (nick, "change")
            )
            assert ordinary_output == ordinary_input - COINJOIN_AMOUNT - MAKER_TXFEE
            maker_settlement = sum(
                output.value_sat for output in plan_channel.first.settlement_outputs
            )
            assert (
                maker_settlement
                == persisted_channel.first_snapshot.local_claim_sat + paid_fees[nick]
            )
            assert plan_channel.first.claim_adjustment_sat == paid_fees[nick]
            assert plan_channel.second.claim_adjustment_sat == -paid_fees[nick]
            assert ordinary_output + maker_settlement == (
                ordinary_input
                - COINJOIN_AMOUNT
                - MAKER_TXFEE
                + persisted_channel.first_snapshot.local_claim_sat
                + paid_fees[nick]
            )

        await mine(funder, 3)
        confirmed = await parent.observe(coordinator.session_id, backend)
        assert confirmed.state is ChannelInputParentState.CONFIRMED
        assert (
            confirmed.observation is not None
            and confirmed.observation.confirmations >= 3
        )
        chain_transaction = await bitcoin_rpc("getrawtransaction", [txid, True])
        assert int(chain_transaction["confirmations"]) >= 3
        assert (
            sum(
                (str(item["txid"]), int(item["vout"])) in expected_channel_inputs
                for item in chain_transaction["vin"]
            )
            == 3
        )
        assert len(chain_transaction["vin"]) == 7

        reloaded_parent = ChannelInputParentCoordinator(
            tmp_path / "channel-input" / "parents"
        )
        reloaded = reloaded_parent.load(coordinator.session_id)
        assert (
            reloaded is not None and reloaded.state is ChannelInputParentState.CONFIRMED
        )
        assert reloaded.final_tx_hex == durable.final_tx_hex

        await taker.stop(close_wallet=False)
        taker = None
        restarted_taker = await make_taker(wallet, backend, config)
        recovered = ChannelInputParentCoordinator(
            tmp_path / "channel-input" / "parents"
        ).load(coordinator.session_id)
        assert (
            recovered is not None
            and recovered.state is ChannelInputParentState.CONFIRMED
        )
    finally:
        for active_taker in (restarted_taker, taker):
            if active_taker is not None:
                await active_taker.stop(close_wallet=False)
        await wallet.close()
