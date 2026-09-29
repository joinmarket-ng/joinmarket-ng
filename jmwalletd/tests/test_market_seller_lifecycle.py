"""Regression coverage for the daemon-owned credential-market seller lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

import taker.wallet_market as wallet_market
from jmcore.market_keys import MarketKeysClosedError, MarketKeyScope
from jmcore.settings import JoinMarketSettings
from jmcore.wallet_market import WalletMarketSellerOptions
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService
from jmwalletd.app import create_app
from jmwalletd.deps import set_daemon_state
from jmwalletd.state import CoinjoinState, DaemonState

_WALLET_NAME = "seller.jmdat"
_NEXT_WALLET_NAME = "next.jmdat"
_MNEMONIC = "all " * 11 + "all"


class _FakeSeller:
    """Small controllable seller replacement for daemon lifecycle tests."""

    instances: ClassVar[list[_FakeSeller]] = []
    start_entered: ClassVar[asyncio.Event | None] = None
    start_release: ClassVar[asyncio.Event | None] = None
    start_finished: ClassVar[asyncio.Event | None] = None
    start_cancelled: ClassVar[asyncio.Event | None] = None
    start_failure: ClassVar[BaseException | None] = None
    stop_errors: ClassVar[list[BaseException]] = []
    stop_observer: ClassVar[Callable[[_FakeSeller], None] | None] = None

    def __init__(
        self,
        wallet: object,
        settings: JoinMarketSettings,
        options: WalletMarketSellerOptions,
    ) -> None:
        self.wallet = wallet
        self.settings = settings
        self.options = options
        self.running = False
        self.stop_calls = 0
        type(self).instances.append(self)

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.start_entered = None
        cls.start_release = None
        cls.start_finished = None
        cls.start_cancelled = None
        cls.start_failure = None
        cls.stop_errors = []
        cls.stop_observer = None

    def status(self) -> dict[str, str | None]:
        return {
            "state": "running" if self.running else "stopped",
            "nickname": "seller-nick",
            "seller_pubkey": "02" + "11" * 32,
        }

    async def start(self) -> None:
        if self.start_entered is not None:
            self.start_entered.set()
        try:
            if self.start_release is not None:
                await self.start_release.wait()
            if self.start_failure is not None:
                raise self.start_failure
            self.running = True
        except asyncio.CancelledError:
            if self.start_cancelled is not None:
                self.start_cancelled.set()
            raise
        finally:
            if self.start_finished is not None:
                self.start_finished.set()

    async def stop(self) -> None:
        self.stop_calls += 1
        observer = type(self).stop_observer
        if observer is not None:
            observer(self)
        if self.stop_errors:
            raise self.stop_errors.pop(0)
        self.running = False


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


def _options() -> WalletMarketSellerOptions:
    return WalletMarketSellerOptions.model_validate(_options_payload())


def _options_payload() -> dict[str, object]:
    return {
        "bond": {"txid": "11" * 32, "vout": 0},
        "products": ["bond"],
        "price_sats": 12_345,
        "quote_ttl": 120,
    }


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _app(state: DaemonState) -> FastAPI:
    app = create_app(data_dir=state.data_dir)
    set_daemon_state(state)
    return app


def _loaded_state(data_dir: Path, wallet: object | None = None) -> tuple[DaemonState, str]:
    state = DaemonState(data_dir=data_dir)
    state.wallet_service = wallet if wallet is not None else MagicMock()
    state.wallet_name = _WALLET_NAME
    token = state.token_authority.issue(_WALLET_NAME).token
    return state, token


def _wallet(data_dir: Path) -> tuple[WalletService, MagicMock]:
    backend = MagicMock(spec=BlockchainBackend)
    backend.close = AsyncMock()
    return WalletService(_MNEMONIC, backend, network="regtest", data_dir=data_dir), backend


def _scope() -> MarketKeyScope:
    return MarketKeyScope(network="regtest", chain_hash="22" * 32, role="seller", period=0)


async def _wait_for_start(state: DaemonState) -> None:
    task = state._market_seller_task
    if task is not None:
        await task


@pytest.fixture(autouse=True)
def _reset_fake_seller() -> None:
    _FakeSeller.reset()


@pytest.mark.asyncio
async def test_authenticated_start_status_stop_maps_options_and_preserves_coinjoin_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _FakeSeller)
    state, token = _loaded_state(tmp_path)
    state.activate_coinjoin_state(CoinjoinState.MAKER_RUNNING)
    app = _app(state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        started = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/start",
            headers=_headers(token),
            json=_options_payload(),
        )
        assert started.status_code == 202
        assert started.json() == {
            "state": "starting",
            "nickname": "seller-nick",
            "seller_pubkey": "02" + "11" * 32,
            "error": None,
        }
        assert len(_FakeSeller.instances) == 1
        seller = _FakeSeller.instances[0]
        assert seller.wallet is state.wallet_service
        assert seller.options == _options()
        assert seller.settings.get_data_dir() == tmp_path

        await _wait_for_start(state)
        status = await client.get(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller", headers=_headers(token)
        )
        assert status.status_code == 200
        assert status.json()["state"] == "running"
        assert state.coinjoin_state is CoinjoinState.MAKER_RUNNING
        assert state.maker_running is True
        assert state.taker_running is False

        stopped = await client.post(
            f"/api/v1/wallet/{_WALLET_NAME}/market/seller/stop", headers=_headers(token)
        )

    assert stopped.status_code == 200
    assert stopped.json() == {
        "state": "stopped",
        "nickname": None,
        "seller_pubkey": None,
        "error": None,
    }
    assert seller.stop_calls == 1
    assert state.coinjoin_state is CoinjoinState.MAKER_RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wallet_name", "headers", "expected_status"),
    [
        (_WALLET_NAME, None, 401),
        (_NEXT_WALLET_NAME, "valid", 404),
        (_WALLET_NAME, "valid", 422),
    ],
    ids=["missing-token", "wrong-wallet", "invalid-options"],
)
async def test_start_rejections_never_construct_a_seller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wallet_name: str,
    headers: str | None,
    expected_status: int,
) -> None:
    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _FakeSeller)
    state, token = _loaded_state(tmp_path)
    app = _app(state)
    invalid_options = _options_payload() | {"price_sats": 0}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/wallet/{wallet_name}/market/seller/start",
            headers=_headers(token) if headers is not None else None,
            json=invalid_options,
        )

    assert response.status_code == expected_status
    assert not _FakeSeller.instances


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["", "/start", "/stop"],
    ids=["status", "start", "stop"],
)
async def test_queued_old_session_request_cannot_act_on_replaced_wallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _FakeSeller)
    state, old_token = _loaded_state(tmp_path)
    observed_lock = _ObservedLock()
    state.wallet_lifecycle_lock = observed_lock
    app = _app(state)
    await observed_lock.acquire()
    request_task: asyncio.Task[httpx.Response] | None = None

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            path = f"/api/v1/wallet/{_WALLET_NAME}/market/seller{endpoint}"
            if endpoint == "":
                request_task = asyncio.create_task(client.get(path, headers=_headers(old_token)))
            elif endpoint == "/start":
                request_task = asyncio.create_task(
                    client.post(path, headers=_headers(old_token), json=_options_payload())
                )
            else:
                request_task = asyncio.create_task(client.post(path, headers=_headers(old_token)))
            await observed_lock.queued.wait()

            state.wallet_service = MagicMock()
            state.wallet_name = _NEXT_WALLET_NAME
            state.token_authority.reset()
            state.token_authority.issue(_NEXT_WALLET_NAME)
            next_seller = _FakeSeller(state.wallet_service, MagicMock(), _options())
            state._market_seller_ref = next_seller
            initial_instances = len(_FakeSeller.instances)

            observed_lock.release()
            response = await request_task

        assert response.status_code == 401
        assert next_seller.stop_calls == 0
        assert len(_FakeSeller.instances) == initial_instances
    finally:
        if observed_lock.locked():
            observed_lock.release()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_wallet_lock_cancels_pending_seller_after_revoking_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _FakeSeller)
    wallet, backend = _wallet(tmp_path)
    state, _ = _loaded_state(tmp_path, wallet)
    _FakeSeller.start_entered = asyncio.Event()
    _FakeSeller.start_release = asyncio.Event()
    _FakeSeller.start_cancelled = asyncio.Event()

    def check_keys_revoked(_seller: _FakeSeller) -> None:
        with pytest.raises(MarketKeysClosedError):
            wallet.market_keys.signing_public_key(_scope())

    _FakeSeller.stop_observer = check_keys_revoked
    async with state.wallet_lifecycle_lock:
        await state.start_market_seller(MagicMock(), _options())
    await _FakeSeller.start_entered.wait()

    assert await state.lock_wallet() is False
    assert _FakeSeller.start_cancelled.is_set()
    assert _FakeSeller.instances[0].stop_calls == 1
    assert state._market_seller_ref is None
    assert state._market_seller_task is None
    assert state.wallet_service is None
    backend.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_wallet_lock_retains_revoked_wallet_until_seller_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _FakeSeller)
    wallet, _ = _wallet(tmp_path)
    state, _ = _loaded_state(tmp_path, wallet)
    _FakeSeller.stop_errors = [RuntimeError("synthetic close failure")]

    async with state.wallet_lifecycle_lock:
        await state.start_market_seller(MagicMock(), _options())
    await _wait_for_start(state)
    seller = _FakeSeller.instances[0]

    with pytest.raises(RuntimeError, match="synthetic close failure"):
        await state.lock_wallet()

    assert state.wallet_service is wallet
    assert state._market_seller_ref is seller
    with pytest.raises(MarketKeysClosedError):
        wallet.market_keys.signing_public_key(_scope())

    assert await state.lock_wallet() is False
    assert seller.stop_calls == 2
    assert state.wallet_service is None
    assert state._market_seller_ref is None


@pytest.mark.asyncio
async def test_failed_startup_is_sanitized_and_requires_successful_prior_stop_to_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _FakeSeller)
    state, _ = _loaded_state(tmp_path)
    _FakeSeller.start_failure = RuntimeError("private transport failure")

    async with state.wallet_lifecycle_lock:
        await state.start_market_seller(MagicMock(), _options())
    await _wait_for_start(state)
    failed_seller = _FakeSeller.instances[0]
    assert state.market_seller_status() == {
        "state": "stopped",
        "nickname": "seller-nick",
        "seller_pubkey": "02" + "11" * 32,
        "error": "Market seller startup failed.",
    }

    _FakeSeller.stop_errors = [RuntimeError("synthetic close failure")]
    with pytest.raises(RuntimeError, match="synthetic close failure"):
        async with state.wallet_lifecycle_lock:
            await state.start_market_seller(MagicMock(), _options())
    assert state._market_seller_ref is failed_seller
    assert len(_FakeSeller.instances) == 1

    async with state.wallet_lifecycle_lock:
        await state.stop_market_seller()
        _FakeSeller.start_failure = None
        await state.start_market_seller(MagicMock(), _options())
    await _wait_for_start(state)

    assert len(_FakeSeller.instances) == 2
    assert state._market_seller_ref is _FakeSeller.instances[1]
    async with state.wallet_lifecycle_lock:
        await state.stop_market_seller()


@pytest.mark.asyncio
async def test_stopping_seller_keeps_wallet_backend_and_key_capability_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _FakeSeller)
    wallet, backend = _wallet(tmp_path)
    state, _ = _loaded_state(tmp_path, wallet)
    expected_public_key = wallet.market_keys.signing_public_key(_scope())

    async with state.wallet_lifecycle_lock:
        await state.start_market_seller(MagicMock(), _options())
    await _wait_for_start(state)
    async with state.wallet_lifecycle_lock:
        await state.stop_market_seller()

    assert state.wallet_service is wallet
    assert wallet.market_keys.signing_public_key(_scope()) == expected_public_key
    backend.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifespan_shutdown_revokes_keys_before_stopping_seller(tmp_path: Path) -> None:
    wallet, backend = _wallet(tmp_path)
    state, _ = _loaded_state(tmp_path, wallet)
    seller = _FakeSeller(wallet, MagicMock(), _options())

    def check_keys_revoked(_seller: _FakeSeller) -> None:
        with pytest.raises(MarketKeysClosedError):
            wallet.market_keys.signing_public_key(_scope())

    _FakeSeller.stop_observer = check_keys_revoked
    state._market_seller_ref = seller
    app = _app(state)

    async with app.router.lifespan_context(app):
        pass

    assert seller.stop_calls == 1
    assert state._market_seller_ref is None
    backend.close.assert_not_awaited()
