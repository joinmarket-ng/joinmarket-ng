"""
Load tests for directory server performance and memory usage.

Simulates real-world JoinMarket scenarios with multiple concurrent peers.
"""

import asyncio
import contextlib
import gc
import json
import random
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest
import pytest_asyncio
from jmcore.models import MessageEnvelope, NetworkType
from jmcore.protocol import COMMAND_PREFIX, JM_VERSION, MessageType
from jmcore.settings import DirectoryServerSettings
from loguru import logger

from directory_server.server import DirectoryServer


class MemoryTracker:
    def __init__(self) -> None:
        self.process = psutil.Process()
        self.start_memory = 0
        self.peak_memory = 0
        self.samples: list[float] = []

    def start(self) -> None:
        gc.collect()
        self.start_memory = self.process.memory_info().rss / 1024 / 1024
        self.peak_memory = self.start_memory
        self.samples = [self.start_memory]

    def sample(self) -> float:
        current = self.process.memory_info().rss / 1024 / 1024
        self.samples.append(current)
        self.peak_memory = max(self.peak_memory, current)
        return current

    def report(self) -> dict[str, float]:
        return {
            "start_mb": round(self.start_memory, 2),
            "end_mb": round(self.samples[-1], 2),
            "peak_mb": round(self.peak_memory, 2),
            "delta_mb": round(self.samples[-1] - self.start_memory, 2),
            "avg_mb": round(sum(self.samples) / len(self.samples), 2),
        }


class MockPeerClient:
    def __init__(self, nick: str, host: str, port: int, network: NetworkType):
        self.nick = nick
        self.host = host
        self.port = port
        self.network = network
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.connected = False
        self.messages_sent = 0
        self.messages_received = 0

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self.connected = True

        handshake_msg = json.dumps(
            {
                "app-name": "JoinMarket",
                "directory": False,
                "location-string": "NOT-SERVING-ONION",
                "proto-ver": JM_VERSION,
                "features": {},
                "nick": self.nick,
                "network": self.network.value,
            }
        )

        envelope = MessageEnvelope(message_type=MessageType.HANDSHAKE, payload=handshake_msg)
        self.writer.write(envelope.to_bytes() + b"\n")
        await self.writer.drain()
        self.messages_sent += 1

        response_data = await self.reader.readuntil(b"\n")
        response = MessageEnvelope.from_bytes(response_data.rstrip(b"\n"))
        self.messages_received += 1

        if response.message_type != MessageType.DN_HANDSHAKE:
            raise ValueError(f"Expected DN_HANDSHAKE, got {response.message_type}")

    async def send_public_message(self, content: str) -> None:
        if not self.connected or not self.writer:
            raise RuntimeError("Not connected")

        msg = f"{COMMAND_PREFIX}{self.nick} PUBLIC {content}"
        envelope = MessageEnvelope(message_type=MessageType.PUBMSG, payload=msg)
        self.writer.write(envelope.to_bytes() + b"\n")
        await self.writer.drain()
        self.messages_sent += 1

    async def send_private_message(self, target_nick: str, content: str) -> None:
        if not self.connected or not self.writer:
            raise RuntimeError("Not connected")

        msg = f"{COMMAND_PREFIX}{self.nick} {target_nick} {content}"
        envelope = MessageEnvelope(message_type=MessageType.PRIVMSG, payload=msg)
        self.writer.write(envelope.to_bytes() + b"\n")
        await self.writer.drain()
        self.messages_sent += 1

    async def receive_message(self, timeout: float = 1.0) -> MessageEnvelope | None:
        if not self.connected or not self.reader:
            raise RuntimeError("Not connected")

        try:
            data = await asyncio.wait_for(self.reader.readuntil(b"\n"), timeout=timeout)
            if not data:
                return None
            self.messages_received += 1
            return MessageEnvelope.from_bytes(data.rstrip(b"\n"))
        except TimeoutError:
            return None

    async def disconnect(self) -> None:
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass  # Ignore errors during cleanup
        self.connected = False


class LoadTestScenario:
    def __init__(self, server_host: str, server_port: int):
        self.server_host = server_host
        self.server_port = server_port
        self.clients: list[MockPeerClient] = []
        self.memory_tracker = MemoryTracker()
        self.start_time = 0.0
        self.end_time = 0.0

    async def create_and_connect_peer(self, nick: str, network: NetworkType) -> MockPeerClient:
        client = MockPeerClient(nick, self.server_host, self.server_port, network)
        await client.connect()
        self.clients.append(client)
        return client

    async def cleanup(self) -> None:
        disconnect_tasks = [client.disconnect() for client in self.clients]
        await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        self.clients.clear()

    def calculate_stats(self) -> dict:
        duration = self.end_time - self.start_time
        total_sent = sum(c.messages_sent for c in self.clients)
        total_received = sum(c.messages_received for c in self.clients)

        return {
            "duration_seconds": round(duration, 2),
            "num_clients": len(self.clients),
            "total_messages_sent": total_sent,
            "total_messages_received": total_received,
            "messages_per_second": round(total_sent / duration, 2) if duration > 0 else 0,
            "avg_messages_per_client": round(total_sent / len(self.clients), 2)
            if self.clients
            else 0,
            "memory": self.memory_tracker.report(),
        }


@contextlib.asynccontextmanager
async def running_test_server() -> AsyncIterator[tuple[str, int]]:
    settings = DirectoryServerSettings(
        host="127.0.0.1",
        port=0,
        max_peers=1000,
        health_check_port=0,  # Use dynamic port to avoid conflicts
    )

    server = DirectoryServer(settings, NetworkType.TESTNET, "J5TestNickOOOOOO")

    server_task = asyncio.create_task(server.start())

    await asyncio.sleep(0.5)

    # Type guard for server.server which should be set after start()
    assert server.server is not None, "Server should be running after start()"
    actual_port = server.server.sockets[0].getsockname()[1]

    try:
        yield "127.0.0.1", actual_port
    finally:
        # Clean up server
        await server.stop()
        # Cancel the server task since serve_forever() won't return naturally
        server_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            # Use timeout to prevent hangs in Python 3.12+ where task cancellation
            # can block indefinitely if serve_forever() doesn't exit cleanly
            await asyncio.wait_for(server_task, timeout=5.0)


@pytest_asyncio.fixture
async def test_server() -> AsyncIterator[tuple[str, int]]:
    async with running_test_server() as address:
        yield address


@pytest.mark.asyncio
async def test_load_basic_handshakes(test_server):
    host, port = test_server
    scenario = LoadTestScenario(host, port)
    scenario.memory_tracker.start()
    scenario.start_time = time.time()

    try:
        num_peers = 50
        for i in range(num_peers):
            await scenario.create_and_connect_peer(f"peer_{i}", NetworkType.TESTNET)
            if i % 10 == 0:
                scenario.memory_tracker.sample()

        await asyncio.sleep(1)
        scenario.end_time = time.time()
        scenario.memory_tracker.sample()

        stats = scenario.calculate_stats()
        logger.info(f"Basic handshakes test stats: {json.dumps(stats, indent=2)}")

        assert stats["num_clients"] == num_peers
        assert stats["memory"]["delta_mb"] < 50

    finally:
        await scenario.cleanup()


@pytest.mark.asyncio
async def test_load_public_broadcast(test_server):
    host, port = test_server
    scenario = LoadTestScenario(host, port)
    scenario.memory_tracker.start()
    scenario.start_time = time.time()

    try:
        num_peers = 30
        peers = []
        for i in range(num_peers):
            peer = await scenario.create_and_connect_peer(f"peer_{i}", NetworkType.TESTNET)
            peers.append(peer)
            await asyncio.sleep(0.01)

        scenario.memory_tracker.sample()

        async def drain_messages(peer: MockPeerClient, duration: float):
            end = time.time() + duration
            while time.time() < end:
                await peer.receive_message(timeout=0.1)

        receiver_tasks = [asyncio.create_task(drain_messages(p, 3.0)) for p in peers]

        for i in range(100):
            sender = random.choice(peers)
            await sender.send_public_message(f"!orderbook !fidelity_bond {i}")
            await asyncio.sleep(0.01)

        await asyncio.gather(*receiver_tasks)

        scenario.end_time = time.time()
        scenario.memory_tracker.sample()

        stats = scenario.calculate_stats()
        logger.info(f"Public broadcast test stats: {json.dumps(stats, indent=2)}")

        assert stats["messages_per_second"] > 10
        assert stats["memory"]["delta_mb"] < 100

    finally:
        await scenario.cleanup()


@pytest.mark.asyncio
async def test_load_private_messages(test_server):
    host, port = test_server
    scenario = LoadTestScenario(host, port)
    scenario.memory_tracker.start()
    scenario.start_time = time.time()

    try:
        num_makers = 10
        num_takers = 5
        makers = []
        takers = []

        for i in range(num_makers):
            peer = await scenario.create_and_connect_peer(f"maker_{i}", NetworkType.TESTNET)
            makers.append(peer)
            await asyncio.sleep(0.01)

        for i in range(num_takers):
            peer = await scenario.create_and_connect_peer(f"taker_{i}", NetworkType.TESTNET)
            takers.append(peer)
            await asyncio.sleep(0.01)

        scenario.memory_tracker.sample()

        async def maker_receive(maker: MockPeerClient):
            for _ in range(20):
                await maker.receive_message(timeout=0.5)

        maker_tasks = [asyncio.create_task(maker_receive(m)) for m in makers]

        for i in range(50):
            taker = random.choice(takers)
            maker = random.choice(makers)
            await taker.send_private_message(maker.nick, f"!fill {i} 10000000")
            await asyncio.sleep(0.02)

        await asyncio.gather(*maker_tasks, return_exceptions=True)

        scenario.end_time = time.time()
        scenario.memory_tracker.sample()

        stats = scenario.calculate_stats()
        logger.info(f"Private messages test stats: {json.dumps(stats, indent=2)}")

        assert stats["messages_per_second"] > 5
        assert stats["memory"]["delta_mb"] < 100

    finally:
        await scenario.cleanup()


@pytest.mark.asyncio
async def test_load_sustained_operation(test_server):
    host, port = test_server
    scenario = LoadTestScenario(host, port)
    scenario.memory_tracker.start()
    scenario.start_time = time.time()

    try:
        num_peers = 100
        peers = []

        for i in range(num_peers):
            peer = await scenario.create_and_connect_peer(f"peer_{i}", NetworkType.TESTNET)
            peers.append(peer)
            if i % 20 == 0:
                scenario.memory_tracker.sample()

        async def peer_activity(peer: MockPeerClient, duration: float):
            end = time.time() + duration
            while time.time() < end:
                action = random.choice(["send_public", "send_private", "receive"])
                try:
                    if action == "send_public":
                        await peer.send_public_message("!orderbook")
                    elif action == "send_private":
                        target = random.choice(peers)
                        await peer.send_private_message(target.nick, "!auth")
                    elif action == "receive":
                        await peer.receive_message(timeout=0.1)
                except Exception:
                    pass
                await asyncio.sleep(random.uniform(0.05, 0.2))

        activity_duration = 10.0
        tasks = [asyncio.create_task(peer_activity(p, activity_duration)) for p in peers[:50]]

        for _ in range(int(activity_duration)):
            await asyncio.sleep(1)
            scenario.memory_tracker.sample()

        await asyncio.gather(*tasks, return_exceptions=True)

        scenario.end_time = time.time()
        scenario.memory_tracker.sample()

        stats = scenario.calculate_stats()
        logger.info(f"Sustained operation test stats: {json.dumps(stats, indent=2)}")

        assert stats["duration_seconds"] >= activity_duration
        assert stats["num_clients"] == num_peers
        assert stats["memory"]["delta_mb"] < 150

    finally:
        await scenario.cleanup()


@pytest.mark.asyncio
async def test_load_churn(test_server):
    host, port = test_server
    scenario = LoadTestScenario(host, port)
    scenario.memory_tracker.start()
    scenario.start_time = time.time()

    try:
        active_peers = []

        for round_num in range(5):
            for i in range(20):
                peer = await scenario.create_and_connect_peer(
                    f"peer_r{round_num}_{i}", NetworkType.TESTNET
                )
                active_peers.append(peer)
                await asyncio.sleep(0.01)

            if len(active_peers) > 10:
                for _ in range(10):
                    peer = active_peers.pop(0)
                    await peer.disconnect()

            scenario.memory_tracker.sample()

        scenario.end_time = time.time()
        scenario.memory_tracker.sample()

        stats = scenario.calculate_stats()
        logger.info(f"Churn test stats: {json.dumps(stats, indent=2)}")

        assert stats["memory"]["delta_mb"] < 100

    finally:
        await scenario.cleanup()


def test_load_stress_max_peers() -> None:
    # The absolute RSS cap must not include memory retained by earlier tests.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stress_subprocess_failure_propagates() -> None:
    result = subprocess.CompletedProcess([], 1, stdout="", stderr="stress assertion failed")
    with (
        patch.object(subprocess, "run", return_value=result),
        pytest.raises(AssertionError, match="stress assertion failed"),
    ):
        test_load_stress_max_peers()


async def run_stress_max_peers(test_server: tuple[str, int]) -> None:
    host, port = test_server
    scenario = LoadTestScenario(host, port)
    scenario.memory_tracker.start()
    scenario.start_time = time.time()

    try:
        num_peers = 200

        connect_tasks = []
        for i in range(num_peers):
            task = asyncio.create_task(
                scenario.create_and_connect_peer(f"peer_{i}", NetworkType.TESTNET)
            )
            connect_tasks.append(task)

        results = await asyncio.gather(*connect_tasks, return_exceptions=True)
        connected = [r for r in results if isinstance(r, MockPeerClient)]

        scenario.memory_tracker.sample()

        async def broadcast_storm(duration: float):
            end = time.time() + duration
            while time.time() < end:
                peer = random.choice(connected)
                await peer.send_public_message("!orderbook")
                await asyncio.sleep(0.001)

        await broadcast_storm(5.0)

        scenario.end_time = time.time()
        scenario.memory_tracker.sample()

        stats = scenario.calculate_stats()
        logger.info(f"Stress test stats: {json.dumps(stats, indent=2)}")

        assert len(connected) >= 100
        assert stats["memory"]["peak_mb"] < 500

    finally:
        await scenario.cleanup()


async def run_isolated_stress_test() -> None:
    async with running_test_server() as address:
        await run_stress_max_peers(address)


if __name__ == "__main__":
    asyncio.run(run_isolated_stress_test())
