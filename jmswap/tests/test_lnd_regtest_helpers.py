"""Exercise funding retries and restart catch-up without a Docker stack."""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import test_lnd_buyout_regtest as regtest

NODE_ID = "02" + "11" * 32
RPC_PREFIX = "compose exec failed: [lncli] rpc error: code = Unknown desc = "


@pytest.fixture
def echo_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> list[asyncio.subprocess.Process]:
    create_process = regtest.asyncio.create_subprocess_exec
    processes: list[asyncio.subprocess.Process] = []

    async def echo_process(*args: Any, **kwargs: Any) -> Any:
        assert args[:3] == ("docker", "exec", "-i")
        process = await create_process(
            sys.executable,
            "-u",
            "-c",
            "import os\nwhile data := os.read(0, 65536): os.write(1, data)",
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(regtest.asyncio, "create_subprocess_exec", echo_process)
    return processes


async def test_grpc_relay_progresses_while_controller_runs_blocking_rpc(
    echo_relay: list[asyncio.subprocess.Process],
) -> None:
    stack = MagicMock()
    stack.container.return_value = "test-lnd"
    async with regtest.grpc_endpoint(stack, "alice") as endpoint:
        host, port = endpoint.rsplit(":", 1)
        # Test controllers use synchronous Core/lncli operations while gRPC
        # sessions stay open. Their bridge must keep moving bytes independently.
        with socket.create_connection((host, int(port)), timeout=2) as connection:
            connection.sendall(b"probe")
            assert connection.recv(5) == b"probe"
    assert echo_relay and all(process.returncode is not None for process in echo_relay)
    assert not any(thread.name == "lnd-regtest-relay" for thread in threading.enumerate())


async def test_grpc_relay_caller_cancellation_reaps_children_and_joins_thread(
    echo_relay: list[asyncio.subprocess.Process],
) -> None:
    stack = MagicMock()
    entered = asyncio.Event()

    async def controller() -> None:
        async with regtest.grpc_endpoint(stack, "alice") as endpoint:
            host, port = endpoint.rsplit(":", 1)
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"probe")
                await writer.drain()
                assert await reader.readexactly(5) == b"probe"
                entered.set()
                await asyncio.Future()
            finally:
                writer.close()
                await writer.wait_closed()

    task = asyncio.create_task(controller())
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert echo_relay and all(process.returncode is not None for process in echo_relay)
    assert not any(thread.name == "lnd-regtest-relay" for thread in threading.enumerate())


async def test_grpc_relay_startup_failure_is_propagated_and_thread_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse_bind(*args: Any, **kwargs: Any) -> Any:
        raise OSError("bind refused")

    monkeypatch.setattr(regtest.asyncio, "start_server", refuse_bind)
    with pytest.raises(OSError, match="bind refused"):
        async with regtest.grpc_endpoint(MagicMock(), "alice"):
            pytest.fail("failed relay must not yield an endpoint")
    assert not any(thread.name == "lnd-regtest-relay" for thread in threading.enumerate())


async def test_grpc_relay_shutdown_aborts_a_stalled_socket_close(
    echo_relay: list[asyncio.subprocess.Process],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wait_closed = asyncio.StreamWriter.wait_closed

    async def stalled_socket(self: asyncio.StreamWriter) -> None:
        if self.get_extra_info("peername") is not None:
            await asyncio.Future()
        else:
            await wait_closed(self)

    monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", stalled_socket)
    async with regtest.grpc_endpoint(MagicMock(), "alice") as endpoint:
        host, port = endpoint.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=2) as connection:
            connection.sendall(b"probe")
            assert connection.recv(5) == b"probe"
    assert all(process.returncode is not None for process in echo_relay)
    assert not any(thread.name == "lnd-regtest-relay" for thread in threading.enumerate())


async def test_grpc_relay_exit_closes_a_client_that_remains_open(
    echo_relay: list[asyncio.subprocess.Process],
) -> None:
    connection = None
    try:
        async with regtest.grpc_endpoint(MagicMock(), "alice") as endpoint:
            host, port = endpoint.rsplit(":", 1)
            connection = socket.create_connection((host, int(port)), timeout=2)
            connection.sendall(b"probe")
            assert connection.recv(5) == b"probe"
        assert connection.recv(1) == b""
    finally:
        if connection is not None:
            connection.close()
    assert echo_relay and all(process.returncode is not None for process in echo_relay)
    assert not any(thread.name == "lnd-regtest-relay" for thread in threading.enumerate())


@pytest.mark.parametrize(
    "detail", ["peer disconnected", f"peer {NODE_ID} disconnected", f"peer {NODE_ID} is not online"]
)
def test_pre_broadcast_peer_error_reconnects_then_retries(
    detail: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = MagicMock(node_ids={"bob": NODE_ID})
    opened = {"funding_txid": "22" * 32, "output_index": 0}
    calls = 0

    def lncli(node: str, command: str, *args: str) -> Any:
        nonlocal calls
        assert node == "alice"
        if command == "listpeers":
            return {"peers": [{"pub_key": NODE_ID}]} if calls == 0 else {"peers": []}
        if command == "connect":
            assert args == (f"{NODE_ID}@bob:9735",)
            return {}
        assert command == "openchannel"
        assert args == ("--node_key", NODE_ID, "--local_amt", "100000")
        calls += 1
        if calls == 1:
            raise regtest.RegtestError(RPC_PREFIX + detail)
        return opened

    stack.lncli.side_effect = lncli
    monkeypatch.setattr(regtest.time, "sleep", MagicMock())
    assert (
        regtest._open_channel_rpc(
            cast(regtest.BuyoutRegtest, stack), "alice", "bob", "--local_amt", "100000"
        )
        == opened
    )
    assert calls == 2
    stack.lncli.assert_any_call("alice", "connect", f"{NODE_ID}@bob:9735")


@pytest.mark.parametrize(
    "detail",
    [
        "stream disconnected after sending update",
        "transport is not connected",
        "context deadline exceeded",
        "insufficient funds",
        "peer disconnected; funding state uncertain",
    ],
)
def test_ambiguous_or_other_funding_error_is_never_retried(detail: str) -> None:
    stack = MagicMock(node_ids={"bob": NODE_ID})
    error = regtest.RegtestError(RPC_PREFIX + detail)
    stack.lncli.side_effect = [{"peers": [{"pub_key": NODE_ID}]}, error]
    with pytest.raises(regtest.RegtestError) as raised:
        regtest._open_channel_rpc(cast(regtest.BuyoutRegtest, stack), "alice", "bob")
    assert raised.value is error
    assert stack.lncli.call_count == 2  # listpeers, then exactly one openchannel


def test_pre_broadcast_disconnect_retry_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    stack = MagicMock(node_ids={"bob": NODE_ID})
    error = regtest.RegtestError(RPC_PREFIX + "peer disconnected")
    outcomes: list[Any] = [{"peers": []}, {}, error] * 3
    stack.lncli.side_effect = outcomes
    sleep = MagicMock()
    monkeypatch.setattr(regtest.time, "sleep", sleep)
    with pytest.raises(regtest.RegtestError) as raised:
        regtest._open_channel_rpc(cast(regtest.BuyoutRegtest, stack), "alice", "bob")
    assert raised.value is error
    assert stack.lncli.call_count == 9  # Three checks, connects, and opens.
    assert sleep.call_count == 2


@pytest.mark.parametrize("stale", ["height", "hash", "wallet", "chain", "changed_tip"])
def test_funding_readiness_requires_a_fresh_stable_tip_without_mining(
    monkeypatch: pytest.MonkeyPatch,
    stale: str,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(regtest.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        regtest.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )
    stack = MagicMock()
    core = {"blocks": 141, "bestblockhash": "11" * 32}
    reads = 0

    def bitcoin(command: str, **kwargs: Any) -> Any:
        nonlocal reads
        assert command == "getblockchaininfo"
        assert kwargs["timeout"] > 0
        reads += 1
        if stale == "changed_tip" and reads == 2:
            return {**core, "bestblockhash": "33" * 32}
        return core

    def lncli(node: str, command: str, **kwargs: Any) -> Any:
        assert (node, command) == ("alice", "getinfo")
        assert kwargs["timeout"] > 0
        info = {
            "identity_pubkey": NODE_ID,
            "block_height": 141,
            "block_hash": "11" * 32,
            "wallet_synced": True,
            "synced_to_chain": True,
        }
        if clock[0] == 0:
            field = {
                "height": "block_height",
                "hash": "block_hash",
                "wallet": "wallet_synced",
                "chain": "synced_to_chain",
            }.get(stale)
            if field is not None:
                info[field] = {"height": 140, "hash": "33" * 32}.get(stale, False)
        return info

    stack.bitcoin.side_effect = bitcoin
    stack.lncli.side_effect = lncli
    assert regtest._wait_for_sync(stack, "alice") == NODE_ID
    assert clock[0] == 0.5
    assert reads == 4


@pytest.fixture
def restart_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [0.0]

    async def advance(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(regtest.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(regtest.asyncio, "sleep", advance)
    return clock


def restart_stack() -> MagicMock:
    stack = MagicMock()
    core: dict[str, Any] = {"blocks": 141, "bestblockhash": "11" * 32}

    def bitcoin(command: str, *args: str, **kwargs: Any) -> Any:
        assert 0 < kwargs["timeout"] <= 6
        if command == "getblockchaininfo":
            return core
        if command == "getnewaddress":
            assert kwargs["wallet"] is True
            return "bcrt1ptestprobe"
        assert command == "generatetoaddress"
        assert args == ("1", "bcrt1ptestprobe")
        assert kwargs["wallet"] is True
        core["blocks"] += 1
        core["bestblockhash"] = f"{core['blocks']:064x}"
        stack.lncli.return_value["block_height"] = core["blocks"]
        stack.lncli.return_value["block_hash"] = core["bestblockhash"]
        return ["22" * 32]

    stack.bitcoin.side_effect = bitcoin
    stack.lncli.return_value = {
        "block_height": 141,
        "block_hash": core["bestblockhash"],
        "wallet_synced": True,
        "synced_to_chain": True,
    }
    return stack


def probe_calls(stack: MagicMock) -> list[Any]:
    return [call for call in stack.bitcoin.call_args_list if call.args[0] == "generatetoaddress"]


async def test_restart_catchup_requires_fresh_block_even_with_ready_flags(
    restart_clock: list[float],
) -> None:
    stack = restart_stack()
    await regtest._wait_for_post_restart_catchup(cast(regtest.BuyoutRegtest, stack), timeout=6)
    assert len(probe_calls(stack)) == 1
    assert {call.args[0] for call in stack.lncli.call_args_list} == set(regtest.NODES)
    assert all(0 < call.kwargs["timeout"] <= 6 for call in stack.lncli.call_args_list)


@pytest.mark.parametrize(
    "field,value",
    [
        ("wallet_synced", False),
        ("wallet_synced", None),
        ("wallet_synced", "true"),
        ("synced_to_chain", False),
        ("block_height", 140),
        ("block_hash", "33" * 32),
    ],
)
async def test_restart_catchup_requires_wallet_readiness_and_matching_tip(
    restart_clock: list[float], field: str, value: Any
) -> None:
    stack = restart_stack()
    ready = stack.lncli.return_value

    def recover(node: str, command: str, **kwargs: Any) -> Any:
        # Keep the bad observation after mining too, so height advancement
        # alone cannot make this test pass without enforcing the safety gate.
        # Alice is ready throughout; the barrier must check Bob too.
        return {**ready, field: value} if node == "bob" and restart_clock[0] < 1 else ready

    stack.lncli.side_effect = recover
    await regtest._wait_for_post_restart_catchup(cast(regtest.BuyoutRegtest, stack), timeout=6)
    assert len(probe_calls(stack)) == 1
    assert restart_clock[0] == 1


async def test_restart_catchup_rechecks_core_tip(restart_clock: list[float]) -> None:
    stack = restart_stack()
    bitcoin = stack.bitcoin.side_effect
    reads = 0

    def changed_tip(command: str, *args: str, **kwargs: Any) -> Any:
        nonlocal reads
        if command == "getblockchaininfo":
            reads += 1
            # After the probe, nodes match the snapshot but Core changes
            # during those RPCs. The barrier must check again, not return.
            return (
                {"blocks": 143, "bestblockhash": "33" * 32}
                if reads == 4
                else bitcoin(command, *args, **kwargs)
            )
        return bitcoin(command, *args, **kwargs)

    stack.bitcoin.side_effect = changed_tip
    await regtest._wait_for_post_restart_catchup(cast(regtest.BuyoutRegtest, stack), timeout=6)
    assert reads == 6
    assert restart_clock[0] == 1
    assert len(probe_calls(stack)) == 1


async def test_restart_catchup_retries_rpc_error_without_mining_unknown_state(
    restart_clock: list[float],
) -> None:
    stack = restart_stack()
    ready = stack.lncli.return_value
    calls = 0

    def reconnect(node: str, command: str, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise regtest.RegtestError("reconnecting")
        return ready

    stack.lncli.side_effect = reconnect
    await regtest._wait_for_post_restart_catchup(cast(regtest.BuyoutRegtest, stack), timeout=6)
    assert len(probe_calls(stack)) == 1
    assert restart_clock[0] >= 1


async def test_restart_catchup_has_spaced_probe_and_deadline_budgets(
    restart_clock: list[float],
) -> None:
    stack = restart_stack()
    stack.lncli.return_value["wallet_synced"] = False
    bitcoin = stack.bitcoin.side_effect
    probe_times = []

    def record_probe(command: str, *args: str, **kwargs: Any) -> Any:
        if command == "generatetoaddress":
            probe_times.append(restart_clock[0])
        return bitcoin(command, *args, **kwargs)

    stack.bitcoin.side_effect = record_probe
    with pytest.raises(regtest.RegtestError, match=r"probes=3/3.*wallet_synced.*False"):
        await regtest._wait_for_post_restart_catchup(cast(regtest.BuyoutRegtest, stack), timeout=6)
    assert probe_times == [0.0, 1.5, 3.0]
    assert restart_clock[0] == 6


@pytest.mark.parametrize("wallet_ready", [False, True])
async def test_restart_catchup_counts_uncertain_mining_replies_against_budget(
    restart_clock: list[float],
    wallet_ready: bool,
) -> None:
    stack = restart_stack()
    stack.lncli.return_value["wallet_synced"] = wallet_ready
    bitcoin = stack.bitcoin.side_effect

    def lost_reply(command: str, *args: str, **kwargs: Any) -> Any:
        if command == "generatetoaddress":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return bitcoin(command, *args, **kwargs)

    stack.bitcoin.side_effect = lost_reply
    with pytest.raises(regtest.RegtestError, match="probes=2/2"):
        await regtest._wait_for_post_restart_catchup(
            cast(regtest.BuyoutRegtest, stack), timeout=6, max_probe_blocks=2
        )
    assert len(probe_calls(stack)) == 2


async def test_restart_catchup_rpc_cannot_outlive_overall_deadline(
    restart_clock: list[float],
) -> None:
    stack = restart_stack()

    def timed_out(command: str, *args: str, **kwargs: Any) -> Any:
        restart_clock[0] += kwargs["timeout"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    stack.bitcoin.side_effect = timed_out
    with pytest.raises(regtest.RegtestError, match="RPC error=TimeoutExpired"):
        await regtest._wait_for_post_restart_catchup(cast(regtest.BuyoutRegtest, stack), timeout=6)
    assert restart_clock[0] == 6
    assert stack.bitcoin.call_count == 1
    assert not probe_calls(stack)


async def test_restart_catchup_recovers_generated_probe_after_lost_reply(
    restart_clock: list[float],
) -> None:
    stack = restart_stack()
    bitcoin = stack.bitcoin.side_effect

    def lost_reply(command: str, *args: str, **kwargs: Any) -> Any:
        result = bitcoin(command, *args, **kwargs)
        if command == "generatetoaddress":
            raise regtest.RegtestError("lost mining reply")
        return result

    stack.bitcoin.side_effect = lost_reply
    await regtest._wait_for_post_restart_catchup(
        cast(regtest.BuyoutRegtest, stack), timeout=6, max_probe_blocks=1
    )
    assert len(probe_calls(stack)) == 1


@pytest.mark.parametrize("client", ["bitcoin", "lncli"])
def test_regtest_clients_forward_optional_rpc_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: str
) -> None:
    compose = MagicMock(return_value="{}")
    monkeypatch.setattr(regtest.BuyoutRegtest, "compose", compose)
    stack = regtest.BuyoutRegtest("isolated", tmp_path, {})
    command = stack.bitcoin if client == "bitcoin" else stack.lncli
    args = ("getblockchaininfo",) if client == "bitcoin" else ("alice", "getinfo")
    command(*args)
    assert compose.call_args.kwargs == {}
    command(*args, timeout=0.25)
    assert compose.call_args.kwargs == {"timeout": 0.25}


@pytest.mark.parametrize("timeout,probes", [(0, 3), (-1, 3), (6, 0), (6, -1)])
async def test_restart_catchup_rejects_invalid_bounds(timeout: float, probes: int) -> None:
    stack = restart_stack()
    with pytest.raises(ValueError, match="bounds must be positive"):
        await regtest._wait_for_post_restart_catchup(
            cast(regtest.BuyoutRegtest, stack), timeout=timeout, max_probe_blocks=probes
        )
    stack.bitcoin.assert_not_called()
    stack.lncli.assert_not_called()
