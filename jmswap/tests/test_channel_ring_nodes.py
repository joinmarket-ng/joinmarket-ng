"""Node ownership must survive restarts without startup inventing history."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig, NodeOwner, RingNodeBinding
from jmcore.channel_ring_store import (
    RingParticipantRecord,
    RingParticipantRole,
    RingParticipantStore,
)
from jmcore.secure_files import atomic_write_private
from pydantic import ValidationError

from jmswap.channel_ring_nodes import (
    REGISTRY_FILE_NAME,
    ChannelRingNodeClaimConflictError,
    ChannelRingNodeEnrollmentError,
    ChannelRingNodeError,
    ChannelRingNodePool,
    ChannelRingNodeRegistryError,
    channel_ring_wallet_identity,
    enroll_configured_ring_nodes,
    enroll_node_bindings,
    initialize_channel_ring_nodes,
    verify_node_bindings,
)
from jmswap.lnd import LndBackend, LndNodeInfo


def pubkey(number: int) -> str:
    return bytes(CKey.from_secret_bytes(number.to_bytes(32, "big")).pub).hex()


def owner(mixdepth: int = 0, wallet: str = "a" * 64) -> NodeOwner:
    return NodeOwner(network="regtest", wallet_identity=wallet, source_mixdepth=mixdepth)


def test_wallet_identity_is_stable_and_not_a_short_fingerprint() -> None:
    public = bytes.fromhex(pubkey(1))
    identity = channel_ring_wallet_identity(public)
    assert len(identity) == 64
    assert identity == channel_ring_wallet_identity(public)
    assert identity != channel_ring_wallet_identity(bytes.fromhex(pubkey(2)))
    with pytest.raises(ValueError, match="compressed master public key"):
        channel_ring_wallet_identity(b"\x02" + b"\xff" * 32)


@pytest.mark.parametrize("mixdepth", [None, True, -1, "0"])
def test_owner_has_no_unscoped_or_coerced_mixdepth(mixdepth: object) -> None:
    with pytest.raises(ValidationError):
        NodeOwner.model_validate(
            {"network": "regtest", "wallet_identity": "a" * 64, "source_mixdepth": mixdepth}
        )


def test_missing_registry_does_not_create_or_infer_ownership(tmp_path: Path) -> None:
    directory = tmp_path / "not-created"
    with pytest.raises(ChannelRingNodeEnrollmentError):
        verify_node_bindings(directory, {pubkey(1): owner()})
    assert not directory.exists()


def test_explicit_enrollment_is_idempotent_and_verification_is_read_only(tmp_path: Path) -> None:
    claims = {pubkey(1): owner()}
    enroll_node_bindings(tmp_path, claims)
    path = tmp_path / REGISTRY_FILE_NAME
    before = path.stat().st_mtime_ns, path.read_bytes()
    verify_node_bindings(tmp_path, claims)
    enroll_node_bindings(tmp_path, claims)
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "changed",
    [
        owner(1),
        owner(wallet="b" * 64),
        NodeOwner(network="signet", wallet_identity="a" * 64, source_mixdepth=0),
    ],
)
def test_claim_cannot_cross_network_wallet_or_mixdepth(tmp_path: Path, changed: NodeOwner) -> None:
    enroll_node_bindings(tmp_path, {pubkey(1): owner()})
    before = (tmp_path / REGISTRY_FILE_NAME).read_bytes()
    for operation in (enroll_node_bindings, verify_node_bindings):
        with pytest.raises(ChannelRingNodeClaimConflictError):
            operation(tmp_path, {pubkey(1): changed})
        assert (tmp_path / REGISTRY_FILE_NAME).read_bytes() == before


def test_conflicting_batch_never_partially_enrolls(tmp_path: Path) -> None:
    enroll_node_bindings(tmp_path, {pubkey(1): owner()})
    before = (tmp_path / REGISTRY_FILE_NAME).read_bytes()
    with pytest.raises(ChannelRingNodeClaimConflictError):
        enroll_node_bindings(tmp_path, {pubkey(2): owner(2), pubkey(1): owner(1)})
    assert (tmp_path / REGISTRY_FILE_NAME).read_bytes() == before
    with pytest.raises(ChannelRingNodeEnrollmentError):
        verify_node_bindings(tmp_path, {pubkey(2): owner(2)})


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"version":1,"bindings":{}}',
        b'{"registry_kind":"channel_ring_node_bindings","bindings":{},"bindings":{}}',
        b'{"registry_kind":"channel_ring_node_bindings","bindings":[]}',
        b'{"registry_kind":"channel_ring_node_bindings","bindings":{"invalid":{}}}',
    ],
)
def test_untrusted_registry_is_preserved_without_repair(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / REGISTRY_FILE_NAME
    atomic_write_private(path, raw)
    for operation in (enroll_node_bindings, verify_node_bindings):
        with pytest.raises(ChannelRingNodeRegistryError):
            operation(tmp_path, {pubkey(1): owner()})
        assert path.read_bytes() == raw


def test_missing_individual_claim_is_not_backfilled(tmp_path: Path) -> None:
    enroll_node_bindings(tmp_path, {pubkey(1): owner()})
    before = (tmp_path / REGISTRY_FILE_NAME).read_bytes()
    with pytest.raises(ChannelRingNodeEnrollmentError):
        verify_node_bindings(tmp_path, {pubkey(1): owner(), pubkey(2): owner(1)})
    assert (tmp_path / REGISTRY_FILE_NAME).read_bytes() == before


def test_concurrent_processes_cannot_claim_the_same_identity(tmp_path: Path) -> None:
    code = """
import sys
from pathlib import Path
from jmcore.channel_ring import NodeOwner
from jmswap.channel_ring_nodes import (
    ChannelRingNodeClaimConflictError, enroll_node_bindings,
)
owner = NodeOwner(network='regtest', wallet_identity='a' * 64,
                  source_mixdepth=int(sys.argv[3]))
try:
    enroll_node_bindings(Path(sys.argv[1]), {sys.argv[2]: owner})
except ChannelRingNodeClaimConflictError:
    raise SystemExit(7)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path), pubkey(1), str(mixdepth)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for mixdepth in (0, 1)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode in (0, 7), (stdout, stderr)
        assert sorted(process.returncode for process in processes) == [0, 7]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    bindings = json.loads((tmp_path / REGISTRY_FILE_NAME).read_text())["bindings"]
    assert list(bindings) == [pubkey(1)]


def pool_config(tmp_path: Path) -> ChannelRingConfig:
    node = ChannelRingNodeConfig(
        lnd_grpc_url="127.0.0.1:10009",
        lnd_tls_cert_path=tmp_path / "tls.cert",
        lnd_macaroon_path=tmp_path / "admin.macaroon",
        onion_endpoint="a" * 56 + ".onion:9735",
    )
    return ChannelRingConfig(
        enabled=True,
        nodes={"md0": node},
        mixdepth_nodes={0: "md0"},
        node_binding_directory=tmp_path / "bindings",
    )


@pytest.fixture
def fake_lnd(monkeypatch: pytest.MonkeyPatch) -> list[AsyncMock]:
    clients: list[AsyncMock] = []

    def create(*args: Any, **kwargs: Any) -> AsyncMock:
        assert args[0] == "127.0.0.1:10009", "unused endpoint must not be contacted"
        node = AsyncMock()
        node.check_production_private_channel_ring.return_value = LndNodeInfo(
            identity_pubkey=pubkey(1),
            network="regtest",
            version="test",
            synced_to_chain=True,
            wallet_synced=True,
            feature_bits=frozenset({81, 47}),
            advertised_uris=(),
        )
        clients.append(node)
        return node

    monkeypatch.setattr(LndBackend, "from_paths", create)
    return clients


async def open_pool(config: ChannelRingConfig, tmp_path: Path) -> ChannelRingNodePool:
    return await initialize_channel_ring_nodes(
        config,
        network="regtest",
        offer_type="tr0absoffer",
        wallet_identity="a" * 64,
        mixdepth_count=5,
        data_directory=tmp_path,
    )


def retained_record(config: ChannelRingConfig, tmp_path: Path) -> RingParticipantRecord:
    record = RingParticipantRecord.fresh(
        round_nonce="11" * 32,
        revision=0,
        taker_session_identity="test",
        local_role=RingParticipantRole.MAKER,
        local_position=0,
        node_binding=RingNodeBinding(
            **owner().model_dump(),
            node_name="md0",
            local_node_id=pubkey(1),
        ),
    )
    store = RingParticipantStore(
        config.persistence_path(tmp_path),
        max_active_sessions=4,
        max_verified_sessions=2,
    )
    store.save(record)
    return record


async def test_startup_never_enrolls_missing_claims(
    tmp_path: Path, fake_lnd: list[AsyncMock]
) -> None:
    config = pool_config(tmp_path)
    assert config.node_binding_directory is not None
    pool = await open_pool(config, tmp_path)
    try:
        with pytest.raises(ChannelRingNodeEnrollmentError):
            await pool.enable_funding()
        assert not config.node_binding_directory.exists()
        assert not pool.mixdepths
        fake_lnd[0].close.assert_awaited_once()
    finally:
        await pool.close()


async def test_explicit_enrollment_pins_live_identity(
    tmp_path: Path, fake_lnd: list[AsyncMock]
) -> None:
    config = pool_config(tmp_path)
    assert config.node_binding_directory is not None
    kwargs: dict[str, Any] = {
        "network": "regtest",
        "offer_type": "tr0absoffer",
        "wallet_identity": "a" * 64,
        "mixdepth_count": 5,
        "acknowledge_prior_use": True,
    }
    with pytest.raises(ChannelRingNodeClaimConflictError):
        await enroll_configured_ring_nodes(config, expected_node_ids={"md0": pubkey(2)}, **kwargs)
    assert not config.node_binding_directory.exists()
    await enroll_configured_ring_nodes(config, expected_node_ids={"md0": pubkey(1)}, **kwargs)
    pool = await open_pool(config, tmp_path)
    try:
        await pool.enable_funding()
        assert pool.mixdepths == {0}
        assert pool.for_mixdepth(0).binding.local_node_id == pubkey(1)
        with pytest.raises(ChannelRingNodeError, match="no enrolled"):
            pool.for_mixdepth(1)
    finally:
        await pool.close()
    for client in fake_lnd:
        client.close.assert_awaited_once()


async def test_recovery_without_funding_mapping_ignores_unused_nodes(
    tmp_path: Path, fake_lnd: list[AsyncMock]
) -> None:
    config = pool_config(tmp_path)
    record = retained_record(config, tmp_path)
    assert config.node_binding_directory is not None
    enroll_node_bindings(config.node_binding_directory, {pubkey(1): owner()})
    config = config.model_copy(
        update={
            "enabled": False,
            "mixdepth_nodes": {},
            "nodes": config.nodes
            | {
                "unavailable": config.nodes["md0"].model_copy(
                    update={"lnd_grpc_url": "127.0.0.1:10010"}
                )
            },
        }
    )
    pool = await open_pool(config, tmp_path)
    try:
        assert not pool.mixdepths
        assert pool.for_binding(record.node_binding).backend is fake_lnd[0]
        with pytest.raises(ChannelRingNodeError, match="funding is disabled"):
            await pool.enable_funding()
    finally:
        await pool.close()


async def test_missing_recovery_claim_never_contacts_lnd(
    tmp_path: Path, fake_lnd: list[AsyncMock]
) -> None:
    config = pool_config(tmp_path)
    record = retained_record(config, tmp_path)
    path = config.persistence_path(tmp_path) / record.key.filename
    before = path.read_bytes()
    with pytest.raises(ChannelRingNodeEnrollmentError):
        await open_pool(config, tmp_path)
    assert not fake_lnd
    assert path.read_bytes() == before


async def test_old_journal_is_preserved_without_backfill(
    tmp_path: Path, fake_lnd: list[AsyncMock]
) -> None:
    config = pool_config(tmp_path)
    record = retained_record(config, tmp_path)
    path = config.persistence_path(tmp_path) / record.key.filename
    old = record.model_dump(mode="json")
    del old["node_binding"]
    atomic_write_private(path, json.dumps(old).encode())
    before = path.read_bytes()
    with pytest.raises(ChannelRingNodeRegistryError, match="provenance"):
        await open_pool(config, tmp_path)
    assert not fake_lnd
    assert path.read_bytes() == before


async def test_journal_lease_prevents_second_runtime_and_releases_on_close(
    tmp_path: Path, fake_lnd: list[AsyncMock]
) -> None:
    config = pool_config(tmp_path)
    pool = await open_pool(config, tmp_path)
    try:
        with pytest.raises(BlockingIOError):
            await open_pool(config, tmp_path)
        assert not fake_lnd
    finally:
        await pool.close()
    again = await open_pool(config, tmp_path)
    await again.close()


async def test_duplicate_live_node_aliases_cannot_be_enrolled(
    tmp_path: Path, fake_lnd: list[AsyncMock]
) -> None:
    config = pool_config(tmp_path)
    config = config.model_copy(
        update={
            "nodes": config.nodes | {"alias": config.nodes["md0"]},
            "mixdepth_nodes": {0: "md0", 1: "alias"},
        }
    )
    with pytest.raises(ChannelRingNodeClaimConflictError, match="same LND identity"):
        await enroll_configured_ring_nodes(
            config,
            network="regtest",
            offer_type="tr0absoffer",
            wallet_identity="a" * 64,
            mixdepth_count=5,
            expected_node_ids={"md0": pubkey(1), "alias": pubkey(1)},
            acknowledge_prior_use=True,
        )
    assert not (tmp_path / "bindings" / REGISTRY_FILE_NAME).exists()
    assert len(fake_lnd) == 2
    for client in fake_lnd:
        client.close.assert_awaited_once()


async def test_invalid_new_mapping_does_not_prevent_recorded_node_recovery(
    tmp_path: Path, fake_lnd: list[AsyncMock]
) -> None:
    config = pool_config(tmp_path)
    record = retained_record(config, tmp_path)
    assert config.node_binding_directory is not None
    enroll_node_bindings(config.node_binding_directory, {pubkey(1): owner()})
    config = config.model_copy(update={"mixdepth_nodes": {1: "md0"}})
    pool = await open_pool(config, tmp_path)
    try:
        with pytest.raises(ChannelRingNodeClaimConflictError):
            await pool.enable_funding()
        assert pool.for_binding(record.node_binding).backend is fake_lnd[0]
        assert not pool.mixdepths
    finally:
        await pool.close()


async def test_backend_initialization_cancellation_closes_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = AsyncMock()
    entered = asyncio.Event()

    async def checking(*args: Any, **kwargs: Any) -> None:
        entered.set()
        await asyncio.Event().wait()

    client.check_production_private_channel_ring.side_effect = checking
    monkeypatch.setattr(LndBackend, "from_paths", lambda *args, **kwargs: client)
    pool = await open_pool(pool_config(tmp_path), tmp_path)
    attempt = asyncio.create_task(pool.enable_funding())
    try:
        await entered.wait()
        attempt.cancel()
        with pytest.raises(asyncio.CancelledError):
            await attempt
        client.close.assert_awaited_once()
    finally:
        await pool.close()
