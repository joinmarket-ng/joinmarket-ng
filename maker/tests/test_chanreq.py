"""The maker funder-side of the true-pooling ring channel-open round-trip.

The taker sends ``chanreq <encrypted "node@host capacity">``; the maker opens a real
Lightning channel of that capacity to the assigned ring partner and replies
``chanaddr`` with the funding address (or empty on failure, so the taker falls back to
plain change).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from maker.maker_session import MakerSession


def _session(open_result: str | None) -> tuple[MakerSession, MagicMock]:
    inner = MagicMock()
    inner.taker_nick = "J5taker"
    inner.crypto.is_encrypted = True
    inner.crypto.decrypt.return_value = "02aa@1.2.3.4:9735 500000"
    inner.crypto.encrypt.side_effect = lambda s: f"enc({s})"
    inner.open_external_channel = AsyncMock(return_value=open_result)
    return MakerSession(inner=inner), inner


def _bot() -> tuple[MagicMock, AsyncMock]:
    client = MagicMock()
    client.send_private_message = AsyncMock()
    bot = MagicMock()
    bot.directory_clients = {"d": client}
    return bot, client.send_private_message


async def test_chanreq_opens_channel_and_replies_chanaddr() -> None:
    session, inner = _session("bcrt1ptaprootfunding")
    bot, send = _bot()

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.open_external_channel.assert_awaited_once_with("02aa@1.2.3.4:9735", 500000)
    assert send.await_args is not None
    nick, command, content = send.await_args.args
    assert nick == "J5taker"
    assert command == "chanaddr"
    assert content == "enc(bcrt1ptaprootfunding)"


async def test_chanreq_open_failure_replies_empty_address() -> None:
    session, inner = _session(None)  # open failed
    bot, send = _bot()

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    assert send.await_args is not None
    _, command, content = send.await_args.args
    assert command == "chanaddr"
    assert content == "enc()"  # empty address -> taker falls back to plain change
