"""Tests for PoDLE commitment broadcasting via ephemeral identity.

Tests the commitment broadcast flow where the maker opens fresh Tor
connections with a random nick to broadcast !hp2 messages, preventing
correlation with the maker's long-lived identity:

- _broadcast_commitment: blacklists locally + schedules ephemeral broadcast
- _broadcast_commitment_ephemeral: fresh connections, pubmsg, close
- _handle_hp2_privmsg: relay requests from other makers
- _handle_hp2_pubmsg: public commitment blacklisting
- _hp2_own_broadcast_semaphore / _hp2_relay_broadcast_semaphore:
  separated concurrency pools so relay floods cannot starve own broadcasts
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from jmcore.models import NetworkType

from maker.bot import MakerBot
from maker.config import MakerConfig

COMMITMENT = "efe182de0a45f10e1af4082c088d30efe36f03fe6dc2cea946c127dad831eb81"


@pytest.fixture
def config() -> MakerConfig:
    return MakerConfig(
        mnemonic="abandon " * 11 + "about",
        directory_servers=["dir1.onion:5222", "dir2.onion:5222"],
        network=NetworkType.REGTEST,
    )


@pytest.fixture
def maker_bot(config: MakerConfig) -> MakerBot:
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    backend = MagicMock()
    backend.get_block_height = AsyncMock(return_value=800_000)
    return MakerBot(wallet=wallet, backend=backend, config=config)


@pytest.fixture
def mock_directory_client() -> MagicMock:
    """A mock DirectoryClient for the maker's long-lived connections."""
    client = MagicMock()
    client.send_public_message = AsyncMock()
    client.send_private_message = AsyncMock()
    client.get_active_nicks = MagicMock(return_value={"PeerA", "PeerB"})
    return client


@pytest.mark.asyncio
class TestBroadcastCommitment:
    """Tests for _broadcast_commitment (the entry point)."""

    async def test_adds_to_local_blacklist(self, maker_bot: MakerBot) -> None:
        with (
            patch("maker.protocol_handlers.add_commitment") as mock_add,
            patch.object(maker_bot, "_broadcast_commitment_ephemeral", new=AsyncMock()),
        ):
            await maker_bot._broadcast_commitment(COMMITMENT)

        mock_add.assert_called_once_with(COMMITMENT)

    async def test_schedules_ephemeral_broadcast(self, maker_bot: MakerBot) -> None:
        with (
            patch("maker.protocol_handlers.add_commitment"),
            patch.object(
                maker_bot, "_broadcast_commitment_ephemeral", new=AsyncMock()
            ) as mock_ephemeral,
        ):
            await maker_bot._broadcast_commitment(COMMITMENT)

        # Let the create_task fire
        await asyncio.sleep(0)

        mock_ephemeral.assert_called_once_with(COMMITMENT, is_relay=False)

    async def test_does_not_use_existing_directory_connections(
        self, maker_bot: MakerBot, mock_directory_client: MagicMock
    ) -> None:
        """The broadcast must NOT use the maker's long-lived connections."""
        maker_bot.directory_clients["dir1"] = mock_directory_client

        with (
            patch("maker.protocol_handlers.add_commitment"),
            patch.object(maker_bot, "_broadcast_commitment_ephemeral", new=AsyncMock()),
        ):
            await maker_bot._broadcast_commitment(COMMITMENT)

        mock_directory_client.send_public_message.assert_not_called()
        mock_directory_client.send_private_message.assert_not_called()


@pytest.mark.asyncio
class TestBroadcastCommitmentEphemeral:
    """Tests for _broadcast_commitment_ephemeral (the background task)."""

    async def test_creates_ephemeral_clients_with_random_nick(self, maker_bot: MakerBot) -> None:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client) as mock_cls:
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT)

        # Should create one client per directory server
        assert mock_cls.call_count == 2

        # Each client should use a NickIdentity (not the maker's own)
        for c in mock_cls.call_args_list:
            kwargs = c[1]
            assert kwargs["nick_identity"] is not None
            # Should not be the maker's own nick identity
            assert kwargs["nick_identity"] != maker_bot.nick_identity

    async def test_uses_unique_socks_credentials(self, maker_bot: MakerBot) -> None:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client) as mock_cls:
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT)

        # Both clients should use the same ephemeral credentials (same broadcast)
        kwargs1 = mock_cls.call_args_list[0][1]
        kwargs2 = mock_cls.call_args_list[1][1]
        assert kwargs1["socks_username"] == "jm-hp2-broadcast"
        assert kwargs2["socks_username"] == "jm-hp2-broadcast"
        # Password should be random (non-empty)
        assert kwargs1["socks_password"]
        assert len(kwargs1["socks_password"]) == 32  # hex(16 bytes)

    async def test_different_broadcasts_get_different_credentials(
        self, maker_bot: MakerBot
    ) -> None:
        passwords: list[str] = []

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client) as mock_cls:
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT)
            passwords.append(mock_cls.call_args_list[0][1]["socks_password"])

            mock_cls.reset_mock()
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT + "aa")
            passwords.append(mock_cls.call_args_list[0][1]["socks_password"])

        # Each broadcast should use a different password (different Tor circuit)
        assert passwords[0] != passwords[1]

    async def test_broadcasts_hp2_pubmsg_on_all_clients(self, maker_bot: MakerBot) -> None:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client):
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT)

        # send_public_message called once per directory server
        assert mock_client.send_public_message.call_count == 2
        mock_client.send_public_message.assert_has_calls(
            [call(f"hp2 {COMMITMENT}"), call(f"hp2 {COMMITMENT}")]
        )

    async def test_closes_all_clients_after_broadcast(self, maker_bot: MakerBot) -> None:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client):
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT)

        assert mock_client.close.call_count == 2

    async def test_closes_clients_even_on_broadcast_failure(self, maker_bot: MakerBot) -> None:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock(side_effect=ConnectionError("lost"))
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client):
            # Should not raise
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT)

        assert mock_client.close.call_count == 2

    async def test_handles_connection_failure_gracefully(self, maker_bot: MakerBot) -> None:
        with patch("maker.protocol_handlers.DirectoryClient") as mock_cls:
            mock_cls.return_value.connect = AsyncMock(side_effect=ConnectionError("unreachable"))
            mock_cls.return_value.close = AsyncMock()

            # Should not raise
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT)

    async def test_partial_connection_still_broadcasts(self, maker_bot: MakerBot) -> None:
        """If one directory fails to connect, broadcast on the others."""
        call_count = 0

        def make_client(*_args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            client = MagicMock()
            client.send_public_message = AsyncMock()
            client.close = AsyncMock()
            if call_count == 0:
                client.connect = AsyncMock(side_effect=ConnectionError("fail"))
            else:
                client.connect = AsyncMock()
            call_count += 1
            return client

        with patch("maker.protocol_handlers.DirectoryClient", side_effect=make_client):
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT)


@pytest.mark.asyncio
class TestHandleHp2Privmsg:
    """Tests for _handle_hp2_privmsg (relay requests from other makers)."""

    async def test_blacklists_commitment_locally(self, maker_bot: MakerBot) -> None:
        with (
            patch("maker.protocol_handlers.add_commitment") as mock_add,
            patch.object(maker_bot, "_broadcast_commitment_ephemeral", new=AsyncMock()),
        ):
            await maker_bot._handle_hp2_privmsg("SomeMaker", f"hp2 {COMMITMENT}")

        mock_add.assert_called_once_with(COMMITMENT)

    async def test_schedules_ephemeral_broadcast(self, maker_bot: MakerBot) -> None:
        with (
            patch("maker.protocol_handlers.add_commitment"),
            patch.object(
                maker_bot, "_broadcast_commitment_ephemeral", new=AsyncMock()
            ) as mock_ephemeral,
        ):
            await maker_bot._handle_hp2_privmsg("SomeMaker", f"hp2 {COMMITMENT}")

        await asyncio.sleep(0)

        mock_ephemeral.assert_called_once_with(COMMITMENT, is_relay=True)

    async def test_ignores_invalid_format(self, maker_bot: MakerBot) -> None:
        with (
            patch("maker.protocol_handlers.add_commitment") as mock_add,
            patch.object(maker_bot, "_broadcast_commitment_ephemeral", new=AsyncMock()),
        ):
            await maker_bot._handle_hp2_privmsg("SomeMaker", "hp2")

        mock_add.assert_not_called()

    async def test_does_not_use_existing_connections(
        self, maker_bot: MakerBot, mock_directory_client: MagicMock
    ) -> None:
        """Relay should use ephemeral connections, not the maker's own."""
        maker_bot.directory_clients["dir1"] = mock_directory_client

        with (
            patch("maker.protocol_handlers.add_commitment"),
            patch.object(maker_bot, "_broadcast_commitment_ephemeral", new=AsyncMock()),
        ):
            await maker_bot._handle_hp2_privmsg("SomeMaker", f"hp2 {COMMITMENT}")

        mock_directory_client.send_public_message.assert_not_called()

    async def test_handles_errors_gracefully(self, maker_bot: MakerBot) -> None:
        with patch("maker.protocol_handlers.add_commitment", side_effect=OSError("disk full")):
            # Should not raise
            await maker_bot._handle_hp2_privmsg("SomeMaker", f"hp2 {COMMITMENT}")


@pytest.mark.asyncio
class TestHandleHp2Pubmsg:
    """Tests for _handle_hp2_pubmsg (public channel blacklisting)."""

    async def test_blacklists_commitment(self, maker_bot: MakerBot) -> None:
        with patch("maker.protocol_handlers.add_commitment") as mock_add:
            mock_add.return_value = True
            await maker_bot._handle_hp2_pubmsg("SomeMaker", f"hp2 {COMMITMENT}")

        mock_add.assert_called_once_with(COMMITMENT)

    async def test_ignores_invalid_format(self, maker_bot: MakerBot) -> None:
        with patch("maker.protocol_handlers.add_commitment") as mock_add:
            await maker_bot._handle_hp2_pubmsg("SomeMaker", "hp2")

        mock_add.assert_not_called()


@pytest.mark.asyncio
class TestHp2BroadcastSemaphores:
    """Tests for the dedicated concurrency pools protecting ephemeral broadcasts."""

    async def test_semaphores_initialized_unlocked(self, maker_bot: MakerBot) -> None:
        assert not maker_bot._hp2_own_broadcast_semaphore.locked()
        assert not maker_bot._hp2_relay_broadcast_semaphore.locked()

    async def test_relay_pool_drops_relays_on_contention(self, maker_bot: MakerBot) -> None:
        """When the relay pool is busy, additional relayed broadcasts are dropped."""
        await maker_bot._hp2_relay_broadcast_semaphore.acquire()

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client):
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT, is_relay=True)

        mock_client.connect.assert_not_called()

        maker_bot._hp2_relay_broadcast_semaphore.release()

    async def test_relay_saturation_does_not_starve_own_broadcast(
        self, maker_bot: MakerBot
    ) -> None:
        """Security invariant: a Sybil flood of relayed broadcasts must not block
        the maker's own post-ioauth commitment broadcast. The two pools are
        independent, so saturating the relay pool leaves the own pool free."""
        await maker_bot._hp2_relay_broadcast_semaphore.acquire()

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client):
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT, is_relay=False)

        # Own broadcast must have reached all configured directories.
        assert mock_client.connect.call_count == 2
        assert mock_client.send_public_message.call_count == 2
        mock_client.send_public_message.assert_has_calls(
            [call(f"hp2 {COMMITMENT}"), call(f"hp2 {COMMITMENT}")]
        )

        maker_bot._hp2_relay_broadcast_semaphore.release()

    async def test_own_pool_released_after_broadcast(self, maker_bot: MakerBot) -> None:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client):
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT, is_relay=False)

        assert not maker_bot._hp2_own_broadcast_semaphore.locked()

    async def test_relay_pool_released_after_broadcast(self, maker_bot: MakerBot) -> None:
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.send_public_message = AsyncMock()
        mock_client.close = AsyncMock()

        with patch("maker.protocol_handlers.DirectoryClient", return_value=mock_client):
            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT, is_relay=True)

        assert not maker_bot._hp2_relay_broadcast_semaphore.locked()

    async def test_own_pool_released_on_failure(self, maker_bot: MakerBot) -> None:
        with patch("maker.protocol_handlers.DirectoryClient") as mock_cls:
            mock_cls.return_value.connect = AsyncMock(side_effect=ConnectionError("fail"))
            mock_cls.return_value.close = AsyncMock()

            await maker_bot._broadcast_commitment_ephemeral(COMMITMENT, is_relay=False)

        assert not maker_bot._hp2_own_broadcast_semaphore.locked()
