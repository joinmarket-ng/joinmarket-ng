"""Offer withdrawal closes publication without retiring accepted-session resources."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.crypto import NickIdentity
from jmcore.models import Offer
from jmcore.protocol import JM_VERSION
from test_offer_refresh import (
    NEW_MAXSIZE,
    OLD_MAXSIZE,
    _DelayGate,
    _make_bot,
    _offer,
    _recording_client,
)

from maker.bot import MakerBot
from maker.generation import GenerationState, MakerGeneration


@pytest.fixture
def bot() -> Iterator[MakerBot]:
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    instance = _make_bot(wallet, MagicMock(), 0)
    instance.running = True
    instance.generations[0].directory_pool.list_disconnected = MagicMock(return_value=[])
    yield instance


def connect(bot: MakerBot, client: MagicMock) -> None:
    bot.directory_clients = {"online": client}
    bot.generations[0].directory_clients = bot.directory_clients


async def test_offline_debt_is_superseded_and_reconnect_only_withdraws(
    bot: MakerBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = _recording_client()
    connect(bot, directory)
    generation = bot.generations[0]
    generation.directory_pool.list_disconnected = MagicMock(
        return_value=[("offline.test:5222", "offline")]
    )
    generation.offer_delivery.queue(["offline"], {9: "stale-announcement"})
    await bot.close_offer_admission()
    assert directory.sent == ["cancel 0", "cancel 9"]
    assert generation.offer_delivery.pending["offline"] == {0: None, 9: None}
    assert bot.running and not bot._stopping and generation.state is GenerationState.ACCEPTING
    assert bot.current_offers[0].maxsize == OLD_MAXSIZE
    reconnected = _recording_client()
    monkeypatch.setattr(bot, "_listen_client", AsyncMock())
    try:
        await bot._install_reconnected_directory(
            generation,
            0,
            "offline.test:5222",
            "offline",
            ("offline", reconnected),
        )
        assert reconnected.sent == ["cancel 0", "cancel 9"]
        assert "offline" not in generation.offer_delivery.pending
        await bot._announce_offers()
        assert all(message.startswith("cancel ") for message in directory.sent + reconnected.sent)
    finally:
        for task in bot.listen_tasks:
            task.cancel()
        await asyncio.gather(*bot.listen_tasks, return_exceptions=True)


@pytest.mark.parametrize("phase", ["build", "unchanged-build", "delay", "nick"])
async def test_closure_across_every_refresh_suspension_never_adopts_or_reannounces(
    bot: MakerBot,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    directory = _recording_client()
    connect(bot, directory)
    entered, release = asyncio.Event(), asyncio.Event()
    proposed = bot.current_offers if phase == "unchanged-build" else [_offer(bot, 0, NEW_MAXSIZE)]

    async def build() -> list[Offer]:
        if phase in {"build", "unchanged-build"}:
            entered.set()
            await release.wait()
        return proposed

    async def nick() -> None:
        if phase == "nick":
            entered.set()
            await release.wait()

    monkeypatch.setattr(bot.offer_manager, "create_offers", build)
    monkeypatch.setattr(bot, "_regenerate_nick", nick)
    gate = _DelayGate()
    if phase == "delay":
        bot.config.offer_reannounce_delay_max = 30
        entered, release = gate.entered, gate.release
    with patch("maker.bot.asyncio.sleep", new=gate.sleep) if phase == "delay" else nullcontext():
        refreshing = asyncio.create_task(bot._update_offers())
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await bot.close_offer_admission()
            release.set()
            await refreshing
            assert bot.current_offers[0].maxsize == OLD_MAXSIZE
            assert directory.sent and all(
                message.startswith("cancel ") for message in directory.sent
            )
        finally:
            release.set()
            await asyncio.gather(refreshing, return_exceptions=True)


async def test_inflight_public_send_can_finish_but_canceling_closure_keeps_its_withdrawal(
    bot: MakerBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    directory = _recording_client()
    connect(bot, directory)
    sending = directory.send_public_message

    async def blocked(message: str) -> None:
        if not message.startswith("cancel "):
            entered.set()
            await release.wait()
        await sending(message)

    monkeypatch.setattr(directory, "send_public_message", blocked)
    announcing = asyncio.create_task(bot._announce_offers())
    await entered.wait()
    closing = asyncio.create_task(bot.close_offer_admission())
    await asyncio.sleep(0)
    try:
        assert bot._fill_admission_closed and bot._offer_admission_closed and not closing.done()
        assert bot.generations[0].offer_delivery.pending["online"] == {0: None}
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not announcing.cancelled()
        release.set()
        await announcing
        await bot._flush_offer_delivery()
        assert directory.sent[0].startswith("sw0reloffer ")
        assert directory.sent[1:] == ["cancel 0"]
    finally:
        release.set()
        await asyncio.gather(announcing, closing, return_exceptions=True)


@pytest.mark.parametrize("direct", [False, True])
async def test_private_response_stops_after_its_inflight_message_and_new_requests_are_closed(
    bot: MakerBot,
    direct: bool,
) -> None:
    bot.current_offers.append(_offer(bot, 1, OLD_MAXSIZE))
    entered, release = asyncio.Event(), asyncio.Event()
    directory = _recording_client()
    connect(bot, directory)
    connection = MagicMock()

    async def blocked(*_args: object) -> None:
        entered.set()
        await release.wait()

    directory.send_private_message = AsyncMock(side_effect=blocked)
    connection.send = AsyncMock(side_effect=blocked)
    response = asyncio.create_task(
        bot._send_offers_via_direct_connection("J5Taker", connection)
        if direct
        else bot._send_offers_to_taker("J5Taker")
    )
    await entered.wait()
    try:
        await bot.close_offer_admission()
        release.set()
        await response
        sender = connection.send if direct else directory.send_private_message
        assert sender.await_count == 1
        await bot._send_offers_via_direct_connection("J5Other", connection)
        await bot._send_offers_to_taker("J5Other")
        assert sender.await_count == 1
    finally:
        release.set()
        await asyncio.gather(response, return_exceptions=True)


async def test_retained_and_late_rotated_identities_never_reopen_publication(bot: MakerBot) -> None:
    old = bot.generations[0]
    old_client, new_client = _recording_client(), _recording_client()
    connect(bot, old_client)
    pool = MagicMock()
    pool.list_disconnected.return_value = []
    replacement = MakerGeneration(
        generation_id=1,
        nick_identity=NickIdentity(JM_VERSION),
        offer_manager=MagicMock(),
        directory_pool=pool,
        directory_clients={"new": new_client},
        current_offers=[_offer(bot, 7, NEW_MAXSIZE)],
    )
    await bot.close_offer_admission()
    bot.generations[1] = replacement
    old.state = GenerationState.GRACE
    bot._activate_generation(replacement)
    await bot._announce_generation_offers(replacement)
    assert new_client.sent == ["cancel 7"]
    await bot._announce_generation_offers(old)  # A cutover rollback must obey the same gate.
    assert all(message.startswith("cancel ") for message in old_client.sent)
    await bot.close_offer_admission()
    assert bot.generations[0] is old and bot.generations[1] is replacement
    pool.close.assert_not_called()
    assert old.state is GenerationState.GRACE and replacement.state is GenerationState.ACCEPTING
