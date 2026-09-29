"""A fallback send must not outlive or block the response collection phase."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from _taker_test_helpers import make_directory_client
from jmcore.crypto import NickIdentity
from jmcore.network import ONION_HOSTID
from jmcore.protocol import MessageType


@pytest.mark.parametrize("timeout", [0.0, 0.05])
async def test_fallback_never_starts_at_or_after_deadline(timeout: float) -> None:
    client = make_directory_client()
    client.clients = {}
    fallback = AsyncMock()
    assert (
        await client.wait_for_responses(
            ["silent"], "!sig", timeout=timeout, on_stalled=(timeout, fallback)
        )
        == {}
    )
    fallback.assert_not_awaited()


@pytest.mark.parametrize("respond", [False, True])
async def test_blocked_fallback_does_not_block_collection_or_deadline(respond: bool) -> None:
    client = make_directory_client()
    client.clients = {}
    maker = NickIdentity(5)
    canceled = asyncio.Event()

    async def fallback(nicks: set[str]) -> None:
        assert nicks == {maker.nick}
        try:
            if respond:
                client._direct_message_queue.put_nowait(
                    {
                        "type": MessageType.PRIVMSG.value,
                        "line": f"{maker.nick}!{client.nick}!sig "
                        + maker.sign_message("signature", ONION_HOSTID),
                    }
                )
            await asyncio.Future[None]()
        finally:
            canceled.set()

    # The outer guard turns a regression into a failure rather than a hung suite.
    responses = await asyncio.wait_for(
        client.wait_for_responses([maker.nick], "!sig", timeout=1.5, on_stalled=(0.0, fallback)),
        timeout=3.0,
    )
    assert (maker.nick in responses) is respond
    assert canceled.is_set()


async def test_canceling_collection_cancels_fallback() -> None:
    client = make_directory_client()
    client.clients = {}
    started, canceled = asyncio.Event(), asyncio.Event()

    async def fallback(nicks: set[str]) -> None:
        started.set()
        try:
            await asyncio.Future[None]()
        finally:
            canceled.set()

    task = asyncio.create_task(
        client.wait_for_responses(["silent"], "!sig", timeout=30, on_stalled=(0, fallback))
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert canceled.is_set()


async def test_queued_response_prevents_unnecessary_fallback() -> None:
    client = make_directory_client()
    client.clients = {}
    maker = NickIdentity(5)
    client._direct_message_queue.put_nowait(
        {
            "type": MessageType.PRIVMSG.value,
            "line": f"{maker.nick}!{client.nick}!sig "
            + maker.sign_message("signature", ONION_HOSTID),
        }
    )
    fallback = AsyncMock()
    responses = await client.wait_for_responses(
        [maker.nick], "!sig", timeout=1, on_stalled=(0, fallback)
    )
    assert maker.nick in responses
    fallback.assert_not_awaited()


async def test_fallback_error_is_observed_and_propagated() -> None:
    client = make_directory_client()
    client.clients = {}

    async def fallback(nicks: set[str]) -> None:
        raise RuntimeError("unexpected callback failure")

    with pytest.raises(RuntimeError, match="unexpected callback failure"):
        await client.wait_for_responses(["silent"], "!sig", timeout=2, on_stalled=(0, fallback))


async def test_fallback_runs_once_while_maker_stays_silent() -> None:
    client = make_directory_client()
    client.clients = {}
    fallback = AsyncMock()
    responses: dict[str, Any] = await client.wait_for_responses(
        ["silent"], "!sig", timeout=1.2, on_stalled=(0, fallback)
    )
    assert responses == {}
    fallback.assert_awaited_once_with({"silent"})
