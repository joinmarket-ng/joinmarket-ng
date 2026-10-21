"""Fresh node initialization checks prerequisites and preserves every older state."""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import httpx
import pytest
from bitcointx.core.key import CKey
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.crypto import mnemonic_to_seed
from jmcore.settings import validate_config_document
from jmcore.wallet_metadata import WalletIdentity, register_identity
from jmswap.lightning_context import lightning_context
from jmswap.managed_lnd import ManagedLnd
from jmswap.managed_nodes import ManagedNodeError, NodeState, allocate_node, new_registry
from pydantic import SecretStr

from jmwallet import setup_lightning
from jmwallet.setup_lightning import SetupLightningError, initialize_managed_nodes

PHRASE = " ".join(["all"] * 12)
PASSWORD = "private-core-password"


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    settings = validate_config_document(
        {
            "data_dir": str(tmp_path / "data"),
            "network_config": {"network": "regtest"},
            "wallet": {"address_type": "p2tr", "mixdepth_count": 2},
            "lightning": {"mode": "managed"},
            "bitcoin": {
                "rpc_url": "http://core.test:18443",
                "rpc_user": "unit",
                "rpc_password": PASSWORD,
            },
        }
    )
    wallet = tmp_path / "wallet"
    wallet.write_text(PHRASE)
    ctx = lightning_context(settings, mnemonic_to_seed(PHRASE, ""), with_keys=False)
    ctx.env.lnd_binary.parent.mkdir(parents=True)
    ctx.env.lnd_binary.write_bytes(b"mock verified executable")
    ctx.env.lnd_binary.chmod(0o700)
    state = SimpleNamespace(
        settings=settings,
        wallet=wallet,
        ctx=ctx,
        events=[],
        keys=[],
        genesis=GENESIS_BLOCK_HASHES["regtest"],
        syncing=False,
        bootstrap="NOTICE BOOTSTRAP PROGRESS=100 TAG=done",
        auth_failure=False,
        rpc_failure=False,
        redirect=False,
    )
    real_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        state.events.append(payload["method"])
        assert payload["method"] in {"getblockhash", "getblockchaininfo"}
        assert request.url.host == "core.test"
        assert request.headers.get("authorization", "").startswith("Basic ")
        if state.redirect:
            return httpx.Response(302, headers={"location": "http://must-not-follow.test"})
        if state.rpc_failure:
            return httpx.Response(500, json={"error": {"message": PASSWORD}})
        result = (
            state.genesis
            if payload["method"] == "getblockhash"
            else {"initialblockdownload": state.syncing}
        )
        return httpx.Response(200, json={"result": result, "error": None})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(setup_lightning.httpx, "AsyncClient", client)

    class Tor:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def connect(self) -> None:
            state.events.append("tor-connect")

        async def authenticate(self) -> None:
            state.events.append("tor-auth")
            if state.auth_failure:
                raise ValueError(PASSWORD)

        async def get_info(self, key: str) -> str:
            assert key == "status/bootstrap-phase"
            state.events.append("tor-bootstrap")
            return state.bootstrap

        async def close(self) -> None:
            state.events.append("tor-close")

    monkeypatch.setattr(setup_lightning, "TorControlClient", Tor)

    class Keys:
        def __init__(self, seed: bytes, network: str) -> None:
            assert network == "regtest"
            self.closed = False
            state.keys.append(self)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(setup_lightning, "WalletLightningKeys", Keys)

    async def create(store: Any, keys: Any, env: Any, **kwargs: Any) -> Any:
        assert not keys.closed
        with pytest.raises(ManagedNodeError):
            with store.exclusive_processes():
                pytest.fail("initialization must retain process ownership")
        state.events.append("create")
        registry = new_registry(env.network, kwargs["wallet_identity"])
        for md in kwargs["mixdepths"]:
            node = allocate_node(registry, md, birthday=1, ports=(20001 + md * 2, 20002 + md * 2))
            node.state = NodeState.ACTIVE
            node.identity_pubkey = bytes(CKey(bytes([md + 1]) * 32).pub).hex()
            node.onion_endpoint = chr(ord("a") + md) * 56 + ".onion:9735"
        return store.update(lambda current: registry)

    create_mock = Mock(wraps=create)
    monkeypatch.setattr(setup_lightning, "create_nodes", create_mock)
    state.create = create_mock
    state.download = Mock(return_value=ctx.env.lnd_binary)
    monkeypatch.setattr(setup_lightning, "download_and_install", state.download)
    state.verified = Mock(return_value=True)
    monkeypatch.setattr(setup_lightning, "is_installed", state.verified)
    return state


def initialize(state: Any, **kwargs: Any) -> Any:
    options = {"consumers_excluded": True, **kwargs}
    return initialize_managed_nodes(
        state.settings,
        SecretStr(PHRASE),
        SecretStr(""),
        wallet_file=state.wallet,
        data_dir=state.settings.get_data_dir(),
        **options,
    )


def test_explicit_initialization_creates_only_after_positive_readiness_and_closes_keys(
    fixture: Any,
) -> None:
    state = fixture
    registry = initialize(state)
    assert len(registry.nodes) == 2 and all(
        node.state is NodeState.ACTIVE for node in registry.nodes
    )
    assert state.events.index("create") > state.events.index("tor-close")
    assert state.events[:2] == ["getblockhash", "getblockchaininfo"]
    assert len(state.keys) == 1 and state.keys[0].closed
    state.download.assert_not_called()
    with state.ctx.store.exclusive_processes():
        pass  # Released only after the awaited creator and key cleanup.


@pytest.mark.parametrize(
    "problem", ["registry", "partial", "backup", "binding", "orphan", "unknown-version"]
)
def test_existing_or_ambiguous_artifacts_are_never_adopted_or_modified(
    fixture: Any, problem: str
) -> None:
    state = fixture
    paths = state.ctx.paths
    paths.root.mkdir(parents=True)
    if problem in {"registry", "partial"}:
        registry = new_registry("regtest", state.ctx.wallet_identity)
        node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
        if problem == "registry":
            node.state = NodeState.ACTIVE
        state.ctx.store.update(lambda current: registry)
    elif problem == "unknown-version":
        paths.registry.write_text('{"version":999}')
    else:
        path = {
            "backup": paths.backup_dir / "old.backup",
            "binding": paths.bindings_dir / "old.json",
            "orphan": paths.nodes_dir / "old-node" / "wallet.db",
        }[problem]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"retained evidence")
    before = {str(p): p.read_bytes() for p in paths.root.rglob("*") if p.is_file()}
    with pytest.raises(SetupLightningError):
        initialize(state)
    assert {str(p): p.read_bytes() for p in paths.root.rglob("*") if p.is_file()} == before
    assert not state.events and not state.keys
    state.create.assert_not_called()
    state.download.assert_not_called()


def test_lockfiles_alone_do_not_authorize_creation_without_explicit_exclusion(fixture: Any) -> None:
    state = fixture
    with state.ctx.store.exclusive_processes():
        pass
    with pytest.raises(SetupLightningError, match="Exclude"):
        initialize(state, consumers_excluded=False)
    assert state.ctx.store.load() is None
    assert not state.events
    assert len(initialize(state).nodes) == 2


@pytest.mark.parametrize("problem", ["network", "syncing", "bootstrap", "auth", "rpc", "redirect"])
def test_failed_prerequisites_never_download_or_allocate_and_errors_are_secret_free(
    fixture: Any, problem: str
) -> None:
    state = fixture
    if problem == "network":
        state.genesis = "00" * 32
    elif problem == "syncing":
        state.syncing = True
    elif problem == "bootstrap":
        state.bootstrap = "NOTICE BOOTSTRAP PROGRESS=10"
    elif problem == "auth":
        state.auth_failure = True
    elif problem == "rpc":
        state.rpc_failure = True
    else:
        state.redirect = True
    with pytest.raises(SetupLightningError) as error:
        initialize(state, install_lnd=True)
    text = "".join(traceback.format_exception(error.value))
    assert PHRASE not in text and PASSWORD not in text
    assert not state.ctx.paths.root.exists() and not state.keys
    state.create.assert_not_called()
    state.download.assert_not_called()
    if problem in {"bootstrap", "auth"}:
        assert state.events[-1] == "tor-close"
    if problem == "redirect":
        assert state.events == ["getblockhash"]


def test_missing_binary_requires_separate_install_authorization(fixture: Any) -> None:
    state = fixture
    state.verified.return_value = False
    with pytest.raises(SetupLightningError, match="--install-lnd"):
        initialize(state)
    assert not state.events and not state.keys
    state.download.assert_not_called()


def test_nonexecutable_binary_never_allocates_or_repairs_permissions(fixture: Any) -> None:
    state = fixture
    state.ctx.env.lnd_binary.chmod(0o600)
    with pytest.raises(SetupLightningError, match="not executable"):
        initialize(state)
    assert state.ctx.env.lnd_binary.stat().st_mode & 0o777 == 0o600
    assert not state.events and not state.keys
    state.create.assert_not_called()


def test_authorized_install_uses_tor_and_must_pass_pinned_verification(fixture: Any) -> None:
    state = fixture
    state.verified.side_effect = [False, True]
    registry = initialize(state, install_lnd=True)
    assert len(registry.nodes) == 2
    state.download.assert_called_once_with(
        state.settings.get_data_dir(), proxy="socks5h://127.0.0.1:9050"
    )


def test_explicit_cookie_authentication_matches_lnd_cookie_precedence(
    fixture: Any, tmp_path: Path
) -> None:
    state = fixture
    cookie = tmp_path / ".cookie"
    cookie.write_text("__cookie__:cookie-private-password")
    state.settings.bitcoin.rpc_cookie_file = str(cookie)
    assert setup_lightning._core_auth(
        lightning_context(
            state.settings,
            mnemonic_to_seed(PHRASE, ""),
            with_keys=False,
        )
    ) == ("__cookie__", "cookie-private-password")
    assert len(initialize(state).nodes) == 2
    assert cookie.read_text() == "__cookie__:cookie-private-password"


@pytest.mark.parametrize(
    "url", ["https://core.test:18443", "http://user:private-core-password@core.test:18443"]
)
def test_unsupported_transport_refuses_before_readiness_or_any_allocation(
    fixture: Any, url: str
) -> None:
    state = fixture
    state.settings.bitcoin.rpc_url = url
    with pytest.raises(SetupLightningError) as error:
        initialize(state)
    assert "private-core-password" not in str(error.value)
    assert not state.events and not state.ctx.paths.root.exists()
    state.create.assert_not_called()
    state.download.assert_not_called()


def test_failed_binary_verification_never_allocates_nodes(fixture: Any) -> None:
    state = fixture
    state.verified.return_value = False
    with pytest.raises(SetupLightningError, match="pinned release"):
        initialize(state, install_lnd=True)
    assert not state.ctx.paths.root.exists() and not state.keys


def test_custom_unverified_executable_is_not_replaced(fixture: Any, tmp_path: Path) -> None:
    state = fixture
    binary = tmp_path / "custom-lnd"
    binary.write_bytes(b"operator executable")
    state.settings.lightning.lnd_binary = str(binary)
    state.verified.return_value = False
    with pytest.raises(SetupLightningError, match="custom LND"):
        initialize(state, install_lnd=True)
    assert binary.read_bytes() == b"operator executable"
    assert not state.events
    state.download.assert_not_called()


def test_registered_identity_mismatch_never_derives_or_initializes_other_nodes(
    fixture: Any,
) -> None:
    state = fixture
    register_identity(state.wallet, WalletIdentity(fingerprint="00000000", bip39="required"))
    with pytest.raises(SetupLightningError, match="registered wallet identity"):
        initialize(state)
    assert not state.events and not state.keys


def test_cancellation_during_child_shutdown_retains_ownership_and_keys_until_cleanup(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    child = SimpleNamespace(returncode=None, reaped=False)

    async def interrupted(store: Any, keys: Any, env: Any, **kwargs: Any) -> Any:
        registry = new_registry("regtest", kwargs["wallet_identity"])
        node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
        store.update(lambda previous: registry)
        lnd = ManagedLnd(node, store.paths, env)
        waiting, exited = asyncio.Event(), asyncio.Event()

        async def wait() -> int:
            waiting.set()
            await exited.wait()
            child.reaped = True
            return 0

        child.wait = wait
        lnd._process = cast(Any, child)
        parent = asyncio.current_task()
        assert parent is not None

        async def cancel() -> None:
            await waiting.wait()
            parent.cancel()
            await asyncio.sleep(0)
            assert not keys.closed and not child.reaped
            with pytest.raises(ManagedNodeError):
                with store.exclusive_processes():
                    pytest.fail("ownership released during cleanup")
            child.returncode = 0
            exited.set()

        canceler = asyncio.create_task(cancel())
        try:
            await lnd.stop()
        finally:
            await canceler
            assert child.reaped and not keys.closed

    monkeypatch.setattr(setup_lightning, "create_nodes", interrupted)
    with pytest.raises(SetupLightningError, match="interrupted"):
        initialize(state)
    assert child.reaped and state.keys[0].closed
    assert state.ctx.store.load().nodes[0].state is NodeState.PROVISIONING
    with state.ctx.store.exclusive_processes():
        pass
    with pytest.raises(SetupLightningError, match="already exists"):
        initialize(state)
    assert len(state.keys) == 1  # Partial state was not regenerated or resumed.
