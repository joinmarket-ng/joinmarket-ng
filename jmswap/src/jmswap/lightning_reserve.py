"""The on-chain anchor reserve of a mixdepth's Lightning node.

Anchor channels need confirmed wallet coins to fee-bump a force close, so each
node keeps a small reserve between ``[lightning] reserve_min_sats`` and
``reserve_max_sats``. The reserve of mixdepth N's node is only ever funded from
mixdepth N (or a CoinJoin equal output that lands in it), so funding it never
links two mixdepths. Unconfirmed coins count toward the reserve: a funding
transaction still in the mempool must not be repeated.
"""

from __future__ import annotations

from typing import Any

from jmcore.bitcoin import address_to_scriptpubkey_for_network
from jmcore.channel_ring import ChannelRingNodeSettings

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
