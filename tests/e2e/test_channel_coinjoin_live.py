"""
Full maker<->taker CoinJoin where the maker's change opens a real Lightning
channel (channel-as-change), live against bitcoind + the forked LDK Server.

An in-process maker (configured with channel_change + an LDK Server + a channel
peer) and an in-process taker run a real CoinJoin through a directory server.
The maker emits its change as the funding output of an externally-funded
Lightning channel; after the round confirms, that channel opens on the maker's
LDK node. This is the end-to-end validation of the taker-driven channel-as-change
design (no maker-to-maker coordination).

Run with::

    docker compose --profile swap up -d
    pytest -m swap tests/e2e/test_channel_coinjoin_live.py --fail-on-skip
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from .conftest import is_port_open

pytestmark = [pytest.mark.swap, pytest.mark.docker]

_RPC_URL = "http://127.0.0.1:18443"
_RPC_USER = "test"
_RPC_PASSWORD = "test"
DIRECTORY_PORT = 5224  # avoid clashing with a docker directory on 5222

MAKER_MNEMONIC = (
    "avoid whisper mesh corn already blur sudden fine planet chicken hover sniff"
)
TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)


async def _rpc(method, params=None, wallet="miner"):
    import httpx

    url = _RPC_URL if wallet is None else f"{_RPC_URL}/wallet/{wallet}"
    async with httpx.AsyncClient(
        auth=(_RPC_USER, _RPC_PASSWORD), timeout=30.0
    ) as client:
        resp = await client.post(
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
            raise RuntimeError(f"{method}: {data['error']}")
        return data["result"]


async def _mine(blocks):
    addr = await _rpc("getnewaddress")
    await _rpc("generatetoaddress", [blocks, addr])


def _cli(service, config, *args):
    out = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            service,
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
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"_raw": out.stdout + out.stderr}


def _ldk_creds(container, network="regtest"):
    import tempfile

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


def _start_directory():
    repo_root = Path(__file__).parent.parent.parent
    ds_path = repo_root / "directory_server"
    proc = subprocess.Popen(
        [sys.executable, "-m", "directory_server.main"],
        cwd=ds_path,
        env={
            "PYTHONPATH": f"{ds_path / 'src'}:{repo_root / 'jmcore' / 'src'}",
            "NETWORK_CONFIG__NETWORK": "testnet",
            "DIRECTORY_SERVER__PORT": str(DIRECTORY_PORT),
            "DIRECTORY_SERVER__HOST": "127.0.0.1",
            "LOGGING__LEVEL": "INFO",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(2)
    if proc.poll() is not None:
        _, stderr = proc.communicate()
        raise RuntimeError(f"directory failed: {stderr.decode()[:500]}")
    return proc


@pytest.mark.timeout(360)
async def test_channel_as_change_coinjoin() -> None:
    if not (is_port_open("127.0.0.1", 18443) and is_port_open("127.0.0.1", 3536)):
        pytest.skip("swap profile (bitcoind + forked ldk-provider) not running")
    try:
        import httpx  # noqa: F401
    except ImportError:
        pytest.skip("httpx[http2] not installed")

    from jmcore.bitcoin import parse_transaction
    from jmcore.config import TorControlConfig
    from jmcore.models import NetworkType
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
    from jmwallet.wallet.service import WalletService
    from maker.bot import MakerBot
    from maker.config import MakerConfig, SwapProviderConfig
    from taker.config import TakerConfig
    from taker.taker import Taker

    api_key, cert = _ldk_creds("jm-ldk-provider")
    client_node = _cli("ldk-client", "/config/client.toml", "get-node-info")["node_id"]

    backend_cfg = {
        "rpc_url": _RPC_URL,
        "rpc_user": _RPC_USER,
        "rpc_password": _RPC_PASSWORD,
    }

    def make_backend():
        return DescriptorWalletBackend(
            rpc_url=_RPC_URL,
            rpc_user=_RPC_USER,
            rpc_password=_RPC_PASSWORD,
            wallet_name=f"cc_{os.getpid()}_{time.time_ns()}",
        )

    maker_backend = make_backend()
    taker_backend = make_backend()
    maker_wallet = WalletService(MAKER_MNEMONIC, maker_backend, "regtest", 5)
    taker_wallet = WalletService(TAKER_MNEMONIC, taker_backend, "regtest", 5)

    directory = None
    bot = None
    bot_task = None
    taker = None
    try:
        # Fund the LDK provider (anchor reserve), the maker and the taker.
        prov_addr = _cli("ldk-provider", "/config/provider.toml", "onchain-receive")[
            "address"
        ]
        await _rpc("sendtoaddress", [prov_addr, 0.02])

        await maker_wallet.setup_descriptor_wallet()
        await taker_wallet.setup_descriptor_wallet()
        maker_addr = maker_wallet.get_new_address(0)
        taker_addr = taker_wallet.get_new_address(0)
        await _rpc("sendtoaddress", [maker_addr, 0.30])
        await _rpc("sendtoaddress", [taker_addr, 0.50])
        await _mine(3)
        await maker_wallet.sync_all()
        await taker_wallet.sync_all()
        assert await maker_wallet.get_total_balance() >= 25_000_000
        assert await taker_wallet.get_total_balance() >= 25_000_000

        directory = _start_directory()

        maker_config = MakerConfig(
            mnemonic=MAKER_MNEMONIC,
            network=NetworkType.TESTNET,
            bitcoin_network=NetworkType.REGTEST,
            backend_type="descriptor_wallet",
            backend_config={**backend_cfg, "wallet_name": maker_backend.wallet_name},
            directory_servers=[f"127.0.0.1:{DIRECTORY_PORT}"],
            min_size=100_000,
            cj_fee_relative="0.0003",
            tx_fee_contribution=1_000,
            tor_control=TorControlConfig(enabled=False),
            swap_provider=SwapProviderConfig(
                channel_change=True,
                ldk_grpc_url="https://127.0.0.1:3536",
                ldk_api_key=api_key,
                ldk_tls_cert=cert,
                channel_peer=f"{client_node}@jm-ldk-client:9735",
            ),
        )
        bot = MakerBot(maker_wallet, maker_backend, maker_config)
        bot_task = asyncio.create_task(bot.start())
        await asyncio.sleep(12)  # let the maker connect + announce its offer

        taker_config = TakerConfig(
            mnemonic=TAKER_MNEMONIC,
            network=NetworkType.TESTNET,
            bitcoin_network=NetworkType.REGTEST,
            backend_type="descriptor_wallet",
            backend_config={**backend_cfg, "wallet_name": taker_backend.wallet_name},
            directory_servers=[f"127.0.0.1:{DIRECTORY_PORT}"],
            counterparty_count=1,
            minimum_makers=1,
            maker_timeout_sec=120,
            order_wait_time=30.0,
        )
        taker = Taker(taker_wallet, taker_backend, taker_config)
        await taker.start()

        dest = taker_wallet.get_new_address(1)
        txid = await taker.do_coinjoin(
            amount=20_000_000, destination=dest, mixdepth=0, counterparty_count=1
        )
        assert txid, "CoinJoin returned no txid"

        # The maker's change output is the channel funding output (a P2WSH).
        raw = await maker_backend.get_transaction(txid)
        assert raw is not None
        tx = parse_transaction(raw.raw if hasattr(raw, "raw") else raw)
        p2wsh_outputs = [
            o for o in tx.outputs if len(o.script) == 34 and o.script[0] == 0x00
        ]
        assert p2wsh_outputs, "no P2WSH (channel funding) output found in the CoinJoin"

        # Confirm the funding and assert the channel opens on the maker's LDK node.
        opened = False
        for _ in range(40):
            await _mine(1)
            channels = _cli("ldk-provider", "/config/provider.toml", "list-channels")
            raw_ch = json.dumps(channels).replace(" ", "")
            if '"is_channel_ready":true' in raw_ch or '"is_usable":true' in raw_ch:
                opened = True
                break
            await asyncio.sleep(2)
        assert opened, "channel-as-change channel did not open after the CoinJoin"
    finally:
        if taker is not None:
            await taker.stop()
        if bot is not None:
            await bot.stop()
        if bot_task is not None:
            bot_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bot_task
        if directory is not None:
            directory.terminate()
        await maker_wallet.close()
        await taker_wallet.close()
