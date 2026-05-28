"""Unit tests for scripts/check_signet_orderbook.py."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts import check_signet_orderbook as cso


class _StubSocket:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self) -> _StubSocket:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed = True


def test_socks_reachable_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cso.socket, "create_connection", lambda *_a, **_kw: _StubSocket()
    )
    assert cso._socks_reachable("127.0.0.1", 9050) is True


def test_socks_reachable_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: Any, **_kw: Any) -> None:
        raise OSError("refused")

    monkeypatch.setattr(cso.socket, "create_connection", _boom)
    assert cso._socks_reachable("127.0.0.1", 9050) is False


def test_parse_args_defaults() -> None:
    ns = cso._parse_args([])
    assert ns.socks_host == "127.0.0.1"
    assert ns.socks_port == 9050
    assert ns.min_offers == 1
    assert ns.directory is None


def test_parse_args_overrides() -> None:
    ns = cso._parse_args(
        [
            "--socks-host",
            "10.0.0.1",
            "--socks-port",
            "9150",
            "--min-offers",
            "3",
            "--directory",
            "a.onion:5222",
            "--directory",
            "b.onion:5222",
        ]
    )
    assert ns.socks_host == "10.0.0.1"
    assert ns.socks_port == 9150
    assert ns.min_offers == 3
    assert ns.directory == ["a.onion:5222", "b.onion:5222"]


def _fake_client(connected: int, offers: list[Any]) -> MagicMock:
    client = MagicMock()
    client.connect_all = AsyncMock(return_value=connected)
    client.fetch_orderbook = AsyncMock(return_value=offers)
    client.close_all = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_fetch_offers_no_connection() -> None:
    """When no directory connects, return 0 and skip fetch."""
    client = _fake_client(connected=0, offers=[])
    with patch.object(cso, "MultiDirectoryClient", return_value=client):
        count = await cso.fetch_offers(
            directories=["x.onion:5222"],
            socks_host="127.0.0.1",
            socks_port=9050,
            min_wait=0.1,
            max_wait=0.2,
            quiet_period=0.1,
        )
    assert count == 0
    client.fetch_orderbook.assert_not_called()
    client.close_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_offers_returns_count() -> None:
    offers = [object(), object(), object()]
    client = _fake_client(connected=2, offers=offers)
    with patch.object(cso, "MultiDirectoryClient", return_value=client):
        count = await cso.fetch_offers(
            directories=["a.onion:5222", "b.onion:5222"],
            socks_host="127.0.0.1",
            socks_port=9050,
            min_wait=0.1,
            max_wait=0.2,
            quiet_period=0.1,
        )
    assert count == 3
    client.close_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_offers_closes_on_exception() -> None:
    client = MagicMock()
    client.connect_all = AsyncMock(return_value=1)
    client.fetch_orderbook = AsyncMock(side_effect=RuntimeError("boom"))
    client.close_all = AsyncMock(return_value=None)
    with patch.object(cso, "MultiDirectoryClient", return_value=client):
        with pytest.raises(RuntimeError, match="boom"):
            await cso.fetch_offers(
                directories=["a.onion:5222"],
                socks_host="127.0.0.1",
                socks_port=9050,
                min_wait=0.1,
                max_wait=0.2,
                quiet_period=0.1,
            )
    client.close_all.assert_awaited_once()


def test_main_tor_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cso, "_socks_reachable", lambda *_a, **_kw: False)
    rc = cso.main(["--socks-port", "9050"])
    assert rc == 1
    assert "Tor SOCKS5 proxy not reachable" in capsys.readouterr().err


def test_main_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cso, "_socks_reachable", lambda *_a, **_kw: True)

    async def _fake(**_kw: Any) -> int:
        return 5

    monkeypatch.setattr(cso, "fetch_offers", _fake)
    rc = cso.main(["--directory", "a.onion:5222"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "5 offers" in out


def test_main_too_few_offers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cso, "_socks_reachable", lambda *_a, **_kw: True)

    async def _fake(**_kw: Any) -> int:
        return 0

    monkeypatch.setattr(cso, "fetch_offers", _fake)
    rc = cso.main(["--directory", "a.onion:5222", "--min-offers", "2"])
    assert rc == 1
    assert "only 0 offers" in capsys.readouterr().err


def test_main_no_directories(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cso, "_socks_reachable", lambda *_a, **_kw: True)
    monkeypatch.setattr(cso, "DIRECTORY_NODES_SIGNET", [])
    rc = cso.main([])
    assert rc == 2
    assert "no directory servers" in capsys.readouterr().err


def test_main_fetch_exception_returns_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cso, "_socks_reachable", lambda *_a, **_kw: True)

    async def _fake(**_kw: Any) -> int:
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(cso, "fetch_offers", _fake)
    rc = cso.main(["--directory", "a.onion:5222"])
    assert rc == 1
    assert "orderbook fetch failed" in capsys.readouterr().err
