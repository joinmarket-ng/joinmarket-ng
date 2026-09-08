"""Focused tests for maker transport diagnostic log levels."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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
