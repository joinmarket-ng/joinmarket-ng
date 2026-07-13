"""E2E test for the jmwalletd transaction monitor (issue #560).

Verifies that an external deposit into a daemon-managed wallet produces a
``{"txid", "txdetails"}`` WebSocket notification without any client polling.

Requires: ``docker compose --profile e2e up -d``.
Run with: ``pytest tests/e2e/test_tx_monitor_e2e.py -m e2e``.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import uuid
from typing import Any

import httpx
import pytest
import websockets

pytestmark = pytest.mark.e2e

JMWALLETD_URL = os.environ.get("JMWALLETD_URL", "https://127.0.0.1:28183")
API = f"{JMWALLETD_URL}/api/v1"
WS_URL = JMWALLETD_URL.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
TLS_VERIFY = False
_RPC_TIMEOUT = 60.0


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _rpc(
    cfg: dict[str, str], method: str, params: list[Any] | None = None, wallet=None
):
    url = cfg["rpc_url"].rstrip("/")
    if wallet:
        url = f"{url}/wallet/{wallet}"
    payload = {
        "jsonrpc": "1.0",
        "id": "jmng-txmon",
        "method": method,
        "params": params or [],
    }
    async with httpx.AsyncClient(timeout=_RPC_TIMEOUT) as client:
        r = await client.post(
            url, auth=(cfg["rpc_user"], cfg["rpc_password"]), json=payload
        )
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"{method} RPC error: {data['error']}")
    return data.get("result")


async def _wait_for_jmwalletd(timeout: float = 60.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(verify=TLS_VERIFY) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await client.get(f"{API}/getinfo", timeout=5)
                if r.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(1.0)
    pytest.fail(f"jmwalletd did not become ready within {timeout}s")


@pytest.mark.asyncio
async def test_external_deposit_produces_ws_notification(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
) -> None:
    cfg = bitcoin_rpc_config
    await _wait_for_jmwalletd()
    name = f"txmon-{uuid.uuid4().hex[:8]}.jmdat"

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    async with httpx.AsyncClient(verify=TLS_VERIFY, timeout=30.0) as client:
        # Clean slate, then create a wallet (this starts the tx monitor).
        r = await client.post(
            f"{API}/wallet/create",
            json={"walletname": name, "password": "testpass", "wallettype": "sw-fb"},
        )
        assert r.status_code == 201, f"create failed: {r.status_code} {r.text}"
        token = r.json()["token"]

        try:
            # Trigger a sync so the descriptor wallet is imported in Core and
            # the monitor can enumerate/baseline.
            await client.get(f"{API}/wallet/{name}/display", headers=_auth(token))
            # A fresh deposit address to fund.
            r = await client.get(
                f"{API}/wallet/{name}/address/new/0", headers=_auth(token)
            )
            assert r.status_code == 200, f"address/new failed: {r.status_code} {r.text}"
            deposit = r.json()["address"]

            async with websockets.connect(WS_URL, ssl=ssl_ctx) as ws:
                await ws.send(token)  # authenticate

                funding_txid = await _rpc(
                    cfg, "sendtoaddress", [deposit, 0.05], wallet="test-funder"
                )
                # Mine to an unrelated address so only the funding tx involves us.
                await _rpc(
                    cfg,
                    "generatetoaddress",
                    [1, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"],
                )

                # Collect frames until the notification for our funding tx arrives.
                deadline = asyncio.get_event_loop().time() + 40.0
                seen_txids: list[str] = []
                found = False
                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except (TimeoutError, asyncio.TimeoutError):
                        break
                    try:
                        frame = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if "txid" in frame and "txdetails" in frame:
                        seen_txids.append(frame["txid"])
                        if frame["txid"] == funding_txid:
                            found = True
                            break
                assert found, (
                    f"no websocket notification for funding tx {funding_txid}; "
                    f"received tx frames: {seen_txids}"
                )
        finally:
            await client.get(f"{API}/wallet/{name}/lock", headers=_auth(token))
