"""The on-chain anchor reserve of a mixdepth's Lightning node.

Anchor channels need confirmed wallet coins to fee-bump a force close, so each
node keeps a small reserve between ``[lightning] reserve_min_sats`` and
``reserve_max_sats``. The reserve of mixdepth N's node is only ever funded from
mixdepth N (or a CoinJoin equal output that lands in it), so funding it never
links two mixdepths. Unconfirmed coins count toward the reserve: a funding
transaction still in the mempool must not be repeated.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from jmcore.bitcoin import address_to_scriptpubkey_for_network
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeSettings
from loguru import logger

from jmswap.lightning_context import lightning_context
from jmswap.lnd import LndBackend
from jmswap.managed_nodes import ManagedNodeError, ring_nodes


class ReserveError(Exception):
    """The reserve of a node cannot be read or funded."""


def default_reserve_amount(
    balance_sats: int, *, reserve_min_sats: int, reserve_max_sats: int
) -> int:
    """The amount to send to a reserve below its minimum, never exceeding the maximum."""
    if balance_sats >= reserve_min_sats:
        return 0
    return max(0, min(reserve_min_sats, reserve_max_sats - balance_sats))


def check_reserve_amount(balance_sats: int, amount_sats: int, *, reserve_max_sats: int) -> None:
    """Refuse an amount that would push the reserve above its maximum."""
    if amount_sats <= 0:
        raise ReserveError("the reserve amount must be positive")
    if balance_sats + amount_sats > reserve_max_sats:
        raise ReserveError(
            f"{amount_sats:,} sats would raise the reserve to {balance_sats + amount_sats:,}, "
            f"above reserve_max_sats ({reserve_max_sats:,})"
        )


def _require_taproot(address: str, network: str) -> None:
    try:
        script = address_to_scriptpubkey_for_network(address, network)
    except ValueError as exc:
        raise ReserveError(f"LND returned an address that is not valid on {network}") from exc
    if len(script) != 34 or script[0] != 0x51 or script[1] != 0x20:
        raise ReserveError("LND returned a non-Taproot reserve address")


async def reserve_funding_address(
    node: ChannelRingNodeSettings,
    *,
    network: str,
    reserve_min_sats: int,
    timeout_seconds: float = 10.0,
) -> tuple[int, str | None]:
    """The node's on-chain balance and, when below the minimum, a fresh Taproot address.

    The address is ``None`` when confirmed plus unconfirmed coins already reach
    ``reserve_min_sats``. Each call asks for a new address, so two funding
    attempts never reuse one on chain.
    """
    try:
        backend = LndBackend.from_paths(
            node.lnd_grpc_url.removeprefix("https://"),
            node.lnd_tls_cert_path,
            node.lnd_macaroon_path,
            network=network,
        )
    except OSError as exc:
        raise ReserveError("the node has no TLS certificate or macaroon; start it first") from exc
    try:
        balance = await backend.onchain_balance_sats(timeout_seconds=timeout_seconds)
        if balance >= reserve_min_sats:
            return balance, None
        address = await backend.new_taproot_address(timeout_seconds=timeout_seconds)
    except ReserveError:
        raise
    except Exception as exc:  # noqa: BLE001 - gRPC and timeout errors carry no secrets
        detail = "it is not running" if "UNAVAILABLE" in str(exc) else type(exc).__name__
        raise ReserveError(f"cannot reach the node ({detail})") from exc
    finally:
        await backend.close()
    _require_taproot(address, network)
    return balance, address


async def managed_reserve_target(
    settings: Any, seed: bytes, mixdepth: int
) -> tuple[int, str | None]:
    """:func:`reserve_funding_address` of the active managed node of ``mixdepth``."""
    ctx = lightning_context(settings, seed, with_keys=False)
    if mixdepth not in ctx.mixdepths:
        raise ReserveError(f"mixdepth {mixdepth} is outside 0..{len(ctx.mixdepths) - 1}")
    try:
        registry = ctx.store.load()
    except ManagedNodeError as exc:
        raise ReserveError(str(exc)) from exc
    if registry is None:
        raise ReserveError("no managed nodes yet; create them with `jm-lightning init`")
    node = registry.active(mixdepth)
    endpoint = None if node is None else ring_nodes(registry, ctx.paths)[0].get(node.name)
    if endpoint is None:
        raise ReserveError(f"mixdepth {mixdepth} has no active managed node with an endpoint")
    return await reserve_funding_address(
        endpoint, network=ctx.network, reserve_min_sats=int(settings.lightning.reserve_min_sats)
    )


#: How often a long-running maker rereads its nodes' reserves.
REFRESH_SECONDS = 300.0
#: A reserve read longer ago than this is unknown and funds nothing.
STALE_SECONDS = 2 * REFRESH_SECONDS
#: A claimed address is not offered again, and its node not reread, for this
#: long: the CoinJoin that pays it may still be negotiating or unconfirmed.
CLAIM_HOLD_SECONDS = 1800.0


@dataclass
class _Reserve:
    balance: int
    address: str | None
    read_at: float
    claimed_at: float | None = None


class ReserveFunder:
    """Lets CoinJoin outputs top up the reserves of the mixdepths' nodes.

    Node RPCs only happen in :meth:`refresh`; :meth:`claim` decides from the
    last reading without I/O, so a maker can answer ``!ioauth`` promptly. An
    unreachable node or an old reading claims nothing.
    """

    def __init__(
        self,
        nodes: Mapping[int, ChannelRingNodeSettings],
        *,
        network: str,
        reserve_min_sats: int,
        reserve_max_sats: int,
        clock: Callable[[], float] = time.monotonic,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._nodes = dict(nodes)
        self._network = network
        self._min = reserve_min_sats
        self._max = reserve_max_sats
        self._clock = clock
        self._timeout = timeout_seconds
        self._reserves: dict[int, _Reserve] = {}
        self._task: asyncio.Task[None] | None = None

    @classmethod
    def from_ring_config(cls, config: ChannelRingConfig, *, network: str) -> ReserveFunder | None:
        """The funder of a managed ring config, ``None`` when it funds no reserve."""
        if config.reserve_funding is None or not config.mixdepth_nodes:
            return None
        reserve_min, reserve_max = config.reserve_funding
        nodes = {mixdepth: config.nodes[name] for mixdepth, name in config.mixdepth_nodes.items()}
        return cls(
            nodes, network=network, reserve_min_sats=reserve_min, reserve_max_sats=reserve_max
        )

    async def refresh(self, mixdepths: Iterable[int] | None = None) -> None:
        """Reread the reserves of ``mixdepths`` (all mapped ones by default)."""
        wanted = set(self._nodes if mixdepths is None else mixdepths) & set(self._nodes)
        await asyncio.gather(*(self._refresh_one(mixdepth) for mixdepth in sorted(wanted)))

    async def _refresh_one(self, mixdepth: int) -> None:
        now = self._clock()
        current = self._reserves.get(mixdepth)
        if (
            current is not None
            and current.claimed_at is not None
            and now < current.claimed_at + CLAIM_HOLD_SECONDS
        ):
            return
        try:
            balance, address = await reserve_funding_address(
                self._nodes[mixdepth],
                network=self._network,
                reserve_min_sats=self._min,
                timeout_seconds=self._timeout,
            )
        except ReserveError as exc:
            self._reserves.pop(mixdepth, None)
            logger.debug(f"Lightning reserve of mixdepth {mixdepth} is unknown: {exc}")
            return
        self._reserves[mixdepth] = _Reserve(balance, address, self._clock())

    def claim(self, mixdepth: int, amount_sats: int) -> str | None:
        """The node address an output of ``amount_sats`` should pay, if any.

        An output only funds a node below its minimum, and only if the node's
        reserve then lies within ``[reserve_min_sats, reserve_max_sats]``. The
        address is handed out once; a new one follows a later refresh.
        """
        reserve = self._reserves.get(mixdepth)
        now = self._clock()
        if (
            reserve is None
            or reserve.address is None
            or reserve.claimed_at is not None
            or now > reserve.read_at + STALE_SECONDS
            or not self._min <= reserve.balance + amount_sats <= self._max
        ):
            return None
        reserve.claimed_at = now
        logger.info(f"A CoinJoin output funds the Lightning reserve of mixdepth {mixdepth}")
        return reserve.address

    def start(self, interval_seconds: float = REFRESH_SECONDS) -> None:
        """Keep the readings current in the background."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(interval_seconds))

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self, interval_seconds: float) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(interval_seconds)
