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
    """When no directory connects, return result with connected=0."""
    client = _fake_client(connected=0, offers=[])
    with patch.object(cso, "MultiDirectoryClient", return_value=client):
        result = await cso.fetch_offers(
            directories=["x.onion:5222"],
            socks_host="127.0.0.1",
            socks_port=9050,
            min_wait=0.1,
            max_wait=0.2,
            quiet_period=0.1,
        )
    assert result.connected == 0
    assert result.offer_count == 0
    assert result.total_directories == 1
    client.fetch_orderbook.assert_not_called()
    client.close_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_offers_returns_count() -> None:
    offers = [object(), object(), object()]
    client = _fake_client(connected=2, offers=offers)
    with patch.object(cso, "MultiDirectoryClient", return_value=client):
        result = await cso.fetch_offers(
            directories=["a.onion:5222", "b.onion:5222"],
            socks_host="127.0.0.1",
            socks_port=9050,
            min_wait=0.1,
            max_wait=0.2,
            quiet_period=0.1,
        )
    assert result.connected == 2
    assert result.offer_count == 3
    assert result.total_directories == 2
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


def _fake_result(connected: int, total: int, offers: int) -> cso.OrderbookResult:
    return cso.OrderbookResult(
        connected=connected, total_directories=total, offer_count=offers
    )


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

    async def _fake(**_kw: Any) -> cso.OrderbookResult:
        return _fake_result(connected=1, total=1, offers=5)

    monkeypatch.setattr(cso, "fetch_offers", _fake)
    rc = cso.main(["--directory", "a.onion:5222"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "5 offers" in out


def test_main_connected_but_no_offers_is_warning_not_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Connected to directory but 0 offers should exit 0 with WARNING."""
    monkeypatch.setattr(cso, "_socks_reachable", lambda *_a, **_kw: True)

    async def _fake(**_kw: Any) -> cso.OrderbookResult:
        return _fake_result(connected=1, total=1, offers=0)

    monkeypatch.setattr(cso, "fetch_offers", _fake)
    rc = cso.main(["--directory", "a.onion:5222", "--min-offers", "2"])
    assert rc == 0, "No offers when connected should be a warning, not a failure"
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "0 offers" in out


def test_main_no_directories_connected_is_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No directories connected should be a WARNING (exit 0), not a hard fail.

    Signet directory nodes are volunteer-operated and may be temporarily
    offline. We cannot distinguish that from a broken install, so we
    treat it the same as 'connected but no offers'.
    """
    monkeypatch.setattr(cso, "_socks_reachable", lambda *_a, **_kw: True)
    monkeypatch.setattr(cso, "time", MagicMock(sleep=MagicMock()))

    async def _fake(**_kw: Any) -> cso.OrderbookResult:
        return _fake_result(connected=0, total=2, offers=0)

    monkeypatch.setattr(cso, "fetch_offers", _fake)
    rc = cso.main(
        ["--directory", "a.onion:5222", "--directory", "b.onion:5222", "--retries", "0"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "could not connect" in out


def test_main_retries_on_no_connection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When connected=0, the script retries up to --retries times."""
    monkeypatch.setattr(cso, "_socks_reachable", lambda *_a, **_kw: True)

    sleep_mock = MagicMock()
    monkeypatch.setattr(cso.time, "sleep", sleep_mock)

    call_count = 0

    async def _fake(**_kw: Any) -> cso.OrderbookResult:
        nonlocal call_count
        call_count += 1
        # Succeed on the second attempt.
        if call_count >= 2:
            return _fake_result(connected=1, total=1, offers=3)
        return _fake_result(connected=0, total=1, offers=0)

    monkeypatch.setattr(cso, "fetch_offers", _fake)
    rc = cso.main(
        ["--directory", "a.onion:5222", "--retries", "2", "--retry-delay", "0"]
    )
    assert rc == 0
    assert call_count == 2, "Should have retried once and succeeded"
    sleep_mock.assert_called_once_with(0.0)
    out = capsys.readouterr().out
    assert "OK" in out
    assert "3 offers" in out


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

    async def _fake(**_kw: Any) -> cso.OrderbookResult:
        raise ConnectionRefusedError("nope")

    monkeypatch.setattr(cso, "fetch_offers", _fake)
    rc = cso.main(["--directory", "a.onion:5222"])
    assert rc == 1
    assert "orderbook fetch failed" in capsys.readouterr().err
