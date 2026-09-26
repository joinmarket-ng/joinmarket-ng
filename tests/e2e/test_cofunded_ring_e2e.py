"""Live mixed five-party co-funded channel-ring test against stock LND nodes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from contextlib import AsyncExitStack
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, cast

import httpx
import pytest
from jmcore.bitcoin import (
    address_to_scriptpubkey_for_network,
    get_txid,
    parse_transaction,
)
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig
from jmcore.channel_ring_store import RingLifecycleState
from jmcore.models import NetworkType, OfferType
from jmswap.buyout_config import BuyoutSettings
from jmswap.buyout_messages import Outpoint
from jmswap.buyout_runtime import ERROR_STATE_PREFIX, UNBOUND_STATE, BuyoutRuntime
from jmswap.buyout_store import ConflictError
from jmswap.lnd import LndBackend
from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    get_mnemonic_fingerprint,
)
from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.service import WalletService
from pydantic import SecretStr

from taker.buyout import ChannelBuyout
from taker.channel_ring import reconcile_taker_ring_records
from taker.config import BroadcastPolicy, MaxCjFee, TakerConfig
from taker.podle_manager import PoDLEManager
from taker.taker import Taker
from tests.e2e.ring_market_helpers import (
    FUNDING_CONFIRMATIONS,
    FUNDER_WALLET,
    PurchasedBondRental,
    PurchasedPoDLE,
    purchase_external_podle,
    purchase_rented_bond,
)

pytestmark = [pytest.mark.docker, pytest.mark.ring_e2e]

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
MAKER1_MNEMONIC = (
    "avoid whisper mesh corn already blur sudden fine planet chicken hover sniff"
)
MAKER2_MNEMONIC = (
    "minute faint grape plate stock mercy tent world space opera apple rocket"
)
MAKER3_MNEMONIC = "echo rural present blue chapter game keen keen keen keen keen keen"

RING_LND_SERVICES = ("lnd-maker1", "lnd-maker2", "lnd-maker3", "lnd-taker")
LND_SERVICES = (*RING_LND_SERVICES, "lnd-maker4")
RING_MAKER_MNEMONICS = {
    "lnd-maker1": MAKER1_MNEMONIC,
    "lnd-maker2": MAKER2_MNEMONIC,
    "lnd-maker3": MAKER3_MNEMONIC,
}
LND_PORTS = {
    "lnd-maker1": os.getenv("RING_E2E_MAKER1_GRPC_PORT", "21009"),
    "lnd-maker2": os.getenv("RING_E2E_MAKER2_GRPC_PORT", "22009"),
    "lnd-maker3": os.getenv("RING_E2E_MAKER3_GRPC_PORT", "23009"),
    "lnd-maker4": os.getenv("RING_E2E_MAKER4_GRPC_PORT", "25009"),
    "lnd-taker": os.getenv("RING_E2E_TAKER_GRPC_PORT", "24009"),
}

# Later channel-input CoinJoin over one ring-created channel.
BUYOUT_CJ_AMOUNT = 500_000
BUYOUT_SOURCE_MIXDEPTH = 0  # The ring nodes belong to their wallets' source mixdepth.
BUYOUT_POLL_INTERVAL = 2.0
BUYOUT_STATE_TIMEOUT = 300.0
BUYOUT_FAILED_STATES = frozenset(
    {"PAYMENT_FAILED", "PARENT_MISSING", "PARENT_RECOVERY_REQUIRED"}
)

RING_MAKER_BUYOUT_REPLENISHMENT_SATS = 2_000_000

# These direct test-only channels fund the two credential-market payments before
# the target CoinJoin creates its own channels. The 100,000-sat margin covers
# the opener's on-chain funding fee above the 1,000,000-sat channel capacity.
BOOTSTRAP_CHANNEL_CAPACITY_SATS = 1_000_000
BOOTSTRAP_OPENER_FUNDING_SATS = 1_100_000
BOOTSTRAP_FUNDING_CONFIRMATIONS = 6
PUBLIC_CHANNEL_GOSSIP_CONFIRMATIONS = 6
MINE_BATCH_BLOCKS = 10


class PublicGraphPropagationTimeout(RuntimeError):
    """The public graph did not learn the expected test-only links in time."""


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


async def bootstrap_bitcoin_rpc(
    method: str, params: list[Any] | None = None, *, wallet: str | None = None
) -> Any:
    """Issue bootstrap funding RPC without exposing returned sensitive values."""
    url = RPC_URL if wallet is None else f"{RPC_URL}/wallet/{wallet}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                auth=("test", "test"),
                json={
                    "jsonrpc": "2.0",
                    "id": "ring-e2e-bootstrap",
                    "method": method,
                    "params": params or [],
                },
            )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"bootstrap Bitcoin RPC {method} failed")
        return payload["result"]
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise RuntimeError(f"bootstrap Bitcoin RPC {method} failed") from None


async def bootstrap_lncli(service: str, *args: str) -> dict[str, Any]:
    """Run a bootstrap LND operation without leaking its output on failure."""
    try:
        return await asyncio.to_thread(lncli, service, *args)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RuntimeError(f"{service} bootstrap lncli {args[0]} failed") from None


async def fund_bootstrap_opener(service: str) -> None:
    """Fund one regtest LND channel opener and wait for a confirmed wallet UTXO."""
    chain = await bootstrap_bitcoin_rpc("getblockchaininfo")
    if str(chain.get("chain", "")) != "regtest":
        raise RuntimeError("refusing bootstrap LND funding outside regtest")

    initial_balance = await bootstrap_lncli(service, "walletbalance")
    try:
        initial_confirmed = int(initial_balance["confirmed_balance"])
        address = str((await bootstrap_lncli(service, "newaddress", "p2tr"))["address"])
        script = address_to_scriptpubkey_for_network(address, "regtest")
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(
            f"{service} could not prepare a bootstrap P2TR address"
        ) from None
    if not script.startswith(b"\x51\x20"):
        raise RuntimeError(f"{service} did not provide a bootstrap P2TR address")

    await bootstrap_bitcoin_rpc(
        "sendtoaddress",
        [address, float(Decimal(BOOTSTRAP_OPENER_FUNDING_SATS) / Decimal(100_000_000))],
        wallet=FUNDER_WALLET,
    )
    mining_address = await bootstrap_bitcoin_rpc(
        "getnewaddress", ["", "bech32m"], wallet=FUNDER_WALLET
    )
    await bootstrap_bitcoin_rpc(
        "generatetoaddress", [BOOTSTRAP_FUNDING_CONFIRMATIONS, mining_address]
    )
    height = int(await bootstrap_bitcoin_rpc("getblockcount"))

    deadline = asyncio.get_running_loop().time() + 120
    while True:
        info, balance = await asyncio.gather(
            bootstrap_lncli(service, "getinfo"),
            bootstrap_lncli(service, "walletbalance"),
        )
        try:
            confirmed = int(balance["confirmed_balance"])
            synced_to_height = (
                bool(info["synced_to_chain"]) and int(info["block_height"]) >= height
            )
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                f"{service} did not report bootstrap wallet or chain status"
            ) from None
        if (
            synced_to_height
            and confirmed >= initial_confirmed + BOOTSTRAP_OPENER_FUNDING_SATS
        ):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"{service} did not confirm bootstrap opener funding on the synced chain"
            )
        await asyncio.sleep(1)


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


async def wait_for_maker1(timeout: float = 90) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        logs = await asyncio.to_thread(compose, "logs", "--no-color", "ring-maker1")
        if "Maker bot started. Listening for takers" in logs.stdout:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("ring-maker1 did not become ready")
        await asyncio.sleep(1)


async def recreate_maker3(fundee_reserve: int) -> None:
    try:
        await asyncio.to_thread(
            compose,
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "ring-maker3",
            environment={"RING_E2E_MAKER3_FUNDEE_RESERVE": str(fundee_reserve)},
        )
    except subprocess.CalledProcessError as exc:
        exc.add_note(
            f"maker3 Compose stdout: {exc.stdout[-4000:] if exc.stdout else ''}"
        )
        exc.add_note(
            f"maker3 Compose stderr: {exc.stderr[-4000:] if exc.stderr else ''}"
        )
        raise
    await wait_for_maker3()


async def recreate_maker1_with_rented_bond() -> None:
    await asyncio.to_thread(
        compose,
        "up",
        "-d",
        "--force-recreate",
        "--no-deps",
        "ring-maker1",
        environment={"RING_E2E_MAKER1_NO_BOND": "false"},
    )
    await wait_for_maker1()


async def install_rented_bond_registry(registry_path: Path) -> None:
    """Copy the production-generated registry into maker1 with writable ownership."""
    if not registry_path.is_file() or registry_path.stat().st_size == 0:
        raise RuntimeError("the rented bond registry artifact is unavailable")
    destination = f"/home/jm/.joinmarket-ng/{registry_path.name}"
    await asyncio.to_thread(
        compose, "cp", str(registry_path), f"ring-maker1:{destination}"
    )
    await asyncio.to_thread(
        compose,
        "exec",
        "-T",
        "--user",
        "root",
        "ring-maker1",
        "sh",
        "-c",
        f"chown 1000:1000 {destination} && chmod 600 {destination} && test -s {destination}",
    )
    await asyncio.to_thread(
        compose,
        "exec",
        "-T",
        "ring-maker1",
        "sh",
        "-c",
        f"test -r {destination} && test -w {destination}",
    )


async def wait_for_rented_bond_offer(
    taker: Taker, rental: PurchasedBondRental, timeout: float = 120
) -> None:
    """Wait for the restarted maker's ordinary offers to prove its rental."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        offers = await taker.directory_client.fetch_orderbook(
            max_wait=10, min_wait=2, quiet_period=1
        )
        verified_height = await taker._update_offers_with_bond_values(offers)
        matches = [
            offer
            for offer in offers
            if offer.fidelity_bond_verified is True
            and offer.fidelity_bond_data is not None
            and offer.fidelity_bond_data["utxo_txid"] == rental.bond.outpoint.txid
            and offer.fidelity_bond_data["utxo_vout"] == rental.bond.outpoint.vout
            and offer.fidelity_bond_data["utxo_pub"] == rental.bond.pubkey
            and offer.fidelity_bond_data["locktime"] == rental.bond.locktime
        ]
        if matches:
            assert verified_height is not None
            assert all(
                offer.fidelity_bond_data is not None
                and offer.fidelity_bond_data["cert_pub"] == rental.certificate_pubkey
                for offer in matches
            )
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("ring-maker1 did not advertise the rented fidelity bond")
        await asyncio.sleep(12)


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


def assert_retained_taker_podle_state(data_dir: Path) -> None:
    """Refuse retained makers when the local spent-commitment ledger is unknown."""
    manager = PoDLEManager(data_dir)
    try:
        if not manager.used_commitments:
            raise RuntimeError(
                "retained ring E2E makers require recovered durable host-taker PoDLE "
                "claims; do not replay a commitment from an absent or empty ledger"
            )
    finally:
        manager.close()


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


async def assert_no_pending_channels() -> None:
    for service in LND_SERVICES:
        pending = await asyncio.to_thread(lncli, service, "pendingchannels")
        assert not pending["pending_open_channels"]
        assert not pending["pending_closing_channels"]
        assert not pending["pending_force_closing_channels"]
        assert not pending["waiting_close_channels"]


def bootstrap_funding_txid(opened: dict[str, Any], opener: str) -> str:
    """Validate the hexadecimal funding txid reported by ``openchannel``."""
    txid = opened.get("funding_txid_str") or opened.get("funding_txid")
    if not isinstance(txid, str) or len(txid) != 64:
        raise RuntimeError(f"{opener} did not return a bootstrap funding transaction")
    try:
        int(txid, 16)
    except ValueError:
        raise RuntimeError(
            f"{opener} did not return a bootstrap funding transaction"
        ) from None
    return txid.lower()


def matching_pending_bootstrap_points(
    pending: dict[str, Any], fundee_id: str, funding_txid: str, opener: str
) -> list[str]:
    """Return canonical points for one peer and one just-opened funding transaction."""
    channels = pending.get("pending_open_channels")
    if not isinstance(channels, list):
        raise RuntimeError(
            f"{opener} returned an invalid pending bootstrap channel response"
        )

    points: list[str] = []
    for pending_channel in channels:
        if not isinstance(pending_channel, dict):
            raise RuntimeError(
                f"{opener} returned an invalid pending bootstrap channel response"
            )
        channel = pending_channel.get("channel")
        if not isinstance(channel, dict):
            raise RuntimeError(
                f"{opener} returned an invalid pending bootstrap channel response"
            )
        if channel.get("remote_node_pub") != fundee_id:
            continue
        channel_point = channel.get("channel_point")
        if not isinstance(channel_point, str):
            raise RuntimeError(
                f"{opener} returned an invalid pending bootstrap channel response"
            )
        txid, separator, vout = channel_point.partition(":")
        if (
            separator != ":"
            or txid.lower() != funding_txid
            or not vout.isdecimal()
            or int(vout) > 0xFFFFFFFF
        ):
            continue
        points.append(f"{funding_txid}:{int(vout)}")
    return points


async def open_test_channel(
    opener: str,
    fundee: str,
    mining_address: str,
    *,
    private: bool,
    push_amount: int = 0,
) -> str:
    """Open and activate one direct channel used only by this E2E test."""
    fundee_info = await asyncio.to_thread(lncli, fundee, "getinfo")
    fundee_id = str(fundee_info["identity_pubkey"])
    await ensure_peer_connected(
        opener,
        fundee_id,
        onion_endpoint(fundee),
        peer_role="bootstrap market peer",
    )
    args = [
        "openchannel",
        f"--node_key={fundee_id}",
        f"--local_amt={BOOTSTRAP_CHANNEL_CAPACITY_SATS}",
    ]
    if private:
        args.append("--channel_type=taproot")
    if push_amount:
        args.append(f"--push_amt={push_amount}")
    if private:
        args.append("--private")
    opened = await asyncio.to_thread(lncli, opener, *args)
    txid = bootstrap_funding_txid(opened, opener)
    deadline = asyncio.get_running_loop().time() + 120
    while True:
        pending = await asyncio.to_thread(lncli, opener, "pendingchannels")
        points = matching_pending_bootstrap_points(pending, fundee_id, txid, opener)
        if len(points) == 1:
            point = points[0]
            break
        if len(points) > 1:
            raise RuntimeError(
                f"{opener} reported ambiguous pending bootstrap channels"
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"{opener} did not report a pending bootstrap channel")
        await asyncio.sleep(1)

    await mine_and_sync(3, mining_address)
    deadline = asyncio.get_running_loop().time() + 120
    while True:
        opener_channels = (await asyncio.to_thread(lncli, opener, "listchannels"))[
            "channels"
        ]
        fundee_channels = (await asyncio.to_thread(lncli, fundee, "listchannels"))[
            "channels"
        ]
        if any(
            str(channel["channel_point"]) == point and channel["active"]
            for channel in opener_channels
        ) and any(
            str(channel["channel_point"]) == point and channel["active"]
            for channel in fundee_channels
        ):
            assert await local_balance(opener, point) > 0
            assert await local_balance(fundee, point) >= 0
            return point
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"bootstrap channel did not activate between {opener} and {fundee}"
            )
        await asyncio.sleep(2)


def bootstrap_pending_channel_state(
    pending: dict[str, Any], channel_point: str, service: str
) -> tuple[str, int | None]:
    """Return one bootstrap point's redacted close phase and confirmation depth.

    LND reports modern cooperative closes in ``waiting_close_channels``. Once
    that response includes a confirmation depth, the shutdown transaction is
    broadcast and is treated as the semantic ``pending_closing`` phase.
    """
    pending_groups = (
        "pending_open_channels",
        "pending_closing_channels",
        "pending_force_closing_channels",
        "waiting_close_channels",
    )
    states: list[tuple[str, int | None]] = []
    for group in pending_groups:
        channels = pending.get(group)
        if not isinstance(channels, list):
            raise RuntimeError(f"{service} returned invalid bootstrap channel state")
        for item in channels:
            if not isinstance(item, dict):
                raise RuntimeError(
                    f"{service} returned invalid bootstrap channel state"
                )
            channel = item.get("channel")
            if not isinstance(channel, dict):
                raise RuntimeError(
                    f"{service} returned invalid bootstrap channel state"
                )
            if channel.get("channel_point") != channel_point:
                continue
            phase = group.removesuffix("_channels")
            close_confirmations: int | None = None
            if group in {"pending_closing_channels", "waiting_close_channels"}:
                blocks = item.get("blocks_til_close_confirmed")
                if blocks is not None:
                    if isinstance(blocks, bool) or not isinstance(blocks, (int, str)):
                        raise RuntimeError(
                            f"{service} returned invalid bootstrap channel state"
                        )
                    try:
                        close_confirmations = int(blocks)
                    except ValueError:
                        raise RuntimeError(
                            f"{service} returned invalid bootstrap channel state"
                        ) from None
                    if close_confirmations < 0:
                        raise RuntimeError(
                            f"{service} returned invalid bootstrap channel state"
                        )
                if (
                    group == "waiting_close_channels"
                    and close_confirmations is not None
                ):
                    phase = "pending_closing"
            states.append((phase, close_confirmations))
    if len(states) > 1:
        raise RuntimeError(f"{service} returned ambiguous bootstrap channel state")
    return states[0] if states else ("absent", None)


def bootstrap_channel_is_listed(
    listed: dict[str, Any], channel_point: str, service: str
) -> bool:
    """Return whether one exact bootstrap point remains in ``listchannels``."""
    channels = listed.get("channels")
    if not isinstance(channels, list):
        raise RuntimeError(f"{service} returned invalid bootstrap channel state")
    for channel in channels:
        if not isinstance(channel, dict):
            raise RuntimeError(f"{service} returned invalid bootstrap channel state")
        if channel.get("channel_point") == channel_point:
            return True
    return False


async def bootstrap_close_state(
    points: dict[str, tuple[str, str]],
) -> tuple[bool, bool, int | None, tuple[str, ...]]:
    """Read closure state for every bootstrap point at both of its endpoints."""
    all_absent = True
    all_broadcast_or_absent = True
    confirmations_needed: int | None = None
    state_labels: list[str] = []
    for channel_point, (opener, fundee) in points.items():
        for service in (opener, fundee):
            listed, pending = await asyncio.gather(
                bootstrap_lncli(service, "listchannels"),
                bootstrap_lncli(service, "pendingchannels"),
            )
            listed_here = bootstrap_channel_is_listed(listed, channel_point, service)
            phase, close_confirmations = bootstrap_pending_channel_state(
                pending, channel_point, service
            )
            if listed_here or phase != "absent":
                all_absent = False
            effective_phase = "listed" if listed_here and phase == "absent" else phase
            if effective_phase == "pending_closing" and close_confirmations is not None:
                confirmations_needed = max(
                    confirmations_needed or 0, close_confirmations
                )
            else:
                all_broadcast_or_absent = False
            state_labels.append(f"{service}:{effective_phase}")
    return (
        all_absent,
        all_broadcast_or_absent,
        confirmations_needed,
        tuple(state_labels),
    )


async def bootstrap_peer_details(service: str) -> tuple[str, str]:
    """Return one bootstrap peer's node ID and single advertised endpoint."""
    info = await bootstrap_lncli(service, "getinfo")
    node_id = info.get("identity_pubkey")
    uris = info.get("uris")
    if not isinstance(node_id, str) or not node_id or not isinstance(uris, list):
        raise RuntimeError(f"{service} returned invalid bootstrap peer state")
    endpoints = [str(uri).partition("@")[2] for uri in uris if "@" in str(uri)]
    if len(endpoints) != 1 or not endpoints[0]:
        raise RuntimeError(f"{service} returned invalid bootstrap peer state")
    return node_id, endpoints[0]


async def ensure_bootstrap_close_peers(
    points: dict[str, tuple[str, str]], timeout: float
) -> None:
    """Keep both endpoints connected while cooperative closes negotiate."""
    services = {service for endpoints in points.values() for service in endpoints}
    details = dict(
        zip(
            services,
            await asyncio.gather(
                *(bootstrap_peer_details(service) for service in services)
            ),
            strict=True,
        )
    )
    await asyncio.gather(
        *(
            ensure_peer_connected(
                opener,
                *details[fundee],
                timeout=timeout,
                peer_role="bootstrap close peer",
            )
            for opener, fundee in points.values()
        ),
        *(
            ensure_peer_connected(
                fundee,
                *details[opener],
                timeout=timeout,
                peer_role="bootstrap close peer",
            )
            for opener, fundee in points.values()
        ),
    )


async def close_bootstrap_channels(
    points: dict[str, tuple[str, str]], mining_address: str
) -> None:
    """Cooperatively close each bootstrap channel before the target CoinJoin."""
    await ensure_bootstrap_close_peers(points, timeout=120)
    deadline = asyncio.get_running_loop().time() + 120
    for point, (opener, _) in points.items():
        txid, _, vout = point.partition(":")
        await bootstrap_lncli(
            opener,
            "closechannel",
            f"--funding_txid={txid}",
            f"--output_index={vout}",
        )
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            all_absent, _, _, state_labels = await bootstrap_close_state(points)
            if all_absent:
                return
            states = ", ".join(state_labels)
            raise RuntimeError(f"bootstrap cooperative close stalled: {states}")
        await ensure_bootstrap_close_peers(points, timeout=remaining)
        (
            all_absent,
            all_broadcast_or_absent,
            confirmations_needed,
            state_labels,
        ) = await bootstrap_close_state(points)
        if all_absent:
            return
        if all_broadcast_or_absent and confirmations_needed is not None:
            await mine_and_sync(max(1, confirmations_needed), mining_address)
            continue
        if asyncio.get_running_loop().time() >= deadline:
            states = ", ".join(state_labels)
            raise RuntimeError(f"bootstrap cooperative close stalled: {states}")
        await asyncio.sleep(1)


async def wait_for_offer_absence(taker: Taker, nick: str, timeout: float = 120) -> None:
    """Wait until a stopped maker's published offer has left the orderbook."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        offers = await taker.directory_client.fetch_orderbook(
            max_wait=10, min_wait=2, quiet_period=1
        )
        if all(offer.counterparty != nick for offer in offers):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"stopped maker offer remained visible: {nick}")
        await asyncio.sleep(5)


async def wait_for_maker_nick(
    service: str, previous: str | None = None, timeout: float = 180
) -> str:
    """Read this fixture process's identity, never reuse a stopped maker's nick."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            result = await asyncio.to_thread(
                compose,
                "exec",
                "-T",
                service,
                "cat",
                "/home/jm/.joinmarket-ng/state/maker_taproot.nick",
            )
            nick = result.stdout.strip()
            if nick and nick != previous:
                return nick
        except subprocess.CalledProcessError:
            # The restarted process may not have published its nick yet.
            pass
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("maker did not publish its current process identity")
        await asyncio.sleep(1)


async def wait_for_eligible_offer_nicks(
    taker: Taker, expected_count: int, timeout: float = 180
) -> frozenset[str]:
    """Return the exact currently eligible ordinary-maker set for one buyout round."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        offers = await taker.directory_client.fetch_orderbook(
            max_wait=10, min_wait=2, quiet_period=1
        )
        nicks = frozenset(
            offer.counterparty
            for offer in offers
            if offer.minsize <= BUYOUT_CJ_AMOUNT <= offer.maxsize
        )
        if len(nicks) == expected_count:
            return nicks
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("eligible maker offers did not reach the expected count")
        await asyncio.sleep(5)


# Stay well inside the wallet's address discovery gap limit.
MAX_REPLENISHMENT_RECEIVE_INDEX = 8


def ring_maker_replenishment_address(service: str, receive_index: int) -> str:
    """Derive a fresh, descriptor-covered regtest receive address for this maker."""
    if not 1 <= receive_index <= MAX_REPLENISHMENT_RECEIVE_INDEX:
        raise ValueError("refusing a reserved or undiscoverable replenishment index")
    key = HDKey.from_seed(mnemonic_to_seed(RING_MAKER_MNEMONICS[service]))
    return key.derive(f"m/86'/1'/0'/0/{receive_index}").get_p2tr_address("regtest")


async def replenish_ring_maker_wallets_for_buyout(
    taker: Taker,
    ring_maker_nicks: frozenset[str],
    config: TakerConfig,
    mining_address: str,
    timeout: float = 720,
    *,
    receive_index: int = 1,
    receive_index_overrides: Mapping[str, int] | None = None,
    excluded_service: str | None = None,
) -> None:
    """Fund fresh regtest receive indices, then await exactly the live maker nicks.

    ``receive_index_overrides`` names a later index for a maker whose wallet
    already handed out ``receive_index`` itself, as an earlier buyer.
    """
    overrides = dict(receive_index_overrides or {})
    if excluded_service is not None and excluded_service not in RING_MAKER_MNEMONICS:
        raise ValueError("excluded service is not a ring maker")
    services = [
        service for service in RING_MAKER_MNEMONICS if service != excluded_service
    ]
    if len(ring_maker_nicks) != len(services):
        raise RuntimeError("maker nick count does not match the funded regtest wallets")
    chain = await bootstrap_bitcoin_rpc("getblockchaininfo")
    if str(chain.get("chain", "")) != "regtest":
        raise RuntimeError("refusing ring-maker replenishment outside regtest")

    amount_btc = float(
        Decimal(RING_MAKER_BUYOUT_REPLENISHMENT_SATS) / Decimal(100_000_000)
    )
    for service in services:
        # Index zero funded the target and each later buyout round uses a
        # fresh index, so rounds are not linked through address reuse.
        address = ring_maker_replenishment_address(
            service, overrides.get(service, receive_index)
        )
        await bootstrap_bitcoin_rpc(
            "sendtoaddress", [address, amount_btc], wallet=FUNDER_WALLET
        )
    await mine_and_sync(config.taker_utxo_age + 1, mining_address)

    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        offers = await taker.directory_client.fetch_orderbook(
            max_wait=10, min_wait=2, quiet_period=1
        )
        ready = {
            offer.counterparty
            for offer in offers
            if offer.counterparty in ring_maker_nicks
            and offer.minsize <= BUYOUT_CJ_AMOUNT <= offer.maxsize
        }
        if ready == ring_maker_nicks:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                "ring makers did not reoffer confirmed replenishment UTXOs"
            )
        await asyncio.sleep(15)


async def fund_buyout_taker_input(wallet: WalletService) -> str:
    """Fund one independently selectable, retained regtest P2TR buyer input."""
    chain = await bootstrap_bitcoin_rpc("getblockchaininfo")
    if chain.get("chain") != "regtest":
        raise RuntimeError("refusing buyout taker funding outside regtest")
    address = await wallet.get_new_address_verified(BUYOUT_SOURCE_MIXDEPTH)
    script = address_to_scriptpubkey_for_network(address, "regtest")
    if not script.startswith(b"\x51\x20"):
        raise RuntimeError("buyout taker funding address is not P2TR")
    value = 1_500_000
    txid = str(
        await bootstrap_bitcoin_rpc(
            "sendtoaddress",
            [address, float(Decimal(value) / Decimal(100_000_000))],
            wallet=FUNDER_WALLET,
        )
    )
    tx = parse_transaction(
        str(await bootstrap_bitcoin_rpc("getrawtransaction", [txid]))
    )
    matches = [
        index
        for index, output in enumerate(tx.outputs)
        if output.script == script and output.value == value
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "regtest buyout funding did not create one exact P2TR output"
        )
    return f"{txid}:{matches[0]}"


def numeric_short_channel_id(value: object) -> int:
    """Parse LND's decimal uint64 SCID, never treating hexadecimal as a SCID."""
    if isinstance(value, bool):
        raise RuntimeError("LND returned an invalid numeric short channel ID")
    if isinstance(value, int):
        channel_id = value
    elif isinstance(value, str) and value.isdecimal():
        channel_id = int(value)
    else:
        raise RuntimeError("LND returned a nonnumeric short channel ID")
    if not 0 < channel_id <= 0xFFFFFFFFFFFFFFFF:
        raise RuntimeError("LND returned an invalid numeric short channel ID")
    return channel_id


def short_channel_id_from_position(
    block_height: int, transaction_index: int, vout: int
) -> int:
    """Encode the BOLT SCID layout used by LND's confirmed channel state."""
    if not (
        0 < block_height <= 0xFFFFFF
        and 0 <= transaction_index <= 0xFFFFFF
        and 0 <= vout <= 0xFFFF
    ):
        raise RuntimeError("Bitcoin returned an invalid confirmed channel position")
    return (block_height << 40) | (transaction_index << 16) | vout


def channel_point_parts(channel_point: str) -> tuple[str, int]:
    """Validate a channel funding outpoint without exposing it in errors."""
    txid, separator, output_index = channel_point.partition(":")
    if (
        separator != ":"
        or len(txid) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in txid)
        or not output_index.isdecimal()
        or len(output_index) > 10
        or int(output_index) > 0xFFFFFFFF
    ):
        raise RuntimeError("invalid channel point")
    return txid.lower(), int(output_index)


def channel_state(
    listed: Mapping[str, Any], channel_point: str, service: str
) -> dict[str, Any]:
    """Return one exact channel state without putting its outpoint in failures."""
    channels = listed.get("channels")
    if not isinstance(channels, list):
        raise RuntimeError(f"{service} returned invalid channel state")
    matches = [
        channel
        for channel in channels
        if isinstance(channel, dict) and channel.get("channel_point") == channel_point
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{service} did not report one expected channel")
    return matches[0]


def channel_reserve_and_balance(channel: Mapping[str, Any]) -> tuple[int, int]:
    """Read sender liquidity after its negotiated LND channel reserve."""
    constraints = channel.get("local_constraints")
    if not isinstance(constraints, dict):
        raise RuntimeError("LND returned invalid local channel constraints")
    try:
        balance = int(channel["local_balance"])
        reserve = int(constraints["chan_reserve_sat"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(
            "LND returned invalid directional channel liquidity"
        ) from None
    if balance < 0 or reserve < 0:
        raise RuntimeError("LND returned invalid directional channel liquidity")
    return reserve, balance


def route_hop_channel_ids(route: Mapping[str, Any]) -> tuple[int, ...]:
    """Return the numeric channel IDs LND actually used in a serialized route."""
    hops = route.get("hops")
    if not isinstance(hops, list) or not hops:
        raise RuntimeError("LND did not report a payment route")
    channel_ids: list[int] = []
    for hop in hops:
        if not isinstance(hop, dict):
            raise RuntimeError("LND returned an invalid payment route")
        channel_ids.append(numeric_short_channel_id(hop.get("chan_id")))
    return tuple(channel_ids)


def route_hop_pubkeys(route: Mapping[str, Any]) -> tuple[str, ...]:
    """Return route destinations, rejecting malformed LND route entries."""
    hops = route.get("hops")
    if not isinstance(hops, list) or not hops:
        raise RuntimeError("LND did not report a payment route")
    pubkeys = tuple(hop.get("pub_key") for hop in hops if isinstance(hop, dict))
    if len(pubkeys) != len(hops) or any(
        not isinstance(pubkey, str) for pubkey in pubkeys
    ):
        raise RuntimeError("LND returned an invalid payment route")
    return cast(tuple[str, ...], pubkeys)


async def channel_short_id(service: str, channel_point: str) -> int:
    listed = await bootstrap_lncli(service, "listchannels")
    reported = channel_state(listed, channel_point, service).get("chan_id")
    try:
        return numeric_short_channel_id(reported)
    except RuntimeError:
        # Some CLI JSON paths expose a hexadecimal channel identifier instead of
        # the uint64 SCID BuildRoute requires. Derive the confirmed SCID rather
        # than ever passing that identifier to --outgoing_chan_id.
        txid, vout = channel_point_parts(channel_point)
        transaction = await bitcoin_rpc("getrawtransaction", [txid, True])
        if not isinstance(transaction, dict) or not isinstance(
            transaction.get("blockhash"), str
        ):
            raise RuntimeError("Bitcoin did not report the channel funding block")
        block = await bitcoin_rpc("getblock", [transaction["blockhash"], 1])
        if not isinstance(block, dict) or not isinstance(block.get("tx"), list):
            raise RuntimeError("Bitcoin did not report the channel funding position")
        try:
            block_height = int(block["height"])
            transaction_index = block["tx"].index(txid)
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                "Bitcoin did not report the channel funding position"
            ) from None
        return short_channel_id_from_position(block_height, transaction_index, vout)


async def pin_payment_to_channel(
    source: str,
    destination: str,
    channel_point: str,
    amount: int,
    memo: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pay a fresh no-hint invoice through one exact numeric LND channel ID."""
    source_state = channel_state(
        await bootstrap_lncli(source, "listchannels"), channel_point, source
    )
    destination_info = await bootstrap_lncli(destination, "getinfo")
    destination_id = destination_info.get("identity_pubkey")
    if not isinstance(destination_id, str) or not destination_id:
        raise RuntimeError("LND returned invalid payment destination state")
    channel_id = await channel_short_id(source, channel_point)
    created = await bootstrap_lncli(
        destination, "addinvoice", f"--amt={amount}", f"--memo={memo}"
    )
    payment_request = created.get("payment_request")
    payment_hash = created.get("r_hash")
    if not isinstance(payment_request, str) or not isinstance(payment_hash, str):
        raise RuntimeError("LND did not create a usable payment invoice")
    decoded = await bootstrap_lncli(destination, "decodepayreq", payment_request)
    if decoded.get("route_hints") != []:
        raise RuntimeError("LND created an invoice with unexpected route hints")
    try:
        final_cltv_delta = int(decoded["cltv_expiry"])
        payment_addr = str(decoded["payment_addr"])
        invoice_amount = int(decoded["num_satoshis"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("LND returned an invalid decoded payment invoice") from None
    if not (
        final_cltv_delta > 0
        and len(payment_addr) == 64
        and decoded.get("destination") == destination_id
        and decoded.get("payment_hash") == payment_hash
        and invoice_amount == amount
    ):
        raise RuntimeError("LND returned an invalid decoded payment invoice")
    built = await bootstrap_lncli(
        source,
        "buildroute",
        f"--amt={amount}",
        f"--final_cltv_delta={final_cltv_delta}",
        f"--hops={destination_id}",
        f"--outgoing_chan_id={channel_id}",
    )
    route = built.get("route")
    if not isinstance(route, dict) or route_hop_channel_ids(route) != (channel_id,):
        raise RuntimeError("LND did not build the requested one-hop payment route")
    hops = route.get("hops")
    hop = hops[0] if isinstance(hops, list) and len(hops) == 1 else None
    try:
        exact_route = (
            isinstance(hop, dict)
            and hop.get("pub_key") == destination_id
            and int(hop["amt_to_forward_msat"]) == amount * 1_000
            and int(hop["fee_msat"]) == 0
        )
    except (KeyError, TypeError, ValueError):
        exact_route = False
    if not exact_route or not isinstance(hop, dict):
        raise RuntimeError("LND did not build the requested one-hop payment route")
    hop["mpp_record"] = {
        "payment_addr": payment_addr,
        "total_amt_msat": str(amount * 1_000),
    }
    hop["tlv_payload"] = True
    attempt = await bootstrap_lncli(
        source, "sendtoroute", f"--payment_hash={payment_hash}", json.dumps(built)
    )
    status = str(attempt.get("status", "")).upper()
    attempt_route = attempt.get("route")
    preimage = attempt.get("preimage")
    if not (
        status in {"SUCCEEDED", "2"}
        and not attempt.get("failure")
        and isinstance(attempt_route, dict)
        and route_hop_channel_ids(attempt_route) == (channel_id,)
        and isinstance(preimage, str)
        and len(preimage) == 64
        and hashlib.sha256(bytes.fromhex(preimage)).hexdigest() == payment_hash
    ):
        raise RuntimeError("LND did not settle the pinned payment")
    settled = await bootstrap_lncli(destination, "lookupinvoice", payment_hash)
    if (
        not (
            bool(settled.get("settled"))
            or str(settled.get("state", "")).upper() == "SETTLED"
        )
        or int(settled.get("amt_paid_msat", -1)) != amount * 1_000
    ):
        raise RuntimeError("LND did not report the pinned invoice as settled")
    return source_state, channel_state(
        await bootstrap_lncli(source, "listchannels"), channel_point, source
    )


async def pay_directed_edges(node_service: dict[str, str], record: Any) -> None:
    """Exercise each ring edge over its exact created channel, without hints."""
    participant_node = {
        participant.participant_key: participant.node_id
        for participant in record.coordinator_participants
    }
    assert record.manifest is not None
    for index, edge in enumerate(record.manifest.edges):
        opener = node_service[participant_node[edge.opener_key]]
        fundee = node_service[participant_node[edge.acceptor_key]]
        point = f"{record.manifest.unsigned_txid}:{edge.output_index}"
        opener_before = channel_state(
            await bootstrap_lncli(opener, "listchannels"), point, opener
        )
        fundee_before = channel_state(
            await bootstrap_lncli(fundee, "listchannels"), point, fundee
        )
        for channel, peer in (
            (opener_before, participant_node[edge.acceptor_key]),
            (fundee_before, participant_node[edge.opener_key]),
        ):
            if not (
                channel.get("remote_pubkey") == peer
                and channel.get("active") is True
                and channel.get("private") is True
                and int(channel.get("capacity", -1)) == edge.capacity
                and channel.get("pending_htlcs") == []
            ):
                raise RuntimeError("ring channel state does not match the created edge")
        if not (
            int(opener_before["local_balance"]) == int(fundee_before["remote_balance"])
            and int(opener_before["remote_balance"])
            == int(fundee_before["local_balance"])
        ):
            raise RuntimeError("ring channel balances are not reciprocal")
        before, _after = await pin_payment_to_channel(
            opener, fundee, point, 1_000, f"ring-edge-{index}"
        )
        try:
            sent_before = int(before["total_satoshis_sent"])
            received_before = int(fundee_before["total_satoshis_received"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                "LND returned invalid channel payment counters"
            ) from None
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            after = channel_state(
                await bootstrap_lncli(opener, "listchannels"), point, opener
            )
            fundee_after = channel_state(
                await bootstrap_lncli(fundee, "listchannels"), point, fundee
            )
            try:
                transferred = (
                    int(after["total_satoshis_sent"]) == sent_before + 1_000
                    and int(fundee_after["total_satoshis_received"])
                    == received_before + 1_000
                    and after.get("pending_htlcs") == []
                    and fundee_after.get("pending_htlcs") == []
                )
            except (KeyError, TypeError, ValueError):
                raise RuntimeError(
                    "LND returned invalid channel payment counters"
                ) from None
            if transferred:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    "pinned ring-edge payment did not update channel counters"
                )
            await asyncio.sleep(1)
        if not (
            int(after["local_balance"]) < int(before["local_balance"])
            and int(fundee_after["local_balance"]) > int(fundee_before["local_balance"])
        ):
            raise RuntimeError(
                "pinned ring-edge payment did not transfer the expected balance"
            )


def escrow_macaroon_path(service: str) -> Path:
    """The dedicated ChannelEscrow macaroon the patched LND image bakes."""
    return lnd_path(service, "data/chain/bitcoin/regtest/channelescrow.macaroon")


def buyout_settings(
    service: str,
    node_id: str,
    peer_node_id: str,
    journal: Path,
    payout_address: str,
    *,
    mixdepth: int,
    wallet_fingerprint: str | None = None,
) -> BuyoutSettings:
    """One explicitly enabled regtest buyout endpoint on a live ring node."""
    values: dict[str, Any] = {
        "enabled": True,
        "automatic_force_close": False,
        "network": "regtest",
        "journal": journal,
        "lnd_endpoint": f"127.0.0.1:{LND_PORTS[service]}",
        "lnd_identity": node_id,
        "lnd_tls_cert": lnd_path(service, "tls.cert"),
        "lnd_peer_macaroon": lnd_path(service, "admin.macaroon"),
        "lnd_escrow_macaroon": escrow_macaroon_path(service),
        "bitcoin_rpc_url": RPC_URL,
        "bitcoin_rpc_user": "test",
        "bitcoin_rpc_password": "test",
        "allowed_peers": (peer_node_id,),
        "payout_addresses": [payout_address],
        "mixdepth": mixdepth,
        "poll_interval_seconds": 1.0,
    }
    if wallet_fingerprint is not None:
        values["wallet_fingerprint"] = wallet_fingerprint
    return BuyoutSettings.model_validate(values)


def node_buyout_journal(
    data_dir: Path, service: str, role: Literal["buyer", "counterparty"]
) -> Path:
    """The one journal of an LN node and role, shared by every buyout it makes.

    The same-ring sibling rule is enforced per journal, so a per-scenario
    journal would hide an earlier buyout of the node's adjacent ring edge.
    """
    directory = data_dir
    for part in ("buyout-journals", service.removeprefix("lnd-")):
        directory = directory / part
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory / role / "sessions.sqlite"


@dataclass(frozen=True)
class BuyerContext:
    """One real JoinMarket wallet that buys a selected ring-created channel."""

    name: str
    service: str
    mnemonic: str
    wallet: WalletService
    backend: DescriptorWalletBackend
    config: TakerConfig
    available_podles: Mapping[str, PurchasedPoDLE]
    eligible_maker_nicks: frozenset[str]
    input_utxo: str


@dataclass(frozen=True)
class SettlementRouteChannel:
    """One directed channel that must carry a cooperative settlement payment."""

    channel_point: str
    source: str
    destination: str


def settlement_route_is_contiguous(
    route: tuple[SettlementRouteChannel, ...], source: str, destination: str
) -> bool:
    """Whether directed helper channels form exactly one settlement path."""
    return (
        bool(route)
        and route[0].source == source
        and route[-1].destination == destination
        and all(
            previous.destination == current.source
            for previous, current in zip(route, route[1:])
        )
    )


def external_podle_commitments(manager: Any) -> frozenset[str]:
    """Read the strict external pool, whose cursor may select any available entry."""
    records = getattr(manager, "external_v1", None)
    if not isinstance(records, dict) or not all(
        isinstance(item, str) for item in records
    ):
        raise RuntimeError("PoDLE manager returned an invalid external credential pool")
    return frozenset(records)


def public_channel_funding_txid(channel_point: str) -> str:
    """Validate and extract a funding transaction ID without exposing it in errors."""
    try:
        txid, _ = channel_point_parts(channel_point)
    except RuntimeError:
        raise RuntimeError("invalid public channel point")
    return txid


async def public_channel_confirmation_counts(
    channel_points: tuple[str, ...],
) -> list[int | None]:
    """Return confirmation depths, using ``None`` for still-unconfirmed funding."""
    funding_txids = tuple(
        public_channel_funding_txid(point) for point in channel_points
    )
    transactions = await asyncio.gather(
        *(bitcoin_rpc("getrawtransaction", [txid, True]) for txid in funding_txids)
    )
    confirmations: list[int | None] = []
    for transaction in transactions:
        if not isinstance(transaction, dict):
            raise RuntimeError(
                "Bitcoin RPC returned an invalid public channel transaction"
            )
        depth = transaction.get("confirmations")
        if depth is None:
            confirmations.append(None)
        elif isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
            confirmations.append(depth)
        else:
            raise RuntimeError(
                "Bitcoin RPC returned an invalid public channel confirmation"
            )
    return confirmations


async def wait_for_public_graph(
    node_service: dict[str, str],
    links: set[frozenset[str]],
    channel_points: tuple[str, ...],
    timeout: float = 120,
    *,
    after_graph_readiness_block: bool = False,
) -> None:
    """Wait until every live LND graph learns the announced routing links."""
    services = tuple(node_service.values())
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        graphs = await asyncio.gather(
            *(
                asyncio.to_thread(
                    lncli, service, "describegraph", "--include_unannounced=false"
                )
                for service in services
            )
        )
        known = []
        for graph in graphs:
            edges = graph.get("edges")
            if not isinstance(edges, list):
                raise RuntimeError("LND returned an invalid public graph")
            known.append(
                {
                    frozenset((str(edge["node1_pub"]), str(edge["node2_pub"])))
                    for edge in edges
                    if isinstance(edge, dict)
                }
            )
        if all(links <= graph for graph in known):
            return
        if asyncio.get_running_loop().time() >= deadline:
            confirmations = await public_channel_confirmation_counts(channel_points)
            missing_by_service = {
                service: len(links - graph)
                for service, graph in zip(services, known, strict=True)
            }
            reason = (
                " after a state-driven post-readiness block"
                if after_graph_readiness_block
                else ""
            )
            raise PublicGraphPropagationTimeout(
                "announced settlement links did not propagate"
                f"{reason} (channel_confirmation_counts={confirmations}, "
                f"missing_expected_edge_counts_per_node={missing_by_service})"
            )
        await asyncio.sleep(1)


async def mature_public_links_and_wait_for_graph(
    channel_points: tuple[str, ...],
    expected_public_edges: set[frozenset[str]],
    node_service: dict[str, str],
    mining_address: str,
    confirmation_timeout: float = 120,
    graph_timeout: float = 120,
) -> None:
    """Reach LND gossip maturity, then require the two public links in every graph."""
    deadline = asyncio.get_running_loop().time() + confirmation_timeout
    while True:
        confirmation_counts = await public_channel_confirmation_counts(channel_points)
        if all(
            isinstance(count, int) and not isinstance(count, bool) and count > 0
            for count in confirmation_counts
        ):
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                "public channel funding did not confirm "
                f"(channel_confirmation_counts={confirmation_counts})"
            )
        await asyncio.sleep(1)

    confirmed_counts = [count for count in confirmation_counts if count is not None]
    assert len(confirmed_counts) == len(confirmation_counts)
    missing_depth = max(0, PUBLIC_CHANNEL_GOSSIP_CONFIRMATIONS - min(confirmed_counts))
    if missing_depth:
        await mine_and_sync(missing_depth, mining_address)

    try:
        await wait_for_public_graph(
            node_service, expected_public_edges, channel_points, timeout=graph_timeout
        )
    except PublicGraphPropagationTimeout:
        # LND can require one post-readiness confirmation before announcing a
        # link. This mine is reached only while the graph predicate is false.
        await mine_and_sync(1, mining_address)
        await wait_for_public_graph(
            node_service,
            expected_public_edges,
            channel_points,
            timeout=graph_timeout,
            after_graph_readiness_block=True,
        )


async def wait_for_spent_channel_removal(
    services: tuple[str, str], channel_point: str, timeout: float = 120
) -> None:
    """Wait until neither endpoint can route through the bought channel."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        listed = await asyncio.gather(
            *(asyncio.to_thread(lncli, service, "listchannels") for service in services)
        )
        if all(
            all(
                str(channel["channel_point"]) != channel_point
                for channel in result["channels"]
            )
            for result in listed
        ):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("spent bought channel remained a routing candidate")
        await asyncio.sleep(1)


async def assert_active_channel(services: tuple[str, str], channel_point: str) -> None:
    """Assert that one independent direct channel remains usable at both endpoints."""
    listed = await asyncio.gather(
        *(asyncio.to_thread(lncli, service, "listchannels") for service in services)
    )
    for channels in listed:
        matches = [
            channel
            for channel in channels["channels"]
            if str(channel["channel_point"]) == channel_point
        ]
        assert len(matches) == 1
        assert matches[0]["active"]


async def settlement_payment(service: str, payment_hash: str) -> dict[str, Any]:
    """Return the one LND payment for this settlement without exposing its invoice."""
    payments = (
        await asyncio.to_thread(lncli, service, "listpayments", "--include_incomplete")
    ).get("payments", [])
    matches = [
        payment for payment in payments if payment.get("payment_hash") == payment_hash
    ]
    assert len(matches) == 1
    return matches[0]


async def pay_over_edge(
    source: str, destination: str, channel_point: str, amount: int, memo: str
) -> None:
    """Move ``amount`` through the intended direct channel, never a discovered path."""
    before, after = await pin_payment_to_channel(
        source, destination, channel_point, amount, memo
    )
    if int(after["local_balance"]) != int(before["local_balance"]) - amount:
        raise RuntimeError("pinned rebalance did not transfer the expected balance")


async def local_balance(service: str, channel_point: str) -> int:
    """The node's own share of one channel, as LND currently reports it."""
    channels = (await asyncio.to_thread(lncli, service, "listchannels"))["channels"]
    balances = [
        int(channel["local_balance"])
        for channel in channels
        if str(channel["channel_point"]) == channel_point
    ]
    if len(balances) != 1:
        raise RuntimeError(
            f"{service} did not report exactly one expected channel balance"
        )
    return balances[0]


async def wait_for_local_balance(
    service: str, channel_point: str, maximum: int, timeout: float = 90
) -> int:
    """Block until the node's share of one channel is at or below ``maximum``."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        balance = await local_balance(service, channel_point)
        if balance <= maximum:
            return balance
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                "channel did not rebalance to its required balance bound"
            )
        await asyncio.sleep(1)


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


async def ensure_peer_connected(
    service: str,
    node_id: str,
    endpoint: str,
    timeout: float = 300,
    *,
    peer_role: str = "buyout counterparty",
) -> None:
    """Connect and verify an LND peer is observable in ``listpeers``.

    A successful ``connect`` response only initiates a connection. Retry until
    LND reports the exact remote node as connected.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    attempts = 0
    backoff = 1.0
    last_exception = "none"
    peer_state_observed = False
    while loop.time() < deadline:
        try:
            peers = (
                await asyncio.to_thread(
                    lncli,
                    service,
                    "listpeers",
                    timeout=min(30.0, deadline - loop.time()),
                )
            ).get("peers") or []
        except subprocess.CalledProcessError as exc:
            last_exception = f"{type(exc).__name__}(exit_status={exc.returncode})"
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            last_exception = type(exc).__name__
        else:
            peer_state_observed = peer_state_observed or bool(peers)
            if any(str(peer["pub_key"]) == node_id for peer in peers):
                return

        if loop.time() >= deadline:
            break
        attempts += 1
        try:
            remaining = deadline - loop.time()
            await asyncio.to_thread(
                lncli,
                service,
                "connect",
                "--timeout",
                f"{max(1, int(min(125.0, remaining)))}s",
                f"{node_id}@{endpoint}",
                timeout=remaining,
            )
        except subprocess.CalledProcessError as exc:
            last_exception = f"{type(exc).__name__}(exit_status={exc.returncode})"
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            last_exception = type(exc).__name__

        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(backoff, remaining))
        backoff = min(backoff * 2, 10.0)

    raise RuntimeError(
        "LND peer did not become connected "
        f"(attempts={attempts}, last_exception={last_exception}, "
        f"peer_state_observed={peer_state_observed})"
    )


async def wait_for_buyout_state(
    runtime: BuyoutRuntime,
    session_id: str,
    wanted: set[str],
    timeout: float = BUYOUT_STATE_TIMEOUT,
) -> str:
    """Poll one runtime until its session reaches ``wanted``, never past a failure."""
    deadline = asyncio.get_running_loop().time() + timeout
    state = "UNPOLLED"
    while True:
        state = (await runtime.poll_once()).get(session_id, "UNKNOWN")
        if state in wanted:
            return state
        if state in BUYOUT_FAILED_STATES or state == UNBOUND_STATE:
            raise RuntimeError(
                f"buyout session reached {state} instead of {sorted(wanted)}"
            )
        if state.startswith(ERROR_STATE_PREFIX):
            raise RuntimeError("buyout session could not be polled")
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"buyout session stayed in {state}, expected {sorted(wanted)}"
            )
        await asyncio.sleep(BUYOUT_POLL_INTERVAL)


@dataclass(frozen=True)
class RingMakerBuyer:
    """A ring maker's own wallet, prepared to buy one of its ring edges."""

    context: BuyerContext
    podle: PurchasedPoDLE
    maker_nick: str
    maker4_nick: str


async def prepare_ring_maker_buyer(
    operations_dir: Path,
    service: str,
    name: str,
    target_config: TakerConfig,
    destination: str,
    *,
    receive_index: int,
    maker4_nick: str,
    receive_index_overrides: Mapping[str, int] | None = None,
) -> RingMakerBuyer:
    """Stop one ring maker bot and ready its wallet for an ordinary buyout.

    The maker buys a PoDLE over a bootstrap channel that is closed again, the
    other two ring makers are replenished at fresh indices, and the ordinary
    maker's bot, which the caller stopped, is started again, so the buyout
    CoinJoin has three eligible counterparties. The caller owns the returned
    wallet and must restart the stopped ring maker bot.
    """
    if service not in RING_MAKER_MNEMONICS:
        raise ValueError("a maker buyer must be a ring maker")
    mnemonic = RING_MAKER_MNEMONICS[service]
    short = service.removeprefix("lnd-")
    maker_service = f"ring-{short}"
    maker_nick = await wait_for_maker_nick(maker_service)
    await asyncio.to_thread(compose, "stop", maker_service)
    buyer_data_dir = operations_dir / f"{short}-buyer"
    await fund_bootstrap_opener(service)
    bootstrap = await open_test_channel(
        service, "lnd-maker4", destination, private=True
    )
    podle = await purchase_external_podle(
        rpc_url=RPC_URL,
        directory_server=DIRECTORY,
        lncli=lncli,
        payer_service=service,
        seller_service="lnd-maker4",
        buyer_data_dir=buyer_data_dir,
        work_dir=operations_dir / f"{short}-external-podle-market",
        socks_port=TOR_SOCKS_PORT,
        include_route_hints=False,
    )
    await close_bootstrap_channels({bootstrap: (service, "lnd-maker4")}, destination)
    await assert_no_pending_channels()

    backend = DescriptorWalletBackend(
        rpc_url=RPC_URL,
        rpc_user="test",
        rpc_password="test",
        wallet_name=f"jm_{get_mnemonic_fingerprint(mnemonic)}_ring_e2e_regtest",
    )
    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network="regtest",
        mixdepth_count=5,
        address_type="p2tr",
        data_dir=buyer_data_dir,
    )
    config = target_config.model_copy(
        update={
            "mnemonic": SecretStr(mnemonic),
            "data_dir": buyer_data_dir,
            # This wallet already owns its maker LND at source mixdepth 0.
            # An ordinary buyout must not enroll the taker's LND as its own.
            "channel_ring": ChannelRingConfig(enabled=False),
        }
    )
    try:
        probe = await make_taker(wallet, backend, config)
        try:
            remaining_services = [
                item for item in RING_MAKER_MNEMONICS if item != service
            ]
            remaining_nicks = frozenset(
                await asyncio.gather(
                    *(
                        wait_for_maker_nick(item.replace("lnd-", "ring-", 1))
                        for item in remaining_services
                    )
                )
            )
            await replenish_ring_maker_wallets_for_buyout(
                probe,
                remaining_nicks,
                target_config,
                destination,
                receive_index=receive_index,
                receive_index_overrides=receive_index_overrides,
                excluded_service=service,
            )
            assert await wait_for_eligible_offer_nicks(probe, 2) == remaining_nicks
            # Started only now, after the replenishment blocks, the ordinary
            # maker offers its confirmed balance, including earlier change.
            await asyncio.to_thread(compose, "up", "-d", "--no-deps", "ring-maker4")
            maker4_nick = await wait_for_maker_nick("ring-maker4", previous=maker4_nick)
            eligible = await wait_for_eligible_offer_nicks(probe, 3)
        finally:
            await probe.stop(close_wallet=False)
        assert remaining_nicks < eligible
        assert maker4_nick in eligible
        input_utxo = await fund_buyout_taker_input(wallet)
    except BaseException:
        await wallet.close()
        raise
    return RingMakerBuyer(
        context=BuyerContext(
            name=name,
            service=service,
            mnemonic=mnemonic,
            wallet=wallet,
            backend=backend,
            config=config,
            available_podles={podle.commitment: podle},
            eligible_maker_nicks=eligible,
            input_utxo=input_utxo,
        ),
        podle=podle,
        maker_nick=maker_nick,
        maker4_nick=maker4_nick,
    )


async def assert_adjacent_buyout_refused(
    data_dir: Path,
    node_service: dict[str, str],
    record: Any,
    ring_txid: str,
    buyer_context: BuyerContext,
    edge: Any,
) -> None:
    """The node's single buyer journal refuses its second edge of one ring.

    Both payouts would return to the same source mixdepth, where a later spend
    could merge them and tie both ring edges to one owner on chain.
    """
    participant_node = {
        participant.participant_key: participant.node_id
        for participant in record.coordinator_participants
    }
    buyer_node = {service: node for node, service in node_service.items()}[
        buyer_context.service
    ]
    opener_node = participant_node[edge.opener_key]
    acceptor_node = participant_node[edge.acceptor_key]
    assert buyer_node in {opener_node, acceptor_node}
    counterparty_node = acceptor_node if buyer_node == opener_node else opener_node
    settings = buyout_settings(
        buyer_context.service,
        buyer_node,
        counterparty_node,
        node_buyout_journal(data_dir, buyer_context.service, "buyer"),
        await buyer_context.wallet.get_new_address_verified(BUYOUT_SOURCE_MIXDEPTH),
        mixdepth=BUYOUT_SOURCE_MIXDEPTH,
        wallet_fingerprint=get_mnemonic_fingerprint(buyer_context.mnemonic),
    )
    async with BuyoutRuntime(settings) as runtime:
        before = [(item.session_id, item.state) for item in runtime.store.list()]
        assert before, "the adjacent edge must be refused by the journal that bought"
        with pytest.raises(ConflictError, match="same funding transaction"):
            await runtime.prepare(
                counterparty_node, [Outpoint(txid=ring_txid, vout=edge.output_index)]
            )
        after = [(item.session_id, item.state) for item in runtime.store.list()]
    assert after == before


async def run_channel_input_buyout(
    data_dir: Path,
    node_service: dict[str, str],
    record: Any,
    ring_txid: str,
    buyer_context: BuyerContext,
    edge: Any,
    *,
    settlement: Literal["cooperative", "split"],
    spent_output_indices: frozenset[int],
    settlement_route: tuple[SettlementRouteChannel, ...] = (),
) -> PurchasedPoDLE:
    """Spend one selected ring channel with a real ordinary CoinJoin and buyout."""
    wallet = buyer_context.wallet
    backend = buyer_context.backend
    config = buyer_context.config
    manifest = record.manifest
    assert manifest is not None
    assert edge.output_index in spent_output_indices
    participant_node = {
        participant.participant_key: participant.node_id
        for participant in record.coordinator_participants
    }
    buyer_service = buyer_context.service
    service_node = {service: node for node, service in node_service.items()}
    buyer_node = service_node[buyer_service]
    opener_node = participant_node[edge.opener_key]
    acceptor_node = participant_node[edge.acceptor_key]
    assert buyer_node in {opener_node, acceptor_node}
    counterparty_node = acceptor_node if buyer_node == opener_node else opener_node
    counterparty_service = node_service[counterparty_node]
    channel_point = f"{ring_txid}:{edge.output_index}"
    # BuyoutStore creates its own journal directory, but not missing ancestors.
    # Do this before any route rebalance or mining for the buyout.
    (data_dir / buyer_context.name).mkdir(mode=0o700, exist_ok=True)
    buyer_journal = node_buyout_journal(data_dir, buyer_service, "buyer")
    counterparty_journal = node_buyout_journal(
        data_dir, counterparty_service, "counterparty"
    )

    if settlement == "cooperative":
        if not settlement_route_is_contiguous(
            settlement_route, buyer_service, counterparty_service
        ):
            raise RuntimeError(
                "cooperative settlement does not have one directed helper route"
            )
        route_availability: list[int] = []
        for route_channel in settlement_route:
            state = channel_state(
                await bootstrap_lncli(route_channel.source, "listchannels"),
                route_channel.channel_point,
                route_channel.source,
            )
            destination_info = await bootstrap_lncli(
                route_channel.destination, "getinfo"
            )
            if state.get("remote_pubkey") != destination_info.get("identity_pubkey"):
                raise RuntimeError(
                    "settlement helper channel has an unexpected endpoint"
                )
            reserve, balance = channel_reserve_and_balance(state)
            route_availability.append(balance - reserve)
        claim_ceiling = min(route_availability) - 10_000
        if claim_ceiling <= config.channel_ring.fundee_reserve:
            raise RuntimeError(
                "settlement helper route lacks reserve-safe directional liquidity"
            )
        pushed = await local_balance(counterparty_service, channel_point)
        if pushed > claim_ceiling:
            await pay_over_edge(
                counterparty_service,
                buyer_service,
                channel_point,
                pushed - claim_ceiling,
                f"{buyer_context.name}-buyout-rebalance",
            )
        await wait_for_local_balance(counterparty_service, channel_point, claim_ceiling)

    mining_address = wallet.get_receive_address(4, 0)
    # Buyout inputs stay in the LN node's source mixdepth; only the equal-value
    # CoinJoin output advances to the next mixdepth. Both wallet and maker
    # inputs must satisfy the configured taker_utxo_age.
    await mine_and_sync(config.taker_utxo_age + 1, mining_address)
    await wallet.sync_all()
    eligible_input = [
        utxo
        for utxo in await wallet.get_utxos(BUYOUT_SOURCE_MIXDEPTH)
        if utxo.outpoint == buyer_context.input_utxo
    ]
    if (
        len(eligible_input) != 1
        or eligible_input[0].confirmations < config.taker_utxo_age
        or eligible_input[0].value <= BUYOUT_CJ_AMOUNT
        or eligible_input[0].frozen
        or eligible_input[0].is_fidelity_bond
        or not bytes.fromhex(eligible_input[0].scriptpubkey).startswith(b"\x51\x20")
    ):
        raise RuntimeError(
            "buyer lacks the reserved, mature P2TR input for this buyout"
        )

    counterparty_payout = str(
        (await asyncio.to_thread(lncli, counterparty_service, "newaddress", "p2tr"))[
            "address"
        ]
    )
    buyout_taker: Taker | None = None
    consumed_podle: PurchasedPoDLE | None = None
    async with AsyncExitStack() as runtimes:
        counterparty_runtime = await runtimes.enter_async_context(
            BuyoutRuntime(
                buyout_settings(
                    counterparty_service,
                    counterparty_node,
                    buyer_node,
                    counterparty_journal,
                    counterparty_payout,
                    mixdepth=0,
                ),
                counterparty=True,
            )
        )
        buyer_runtime = await runtimes.enter_async_context(
            BuyoutRuntime(
                buyout_settings(
                    buyer_service,
                    buyer_node,
                    counterparty_node,
                    buyer_journal,
                    await wallet.get_new_address_verified(BUYOUT_SOURCE_MIXDEPTH),
                    mixdepth=BUYOUT_SOURCE_MIXDEPTH,
                    wallet_fingerprint=get_mnemonic_fingerprint(buyer_context.mnemonic),
                )
            )
        )
        assert buyer_runtime.runtime_binding["lnd_identity"] == buyer_node
        assert buyer_runtime.runtime_binding["mixdepth"] == BUYOUT_SOURCE_MIXDEPTH
        assert counterparty_runtime.runtime_binding["lnd_identity"] == counterparty_node
        assert buyer_runtime.peer._payment_route_hints == ()

        session_id = await buyer_runtime.prepare(
            counterparty_node, [Outpoint(txid=ring_txid, vout=edge.output_index)]
        )
        buyout = ChannelBuyout(buyer_runtime.buyer, session_id)
        assert buyout.total_value == edge.capacity
        assert buyout.inputs == [
            {
                "txid": ring_txid,
                "vout": edge.output_index,
                "value": edge.capacity,
                "scriptpubkey": edge.script_pubkey,
            }
        ]

        try:
            buyout_config = config.model_copy(
                update={
                    "channel_ring": config.channel_ring.model_copy(
                        update={"enabled": False}
                    ),
                    "external_podle_mode": "only",
                }
            )
            buyout_taker = await make_taker(wallet, backend, buyout_config)
            external_before = external_podle_commitments(buyout_taker.podle_manager)
            used_before = frozenset(buyout_taker.podle_manager.used_commitments)
            if external_before != frozenset(buyer_context.available_podles):
                raise RuntimeError(
                    "buyer did not load the expected external credential set"
                )
            if buyout_taker.podle_manager.external_count() != len(external_before):
                raise RuntimeError(
                    "buyer external credential count does not match its pool"
                )
            # Makers withdraw spent offers before the ring confirms, then rebuild
            # them on their periodic wallet rescan. Wait for usable liquidity.
            deadline = asyncio.get_running_loop().time() + 720
            while True:
                offers = await buyout_taker.directory_client.fetch_orderbook(
                    max_wait=10, min_wait=2, quiet_period=1
                )
                ready = {
                    offer.counterparty
                    for offer in offers
                    if offer.counterparty in buyer_context.eligible_maker_nicks
                    and offer.minsize <= BUYOUT_CJ_AMOUNT <= offer.maxsize
                }
                if ready == buyer_context.eligible_maker_nicks:
                    # do_coinjoin fetches again. Respect the maker's 10-second
                    # per-peer orderbook limit for that request as well.
                    await asyncio.sleep(15)
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        "eligible makers did not reoffer confirmed buyout inputs"
                    )
                await asyncio.sleep(15)
            parent_txid = await buyout_taker.do_coinjoin(
                amount=BUYOUT_CJ_AMOUNT,
                destination=await wallet.get_new_address_verified(
                    BUYOUT_SOURCE_MIXDEPTH + 1
                ),
                mixdepth=BUYOUT_SOURCE_MIXDEPTH,
                counterparty_count=3,
                buyout=buyout,
                input_utxos=[buyer_context.input_utxo],
            )
            assert parent_txid is not None
            external_after = external_podle_commitments(buyout_taker.podle_manager)
            used_after = frozenset(buyout_taker.podle_manager.used_commitments)
            consumed = external_before - external_after
            newly_used = used_after - used_before
            if len(consumed) != 1 or consumed != newly_used:
                raise RuntimeError(
                    "buyer did not consume exactly one external credential"
                )
            consumed_podle = buyer_context.available_podles.get(next(iter(consumed)))
            if consumed_podle is None:
                raise RuntimeError("buyer consumed an unexpected external credential")
        finally:
            if buyout_taker is not None:
                await buyout_taker.stop(close_wallet=False)

        parent = parse_transaction(
            str(await bitcoin_rpc("getrawtransaction", [parent_txid]))
        )
        assert buyer_context.input_utxo in {
            f"{tx_input.txid}:{tx_input.vout}" for tx_input in parent.inputs
        }
        assert (ring_txid, edge.output_index) in {
            (tx_input.txid, tx_input.vout) for tx_input in parent.inputs
        }
        assert (
            consumed_podle.backing_txid,
            consumed_podle.backing_vout,
        ) not in {(tx_input.txid, tx_input.vout) for tx_input in parent.inputs}
        assert (
            await bitcoin_rpc(
                "gettxout",
                [
                    consumed_podle.backing_txid,
                    consumed_podle.backing_vout,
                ],
            )
            is not None
        )
        # One channel input, three maker inputs and the ordinary P2TR wallet
        # input that authenticates the round; the channel never stands alone.
        assert len(parent.inputs) >= len(buyout.inputs) + 4
        assert sum(output.value == BUYOUT_CJ_AMOUNT for output in parent.outputs) == 4
        escrow_script = buyout.terms.escrow.output_script()
        escrow_index = next(
            index
            for index, output in enumerate(parent.outputs)
            if output.script == escrow_script
        )

        depth = buyout.terms.proposal.buyer_settlement_depth
        counterparty_endpoint = onion_endpoint(counterparty_service)
        await mine_and_sync(depth, mining_address)
        # The purchased channel is gone, so the custom-message transport needs
        # a direct peer connection independent of Lightning payment routing.
        await ensure_peer_connected(
            buyer_service, counterparty_node, counterparty_endpoint
        )
        await wait_for_spent_channel_removal(
            (buyer_service, counterparty_service), channel_point
        )
        for route_channel in settlement_route:
            await assert_active_channel(
                (route_channel.source, route_channel.destination),
                route_channel.channel_point,
            )
        await ensure_peer_connected(
            buyer_service, counterparty_node, counterparty_endpoint
        )
        if settlement == "cooperative":
            await wait_for_buyout_state(buyer_runtime, session_id, {"SETTLED"})
            payment = await settlement_payment(
                buyer_service, buyout.terms.acceptance.payment_hash
            )
            assert str(payment.get("status", "")).upper() in {"SUCCEEDED", "2"}
            htlcs = payment.get("htlcs")
            if (
                not isinstance(htlcs, list)
                or not htlcs
                or not isinstance(htlcs[-1], dict)
            ):
                raise RuntimeError(
                    "LND did not retain the cooperative settlement route"
                )
            route = htlcs[-1].get("route")
            if not isinstance(route, dict):
                raise RuntimeError(
                    "LND returned an invalid cooperative settlement route"
                )
            expected_channel_ids = tuple(
                await asyncio.gather(
                    *(
                        channel_short_id(item.source, item.channel_point)
                        for item in settlement_route
                    )
                )
            )
            destination_infos = await asyncio.gather(
                *(
                    bootstrap_lncli(item.destination, "getinfo")
                    for item in settlement_route
                )
            )
            expected_hops = tuple(
                str(info["identity_pubkey"]) for info in destination_infos
            )
            if (
                route_hop_channel_ids(route) != expected_channel_ids
                or route_hop_pubkeys(route) != expected_hops
            ):
                raise RuntimeError(
                    "cooperative settlement used an unexpected helper route"
                )
            await ensure_peer_connected(
                buyer_service, counterparty_node, counterparty_endpoint
            )
            await wait_for_buyout_state(buyer_runtime, session_id, {"SPEND_BROADCAST"})
        else:
            await wait_for_buyout_state(buyer_runtime, session_id, {"PAYMENT_FAILED"})
            payment = await settlement_payment(
                buyer_service, buyout.terms.acceptance.payment_hash
            )
            assert str(payment.get("status", "")).upper() in {"FAILED", "3"}
            # With the purchased channel gone, LND reports INSUFFICIENT_BALANCE
            # (before pathfinding, no HTLC) when no remaining local channel can
            # carry the amount, and NO_ROUTE otherwise. Settlement treats both as
            # a failed payment; what matters is that nothing was delivered.
            reason = payment.get("failure_reason")
            assert reason in {
                "FAILURE_REASON_NO_ROUTE",
                "FAILURE_REASON_INSUFFICIENT_BALANCE",
            }
            htlcs = payment.get("htlcs")
            if not isinstance(htlcs, list) or len(htlcs) > 1:
                raise RuntimeError("LND recorded an unexpected settlement attempt")
            if reason == "FAILURE_REASON_INSUFFICIENT_BALANCE" and htlcs:
                raise RuntimeError("an insufficient-balance failure dispatched an HTLC")
            if any(
                str(item.get("status", "")).upper() in {"SUCCEEDED", "1"}
                for item in htlcs
                if isinstance(item, dict)
            ):
                raise RuntimeError("a no-route settlement attempt succeeded")
            assert buyer_runtime.store.get(session_id).data["payment_failed"] is True
            assert buyout.terms.proposal.csv_delay >= 144
            await mine_and_sync(
                max(0, buyout.terms.proposal.csv_delay - depth), mining_address
            )
            await wait_for_buyout_state(buyer_runtime, session_id, {"SPEND_BROADCAST"})
        spends = cast(
            list[dict[str, Any]], buyer_runtime.store.get(session_id).data["spends"]
        )
        assert spends[-1]["kind"] == settlement
        spend = parse_transaction(str(spends[-1]["raw"]))
        assert [(tx_input.txid, tx_input.vout) for tx_input in spend.inputs] == [
            (parent_txid, escrow_index)
        ]
        if settlement == "split":
            expected_buyer = (
                parent.outputs[escrow_index].value
                - buyout.terms.counterparty_split_sat
                - buyout.terms.proposal.split_fee
            )
            assert len(spend.outputs) == 2
            assert any(
                output.script == bytes.fromhex(buyout.terms.proposal.split_script_B)
                and output.value == expected_buyer
                for output in spend.outputs
            )
            assert any(
                output.script == bytes.fromhex(buyout.terms.acceptance.split_script_C)
                and output.value == buyout.terms.counterparty_split_sat
                for output in spend.outputs
            )
        spend_txid = get_txid(str(spends[-1]["raw"]))

        await mine_and_sync(1, mining_address)
        confirmed = await bitcoin_rpc("getrawtransaction", [spend_txid, True])
        assert int(confirmed["confirmations"]) >= 1
        await mine_and_sync(depth, mining_address)
        await wait_for_buyout_state(buyer_runtime, session_id, {"COMPLETED"})

        assert await bitcoin_rpc("gettxout", [parent_txid, escrow_index]) is None
        assert await bitcoin_rpc("gettxout", [ring_txid, edge.output_index]) is None
        for other in manifest.edges:
            if other.output_index in spent_output_indices:
                assert (
                    await bitcoin_rpc("gettxout", [ring_txid, other.output_index])
                    is None
                )
            else:
                assert (
                    await bitcoin_rpc("gettxout", [ring_txid, other.output_index])
                    is not None
                )
    if consumed_podle is None:
        raise RuntimeError("buyer did not identify its consumed external credential")
    return consumed_podle


@pytest.mark.asyncio
@pytest.mark.timeout(3600)
async def test_rejected_revision_then_complete_mixed_five_party_ring() -> None:
    if os.getenv("RING_E2E") != "1":
        pytest.skip("start the ring-e2e Compose profile and set RING_E2E=1")

    # The dedicated runner retains this directory with the signed chain. Keep
    # market receipts, buyer wallets and buyout journals here as well.
    operations_dir = DATA_DIR / "host-operations"
    operations_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    assert 100_000 <= BUYOUT_CJ_AMOUNT <= 1_000_000

    credential_paths = [
        path
        for service in LND_SERVICES
        for path in (lnd_path(service, "tls.cert"), lnd_path(service, "admin.macaroon"))
    ]
    await asyncio.gather(*(wait_for_file(path) for path in credential_paths))

    # Re-running against retained makers must retain the taker's spent PoDLE
    # claims. Ring journals remain per attempt so assertions cannot mistake a
    # previous retired round for the one under test.
    taker_data_dir = DATA_DIR / "host-taker"
    if os.getenv("RING_E2E_NO_RESET") == "1":
        assert_retained_taker_podle_state(taker_data_dir)

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
        data_dir=taker_data_dir,
    )
    # Signed ring evidence must stay in the preserved host fixture when the
    # E2E fails; pytest's tmp_path is not part of the retained stack.
    config = taker_config(taker_data_dir)
    taker: Taker | None = None
    restart_taker: Taker | None = None
    maker_buyer_wallets: list[WalletService] = []
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
        # Keep this preflight regression on the original all-ring selection so
        # the rejected revision reaches durable planning before it is retired.
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

        # Buy the taker's credentials and maker1's rented bond over temporary
        # direct, private channels before the target ring exists.
        await fund_bootstrap_opener("lnd-taker")
        await fund_bootstrap_opener("lnd-maker1")
        podle_bootstrap = await open_test_channel(
            "lnd-taker", "lnd-maker4", destination, private=True
        )
        bond_bootstrap = await open_test_channel(
            "lnd-maker1", "lnd-maker4", destination, private=True
        )
        bootstrap_channels = {
            podle_bootstrap: ("lnd-taker", "lnd-maker4"),
            bond_bootstrap: ("lnd-maker1", "lnd-maker4"),
        }
        assert len(bootstrap_channels) == 2
        assert FUNDING_CONFIRMATIONS >= config.taker_utxo_age
        podle_a = await purchase_external_podle(
            rpc_url=RPC_URL,
            directory_server=DIRECTORY,
            lncli=lncli,
            payer_service="lnd-taker",
            seller_service="lnd-maker4",
            buyer_data_dir=taker_data_dir,
            work_dir=operations_dir / "external-podle-market-a",
            socks_port=TOR_SOCKS_PORT,
            include_route_hints=False,
        )
        podle_second = await purchase_external_podle(
            rpc_url=RPC_URL,
            directory_server=DIRECTORY,
            lncli=lncli,
            payer_service="lnd-taker",
            seller_service="lnd-maker4",
            buyer_data_dir=taker_data_dir,
            work_dir=operations_dir / "external-podle-market-second",
            socks_port=TOR_SOCKS_PORT,
            include_route_hints=False,
        )
        podle_third = await purchase_external_podle(
            rpc_url=RPC_URL,
            directory_server=DIRECTORY,
            lncli=lncli,
            payer_service="lnd-taker",
            seller_service="lnd-maker4",
            buyer_data_dir=taker_data_dir,
            work_dir=operations_dir / "external-podle-market-third",
            socks_port=TOR_SOCKS_PORT,
            include_route_hints=False,
        )
        rented_bond = await purchase_rented_bond(
            rpc_url=RPC_URL,
            directory_server=DIRECTORY,
            lncli=lncli,
            payer_service="lnd-maker1",
            seller_service="lnd-maker4",
            renter_wallet_fingerprint=get_mnemonic_fingerprint(MAKER1_MNEMONIC),
            work_dir=operations_dir / "fidelity-bond-market",
            socks_port=TOR_SOCKS_PORT,
            include_route_hints=False,
        )
        assert rented_bond.bond != podle_a.seller_bond
        assert rented_bond.bond != podle_second.seller_bond
        await install_rented_bond_registry(rented_bond.registry_path)
        await recreate_maker1_with_rented_bond()

        taker = await make_taker(wallet, backend, config)
        await wait_for_rented_bond_offer(taker, rented_bond)
        await taker.stop(close_wallet=False)
        taker = None
        await close_bootstrap_channels(bootstrap_channels, destination)
        await assert_no_pending_channels()

        # The target round has no local PoDLE fallback. Its selected credential
        # is asserted below, while the other two pre-imported credentials remain
        # for the public and private channel-input buyouts.
        target_config = config.model_copy(update={"external_podle_mode": "only"})
        taker = await make_taker(wallet, backend, target_config)
        assert taker.podle_manager.external_count() == 3
        destination = wallet.get_receive_address(1, 1)
        txid = await taker.do_coinjoin(
            amount=BUYOUT_CJ_AMOUNT,
            destination=destination,
            mixdepth=0,
            counterparty_count=4,
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
        assert len(manifest.equal_output_indices) == 5
        assert len(manifest.outputs) == 10

        raw_tx = str(await bitcoin_rpc("getrawtransaction", [txid]))
        parsed = parse_transaction(raw_tx)
        assert len(parsed.outputs) == 10
        assert sum(output.value == BUYOUT_CJ_AMOUNT for output in parsed.outputs) == 5
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
        assert record.coordinator_tx_fee is not None
        assert actual_fee == record.coordinator_tx_fee + maker_txfees

        await bitcoin_rpc("generatetoaddress", [3, destination])
        channels = await wait_for_ring_channels()
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

        maker4_channels = (
            await asyncio.to_thread(lncli, "lnd-maker4", "listchannels")
        )["channels"]
        assert not maker4_channels
        maker4_logs = await asyncio.to_thread(
            compose, "logs", "--no-color", "ring-maker4"
        )
        assert "private channel ring" not in maker4_logs.stdout.lower()

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

        participant_node = {
            participant.participant_key: participant.node_id
            for participant in record.coordinator_participants
        }
        ring_nodes = set(participant_node.values())
        assert ring_nodes == {
            node
            for node, service in node_service.items()
            if service in RING_LND_SERVICES
        }
        ordinary_sessions = taker._session.ordinary_maker_sessions
        assert len(ordinary_sessions) == 1
        maker4_nick, maker4_session = next(iter(ordinary_sessions.items()))
        ring_maker_nicks = frozenset(taker._session.ring_maker_sessions)
        assert len(ring_maker_nicks) == 3
        ordinary_output_indices = (
            set(range(len(parsed.outputs)))
            - set(manifest.equal_output_indices)
            - {edge.output_index for edge in manifest.edges}
        )
        assert len(ordinary_output_indices) == 1
        ordinary_output = parsed.outputs[ordinary_output_indices.pop()]
        assert ordinary_output.script == address_to_scriptpubkey_for_network(
            maker4_session.change_address, "regtest"
        )

        purchased_podles = (podle_a, podle_second, podle_third)
        used_podles = [
            purchased
            for purchased in purchased_podles
            if purchased.commitment in taker.podle_manager.used_commitments
        ]
        assert len(used_podles) == 1
        target_podle = used_podles[0]
        buyout_podles = {
            purchased.commitment: purchased
            for purchased in purchased_podles
            if purchased != target_podle
        }
        assert len(buyout_podles) == 2
        assert rented_bond.bond != target_podle.seller_bond
        assert (
            await bitcoin_rpc(
                "gettxout", [target_podle.backing_txid, target_podle.backing_vout]
            )
            is not None
        )
        assert taker.podle_manager.external_count() == 2

        assert taker._channel_ring_store is not None
        assert taker._channel_ring_nodes is not None
        await reconcile_taker_ring_records(
            taker._channel_ring_store, taker._channel_ring_nodes, backend
        )
        assert coordinator._record().state is RingLifecycleState.CONFIRMED_OPEN

        await taker.stop(close_wallet=False)
        taker = None
        restart_taker = await make_taker(wallet, backend, target_config)
        stored = restart_taker._channel_ring_store
        assert stored is not None
        successful = [
            item for item in stored.load_all().records if item.manifest is not None
        ]
        assert len(successful) == 1
        assert successful[0].state is RingLifecycleState.CONFIRMED_OPEN
        assert successful[0].final_tx == raw_tx

        # Subsequent ordinary CoinJoins spend three distinct target channels.
        # Stop only the ordinary maker bot; its LND remains the public router.
        await asyncio.to_thread(compose, "stop", "ring-maker4")
        await wait_for_offer_absence(restart_taker, maker4_nick)
        await replenish_ring_maker_wallets_for_buyout(
            restart_taker, ring_maker_nicks, target_config, destination
        )
        public_input = await fund_buyout_taker_input(wallet)
        await restart_taker.stop(close_wallet=False)
        restart_taker = None

        service_node = {service: node for node, service in node_service.items()}
        taker_node = service_node["lnd-taker"]
        taker_outgoing = next(
            edge
            for edge in manifest.edges
            if participant_node[edge.opener_key] == taker_node
        )
        taker_incoming = next(
            edge
            for edge in manifest.edges
            if participant_node[edge.acceptor_key] == taker_node
        )
        taker_predecessor = participant_node[taker_incoming.opener_key]
        private_service = node_service[taker_predecessor]
        if private_service not in RING_MAKER_MNEMONICS:
            raise RuntimeError("private settlement buyer is not a ring maker")
        split_candidates = [
            edge
            for edge in manifest.edges
            if participant_node[edge.opener_key] not in {taker_node, taker_predecessor}
        ]
        assert len(split_candidates) == 2
        split_edge = split_candidates[0]
        split_service = node_service[participant_node[split_edge.opener_key]]
        assert split_service in RING_MAKER_MNEMONICS
        assert split_service != private_service
        selected_outpoints = {
            f"{txid}:{edge.output_index}"
            for edge in (taker_outgoing, taker_incoming, split_edge)
        }
        assert len(selected_outpoints) == 3

        # The public case has no sender hints: LND discovers taker -> maker4 ->
        # target acceptor from its public graph after that bought edge is gone.
        public_acceptor = node_service[participant_node[taker_outgoing.acceptor_key]]
        await fund_bootstrap_opener("lnd-taker")
        await fund_bootstrap_opener("lnd-taker")
        await fund_bootstrap_opener(public_acceptor)
        public_taker_router = await open_test_channel(
            "lnd-taker", "lnd-maker4", destination, private=False
        )
        public_router_acceptor = await open_test_channel(
            public_acceptor,
            "lnd-maker4",
            destination,
            private=False,
            push_amount=500_000,
        )
        assert public_taker_router != public_router_acceptor
        await mature_public_links_and_wait_for_graph(
            (public_taker_router, public_router_acceptor),
            {
                frozenset((taker_node, service_node["lnd-maker4"])),
                frozenset(
                    (
                        service_node["lnd-maker4"],
                        participant_node[taker_outgoing.acceptor_key],
                    )
                ),
            },
            node_service,
            destination,
        )

        taker_public_context = BuyerContext(
            name="public-route",
            service="lnd-taker",
            mnemonic=TAKER_MNEMONIC,
            wallet=wallet,
            backend=backend,
            config=target_config,
            available_podles=buyout_podles,
            eligible_maker_nicks=ring_maker_nicks,
            input_utxo=public_input,
        )
        public_consumed_podle = await run_channel_input_buyout(
            operations_dir,
            node_service,
            record,
            txid,
            taker_public_context,
            taker_outgoing,
            settlement="cooperative",
            spent_output_indices=frozenset({taker_outgoing.output_index}),
            settlement_route=(
                SettlementRouteChannel(public_taker_router, "lnd-taker", "lnd-maker4"),
                SettlementRouteChannel(
                    public_router_acceptor, "lnd-maker4", public_acceptor
                ),
            ),
        )
        assert public_consumed_podle.commitment in buyout_podles

        # One buyer journal per LN node: the same journal refuses the taker's
        # incoming edge of this ring before any message or credential is used.
        await assert_adjacent_buyout_refused(
            operations_dir,
            node_service,
            record,
            txid,
            taker_public_context,
            taker_incoming,
        )

        # Retire the public helpers. They could otherwise form an alternate
        # graph path for the private and no-route cases below.
        await close_bootstrap_channels(
            {
                public_taker_router: ("lnd-taker", "lnd-maker4"),
                public_router_acceptor: (public_acceptor, "lnd-maker4"),
            },
            destination,
        )
        await assert_no_pending_channels()

        # The private-peer case: the taker's predecessor buys the incoming
        # edge and pays the taker over a second, direct private channel. The
        # taker is only its counterparty, so no node buys two edges of a ring.
        private_buyer = await prepare_ring_maker_buyer(
            operations_dir,
            private_service,
            f"{private_service.removeprefix('lnd-')}-private-peer-route",
            target_config,
            destination,
            receive_index=2,
            maker4_nick=maker4_nick,
        )
        maker_buyer_wallets.append(private_buyer.context.wallet)
        await fund_bootstrap_opener(private_service)
        private_peer_channel = await open_test_channel(
            private_service, "lnd-taker", destination, private=True
        )
        assert private_peer_channel != f"{txid}:{taker_incoming.output_index}"
        private_consumed_podle = await run_channel_input_buyout(
            operations_dir,
            node_service,
            record,
            txid,
            private_buyer.context,
            taker_incoming,
            settlement="cooperative",
            spent_output_indices=frozenset(
                {taker_outgoing.output_index, taker_incoming.output_index}
            ),
            settlement_route=(
                SettlementRouteChannel(
                    private_peer_channel, private_service, "lnd-taker"
                ),
            ),
        )
        assert private_consumed_podle.commitment == private_buyer.podle.commitment
        await close_bootstrap_channels(
            {private_peer_channel: (private_service, "lnd-taker")}, destination
        )
        await assert_no_pending_channels()
        # The maker keeps running after its own ring edge was bought out.
        private_maker = private_service.replace("lnd-", "ring-", 1)
        await asyncio.to_thread(compose, "up", "-d", "--no-deps", private_maker)
        await wait_for_maker_nick(private_maker, previous=private_buyer.maker_nick)

        # The manifest-selected non-incident maker buys its own outgoing edge
        # with no route left for the payment, so the escrow takes the CSV split.
        # The ordinary maker would offer only its confirmed balance from before
        # the private-peer CoinJoin until its next periodic rescan, so restart
        # its bot after the helper has mined that change.
        await asyncio.to_thread(compose, "stop", "ring-maker4")
        split_buyer = await prepare_ring_maker_buyer(
            operations_dir,
            split_service,
            f"{split_service.removeprefix('lnd-')}-no-route",
            target_config,
            destination,
            receive_index=3,
            # The private buyer's own wallet handed out its next receive
            # addresses for the buyout input and payout.
            receive_index_overrides={
                private_service: private_buyer.context.wallet.get_next_address_index(
                    BUYOUT_SOURCE_MIXDEPTH, 0
                )
            },
            maker4_nick=private_buyer.maker4_nick,
        )
        maker_buyer_wallets.append(split_buyer.context.wallet)
        assert len(split_buyer.context.eligible_maker_nicks) == 3
        split_consumed_podle = await run_channel_input_buyout(
            operations_dir,
            node_service,
            record,
            txid,
            split_buyer.context,
            split_edge,
            settlement="split",
            spent_output_indices=frozenset(
                {
                    taker_outgoing.output_index,
                    taker_incoming.output_index,
                    split_edge.output_index,
                }
            ),
        )
        assert split_consumed_podle.commitment == split_buyer.podle.commitment
    finally:
        if restart_taker is not None:
            await restart_taker.stop(close_wallet=False)
        if taker is not None:
            await taker.stop(close_wallet=False)
        await asyncio.gather(
            *(item.close() for item in lnd_backends), return_exceptions=True
        )
        for maker_buyer_wallet in maker_buyer_wallets:
            await maker_buyer_wallet.close()
        await wallet.close()
