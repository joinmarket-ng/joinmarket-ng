"""Focused tests for maker identity generation ownership and cutover."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.bitcoin import get_txid
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClientError
from jmcore.models import NetworkType
from jmcore.network import HiddenServiceListener, TCPConnection, connect_direct
from jmcore.protocol import JM_VERSION, MessageType

from maker.bot import MakerBot
from maker.coinjoin import CoinJoinState
from maker.config import MakerConfig
from maker.directory_pool import MakerDirectoryPool
from maker.generation import GenerationState, MakerGeneration
from maker.maker_session import MakerSession, PendingSignedRound
from maker.offers import OfferManager


@pytest.fixture
def config() -> MakerConfig:
    return MakerConfig(
        mnemonic="test " * 12,
        network=NetworkType.REGTEST,
        directory_servers=["directory.onion:5222"],
        stream_isolation=True,
        identity_renewal_min_sec=60,
        identity_renewal_max_sec=120,
        identity_grace_sec=60,
        identity_rotation_quiet_min_sec=0,
        identity_rotation_quiet_max_sec=0,
    )


@pytest.fixture
def bot(config: MakerConfig) -> MakerBot:
    backend = MagicMock()
    backend.can_provide_neutrino_metadata.return_value = False
    wallet = MagicMock()
    return MakerBot(wallet=wallet, backend=backend, config=config)


def _generation(bot: MakerBot, generation_id: int) -> MakerGeneration:
    identity = NickIdentity(JM_VERSION)
    pool = MakerDirectoryPool(
        config=bot.config,
        nick_identity=identity,
        neutrino_compat=False,
        onion_host=f"generation-{generation_id}.onion",
        onion_serving_port=bot.config.onion_serving_port,
    )
    manager = OfferManager(bot.wallet, bot.config, identity.nick)
    generation = MakerGeneration(
        generation_id=generation_id,
        nick_identity=identity,
        offer_manager=manager,
        directory_pool=pool,
    )
    pool.clients = generation.directory_clients
    return generation


@pytest.mark.parametrize("stream_isolation", [False, True])
def test_generation_pools_use_distinct_stable_tor_credentials(
    config: MakerConfig, stream_isolation: bool
) -> None:
    config = config.model_copy(update={"stream_isolation": stream_isolation})
    first = MakerDirectoryPool(
        config=config,
        nick_identity=NickIdentity(JM_VERSION),
        neutrino_compat=False,
    )
    second = MakerDirectoryPool(
        config=config,
        nick_identity=NickIdentity(JM_VERSION),
        neutrino_compat=False,
    )

    assert first._dir_creds != second._dir_creds
    first_kwargs = first._build_client_kwargs("directory.onion", 5222)
    repeated_kwargs = first._build_client_kwargs("directory.onion", 5222)
    assert first_kwargs["socks_username"] == repeated_kwargs["socks_username"]
    assert first_kwargs["socks_password"] == repeated_kwargs["socks_password"]
    assert first_kwargs["socks_password"] == first._dir_creds[1]


def test_generations_own_distinct_identity_and_mutable_state(bot: MakerBot) -> None:
    first = bot.generations[0]
    second = _generation(bot, 1)

    assert first.nick_identity is not second.nick_identity
    assert first.nick_identity.nick != second.nick_identity.nick
    assert first.offer_manager is not second.offer_manager
    assert first.directory_pool is not second.directory_pool
    assert first.directory_clients is not second.directory_clients
    assert first.current_offers is not second.current_offers
    assert first.direct_connections is not second.direct_connections
    assert first.direct_connection_states is not second.direct_connection_states


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active,bonded", [(False, False), (False, True), (True, False), (True, True)]
)
async def test_scheduler_samples_independently_of_activity(
    bot: MakerBot, active: bool, bonded: bool
) -> None:
    bot.running = True
    bot.fidelity_bond = MagicMock() if bonded else None
    if active:
        bot.active_sessions[(0, "J5Active")] = MagicMock()
    bot.current_offers = [MagicMock()]
    samples: list[tuple[int, int]] = []

    sleep_count = 0

    async def sleep(delay: float) -> None:
        nonlocal sleep_count
        samples.append((bot.config.identity_renewal_min_sec, bot.config.identity_renewal_max_sec))
        assert delay == 73.0
        sleep_count += 1
        if sleep_count == 2:
            bot.running = False

    with (
        patch("maker.bot.secure_random.uniform", return_value=73.0),
        patch("maker.bot.asyncio.sleep", new=AsyncMock(side_effect=sleep)),
        patch.object(bot, "_rotate_generation", new=AsyncMock(return_value=True)) as rotate,
    ):
        await bot._identity_renewal_scheduler()

    assert samples == [(60, 120), (60, 120)]
    rotate.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("bonded", [False, True])
async def test_cutover_silently_disconnects_before_replacement(bot: MakerBot, bonded: bool) -> None:
    old = bot.generations[0]
    old_nick = old.nick_identity.nick
    old.current_offers = [MagicMock(oid=0), MagicMock(oid=1)]
    bot.current_offers = old.current_offers
    old_client = MagicMock()
    old_client.close = AsyncMock()
    old_client.send_public_message = AsyncMock()
    old.directory_clients["directory:5222"] = old_client
    old_connection = MagicMock()
    old.direct_connections["J5SameTaker"] = old_connection
    old.hidden_service_listener = MagicMock()
    old.hidden_service_listener.stop = AsyncMock()
    replacement = _generation(bot, 1)
    replacement.directory_pool.connect_all_with_retry = AsyncMock(return_value=1)
    new_connection = MagicMock()
    replacement.direct_connections["J5SameTaker"] = new_connection
    bot.running = True
    bot.fidelity_bond = MagicMock() if bonded else None
    publish_nick_change = MagicMock()
    bot._nick_change_callback = publish_nick_change
    events: list[str] = []
    old_client.close.side_effect = lambda: events.append("old-close")

    async def connect_replacement(**_kwargs: object) -> int:
        events.append("new-connect")
        return 1

    replacement.directory_pool.connect_all_with_retry.side_effect = connect_replacement

    async def announce(_generation: MakerGeneration) -> None:
        events.append("new-announce")

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch.object(bot, "_announce_generation_offers", side_effect=announce),
        patch.object(bot, "_start_generation_listeners"),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is True

    assert bot.current_generation_id == 1
    assert old.state is GenerationState.GRACE
    assert old.direct_connections["J5SameTaker"] is old_connection
    assert bot.direct_connections["J5SameTaker"] is new_connection
    old.hidden_service_listener.stop.assert_awaited_once_with()
    old_client.send_public_message.assert_not_awaited()
    assert events == ["old-close", "new-connect", "new-announce"]
    publish_nick_change.assert_called_once_with(old_nick, replacement.nick_identity.nick)
    for task in old.tasks:
        task.cancel()
    await asyncio.gather(*old.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cutover_waits_quietly_after_old_disconnect(bot: MakerBot) -> None:
    old = bot.generations[0]
    old_client = MagicMock()
    old_client.close = AsyncMock()
    old.directory_clients["directory:5222"] = old_client
    replacement = _generation(bot, 1)
    replacement.directory_pool.connect_all_with_retry = AsyncMock(return_value=1)
    bot.config.identity_rotation_quiet_min_sec = 60
    bot.config.identity_rotation_quiet_max_sec = 600
    bot.running = True
    events: list[str] = []
    old_client.close.side_effect = lambda: events.append("old-close")

    async def sleep(delay: float) -> None:
        events.append(f"sleep:{delay}")

    async def connect_replacement(**_kwargs: object) -> int:
        events.append("new-connect")
        return 1

    replacement.directory_pool.connect_all_with_retry.side_effect = connect_replacement
    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch.object(bot, "_announce_generation_offers", new=AsyncMock()),
        patch.object(bot, "_start_generation_listeners"),
        patch("maker.bot.secure_random.uniform", return_value=173.0),
        patch("maker.bot.asyncio.sleep", side_effect=sleep),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is True

    assert events == ["old-close", "sleep:173.0", "new-connect"]
    for task in old.tasks:
        task.cancel()
    await asyncio.gather(*old.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_rotation_drains_generation_pinned_session_before_disconnect(bot: MakerBot) -> None:
    old = bot.generations[0]
    old_client = MagicMock()
    old_client.close = AsyncMock()
    old.directory_clients["directory:5222"] = old_client
    bot.active_sessions[(0, "J5Active")] = MagicMock()
    replacement = _generation(bot, 1)
    replacement.directory_pool.connect_all_with_retry = AsyncMock(return_value=1)
    bot.running = True
    sleeps = 0

    async def sleep(_delay: float) -> None:
        nonlocal sleeps
        sleeps += 1
        bot.active_sessions.clear()

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch.object(bot, "_announce_generation_offers", new=AsyncMock()),
        patch.object(bot, "_start_generation_listeners"),
        patch("maker.bot.asyncio.sleep", side_effect=sleep),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is True

    assert sleeps == 1
    old_client.close.assert_awaited_once_with()
    for task in old.tasks:
        task.cancel()
    await asyncio.gather(*old.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_replacement_connect_failure_closes_prepared_generation(bot: MakerBot) -> None:
    old = bot.generations[0]
    old_client = MagicMock()
    old_client.close = AsyncMock()
    old.directory_clients["directory:5222"] = old_client
    old.hidden_service_listener = MagicMock(running=True, port=0)
    old.hidden_service_listener.stop = AsyncMock(
        side_effect=lambda: setattr(old.hidden_service_listener, "running", False)
    )
    old.hidden_service_listener.start = AsyncMock(
        side_effect=lambda: setattr(old.hidden_service_listener, "running", True)
    )
    old.hidden_service_listener.serve_forever = AsyncMock()
    old.listener_port = 49152
    restored_client = MagicMock()

    async def restore_old_directories(**_kwargs: object) -> int:
        old.directory_clients["directory:5222"] = restored_client
        return 1

    old.directory_pool.connect_all_with_retry = AsyncMock(side_effect=restore_old_directories)
    replacement = _generation(bot, 1)
    replacement.directory_pool.connect_all_with_retry = AsyncMock(
        side_effect=RuntimeError("connect failed")
    )
    bot.running = True
    publish_nick_change = MagicMock()
    bot._nick_change_callback = publish_nick_change
    notifier = MagicMock()
    notifier.notify_all_directories_disconnected = AsyncMock()
    notifier.notify_all_directories_reconnected = AsyncMock()

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch.object(bot, "_announce_generation_offers", new=AsyncMock()),
        patch.object(bot, "_start_generation_listeners") as start_listeners,
        patch("maker.bot.get_notifier", return_value=notifier),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is False

    assert bot.current_generation_id == 0
    assert old.state is GenerationState.ACCEPTING
    assert replacement.state is GenerationState.CLOSED
    assert bot.generations == {0: old}
    old_client.close.assert_awaited_once_with()
    old.hidden_service_listener.start.assert_awaited_once_with()
    assert old.hidden_service_listener.port == old.listener_port
    old.directory_pool.connect_all_with_retry.assert_awaited_once()
    publish_nick_change.assert_not_called()
    start_listeners.assert_called_once_with(old)
    notifier.notify_all_directories_disconnected.assert_called_once_with()
    notifier.notify_all_directories_reconnected.assert_called_once_with(1, 1)
    assert bot._all_directories_disconnected is False


@pytest.mark.asyncio
async def test_zero_directory_replacement_rolls_back_current_generation(bot: MakerBot) -> None:
    old = bot.generations[0]
    old.directory_pool.connect_all_with_retry = AsyncMock(return_value=0)
    replacement = _generation(bot, 1)
    replacement.directory_pool.connect_all_with_retry = AsyncMock(return_value=0)
    bot.running = True
    notifier = MagicMock()
    notifier.notify_all_directories_disconnected = AsyncMock()
    notifier.notify_all_directories_reconnected = AsyncMock()
    notifier.notify_directory_reconnect = AsyncMock()

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch("maker.bot.get_notifier", return_value=notifier),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is False

    assert bot.current_generation_id == 0
    assert bot.generations == {0: old}
    assert old.state is GenerationState.ACCEPTING
    assert replacement.state is GenerationState.CLOSED
    notifier.notify_all_directories_disconnected.assert_called_once_with()
    assert bot._all_directories_disconnected is True

    restored_node_id = "directory.onion:5222"
    restored_client = MagicMock()

    async def reconnect(_server: str) -> tuple[str, MagicMock]:
        bot.running = False
        return restored_node_id, restored_client

    old.directory_pool.list_disconnected = MagicMock(
        return_value=[(restored_node_id, restored_node_id)]
    )
    with (
        patch("maker.background_tasks.asyncio.sleep", new=AsyncMock()),
        patch.object(bot, "_connect_to_directory", side_effect=reconnect),
        patch.object(bot, "_listen_client", new=AsyncMock()),
        patch("maker.background_tasks.get_notifier", return_value=notifier),
        patch("maker.background_tasks.spawn_task", side_effect=lambda coroutine: coroutine.close()),
        patch("maker.bot.get_notifier", return_value=notifier),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        await bot._periodic_directory_reconnect()

    assert old.directory_clients == {restored_node_id: restored_client}
    notifier.notify_directory_reconnect.assert_called_once_with(restored_node_id, 1, 1)
    notifier.notify_all_directories_reconnected.assert_called_once_with(1, 1)
    assert bot._all_directories_disconnected is False


@pytest.mark.asyncio
async def test_rotation_failure_before_directory_teardown_does_not_open_outage(
    bot: MakerBot,
) -> None:
    old = bot.generations[0]
    old_client = MagicMock()
    old_client.close = AsyncMock()
    old.directory_clients["directory:5222"] = old_client
    replacement = _generation(bot, 1)
    bot.running = True
    notifier = MagicMock()
    notifier.notify_all_directories_disconnected = AsyncMock()

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch.object(
            bot,
            "_wait_for_generation_sessions",
            new=AsyncMock(side_effect=RuntimeError("session wait failed")),
        ),
        patch("maker.bot.get_notifier", return_value=notifier),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is False

    assert bot.current_generation_id == 0
    assert old.state is GenerationState.ACCEPTING
    assert old.directory_clients == {"directory:5222": old_client}
    old_client.close.assert_not_awaited()
    notifier.notify_all_directories_disconnected.assert_not_called()
    assert bot._all_directories_disconnected is False


@pytest.mark.asyncio
async def test_rotation_cancellation_closes_unregistered_replacement(bot: MakerBot) -> None:
    old = bot.generations[0]
    old.directory_pool.connect_all_with_retry = AsyncMock(return_value=1)
    replacement = _generation(bot, 1)
    replacement_client = MagicMock()
    replacement_client.close = AsyncMock()
    replacement.directory_clients["directory:5222"] = replacement_client
    bot.config.identity_rotation_quiet_min_sec = 60
    bot.config.identity_rotation_quiet_max_sec = 60
    bot.running = True
    real_sleep = asyncio.sleep

    async def cancel_quiet_sleep(delay: float) -> None:
        if delay == 60:
            raise asyncio.CancelledError
        await real_sleep(delay)

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch("maker.bot.asyncio.sleep", side_effect=cancel_quiet_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await bot._rotate_generation()

    assert replacement.state is GenerationState.CLOSED
    assert old.state is GenerationState.ACCEPTING
    assert bot.generations == {0: old}
    replacement_client.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_grace_retirement_starts_only_after_cutover(bot: MakerBot) -> None:
    old = bot.generations[0]
    replacement = _generation(bot, 1)
    replacement.directory_pool.connect_all_with_retry = AsyncMock(return_value=1)
    bot.config.identity_rotation_quiet_min_sec = 60
    bot.config.identity_rotation_quiet_max_sec = 60
    bot.running = True
    observed_retirement_task = False

    async def sleep(_delay: float) -> None:
        nonlocal observed_retirement_task
        observed_retirement_task = any(
            task.get_name() == "maker-generation-retire-0" for task in old.tasks
        )
        return None

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch("maker.bot.asyncio.sleep", side_effect=sleep),
        patch.object(bot, "_announce_generation_offers", new=AsyncMock()),
        patch.object(bot, "_start_generation_listeners"),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is True

    assert observed_retirement_task is False
    assert any(task.get_name() == "maker-generation-retire-0" for task in old.tasks)
    for task in old.tasks:
        task.cancel()
    await asyncio.gather(*old.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_grace_retirement_never_removes_current_generation(bot: MakerBot) -> None:
    old = bot.generations[0]
    old.state = GenerationState.GRACE
    old.grace_deadline = time.monotonic()

    with patch("maker.bot.asyncio.sleep", new=AsyncMock()):
        await bot._retire_generation_after_grace(0, old.grace_deadline)

    assert bot.generations == {0: old}
    assert old.state is GenerationState.GRACE


@pytest.mark.asyncio
async def test_old_fill_rejected_while_same_taker_can_fill_new_generation(bot: MakerBot) -> None:
    taker_nick = "J5SameTaker"
    old = bot.generations[0]
    old.state = GenerationState.GRACE
    old.grace_deadline = time.monotonic() + 60
    old_session = MagicMock()
    bot.active_sessions[(0, taker_nick)] = old_session

    replacement = _generation(bot, 1)
    offer = MagicMock()
    offer.oid = 0
    offer.ordertype.value = "sw0reloffer"
    replacement.current_offers = [offer]
    replacement.offer_manager = MagicMock()
    replacement.offer_manager.get_offer_by_id.return_value = offer
    replacement.offer_manager.validate_offer_fill.return_value = (True, "")
    bot.generations[1] = replacement
    bot._activate_generation(replacement)

    inner = MagicMock()
    inner.taker_nick = taker_nick
    inner.session_timeout_sec = 300
    inner.state = CoinJoinState.PUBKEY_SENT
    inner.validate_channel.return_value = True
    inner.handle_fill = AsyncMock(return_value=(True, {"nacl_pubkey": "abc123", "features": []}))

    with (
        patch("maker.protocol_handlers.CoinJoinSession", return_value=inner) as session_class,
        patch("maker.protocol_handlers.check_commitment", return_value=True),
        patch.object(bot, "_initialize_minimum_fee_policy", new=AsyncMock()),
        patch.object(bot, "_send_response", new=AsyncMock()) as send_response,
    ):
        await bot._handle_fill(
            taker_nick,
            f"fill 0 500000 taker_pk P{'ab' * 32}",
            generation_id=0,
        )
        session_class.assert_not_called()

        await bot._handle_fill(
            taker_nick,
            f"fill 0 500000 taker_pk P{'cd' * 32}",
            generation_id=1,
        )

    assert bot.active_sessions[(0, taker_nick)] is old_session
    assert isinstance(bot.active_sessions[(1, taker_nick)], MakerSession)
    send_response.assert_awaited_once()
    assert send_response.await_args is not None
    assert send_response.await_args.kwargs["generation_id"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["_handle_auth", "_handle_tx"])
async def test_old_continuation_dispatches_only_to_old_session(
    bot: MakerBot, handler_name: str
) -> None:
    taker_nick = "J5PinnedTaker"
    old = bot.generations[0]
    old.state = GenerationState.GRACE
    old.grace_deadline = time.monotonic() + 60
    old_session = MagicMock()
    bot.active_sessions[(0, taker_nick)] = old_session
    replacement = _generation(bot, 1)
    bot.generations[1] = replacement
    bot._activate_generation(replacement)

    dispatch = AsyncMock()
    with patch.object(bot, "_dispatch_session_handler", new=dispatch):
        await getattr(bot, handler_name)(taker_nick, "command payload", generation_id=1)
        dispatch.assert_not_awaited()

        await getattr(bot, handler_name)(taker_nick, "command payload", generation_id=0)

    assert dispatch.await_count == 1
    assert dispatch.await_args is not None
    assert dispatch.await_args.args[0] is old_session


@pytest.mark.asyncio
async def test_old_session_response_uses_only_old_clients(bot: MakerBot) -> None:
    taker_nick = "J5PinnedResponse"
    old = bot.generations[0]
    old.state = GenerationState.GRACE
    old.grace_deadline = time.monotonic() + 60
    old_client = MagicMock()
    old_client.send_private_message = AsyncMock()
    old.directory_clients["old:5222"] = old_client

    replacement = _generation(bot, 1)
    new_client = MagicMock()
    new_client.send_private_message = AsyncMock()
    replacement.directory_clients["new:5222"] = new_client
    bot.generations[1] = replacement
    bot._activate_generation(replacement)

    inner = MagicMock()
    inner.taker_nick = taker_nick
    inner.session_timeout_sec = 300
    inner.state = CoinJoinState.AUTH_RECEIVED
    inner.crypto.encrypt.return_value = "ciphertext"
    session = MakerSession(inner, generation_id=0)
    bot.active_sessions[(0, taker_nick)] = session

    sent = await session.send_response(
        bot,
        "ioauth",
        {
            "utxo_list": "ab:0",
            "auth_pub": "pubkey",
            "cj_addr": "bcrt1qcj",
            "change_addr": "bcrt1qchange",
            "btc_sig": "signature",
        },
    )

    assert sent is True
    old_client.send_private_message.assert_awaited_once()
    new_client.send_private_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_push_is_generation_pinned_during_grace(bot: MakerBot) -> None:
    taker_nick = "J5PinnedPush"
    old = bot.generations[0]
    old.state = GenerationState.GRACE
    old.grace_deadline = time.monotonic() + 60
    replacement = _generation(bot, 1)
    bot.generations[1] = replacement
    bot._activate_generation(replacement)

    tx_bytes = bytes.fromhex("01000000000000000000")
    txid = get_txid(tx_bytes.hex())
    key = (0, taker_nick, txid)
    bot._pending_signed_rounds[key] = PendingSignedRound(
        generation_id=0,
        taker_nick=taker_nick,
        txid=txid,
        input_lock_owner="round-owner",
        outpoints=frozenset({("ab" * 32, 0)}),
        expires_at=time.monotonic() + 60,
        lock_ttl_sec=3600,
    )
    bot.wallet.renew_coinjoin_inputs.return_value = True
    bot.backend.broadcast_transaction = AsyncMock(return_value=txid)
    message = f"push {base64.b64encode(tx_bytes).decode('ascii')}"

    await bot._handle_push(taker_nick, message, generation_id=1)
    assert key in bot._pending_signed_rounds
    bot.backend.broadcast_transaction.assert_not_awaited()

    await bot._handle_push(taker_nick, message, generation_id=0)
    assert key not in bot._pending_signed_rounds
    bot.backend.broadcast_transaction.assert_awaited_once_with(tx_bytes.hex())


def test_dynamic_pool_advertises_virtual_onion_port(bot: MakerBot) -> None:
    generation = _generation(bot, 1)
    generation.listener_port = 49152
    kwargs = generation.directory_pool._build_client_kwargs("directory.onion", 5222)

    assert kwargs["location"] == f"generation-1.onion:{bot.config.onion_serving_port}"
    assert str(generation.listener_port) not in kwargs["location"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement_connects", [True, False])
async def test_rotation_with_open_direct_socket_keeps_serving(
    bot: MakerBot, replacement_connects: bool
) -> None:
    """Real sockets must survive listener retirement without blocking cutover or rollback."""
    bot.running = True
    bot.config.tor_control.enabled = True
    old = bot.generations[0]
    old.onion_host = "generation-0.onion"
    handler_tasks: list[asyncio.Task[None]] = []
    on_direct_connection = bot._on_direct_connection

    async def handle(
        connection: TCPConnection, peer: str, generation_id: int | None = None
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        handler_tasks.append(task)
        await on_direct_connection(connection, peer, generation_id=generation_id)

    async def handshake(connection: TCPConnection) -> dict[str, object]:
        await connection.send(
            json.dumps(
                {
                    "type": MessageType.HANDSHAKE.value,
                    "line": json.dumps({"nick": "J5Peer", "network": "regtest"}),
                }
            ).encode()
        )
        response = await asyncio.wait_for(connection.receive(), timeout=2)
        return json.loads(json.loads(response)["line"])

    old.hidden_service_listener = HiddenServiceListener(
        host="127.0.0.1",
        port=0,
        on_connection=lambda connection, peer: handle(connection, peer, generation_id=0),
    )
    old.listener_port = await old.hidden_service_listener.start()
    bot._start_generation_listeners(old)
    tor = AsyncMock()
    tor.create_ephemeral_hidden_service.return_value = MagicMock(
        onion_address="generation-1.onion", service_id="generation-1"
    )
    connections: list[TCPConnection] = []
    replacement: MakerGeneration | None = None
    try:
        existing = await connect_direct("127.0.0.1", old.listener_port)
        connections.append(existing)
        assert (await handshake(existing))["nick"] == old.nick_identity.nick
        with (
            patch.object(bot, "_on_direct_connection", side_effect=handle),
            patch("maker.bot.TorControlClient", return_value=tor),
            patch.object(OfferManager, "create_offers", new=AsyncMock(return_value=[])),
        ):
            replacement = await bot._create_replacement_generation()
            assert replacement is not None
            assert replacement.listener_port is not None
            assert replacement.listener_port != old.listener_port
            tor.create_ephemeral_hidden_service.assert_awaited_once()
            assert tor.create_ephemeral_hidden_service.await_args.kwargs["ports"] == [
                (bot.config.onion_serving_port, f"127.0.0.1:{replacement.listener_port}")
            ]
            replacement.directory_pool.connect_all_with_retry = AsyncMock(
                return_value=int(replacement_connects)
            )
            old.directory_pool.connect_all_with_retry = AsyncMock(return_value=1)
            with (
                patch.object(
                    bot,
                    "_create_replacement_generation",
                    new=AsyncMock(return_value=replacement),
                ),
                patch.object(bot, "_announce_generation_offers", new=AsyncMock()),
                patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
            ):
                assert await asyncio.wait_for(bot._rotate_generation(), timeout=2) is (
                    replacement_connects
                )

            current = replacement if replacement_connects else old
            assert bot.current_generation_id == current.generation_id
            # The existing socket still belongs to the old identity during grace.
            assert (await handshake(existing))["nick"] == old.nick_identity.nick
            assert current.listener_port is not None
            fresh = await connect_direct("127.0.0.1", current.listener_port)
            connections.append(fresh)
            response = await handshake(fresh)
            assert response["nick"] == current.nick_identity.nick
            assert response["location-string"] == (
                f"{current.onion_host}:{bot.config.onion_serving_port}"
            )
            if replacement_connects:
                with pytest.raises(OSError):
                    await asyncio.open_connection("127.0.0.1", old.listener_port)
    finally:
        for connection in connections:
            await connection.close()
        await bot.stop()
        if replacement is not None:
            await bot._close_generation(replacement)
        await asyncio.wait_for(asyncio.gather(*handler_tasks), timeout=2)


@pytest.mark.asyncio
async def test_direct_handshake_advertises_virtual_onion_port(bot: MakerBot) -> None:
    generation = _generation(bot, 1)
    generation.onion_host = "generation-1.onion"
    generation.listener_port = 49152
    bot.generations[1] = generation
    bot._activate_generation(generation)
    connection = MagicMock()
    connection.send = AsyncMock()
    handshake = {
        "type": MessageType.HANDSHAKE.value,
        "line": json.dumps({"nick": "J5Peer", "network": "regtest"}),
    }

    handled = await bot._try_handle_handshake(
        connection, json.dumps(handshake).encode(), "peer", generation_id=1
    )

    assert handled is True
    response = json.loads(connection.send.await_args.args[0].decode())
    response_data = json.loads(response["line"])
    assert response_data["location-string"] == (
        f"generation-1.onion:{bot.config.onion_serving_port}"
    )
    assert str(generation.listener_port) not in response_data["location-string"]


@pytest.mark.asyncio
async def test_grace_cleanup_closes_only_old_generation(bot: MakerBot) -> None:
    old = bot.generations[0]
    current = _generation(bot, 1)
    old.state = GenerationState.GRACE
    old.grace_deadline = time.monotonic()
    old_client = MagicMock()
    old_client.close = AsyncMock()
    old.directory_clients["old:5222"] = old_client
    current_client = MagicMock()
    current_client.close = AsyncMock()
    current.directory_clients["new:5222"] = current_client
    bot.generations[1] = current
    bot._activate_generation(current)

    with patch("maker.bot.asyncio.sleep", new=AsyncMock()):
        await bot._retire_generation_after_grace(0, old.grace_deadline)

    assert 0 not in bot.generations
    assert bot.generations[1] is current
    old_client.close.assert_awaited_once_with()
    current_client.close.assert_not_awaited()

    dispatch = AsyncMock()
    with patch.object(bot, "_dispatch_session_handler", new=dispatch):
        await bot._handle_auth("J5Expired", "auth payload", generation_id=0)
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("retired_state", [GenerationState.GRACE, GenerationState.CLOSED])
async def test_retired_listener_is_silent_and_cannot_remove_current_client(
    bot: MakerBot, retired_state: GenerationState
) -> None:
    node_id = "directory.onion:5222"
    old = bot.generations[0]
    old.state = retired_state
    old_client = MagicMock()
    old_client.listen_for_messages = AsyncMock(side_effect=DirectoryClientError("lost"))
    old_client.close = AsyncMock()
    old.directory_clients[node_id] = old_client

    replacement = _generation(bot, 1)
    new_client = MagicMock()
    replacement.directory_clients[node_id] = new_client
    bot.generations[1] = replacement
    bot._activate_generation(replacement)
    bot.running = True
    notifier = MagicMock()
    notifier.notify_directory_disconnect = AsyncMock()
    notifier.notify_all_directories_disconnected = AsyncMock()

    with (
        patch("maker.background_tasks.get_notifier", return_value=notifier),
        patch("maker.background_tasks.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        await bot._listen_client(node_id, old_client, generation_id=0)

    assert bot.directory_clients[node_id] is new_client
    old_client.close.assert_awaited_once_with()
    assert bot._all_directories_disconnected is False
    notifier.notify_directory_disconnect.assert_not_called()
    notifier.notify_all_directories_disconnected.assert_not_called()


@pytest.mark.asyncio
async def test_current_accepting_listener_opens_directory_outage_once(bot: MakerBot) -> None:
    node_id = "directory.onion:5222"
    bot.running = True
    notifier = MagicMock()
    notifier.notify_directory_disconnect = AsyncMock()
    notifier.notify_all_directories_disconnected = AsyncMock()

    for _ in range(2):
        client = MagicMock()
        client.listen_for_messages = AsyncMock(side_effect=DirectoryClientError("lost"))
        client.close = AsyncMock()
        bot.directory_clients[node_id] = client
        with (
            patch("maker.background_tasks.get_notifier", return_value=notifier),
            patch(
                "maker.background_tasks.spawn_task", side_effect=lambda coroutine: coroutine.close()
            ),
            patch("maker.bot.get_notifier", return_value=notifier),
            patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
        ):
            await bot._listen_client(node_id, client)

    assert bot._all_directories_disconnected is True
    assert notifier.notify_directory_disconnect.call_count == 2
    notifier.notify_all_directories_disconnected.assert_called_once_with()


@pytest.mark.asyncio
async def test_concurrent_listener_failures_open_one_directory_outage(bot: MakerBot) -> None:
    node_ids = [f"directory-{index}.onion:5222" for index in range(6)]
    bot.config.directory_servers = node_ids
    bot.running = True
    notifier = MagicMock()
    notifier.notify_directory_disconnect = AsyncMock()
    notifier.notify_all_directories_disconnected = AsyncMock()
    all_closing = asyncio.Event()
    close_count = 0

    async def synchronized_close() -> None:
        nonlocal close_count
        close_count += 1
        if close_count == len(node_ids):
            all_closing.set()
        await all_closing.wait()

    clients = []
    for node_id in node_ids:
        client = MagicMock()
        client.listen_for_messages = AsyncMock(side_effect=DirectoryClientError("lost"))
        client.close = AsyncMock(side_effect=synchronized_close)
        bot.directory_clients[node_id] = client
        clients.append(client)

    with (
        patch("maker.background_tasks.get_notifier", return_value=notifier),
        patch("maker.background_tasks.spawn_task", side_effect=lambda coroutine: coroutine.close()),
        patch("maker.bot.get_notifier", return_value=notifier),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        await asyncio.gather(
            *(bot._listen_client(node_id, client) for node_id, client in zip(node_ids, clients))
        )

    assert bot.directory_clients == {}
    assert close_count == len(node_ids)
    assert bot._all_directories_disconnected is True
    assert notifier.notify_directory_disconnect.call_count == len(node_ids)
    notifier.notify_all_directories_disconnected.assert_called_once_with()


@pytest.mark.asyncio
async def test_cutover_resolves_existing_process_directory_outage_once(bot: MakerBot) -> None:
    old = bot.generations[0]
    replacement = _generation(bot, 1)
    replacement_client = MagicMock()
    replacement.directory_clients["directory.onion:5222"] = replacement_client
    bot.running = True
    bot._all_directories_disconnected = True
    observed_outage_during_connect = False

    async def connect_replacement(**_kwargs: object) -> int:
        nonlocal observed_outage_during_connect
        observed_outage_during_connect = bot._all_directories_disconnected
        return 1

    replacement.directory_pool.connect_all_with_retry = AsyncMock(side_effect=connect_replacement)
    notifier = MagicMock()
    notifier.notify_all_directories_reconnected = AsyncMock()
    notifier.notify_nick_change = AsyncMock()

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch.object(bot, "_announce_generation_offers", new=AsyncMock()),
        patch.object(bot, "_start_generation_listeners"),
        patch("maker.bot.get_notifier", return_value=notifier),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is True

    assert old.state is GenerationState.GRACE
    assert observed_outage_during_connect is True
    assert bot._all_directories_disconnected is False
    notifier.notify_all_directories_reconnected.assert_called_once_with(1, 1)

    for task in old.tasks:
        task.cancel()
    await asyncio.gather(*old.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_normal_cutover_does_not_resolve_directory_outage(bot: MakerBot) -> None:
    replacement = _generation(bot, 1)
    replacement_client = MagicMock()
    replacement.directory_clients["directory.onion:5222"] = replacement_client
    replacement.directory_pool.connect_all_with_retry = AsyncMock(return_value=1)
    bot.running = True
    notifier = MagicMock()
    notifier.notify_all_directories_disconnected = AsyncMock()
    notifier.notify_all_directories_reconnected = AsyncMock()
    notifier.notify_nick_change = AsyncMock()

    with (
        patch.object(
            bot, "_create_replacement_generation", new=AsyncMock(return_value=replacement)
        ),
        patch.object(bot, "_announce_generation_offers", new=AsyncMock()),
        patch.object(bot, "_start_generation_listeners"),
        patch("maker.bot.get_notifier", return_value=notifier),
        patch("maker.bot.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        assert await bot._rotate_generation() is True

    assert bot._all_directories_disconnected is False
    notifier.notify_all_directories_disconnected.assert_not_called()
    notifier.notify_all_directories_reconnected.assert_not_called()

    old = bot.generations[0]
    for task in old.tasks:
        task.cancel()
    await asyncio.gather(*old.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_reconnect_skips_grace_generation(bot: MakerBot) -> None:
    generation = bot.generations[0]
    generation.state = GenerationState.GRACE
    generation.directory_pool.list_disconnected = MagicMock(
        return_value=[("directory.onion:5222", "directory.onion:5222")]
    )
    client = MagicMock()
    client.close = AsyncMock()
    client.send_public_message = AsyncMock()
    bot.current_offers = [MagicMock()]
    bot.running = True
    sleep_calls = 0

    async def sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            bot.running = False

    with (
        patch("maker.background_tasks.asyncio.sleep", side_effect=sleep),
        patch.object(
            bot,
            "_connect_to_directory",
            new=AsyncMock(return_value=("directory.onion:5222", client)),
        ) as connect,
        patch.object(bot, "_format_offer_announcement", return_value="offer") as format_offer,
    ):
        await bot._periodic_directory_reconnect()

    connect.assert_not_awaited()
    format_offer.assert_not_called()
    client.send_public_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_closes_client_when_generation_enters_grace_during_dial(
    bot: MakerBot,
) -> None:
    generation = bot.generations[0]
    generation.directory_pool.list_disconnected = MagicMock(
        return_value=[("directory.onion:5222", "directory.onion:5222")]
    )
    client = MagicMock()
    client.close = AsyncMock()
    client.send_public_message = AsyncMock()
    bot.current_offers = [MagicMock()]
    bot.running = True

    async def connect(_server: str) -> tuple[str, MagicMock]:
        generation.state = GenerationState.GRACE
        bot.running = False
        return "directory.onion:5222", client

    with (
        patch("maker.background_tasks.asyncio.sleep", new=AsyncMock()),
        patch.object(bot, "_connect_to_directory", side_effect=connect),
        patch.object(bot, "_format_offer_announcement", return_value="offer") as format_offer,
    ):
        await bot._periodic_directory_reconnect()

    assert generation.directory_clients == {}
    client.close.assert_awaited_once_with()
    client.send_public_message.assert_not_awaited()
    format_offer.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_result_is_discarded_after_cutover(bot: MakerBot) -> None:
    old = bot.generations[0]
    old.directory_pool.list_disconnected = MagicMock(
        return_value=[("directory.onion:5222", "directory.onion:5222")]
    )
    replacement = _generation(bot, 1)
    stale_client = MagicMock()
    stale_client.close = AsyncMock()
    bot.running = True

    async def connect(_server: str):
        bot.generations[1] = replacement
        bot._activate_generation(replacement)
        bot.running = False
        return "directory.onion:5222", stale_client

    with (
        patch("maker.background_tasks.asyncio.sleep", new=AsyncMock()),
        patch.object(bot, "_connect_to_directory", side_effect=connect),
    ):
        await bot._periodic_directory_reconnect()

    assert replacement.directory_clients == {}
    stale_client.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_closes_every_generation(bot: MakerBot) -> None:
    old = bot.generations[0]
    old.state = GenerationState.GRACE
    old_client = MagicMock()
    old_client.close = AsyncMock()
    old.directory_clients["old:5222"] = old_client
    replacement = _generation(bot, 1)
    new_client = MagicMock()
    new_client.close = AsyncMock()
    replacement.directory_clients["new:5222"] = new_client
    bot.generations[1] = replacement
    bot._activate_generation(replacement)

    await bot.stop()

    assert bot.generations == {}
    old_client.close.assert_awaited_once_with()
    new_client.close.assert_awaited_once_with()
