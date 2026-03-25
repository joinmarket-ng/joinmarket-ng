"""
Test for memory leak in _rate_limited_log_times
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from jmcore.models import NetworkType

from maker.bot import MakerBot
from maker.config import MakerConfig


class TestLogLeak:
    @pytest.fixture
    def mock_wallet(self):
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        return wallet

    @pytest.fixture
    def mock_backend(self):
        return MagicMock()

    @pytest.fixture
    def config(self):
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )

    @pytest.fixture
    def bot(self, mock_wallet, mock_backend, config):
        return MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

    def test_log_times_size_limit(self, bot, monkeypatch):
        """Verify that _rate_limited_log_times does not exceed MAX_LOG_RATE_LIMIT_ENTRIES."""
        import maker.bot

        test_limit = 10
        monkeypatch.setattr(maker.bot, "MAX_LOG_RATE_LIMIT_ENTRIES", test_limit)

        for i in range(test_limit):
            key = f"test_key_{i}"
            bot._log_rate_limited(key, f"Message {i}")

        assert len(bot._rate_limited_log_times) == test_limit

        oldest_key = "test_key_0"
        assert oldest_key in bot._rate_limited_log_times

        new_key = "key_overflow"
        bot._log_rate_limited(new_key, "Overflow message")

        assert len(bot._rate_limited_log_times) == test_limit
        assert new_key in bot._rate_limited_log_times
        assert oldest_key not in bot._rate_limited_log_times, "Oldest key should have been evicted"
