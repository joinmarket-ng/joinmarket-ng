"""Registry of JoinMarket-managed LND nodes, one per mixdepth.

The registry is the only authority to create, start, rotate, or recover a
managed node. Missing state never creates or starts anything; malformed or
unsupported state is preserved and refused. Node identities are scoped to the
registry's random epoch, so an allocation never collides with an identity used
by another installation. See docs/technical/standard-taproot-setup.md.
"""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from jmcore.channel_ring import ChannelRingNodeSettings
from jmcore.lightning_keys import MAX_GENERATIONS, MAX_MIXDEPTHS, NodeKeyScope, new_epoch
from jmcore.secure_files import (
    atomic_write_private,
    ensure_private_directory,
    exclusive_file_lock,
    read_private_file,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

REGISTRY_VERSION = 1
REGISTRY_FILE_NAME = "registry.json"
_CHANNEL_POINT = re.compile(r"^[0-9a-f]{64}:[0-9]{1,5}$")

Network = Literal["mainnet", "testnet", "signet", "regtest"]


class ManagedNodeError(Exception):
    """A managed node operation cannot be performed safely."""


class RegistryError(ManagedNodeError):
    """The registry is malformed or unsupported; it is preserved for review."""


class NodeState(StrEnum):
    """Lifecycle of one node identity; ``retired`` entries are kept as tombstones."""

    PROVISIONING = "provisioning"
    ACTIVE = "active"
    RETIRING = "retiring"
    RECOVERING = "recovering"
    RETIRED = "retired"


# States that may receive new rings, payments, and invoices (one per mixdepth).
OPERATIONAL_STATES = frozenset({NodeState.PROVISIONING, NodeState.ACTIVE})
# States whose LND process the supervisor runs.
RUNNABLE_STATES = frozenset(
    {NodeState.PROVISIONING, NodeState.ACTIVE, NodeState.RETIRING, NodeState.RECOVERING}
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CloseIntent(_Model):
    """Recorded before requesting a close, so a crash cannot lose the destination."""

    channel_point: str = Field(pattern=_CHANNEL_POINT.pattern)
    delivery_address: str = Field(min_length=1, max_length=100)
    requested_at: int = Field(ge=0)
    force: bool = False


class SweepIntent(_Model):
    """Recorded before sweeping a retiring node's wallet to its mixdepth."""

    address: str = Field(min_length=1, max_length=100)
    requested_at: int = Field(ge=0)
    txid: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ManagedNode(_Model):
    epoch: str = Field(pattern=r"^[0-9a-f]{16}$")
    mixdepth: int = Field(ge=0, lt=MAX_MIXDEPTHS)
    generation: int = Field(ge=0, lt=MAX_GENERATIONS)
    state: NodeState
    p2p_port: int = Field(ge=1024, le=65535)
    rpc_port: int = Field(ge=1024, le=65535)
    birthday: int = Field(ge=0, description="Unix time of wallet creation")
    identity_pubkey: str | None = Field(default=None, pattern=r"^(02|03)[0-9a-f]{64}$")
    onion_endpoint: str | None = None
    close_intents: list[CloseIntent] = Field(default_factory=list)
    sweep: SweepIntent | None = None

    @property
    def name(self) -> str:
        """Stable node name used by ring journals and bindings."""
        return f"jm-{self.epoch}-m{self.mixdepth}-g{self.generation}"

    def scope(self, network: str) -> NodeKeyScope:
        return NodeKeyScope(network, self.epoch, self.mixdepth, self.generation)


class NodeRegistry(_Model):
    version: Literal[1] = 1
    network: Network
    wallet_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    epoch: str = Field(pattern=r"^[0-9a-f]{16}$", description="Epoch for new allocations")
    nodes: list[ManagedNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> NodeRegistry:
        keys = [(node.epoch, node.mixdepth, node.generation) for node in self.nodes]
        if len(set(keys)) != len(keys):
            raise ValueError("a node identity is listed twice")
        operational = [n.mixdepth for n in self.nodes if n.state in OPERATIONAL_STATES]
        if len(set(operational)) != len(operational):
            raise ValueError("a mixdepth has more than one operational node")
        ports = [
            port
            for node in self.nodes
            if node.state is not NodeState.RETIRED
            for port in (node.p2p_port, node.rpc_port)
        ]
        if len(set(ports)) != len(ports):
            raise ValueError("two live nodes share a port")
        return self

    def operational(self, mixdepth: int) -> ManagedNode | None:
        return next(
            (n for n in self.nodes if n.mixdepth == mixdepth and n.state in OPERATIONAL_STATES),
            None,
        )

    def active(self, mixdepth: int) -> ManagedNode | None:
        node = self.operational(mixdepth)
        return node if node is not None and node.state is NodeState.ACTIVE else None

    def runnable(self) -> list[ManagedNode]:
        return [node for node in self.nodes if node.state in RUNNABLE_STATES]

    def find(self, name: str) -> ManagedNode:
        for node in self.nodes:
            if node.name == name:
                return node
        raise ManagedNodeError(f"no managed node named {name!r}")


def new_registry(network: str, wallet_identity: str) -> NodeRegistry:
    """A registry with a fresh epoch and no nodes."""
    return NodeRegistry.model_validate(
        {"network": network, "wallet_identity": wallet_identity, "epoch": new_epoch()}
    )


def free_local_ports(count: int, exclude: Iterable[int] = ()) -> list[int]:
    """Ports currently free on loopback, distinct from ``exclude``."""
    taken = set(exclude)
    ports: list[int] = []
    sockets = []
    try:
        while len(ports) < count:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(sock)
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
            if port not in taken and port >= 1024:
                taken.add(port)
                ports.append(port)
    finally:
        for sock in sockets:
            sock.close()
    return ports


def allocate_node(
    registry: NodeRegistry,
    mixdepth: int,
    *,
    birthday: int,
    ports: tuple[int, int] | None = None,
) -> ManagedNode:
    """Append the next generation of ``mixdepth`` in the current epoch as provisioning.

    The caller must persist the registry before initializing the LND wallet.
    """
    if registry.operational(mixdepth) is not None:
        raise ManagedNodeError(f"mixdepth {mixdepth} already has an operational node")
    used = [
        n.generation for n in registry.nodes if (n.epoch, n.mixdepth) == (registry.epoch, mixdepth)
    ]
    generation = max(used) + 1 if used else 0
    if generation >= MAX_GENERATIONS:
        raise ManagedNodeError(f"mixdepth {mixdepth} has exhausted its node generations")
    if ports is None:
        live = [
            port
            for node in registry.nodes
            if node.state is not NodeState.RETIRED
            for port in (node.p2p_port, node.rpc_port)
        ]
        p2p_port, rpc_port = free_local_ports(2, exclude=live)
    else:
        p2p_port, rpc_port = ports
    node = ManagedNode(
        epoch=registry.epoch,
        mixdepth=mixdepth,
        generation=generation,
        state=NodeState.PROVISIONING,
        p2p_port=p2p_port,
        rpc_port=rpc_port,
        birthday=birthday,
    )
    registry.nodes.append(node)
    NodeRegistry.model_validate(registry.model_dump())
    return node


_LND_NETWORK_DIRS = {"mainnet": "mainnet", "testnet": "testnet", "signet": "signet"}


@dataclass(frozen=True)
class ManagedNodePaths:
    """Filesystem layout of one wallet's managed nodes on one network."""

    root: Path
    network: str

    @classmethod
    def for_wallet(cls, data_dir: Path, network: str, wallet_identity: str) -> ManagedNodePaths:
        return cls(data_dir / "lightning" / network / wallet_identity[:16], network)

    @property
    def registry(self) -> Path:
        return self.root / REGISTRY_FILE_NAME

    @property
    def lock(self) -> Path:
        return self.root / "registry.lock"

    @property
    def process_lock(self) -> Path:
        """Held by the one command that runs these nodes' LND processes."""
        return self.root / "processes.lock"

    @property
    def nodes_dir(self) -> Path:
        return self.root / "nodes"

    @property
    def backup_dir(self) -> Path:
        return self.root / "backup"

    @property
    def bindings_dir(self) -> Path:
        """Ring node bindings of managed nodes live next to their registry."""
        return self.root

    def node_dir(self, node: ManagedNode) -> Path:
        return self.nodes_dir / f"{node.epoch}-{node.mixdepth}-{node.generation}"

    def lnd_conf(self, node: ManagedNode) -> Path:
        return self.node_dir(node) / "lnd.conf"

    def tls_cert(self, node: ManagedNode) -> Path:
        return self.node_dir(node) / "tls.cert"

    def _chain_dir(self, node: ManagedNode) -> Path:
        network_dir = _LND_NETWORK_DIRS.get(self.network, self.network)
        return self.node_dir(node) / "data" / "chain" / "bitcoin" / network_dir

    def macaroon(self, node: ManagedNode) -> Path:
        return self._chain_dir(node) / "admin.macaroon"

    def wallet_db(self, node: ManagedNode) -> Path:
        return self._chain_dir(node) / "wallet.db"

    def channel_backup(self, node: ManagedNode) -> Path:
        return self.backup_dir / f"{node.name}.backup"

    def has_any_node_data(self) -> bool:
        return self.nodes_dir.exists() and any(self.nodes_dir.iterdir())


class RegistryStore:
    """Load and atomically update the registry under a cross-process lock."""

    def __init__(self, paths: ManagedNodePaths) -> None:
        self.paths = paths

    @contextmanager
    def exclusive_processes(self) -> Iterator[None]:
        """Ensure no other command runs these nodes' LND processes meanwhile."""
        ensure_private_directory(self.paths.root)
        with ExitStack() as stack:
            try:
                stack.enter_context(exclusive_file_lock(self.paths.process_lock, blocking=False))
            except BlockingIOError:
                raise ManagedNodeError(
                    "another jm-lightning command is running these nodes; stop it first"
                ) from None
            yield

    def load(self) -> NodeRegistry | None:
        """``None`` only when no registry file exists; anything unreadable raises."""
        try:
            raw = read_private_file(self.paths.registry)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RegistryError("cannot read the managed node registry") from exc
        try:
            document = json.loads(raw)
            if not isinstance(document, dict) or document.get("version") != REGISTRY_VERSION:
                raise ValueError("unsupported registry version")
            registry = NodeRegistry.model_validate(document)
        except (ValueError, TypeError, UnicodeDecodeError, ValidationError) as exc:
            raise RegistryError(
                f"managed node registry {self.paths.registry} is malformed or unsupported; "
                "it was left untouched"
            ) from exc
        if registry.network != self.paths.network:
            raise RegistryError("managed node registry belongs to another network")
        return registry

    def update(
        self, change: Callable[[NodeRegistry | None], NodeRegistry | None]
    ) -> NodeRegistry | None:
        """Apply ``change`` to the current registry and persist its result durably."""
        ensure_private_directory(self.paths.root)
        with exclusive_file_lock(self.paths.lock):
            current = self.load()
            before = current.model_dump_json() if current is not None else None
            result = change(current.model_copy(deep=True) if current is not None else None)
            if result is None:
                return current
            result = NodeRegistry.model_validate(result.model_dump())
            if result.model_dump_json() != before:
                self._write(result)
            return result

    def _write(self, registry: NodeRegistry) -> None:
        data = (registry.model_dump_json(indent=2) + "\n").encode()
        atomic_write_private(self.paths.registry, data)
        _fsync_directory(self.paths.root)
        # A copy outside the node data directories survives losing them.
        ensure_private_directory(self.paths.backup_dir)
        atomic_write_private(self.paths.backup_dir / REGISTRY_FILE_NAME, data)


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ring_nodes(
    registry: NodeRegistry, paths: ManagedNodePaths
) -> tuple[dict[str, ChannelRingNodeSettings], dict[int, str]]:
    """Channel ring endpoints of the managed nodes and the mixdepth mapping.

    Retiring and recovering nodes stay configured so their recorded rings can be
    recovered, but only active nodes are mapped to receive new rings.
    """
    nodes: dict[str, ChannelRingNodeSettings] = {}
    mapping: dict[int, str] = {}
    for node in registry.nodes:
        if node.state is NodeState.RETIRED or node.state is NodeState.PROVISIONING:
            continue
        if node.onion_endpoint is None:
            continue
        nodes[node.name] = ChannelRingNodeSettings(
            lnd_grpc_url=f"https://127.0.0.1:{node.rpc_port}",
            lnd_tls_cert_path=paths.tls_cert(node),
            lnd_macaroon_path=paths.macaroon(node),
            onion_endpoint=node.onion_endpoint,
        )
        if node.state is NodeState.ACTIVE:
            mapping[node.mixdepth] = node.name
    return nodes, mapping
