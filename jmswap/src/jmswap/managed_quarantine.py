"""Durable inbound-channel quarantine intent, serialized with managed LND launches.

A request is not proof that an existing process has reloaded its policy. Only
subsequent supported launches are forced to reject inbound opens. Existing
channels and settlement remain the responsibility of their normal monitors.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from enum import StrEnum

from jmcore.secure_files import (
    atomic_write_private,
    ensure_private_directory,
    exclusive_file_lock,
    read_private_file,
)
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from jmswap.managed_nodes import (
    ManagedNode,
    ManagedNodeError,
    ManagedNodePaths,
    NodeState,
    RegistryStore,
    _fsync_directory,
)


class QuarantineState(StrEnum):
    ABSENT = "absent"
    REQUESTED = "requested"
    UNKNOWN = "unknown"


class QuarantineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(strict=True, ge=1, le=1)
    wallet_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    network: str
    epoch: str = Field(pattern=r"^[0-9a-f]{16}$")
    mixdepth: int = Field(ge=0)
    generation: int = Field(ge=0)
    identity_pubkey: str | None = Field(default=None, pattern=r"^(02|03)[0-9a-f]{64}$")


def _owned_node(paths: ManagedNodePaths, node: ManagedNode, network: str) -> ManagedNode:
    if network != paths.network:
        raise ManagedNodeError("managed LND environment belongs to another network")
    registry = RegistryStore(paths).load()
    if registry is None:
        raise ManagedNodeError("managed LND launch requires its owned registry")
    current = registry.find(node.name)
    if current.state is NodeState.RETIRED:
        raise ManagedNodeError("an explicitly retired managed node cannot be launched")
    if node.identity_pubkey is not None and current.identity_pubkey != node.identity_pubkey:
        raise ManagedNodeError("managed node identity changed before launch")
    return current


def quarantine_state(paths: ManagedNodePaths, node: ManagedNode) -> QuarantineState:
    """Unknown marker data restricts admission but is not an owned request."""
    try:
        raw = read_private_file(paths.quarantine(node))
    except FileNotFoundError:
        return QuarantineState.ABSENT
    except OSError:
        return QuarantineState.UNKNOWN
    try:
        request = QuarantineRequest.model_validate_json(raw)
        if (
            request.wallet_identity != paths.wallet_identity
            or request.network != paths.network
            or (request.epoch, request.mixdepth, request.generation)
            != (node.epoch, node.mixdepth, node.generation)
            or (
                request.identity_pubkey is not None
                and request.identity_pubkey != node.identity_pubkey
            )
        ):
            return QuarantineState.UNKNOWN
    except ValueError:
        return QuarantineState.UNKNOWN
    return QuarantineState.REQUESTED


@asynccontextmanager
async def _policy_lock(paths: ManagedNodePaths, node: ManagedNode) -> AsyncIterator[None]:
    # Callers validate authority before allowing directory creation, then again
    # under this lock. Never block the event loop waiting for another launch.
    ensure_private_directory(paths.node_dir(node))
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(exclusive_file_lock(paths.policy_lock(node), blocking=False))
            except BlockingIOError:
                await asyncio.sleep(0.05)
            else:
                break
        yield


def _write_request(paths: ManagedNodePaths, node: ManagedNode) -> None:
    request = QuarantineRequest(
        version=1,
        wallet_identity=paths.wallet_identity,
        network=paths.network,
        epoch=node.epoch,
        mixdepth=node.mixdepth,
        generation=node.generation,
        identity_pubkey=node.identity_pubkey,
    )
    atomic_write_private(paths.quarantine(node), (request.model_dump_json() + "\n").encode())


def _sync_request(paths: ManagedNodePaths, node: ManagedNode) -> None:
    # Persist the rename and newly created node/nodes directory entries before
    # reporting durable intent. Errors must prevent a launch/success response.
    for directory in (paths.node_dir(node), paths.nodes_dir, paths.root):
        _fsync_directory(directory)


async def request_quarantine(paths: ManagedNodePaths, node: ManagedNode) -> None:
    """Record owned intent only; an existing LND may still need a verified restart."""
    _owned_node(paths, node, paths.network)
    async with _policy_lock(paths, node):
        current = _owned_node(paths, node, paths.network)
        state = quarantine_state(paths, current)
        if state is QuarantineState.UNKNOWN:
            raise ManagedNodeError(
                "quarantine evidence is unreadable or mismatched; preserved for review"
            )
        if state is QuarantineState.ABSENT:
            _write_request(paths, current)
        # A prior attempt may have written the file but failed its directory
        # flush. Reading a valid file alone is not a durable-success receipt.
        _sync_request(paths, current)


@asynccontextmanager
async def managed_launch_policy(
    paths: ManagedNodePaths, node: ManagedNode, *, network: str
) -> AsyncIterator[tuple[ManagedNode, bool]]:
    """Resolve current authority and hold launch ordering through subprocess spawn.

    A launch ordered before a request can still use its former policy. This
    context deliberately does not attest that any process loaded quarantine.
    """
    _owned_node(paths, node, network)
    async with _policy_lock(paths, node):
        current = _owned_node(paths, node, network)
        state = quarantine_state(paths, current)
        if (
            current.state in {NodeState.RETIRING, NodeState.RECOVERING}
            and state is QuarantineState.ABSENT
        ):
            _write_request(paths, current)
            state = QuarantineState.REQUESTED
        if state is QuarantineState.REQUESTED:
            _sync_request(paths, current)
        if state is QuarantineState.UNKNOWN:
            logger.warning(
                "Quarantine evidence for {} is unknown; preserving it and refusing inbound opens",
                current.name,
            )
        yield current, state is not QuarantineState.ABSENT
