"""Focused tests for maker transport diagnostic log levels."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.crypto import NickIdentity
from jmcore.models import NetworkType, Offer, OfferType
from jmcore.network import ONION_HOSTID
from jmcore.protocol import JM_VERSION, MessageType, create_handshake_request
from loguru import logger

from maker.bot import MakerBot
from maker.config import MakerConfig

LogRecord = tuple[str, str]


@pytest.fixture
def maker_bot(tmp_path: Path) -> MakerBot:
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    backend = MagicMock()
    backend.can_provide_neutrino_metadata.return_value = False
    config = MakerConfig(
        mnemonic="test " * 12,
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
        data_dir=tmp_path,
    )
    return MakerBot(wallet=wallet, backend=backend, config=config)


@pytest.fixture
def captured_logs() -> Iterator[list[LogRecord]]:
    records: list[LogRecord] = []

    def capture_log(message: Any) -> None:
        records.append((message.record["level"].name, message.record["message"]))

    handler_id = logger.add(capture_log, level="TRACE")
    try:
        yield records
    finally:
        logger.remove(handler_id)


def assert_trace(records: list[LogRecord], message_fragment: str) -> None:
    levels = [level for level, message in records if message_fragment in message]
    assert levels
    assert set(levels) == {"TRACE"}


@pytest.mark.asyncio
async def test_generic_protocol_diagnostics_use_trace(
    maker_bot: MakerBot, captured_logs: list[LogRecord]
) -> None:
    message = {
        "type": MessageType.PUBMSG.value,
        "line": "J5peer!PUBLIC!announcement",
    }

    await maker_bot._handle_message(message, source="dir:first")
    await maker_bot._handle_message(message, source="dir:second")
    await maker_bot._handle_message(
        {"type": MessageType.PEERLIST.value, "line": "J5peer, J5other"}, source="dir:first"
    )
    await maker_bot._handle_message({"type": "unknown", "line": "ignored"}, source="dir:first")

    assert_trace(captured_logs, "PUBMSG parts=")
    assert_trace(captured_logs, "Duplicate message #2")
    assert_trace(captured_logs, "Received peerlist: J5peer, J5other...")
    assert_trace(captured_logs, "Ignoring message type unknown")


@pytest.mark.asyncio
async def test_orderbook_offer_send_diagnostics_use_trace(
    maker_bot: MakerBot, captured_logs: list[LogRecord]
) -> None:
    maker_bot.current_offers = [
        Offer(
            counterparty=maker_bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=1_000_000,
            txfee=0,
            cjfee="0.0001",
        )
    ]
    client = MagicMock()
    client.send_private_message = AsyncMock()
    maker_bot.directory_clients["directory"] = client
    maker_bot._directory_orderbook_response_limiter = MagicMock()
    maker_bot._directory_orderbook_response_limiter.try_consume.return_value = True

    await maker_bot._send_offers_to_taker("J5peer")

    assert_trace(captured_logs, "Received !orderbook request")
    assert_trace(captured_logs, "Sent sw0reloffer offer")


@pytest.mark.asyncio
async def test_direct_command_summary_uses_trace(
    maker_bot: MakerBot, captured_logs: list[LogRecord]
) -> None:
    maker_bot.running = True
    taker = NickIdentity(JM_VERSION)
    handshake = create_handshake_request(
        nick=taker.nick,
        location="NOT-SERVING-ONION",
        network=NetworkType.REGTEST.value,
        directory=False,
    )
    signed_data = taker.sign_message("payload", ONION_HOSTID)
    messages = [
        json.dumps({"type": MessageType.HANDSHAKE.value, "line": json.dumps(handshake)}).encode(),
        json.dumps(
            {
                "type": MessageType.PRIVMSG.value,
                "line": f"{taker.nick}!{maker_bot.nick}!unknown {signed_data}",
            }
        ).encode(),
    ]
    connection = MagicMock()
    connection.is_connected.side_effect = [True, True, False]
    connection.receive = AsyncMock(side_effect=messages)
    connection.send = AsyncMock()
    connection.close = AsyncMock()

    await maker_bot._on_direct_connection(connection, "peer:1")

    assert_trace(captured_logs, f"Direct message from {taker.nick}: cmd=unknown")
    assert_trace(captured_logs, f"Unknown direct command from {taker.nick}: unknown")


@pytest.mark.asyncio
async def test_spam_is_reported_as_bounded_episodes(
    maker_bot: MakerBot, captured_logs: list[LogRecord]
) -> None:
    """One INFO line on start, one per hour while it lasts, one on end; bans stay at DEBUG."""
    clock = [10_000.0]
    orderbook = maker_bot._orderbook_rate_limiter
    direct = maker_bot._direct_connection_rate_limiter

    def spam(nick: str, requests: int) -> None:
        for _ in range(requests):
            orderbook.check(nick, source="dir:localhost:5222")

    # Actions taken after each hourly tick (ticks happen at 10min, then every hour).
    after_tick: list[object] = [lambda: spam("J5other", 3), None, None, "stop"]
    maker_bot.running = True

    async def sleep(delay: float) -> None:
        clock[0] += delay
        if delay != 3600:
            return
        action = after_tick.pop(0)
        if action == "stop":
            maker_bot.running = False
        elif callable(action):
            action()

    with (
        patch("maker.background_tasks.asyncio.sleep", side_effect=sleep),
        patch("maker.background_tasks.time.monotonic", side_effect=lambda: clock[0]),
        patch("maker.rate_limiting.time.monotonic", side_effect=lambda: clock[0]),
    ):
        # First request is admitted; the ban lands on the check after the threshold is reached.
        spam("J5spammer", orderbook.violation_ban_threshold + 2)
        for _ in range(direct.orderbook_ban_threshold + 1):
            direct.check_orderbook("peer:1")
        assert orderbook.is_banned("J5spammer")
        assert direct.is_banned("peer:1")

        await maker_bot._periodic_rate_limit_status()

    episodes = [message for _, message in captured_logs if message.startswith("Spam episode")]
    assert [message.split("; top")[0] for message in episodes] == [
        "Spam episode started: +110 violations, +2 bans since last report; 2 currently banned",
        "Spam episode ongoing (1h): +2 violations, +0 bans since last report; 0 currently banned",
        "Spam episode ended (quiet for an hour) after 1h: 112 violations, 2 bans in total",
    ]
    assert [level for level, message in captured_logs if message.startswith("Spam episode")] == [
        "INFO"
    ] * 3
    detail = [
        level
        for level, message in captured_logs
        if message.startswith(("BANNED", "Rate limit status:", "Direct connection rate limit:"))
    ]
    assert detail and set(detail) == {"DEBUG"}
    assert [m for level, m in captured_logs if level in {"INFO", "WARNING", "ERROR"}] == episodes


@pytest.mark.asyncio
async def test_directory_receive_count_uses_trace(
    maker_bot: MakerBot, captured_logs: list[LogRecord]
) -> None:
    maker_bot.running = True
    client = MagicMock()
    client.listen_for_messages = AsyncMock(return_value=[{"type": MessageType.PEERLIST.value}])
    maker_bot.directory_clients["directory"] = client

    async def handle_message(*_args: object, **_kwargs: object) -> None:
        maker_bot.running = False

    maker_bot._handle_message = handle_message

    await maker_bot._listen_client("directory", client)

    assert_trace(captured_logs, "Received 1 messages from directory")
