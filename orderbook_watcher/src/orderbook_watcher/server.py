"""
HTTP server for serving static files and orderbook data.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

from aiohttp import web
from jmcore.fee_quantization import QUANT_ABS, QUANT_REL
from jmcore.models import OrderBook
from jmcore.settings import OrderbookWatcherSettings
from loguru import logger

from orderbook_watcher.aggregator import OrderbookAggregator


class OrderbookServer:
    def __init__(self, settings: OrderbookWatcherSettings, aggregator: OrderbookAggregator) -> None:
        self.settings = settings
        self.aggregator = aggregator
        self.app = web.Application()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._update_task: asyncio.Task[Any] | None = None
        self._cached_orderbook: str | None = None
        self._cache_lock = asyncio.Lock()
        self._background_update_task: asyncio.Task[Any] | None = None
        self._stopping = False
        self._setup_routes()

    def _setup_routes(self) -> None:
        self.app.router.add_get("/", self._handle_index)
        self.app.router.add_get("/orderbook.json", self._handle_orderbook_json)
        self.app.router.add_get("/health", self._handle_health)

        static_dir = Path(__file__).parent.parent.parent / "static"
        if static_dir.exists():
            self.app.router.add_static("/static/", path=static_dir, name="static")

    async def _handle_index(self, _request: web.Request) -> web.Response | web.FileResponse:
        static_dir = Path(__file__).parent.parent.parent / "static"
        index_file = static_dir / "index.html"
        if index_file.exists():
            return web.FileResponse(index_file)
        return web.Response(text="Orderbook Watcher", status=200)

    async def _handle_orderbook_json(self, _request: web.Request) -> web.Response:
        async with self._cache_lock:
            if self._cached_orderbook:
                return web.Response(text=self._cached_orderbook, content_type="application/json")

        orderbook = await self.aggregator.get_live_orderbook()
        if orderbook is None:
            return web.json_response({"error": "Orderbook not available"}, status=503)

        data = self._format_orderbook(orderbook)
        json_str = json.dumps(data)

        async with self._cache_lock:
            self._cached_orderbook = json_str

        return web.Response(text=json_str, content_type="application/json")

    def _format_orderbook(self, orderbook: OrderBook) -> dict[str, Any]:
        offers_by_directory = orderbook.get_offers_by_directory()
        directory_stats: dict[str, dict[str, Any]] = {}
        for node, offers in offers_by_directory.items():
            # Count unique bonds per directory (deduplicate by UTXO)
            unique_bond_utxos: set[str] = set()
            for o in offers:
                if o.fidelity_bond_data:
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
        # A maker counts as bonded when it advertises a bond
        # (``fidelity_bond_data``) even if the bond *value* cannot be computed
        # (no blockchain/mempool backend configured): the advertised bond is
        # still sybil-resistant, and the value only gates value-weighted views.
        feature_stats: dict[str, int] = {}
        unique_makers: set[str] = set()
        bonded_makers: set[str] = set()
        for offer_data in grouped_offers.values():
            counterparty = offer_data["counterparty"]
            if counterparty in unique_makers:
                continue
            unique_makers.add(counterparty)
            has_bond = (
                offer_data.get("fidelity_bond_value", 0) > 0
                or offer_data.get("fidelity_bond_data") is not None
            )
            if not has_bond:
                continue
            bonded_makers.add(counterparty)
            features = offer_data.get("features", {})
            for feature, value in features.items():
                if value:
                    feature_stats[feature] = feature_stats.get(feature, 0) + 1
            # Track bonded makers without any features (legacy/reference
            # implementation makers that don't announce a feature map).
            if not features:
                feature_stats["legacy"] = feature_stats.get("legacy", 0) + 1

        return {
            "timestamp": orderbook.timestamp.isoformat(),
            "offers": list(grouped_offers.values()),
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
        last_hash = 0

        while True:
            try:
                orderbook = await self.aggregator.get_live_orderbook()

                current_hash = hash(
                    (
                        tuple((o.counterparty, o.oid, o.directory_node) for o in orderbook.offers),
                        tuple((b.utxo_txid, b.utxo_vout) for b in orderbook.fidelity_bonds),
                    )
                )

                if current_hash != last_hash:
                    data = self._format_orderbook(orderbook)
                    json_str = json.dumps(data)

                    async with self._cache_lock:
                        self._cached_orderbook = json_str

                    logger.debug(f"Cache updated: {len(orderbook.offers)} offers")
                    last_hash = current_hash

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
