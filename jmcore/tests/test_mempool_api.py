"""Tests for MempoolAPI transport selection and proxy safety."""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import MagicMock, patch

import pytest

from jmcore.mempool_api import MempoolAPI, MempoolAPIError


def test_direct_client_ignores_proxy_environment() -> None:
    """Explicit direct clients must not inherit process proxy variables."""
    with patch("jmcore.mempool_api.httpx.AsyncClient") as client_cls:
        MempoolAPI("http://127.0.0.1:8999", trust_env=False)

    assert client_cls.call_args.kwargs["trust_env"] is False
    assert "transport" not in client_cls.call_args.kwargs


def test_tor_client_uses_socks_transport_and_ignores_proxy_environment() -> None:
    """SOCKS mode keeps remote DNS and has an explicit transport."""
    transport = MagicMock()
    with (
        patch("jmcore.mempool_api.httpx.AsyncClient") as client_cls,
        patch("httpx_socks.AsyncProxyTransport.from_url", return_value=transport) as from_url,
    ):
        MempoolAPI(
            "https://mempool.example/api",
            socks_proxy="socks5h://127.0.0.1:9050",
            trust_env=False,
        )

    from_url.assert_called_once_with("socks5://127.0.0.1:9050", rdns=True)
    assert client_cls.call_args.kwargs["transport"] is transport
    assert client_cls.call_args.kwargs["trust_env"] is False


def test_tor_transport_setup_fails_closed() -> None:
    """A requested Tor route must never silently construct a direct client."""
    with (
        patch("jmcore.mempool_api.httpx.AsyncClient") as client_cls,
        patch.dict(sys.modules, {"httpx_socks": None}),
        pytest.raises(MempoolAPIError, match="httpx-socks"),
    ):
        MempoolAPI("https://mempool.example/api", socks_proxy="socks5h://127.0.0.1:9050")

    client_cls.assert_not_called()


def test_tor_transport_construction_failure_prevents_direct_client() -> None:
    """Transport factory errors fail before an HTTP client can issue requests."""
    with (
        patch("jmcore.mempool_api.httpx.AsyncClient") as client_cls,
        patch(
            "httpx_socks.AsyncProxyTransport.from_url",
            side_effect=RuntimeError("proxy unavailable"),
        ),
        pytest.raises(MempoolAPIError, match="Tor transport"),
    ):
        MempoolAPI("https://mempool.example/api", socks_proxy="socks5h://127.0.0.1:9050")

    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_direct_client_reaches_loopback_without_inherited_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct mode reaches a local mempool-compatible endpoint without a proxy."""

    received_requests: list[bytes] = []

    async def handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        received_requests.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\n123")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle_request, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    try:
        async with MempoolAPI(f"http://127.0.0.1:{port}", trust_env=False) as api:
            assert await api.test_connection() is True
    finally:
        server.close()
        await server.wait_closed()

    assert received_requests
