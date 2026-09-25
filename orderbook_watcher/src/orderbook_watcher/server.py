"""
HTTP server for serving static files and orderbook data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.typedefs import Handler
from jmcore.fee_quantization import QUANT_ABS, QUANT_REL
from jmcore.models import Offer, OrderBook
from jmcore.settings import OrderbookWatcherSettings
from loguru import logger

from orderbook_watcher.aggregator import OrderbookAggregator

_STATIC_DIR = Path(__file__).parent / "static"
_REQUIRED_STATIC_ASSETS = ("index.html", "app.js", "style.css", "favicon.ico")


@web.middleware
async def _no_store_ui_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Disable browser caching for the UI and its static assets.

    Release images normalize file mtimes for reproducible builds, so
    ``Last-Modified``/``ETag`` validators never change across releases and a
    browser that cached ``app.js`` once would keep revalidating into ``304``s
    forever, serving a stale frontend. The assets are small and the page polls
    the API anyway, so ``no-store`` is the simple, correct choice. Per RFC
    9111 the header also updates already-stored responses on ``304``.
    """
    response: web.StreamResponse = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


class OrderbookServer:
    def __init__(self, settings: OrderbookWatcherSettings, aggregator: OrderbookAggregator) -> None:
        self.settings = settings
        self.aggregator = aggregator
        self.app = web.Application(middlewares=[_no_store_ui_middleware])
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._update_task: asyncio.Task[Any] | None = None
        self._cached_orderbook: str | None = None
        self._cached_credential_expiry: int | None = None
        self._cache_lock = asyncio.Lock()
        self._background_update_task: asyncio.Task[Any] | None = None
        self._stopping = False
        self._setup_routes()

    def _setup_routes(self) -> None:
        missing_assets = [
            asset for asset in _REQUIRED_STATIC_ASSETS if not (_STATIC_DIR / asset).is_file()
        ]
        if missing_assets:
            missing = ", ".join(missing_assets)
            raise RuntimeError(
                f"Orderbook Watcher frontend assets are missing ({missing}). "
                "Reinstall joinmarket-orderbook-watcher."
            )

        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/orderbook.json", self._handle_orderbook_json)
        self.app.router.add_get("/health", self._handle_health)

        self.app.router.add_static("/static/", path=_STATIC_DIR, name="static")

    async def _handle_index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    async def _handle_orderbook_json(self, _request: web.Request) -> web.Response:
        async with self._cache_lock:
            if self._cached_orderbook:
                now = int(time.time())
                if (
                    self._cached_credential_expiry is not None
                    and self._cached_credential_expiry <= now
                ):
                    self._install_cached_orderbook(json.loads(self._cached_orderbook), now=now)
                return web.Response(text=self._cached_orderbook, content_type="application/json")

        orderbook = await self.aggregator.get_live_orderbook()
        if orderbook is None:
            return web.json_response({"error": "Orderbook not available"}, status=503)

        data = self._format_orderbook(orderbook)

        async with self._cache_lock:
            json_str = self._install_cached_orderbook(data, now=int(time.time()))

        return web.Response(text=json_str, content_type="application/json")

    @staticmethod
    def _credential_offer_expiry(offer: object) -> int | None:
        if not isinstance(offer, dict):
            return None
        listing = offer.get("listing")
        if not isinstance(listing, dict):
            return None
        body = listing.get("body")
        if not isinstance(body, dict):
            return None
        expires_at = body.get("expires_at")
        if isinstance(expires_at, int) and not isinstance(expires_at, bool):
            return expires_at
        return None

    def _install_cached_orderbook(self, data: dict[str, Any], *, now: int) -> str:
        """Install a serialized response after removing expired credential advertisements.

        The caller must hold ``_cache_lock`` so the serialized response and expiry deadline
        always describe the same cache entry.
        """
        earliest_expiry: int | None = None
        credential_market = data.get("credential_market")
        if isinstance(credential_market, dict):
            for product in ("podle_offers", "bond_offers"):
                offers = credential_market.get(product)
                if not isinstance(offers, list):
                    continue

                active_offers: list[dict[str, Any]] = []
                for offer in offers:
                    expires_at = self._credential_offer_expiry(offer)
                    if not isinstance(offer, dict) or expires_at is None or expires_at <= now:
                        continue
                    active_offers.append(offer)
                    if earliest_expiry is None or expires_at < earliest_expiry:
                        earliest_expiry = expires_at
                credential_market[product] = active_offers

        self._cached_orderbook = json.dumps(data)
        self._cached_credential_expiry = earliest_expiry
        return self._cached_orderbook

    def _format_orderbook(self, orderbook: OrderBook) -> dict[str, Any]:
        market_offers = self.aggregator.get_market_offers()
        offers_by_directory = orderbook.get_offers_by_directory()
        directory_stats: dict[str, dict[str, Any]] = {}
        for node, offers in offers_by_directory.items():
            # Count unique bonds per directory (deduplicate by UTXO)
            unique_bond_utxos: set[str] = set()
            for o in offers:
                if o.fidelity_bond_data and self._has_active_bond(
                    o, orderbook.current_block_height
                ):
                    utxo_key = (
                        f"{o.fidelity_bond_data['utxo_txid']}:{o.fidelity_bond_data['utxo_vout']}"
                    )
                    unique_bond_utxos.add(utxo_key)
            directory_stats[node] = {
                "offer_count": len(offers),
                "bond_offer_count": len(unique_bond_utxos),
            }

        for node_tuple in self.aggregator.directory_nodes:
            node_str = f"{node_tuple[0]}:{node_tuple[1]}"
            if node_str not in directory_stats:
                directory_stats[node_str] = {"offer_count": 0, "bond_offer_count": 0}

        # Add connection status and directory metadata
        for status_node_id, status in self.aggregator.node_statuses.items():
            if status_node_id in directory_stats:
                directory_stats[status_node_id].update(status.to_dict(orderbook.timestamp))

        # Add directory metadata (MOTD, version, features)
        for node_str, client in self.aggregator.clients.items():
            if node_str in directory_stats:
                directory_stats[node_str].update(
                    {
                        "motd": client.directory_motd,
                        "nick": client.directory_nick,
                        "proto_ver_min": client.directory_proto_ver_min,
                        "proto_ver_max": client.directory_proto_ver_max,
                        "features": client.directory_features,
                    }
                )

        grouped_offers: dict[tuple[str, int], dict[str, Any]] = {}
        for offer in orderbook.offers:
            key = (offer.counterparty, offer.oid)
            if key not in grouped_offers:
                # Use directory_nodes (plural) which is already populated by the aggregator
                grouped_offers[key] = {
                    "counterparty": offer.counterparty,
                    "oid": offer.oid,
                    "ordertype": offer.ordertype.value,
                    "minsize": offer.minsize,
                    "maxsize": offer.maxsize,
                    "txfee": offer.txfee,
                    "cjfee": offer.cjfee,
                    "fidelity_bond_value": offer.fidelity_bond_value,
                    "fidelity_bond_verified": offer.fidelity_bond_verified,
                    "fidelity_bond_verification_stale": (offer.fidelity_bond_verification_stale),
                    "directory_nodes": offer.directory_nodes.copy(),
                    "fidelity_bond_data": offer.fidelity_bond_data,
                    "features": offer.features.copy(),
                    "directly_reachable": offer.directly_reachable,
                }
            # Offers are already deduplicated by the aggregator with directory_nodes populated
            # This branch should not be reached, but handle it gracefully just in case

        # Calculate feature statistics over bonded makers only.
        #
        # Bondless makers are sybil-cheap: a single operator can announce an
        # unbounded number of them and skew "% of makers supporting feature X"
        # arbitrarily. Restricting both numerator and denominator to makers
        # that advertise a fidelity bond yields a sybil-resistant share that
        # reflects committed capital, not raw nick count. See issue #483.
        #
        # An active advertised certificate counts even when the UTXO value has
        # not been computed. Expired or height-unverified proofs do not provide
        # sybil-resistant weight.
        feature_stats: dict[str, int] = {}
        bonded_makers: set[str] = set()
        offers_by_maker: dict[str, list[dict[str, Any]]] = {}
        for offer_data in grouped_offers.values():
            offers_by_maker.setdefault(offer_data["counterparty"], []).append(offer_data)

        for counterparty, maker_offers in offers_by_maker.items():
            active_offers: list[dict[str, Any]] = []
            for offer_data in maker_offers:
                if self._has_active_bond_data(offer_data, orderbook.current_block_height):
                    active_offers.append(offer_data)

            if not active_offers:
                continue
            bonded_makers.add(counterparty)
            features: dict[str, bool] = {}
            for offer_data in active_offers:
                for feature, value in offer_data.get("features", {}).items():
                    if value:
                        features[feature] = True
            for feature, value in features.items():
                if value:
                    feature_stats[feature] = feature_stats.get(feature, 0) + 1
            # Track bonded makers without any features (legacy/reference
            # implementation makers that don't announce a feature map).
            if not features:
                feature_stats["legacy"] = feature_stats.get("legacy", 0) + 1

        return {
            "timestamp": orderbook.timestamp.isoformat(),
            "current_block_height": orderbook.current_block_height,
            "offers": list(grouped_offers.values()),
            "credential_market": {
                "podle_offers": list(market_offers["podle_offers"]),
                "bond_offers": list(market_offers["bond_offers"]),
            },
            "fidelitybonds": [
                {
                    "counterparty": bond.counterparty,
                    "utxo": {"txid": bond.utxo_txid, "vout": bond.utxo_vout},
                    "bond_value": bond.bond_value,
                    "locktime": bond.locktime,
                    "amount": bond.amount,
                    "script": bond.script,
                    "utxo_confirmations": bond.utxo_confirmations,
                    "utxo_confirmation_timestamp": bond.utxo_confirmation_timestamp,
                    "cert_expiry": bond.cert_expiry,
                    "verification_valid": bond.verification_valid,
                    "verification_stale": bond.verification_stale,
                    "utxo_pub": (bond.fidelity_bond_data or {}).get("utxo_pub") or bond.script,
                    "directory_node": bond.directory_node,
                }
                for bond in orderbook.fidelity_bonds
            ],
            "directory_nodes": orderbook.directory_nodes,
            "directory_stats": directory_stats,
            "feature_stats": feature_stats,
            "feature_stats_denominator": len(bonded_makers),
            "fee_quantization": {
                # Public fee-homogenization grid (issue #508). The taker rounds
                # its fee limits down onto these values; the chart buckets offers
                # the same way so makers can see which quantum band they land in.
                "rel_grid": [str(q) for q in QUANT_REL],
                "abs_grid": list(QUANT_ABS),
            },
            "mempool_url": self.settings.mempool_web_url
            or (
                self.settings.mempool_api_url.replace("/api", "")
                if self.settings.mempool_api_url
                else None
            ),
        }

    @staticmethod
    def _has_active_bond(offer: Offer, current_block_height: int | None) -> bool:
        return OrderbookServer._has_active_bond_data(
            {
                "fidelity_bond_data": offer.fidelity_bond_data,
                "fidelity_bond_value": offer.fidelity_bond_value,
                "fidelity_bond_verified": offer.fidelity_bond_verified,
                "fidelity_bond_verification_stale": (offer.fidelity_bond_verification_stale),
            },
            current_block_height,
        )

    @staticmethod
    def _has_active_bond_data(offer_data: dict[str, Any], current_block_height: int | None) -> bool:
        bond_data = offer_data.get("fidelity_bond_data")
        if bond_data is None:
            return offer_data.get("fidelity_bond_value", 0) > 0
        cert_expiry = bond_data.get("cert_expiry")
        return (
            offer_data.get("fidelity_bond_verified") is not False
            and offer_data.get("fidelity_bond_verification_stale") is not True
            and current_block_height is not None
            and isinstance(cert_expiry, int)
            and current_block_height <= cert_expiry
        )

    async def _handle_health(self, _request: web.Request) -> web.Response:
        orderbook = await self.aggregator.get_orderbook()
        return web.json_response(
            {
                "status": "healthy",
                "offers": len(orderbook.offers),
                "fidelity_bonds": len(orderbook.fidelity_bonds),
                "directory_nodes": len(orderbook.directory_nodes),
                "last_update": orderbook.timestamp.isoformat(),
            }
        )

    async def _update_cache_loop(self) -> None:
        await asyncio.sleep(2)

        while True:
            try:
                orderbook = await self.aggregator.get_live_orderbook()
                data = self._format_orderbook(orderbook)

                async with self._cache_lock:
                    previous_json = self._cached_orderbook
                    json_str = self._install_cached_orderbook(data, now=int(time.time()))
                    if json_str != previous_json:
                        logger.debug(f"Cache updated: {len(orderbook.offers)} offers")

            except Exception as e:
                logger.error(f"Error updating cache: {e}")

            await asyncio.sleep(30)

    async def start(self) -> None:
        logger.info(
            f"Starting orderbook server on {self.settings.http_host}:{self.settings.http_port}"
        )

        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        self.site = web.TCPSite(self.runner, self.settings.http_host, self.settings.http_port)
        await self.site.start()

        logger.info("Starting continuous directory listeners...")
        await self.aggregator.start_continuous_listening()

        self._background_update_task = asyncio.create_task(self._update_cache_loop())

        logger.info(
            f"Orderbook server running at http://{self.settings.http_host}:{self.settings.http_port}"
        )

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True

        logger.info("Stopping orderbook server...")

        if self._background_update_task:
            self._background_update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._background_update_task
            self._background_update_task = None

        logger.info("Stopping directory listeners...")
        await self.aggregator.stop_listening()

        if self.site:
            with contextlib.suppress(RuntimeError):
                await self.site.stop()
            self.site = None

        if self.runner:
            with contextlib.suppress(RuntimeError):
                await self.runner.cleanup()
            self.runner = None

        logger.info("Orderbook server stopped")
