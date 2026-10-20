"""The anchor reserve of a mixdepth's Lightning node."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig, ChannelRingNodeSettings
from jmcore.crypto import mnemonic_to_seed
from jmcore.settings import JoinMarketSettings

from jmswap import lightning_reserve
from jmswap.lightning_context import wallet_identity_of
from jmswap.lightning_reserve import (
    CLAIM_HOLD_SECONDS,
    STALE_SECONDS,
    ReserveError,
    ReserveFunder,
    check_reserve_amount,
    default_reserve_amount,
    managed_reserve_target,
    reserve_funding_address,
)
from jmswap.managed_nodes import (
    ManagedNodePaths,
    NodeState,
    RegistryStore,
    allocate_node,
    new_registry,
)

MNEMONIC = " ".join(["abandon"] * 11 + ["about"])
TAPROOT = scriptpubkey_to_address(bytes([0x51, 0x20]) + b"\x01" * 32, "signet")
SEGWIT_V0 = scriptpubkey_to_address(bytes([0x00, 0x14]) + b"\x02" * 20, "signet")
NODE = ChannelRingNodeSettings(
    lnd_grpc_url="https://127.0.0.1:10009",
    lnd_tls_cert_path=Path("tls.cert"),
    lnd_macaroon_path=Path("admin.macaroon"),
    onion_endpoint="a" * 56 + ".onion:9735",
)


def write_registry(data_dir: Path, *, states: tuple[NodeState, ...]) -> ManagedNodePaths:
    """A signet registry for MNEMONIC with one node per state, from mixdepth 0."""
    identity = wallet_identity_of(mnemonic_to_seed(MNEMONIC, ""))
    paths = ManagedNodePaths.for_wallet(data_dir, "signet", identity)
    registry = new_registry("signet", identity)
    for mixdepth, state in enumerate(states):
        node = allocate_node(
            registry, mixdepth, birthday=1, ports=(21000 + 2 * mixdepth, 21001 + 2 * mixdepth)
        )
        node.state = state
        node.identity_pubkey = "02" + f"{mixdepth:02x}" * 32
        node.onion_endpoint = NODE.onion_endpoint
    RegistryStore(paths).update(lambda current: registry)
    return paths


@pytest.mark.parametrize(
    ("balance", "expected"),
    [(0, 25_000), (10_000, 20_000), (24_000, 6_000), (25_000, 0), (40_000, 0)],
)
def test_the_default_amount_reaches_the_minimum_without_passing_the_maximum(
    balance: int, expected: int
) -> None:
    # A maximum close to the minimum caps the top-up at the remaining room.
    assert (
        default_reserve_amount(balance, reserve_min_sats=25_000, reserve_max_sats=30_000)
        == expected
    )


def test_an_amount_above_the_maximum_or_not_positive_is_refused() -> None:
    check_reserve_amount(10_000, 140_000, reserve_max_sats=150_000)
    with pytest.raises(ReserveError, match="above reserve_max_sats"):
        check_reserve_amount(10_000, 140_001, reserve_max_sats=150_000)
    with pytest.raises(ReserveError, match="positive"):
        check_reserve_amount(10_000, 0, reserve_max_sats=150_000)


class FakeBackend:
    def __init__(self, balance: int, address: str = TAPROOT, error: Exception | None = None):
        self.balance = balance
        self.address = address
        self.error = error
        self.addresses = 0
        self.closed = False

    async def onchain_balance_sats(self, *, timeout_seconds: float) -> int:
        if self.error is not None:
            raise self.error
        return self.balance

    async def new_taproot_address(self, *, timeout_seconds: float) -> str:
        self.addresses += 1
        return self.address

    async def close(self) -> None:
        self.closed = True


def _use(monkeypatch: pytest.MonkeyPatch, backend: FakeBackend) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def from_paths(*args: Any, **kwargs: Any) -> FakeBackend:
        calls.append((*args, kwargs["network"]))
        return backend

    monkeypatch.setattr(lightning_reserve.LndBackend, "from_paths", from_paths)
    return calls


async def test_a_funded_reserve_asks_for_no_address(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unconfirmed coins count, so a pending funding is not repeated.
    backend = FakeBackend(balance=25_000)
    calls = _use(monkeypatch, backend)
    assert await reserve_funding_address(NODE, network="signet", reserve_min_sats=25_000) == (
        25_000,
        None,
    )
    assert backend.addresses == 0
    assert backend.closed
    assert calls == [("127.0.0.1:10009", Path("tls.cert"), Path("admin.macaroon"), "signet")]


async def test_a_low_reserve_gets_a_fresh_taproot_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = FakeBackend(balance=1_000)
    _use(monkeypatch, backend)
    result = await reserve_funding_address(NODE, network="signet", reserve_min_sats=25_000)
    assert result == (1_000, TAPROOT)
    assert backend.closed


@pytest.mark.parametrize("address", [SEGWIT_V0, "not-an-address"])
async def test_only_a_taproot_address_of_the_network_is_returned(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    _use(monkeypatch, FakeBackend(balance=0, address=address))
    with pytest.raises(ReserveError):
        await reserve_funding_address(NODE, network="signet", reserve_min_sats=25_000)


async def test_an_unreachable_node_is_a_reserve_error(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeBackend(balance=0, error=RuntimeError("StatusCode.UNAVAILABLE"))
    _use(monkeypatch, backend)
    with pytest.raises(ReserveError, match="not running"):
        await reserve_funding_address(NODE, network="signet", reserve_min_sats=25_000)
    assert backend.closed


async def test_a_node_without_credentials_is_a_reserve_error() -> None:
    # The default paths do not exist in the test's working directory.
    missing = NODE.model_copy(update={"lnd_tls_cert_path": Path("/nonexistent/tls.cert")})
    with pytest.raises(ReserveError, match="start it first"):
        await reserve_funding_address(missing, network="signet", reserve_min_sats=25_000)


def _settings(data_dir: Path) -> JoinMarketSettings:
    return JoinMarketSettings(
        data_dir=data_dir,
        network_config={"network": "signet"},
        wallet={"address_type": "p2tr", "mixdepth_count": 3},
        bitcoin={"backend_type": "descriptor_wallet", "rpc_url": "http://127.0.0.1:38332"},
        tor={"control_enabled": True},
        lightning={"mode": "managed", "reserve_min_sats": 30_000},
    )


async def test_the_managed_target_is_the_active_node_of_the_mixdepth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = write_registry(tmp_path, states=(NodeState.ACTIVE, NodeState.RETIRING))
    seen: list[tuple[ChannelRingNodeSettings, str, int]] = []

    async def target(
        node: ChannelRingNodeSettings, *, network: str, reserve_min_sats: int
    ) -> tuple[int, str | None]:
        seen.append((node, network, reserve_min_sats))
        return 0, TAPROOT

    monkeypatch.setattr(lightning_reserve, "reserve_funding_address", target)
    settings = _settings(tmp_path)
    seed = mnemonic_to_seed(MNEMONIC, "")

    assert await managed_reserve_target(settings, seed, 0) == (0, TAPROOT)
    [(node, network, reserve_min)] = seen
    assert node.lnd_grpc_url == "https://127.0.0.1:21001"
    assert node.lnd_macaroon_path.is_relative_to(paths.root)
    assert (network, reserve_min) == ("signet", 30_000)

    # A retiring node gets no new reserve, and nothing outside the wallet.
    for mixdepth in (1, 2, 3):
        with pytest.raises(ReserveError):
            await managed_reserve_target(settings, seed, mixdepth)
    assert len(seen) == 1


async def test_no_registry_is_a_reserve_error(tmp_path: Path) -> None:
    with pytest.raises(ReserveError, match="jm-lightning init"):
        await managed_reserve_target(_settings(tmp_path), mnemonic_to_seed(MNEMONIC, ""), 0)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class Readings:
    """reserve_funding_address double: per-mixdepth balances, fresh addresses."""

    def __init__(self, balances: dict[int, int]) -> None:
        self.balances = balances
        self.reads: list[int] = []
        self.unreachable: set[int] = set()

    async def __call__(
        self,
        node: ChannelRingNodeSettings,
        *,
        network: str,
        reserve_min_sats: int,
        timeout_seconds: float,
    ) -> tuple[int, str | None]:
        mixdepth = int(node.lnd_grpc_url.rsplit(":", 1)[1]) - 10_000
        self.reads.append(mixdepth)
        if mixdepth in self.unreachable:
            raise ReserveError("cannot reach the node (it is not running)")
        balance = self.balances[mixdepth]
        if balance >= reserve_min_sats:
            return balance, None
        program = bytes([mixdepth, len(self.reads)]) + b"\x00" * 30
        return balance, scriptpubkey_to_address(bytes([0x51, 0x20]) + program, "signet")


def _funder(
    monkeypatch: pytest.MonkeyPatch, balances: dict[int, int]
) -> tuple[ReserveFunder, Readings, Clock]:
    readings = Readings(balances)
    monkeypatch.setattr(lightning_reserve, "reserve_funding_address", readings)
    clock = Clock()
    nodes = {
        mixdepth: NODE.model_copy(update={"lnd_grpc_url": f"https://127.0.0.1:{10_000 + mixdepth}"})
        for mixdepth in balances
    }
    funder = ReserveFunder(
        nodes,
        network="signet",
        reserve_min_sats=25_000,
        reserve_max_sats=150_000,
        clock=clock,
    )
    return funder, readings, clock


async def test_an_output_funds_a_low_node_only_within_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    funder, _, _ = _funder(monkeypatch, {0: 5_000, 1: 30_000})
    await funder.refresh()

    # Too small to reach the minimum, or too large for the maximum.
    assert funder.claim(0, 19_999) is None
    assert funder.claim(0, 145_001) is None
    # A node at its minimum needs nothing; an unmapped mixdepth has no node.
    assert funder.claim(1, 50_000) is None
    assert funder.claim(2, 50_000) is None
    address = funder.claim(0, 20_000)
    assert address is not None
    # Each address is handed out once.
    assert funder.claim(0, 20_000) is None


async def test_a_claimed_node_is_not_reread_until_its_hold_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    funder, readings, clock = _funder(monkeypatch, {0: 0})
    await funder.refresh()
    first = funder.claim(0, 50_000)
    assert first is not None

    clock.now += CLAIM_HOLD_SECONDS - 1
    await funder.refresh()
    assert readings.reads == [0]
    assert funder.claim(0, 50_000) is None

    # The paying CoinJoin never happened: a fresh address funds the node.
    clock.now += 2
    await funder.refresh()
    second = funder.claim(0, 50_000)
    assert second is not None and second != first

    # Once paid (unconfirmed coins count), nothing more is sent.
    readings.balances[0] = 50_000
    clock.now += CLAIM_HOLD_SECONDS + 1
    await funder.refresh()
    assert funder.claim(0, 50_000) is None


async def test_an_unknown_or_old_reading_funds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    funder, readings, clock = _funder(monkeypatch, {0: 0, 1: 0})
    await funder.refresh()
    readings.unreachable.add(1)
    await funder.refresh([1])
    assert funder.claim(1, 50_000) is None

    clock.now += STALE_SECONDS + 1
    assert funder.claim(0, 50_000) is None


async def test_the_background_loop_keeps_readings_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    funder, readings, _ = _funder(monkeypatch, {0: 0})
    funder.start(interval_seconds=0.01)
    for _ in range(100):
        if len(readings.reads) >= 2:
            break
        await asyncio.sleep(0.01)
    await funder.stop()
    assert len(readings.reads) >= 2
    assert funder.claim(0, 50_000) is not None


def test_only_a_managed_ring_config_with_mapped_nodes_funds_reserves() -> None:
    node = ChannelRingNodeConfig.model_validate(NODE.model_dump())
    ring = {"nodes": {"n0": node}, "mixdepth_nodes": {0: "n0"}}
    assert ReserveFunder.from_ring_config(ChannelRingConfig(**ring), network="signet") is None
    assert (
        ReserveFunder.from_ring_config(
            ChannelRingConfig(reserve_funding=(25_000, 150_000)), network="signet"
        )
        is None
    )
    funder = ReserveFunder.from_ring_config(
        ChannelRingConfig(**ring, reserve_funding=(25_000, 150_000)), network="signet"
    )
    assert funder is not None
    with pytest.raises(ValueError, match="reserve_funding"):
        ChannelRingConfig(**ring, reserve_funding=(30_000, 20_000))
