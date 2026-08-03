"""
Tests for heartbeat liveness detection (HeartbeatManager).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from jmcore.models import NetworkType, PeerInfo, PeerStatus
from jmcore.protocol import MessageType

from directory_server.heartbeat import HeartbeatConfig, HeartbeatManager
from directory_server.peer_registry import PeerRegistry


@pytest.fixture
def registry() -> PeerRegistry:
    return PeerRegistry(max_peers=100)


def _make_peer(
    nick: str,
    *,
    onion: str | None = None,
    features: dict | None = None,
    network: NetworkType = NetworkType.MAINNET,
) -> PeerInfo:
    """Helper to create a registered-ready PeerInfo."""
    if onion is None:
        # Generate a unique valid onion address from the nick
        base = nick.ljust(56, "a")[:56].replace("_", "a")
        # Ensure only valid base32 chars (a-z, 2-7)
        safe = ""
        for ch in base:
            if ch in "abcdefghijklmnopqrstuvwxyz234567":
                safe += ch
            else:
                safe += "a"
        onion = f"{safe}.onion"
    return PeerInfo(
        nick=nick,
        onion_address=onion,
        port=5222,
        network=network,
        status=PeerStatus.HANDSHAKED,
        features=features or {},
    )


def _register_and_handshake(
    registry: PeerRegistry,
    peer: PeerInfo,
    last_seen: datetime | None = None,
) -> str:
    """Register a peer, mark it HANDSHAKED, optionally backdate last_seen.

    Returns the peer key used in the registry.
    """
    registry.register(peer)
    key = peer.nick
    registry.update_status(key, PeerStatus.HANDSHAKED)
    if last_seen is not None:
        p = registry.get_by_key(key)
        assert p is not None
        p.last_seen = last_seen
    return key


# ---------------------------------------------------------------------------
# HeartbeatConfig
# ---------------------------------------------------------------------------


class TestHeartbeatConfig:
    def test_defaults_match_joinmarket_rs(self) -> None:
        cfg = HeartbeatConfig()
        assert cfg.sweep_interval_sec == 60.0
        assert cfg.idle_threshold_sec == 600.0
        assert cfg.hard_evict_sec == 1500.0
        assert cfg.pong_wait_sec == 30.0

    def test_custom_values(self) -> None:
        cfg = HeartbeatConfig(
            sweep_interval_sec=10,
            idle_threshold_sec=20,
            hard_evict_sec=30,
            pong_wait_sec=5,
        )
        assert cfg.sweep_interval_sec == 10
        assert cfg.idle_threshold_sec == 20
        assert cfg.hard_evict_sec == 30
        assert cfg.pong_wait_sec == 5


# ---------------------------------------------------------------------------
# HeartbeatManager — sweep logic
# ---------------------------------------------------------------------------


class TestHeartbeatSweep:
    """Test a single _sweep() invocation with various peer states."""

    @pytest.fixture
    def config(self) -> HeartbeatConfig:
        return HeartbeatConfig(
            sweep_interval_sec=0.05,
            idle_threshold_sec=60,
            hard_evict_sec=120,
            pong_wait_sec=0.05,
        )

    @pytest.fixture
    def send_cb(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def evict_cb(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture
    def manager(
        self,
        registry: PeerRegistry,
        send_cb: AsyncMock,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> HeartbeatManager:
        return HeartbeatManager(
            peer_registry=registry,
            send_callback=send_cb,
            evict_callback=evict_cb,
            config=config,
        )

    # -- hard eviction --

    @pytest.mark.anyio
    async def test_hard_evict_peers_idle_beyond_hard_evict_sec(
        self,
        registry: PeerRegistry,
        manager: HeartbeatManager,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Peers idle longer than hard_evict_sec are evicted unconditionally."""
        old_time = datetime.now(UTC) - timedelta(seconds=config.hard_evict_sec + 10)
        peer = _make_peer("stale_maker", features={"ping": True})
        key = _register_and_handshake(registry, peer, last_seen=old_time)
        connection_id = registry.get_connection_id(key)
        assert connection_id is not None

        await manager._sweep()

        evict_cb.assert_any_await(key, "idle timeout", connection_id)

    @pytest.mark.anyio
    async def test_hard_evict_clears_pong_pending(
        self,
        registry: PeerRegistry,
        manager: HeartbeatManager,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Hard-evicted peers are removed from pong_pending set."""
        old_time = datetime.now(UTC) - timedelta(seconds=config.hard_evict_sec + 10)
        peer = _make_peer("stale_peer", features={"ping": True})
        key = _register_and_handshake(registry, peer, last_seen=old_time)

        # Manually mark pending
        connection_id = registry.get_connection_id(key)
        assert connection_id is not None
        manager._pong_pending.add((key, connection_id))

        await manager._sweep()

        assert (key, connection_id) not in manager.pong_pending

    # -- probing idle peers --

    @pytest.mark.anyio
    async def test_ping_capable_peer_receives_ping(
        self,
        registry: PeerRegistry,
        manager: HeartbeatManager,
        send_cb: AsyncMock,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Peers with ping feature and idle > idle_threshold get a PING."""
        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        peer = _make_peer("ping_maker", features={"ping": True})
        key = _register_and_handshake(registry, peer, last_seen=idle_time)

        await manager._sweep()

        # Verify PING was sent
        assert send_cb.await_count >= 1
        sent_data = send_cb.call_args_list[0][0][1]
        # Decode the envelope to check message type
        import json

        envelope = json.loads(sent_data.decode("utf-8"))
        assert envelope["type"] == MessageType.PING.value

        # Peer should be in pong_pending (will be cleared after pong_wait)
        # Note: after _sweep completes, pong_pending was checked and evict was called.
        # Since we didn't send a pong, the peer would have been evicted.
        evict_calls = [c[0][0] for c in evict_cb.call_args_list]
        assert key in evict_calls

    @pytest.mark.anyio
    async def test_non_ping_maker_receives_orderbook_probe(
        self,
        registry: PeerRegistry,
        send_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Non-ping makers get a unicast !orderbook probe with proper PUBLIC format."""
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=send_cb,
            evict_callback=AsyncMock(),
            config=config,
            server_nick="J5DNtestdir",
        )
        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        # No "ping" feature, but is a maker (has onion address)
        peer = _make_peer("legacy_maker")
        _register_and_handshake(registry, peer, last_seen=idle_time)

        await manager._sweep()

        # Verify orderbook probe was sent
        assert send_cb.await_count >= 1
        sent_data = send_cb.call_args_list[0][0][1]
        import json

        envelope = json.loads(sent_data.decode("utf-8"))
        assert envelope["type"] == MessageType.PUBMSG.value
        # Must be proper format: from_nick!PUBLIC!orderbook
        payload = envelope["line"]
        assert "orderbook" in payload
        assert "!PUBLIC!" in payload
        assert payload.startswith("J5DNtestdir!")

    @pytest.mark.anyio
    async def test_shared_location_peers_are_probed_by_nick(
        self,
        registry: PeerRegistry,
        send_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=send_cb,
            evict_callback=AsyncMock(),
            config=config,
            server_nick="J5DNtestdir",
        )
        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        shared_onion = "a" * 56 + ".onion"
        first_key = _register_and_handshake(
            registry,
            _make_peer("first_maker", onion=shared_onion),
            last_seen=idle_time,
        )
        second_key = _register_and_handshake(
            registry,
            _make_peer("second_maker", onion=shared_onion),
            last_seen=idle_time,
        )

        await manager._sweep()

        assert {call.args[0] for call in send_cb.await_args_list} == {first_key, second_key}

    @pytest.mark.anyio
    async def test_orderbook_probe_format_compatible_with_reference(
        self,
        registry: PeerRegistry,
        send_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Orderbook probe must be parseable as on_pubmsg by reference clients.

        The reference implementation splits on '!' (COMMAND_PREFIX) and checks
        that to_nick == "PUBLIC" to route through on_pubmsg.  A malformed probe
        that sets to_nick to the maker's nick would be routed through on_privmsg
        instead, causing "Sig not properly appended to privmsg" errors.
        """
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=send_cb,
            evict_callback=AsyncMock(),
            config=config,
            server_nick="J5DNnode",
        )
        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        peer = _make_peer("legacy_maker2", onion="e" * 56 + ".onion")
        _register_and_handshake(registry, peer, last_seen=idle_time)

        await manager._sweep()

        import json

        sent_data = send_cb.call_args_list[0][0][1]
        envelope = json.loads(sent_data.decode("utf-8"))
        payload = envelope["line"]

        # Simulate reference client parsing: split on "!" (COMMAND_PREFIX)
        parts = payload.split("!")
        assert len(parts) >= 3, f"Expected at least 3 parts, got {parts}"
        from_nick = parts[0]
        to_nick = parts[1]
        rest = "!".join(parts[2:])

        assert from_nick == "J5DNnode"
        assert to_nick == "PUBLIC", (
            f"to_nick must be 'PUBLIC' for on_pubmsg routing, got '{to_nick}'"
        )
        assert rest == "orderbook"

    @pytest.mark.anyio
    async def test_non_ping_taker_gets_no_probe(
        self,
        registry: PeerRegistry,
        manager: HeartbeatManager,
        send_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Non-ping takers (NOT-SERVING-ONION, no ping feature) get no probe."""
        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        peer = PeerInfo(
            nick="legacy_taker",
            onion_address="NOT-SERVING-ONION",
            port=-1,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        _register_and_handshake(registry, peer, last_seen=idle_time)

        await manager._sweep()

        # No send at all -- taker has no probe mechanism
        send_cb.assert_not_awaited()

    @pytest.mark.anyio
    async def test_recently_active_peer_not_probed(
        self,
        registry: PeerRegistry,
        manager: HeartbeatManager,
        send_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Peers active within idle_threshold are not probed."""
        recent_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec - 10)
        peer = _make_peer("active_maker", features={"ping": True})
        _register_and_handshake(registry, peer, last_seen=recent_time)

        await manager._sweep()

        send_cb.assert_not_awaited()

    # -- PONG response handling --

    @pytest.mark.anyio
    async def test_pong_clears_pending_and_prevents_eviction(
        self,
        registry: PeerRegistry,
        manager: HeartbeatManager,
        send_cb: AsyncMock,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Receiving PONG before pong_wait expires prevents eviction."""
        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        peer = _make_peer("responsive_maker", features={"ping": True})
        key = _register_and_handshake(registry, peer, last_seen=idle_time)

        # Patch pong_wait_sec to give us time to inject PONG
        manager.config = HeartbeatConfig(
            sweep_interval_sec=0.05,
            idle_threshold_sec=config.idle_threshold_sec,
            hard_evict_sec=config.hard_evict_sec,
            pong_wait_sec=0.5,  # longer wait to give us time
        )

        # Run sweep in background
        sweep_task = asyncio.create_task(manager._sweep())

        # Wait a bit for PING to be sent, then simulate PONG
        await asyncio.sleep(0.05)
        manager.handle_pong(key)

        await sweep_task

        # Peer should NOT be evicted
        evict_calls = [c[0][0] for c in evict_cb.call_args_list]
        assert key not in evict_calls

    @pytest.mark.anyio
    async def test_pong_timeout_evicts_peer(
        self,
        registry: PeerRegistry,
        manager: HeartbeatManager,
        send_cb: AsyncMock,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Peers that don't respond with PONG within pong_wait_sec are evicted."""
        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        peer = _make_peer("unresponsive_maker", features={"ping": True})
        key = _register_and_handshake(registry, peer, last_seen=idle_time)
        connection_id = registry.get_connection_id(key)
        assert connection_id is not None

        await manager._sweep()

        # Peer should have been evicted with "pong timeout" reason
        evict_cb.assert_any_await(key, "pong timeout", connection_id)

    # -- send failure --

    @pytest.mark.anyio
    async def test_send_ping_failure_triggers_immediate_eviction(
        self,
        registry: PeerRegistry,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """If sending PING fails, the peer is evicted immediately."""
        send_cb = AsyncMock(side_effect=ConnectionError("broken pipe"))
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=send_cb,
            evict_callback=evict_cb,
            config=config,
        )

        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        peer = _make_peer("broken_maker", features={"ping": True})
        key = _register_and_handshake(registry, peer, last_seen=idle_time)

        await manager._sweep()

        # Should be evicted with send-failed reason
        evict_calls = [c[0] for c in evict_cb.call_args_list]
        send_failed = [c for c in evict_calls if c[0] == key and "send failed" in c[1]]
        assert len(send_failed) >= 1

    @pytest.mark.anyio
    async def test_send_ping_failure_clears_pong_pending(
        self,
        registry: PeerRegistry,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """Send failure should clear pong_pending to prevent double eviction."""
        send_cb = AsyncMock(side_effect=ConnectionError("broken pipe"))
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=send_cb,
            evict_callback=evict_cb,
            config=config,
        )

        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        peer = _make_peer("broken_maker2", features={"ping": True})
        _register_and_handshake(registry, peer, last_seen=idle_time)

        await manager._sweep()

        assert manager.pong_pending == frozenset()

    # -- mixed peers --

    @pytest.mark.anyio
    async def test_sweep_handles_mixed_peer_types(
        self,
        registry: PeerRegistry,
        manager: HeartbeatManager,
        send_cb: AsyncMock,
        evict_cb: AsyncMock,
        config: HeartbeatConfig,
    ) -> None:
        """A sweep with a mix of peer types handles each correctly."""
        idle_time = datetime.now(UTC) - timedelta(seconds=config.idle_threshold_sec + 10)
        hard_evict_time = datetime.now(UTC) - timedelta(seconds=config.hard_evict_sec + 10)
        recent_time = datetime.now(UTC) - timedelta(seconds=10)

        # 1) Ping-capable idle maker
        ping_peer = _make_peer("ping_maker", features={"ping": True})
        ping_key = _register_and_handshake(registry, ping_peer, last_seen=idle_time)

        # 2) Legacy (non-ping) idle maker
        legacy_maker = _make_peer("legacy_maker", onion="b" * 56 + ".onion")
        _register_and_handshake(registry, legacy_maker, last_seen=idle_time)

        # 3) Hard-evict candidate
        stale_peer = _make_peer("stale_peer", onion="c" * 56 + ".onion")
        stale_key = _register_and_handshake(registry, stale_peer, last_seen=hard_evict_time)
        stale_connection_id = registry.get_connection_id(stale_key)
        assert stale_connection_id is not None

        # 4) Recently active peer (should not be touched)
        active_peer = _make_peer("active_peer", onion="d" * 56 + ".onion", features={"ping": True})
        _register_and_handshake(registry, active_peer, last_seen=recent_time)

        await manager._sweep()

        # Stale peer should be hard-evicted
        evict_cb.assert_any_await(stale_key, "idle timeout", stale_connection_id)

        # Ping peer should be evicted (no pong response)
        evict_calls = [c[0][0] for c in evict_cb.call_args_list]
        assert ping_key in evict_calls

        # send_cb should have been called (PING + !orderbook probe)
        assert send_cb.await_count >= 2


# ---------------------------------------------------------------------------
# HeartbeatManager — handle_pong
# ---------------------------------------------------------------------------


class TestHandlePong:
    def test_handle_pong_clears_pending(self) -> None:
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
        )
        manager._pong_pending.add(("some_key", "connection"))

        manager.handle_pong("some_key", "connection")

        assert ("some_key", "connection") not in manager.pong_pending

    def test_handle_pong_noop_for_unknown_key(self) -> None:
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
        )

        # Should not raise
        manager.handle_pong("unknown_key")

        assert manager.pong_pending == frozenset()


# ---------------------------------------------------------------------------
# HeartbeatManager — pong_pending property
# ---------------------------------------------------------------------------


class TestPongPendingProperty:
    def test_returns_frozen_copy(self) -> None:
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
        )
        manager._pong_pending.add(("key1", "connection1"))
        manager._pong_pending.add(("key2", "connection2"))

        pending = manager.pong_pending
        assert isinstance(pending, frozenset)
        assert pending == frozenset({("key1", "connection1"), ("key2", "connection2")})

        # Mutating the internal set should not affect previously returned snapshot
        manager._pong_pending.discard(("key1", "connection1"))
        assert ("key1", "connection1") in pending

    def test_stale_pong_does_not_clear_replacement_pending(self) -> None:
        registry = PeerRegistry()
        peer = _make_peer("maker", features={"ping": True})
        key = registry.register(peer, "old").peer_key
        registry.register(peer.model_copy(deep=True), "new", verified_pubkey=b"pubkey")
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
        )
        manager._pong_pending.add((key, "new"))

        manager.handle_pong(key, "old")

        assert manager.pong_pending == frozenset({(key, "new")})


# ---------------------------------------------------------------------------
# HeartbeatManager — start / stop lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.anyio
    async def test_start_creates_task(self) -> None:
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
            config=HeartbeatConfig(sweep_interval_sec=999),
        )

        manager.start()
        try:
            assert manager._task is not None
            assert not manager._task.done()
        finally:
            await manager.stop()

    @pytest.mark.anyio
    async def test_start_is_idempotent(self) -> None:
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
            config=HeartbeatConfig(sweep_interval_sec=999),
        )

        manager.start()
        task1 = manager._task
        manager.start()  # second call is a no-op
        assert manager._task is task1

        await manager.stop()

    @pytest.mark.anyio
    async def test_stop_cancels_task(self) -> None:
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
            config=HeartbeatConfig(sweep_interval_sec=999),
        )

        manager.start()
        task = manager._task
        assert task is not None

        await manager.stop()

        assert manager._task is None
        assert task.done()

    @pytest.mark.anyio
    async def test_stop_clears_pong_pending(self) -> None:
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
            config=HeartbeatConfig(sweep_interval_sec=999),
        )

        manager._pong_pending.add(("leftover_key", "connection"))
        manager.start()
        await manager.stop()

        assert manager.pong_pending == frozenset()

    @pytest.mark.anyio
    async def test_stop_is_idempotent(self) -> None:
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
        )

        # stop() before start() should be a no-op
        await manager.stop()
        await manager.stop()

    @pytest.mark.anyio
    async def test_loop_runs_sweep_periodically(self) -> None:
        """Verify the background loop actually calls _sweep."""
        sweep_count = 0
        target = 2
        reached = asyncio.Event()
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
            config=HeartbeatConfig(sweep_interval_sec=0.05),
        )

        original_sweep = manager._sweep

        async def counting_sweep() -> None:
            nonlocal sweep_count
            sweep_count += 1
            await original_sweep()
            if sweep_count >= target:
                reached.set()

        manager._sweep = counting_sweep  # type: ignore[assignment]

        manager.start()
        try:
            # Wait deterministically for the target number of sweeps instead of
            # relying on wall-clock timing, which is flaky under CI load.
            await asyncio.wait_for(reached.wait(), timeout=5.0)
        finally:
            await manager.stop()

        assert sweep_count >= target

    @pytest.mark.anyio
    async def test_loop_survives_sweep_exception(self) -> None:
        """A failing sweep should not kill the heartbeat loop."""
        call_count = 0
        target = 2
        reached = asyncio.Event()
        registry = PeerRegistry()
        manager = HeartbeatManager(
            peer_registry=registry,
            send_callback=AsyncMock(),
            evict_callback=AsyncMock(),
            config=HeartbeatConfig(sweep_interval_sec=0.05),
        )

        async def failing_sweep() -> None:
            nonlocal call_count
            call_count += 1
            try:
                if call_count == 1:
                    raise RuntimeError("transient error")
                # Subsequent calls succeed (no-op)
            finally:
                if call_count >= target:
                    reached.set()

        manager._sweep = failing_sweep  # type: ignore[assignment]

        manager.start()
        try:
            # Wait deterministically for the loop to recover and call sweep
            # again instead of relying on wall-clock timing, which is flaky
            # under CI load (observed on Python 3.11 GitHub Actions runners).
            await asyncio.wait_for(reached.wait(), timeout=5.0)
        finally:
            await manager.stop()

        # Should have called sweep multiple times despite the first failure
        assert call_count >= target
