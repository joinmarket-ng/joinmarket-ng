"""Offer refresh exposure, adoption, and public delivery guarantees.

These cover the ordering contract of an offer update: freshly built offers stay
invisible to every peer until the privacy delay elapses and the identity that
built them is still serving, and the public messages that carry an adopted
update survive a directory failure without being rebuilt.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.models import NetworkType, Offer, OfferType

from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.generation import GenerationState

pytestmark = pytest.mark.asyncio

OLD_MAXSIZE = 262_144
NEW_MAXSIZE = 524_288
BASELINE_BALANCE = 400_000
CHANGED_BALANCE = 600_000


@pytest.fixture
def mock_wallet() -> MagicMock:
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    wallet.sync_all = AsyncMock()
    wallet.reconstruct_imported_state_safe = AsyncMock()
    wallet.get_total_balance = AsyncMock(return_value=1_000_000)
    wallet.get_balance_for_offers = AsyncMock(return_value=CHANGED_BALANCE)
    wallet.get_locked_input_outpoints = MagicMock(return_value=set())
    wallet.get_maker_rotation_lineage_outpoints = AsyncMock(return_value=set())
    return wallet


@pytest.fixture
def mock_backend() -> MagicMock:
    backend = MagicMock()
    backend.can_provide_neutrino_metadata = MagicMock(return_value=True)
    backend.get_block_height = AsyncMock(return_value=930_000)
    return backend


def _offer(bot: MakerBot, oid: int, maxsize: int) -> Offer:
    return Offer(
        counterparty=bot.nick,
        oid=oid,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=100_000,
        maxsize=maxsize,
        txfee=1000,
        cjfee="0.001",
    )


def _make_bot(mock_wallet: MagicMock, mock_backend: MagicMock, delay_max: int) -> MakerBot:
    config = MakerConfig(
        mnemonic="test " * 12,
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
        rescan_interval_sec=60,
        offer_reannounce_delay_max=delay_max,
    )
    bot = MakerBot(wallet=mock_wallet, backend=mock_backend, config=config)
    bot.current_offers = [_offer(bot, 0, OLD_MAXSIZE)]
    bot.generations[0].current_offers = bot.current_offers
    bot.offer_manager.offer_balance = BASELINE_BALANCE
    return bot


def _recording_client(failures: int = 0) -> MagicMock:
    """Directory client recording public messages, failing the first N sends."""
    client = MagicMock()
    sent: list[str] = []
    client.sent = sent
    remaining = [failures]

    async def send_public_message(message: str) -> None:
        if remaining[0] > 0:
            remaining[0] -= 1
            raise ConnectionError("directory write failed")
        client.sent.append(message)

    client.send_public_message = AsyncMock(side_effect=send_public_message)
    return client


class _DelayGate:
    """Blocks the privacy delay so the pre-adoption window can be inspected."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def sleep(self, _delay: float) -> None:
        self.entered.set()
        await self.release.wait()


async def test_private_and_direct_answers_serve_old_terms_until_delay_expires(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """A blocked privacy delay must not leak the pending update to any peer."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=30)
    directory = _recording_client()
    bot.directory_clients = {"dir": directory}
    bot.generations[0].directory_clients = bot.directory_clients
    directory.send_private_message = AsyncMock()
    bot.offer_manager.create_offers = AsyncMock(return_value=[_offer(bot, 0, NEW_MAXSIZE)])
    connection = MagicMock()
    connection.send = AsyncMock()
    gate = _DelayGate()

    with (
        patch("maker.bot.secure_random.uniform", return_value=30.0),
        patch("maker.bot.asyncio.sleep", new=gate.sleep),
    ):
        refresh = asyncio.create_task(bot._update_offers())
        await asyncio.wait_for(gate.entered.wait(), timeout=5)

        await bot._send_offers_to_taker("J5Taker")
        await bot._send_offers_via_direct_connection("J5Taker", connection)

        privmsg_data = directory.send_private_message.await_args.args[2]
        direct_line = json.loads(connection.send.await_args.args[0].decode())["line"]
        assert f" {OLD_MAXSIZE} " in privmsg_data
        assert str(NEW_MAXSIZE) not in privmsg_data
        assert f" {OLD_MAXSIZE} " in direct_line
        assert str(NEW_MAXSIZE) not in direct_line
        assert bot.current_offers[0].maxsize == OLD_MAXSIZE
        assert directory.sent == []

        gate.release.set()
        await asyncio.wait_for(refresh, timeout=5)

    assert bot.current_offers[0].maxsize == NEW_MAXSIZE
    assert directory.sent == [f"sw0reloffer 0 100000 {NEW_MAXSIZE} 1000 0.001"]


async def test_refresh_cancelled_in_delay_keeps_exposed_offers_and_retries_later(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """A cancelled refresh must not look like a completed one to the next rescan."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=30)
    directory = _recording_client()
    bot.directory_clients = {"dir": directory}
    bot.generations[0].directory_clients = bot.directory_clients

    async def create_offers() -> list[Offer]:
        bot.offer_manager.offer_balance = CHANGED_BALANCE
        return [_offer(bot, 0, NEW_MAXSIZE)]

    bot.offer_manager.create_offers = AsyncMock(side_effect=create_offers)
    gate = _DelayGate()

    with (
        patch("maker.bot.secure_random.uniform", return_value=30.0),
        patch("maker.bot.asyncio.sleep", new=gate.sleep),
    ):
        refresh = asyncio.create_task(bot._update_offers())
        await asyncio.wait_for(gate.entered.wait(), timeout=5)
        refresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await refresh

    assert bot.current_offers[0].maxsize == OLD_MAXSIZE
    assert directory.sent == []
    # The balance the exposed offers were built from, so the next rescan sees a
    # difference and retries instead of trusting the abandoned refresh.
    assert bot.offer_manager.offer_balance == BASELINE_BALANCE


async def test_refresh_is_discarded_when_identity_stops_serving_during_delay(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """An identity that entered its cutover grace must not publish new offers."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=30)
    directory = _recording_client()
    bot.directory_clients = {"dir": directory}
    bot.generations[0].directory_clients = bot.directory_clients
    bot.offer_manager.create_offers = AsyncMock(return_value=[_offer(bot, 0, NEW_MAXSIZE)])
    gate = _DelayGate()

    with (
        patch("maker.bot.secure_random.uniform", return_value=30.0),
        patch("maker.bot.asyncio.sleep", new=gate.sleep),
    ):
        refresh = asyncio.create_task(bot._update_offers())
        await asyncio.wait_for(gate.entered.wait(), timeout=5)
        bot.generations[0].state = GenerationState.GRACE
        gate.release.set()
        await asyncio.wait_for(refresh, timeout=5)

    assert bot.current_offers[0].maxsize == OLD_MAXSIZE
    assert directory.sent == []
    assert bot.offer_manager.offer_balance == BASELINE_BALANCE


async def test_failed_announcement_is_retried_verbatim_on_the_next_rescan(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """A directory that refused an announcement gets the identical payload later."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=0)
    healthy = _recording_client()
    failing = _recording_client(failures=1)
    bot.directory_clients = {"healthy": healthy, "failing": failing}
    bot.generations[0].directory_clients = bot.directory_clients

    async def create_offers() -> list[Offer]:
        # Mirrors the real OfferManager, which records the balance its result
        # was built from before the bot decides whether to adopt it.
        bot.offer_manager.offer_balance = CHANGED_BALANCE
        return [_offer(bot, 0, NEW_MAXSIZE)]

    bot.offer_manager.create_offers = AsyncMock(side_effect=create_offers)

    await bot._resync_wallet_and_update_offers()

    announcement = f"sw0reloffer 0 100000 {NEW_MAXSIZE} 1000 0.001"
    assert healthy.sent == [announcement]
    assert failing.sent == []

    # Next rescan of the very same wallet state: no rebuild happens because the
    # adopted offers already describe this balance, and the undelivered payload
    # is retried byte for byte without spamming the healthy directory.
    bot.offer_manager.create_offers.reset_mock()

    await bot._resync_wallet_and_update_offers()

    bot.offer_manager.create_offers.assert_not_awaited()
    assert failing.sent == [announcement]
    assert healthy.sent == [announcement]
    assert bot.generations[0].offer_delivery.pending == {}


async def test_retries_are_not_starved_when_a_rescan_rebuilds_identical_offers(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """Balance churn that yields the same offers must still drain the backlog."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=0)
    failing = _recording_client(failures=1)
    bot.directory_clients = {"failing": failing}
    bot.generations[0].directory_clients = bot.directory_clients
    bot.offer_manager.create_offers = AsyncMock(return_value=[_offer(bot, 0, NEW_MAXSIZE)])

    await bot._update_offers()
    announcement = f"sw0reloffer 0 100000 {NEW_MAXSIZE} 1000 0.001"
    assert failing.sent == []

    # The balance moved again, but the rebuild produces byte-identical offers.
    bot.offer_manager.offer_balance = BASELINE_BALANCE
    await bot._resync_wallet_and_update_offers()

    assert failing.sent == [announcement]
    assert bot.generations[0].offer_delivery.pending == {}


async def test_offer_that_disappears_is_withdrawn_even_on_a_disconnected_directory(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """A queued announcement must not resurrect an offer that no longer exists."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=0)
    bot.current_offers = [_offer(bot, 0, OLD_MAXSIZE), _offer(bot, 1, OLD_MAXSIZE)]
    bot.generations[0].current_offers = bot.current_offers
    down = _recording_client(failures=2)
    bot.directory_clients = {"down": down}
    bot.generations[0].directory_clients = bot.directory_clients
    bot.offer_manager.create_offers = AsyncMock(
        return_value=[_offer(bot, 0, NEW_MAXSIZE), _offer(bot, 1, NEW_MAXSIZE)]
    )

    await bot._update_offers()
    assert set(bot.generations[0].offer_delivery.pending["down"]) == {0, 1}

    # The directory is now disconnected while oid 1 disappears from the offers.
    bot.directory_clients.clear()
    bot.offer_manager.create_offers = AsyncMock(return_value=[_offer(bot, 0, NEW_MAXSIZE)])
    await bot._update_offers()

    assert bot.generations[0].offer_delivery.pending["down"][1] is None

    reconnected = _recording_client()
    bot.directory_clients["down"] = reconnected
    await bot._flush_offer_delivery()

    assert reconnected.sent == ["cancel 1", f"sw0reloffer 0 100000 {NEW_MAXSIZE} 1000 0.001"]


async def test_flush_sends_the_superseding_payload_when_a_send_blocks(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """A message superseded while an earlier send blocks must never be published."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=0)
    delivery = bot.generations[0].offer_delivery
    stale = "sw0reloffer 0 100000 262144 1000 0.001"
    fresh = "sw0reloffer 0 100000 999999 1000 0.001"
    sent: list[str] = []
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def send_public_message(message: str) -> None:
        sent.append(message)
        if len(sent) == 1:
            blocked.set()
            await release.wait()

    client = MagicMock()
    client.send_public_message = AsyncMock(side_effect=send_public_message)
    delivery.queue(["dir"], {1: None, 0: stale})
    clients = {"dir": client}

    flush = asyncio.create_task(delivery.flush(clients))
    await asyncio.wait_for(blocked.wait(), timeout=5)
    # A refresh supersedes oid 0 while the withdrawal of oid 1 is still in flight.
    delivery.queue(["dir"], {0: fresh})
    release.set()
    await asyncio.wait_for(flush, timeout=5)

    assert sent == ["cancel 1", fresh]
    assert delivery.pending == {}


async def test_superseded_updates_coalesce_per_offer_id_for_a_failing_directory(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """Repeated failures must not grow a queue; only the latest terms are owed."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=0)
    failing = _recording_client(failures=2)
    bot.directory_clients = {"failing": failing}
    bot.generations[0].directory_clients = bot.directory_clients

    bot.offer_manager.create_offers = AsyncMock(return_value=[_offer(bot, 0, NEW_MAXSIZE)])
    await bot._update_offers()
    bot.offer_manager.create_offers = AsyncMock(return_value=[_offer(bot, 0, 1_048_576)])
    await bot._update_offers()

    assert bot.generations[0].offer_delivery.pending["failing"] == {
        0: "sw0reloffer 0 100000 1048576 1000 0.001"
    }

    await bot._flush_offer_delivery()

    assert failing.sent == ["sw0reloffer 0 100000 1048576 1000 0.001"]
    assert bot.generations[0].offer_delivery.pending == {}


async def test_failed_cancellation_is_retried_per_directory_before_announcements(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """A withdrawal that one directory refused blocks only that directory's update."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=0)
    bot.current_offers = [_offer(bot, 0, OLD_MAXSIZE), _offer(bot, 1, OLD_MAXSIZE)]
    bot.generations[0].current_offers = bot.current_offers
    healthy = _recording_client()
    # Refuses the withdrawal, which is the first message of the publication.
    failing = _recording_client(failures=1)
    bot.directory_clients = {"healthy": healthy, "failing": failing}
    bot.generations[0].directory_clients = bot.directory_clients
    bot.offer_manager.create_offers = AsyncMock(return_value=[_offer(bot, 0, NEW_MAXSIZE)])

    await bot._update_offers()

    announcement = f"sw0reloffer 0 100000 {NEW_MAXSIZE} 1000 0.001"
    assert healthy.sent == ["cancel 1", announcement]
    # The announcement must not overtake the cancellation this directory still owes.
    assert failing.sent == []
    assert bot.generations[0].offer_delivery.pending == {"failing": {1: None, 0: announcement}}

    await bot._flush_offer_delivery()

    assert failing.sent == ["cancel 1", announcement]
    assert healthy.sent == ["cancel 1", announcement]


async def test_cancelled_delivery_retains_complete_adopted_update(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """Cancellation during withdrawal must not lose the replacement announcement."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=0)
    bot.current_offers.append(_offer(bot, 1, OLD_MAXSIZE))
    directory = _recording_client()
    bot.directory_clients["dir"] = directory
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_send(message: str) -> None:
        entered.set()
        await release.wait()

    directory.send_public_message.side_effect = blocked_send

    async def create_offers() -> list[Offer]:
        bot.offer_manager.offer_balance = CHANGED_BALANCE
        return [_offer(bot, 0, NEW_MAXSIZE)]

    bot.offer_manager.create_offers = AsyncMock(side_effect=create_offers)
    update = asyncio.create_task(bot._resync_wallet_and_update_offers())
    await asyncio.wait_for(entered.wait(), timeout=5)
    update.cancel()
    with pytest.raises(asyncio.CancelledError):
        await update

    assert bot.offer_manager.offer_balance == CHANGED_BALANCE
    announcement = f"sw0reloffer 0 100000 {NEW_MAXSIZE} 1000 0.001"
    assert bot.generations[0].offer_delivery.pending == {"dir": {1: None, 0: announcement}}
    directory.send_public_message.side_effect = None
    directory.send_public_message.reset_mock()
    await bot._resync_wallet_and_update_offers()
    assert [call.args[0] for call in directory.send_public_message.await_args_list] == [
        "cancel 1",
        announcement,
    ]
    bot.offer_manager.create_offers.assert_awaited_once()
    assert bot.generations[0].offer_delivery.pending == {}


async def test_reconnected_directory_receives_cancels_before_current_offers(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """Reconnect delivers the exposed offers without overtaking a pending cancel."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=0)
    bot.running = True
    node_id = "localhost:5222"
    reconnected = _recording_client()
    bot.directory_clients = {}
    bot.generations[0].directory_clients = bot.directory_clients
    bot.generations[0].offer_delivery.queue([node_id], {1: None})
    bot._connect_to_directory = AsyncMock(return_value=(node_id, reconnected))

    try:
        with (
            patch("maker.background_tasks.get_notifier") as notifier,
            patch("maker.background_tasks.spawn_task", side_effect=lambda coro: coro.close()),
            patch("asyncio.sleep", side_effect=[0, 0, asyncio.CancelledError()]),
        ):
            notifier.return_value = MagicMock()
            await bot._periodic_directory_reconnect()
    finally:
        bot.running = False
        for task in bot.listen_tasks:
            task.cancel()
        await asyncio.gather(*bot.listen_tasks, return_exceptions=True)

    assert reconnected.sent == ["cancel 1", f"sw0reloffer 0 100000 {OLD_MAXSIZE} 1000 0.001"]
    assert bot.generations[0].offer_delivery.pending == {}


async def test_overlapping_rescans_do_not_rebuild_unchanged_offers(
    mock_wallet: MagicMock, mock_backend: MagicMock
) -> None:
    """A rescan queued behind an in-flight refresh must not re-randomize."""
    bot = _make_bot(mock_wallet, mock_backend, delay_max=30)
    directory = _recording_client()
    bot.directory_clients = {"dir": directory}
    bot.generations[0].directory_clients = bot.directory_clients
    building = asyncio.Event()
    finish_building = asyncio.Event()

    async def create_offers() -> list[Offer]:
        building.set()
        await finish_building.wait()
        # The real OfferManager records the balance its result was built from.
        bot.offer_manager.offer_balance = CHANGED_BALANCE
        return [_offer(bot, 0, NEW_MAXSIZE)]

    bot.offer_manager.create_offers = AsyncMock(side_effect=create_offers)
    gate = _DelayGate()

    with (
        patch("maker.bot.secure_random.uniform", return_value=30.0),
        patch("maker.bot.asyncio.sleep", new=gate.sleep),
    ):
        first = asyncio.create_task(bot._resync_wallet_and_update_offers())
        await asyncio.wait_for(building.wait(), timeout=5)
        # The second rescan observes the same stale balance and blocks on the
        # refresh lock until the first refresh has adopted its offers.
        second = asyncio.create_task(bot._resync_wallet_and_update_offers())
        finish_building.set()
        await asyncio.wait_for(gate.entered.wait(), timeout=5)
        # Still parked on the refresh lock while the first refresh is in its
        # privacy delay, so the overlap is real and not just interleaved.
        assert not second.done()
        gate.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=5)

    bot.offer_manager.create_offers.assert_awaited_once()
    assert directory.sent == [f"sw0reloffer 0 100000 {NEW_MAXSIZE} 1000 0.001"]
