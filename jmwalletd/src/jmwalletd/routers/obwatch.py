"""Orderbook proxy endpoints.

Proxies JAM's ``/obwatch/`` requests to the orderbook_watcher HTTP server.
The orderbook_watcher runs independently on its own port (default 8000) and
provides the live orderbook data from directory servers.

Set ``OBWATCH_URL`` to override the connect-to URL (e.g. in Docker where the
watcher runs in a separate container).
"""

from __future__ import annotations

import os

import aiohttp
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from loguru import logger

from jmwalletd.deps import get_daemon_state
from jmwalletd.state import DaemonState

router = APIRouter()

_EMPTY_ORDERBOOK: dict[str, list[object]] = {"offers": [], "fidelitybonds": []}


def _get_obwatch_url(state: DaemonState) -> str:
    """Get the orderbook watcher base URL.

    Resolution order:
    1. ``OBWATCH_URL`` environment variable (e.g. ``http://jm-orderbook-watcher:8000``)
    2. Settings ``orderbook_watcher.http_host`` / ``http_port`` with
       ``127.0.0.1`` substituted for ``0.0.0.0`` (a bind address is not a
       valid client destination).
    3. Hard-coded fallback ``http://127.0.0.1:8000``.
    """
    env_url = os.environ.get("OBWATCH_URL")
    if env_url:
        return env_url.rstrip("/")

    try:
        from jmcore.settings import get_settings

        settings = get_settings()
        host = settings.orderbook_watcher.http_host
        port = settings.orderbook_watcher.http_port
        # 0.0.0.0 is a bind address, not reachable as a client destination.
        if host == "0.0.0.0":  # noqa: S104
            host = "127.0.0.1"
        return f"http://{host}:{port}"
    except Exception:
        return "http://127.0.0.1:8000"


@router.get("/obwatch/orderbook.json")
async def get_orderbook(
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Proxy orderbook data from the orderbook_watcher service."""
    url = f"{_get_obwatch_url(state)}/orderbook.json"
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp,
        ):
            if resp.status == 200:
                data = await resp.json()
                return JSONResponse(content=data)
            logger.warning("Orderbook watcher returned status {}", resp.status)
            return JSONResponse(
                content={"offers": [], "fidelitybonds": []},
                status_code=502,
            )
    except Exception:
        logger.warning("Could not reach orderbook watcher at {}", url)
        return JSONResponse(
            content={"offers": [], "fidelitybonds": []},
            status_code=502,
        )


@router.api_route("/obwatch/refreshorderbook", methods=["GET", "POST"])
async def refresh_orderbook(
    state: DaemonState = Depends(get_daemon_state),
) -> JSONResponse:
    """Request the orderbook watcher to refresh its cache.

    Accepts both GET and POST to match the JAM reference implementation which
    calls POST /obwatch/refreshorderbook.  Our orderbook_watcher auto-refreshes
    every 30 s, so this is a no-op proxy that just returns the latest data.
    """
    url = f"{_get_obwatch_url(state)}/orderbook.json"
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp,
        ):
            if resp.status == 200:
                data = await resp.json()
                return JSONResponse(content=data)
            return JSONResponse(content={"offers": [], "fidelitybonds": []})
    except Exception:
        return JSONResponse(content={"offers": [], "fidelitybonds": []})
