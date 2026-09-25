"""Durable LN identity ownership for mixdepth-isolated channel rings.

Adapted from the binding registry introduced by 83338a7c. Enrollment is an
explicit operator operation, never a side effect of startup or recovery.
Claims cover cooperating processes sharing this directory, not other clients
or hosts. They authorize future use and do not prove historical isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bitcointx.core.key import CPubKey
from jmcore.channel_ring import ChannelRingConfig, NodeOwner, RingNodeBinding
from jmcore.channel_ring_store import RingParticipantStore
from jmcore.secure_files import (
    atomic_write_private,
    ensure_private_directory,
    exclusive_file_lock,
    read_private_file,
)
from pydantic import ValidationError

from jmswap.channel_ring import InitializedChannelRingBackend, initialize_channel_ring_backend

REGISTRY_FILE_NAME = "node-bindings.json"
_REGISTRY_KIND = "channel_ring_node_bindings"


class ChannelRingNodeError(Exception):
    """A node cannot safely participate in the requested wallet/mixdepth."""


class ChannelRingNodeRegistryError(ChannelRingNodeError):
    """Ownership history is malformed or unreadable and must not be reset."""


class ChannelRingNodeEnrollmentError(ChannelRingNodeError):
    """Ownership is unknown; explicit enrollment is required before startup."""


class ChannelRingNodeClaimConflictError(ChannelRingNodeError):
    """An identity is already enrolled for another network, wallet or mixdepth."""


def channel_ring_wallet_identity(master_public_key: bytes) -> str:
    """Identify the wallet across pits without storing its seed or extended key."""
    if (
        len(master_public_key) != 33
        or master_public_key[0] not in (2, 3)
        or not CPubKey(master_public_key).is_fullyvalid()
    ):
        raise ValueError("wallet identity requires a compressed master public key")
    return hashlib.sha256(b"joinmarket/channel-ring-wallet\0" + master_public_key).hexdigest()


def _validated_node_id(value: str) -> str:
    if (
        len(value) != 66
        or value[:2] not in {"02", "03"}
        or any(character not in "0123456789abcdef" for character in value)
        or not CPubKey(bytes.fromhex(value)).is_fullyvalid()
    ):
        raise ValueError("LND identity must be a valid lowercase compressed public key")
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in node binding registry")
        result[key] = value
    return result


def _load_bindings(path: Path) -> dict[str, NodeOwner]:
    """An absent file stays absent; callers distinguish enrollment from use."""
    try:
        raw = read_private_file(path)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise ChannelRingNodeRegistryError("cannot read private node binding registry") from exc
    try:
        document = json.loads(raw, object_pairs_hook=_unique_json_object)
        if (
            not isinstance(document, dict)
            or set(document) != {"registry_kind", "bindings"}
            or document["registry_kind"] != _REGISTRY_KIND
            or not isinstance(document["bindings"], dict)
        ):
            raise ValueError("unrecognized node binding registry")
        return {
            _validated_node_id(node_id): NodeOwner.model_validate(owner)
            for node_id, owner in document["bindings"].items()
        }
    except (ValueError, TypeError, UnicodeDecodeError, ValidationError) as exc:
        raise ChannelRingNodeRegistryError(
            "node binding registry is malformed or unsupported; preserve it for operator review"
        ) from exc


def _validated_claims(claims: Mapping[str, NodeOwner]) -> dict[str, NodeOwner]:
    result = {}
    for node_id, owner in claims.items():
        if not isinstance(owner, NodeOwner):
            raise TypeError("node ownership must be a validated NodeOwner")
        result[_validated_node_id(node_id)] = owner
    return result


def verify_node_bindings(directory: Path, claims: Mapping[str, NodeOwner]) -> None:
    """Verify every claim without creating files, backfilling or reassigning."""
    expected = _validated_claims(claims)
    if not expected:
        return
    try:
        owners = _load_bindings(directory / REGISTRY_FILE_NAME)
    except FileNotFoundError as exc:
        raise ChannelRingNodeEnrollmentError(
            "node ownership is unknown; explicitly enroll identities before enabling channel rings"
        ) from exc
    for node_id, owner in expected.items():
        if node_id not in owners:
            raise ChannelRingNodeEnrollmentError(
                "a configured LND identity has no ownership claim; explicit enrollment is required"
            )
        if owners[node_id] != owner:
            raise ChannelRingNodeClaimConflictError(
                "LND identity is enrolled for a different network, wallet or source mixdepth"
            )


def enroll_node_bindings(directory: Path, claims: Mapping[str, NodeOwner]) -> None:
    """Explicitly enroll a complete batch or change nothing if any claim conflicts.

    The caller must verify live node identities and obtain the operator's
    acknowledgment of prior node use. This function never clears prior claims.
    Repeating an identical enrollment performs no registry write.
    """
    requested = _validated_claims(claims)
    if not requested:
        raise ValueError("enrollment requires at least one node identity")
    ensure_private_directory(directory)
    path = directory / REGISTRY_FILE_NAME
    with exclusive_file_lock(directory / "node-bindings.lock"):
        try:
            owners = _load_bindings(path)
        except FileNotFoundError:
            owners = {}
        for node_id, owner in requested.items():
            if node_id in owners and owners[node_id] != owner:
                raise ChannelRingNodeClaimConflictError(
                    "LND identity already belongs to another network, wallet or source mixdepth; "
                    "enrollment cannot reassign it"
                )
        if all(owners.get(node_id) == owner for node_id, owner in requested.items()):
            return
        owners.update(requested)
        document = {
            "registry_kind": _REGISTRY_KIND,
            "bindings": {node_id: owner.model_dump() for node_id, owner in sorted(owners.items())},
        }
        atomic_write_private(path, (json.dumps(document, indent=2) + "\n").encode())
        if os.name == "posix":
            directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)


@dataclass(frozen=True)
class BoundChannelRingNode(InitializedChannelRingBackend):
    """A validated backend whose identity has an existing wallet/mixdepth claim."""

    binding: RingNodeBinding


def _bound_node(
    initialized: InitializedChannelRingBackend, binding: RingNodeBinding
) -> BoundChannelRingNode:
    if (
        initialized.node_info.identity_pubkey != binding.local_node_id
        or initialized.node_info.network != binding.network
    ):
        raise ChannelRingNodeClaimConflictError(
            "configured endpoint resolves to another LND identity"
        )
    return BoundChannelRingNode(
        backend=initialized.backend,
        node_info=initialized.node_info,
        onion_endpoint=initialized.onion_endpoint,
        backend_limits=initialized.backend_limits,
        binding=binding,
    )


def _owner(binding: RingNodeBinding) -> NodeOwner:
    return NodeOwner(
        network=binding.network,
        wallet_identity=binding.wallet_identity,
        source_mixdepth=binding.source_mixdepth,
    )


async def _close_nodes(nodes: Mapping[str, InitializedChannelRingBackend]) -> None:
    for initialized in nodes.values():
        with suppress(Exception):
            await initialized.backend.close()


async def _inspect_nodes(
    config: ChannelRingConfig,
    names: Iterable[str],
    *,
    network: str,
    offer_type: str,
    recovery: bool = False,
) -> dict[str, InitializedChannelRingBackend]:
    nodes: dict[str, InitializedChannelRingBackend] = {}
    try:
        for name in sorted(set(names)):
            if name not in config.nodes:
                raise ChannelRingNodeError(
                    "journal-recorded LND node is not configured for recovery"
                )
            nodes[name] = await initialize_channel_ring_backend(
                config,
                node=config.nodes[name],
                network=network,
                offer_type=offer_type,
                recovery=recovery,
            )
        identities = [node.node_info.identity_pubkey for node in nodes.values()]
        if len(set(identities)) != len(identities):
            raise ChannelRingNodeClaimConflictError(
                "multiple configured aliases resolve to the same LND identity"
            )
        return nodes
    except BaseException:
        await _close_nodes(nodes)
        raise


def _mapped_bindings(
    config: ChannelRingConfig,
    nodes: Mapping[str, InitializedChannelRingBackend],
    *,
    network: str,
    wallet_identity: str,
) -> dict[int, RingNodeBinding]:
    return {
        mixdepth: RingNodeBinding.model_validate(
            {
                "network": network,
                "wallet_identity": wallet_identity,
                "source_mixdepth": mixdepth,
                "node_name": name,
                "local_node_id": nodes[name].node_info.identity_pubkey,
            }
        )
        for mixdepth, name in config.mixdepth_nodes.items()
    }


class ChannelRingNodePool:
    """Own the live backends and the exclusive journal-directory lease."""

    def __init__(
        self,
        nodes: dict[str, InitializedChannelRingBackend],
        config: ChannelRingConfig,
        network: str,
        offer_type: str,
        wallet_identity: str,
        mixdepth_count: int,
        store: RingParticipantStore,
        lease: ExitStack,
    ) -> None:
        self._nodes = nodes
        self._bindings: dict[int, RingNodeBinding] = {}
        self._config = config
        self._network = network
        self._offer_type = offer_type
        self._wallet_identity = wallet_identity
        self._mixdepth_count = mixdepth_count
        self.store = store
        self._lease = lease
        self._closed = False

    async def enable_funding(self) -> None:
        """Enable current mappings only after recovery has had a chance to run."""
        if self._closed:
            raise ChannelRingNodeError("channel ring node pool is closed")
        config = self._config
        if not config.enabled:
            raise ChannelRingNodeError("channel ring funding is disabled")
        if any(mixdepth >= self._mixdepth_count for mixdepth in config.mixdepth_nodes):
            raise ChannelRingNodeError("configured ring mixdepth is outside this wallet")
        additions = await _inspect_nodes(
            config,
            set(config.mixdepth_nodes.values()) - self._nodes.keys(),
            network=self._network,
            offer_type=self._offer_type,
        )
        try:
            nodes = self._nodes | additions
            identities = [node.node_info.identity_pubkey for node in nodes.values()]
            if len(set(identities)) != len(identities):
                raise ChannelRingNodeClaimConflictError(
                    "multiple configured aliases resolve to the same LND identity"
                )
            bindings = _mapped_bindings(
                config, nodes, network=self._network, wallet_identity=self._wallet_identity
            )
            assert config.node_binding_directory is not None
            verify_node_bindings(
                config.node_binding_directory,
                {binding.local_node_id: _owner(binding) for binding in bindings.values()},
            )
            self._nodes = nodes
            self._bindings = bindings
        except BaseException:
            await _close_nodes(additions)
            raise

    @property
    def mixdepths(self) -> frozenset[int]:
        return frozenset(self._bindings)

    def for_mixdepth(self, mixdepth: int) -> BoundChannelRingNode:
        if self._closed:
            raise ChannelRingNodeError("channel ring node pool is closed")
        if mixdepth not in self._bindings:
            raise ChannelRingNodeError("source mixdepth has no enrolled channel ring node")
        return self.for_binding(self._bindings[mixdepth])

    def for_binding(self, binding: RingNodeBinding) -> BoundChannelRingNode:
        """Recover by recorded identity, never by the current mixdepth mapping."""
        if self._closed:
            raise ChannelRingNodeError("channel ring node pool is closed")
        if (binding.network, binding.wallet_identity) != (self._network, self._wallet_identity):
            raise ChannelRingNodeClaimConflictError("ring journal belongs to a different wallet")
        if binding.source_mixdepth >= self._mixdepth_count:
            raise ChannelRingNodeClaimConflictError("ring journal mixdepth is outside this wallet")
        if binding.node_name not in self._nodes:
            raise ChannelRingNodeError("journal-recorded LND node is not configured for recovery")
        node = _bound_node(self._nodes[binding.node_name], binding)
        if self._config.node_binding_directory is None:
            raise ChannelRingNodeEnrollmentError("recovery requires the existing binding directory")
        verify_node_bindings(
            self._config.node_binding_directory, {binding.local_node_id: _owner(binding)}
        )
        return node

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await _close_nodes(self._nodes)
        finally:
            self._lease.close()


async def initialize_channel_ring_nodes(
    config: ChannelRingConfig,
    *,
    network: str,
    offer_type: str,
    wallet_identity: str,
    mixdepth_count: int,
    data_directory: Path,
) -> ChannelRingNodePool:
    """Lease journals and prepare recovery independently of new funding eligibility.

    Call ``enable_funding`` after reconciliation to validate current mappings.
    Unsupported journals are preserved, never inferred from current settings.
    """
    lease = ExitStack()
    nodes: dict[str, InitializedChannelRingBackend] = {}
    try:
        lease.enter_context(
            exclusive_file_lock(
                config.persistence_path(data_directory) / "runtime.lock", blocking=False
            )
        )
        store = RingParticipantStore(
            config.persistence_path(data_directory),
            max_active_sessions=config.max_active_sessions,
            max_verified_sessions=config.max_verified_sessions,
        )
        report = store.load_all()
        if report.corruptions:
            raise ChannelRingNodeRegistryError(
                "ring journals are malformed or lack ownership provenance; preserved for operator review"
            )
        retained = [record.node_binding for record in report.records if record.active]
        for binding in retained:
            if (
                binding.network != network
                or binding.wallet_identity != wallet_identity
                or binding.source_mixdepth >= mixdepth_count
            ):
                raise ChannelRingNodeClaimConflictError(
                    "ring journal belongs to a different wallet"
                )
        if retained:
            if config.node_binding_directory is None:
                raise ChannelRingNodeEnrollmentError(
                    "recovery requires the existing binding directory"
                )
            # Verify separately: two conflicting records must not collapse into one dict entry.
            for binding in retained:
                verify_node_bindings(
                    config.node_binding_directory, {binding.local_node_id: _owner(binding)}
                )
        nodes = await _inspect_nodes(
            config,
            (binding.node_name for binding in retained),
            network=network,
            offer_type=offer_type,
            recovery=True,
        )
        pool = ChannelRingNodePool(
            nodes, config, network, offer_type, wallet_identity, mixdepth_count, store, lease
        )
        for binding in retained:
            pool.for_binding(binding)
        return pool
    except BaseException:
        await _close_nodes(nodes)
        lease.close()
        raise


async def enroll_configured_ring_nodes(
    config: ChannelRingConfig,
    *,
    network: str,
    offer_type: str,
    wallet_identity: str,
    mixdepth_count: int,
    expected_node_ids: Mapping[str, str],
    acknowledge_prior_use: bool,
) -> tuple[RingNodeBinding, ...]:
    """Explicit enrollment only, without starting participants or recovering journals."""
    if acknowledge_prior_use is not True:
        raise ChannelRingNodeEnrollmentError("enrollment requires acknowledgment of prior node use")
    if not config.mixdepth_nodes or config.node_binding_directory is None:
        raise ChannelRingNodeEnrollmentError(
            "enrollment requires explicit mappings and a binding directory"
        )
    if any(mixdepth >= mixdepth_count for mixdepth in config.mixdepth_nodes):
        raise ChannelRingNodeError("configured ring mixdepth is outside this wallet")
    if set(expected_node_ids) != set(config.mixdepth_nodes.values()):
        raise ChannelRingNodeEnrollmentError("pin the expected identity of every mapped node")
    nodes = await _inspect_nodes(
        config, config.mixdepth_nodes.values(), network=network, offer_type=offer_type
    )
    try:
        if any(
            nodes[name].node_info.identity_pubkey != node_id
            for name, node_id in expected_node_ids.items()
        ):
            raise ChannelRingNodeClaimConflictError(
                "live LND identity differs from enrollment request"
            )
        bindings = _mapped_bindings(config, nodes, network=network, wallet_identity=wallet_identity)
        assert config.node_binding_directory is not None
        enroll_node_bindings(
            config.node_binding_directory,
            {binding.local_node_id: _owner(binding) for binding in bindings.values()},
        )
        return tuple(bindings.values())
    finally:
        await _close_nodes(nodes)
