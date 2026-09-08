"""Maker request-path regressions for multi-directory admission and recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.models import NetworkType, Offer, OfferType
from jmcore.protocol import MessageType

from maker.bot import MakerBot
from maker.config import MakerConfig


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    clock = [10_000.0]
    monkeypatch.setattr("maker.rate_limiting.time.monotonic", lambda: clock[0])
    return clock


@pytest.fixture
def bot(tmp_path: Path, now: list[float]) -> MakerBot:
    wallet = MagicMock(mixdepth_count=5, utxo_cache={})
    backend = MagicMock()
    backend.can_provide_neutrino_metadata.return_value = False
    config = MakerConfig(
        mnemonic="test " * 12,
        directory_servers=[f"localhost:{5222 + index}" for index in range(7)],
        network=NetworkType.REGTEST,
        data_dir=tmp_path,
    )
    bot = MakerBot(wallet=wallet, backend=backend, config=config)
    bot.current_offers = [
        Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=100_000,
            txfee=0,
            cjfee="0.0001",
        )
    ]
    for directory in config.directory_servers:
        bot.directory_clients[directory] = MagicMock(send_private_message=AsyncMock())
    return bot


async def request(bot: MakerBot, nick: str, directory: str) -> None:
    await bot._handle_message(
        {"type": MessageType.PUBMSG.value, "line": f"{nick}!PUBLIC!orderbook"},
        source=f"dir:{directory}",
    )


def response_count(bot: MakerBot) -> int:
    client = next(iter(bot.directory_clients.values()))
    return client.send_private_message.await_count


@pytest.mark.parametrize("directories", [6, 7])
async def test_watcher_fanout_for_more_than_a_day(
    bot: MakerBot, now: list[float], directories: int
) -> None:
    sources = bot.config.directory_servers[:directories]
    for round_number in range(50):
        for source in sources:
            await request(bot, "J5watcher", source)
        assert response_count(bot) == round_number + 1
        assert not bot._orderbook_rate_limiter.is_banned("J5watcher")
        assert bot._orderbook_rate_limiter.get_violation_count("J5watcher") == 0
        now[0] += 1800.0
        bot._orderbook_rate_limiter.cleanup_old_entries()

    assert bot._orderbook_rate_limiter.get_statistics()["fanout_duplicates"] == 50 * (
        directories - 1
    )


async def test_five_requesters_across_seven_directories_spend_five_tokens(bot: MakerBot) -> None:
    for index in range(5):
        for source in bot.config.directory_servers:
            await request(bot, f"J5requester{index}", source)

    assert response_count(bot) == 5
    assert bot._orderbook_rate_limiter.get_statistics()["total_violations"] == 0
    assert bot._orderbook_rate_limiter.get_statistics()["fanout_duplicates"] == 30
    assert sum(bot._orderbook_proof_work_limiter.try_consume() for _ in range(16)) == 15


async def test_fanout_does_not_log_spam_backoff_but_repeated_source_does(bot: MakerBot) -> None:
    sources = bot.config.directory_servers
    with patch("maker.protocol_handlers.logger.debug") as debug:
        await request(bot, "J5watcher", sources[0])
        await request(bot, "J5watcher", sources[1])
        debug.assert_not_called()
        assert bot._orderbook_rate_limiter.get_violation_count("J5watcher") == 0

        await request(bot, "J5watcher", sources[1])
        assert bot._orderbook_rate_limiter.get_violation_count("J5watcher") == 1
        debug.assert_any_call(
            "Rate limiting orderbook request from J5watcher (violations: 1, backoff: NORMAL)"
        )


@pytest.mark.parametrize("bonded", [False, True])
async def test_default_global_burst_and_recovery(
    bot: MakerBot, now: list[float], bonded: bool
) -> None:
    bot.fidelity_bond = MagicMock() if bonded else None
    source = bot.config.directory_servers[0]
    with patch("maker.protocol_handlers.create_fidelity_bond_proof", return_value="proof") as proof:
        for index in range(21):
            await request(bot, f"J5requester{index}", source)
        assert response_count(bot) == 20
        assert proof.call_count == (20 if bonded else 0)

        now[0] += 0.999
        await request(bot, "J5tooearly", source)
        assert response_count(bot) == 20
        now[0] += 0.0011
        await request(bot, "J5recovered", source)
        assert response_count(bot) == 21
        assert proof.call_count == (21 if bonded else 0)

        now[0] += 20.0
        for index in range(21):
            await request(bot, f"J5later{index}", source)
        assert response_count(bot) == 41
        assert proof.call_count == (41 if bonded else 0)


async def test_delayed_directory_copies_obey_base_interval(bot: MakerBot, now: list[float]) -> None:
    for index, source in enumerate(bot.config.directory_servers):
        await request(bot, "J5delayed", source)
        assert response_count(bot) == index + 1
        now[0] += 10.0
    assert bot._orderbook_rate_limiter.get_violation_count("J5delayed") == 0


async def test_direct_and_directory_requests_share_default_budget(
    bot: MakerBot, now: list[float]
) -> None:
    connection = MagicMock(send=AsyncMock())
    source = bot.config.directory_servers[0]
    for index in range(10):
        await request(bot, f"J5directory{index}", source)
        await bot._send_offers_via_direct_connection(f"J5direct{index}", connection)
    assert response_count(bot) == 10
    assert connection.send.await_count == 10

    await request(bot, "J5directoryblocked", source)
    await bot._send_offers_via_direct_connection("J5directblocked", connection)
    assert response_count(bot) == 10
    assert connection.send.await_count == 10

    now[0] += 1.0
    await bot._send_offers_via_direct_connection("J5directrecovered", connection)
    assert connection.send.await_count == 11
    assert bot._orderbook_response_counts == {
        "directory_admitted": 10,
        "directory_suppressed": 1,
        "direct_admitted": 11,
        "direct_suppressed": 1,
    }


async def test_suppression_logs_are_throttled_but_all_drops_are_counted(bot: MakerBot) -> None:
    for _ in range(20):
        assert bot._orderbook_proof_work_limiter.try_consume()
    connection = MagicMock(send=AsyncMock())
    with (
        patch("maker.bot.time.time", return_value=1000.0),
        patch("maker.bot.logger.warning") as warning,
        patch("maker.bot.logger.debug") as debug,
        patch("maker.protocol_handlers.create_fidelity_bond_proof") as proof,
    ):
        bot.fidelity_bond = MagicMock()
        for index in range(5):
            await request(bot, f"J5directory{index}", bot.config.directory_servers[0])
            await bot._send_offers_via_direct_connection(f"J5direct{index}", connection)
        warning.assert_called_once_with(
            "Suppressing !orderbook response "
            "(global response budget exhausted; refills automatically)"
        )
        debug.assert_called_once_with(
            "Dropping direct orderbook response "
            "(global response budget exhausted; refills automatically)"
        )
        proof.assert_not_called()

    assert bot._orderbook_response_counts == {
        "directory_admitted": 0,
        "directory_suppressed": 5,
        "direct_admitted": 0,
        "direct_suppressed": 5,
    }
    assert response_count(bot) == 0
    connection.send.assert_not_awaited()


@pytest.mark.parametrize("active", [False, True])
async def test_periodic_admission_summary_is_aggregate_only(
    bot: MakerBot, now: list[float], active: bool
) -> None:
    if active:
        for source in bot.config.directory_servers:
            await request(bot, "J5private-requester", source)
    now[0] += 600.0
    bot.running = True
    delays = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if delay == 3600:
            bot.running = False

    with (
        patch("maker.background_tasks.asyncio.sleep", side_effect=sleep),
        patch("maker.background_tasks.logger.info") as info,
    ):
        await bot._periodic_rate_limit_status()

    assert delays == [600, 3600]
    if active:
        info.assert_called_once_with(
            "Orderbook response admission since startup (600s): "
            "directory admitted=1 suppressed=0; direct admitted=0 suppressed=0; "
            "fanout duplicates=6"
        )
    else:
        info.assert_not_called()


async def test_periodic_summary_counts_suppression_without_peer_violations(bot: MakerBot) -> None:
    for _ in range(20):
        assert bot._orderbook_proof_work_limiter.try_consume()
    await request(bot, "J5private-requester", bot.config.directory_servers[0])
    await bot._send_offers_via_direct_connection("J5direct", MagicMock(send=AsyncMock()))
    bot.running = True
    with (
        patch("maker.background_tasks.asyncio.sleep", side_effect=[None, asyncio.CancelledError]),
        patch("maker.background_tasks.logger.info") as info,
    ):
        await bot._periodic_rate_limit_status()
    info.assert_called_once_with(
        "Orderbook response admission since startup (0s): "
        "directory admitted=0 suppressed=1; direct admitted=0 suppressed=1; fanout duplicates=0"
    )


def test_default_port_sources_match_listener_ids(tmp_path: Path) -> None:
    config = MakerConfig(
        mnemonic="test " * 12,
        directory_servers=["localhost", "localhost:5223", "invalid:port"],
        network=NetworkType.REGTEST,
        data_dir=tmp_path,
    )
    bot = MakerBot(wallet=MagicMock(), backend=MagicMock(), config=config)
    assert bot._orderbook_rate_limiter.directory_sources == {
        "dir:localhost:5222",
        "dir:localhost:5223",
    }
