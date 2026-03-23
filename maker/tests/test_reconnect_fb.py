import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.models import NetworkType, Offer, OfferType

from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.fidelity import FidelityBondInfo


@pytest.fixture
def mock_wallet():
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    return wallet


@pytest.fixture
def mock_backend():
    return MagicMock()


@pytest.fixture
def config():
    return MakerConfig(
        mnemonic="test " * 12,
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
    )


@pytest.fixture
def maker_bot(mock_wallet, mock_backend, config):
    bot = MakerBot(
        wallet=mock_wallet,
        backend=mock_backend,
        config=config,
    )
    bot.running = True
    return bot


@pytest.fixture
def sample_offer(maker_bot):
    return Offer(
        counterparty=maker_bot.nick,
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=100_000,
        maxsize=10_000_000,
        txfee=1000,
        cjfee="0.0003",
        fidelity_bond_value=0,
    )


@pytest.mark.asyncio
async def test_reconnect_includes_bond_final(
    maker_bot, sample_offer, test_private_key, test_pubkey
):
    """
    Test that reconnection announcements include fidelity bonds.
    """
    bot = maker_bot
    bot.current_offers = [sample_offer]

    bot.fidelity_bond = FidelityBondInfo(
        txid="ab" * 32,
        vout=0,
        value=100_000_000,
        locktime=800000,
        confirmation_time=1000,
        bond_value=50_000,
        pubkey=test_pubkey,
        private_key=test_private_key,
    )

    node_id = "localhost:5222"
    bot.directory_clients = {}
    bot._directory_reconnect_attempts = {node_id: 0}

    mock_client = MagicMock()
    mock_client.send_public_message = AsyncMock(return_value=None)

    bot._connect_to_directory = AsyncMock(return_value=(node_id, mock_client))

    with (
        patch("maker.background_tasks.get_notifier") as mock_notifier,
        patch.object(
            bot, "_format_offer_announcement", wraps=bot._format_offer_announcement
        ) as mock_format,
        patch("asyncio.sleep", side_effect=[0, 0, asyncio.CancelledError()]),
    ):
        mock_notifier.return_value = MagicMock()
        mock_notifier.return_value.notify_directory_reconnect = AsyncMock()
        mock_notifier.return_value.notify_all_directories_reconnected = AsyncMock()

        try:
            await bot._periodic_directory_reconnect()
        except asyncio.CancelledError:
            pass

        assert mock_format.called, "_format_offer_announcement was not called during reconnect"

        reconnect_call = next(
            (c for c in mock_format.call_args_list if c.kwargs.get("include_bond") is True), None
        )
        assert reconnect_call is not None, (
            "Reconnection announcement failed to include bond (include_bond=True missing)"
        )
