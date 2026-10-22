"""Closing new fills never turns accepted sessions into completed obligations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from jmcore.models import NetworkType, Offer, OfferType
from jmswap.channel_ring_nodes import ChannelRingNodePool
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService
from pydantic import SecretStr

from maker import channel_ring, maker_session, protocol_handlers
from maker.bot import MakerBot
from maker.coinjoin import CoinJoinSession, CoinJoinState
from maker.config import MakerConfig, OfferConfig


@pytest.fixture
async def bot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[MakerBot]:
    wallet = MagicMock(spec=WalletService)
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    wallet.data_dir = tmp_path
    backend = MagicMock(spec=BlockchainBackend)
    backend.can_provide_neutrino_metadata.return_value = True
    backend.requires_neutrino_metadata.return_value = False
    config = MakerConfig(
        mnemonic=SecretStr("all " * 11 + "all"),
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
        offer_configs=[
            OfferConfig(offer_type=OfferType.SW0_ABSOLUTE, min_size=100_000, cj_fee_absolute=500)
        ],
    )
    instance = MakerBot(
        wallet=cast(WalletService, wallet), backend=cast(BlockchainBackend, backend), config=config
    )
    instance.current_offers = [
        Offer(
            counterparty=instance.nick,
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=100_000,
            maxsize=900_000,
            txfee=0,
            cjfee=500,
        )
    ]
    instance.running = True
    monkeypatch.setattr(instance, "_send_response", AsyncMock())
    monkeypatch.setattr(instance, "_initialize_minimum_fee_policy", AsyncMock())
    monkeypatch.setattr(protocol_handlers, "check_commitment", Mock(return_value=True))
    monkeypatch.setattr(
        protocol_handlers, "get_notifier", lambda: SimpleNamespace(notify_fill_request=AsyncMock())
    )

    def inner(**kwargs: Any) -> CoinJoinSession:
        session = MagicMock(spec=CoinJoinSession)
        session.taker_nick = kwargs["taker_nick"]
        session.offer = kwargs["offer"]
        session.session_timeout_sec = 60.0
        session.state = CoinJoinState.PUBKEY_SENT
        session.commitment_authenticated = False
        session.our_utxos = {}
        session.mixdepth = 1
        session.crypto = MagicMock()

        async def fill(_amount: int, commitment: str, _key: str) -> tuple[bool, dict[str, object]]:
            session.commitment = bytes.fromhex(commitment)
            return True, {"pubkey": "maker-public-key"}

        session.handle_fill = AsyncMock(side_effect=fill)
        return cast(CoinJoinSession, session)

    monkeypatch.setattr(protocol_handlers, "CoinJoinSession", inner)
    try:
        yield instance
    finally:
        for task in instance._session_handler_tasks | instance._detached_handler_tasks:
            task.cancel()
        await asyncio.gather(
            *instance._session_handler_tasks,
            *instance._detached_handler_tasks,
            return_exceptions=True,
        )


def request(number: int) -> str:
    return f"fill 0 500000 taker-public-key P{number:064x}"


async def test_closed_fills_allocate_nothing_and_cannot_replace_accepted_session(
    bot: MakerBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await bot._handle_fill("peer", request(1))
    original = bot.active_sessions[(0, "peer")]
    release = Mock()
    monkeypatch.setattr(original, "release_input_locks", release)
    await bot.close_fill_admission()
    await bot._handle_fill("peer", request(2))
    await bot._handle_fill("new-peer", request(3))
    assert bot.active_sessions == {(0, "peer"): original}
    assert bot._reserved_commitments == {f"{1:064x}"}
    release.assert_not_called()


@pytest.mark.parametrize("phase", ["fee", "fill"])
@pytest.mark.parametrize("closure", ["fill", "offer"])
async def test_preaccepted_fill_finishes_after_closure_without_being_canceled(
    bot: MakerBot,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    closure: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    if phase == "fee":

        async def fee(**_kwargs: object) -> None:
            entered.set()
            await release.wait()

        monkeypatch.setattr(bot, "_initialize_minimum_fee_policy", fee)
    else:
        original = protocol_handlers.CoinJoinSession

        def pending(**kwargs: Any) -> CoinJoinSession:
            session = original(**kwargs)
            original_fill = session.handle_fill

            async def fill(*args: Any) -> Any:
                entered.set()
                await release.wait()
                return await original_fill(*args)

            monkeypatch.setattr(session, "handle_fill", fill)
            return session

        monkeypatch.setattr(protocol_handlers, "CoinJoinSession", pending)
    filling = asyncio.create_task(bot._handle_fill("peer", request(1)))
    await entered.wait()
    close = bot.close_fill_admission if closure == "fill" else bot.close_offer_admission
    closing = asyncio.create_task(close())
    await asyncio.sleep(0)
    try:
        assert bot._fill_admission_closed and not closing.done()
        await bot._handle_fill("other", request(2))
        assert f"{2:064x}" not in bot._reserved_commitments
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not filling.cancelled()
        release.set()
        await filling
        await bot.close_fill_admission()
        assert (0, "peer") in bot.active_sessions
        assert bot._active_fill_admissions == 0
        assert bot.running and not bot._stopping
    finally:
        release.set()
        await asyncio.gather(filling, closing, return_exceptions=True)


async def test_canceling_an_admitted_handler_does_not_clear_its_unknown_reservation(
    bot: MakerBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def fee(**_kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(bot, "_initialize_minimum_fee_policy", fee)
    filling = asyncio.create_task(bot._handle_fill("peer", request(1)))
    await entered.wait()
    filling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await filling
    await bot.close_fill_admission()
    assert bot._active_fill_admissions == 0 and not bot.active_sessions
    assert bot._reserved_commitments == {f"{1:064x}"}
    assert bot.running and not bot._stopping


async def test_multiple_fills_and_closure_waiters_have_independent_lifetimes(
    bot: MakerBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = {peer: asyncio.Event() for peer in ("first", "second")}
    release = {peer: asyncio.Event() for peer in entered}
    original = protocol_handlers.CoinJoinSession

    def pending(**kwargs: Any) -> CoinJoinSession:
        session = original(**kwargs)
        peer = kwargs["taker_nick"]
        original_fill = session.handle_fill

        async def fill(*args: Any) -> Any:
            entered[peer].set()
            await release[peer].wait()
            return await original_fill(*args)

        monkeypatch.setattr(session, "handle_fill", fill)
        return session

    monkeypatch.setattr(protocol_handlers, "CoinJoinSession", pending)
    fills = [
        asyncio.create_task(bot._handle_fill(peer, request(number)))
        for number, peer in enumerate(entered, 1)
    ]
    await asyncio.gather(*(event.wait() for event in entered.values()))
    waiters = [asyncio.create_task(bot.close_fill_admission()) for _ in range(2)]
    await asyncio.sleep(0)
    try:
        assert bot._active_fill_admissions == 2 and all(not task.done() for task in waiters)
        waiters[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiters[0]
        release["first"].set()
        await fills[0]
        assert bot._active_fill_admissions == 1 and not waiters[1].done()
        release["second"].set()
        await asyncio.gather(fills[1], waiters[1])
        assert bot._active_fill_admissions == 0 and len(bot.active_sessions) == 2
    finally:
        for event in release.values():
            event.set()
        await asyncio.gather(*fills, *waiters, return_exceptions=True)


@pytest.mark.parametrize("closure", ["fill", "offer"])
async def test_accepted_auth_and_first_ring_dispatch_keep_original_node_resources(
    bot: MakerBot,
    monkeypatch: pytest.MonkeyPatch,
    closure: str,
) -> None:
    await bot._handle_fill("peer", request(1))
    session = bot.active_sessions[(0, "peer")]
    authenticated = AsyncMock()
    monkeypatch.setattr(session, "on_auth", authenticated)
    pool = MagicMock(spec=ChannelRingNodePool)
    original_node = object()
    pool.for_mixdepth.return_value = original_node
    store = MagicMock()
    bot._channel_ring_nodes = cast(ChannelRingNodePool, pool)
    bot._channel_ring_store = store
    bot.config.channel_ring = bot.config.channel_ring.model_copy(update={"enabled": True})
    bot.channel_ring_capability_validated = True
    if closure == "fill":
        await bot.close_fill_admission()
    else:
        await bot.close_offer_admission()
    await bot._handle_auth("peer", "auth ciphertext")
    authenticated.assert_awaited_once()
    session.state = CoinJoinState.IOAUTH_SENT
    crypto = cast(MagicMock, session.inner.crypto)
    crypto.is_encrypted = True
    crypto.decrypt.return_value = "encoded-invitation"
    invitation = object()
    monkeypatch.setattr(
        maker_session, "verify_signed_privmsg", lambda *_args: (True, "ring", "ciphertext")
    )
    monkeypatch.setattr(maker_session, "decode_ring_message", lambda _raw: invitation)
    participant = MagicMock()
    participant.handle = AsyncMock(return_value=[])
    factory = Mock(return_value=participant)
    monkeypatch.setattr(channel_ring, "MakerRingParticipant", factory)
    await bot._handle_ring("peer", "ring ciphertext signature pubkey")
    participant.handle.assert_awaited_once_with(invitation)
    assert factory.call_args is not None
    assert factory.call_args.kwargs["initialized_backend"] is original_node
    assert factory.call_args.kwargs["store"] is store
    assert bot._channel_ring_nodes is pool and bot._channel_ring_store is store
    assert session.ring_participant is participant and bot.active_sessions[(0, "peer")] is session
    pool.close.assert_not_called()
    store.close.assert_not_called()
