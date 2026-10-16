from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from jmcore.constants import GENESIS_BLOCK_HASHES
from maker.channel_ring import _chain_hash as maker_chain_hash
from taker.channel_ring import _chain_hash as taker_chain_hash

from tests.e2e.test_cofunded_ring_e2e import (
    MAX_REPLENISHMENT_RECEIVE_INDEX,
    RING_MAKER_MNEMONICS,
    SettlementRouteChannel,
    assert_retained_taker_podle_state,
    compose,
    fund_buyout_taker_input,
    replenish_ring_maker_wallets_for_buyout,
    ring_maker_replenishment_address,
    taker_config,
    numeric_short_channel_id,
    route_hop_channel_ids,
    settlement_route_is_contiguous,
    short_channel_id_from_position,
)


def test_buyout_maker_replenishment_uses_distinct_regtest_receive_indices() -> None:
    # Index one is the previously confirmed source-mixdepth replenishment.
    first_addresses = (
        "bcrt1p90m9u9f2r9fg6vk5ykdma3sw8jtgfcwvjtaj7yw0p9qcxqgp3eeq9x4g7s",
        "bcrt1pvurh0sjklz9saysshp2skcvlspj26yuqg7wej8g83ql0yvkkvnzqs50lpf",
        "bcrt1p73ua0c34yn4xlpdjzakqplgtdc9yp92atrdzgj4ltgv9exs3fa0q0eux2z",
    )
    for service, previous in zip(RING_MAKER_MNEMONICS, first_addresses, strict=True):
        assert ring_maker_replenishment_address(service, 1) == previous
        assert ring_maker_replenishment_address(service, 2) != previous
    for reserved_or_undiscoverable in (0, MAX_REPLENISHMENT_RECEIVE_INDEX + 1):
        with pytest.raises(ValueError, match="reserved or undiscoverable"):
            ring_maker_replenishment_address("lnd-maker1", reserved_or_undiscoverable)


@pytest.mark.asyncio
async def test_split_buyout_replenishes_only_live_makers_on_fresh_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = "tests.e2e.test_cofunded_ring_e2e"
    rpc = AsyncMock(side_effect=[{"chain": "regtest"}, "first", "second"])
    mine = AsyncMock()
    monkeypatch.setattr(f"{module}.bootstrap_bitcoin_rpc", rpc)
    monkeypatch.setattr(f"{module}.mine_and_sync", mine)
    nicks = frozenset({"maker2", "maker3"})
    taker = Mock()
    taker.directory_client.fetch_orderbook = AsyncMock(
        return_value=[
            Mock(counterparty=nick, minsize=100_000, maxsize=1_000_000)
            for nick in nicks
        ]
    )

    await replenish_ring_maker_wallets_for_buyout(
        taker,
        nicks,
        Mock(taker_utxo_age=3),
        "mining-address",
        receive_index=2,
        excluded_service="lnd-maker1",
    )

    assert rpc.await_count == 3
    assert [call.args[0] for call in rpc.await_args_list] == [
        "getblockchaininfo",
        "sendtoaddress",
        "sendtoaddress",
    ]
    funded_addresses = {call.args[1][0] for call in rpc.await_args_list[1:]}
    assert funded_addresses == {
        ring_maker_replenishment_address(service, 2)
        for service in ("lnd-maker2", "lnd-maker3")
    }
    mine.assert_awaited_once_with(4, "mining-address")


def test_ring_test_compose_inherits_runner_ipam_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "ring-ipam.yml"
    monkeypatch.setenv("RING_E2E_IPAM_FILE", str(override))
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(subprocess, "run", run)

    compose("ps", "--all")

    cmd = run.call_args.args[0]
    assert cmd[cmd.index("-f") + 1].endswith("docker-compose.ring-e2e.yml")
    assert cmd[-6:] == ["-f", str(override), "--profile", "ring-e2e", "ps", "--all"]
    assert run.call_args.kwargs["env"]["RING_E2E_IPAM_FILE"] == str(override)


@pytest.mark.asyncio
async def test_buyout_funding_requires_regtest_before_wallet_or_chain_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = Mock()
    wallet.get_new_address_verified = AsyncMock()
    rpc = AsyncMock(return_value={"chain": "signet"})
    monkeypatch.setattr("tests.e2e.test_cofunded_ring_e2e.bootstrap_bitcoin_rpc", rpc)

    with pytest.raises(RuntimeError, match="outside regtest"):
        await fund_buyout_taker_input(wallet)

    rpc.assert_awaited_once_with("getblockchaininfo")
    wallet.get_new_address_verified.assert_not_awaited()


@pytest.mark.asyncio
async def test_buyout_funding_requires_one_exact_p2tr_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = "tests.e2e.test_cofunded_ring_e2e"
    wallet = Mock()
    wallet.get_new_address_verified = AsyncMock(return_value="fresh-test-address")
    script = b"\x51\x20" + b"\x02" * 32
    monkeypatch.setattr(
        f"{module}.address_to_scriptpubkey_for_network", lambda *_: script
    )
    txid = "a" * 64
    rpc = AsyncMock(side_effect=[{"chain": "regtest"}, txid, "raw-transaction"])
    monkeypatch.setattr(f"{module}.bootstrap_bitcoin_rpc", rpc)
    monkeypatch.setattr(
        f"{module}.parse_transaction",
        lambda _: Mock(
            outputs=[
                Mock(script=b"other", value=1_500_000),
                Mock(script=script, value=1_500_000),
            ]
        ),
    )

    assert await fund_buyout_taker_input(wallet) == f"{txid}:1"
    wallet.get_new_address_verified.assert_awaited_once_with(0)
    assert rpc.await_args_list[1].args[0] == "sendtoaddress"
    assert rpc.await_args_list[1].kwargs["wallet"] == "ring-funder"


def test_ring_route_helpers_accept_only_numeric_scids_and_contiguous_paths() -> None:
    assert numeric_short_channel_id("1099511627777") == 1099511627777
    assert short_channel_id_from_position(500, 12, 2) == (500 << 40) | (12 << 16) | 2
    assert route_hop_channel_ids({"hops": [{"chan_id": "42"}, {"chan_id": 43}]}) == (
        42,
        43,
    )
    with pytest.raises(RuntimeError, match="nonnumeric"):
        numeric_short_channel_id("000000000000002a")

    route = (
        SettlementRouteChannel("first", "buyer", "router"),
        SettlementRouteChannel("second", "router", "counterparty"),
    )
    assert settlement_route_is_contiguous(route, "buyer", "counterparty")
    assert not settlement_route_is_contiguous(route, "counterparty", "buyer")
    assert not settlement_route_is_contiguous(
        (
            SettlementRouteChannel("first", "buyer", "router"),
            SettlementRouteChannel("second", "other", "counterparty"),
        ),
        "buyer",
        "counterparty",
    )


def test_ring_fixture_has_mixed_maker_roles_and_safe_ring_deadlines() -> None:
    compose_path = (
        Path(__file__).resolve().parents[1] / "jmswap/docker-compose.ring-e2e.yml"
    )
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    for name in ("ring-maker1", "ring-maker2", "ring-maker3"):
        environment = compose["services"][name]["environment"]
        assert "MAKER__CHANNEL_RING__LND_GRPC_URL" not in environment
        assert (
            environment["MAKER__CHANNEL_RING__NODE_BINDING_DIRECTORY"]
            == "/node-bindings"
        )
        assert any(
            volume.endswith("/node-bindings:/node-bindings")
            for volume in compose["services"][name]["volumes"]
        )
        assert environment["MAKER__CHANNEL_RING__ENABLED"] == "true"
        assert int(environment["MAKER__CHANNEL_RING__SETUP_TIMEOUT_SECONDS"]) == 600
        assert int(environment["MAKER__CHANNEL_RING__HOLD_SAFETY_MARGIN_SECONDS"]) == 30
        assert int(environment["MAKER__CHANNEL_RING__MAKER_SETUP_HOLD_SECONDS"]) == 660
        assert int(environment["MAKER__PRE_SIGN_TIMEOUT_SEC"]) >= 660
        assert int(environment["MAKER__SESSION_TIMEOUT_SEC"]) >= 660

    maker4 = compose["services"]["ring-maker4"]
    maker4_environment = maker4["environment"]
    assert maker4_environment["MAKER__CHANNEL_RING__ENABLED"] == "false"
    assert maker4_environment["TOR__CONTROL_HOST"] == "ring-tor"
    assert maker4_environment["TOR__CONTROL_PORT"] == 9051
    assert maker4_environment["TOR__TARGET_HOST"] == "lnd-maker4"
    assert "MAKER__ONION_SERVING_PORT" not in maker4_environment
    assert "TOR__CONTROL_ENABLED" not in maker4_environment
    setup_timeout = int(
        maker4_environment["MAKER__CHANNEL_RING__SETUP_TIMEOUT_SECONDS"]
    )
    safety_margin = int(
        maker4_environment["MAKER__CHANNEL_RING__HOLD_SAFETY_MARGIN_SECONDS"]
    )
    required_hold = int(
        maker4_environment["MAKER__CHANNEL_RING__MAKER_SETUP_HOLD_SECONDS"]
    )
    assert required_hold == 660
    assert required_hold > setup_timeout + safety_margin
    assert int(maker4_environment["MAKER__SESSION_TIMEOUT_SEC"]) == required_hold
    assert int(maker4_environment["MAKER__PRE_SIGN_TIMEOUT_SEC"]) == required_hold
    for name in ("ring-maker1", "ring-maker2", "ring-maker3", "ring-maker4"):
        environment = compose["services"][name]["environment"]
        assert int(environment["MAKER__MIN_SIZE"]) == 100_000

    assert compose["services"]["lnd-maker4"]["environment"]["LND_NODE"] == "lnd-maker4"
    assert "ring-onion-maker4" in compose["services"]["ring-tor"]["depends_on"]

    host_path = compose_path.with_name("docker-compose.ring-e2e.host.yml")
    host = yaml.safe_load(host_path.read_text(encoding="utf-8"))
    assert host["services"]["lnd-maker4"]["ports"] == [
        "127.0.0.1:${RING_E2E_MAKER4_GRPC_PORT:-25009}:10009"
    ]


def test_host_taker_enrollment_does_not_replace_maker_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is a config ownership test, not a live LND test. Never query an
    # operator's retained node when the unit suite constructs the config.
    monkeypatch.setattr(
        "tests.e2e.test_cofunded_ring_e2e.onion_endpoint",
        lambda _service: "a" * 56 + ".onion:9735",
    )
    wallet_data = tmp_path / "host-taker"
    config = taker_config(wallet_data, journal_dir=tmp_path / "attempt")
    assert config.data_dir == wallet_data
    assert config.channel_ring.node_binding_directory == wallet_data / "node-bindings"
    assert config.channel_ring.persistence_directory == tmp_path / "attempt" / "rings"


def test_retained_taker_requires_spent_podle_claim(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="absent or empty ledger"):
        assert_retained_taker_podle_state(tmp_path)

    state = tmp_path / "cmtdata" / "commitments.json"
    state.parent.mkdir(exist_ok=True)
    state.write_text(json.dumps({"used": []}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="absent or empty ledger"):
        assert_retained_taker_podle_state(tmp_path)

    state.write_text("{invalid", encoding="utf-8")
    with pytest.raises(RuntimeError, match="absent or empty ledger"):
        assert_retained_taker_podle_state(tmp_path)

    state.write_text(json.dumps({"used": ["a" * 64]}), encoding="utf-8")
    assert_retained_taker_podle_state(tmp_path)


@pytest.mark.parametrize("network", ["mainnet", "testnet", "signet", "regtest"])
def test_ring_channel_acceptors_use_canonical_genesis_hash(network: str) -> None:
    # Checked against the live signet Core genesis, not the earlier malformed
    # local duplicate that prevented all signet channel acceptors from arming.
    assert GENESIS_BLOCK_HASHES["signet"] == (
        "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6"
    )
    expected = bytes.fromhex(GENESIS_BLOCK_HASHES[network])[::-1]
    assert maker_chain_hash(network) == expected
    assert taker_chain_hash(network) == expected


def test_ring_fixture_funds_all_four_maker_wallets() -> None:
    funding_path = (
        Path(__file__).resolve().parents[1] / "scripts/fund-ring-e2e-wallets.sh"
    )
    funding = funding_path.read_text(encoding="utf-8")
    assert "bcrt1p8wpt9v4frpf3tkn0srd97pksgsxc5hs52lafxwru9kgeephvs7rqjeprhg" in funding


def test_ring_runner_runs_direct_heartbeat_with_cofunded_ring_test() -> None:
    runner_path = Path(__file__).resolve().parents[1] / "scripts/run-ring-e2e.sh"
    runner = runner_path.read_text(encoding="utf-8")
    heartbeat = '"${PROJECT_ROOT}/tests/e2e/test_direct_heartbeat_e2e.py"'
    cofunded = '"${PROJECT_ROOT}/tests/e2e/test_cofunded_ring_e2e.py"'
    assert runner.index(heartbeat) < runner.index(cofunded)
    assert "-m ring_e2e --fail-on-skip --no-cov -v --timeout=3600" in runner
    assert runner.index("RING_NODE_ENROLLMENT_PUBKEY=$node_id") < runner.index(
        'ring_compose up -d --no-deps "${makers[@]}"'
    )
    assert runner.index(
        'wait_for_ring_lnd_sync "${nodes[@]}"',
        runner.index("wait_for_healthy ring_compose 300"),
    ) < runner.index("for maker in 1 2 3; do")
    assert runner.index("--force-recreate ring-tor") < runner.index(
        "for maker in 1 2 3; do"
    )
    assert runner.index(
        '    wait_for_ring_lnd_sync "${nodes[@]}"',
        runner.index('ring_compose up -d --no-deps "${makers[@]}"'),
    ) < runner.index('"${PROJECT_ROOT}/tests/e2e/test_direct_heartbeat_e2e.py"')
    assert (
        'info.get("synced_to_chain") is True and info.get("wallet_synced") is True'
        in runner
    )
    entrypoint = (runner_path.parent / "ring-e2e-maker.sh").read_text()
    assert "MAKER__CHANNEL_RING__NODES" in entrypoint
    assert "MAKER__CHANNEL_RING__MIXDEPTH_NODES" in entrypoint
    assert 'if [ -n "${RING_NODE_ENROLLMENT_PUBKEY:-}" ]' in entrypoint
    lnd_entrypoint = (runner_path.parent / "ring-e2e-lnd.sh").read_text()
    assert "--protocol.option-scid-alias" in lnd_entrypoint


def test_ring_e2e_helpers_keep_public_channels_and_onion_failures_bounded() -> None:
    project_root = Path(__file__).resolve().parents[1]
    cofunded = (project_root / "tests/e2e/test_cofunded_ring_e2e.py").read_text(
        encoding="utf-8"
    )
    heartbeat = (project_root / "tests/e2e/test_direct_heartbeat_e2e.py").read_text(
        encoding="utf-8"
    )

    assert 'if private:\n        args.append("--channel_type=taproot")' in cofunded
    # Discovery plus the onion wait must end with the diagnostic error, not a
    # bare pytest timeout.
    assert "ONION_READY_TIMEOUT = 180.0" in heartbeat
    assert "DISCOVERY_TIMEOUT = 120.0" in heartbeat
    assert "timeout=min(CONNECTION_TIMEOUT, remaining)" in heartbeat
    assert "@pytest.mark.timeout(420)" in heartbeat
    assert "(attempts={attempts}, last_exception={last_exception_type})" in heartbeat
    assert "timeout: float = 300" in cofunded
    assert "backoff = min(backoff * 2, 10.0)" in cofunded
    assert "peer_state_observed={peer_state_observed}" in cofunded
    assert (
        'HOST_COMPOSE_FILE = ROOT / "jmswap" / "docker-compose.ring-e2e.host.yml"'
        in cofunded
    )
    assert "str(HOST_COMPOSE_FILE)" in cofunded
