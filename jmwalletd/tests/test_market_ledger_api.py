"""HTTP coverage for authenticated wallet-ledger maintenance."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from jmcore.market_store import MarketStore
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService
from jmwalletd.app import create_app
from jmwalletd.deps import set_daemon_state
from jmwalletd.state import CoinjoinState, DaemonState

_WALLET_NAME = "ledger.jmdat"
_NEXT_WALLET_NAME = "next.jmdat"
_MNEMONIC = "all " * 11 + "all"


class _ObservedLock:
    """An asyncio lock that reports when a route reaches lock acquisition."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.queued = asyncio.Event()

    async def acquire(self) -> bool:
        return await self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> _ObservedLock:
        self.queued.set()
        await self._lock.acquire()
        return self

    async def __aexit__(self, *_args: object) -> None:
        self._lock.release()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _wallet(data_dir: Path) -> tuple[WalletService, MagicMock]:
    backend = MagicMock(spec=BlockchainBackend)
    backend.close = AsyncMock()
    return WalletService(_MNEMONIC, backend, network="regtest", data_dir=data_dir), backend


def _loaded_state(data_dir: Path, wallet: WalletService) -> tuple[DaemonState, str]:
    state = DaemonState(data_dir=data_dir)
    state.wallet_service = wallet
    state.wallet_name = _WALLET_NAME
    return state, state.token_authority.issue(_WALLET_NAME).token


def _app(state: DaemonState) -> FastAPI:
    app = create_app(data_dir=state.data_dir)
    set_daemon_state(state)
    return app


def _ledger_path(wallet_name: str = _WALLET_NAME) -> str:
    return f"/api/v1/wallet/{wallet_name}/market/ledger"


def _write_history(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"external_v1": {}, "used": []}, sort_keys=True), encoding="ascii")


def _tree_snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Capture all test-owned artifacts and modes after wallet construction."""

    snapshot: dict[str, tuple[int, bytes | None]] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        mode = path.lstat().st_mode & 0o777
        snapshot[str(path.relative_to(root))] = (
            mode,
            path.read_bytes() if path.is_file() else None,
        )
    return snapshot


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    headers: dict[str, str] | None,
    body: dict[str, object] | None = None,
) -> httpx.Response:
    return await client.request(method, path, headers=headers, json=body)


@pytest.mark.asyncio
async def test_ledger_diagnosis_is_authenticated_read_only_and_redacted(tmp_path: Path) -> None:
    wallet, backend = _wallet(tmp_path)
    state, token = _loaded_state(tmp_path, wallet)
    app = _app(state)
    before = _tree_snapshot(tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(_ledger_path(), headers=_headers(token))

    assert response.status_code == 200
    assert response.json() == {
        "state": "absent",
        "schema_version": None,
        "reason": "no ledger artifacts",
    }
    assert _tree_snapshot(tmp_path) == before
    assert not get_market_store_path(tmp_path).parent.exists()
    assert not (tmp_path / "cmtdata").exists()
    assert wallet.market_wallet_id not in response.text
    assert "path" not in response.json()
    assert "key" not in response.json()
    assert backend.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "headers", "body", "expected_status"),
    [
        ("GET", _ledger_path(), None, None, 401),
        ("GET", _ledger_path(_NEXT_WALLET_NAME), "valid", None, 404),
        ("POST", f"{_ledger_path()}/activate", None, {"history_confirmed": True}, 401),
        (
            "POST",
            f"{_ledger_path(_NEXT_WALLET_NAME)}/activate",
            "valid",
            {"history_confirmed": True},
            404,
        ),
    ],
    ids=["read-missing-token", "read-wrong-wallet", "write-missing-token", "write-wrong-wallet"],
)
async def test_ledger_auth_and_wallet_matching_reject_before_side_effects(
    tmp_path: Path,
    method: str,
    path: str,
    headers: str | None,
    body: dict[str, object] | None,
    expected_status: int,
) -> None:
    wallet, _ = _wallet(tmp_path)
    state, token = _loaded_state(tmp_path, wallet)
    app = _app(state)
    before = _tree_snapshot(tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await _request(client, method, path, _headers(token) if headers else None, body)

    assert response.status_code == expected_status
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.asyncio
async def test_ledger_activation_requires_explicit_history_confirmation_before_creation(
    tmp_path: Path,
) -> None:
    wallet, _ = _wallet(tmp_path)
    state, token = _loaded_state(tmp_path, wallet)
    app = _app(state)
    before = _tree_snapshot(tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(f"{_ledger_path()}/activate", headers=_headers(token), json={})

    assert response.status_code == 400
    assert _tree_snapshot(tmp_path) == before
    assert not get_market_store_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_ledger_activate_block_and_recover_return_durable_states(tmp_path: Path) -> None:
    wallet, backend = _wallet(tmp_path)
    history = get_used_commitments_path(tmp_path)
    _write_history(history)
    state, token = _loaded_state(tmp_path, wallet)
    app = _app(state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        activated = await client.post(
            f"{_ledger_path()}/activate", headers=_headers(token), json={"history_confirmed": True}
        )
        blocked = await client.post(f"{_ledger_path()}/block", headers=_headers(token), json={})
        recovered = await client.post(
            f"{_ledger_path()}/recover", headers=_headers(token), json={"history_confirmed": True}
        )

    assert activated.status_code == 200
    assert activated.json()["state"] == "ready"
    assert activated.json()["schema_version"] == "2"
    assert blocked.status_code == 200
    assert blocked.json()["state"] == "recovery_required"
    assert recovered.status_code == 200
    assert recovered.json()["state"] == "ready"
    assert backend.mock_calls == []


@pytest.mark.asyncio
async def test_ledger_recovery_refuses_missing_history(tmp_path: Path) -> None:
    wallet, _ = _wallet(tmp_path)
    history = get_used_commitments_path(tmp_path)
    _write_history(history)
    state, token = _loaded_state(tmp_path, wallet)
    app = _app(state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (
            await client.post(
                f"{_ledger_path()}/activate",
                headers=_headers(token),
                json={"history_confirmed": True},
            )
        ).status_code == 200
        assert (
            await client.post(f"{_ledger_path()}/block", headers=_headers(token), json={})
        ).status_code == 200
        history.unlink()
        database = get_market_store_path(tmp_path)
        intent = database.with_name(f"{database.name}.wallet-ledger")
        before = (database.read_bytes(), intent.read_bytes())
        refused = await client.post(
            f"{_ledger_path()}/recover", headers=_headers(token), json={"history_confirmed": True}
        )
        status = await client.get(_ledger_path(), headers=_headers(token))

    assert refused.status_code == 400
    assert not history.exists()
    assert (database.read_bytes(), intent.read_bytes()) == before
    assert status.status_code == 200
    assert status.json()["state"] == "recovery_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("activate", "block", "recover", "rebind"))
@pytest.mark.parametrize("busy_state", ("coinjoin", "seller"))
async def test_ledger_maintenance_refuses_active_services_before_writes(
    tmp_path: Path, operation: str, busy_state: str
) -> None:
    wallet, _ = _wallet(tmp_path)
    state, token = _loaded_state(tmp_path, wallet)
    if busy_state == "coinjoin":
        state.coinjoin_state = CoinjoinState.TAKER_RUNNING
    else:
        state._market_seller_ref = MagicMock()
    app = _app(state)
    before = _tree_snapshot(tmp_path)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"{_ledger_path()}/{operation}", headers=_headers(token), json={}
        )

    assert response.status_code == 400
    assert response.json() == {"message": "Stop wallet trading services before ledger maintenance."}
    assert _tree_snapshot(tmp_path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", _ledger_path(), None),
        ("POST", f"{_ledger_path()}/activate", {"history_confirmed": True}),
    ],
    ids=["read", "write"],
)
async def test_ledger_rejects_wallet_and_daemon_directory_mismatch(
    tmp_path: Path, method: str, path: str, body: dict[str, object] | None
) -> None:
    daemon_dir = tmp_path / "daemon"
    wallet_dir = tmp_path / "wallet"
    wallet, _ = _wallet(wallet_dir)
    state, token = _loaded_state(daemon_dir, wallet)
    app = _app(state)
    daemon_before = _tree_snapshot(daemon_dir)
    wallet_before = _tree_snapshot(wallet_dir)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await _request(client, method, path, _headers(token), body)

    assert response.status_code == 400
    assert response.json() == {"message": "Wallet and daemon must use the same data directory."}
    assert _tree_snapshot(daemon_dir) == daemon_before
    assert _tree_snapshot(wallet_dir) == wallet_before


@pytest.mark.asyncio
async def test_ledger_rebind_uses_canonical_target_and_refuses_invalid_requests(
    tmp_path: Path,
) -> None:
    old_directory = tmp_path / "before-move"
    old_directory.mkdir()
    wallet, backend = _wallet(old_directory)
    source = get_used_commitments_path(old_directory)
    _write_history(source)
    wallet.activate_market_ledger(history_confirmed=True)
    database = get_market_store_path(old_directory)
    old_intent = database.with_name(f"{database.name}.wallet-ledger")
    assert old_intent.read_bytes() == MarketStore._wallet_intent_bytes(source)

    daemon_dir = tmp_path / "daemon"
    old_directory.rename(daemon_dir)
    wallet.data_dir = daemon_dir
    target = get_used_commitments_path(daemon_dir)
    assert target.read_bytes() == json.dumps(
        {"external_v1": {}, "used": []}, sort_keys=True
    ).encode("ascii")
    state, token = _loaded_state(daemon_dir, wallet)
    app = _app(state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        rebound = await client.post(
            f"{_ledger_path()}/rebind",
            headers=_headers(token),
            json={
                "previous_commitments_path": str(source),
                "history_confirmed": True,
                "writers_stopped": True,
            },
        )

        before = (
            get_market_store_path(daemon_dir).read_bytes(),
            get_market_store_path(daemon_dir)
            .with_name(f"{get_market_store_path(daemon_dir).name}.wallet-ledger")
            .read_bytes(),
            target.read_bytes(),
        )
        wrong_prior = await client.post(
            f"{_ledger_path()}/rebind",
            headers=_headers(token),
            json={
                "previous_commitments_path": str(tmp_path / "wrong-history.json"),
                "history_confirmed": True,
                "writers_stopped": True,
            },
        )
        absent_confirmation = await client.post(
            f"{_ledger_path()}/rebind",
            headers=_headers(token),
            json={"previous_commitments_path": str(source), "writers_stopped": True},
        )

    intent = get_market_store_path(daemon_dir).with_name(
        f"{get_market_store_path(daemon_dir).name}.wallet-ledger"
    )
    assert rebound.status_code == 200
    assert rebound.json()["state"] == "ready"
    assert rebound.json()["schema_version"] == "2"
    assert target == daemon_dir / "cmtdata" / "commitments.json"
    assert intent.read_bytes() == MarketStore._wallet_intent_bytes(target)
    assert not source.exists()
    with MarketStore(
        get_market_store_path(daemon_dir), wallet_id=wallet.market_wallet_id, create=False
    ) as store:
        store.check_commitments_path(target)
        assert store.wallet_state() == "ready"
    assert wrong_prior.status_code == 400
    assert absent_confirmation.status_code == 400
    assert (
        get_market_store_path(daemon_dir).read_bytes(),
        intent.read_bytes(),
        target.read_bytes(),
    ) == before
    assert backend.mock_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("GET", "POST"), ids=["read", "mutation"])
async def test_queued_old_token_cannot_access_ledger_after_wallet_replacement(
    tmp_path: Path, method: str
) -> None:
    wallet, _ = _wallet(tmp_path)
    next_wallet, _ = _wallet(tmp_path)
    state, old_token = _loaded_state(tmp_path, wallet)
    observed_lock = _ObservedLock()
    state.wallet_lifecycle_lock = observed_lock
    app = _app(state)
    before = _tree_snapshot(tmp_path)
    await observed_lock.acquire()
    request_task: asyncio.Task[httpx.Response] | None = None

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            if method == "GET":
                request_task = asyncio.create_task(
                    client.get(_ledger_path(), headers=_headers(old_token))
                )
            else:
                request_task = asyncio.create_task(
                    client.post(
                        f"{_ledger_path()}/activate",
                        headers=_headers(old_token),
                        json={"history_confirmed": True},
                    )
                )
            await observed_lock.queued.wait()

            state.wallet_service = next_wallet
            state.wallet_name = _NEXT_WALLET_NAME
            state.token_authority.reset()
            state.token_authority.issue(_NEXT_WALLET_NAME)
            observed_lock.release()
            response = await request_task

        assert response.status_code == 401
        assert _tree_snapshot(tmp_path) == before
    finally:
        if observed_lock.locked():
            observed_lock.release()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
