"""Unsupported future-launch transport must not silently downgrade or block node reads."""

from __future__ import annotations

import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from jmcore.crypto import mnemonic_to_seed
from jmcore.settings import validate_config_document
from typer.testing import CliRunner

from jmswap.lightning_context import lightning_context
from jmswap.lnd_config import LndEnvironment, render_lnd_conf
from jmswap.managed_lnd import ManagedLnd
from jmswap.managed_nodes import ManagedNodeError, NodeState, allocate_node, new_registry
from jmswap.managed_status import wallet_lightning_lines


def config(tmp_path: Path, url: str, **bitcoin: Any) -> Any:
    return validate_config_document(
        {
            "data_dir": str(tmp_path),
            "network_config": {"network": "regtest"},
            "wallet": {"address_type": "p2tr"},
            "lightning": {"mode": "managed"},
            "bitcoin": {"rpc_url": url, **bitcoin},
        }
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://core.local:8332",
        "socks5://core.local:8332",
        "http://user:private-url-password@core.local:8332",
        "http://core.local:8332/wallet/hidden",
        "http://core.local:8332?token=private-url-password",
        "http://core.local:8332#fragment",
        "http://core.local:8332?",
        f"http://{'a' * 56}.onion:8332",
        f"http://{'A' * 56}.ONION.:8332",
        "http://core.local:0",
        "http://core.local:",
        "http://cor\te.local:8332",
    ],
)
def test_unrepresentable_urls_refuse_rendering_without_leaking_url_inputs(
    tmp_path: Path,
    url: str,
) -> None:
    env = LndEnvironment.from_settings(
        config(tmp_path, url), network="regtest", lnd_binary=tmp_path / "lnd"
    )
    node = allocate_node(new_registry("regtest", "ab" * 32), 0, birthday=1, ports=(20001, 20002))
    with pytest.raises(ManagedNodeError) as error:
        render_lnd_conf(node, env)
    assert "private-url-password" not in str(error.value)
    assert "private-url-password" not in repr(env)
    assert "private-url-password" not in "".join(traceback.format_exception(error.value))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "target",
    [
        "https://core.local:8332",
        "user:private-url-password@core.local:8332",
        "core.local:8332/path",
        "core.local:8332?x",
        f"{'a' * 56}.onion:8332",
        "core.local:0",
        "cor\te.local:8332",
        "core.local:",
    ],
)
def test_manual_environment_without_a_recorded_error_still_requires_valid_transport(
    tmp_path: Path,
    target: str,
) -> None:
    env = LndEnvironment(network="regtest", lnd_binary=tmp_path / "lnd", bitcoind_host=target)
    with pytest.raises(ManagedNodeError):
        env.validate_for_launch()


@pytest.mark.parametrize(
    "url", ["core.local", "http://core.local/", "http://127.0.0.1:18443", "http://[::1]:18443/"]
)
def test_existing_explicit_http_and_schemeless_transports_remain_supported(
    tmp_path: Path, url: str
) -> None:
    env = LndEnvironment.from_settings(
        config(tmp_path, url), network="regtest", lnd_binary=tmp_path / "lnd"
    )
    env.validate_for_launch()


@pytest.mark.parametrize(
    "url",
    [
        "http://core.local:65536",
        "http://core.local:not-a-port",
        "http://user:private-url-password@[invalid",
    ],
)
def test_malformed_urls_keep_static_construction_errors(tmp_path: Path, url: str) -> None:
    with pytest.raises(ManagedNodeError) as error:
        LndEnvironment.from_settings(
            config(tmp_path, url), network="regtest", lnd_binary=tmp_path / "lnd"
        )
    assert "private-url-password" not in "".join(traceback.format_exception(error.value))


async def test_launch_refusal_precedes_stale_process_stop_or_any_node_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = lightning_context(config(tmp_path, "https://core.local:8332"), b"unit" * 16)
    node = allocate_node(
        new_registry("regtest", ctx.wallet_identity), 0, birthday=1, ports=(20001, 20002)
    )
    lnd = ManagedLnd(node, ctx.paths, ctx.env)
    stop_stale, spawn = AsyncMock(), AsyncMock()
    monkeypatch.setattr(lnd, "_stop_stale_instance", stop_stale)
    monkeypatch.setattr("asyncio.create_subprocess_exec", spawn)
    with pytest.raises(ManagedNodeError, match="direct HTTP"):
        await lnd.start()
    stop_stale.assert_not_awaited()
    spawn.assert_not_awaited()
    assert not ctx.paths.root.exists()
    running = SimpleNamespace(returncode=None)
    lnd._process = cast(Any, running)
    await lnd.start()  # A no-op for an already-owned running process, not a restart.
    assert lnd._process is running
    stop_stale.assert_not_awaited()


async def test_existing_node_status_remains_available_under_launch_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = config(tmp_path, "https://core.local:8332")
    seed = mnemonic_to_seed(" ".join(["all"] * 12), "")
    ctx = lightning_context(settings, seed, with_keys=False)
    registry = new_registry("regtest", ctx.wallet_identity)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    node.state = NodeState.ACTIVE
    ctx.store.update(lambda previous: registry)
    ctx.paths.node_dir(node).mkdir(parents=True)
    ctx.paths.tls_cert(node).write_bytes(b"test certificate")
    ctx.paths.macaroon(node).parent.mkdir(parents=True)
    ctx.paths.macaroon(node).write_bytes(b"test macaroon")
    responses = {
        "GetInfo": SimpleNamespace(
            synced_to_chain=True, block_height=100, num_active_channels=1, num_inactive_channels=0
        ),
        "WalletBalance": SimpleNamespace(confirmed_balance=100_000, unconfirmed_balance=0),
        "ChannelBalance": SimpleNamespace(
            local_balance=SimpleNamespace(sat=1000), remote_balance=SimpleNamespace(sat=2000)
        ),
        "PendingChannels": SimpleNamespace(
            pending_open_channels=[], waiting_close_channels=[], pending_force_closing_channels=[]
        ),
    }

    async def call(self: ManagedLnd, method: str, request: Any, **kwargs: Any) -> Any:
        return responses[method]

    monkeypatch.setattr(ManagedLnd, "call", call)
    launch = AsyncMock(side_effect=AssertionError("inspection must not launch LND"))
    monkeypatch.setattr(ManagedLnd, "start", launch)
    lines = await wallet_lightning_lines(settings, seed)
    assert any("100,000" in line and "1 channels" in line for line in lines)
    assert not any("Unavailable" in line for line in lines)
    launch.assert_not_awaited()


@pytest.mark.parametrize("command", ["init", "run"])
def test_cli_refusal_precedes_initialization_or_process_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    import jmswap.lightning_cli as lightning_cli
    from jmswap import managed_supervisor

    ctx = lightning_context(config(tmp_path, "https://core.local:8332"), b"unit" * 16)
    monkeypatch.setattr(lightning_cli, "_context", lambda *args: ctx)
    ownership = Mock(side_effect=AssertionError("must not acquire process ownership"))
    monkeypatch.setattr(ctx.store, "exclusive_processes", ownership)
    supervisor = Mock(side_effect=AssertionError("must not construct a launch supervisor"))
    monkeypatch.setattr(managed_supervisor, "Supervisor", supervisor)
    error = Mock()
    monkeypatch.setattr(lightning_cli.logger, "error", error)
    result = CliRunner().invoke(lightning_cli.app, [command])
    assert result.exit_code == 1, result.output
    error.assert_called_once()
    assert "direct HTTP" in error.call_args.args[0]
    ownership.assert_not_called()
    supervisor.assert_not_called()
    assert not ctx.paths.root.exists()


def test_structured_ipv6_core_and_tor_endpoints_are_bracketed(tmp_path: Path) -> None:
    settings = config(tmp_path, "http://[::1]:18443")
    settings.tor.socks_host = "::1"
    settings.tor.control_host = "::1"
    env = LndEnvironment.from_settings(settings, network="regtest", lnd_binary=tmp_path / "lnd")
    env.validate_for_launch()
    assert env.bitcoind_host == "[::1]:18443"
    assert env.tor_socks == "[::1]:9050"
    assert env.tor_control == "[::1]:9051"
