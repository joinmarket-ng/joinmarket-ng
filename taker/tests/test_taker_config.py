"""
Tests for taker configuration module.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from taker.config import MaxCjFee, Schedule, ScheduleEntry, TakerConfig


class TestMaxCjFee:
    """Tests for MaxCjFee model."""

    def test_default_values(self) -> None:
        """Test default fee values."""
        fee = MaxCjFee()
        assert fee.abs_fee == 500
        assert fee.rel_fee == "0.001"

    def test_custom_values(self) -> None:
        """Test custom fee values."""
        fee = MaxCjFee(abs_fee=100_000, rel_fee="0.005")
        assert fee.abs_fee == 100_000
        assert fee.rel_fee == "0.005"

    def test_abs_fee_must_be_non_negative(self) -> None:
        """Test that absolute fee cannot be negative."""
        with pytest.raises(ValidationError):
            MaxCjFee(abs_fee=-1)


class TestTakerConfig:
    """Tests for TakerConfig model."""

    def test_minimal_config(self, sample_mnemonic: str) -> None:
        """Test minimal required configuration."""
        config = TakerConfig(mnemonic=sample_mnemonic)
        assert config.mnemonic.get_secret_value() == sample_mnemonic
        assert config.network.value == "mainnet"
        assert config.counterparty_count == 10

    def test_full_config(self, sample_mnemonic: str) -> None:
        """Test full configuration with all options."""
        config = TakerConfig(
            mnemonic=sample_mnemonic,
            network="testnet",
            backend_type="scantxoutset",
            directory_servers=["server1:5222", "server2:5222"],
            destination_address="tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
            amount=1_000_000,
            mixdepth=2,
            counterparty_count=5,
            max_cj_fee=MaxCjFee(abs_fee=10_000, rel_fee="0.002"),
            tx_fee_factor=2.5,
            taker_utxo_retries=5,
            taker_utxo_age=10,
            minimum_makers=3,
        )
        assert config.network.value == "testnet"
        assert config.backend_type == "scantxoutset"
        assert len(config.directory_servers) == 2
        assert config.counterparty_count == 5
        assert config.max_cj_fee.abs_fee == 10_000
        assert config.tx_fee_factor == 2.5
        assert config.minimum_makers == 3

    def test_counterparty_count_bounds(self, sample_mnemonic: str) -> None:
        """Test counterparty count validation bounds."""
        # Valid minimum
        config = TakerConfig(mnemonic=sample_mnemonic, counterparty_count=1)
        assert config.counterparty_count == 1

        # Valid maximum
        config = TakerConfig(mnemonic=sample_mnemonic, counterparty_count=20)
        assert config.counterparty_count == 20

        # Invalid - too low
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, counterparty_count=0)

        # Invalid - too high
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, counterparty_count=21)

    def test_tx_fee_factor_bounds(self, sample_mnemonic: str) -> None:
        """Test tx_fee_factor can be 0 or positive (for randomization)."""
        # 0 disables randomization
        config = TakerConfig(mnemonic=sample_mnemonic, tx_fee_factor=0.0)
        assert config.tx_fee_factor == 0.0

        # Default is 0.2 (20% randomization range)
        config = TakerConfig(mnemonic=sample_mnemonic)
        assert config.tx_fee_factor == 0.2

        # Negative not allowed
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, tx_fee_factor=-0.1)

    def test_mixdepth_count_bounds(self, sample_mnemonic: str) -> None:
        """Test mixdepth count validation."""
        # Valid
        config = TakerConfig(mnemonic=sample_mnemonic, mixdepth_count=10)
        assert config.mixdepth_count == 10

        # Invalid - too low
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, mixdepth_count=0)

        # Invalid - too high
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, mixdepth_count=11)

    def test_gap_limit_minimum(self, sample_mnemonic: str) -> None:
        """Test gap limit must be at least 6."""
        config = TakerConfig(mnemonic=sample_mnemonic, gap_limit=6)
        assert config.gap_limit == 6

        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, gap_limit=5)

    def test_rescan_interval_default(self, sample_mnemonic: str) -> None:
        """Test default rescan interval is 600 seconds (10 minutes)."""
        config = TakerConfig(mnemonic=sample_mnemonic)
        assert config.rescan_interval_sec == 600

    def test_rescan_interval_custom(self, sample_mnemonic: str) -> None:
        """Test custom rescan interval."""
        config = TakerConfig(mnemonic=sample_mnemonic, rescan_interval_sec=120)
        assert config.rescan_interval_sec == 120

    def test_rescan_interval_minimum(self, sample_mnemonic: str) -> None:
        """Test rescan interval must be at least 60 seconds."""
        # Valid minimum
        config = TakerConfig(mnemonic=sample_mnemonic, rescan_interval_sec=60)
        assert config.rescan_interval_sec == 60

        # Invalid - too low
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, rescan_interval_sec=30)

    def test_connection_timeout_default(self, sample_mnemonic: str) -> None:
        """Test that connection_timeout defaults to 120s (matches Tor circuit timeout).

        The timeout covers the entire SOCKS5 connection lifecycle including
        Tor circuit building and PoW solving. Under PoW defense, connections
        can take much longer than the ~5-15s normal circuit establishment.
        """
        config = TakerConfig(mnemonic=sample_mnemonic)
        assert config.connection_timeout == 120.0

    def test_connection_timeout_inherited_from_wallet_config(self, sample_mnemonic: str) -> None:
        """Test that TakerConfig inherits connection_timeout from WalletConfig."""
        config = TakerConfig(mnemonic=sample_mnemonic, connection_timeout=90.0)
        assert config.connection_timeout == 90.0

    def test_fee_rate_default_is_none(self, sample_mnemonic: str) -> None:
        """Test that fee_rate defaults to None (use estimation)."""
        config = TakerConfig(mnemonic=sample_mnemonic)
        assert config.fee_rate is None

    def test_fee_rate_custom(self, sample_mnemonic: str) -> None:
        """Test setting custom fee rate."""
        config = TakerConfig(mnemonic=sample_mnemonic, fee_rate=1.5)
        assert config.fee_rate == 1.5

    def test_fee_rate_sub_sat(self, sample_mnemonic: str) -> None:
        """Test sub-1 sat/vB fee rate is allowed."""
        config = TakerConfig(mnemonic=sample_mnemonic, fee_rate=0.5)
        assert config.fee_rate == 0.5

    def test_fee_rate_must_be_positive(self, sample_mnemonic: str) -> None:
        """Test that fee_rate must be positive if set."""
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, fee_rate=0)

        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, fee_rate=-1.0)

    def test_fee_block_target_default_is_none(self, sample_mnemonic: str) -> None:
        """Test that fee_block_target defaults to None (use default 3)."""
        config = TakerConfig(mnemonic=sample_mnemonic)
        assert config.fee_block_target is None

    def test_fee_block_target_custom(self, sample_mnemonic: str) -> None:
        """Test setting custom block target."""
        config = TakerConfig(mnemonic=sample_mnemonic, fee_block_target=6)
        assert config.fee_block_target == 6

    def test_fee_block_target_bounds(self, sample_mnemonic: str) -> None:
        """Test block target must be between 1 and 1008."""
        # Valid minimum
        config = TakerConfig(mnemonic=sample_mnemonic, fee_block_target=1)
        assert config.fee_block_target == 1

        # Valid maximum
        config = TakerConfig(mnemonic=sample_mnemonic, fee_block_target=1008)
        assert config.fee_block_target == 1008

        # Invalid - too low
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, fee_block_target=0)

        # Invalid - too high
        with pytest.raises(ValidationError):
            TakerConfig(mnemonic=sample_mnemonic, fee_block_target=1009)

    def test_fee_rate_and_block_target_mutually_exclusive(self, sample_mnemonic: str) -> None:
        """Test that fee_rate and fee_block_target cannot both be set."""
        with pytest.raises(ValidationError) as excinfo:
            TakerConfig(mnemonic=sample_mnemonic, fee_rate=2.0, fee_block_target=3)

        assert "Cannot specify both fee_rate and fee_block_target" in str(excinfo.value)


class TestScheduleEntry:
    """Tests for ScheduleEntry model."""

    def test_basic_entry(self) -> None:
        """Test basic schedule entry."""
        entry = ScheduleEntry(
            mixdepth=0,
            amount=1_000_000,
            counterparty_count=3,
            destination="INTERNAL",
        )
        assert entry.mixdepth == 0
        assert entry.amount == 1_000_000
        assert entry.counterparty_count == 3
        assert entry.destination == "INTERNAL"
        assert entry.wait_time == 0.0
        assert entry.rounding == 16
        assert entry.completed is False

    def test_fractional_amount(self) -> None:
        """Test fractional amount (sweep percentage)."""
        entry = ScheduleEntry(
            mixdepth=1,
            amount_fraction=0.5,
            counterparty_count=4,
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        )
        assert entry.amount_fraction == 0.5
        assert entry.amount is None

    def test_amount_mutual_exclusivity(self) -> None:
        """Test that amount and amount_fraction are mutually exclusive."""
        # Both None
        with pytest.raises(ValidationError):
            ScheduleEntry(
                mixdepth=1,
                counterparty_count=4,
                destination="INTERNAL",
            )

        # Both set
        with pytest.raises(ValidationError):
            ScheduleEntry(
                mixdepth=1,
                amount=100000,
                amount_fraction=0.5,
                counterparty_count=4,
                destination="INTERNAL",
            )

    def test_mixdepth_bounds(self) -> None:
        """Test mixdepth must be 0-9."""
        # Valid
        entry = ScheduleEntry(
            mixdepth=9, amount=100000, counterparty_count=2, destination="INTERNAL"
        )
        assert entry.mixdepth == 9

        # Invalid - negative
        with pytest.raises(ValidationError):
            ScheduleEntry(mixdepth=-1, amount=100000, counterparty_count=2, destination="INTERNAL")

        # Invalid - too high
        with pytest.raises(ValidationError):
            ScheduleEntry(mixdepth=10, amount=100000, counterparty_count=2, destination="INTERNAL")

    def test_counterparty_bounds(self) -> None:
        """Test counterparty count bounds in schedule entry."""
        # Invalid - zero
        with pytest.raises(ValidationError):
            ScheduleEntry(mixdepth=0, amount=100000, counterparty_count=0, destination="INTERNAL")

        # Invalid - too high
        with pytest.raises(ValidationError):
            ScheduleEntry(mixdepth=0, amount=100000, counterparty_count=21, destination="INTERNAL")


class TestSchedule:
    """Tests for Schedule model."""

    def test_empty_schedule(self) -> None:
        """Test empty schedule."""
        schedule = Schedule()
        assert len(schedule.entries) == 0
        assert schedule.current_index == 0
        assert schedule.current_entry() is None
        assert schedule.is_complete()

    def test_schedule_with_entries(self) -> None:
        """Test schedule with multiple entries."""
        entries = [
            ScheduleEntry(
                mixdepth=0, amount=1_000_000, counterparty_count=3, destination="INTERNAL"
            ),
            ScheduleEntry(mixdepth=1, amount=500_000, counterparty_count=4, destination="INTERNAL"),
            ScheduleEntry(
                mixdepth=2,
                amount_fraction=0.5,
                counterparty_count=5,
                destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            ),
        ]
        schedule = Schedule(entries=entries)

        assert len(schedule.entries) == 3
        assert schedule.current_index == 0
        assert not schedule.is_complete()

        # Check current entry
        current = schedule.current_entry()
        assert current is not None
        assert current.mixdepth == 0
        assert current.amount == 1_000_000

    def test_schedule_advance(self) -> None:
        """Test advancing through schedule entries."""
        entries = [
            ScheduleEntry(mixdepth=0, amount=100000, counterparty_count=2, destination="INTERNAL"),
            ScheduleEntry(mixdepth=1, amount=200000, counterparty_count=3, destination="INTERNAL"),
        ]
        schedule = Schedule(entries=entries)

        # First entry
        assert schedule.current_index == 0
        assert not schedule.entries[0].completed

        # Advance
        has_more = schedule.advance()
        assert has_more is True
        assert schedule.current_index == 1
        assert schedule.entries[0].completed

        # Get current
        current = schedule.current_entry()
        assert current is not None
        assert current.mixdepth == 1

        # Advance again
        has_more = schedule.advance()
        assert has_more is False
        assert schedule.is_complete()
        assert schedule.entries[1].completed

    def test_schedule_current_entry_after_completion(self) -> None:
        """Test current_entry returns None when complete."""
        entries = [
            ScheduleEntry(mixdepth=0, amount=100000, counterparty_count=2, destination="INTERNAL"),
        ]
        schedule = Schedule(entries=entries)

        schedule.advance()
        assert schedule.is_complete()
        assert schedule.current_entry() is None
