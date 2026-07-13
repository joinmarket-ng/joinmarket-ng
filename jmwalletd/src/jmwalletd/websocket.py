"""WebSocket endpoint for push notifications.

Implements the reference JoinMarket WebSocket protocol:
1. Client connects.
2. Client sends its JWT access token as a plain text message.
3. Server verifies the token.
4. On success, the client starts receiving JSON notifications.
5. Any non-token message or invalid token drops the connection.

Notification types:
- ``{"coinjoin_state": <int>}`` -- coinjoin state change.
- ``{"txid": "...", "txdetails": {...}}`` -- a wallet transaction. Emitted for
  every transaction that affects the wallet (external deposits, maker/taker
  coinjoins, and sends), once when first seen in the mempool and again when it
  first confirms (``txdetails.confirmations`` carries the count). Clients should
  treat it as a signal to reload balances/UTXOs.
"""

from __future__ import annotations

import asyncio

import jwt as pyjwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

from jmwalletd.deps import get_daemon_state

router = APIRouter()

# Mounted at /ws, /jmws, and /api/v1/ws in app.py.
_WS_PATH = ""


@router.websocket(_WS_PATH)
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Handle a WebSocket connection with token-based authentication."""
    await websocket.accept()

    state = get_daemon_state()
    queue: asyncio.Queue[str] | None = None

    try:
        # Wait for the auth token (first message).
        token_msg = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)

        # Verify the token.
        try:
            state.token_authority.verify_access(token_msg.strip())
        except pyjwt.InvalidTokenError as exc:
            logger.debug("WebSocket auth failed: {}", exc)
            await websocket.close(code=4001, reason="Invalid token")
            return

        # Register for notifications.
        queue = state.register_ws_client()

        # Run two tasks concurrently:
        # 1. Read incoming messages (heartbeat tokens or close).
        # 2. Send outgoing notifications from the queue.
        async def _reader() -> None:
            """Read incoming messages. Heartbeat tokens are re-verified."""
            while True:
                try:
                    msg = await websocket.receive_text()
                    # Treat any incoming message as a heartbeat token re-auth.
                    try:
                        state.token_authority.verify_access(msg.strip())
                    except pyjwt.InvalidTokenError:
                        logger.debug("WebSocket heartbeat token invalid, dropping")
                        await websocket.close(code=4001, reason="Invalid token")
                        return
                except WebSocketDisconnect:
                    return

        async def _writer() -> None:
            """Send queued notifications to the client."""
            while True:
                msg = await queue.get()  # type: ignore[union-attr]
                try:
                    await websocket.send_text(msg)
                except Exception:
                    return

        # Run reader and writer concurrently; when either exits, we're done.
        reader_task = asyncio.create_task(_reader())
        writer_task = asyncio.create_task(_writer())

        done, pending = await asyncio.wait(
            [reader_task, writer_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()

    except TimeoutError:
        logger.debug("WebSocket auth timeout")
        await websocket.close(code=4002, reason="Auth timeout")
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error")
    finally:
        if queue is not None:
            state.unregister_ws_client(queue)
