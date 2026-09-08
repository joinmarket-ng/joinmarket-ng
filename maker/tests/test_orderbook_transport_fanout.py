"""Real-TCP regression coverage for multi-directory ``!orderbook`` fanout."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from bitcointx.core.key import CKey
from directory_server.server import DirectoryServer
from jmcore.config import TorControlConfig
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.directory_client import DirectoryClient, parse_fidelity_bond_proof
from jmcore.models import NetworkType, Offer, OfferType
from jmcore.network import ONION_HOSTID
from jmcore.nick_auth import NickAuthMode
from jmcore.protocol import COMMAND_PREFIX, MessageType
from jmcore.settings import DirectoryServerSettings

from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.fidelity import FidelityBondInfo

_HOST = "127.0.0.1"
_STARTUP_TIMEOUT = 5.0
_RESPONSE_TIMEOUT = 5.0
_TEARDOWN_TIMEOUT = 5.0
_REQUESTER_COUNT = 5


async def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    """Wait for a transport state transition without a timing-based delay."""
    deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Timed out waiting for {description}")
        await asyncio.sleep(0)


async def _wait_for_server_start(server: DirectoryServer, server_task: asyncio.Task[None]) -> int:
    """Return a dynamically bound directory port once its serving task is ready."""

    def server_is_ready() -> bool:
        if server_task.done():
            server_task.result()
        return server.server is not None and bool(server.server.sockets)

    await _wait_until(server_is_ready, "directory server startup")
    assert server.server is not None
    assert server.server.sockets is not None
    return int(server.server.sockets[0].getsockname()[1])


async def _receive_offer(client: DirectoryClient, maker_nick: str) -> dict[str, Any]:
    """Receive the offer addressed to ``client`` while skipping public fanout copies."""
    assert client.connection is not None
    deadline = asyncio.get_running_loop().time() + _RESPONSE_TIMEOUT
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Timed out waiting for offer on {client.host}:{client.port}")
        message = json.loads(
            (await asyncio.wait_for(client.connection.receive(), timeout=remaining)).decode("utf-8")
        )
        line = message.get("line", "")
        parts = line.split(COMMAND_PREFIX, 2)
        if (
            message.get("type") == MessageType.PRIVMSG.value
            and len(parts) == 3
            and parts[0] == maker_nick
            and parts[1] == client.nick
            and parts[2].split(maxsplit=1)[0] == OfferType.SW0_RELATIVE.value
        ):
            return message


def _unexpected_failures(results: Sequence[object]) -> list[BaseException]:
    """Ignore expected cancellation while retaining genuine teardown failures."""
    return [
        result
        for result in results
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
    ]


@dataclass
class Requester:
    """One identity connected to each distinct directory endpoint."""

    identity: NickIdentity
    clients: list[DirectoryClient]

    @property
    def nick(self) -> str:
        return self.identity.nick


class TransportFanoutHarness:
    """Own real TCP resources so every failure path can close them deterministically."""

    def __init__(
        self, directory_count: int, tmp_path: Path, requester_count: int = _REQUESTER_COUNT
    ) -> None:
        self.directory_count = directory_count
        self.requester_count = requester_count
        self.tmp_path = tmp_path
        self.servers: list[DirectoryServer] = []
        self.server_tasks: list[asyncio.Task[None]] = []
        self.requesters: list[Requester] = []
        self.bot: MakerBot | None = None

    async def start(self) -> None:
        self.servers = [
            DirectoryServer(
                DirectoryServerSettings(
                    host=_HOST,
                    port=0,
                    max_peers=self.requester_count + 1,
                    health_check_port=0,
                    nick_auth_mode=NickAuthMode.DISABLED,
                ),
                NetworkType.REGTEST,
                f"J5TransportDirectory{index}OOOO",
            )
            for index in range(self.directory_count)
        ]
        self.server_tasks = [asyncio.create_task(server.start()) for server in self.servers]
        ports = await asyncio.gather(
            *(
                _wait_for_server_start(server, task)
                for server, task in zip(self.servers, self.server_tasks, strict=True)
            )
        )
        endpoints = [f"{_HOST}:{port}" for port in ports]

        wallet = MagicMock(mixdepth_count=5, utxo_cache={})
        backend = MagicMock()
        backend.can_provide_neutrino_metadata.return_value = False
        config = MakerConfig(
            mnemonic="test " * 12,
            data_dir=self.tmp_path,
            directory_servers=endpoints,
            network=NetworkType.REGTEST,
            nick_auth_mode=NickAuthMode.DISABLED,
            tor_control=TorControlConfig(enabled=False),
        )
        self.bot = MakerBot(wallet=wallet, backend=backend, config=config)
        self.bot.current_offers = [
            Offer(
                counterparty=self.bot.nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=100_000,
                txfee=0,
                cjfee="0.0001",
            )
        ]
        assert await self.bot._directory_pool.connect_all_parallel() == self.directory_count
        self.bot.running = True
        self.bot._start_generation_listeners(self.bot.generations[self.bot.current_generation_id])
        bot = self.bot
        await _wait_until(
            lambda: all(
                client.connection is not None and client.connection._receive_lock.locked()
                for client in bot.directory_clients.values()
            ),
            "maker directory listeners",
        )

        for _ in range(self.requester_count):
            identity = NickIdentity()
            self.requesters.append(
                Requester(
                    identity=identity,
                    clients=[
                        DirectoryClient(
                            host=_HOST,
                            port=port,
                            network=NetworkType.REGTEST.value,
                            nick_identity=identity,
                            nick_auth_mode=NickAuthMode.DISABLED,
                            timeout=_STARTUP_TIMEOUT,
                        )
                        for port in ports
                    ],
                )
            )
        await asyncio.gather(
            *(client.connect() for requester in self.requesters for client in requester.clients)
        )
        await _wait_until(
            lambda: all(
                server.peer_registry.count() == self.requester_count + 1 for server in self.servers
            ),
            "all maker and requester directory handshakes",
        )

    async def close(self) -> None:
        failures: list[BaseException] = []

        requester_results = await asyncio.gather(
            *(client.close() for requester in self.requesters for client in requester.clients),
            return_exceptions=True,
        )
        failures.extend(_unexpected_failures(requester_results))

        if self.bot is not None:
            self.bot.running = False
            await self.bot._directory_pool.close_all()
            for task in self.bot.listen_tasks:
                if not task.done():
                    task.cancel()
            listener_results = await asyncio.wait_for(
                asyncio.gather(*self.bot.listen_tasks, return_exceptions=True),
                timeout=_TEARDOWN_TIMEOUT,
            )
            failures.extend(_unexpected_failures(listener_results))

        server_results = await asyncio.gather(
            *(server.stop() for server in self.servers), return_exceptions=True
        )
        failures.extend(_unexpected_failures(server_results))
        for task in self.server_tasks:
            if not task.done():
                task.cancel()
        task_results = await asyncio.wait_for(
            asyncio.gather(*self.server_tasks, return_exceptions=True),
            timeout=_TEARDOWN_TIMEOUT,
        )
        failures.extend(_unexpected_failures(task_results))
        if any(server.health_server.thread is not None for server in self.servers):
            failures.append(
                RuntimeError("directory health-check thread remained active after teardown")
            )
        if failures:
            raise RuntimeError("transport fanout teardown failed") from failures[0]


@pytest.mark.parametrize("directory_count", [2, 6])
async def test_orderbook_fanout_over_real_regtest_tcp(directory_count: int, tmp_path: Path) -> None:
    harness = TransportFanoutHarness(directory_count, tmp_path)
    try:
        await harness.start()
        assert harness.bot is not None
        bot = harness.bot
        expected_sources = {f"dir:{node_id}" for node_id in bot.directory_clients}
        assert bot._orderbook_rate_limiter.directory_sources == expected_sources

        await asyncio.gather(
            *(
                client.send_public_message("orderbook")
                for requester in harness.requesters
                for client in requester.clients
            )
        )
        responses = await asyncio.gather(
            *(
                _receive_offer(client, bot.nick)
                for requester in harness.requesters
                for client in requester.clients
            )
        )

        assert len(responses) == _REQUESTER_COUNT * directory_count
        expected_duplicates = _REQUESTER_COUNT * (directory_count - 1)
        await _wait_until(
            lambda: (
                bot._orderbook_rate_limiter.get_statistics()["fanout_duplicates"]
                >= expected_duplicates
            ),
            "all directory fanout copies processed",
        )
        statistics = bot._orderbook_rate_limiter.get_statistics()
        assert statistics["total_violations"] == 0
        assert statistics["fanout_duplicates"] == _REQUESTER_COUNT * (directory_count - 1)
        assert all(
            bot._orderbook_rate_limiter.get_violation_count(requester.nick) == 0
            and not bot._orderbook_rate_limiter.is_banned(requester.nick)
            for requester in harness.requesters
        )
        assert bot._orderbook_response_counts == {
            "directory_admitted": _REQUESTER_COUNT,
            "directory_suppressed": 0,
            "direct_admitted": 0,
            "direct_suppressed": 0,
        }
    finally:
        await harness.close()


async def _receive_bonded_offers(client: DirectoryClient, maker_nick: str) -> None:
    """Check that both signed offers and their requester-bound proofs survive routing."""
    offer_ids = set()
    for _ in range(2):
        message = await _receive_offer(client, maker_nick)
        command_data = message["line"].split(COMMAND_PREFIX, 2)[2]
        authenticated, command, data = verify_signed_privmsg(maker_nick, command_data, ONION_HOSTID)
        assert authenticated
        assert command == OfferType.SW0_RELATIVE.value
        offer_data, proof = data.split("!tbond ")
        offer_ids.add(int(offer_data.split()[0]))
        assert parse_fidelity_bond_proof(proof, maker_nick, client.nick) is not None
    assert offer_ids == {0, 1}


@pytest.mark.parametrize(
    ("directory_count", "requester_count", "request_interval"),
    [(2, 200, 0.0), (8, 40, 0.0), (2, 200, 0.05)],
    ids=["full-burst", "eight-directories", "twenty-per-second"],
)
async def test_bonded_discovery_capacity_over_real_tcp(
    directory_count: int, requester_count: int, request_interval: float, tmp_path: Path
) -> None:
    """Exercise real proof signing, directory fanout, and delivery at the new capacity."""
    harness = TransportFanoutHarness(directory_count, tmp_path, requester_count)
    try:
        await harness.start()
        assert harness.bot is not None
        bot = harness.bot
        bot.current_offers.append(bot.current_offers[0].model_copy(update={"oid": 1}))
        key = CKey(b"\x01" * 32)
        bot.fidelity_bond = FidelityBondInfo(
            txid="00" * 32,
            vout=0,
            value=100_000_000,
            locktime=2_000_000_000,
            confirmation_time=1_700_000_000,
            bond_value=1,
            pubkey=bytes(key.pub),
            private_key=key,
        )

        # One task per requester drains all its directories while sending. In the
        # paced case, requesters start at 20/s without blocking response readers.
        async def discover(requester: Requester, index: int) -> None:
            if request_interval:
                await asyncio.sleep(index * request_interval)
            async with asyncio.TaskGroup() as exchange:
                for client in requester.clients:
                    exchange.create_task(client.send_public_message("orderbook"))
                    exchange.create_task(_receive_bonded_offers(client, bot.nick))

        async with asyncio.TaskGroup() as discoveries:
            for index, requester in enumerate(harness.requesters):
                discoveries.create_task(discover(requester, index))

        expected_duplicates = requester_count * (directory_count - 1)
        await _wait_until(
            lambda: (
                bot._orderbook_rate_limiter.get_statistics()["fanout_duplicates"]
                == expected_duplicates
            ),
            "capacity-test directory fanout copies",
        )
        assert bot._orderbook_response_counts == {
            "directory_admitted": requester_count,
            "directory_suppressed": 0,
            "direct_admitted": 0,
            "direct_suppressed": 0,
        }
        assert bot._orderbook_rate_limiter.get_statistics()["total_violations"] == 0
        assert len(bot.directory_clients) == directory_count
        assert all(client.connection is not None for client in bot.directory_clients.values())
    finally:
        await harness.close()
