"""Lifecycle coverage for the wallet-owned native market seller runtime."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.constants import MAX_MONEY
from jmcore.credential_market import (
    BondReference,
    MarketAuthorization,
    MarketError,
    SignedDocument,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_keys import BoundMarketKeys, MarketKeyScope
from jmcore.market_store import MarketStore, MarketStoreUnavailableError
from jmcore.models import NetworkType
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.settings import JoinMarketSettings, NetworkSettings, TorSettings
from jmwallet.wallet.service import WalletService

import taker.wallet_market as wallet_market
from taker.wallet_market import WalletMarketSeller, WalletMarketSellerOptions

MNEMONIC = "all " * 11 + "all"


class _Backend:
    def __init__(self) -> None:
        self.close = AsyncMock()


class _Transport:
    instances: list[_Transport] = []
    start_error: BaseException | None = None
    close_error: BaseException | None = None

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.nick = "J5ABCDEFGHJKLMNP"
        self.close_calls = 0
        _Transport.instances.append(self)

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _BlockingCallbacks:
    def __init__(self) -> None:
        self.listing_entered = asyncio.Event()
        self.respond_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def listing(self) -> bytes:
        self.listing_entered.set()
        await self.release.wait()
        return b"listing"

    async def respond(self, _sender: str, _body: dict[str, object]) -> dict[str, object]:
        self.respond_entered.set()
        await self.release.wait()
        return {"quote": "published-before-stop"}


def _settings(data_dir: Path, network: NetworkType = NetworkType.REGTEST) -> JoinMarketSettings:
    return JoinMarketSettings(
        data_dir=data_dir,
        network_config=NetworkSettings(network=network, directory_servers=[]),
        tor=TorSettings(connection_timeout=0.2),
    )


def _bond() -> BondReference:
    return BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid="11" * 32, vout=0),
        pubkey=bytes(CKey(b"\x02" * 32).pub).hex(),
        locktime=int(time.time()) + 86_400,
    )


def _bound_authorization(wallet: WalletService) -> tuple[BoundMarketKeys, SignedDocument]:
    bond = _bond()
    bound = wallet.market_keys.bind(
        MarketKeyScope(
            network="regtest",
            chain_hash="22" * 32,
            role="seller",
            period=0,
            bond=bond.outpoint,
        )
    )
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bound.signing_public_key().hex()),
        CKey(b"\x02" * 32),
    )
    return bound, authorization


def _options() -> WalletMarketSellerOptions:
    return WalletMarketSellerOptions(bond=_bond().outpoint, products=["bond"], price_sats=1_000)


def _activate_store(data_dir: Path, wallet: WalletService) -> None:
    commitments = get_used_commitments_path(data_dir)
    commitments.parent.mkdir(parents=True, exist_ok=True)
    commitments.write_text(json.dumps({"external_v1": {}, "used": []}), encoding="ascii")
    with MarketStore(get_market_store_path(data_dir), wallet_id=wallet.market_wallet_id) as store:
        store.activate_wallet(commitments, history_confirmed=True)


def _wallet(data_dir: Path) -> tuple[WalletService, _Backend]:
    backend = _Backend()
    return WalletService(MNEMONIC, backend, network="regtest", data_dir=data_dir), backend


@pytest.fixture(autouse=True)
def _reset_transport() -> None:
    _Transport.instances.clear()
    _Transport.start_error = None
    _Transport.close_error = None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["missing", "legacy", "not_ready"])
async def test_unready_ledger_never_authorizes_or_creates(tmp_path: Path, state: str) -> None:
    wallet, backend = _wallet(tmp_path)
    authorization = AsyncMock()
    wallet.create_market_authorization = authorization  # type: ignore[method-assign]
    path = get_market_store_path(tmp_path)
    if state == "legacy":
        MarketStore(path).close()
    elif state == "not_ready":
        _activate_store(tmp_path, wallet)
        with MarketStore(path, wallet_id=wallet.market_wallet_id) as store:
            store.mark_recovery_required()

    seller = WalletMarketSeller(wallet, _settings(tmp_path), _options())
    try:
        with pytest.raises(MarketStoreUnavailableError):
            await seller.start()
        authorization.assert_not_awaited()
        assert not _Transport.instances
        if state == "missing":
            assert not path.exists()
    finally:
        await seller.stop()
        await wallet.close()
    assert backend.close.await_count == 1


@pytest.mark.asyncio
async def test_start_stop_owns_only_market_resources_and_snapshots_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, backend = _wallet(tmp_path)
    _activate_store(tmp_path, wallet)
    bound, authorization = _bound_authorization(wallet)
    wallet.create_market_authorization = AsyncMock(return_value=(bound, authorization))  # type: ignore[method-assign]
    opened: list[MarketStore] = []
    real_store = wallet_market.MarketStore

    def open_store(*args: object, **kwargs: object) -> MarketStore:
        store = real_store(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(store)
        return store

    monkeypatch.setattr(wallet_market, "MarketStore", open_store)
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    options = _options()
    seller = WalletMarketSeller(wallet, _settings(tmp_path), options)
    options.products.append("podle")
    try:
        await seller.start()
        assert seller.running
        assert seller._service is not None
        assert seller._service.products == ["bond"]
        assert seller.status() == {
            "state": "running",
            "nickname": "J5ABCDEFGHJKLMNP",
            "seller_pubkey": bound.signing_public_key().hex(),
        }
        await seller.stop()
        assert not seller.running
        assert opened[0]._closed
        assert _Transport.instances[0].close_calls == 1
        assert wallet.market_keys.ledger_identity() == wallet.market_wallet_id
        backend.close.assert_not_awaited()
        with pytest.raises(RuntimeError, match="cannot restart"):
            await seller.start()
    finally:
        await wallet.close()


@pytest.mark.asyncio
async def test_start_failure_closes_owned_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, _ = _wallet(tmp_path)
    _activate_store(tmp_path, wallet)
    bound, authorization = _bound_authorization(wallet)
    wallet.create_market_authorization = AsyncMock(return_value=(bound, authorization))  # type: ignore[method-assign]
    opened: list[MarketStore] = []
    real_store = wallet_market.MarketStore

    def open_store(*args: object, **kwargs: object) -> MarketStore:
        store = real_store(*args, **kwargs)  # type: ignore[arg-type]
        opened.append(store)
        return store

    _Transport.start_error = RuntimeError("synthetic transport startup failure")
    monkeypatch.setattr(wallet_market, "MarketStore", open_store)
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    seller = WalletMarketSeller(wallet, _settings(tmp_path), _options())
    try:
        with pytest.raises(RuntimeError, match="startup failure"):
            await seller.start()
        assert opened[0]._closed
        assert _Transport.instances[0].close_calls == 1
    finally:
        await seller.stop()
        await wallet.close()


@pytest.mark.asyncio
async def test_stop_cancels_and_joins_pending_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, _ = _wallet(tmp_path)
    _activate_store(tmp_path, wallet)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_authorization(
        _outpoint: ExternalPoDLEOutpoint,
    ) -> tuple[BoundMarketKeys, SignedDocument]:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    wallet.create_market_authorization = pending_authorization  # type: ignore[method-assign]
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    seller = WalletMarketSeller(wallet, _settings(tmp_path), _options())
    start_task = asyncio.create_task(seller.start())
    try:
        await entered.wait()
        await seller.stop()
        assert cancelled.is_set()
        assert start_task.cancelled()
        assert not _Transport.instances
        assert seller._store is None
        assert not seller.running
    finally:
        if not start_task.done():
            start_task.cancel()
        await asyncio.gather(start_task, return_exceptions=True)
        await wallet.close()


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_retained_callbacks_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, _ = _wallet(tmp_path)
    _activate_store(tmp_path, wallet)
    bound, authorization = _bound_authorization(wallet)
    wallet.create_market_authorization = AsyncMock(return_value=(bound, authorization))  # type: ignore[method-assign]
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    seller = WalletMarketSeller(wallet, _settings(tmp_path), _options())
    try:
        await seller.start()
        callbacks = _Transport.instances[0].kwargs
        await asyncio.gather(seller.stop(), seller.stop())
        assert _Transport.instances[0].close_calls == 1
        listing = callbacks["listing_callback"]
        responder = callbacks["responder"]
        assert callable(listing)
        assert callable(responder)
        with pytest.raises(MarketError, match="unavailable"):
            await listing()  # type: ignore[misc]
        assert await responder("J5ABCDEFGHJKLMNP", {}) == {"error": "unavailable"}  # type: ignore[misc]
    finally:
        await wallet.close()


@pytest.mark.asyncio
async def test_stop_gates_callbacks_that_complete_after_await(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, _ = _wallet(tmp_path)
    _activate_store(tmp_path, wallet)
    bound, authorization = _bound_authorization(wallet)
    wallet.create_market_authorization = AsyncMock(return_value=(bound, authorization))  # type: ignore[method-assign]
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    seller = WalletMarketSeller(wallet, _settings(tmp_path), _options())
    blocker = _BlockingCallbacks()
    try:
        await seller.start()
        seller._service = blocker  # type: ignore[assignment]
        callbacks = _Transport.instances[0].kwargs
        listing = callbacks["listing_callback"]
        responder = callbacks["responder"]
        assert callable(listing)
        assert callable(responder)
        listing_task = asyncio.create_task(listing())  # type: ignore[misc]
        response_task = asyncio.create_task(responder("J5ABCDEFGHJKLMNP", {}))  # type: ignore[misc]
        await blocker.listing_entered.wait()
        await blocker.respond_entered.wait()
        await seller.stop()
        blocker.release.set()
        with pytest.raises(MarketError, match="unavailable"):
            await listing_task
        assert await response_task == {"error": "unavailable"}
    finally:
        blocker.release.set()
        await seller.stop()
        await wallet.close()


@pytest.mark.asyncio
async def test_stop_retries_a_failed_owned_transport_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet, _ = _wallet(tmp_path)
    _activate_store(tmp_path, wallet)
    bound, authorization = _bound_authorization(wallet)
    wallet.create_market_authorization = AsyncMock(return_value=(bound, authorization))  # type: ignore[method-assign]
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    seller = WalletMarketSeller(wallet, _settings(tmp_path), _options())
    try:
        await seller.start()
        _Transport.close_error = RuntimeError("synthetic close failure")
        with pytest.raises(RuntimeError, match="close failure"):
            await seller.stop()
        assert seller._transport is _Transport.instances[0]
        _Transport.close_error = None
        await seller.stop()
        assert seller._transport is None
        assert _Transport.instances[0].close_calls == 2
    finally:
        _Transport.close_error = None
        await seller.stop()
        await wallet.close()


def test_constructor_rejects_directory_or_network_mismatch(tmp_path: Path) -> None:
    wallet, _ = _wallet(tmp_path)
    try:
        with pytest.raises(ValueError, match="data directory"):
            WalletMarketSeller(wallet, _settings(tmp_path / "other"), _options())
        with pytest.raises(ValueError, match="same network"):
            WalletMarketSeller(wallet, _settings(tmp_path, NetworkType.SIGNET), _options())
    finally:
        asyncio.run(wallet.close())


def test_options_bound_products_prices_and_ttl() -> None:
    bond = _bond().outpoint
    with pytest.raises(ValueError, match="unique"):
        WalletMarketSellerOptions(bond=bond, products=["bond", "bond"], price_sats=1)
    with pytest.raises(ValueError):
        WalletMarketSellerOptions(bond=bond, products=[], price_sats=1)
    with pytest.raises(ValueError):
        WalletMarketSellerOptions(bond=bond, products=["bond"], price_sats=MAX_MONEY + 1)
    with pytest.raises(ValueError):
        WalletMarketSellerOptions(bond=bond, products=["bond"], price_sats=1, quote_ttl=901)
