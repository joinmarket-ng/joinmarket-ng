"""
Tests for taker CLI module.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest
from jmcore.models import NetworkType
from typer.testing import CliRunner

from taker.cli import app, build_taker_config, create_backend

runner = CliRunner()


def test_root_help_shows_completion_options() -> None:
    """Taker CLI should expose Typer shell completion options."""
    result = runner.invoke(app, ["--help"], prog_name="jm-taker")
    output = click.unstyle(result.stdout)

    assert result.exit_code == 0
    assert "--install-completion" in output
    assert "--show-completion" in output


class TestBuildTakerConfig:
    """Tests for build_taker_config function."""

    @pytest.fixture
    def mock_settings(self, sample_mnemonic: str) -> MagicMock:
        """Create a mock Settings object with default values."""
        settings = MagicMock()

        # Network config - use actual NetworkType enum
        settings.network_config.network = NetworkType.SIGNET
        settings.network_config.bitcoin_network = None
        settings.network_config.directory_servers = ["dir1.onion:5222"]

        # Data dir
        settings.get_data_dir.return_value = "/tmp/jm-test"

        # Bitcoin backend
        settings.bitcoin.backend_type = "descriptor_wallet"
        settings.bitcoin.rpc_url = "http://localhost:8332"
        settings.bitcoin.rpc_user = "user"
        settings.bitcoin.rpc_password.get_secret_value.return_value = "password"
        settings.bitcoin.neutrino_url = "http://localhost:8334"
        settings.bitcoin.neutrino_tls_cert = None
        settings.bitcoin.neutrino_auth_token = None

        # Tor config
        settings.tor.socks_host = "127.0.0.1"
        settings.tor.socks_port = 9050

        # Taker config
        settings.taker.counterparty_count = 4
        settings.taker.max_cj_fee_abs = 1000
        settings.taker.max_cj_fee_rel = "0.002"
        settings.taker.fee_rate = None  # Not set in config
        settings.taker.fee_block_target = None  # Not set in config
        settings.taker.bondless_makers_allowance = 0.1
        settings.taker.bond_value_exponent = 1.3
        settings.taker.bondless_require_zero_fee = True
        settings.taker.tx_broadcast = "MULTIPLE_PEERS"
        settings.taker.broadcast_peer_count = 4
        settings.taker.minimum_makers = 4
        settings.taker.tx_fee_factor = 0.2
        settings.taker.maker_timeout_sec = 60
        settings.taker.order_wait_time = 10.0
        settings.taker.orderbook_min_wait = 30.0
        settings.taker.orderbook_quiet_period = 15.0
        settings.taker.rescan_interval_sec = 600
        settings.taker.taker_utxo_age = 5
        settings.taker.taker_utxo_retries = 3
        settings.taker.taker_utxo_amtpercent = 20

        # Wallet config
        settings.wallet.mixdepth_count = 5
        settings.wallet.gap_limit = 6
        settings.wallet.scan_range = 1000
        settings.wallet.dust_threshold = 546
        settings.wallet.max_sats_freeze_reuse = -1
        settings.wallet.smart_scan = True
        settings.wallet.background_full_rescan = False
        settings.wallet.scan_lookback_blocks = 1000
        settings.wallet.default_fee_block_target = 3  # Has a default value
        settings.wallet.max_fee_rate_sat_vb = 1_000.0  # fee-rate cap

        return settings

    def test_fee_rate_without_block_target(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """
        Test that when fee_rate is provided, fee_block_target is not set.

        This is a regression test for the bug where providing --fee-rate CLI flag
        still resulted in fee_block_target being set from defaults, causing validation
        to fail with "Cannot specify both fee_rate and fee_block_target" error.
        """
        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            fee_rate=5.0,  # User explicitly sets fee rate
            # block_target not set
        )

        assert config.fee_rate == 5.0
        assert config.fee_block_target is None

    def test_block_target_default_when_no_fee_rate(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Test that fee_block_target defaults to wallet setting when fee_rate is not provided."""
        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            # Neither fee_rate nor block_target set
        )

        assert config.fee_rate is None
        assert config.fee_block_target == 3  # From wallet.default_fee_block_target

    def test_max_sats_freeze_reuse_forwarded(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """``wallet.max_sats_freeze_reuse`` must reach the TakerConfig (#529)."""
        mock_settings.wallet.max_sats_freeze_reuse = 12_345
        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )
        assert config.max_sats_freeze_reuse == 12_345

    def test_explicit_block_target_overrides_default(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Test that explicit block_target CLI argument overrides defaults."""
        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            block_target=6,  # User explicitly sets block target
        )

        assert config.fee_rate is None
        assert config.fee_block_target == 6

    def test_counterparties_override_caps_minimum_makers(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """A per-run counterparty override must not leave a stale higher
        minimum-maker threshold behind.

        This matters for tumbler sweeps on sparse networks like signet:
        ``--counterparties 1`` should allow a 1-maker sweep if the taker
        explicitly requested that, even if config.toml normally requires 4.
        """
        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=0,
            mixdepth=0,
            counterparties=1,
        )

        assert config.counterparty_count == 1
        assert config.minimum_makers == 1

    def test_orderbook_wait_settings_forwarded(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Regression: ``taker.orderbook_min_wait`` and ``taker.orderbook_quiet_period``
        from config.toml must reach the TakerConfig instead of silently falling
        back to the model defaults."""
        mock_settings.taker.orderbook_min_wait = 45.0
        mock_settings.taker.orderbook_quiet_period = 20.0

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.orderbook_min_wait == 45.0
        assert config.orderbook_quiet_period == 20.0

    def test_taker_fee_rate_setting_honored_without_cli_flag(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Regression: taker.fee_rate from config.toml must be honored when no CLI
        flag is passed, and must suppress the fee_block_target fallback."""
        mock_settings.taker.fee_rate = 1.1  # Set in config.toml
        mock_settings.taker.fee_block_target = None

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.fee_rate == 1.1
        assert config.fee_block_target is None

    def test_cli_fee_rate_overrides_taker_fee_rate_setting(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """CLI --fee-rate must take precedence over taker.fee_rate from settings."""
        mock_settings.taker.fee_rate = 1.1

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            fee_rate=7.5,
        )

        assert config.fee_rate == 7.5
        assert config.fee_block_target is None

    def test_cli_block_target_overrides_taker_fee_rate_setting(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """CLI --block-target must override taker.fee_rate from settings."""
        mock_settings.taker.fee_rate = 1.1

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            block_target=8,
        )

        assert config.fee_rate is None
        assert config.fee_block_target == 8

    def test_taker_fee_block_target_setting_overrides_wallet_default(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Test that taker.fee_block_target takes priority over wallet.default_fee_block_target."""
        mock_settings.taker.fee_block_target = 10  # Set in taker config

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.fee_rate is None
        assert config.fee_block_target == 10  # From taker.fee_block_target, not wallet default

    def test_neutrino_add_peers_in_backend_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Test that neutrino_add_peers from settings flows into backend_config."""
        mock_settings.bitcoin.backend_type = "neutrino"
        mock_settings.get_neutrino_add_peers.return_value = ["peer1.example.com:38333"]
        mock_settings.swap.provider_url = "http://127.0.0.1:19999"

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.backend_type == "neutrino"
        assert config.backend_config.get("add_peers") == ["peer1.example.com:38333"]

    def test_neutrino_empty_add_peers_by_default(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Test that add_peers defaults to empty list when not configured."""
        mock_settings.bitcoin.backend_type = "neutrino"
        mock_settings.get_neutrino_add_peers.return_value = []
        mock_settings.swap.provider_url = "http://127.0.0.1:19999"

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.backend_config.get("add_peers") == []

    def test_neutrino_tls_and_auth_in_backend_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Test that neutrino TLS cert and auth token flow into backend_config."""
        mock_settings.bitcoin.backend_type = "neutrino"
        mock_settings.get_neutrino_add_peers.return_value = []
        mock_settings.bitcoin.neutrino_tls_cert = "/tmp/neutrino/tls.cert"
        mock_settings.bitcoin.neutrino_auth_token = "token-123"

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.backend_config.get("tls_cert_path") == "/tmp/neutrino/tls.cert"
        assert config.backend_config.get("auth_token") == "token-123"

    def test_neutrino_defaults_resolve_and_upgrade_https(
        self, sample_mnemonic: str, tmp_path, monkeypatch
    ) -> None:
        """Default relative cert/token paths resolve against the data dir, the
        auth-token file is read, and the URL is upgraded to HTTPS."""
        from jmcore.settings import JoinMarketSettings

        # Isolate from any real user config so the test relies on defaults.
        monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "missing.toml"))

        token_dir = tmp_path / "neutrino"
        token_dir.mkdir()
        (token_dir / "auth_token").write_text("filetoken\n")

        settings = JoinMarketSettings()
        settings.bitcoin.backend_type = "neutrino"

        config = build_taker_config(
            settings=settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            data_dir=tmp_path,
        )

        assert config.backend_config.get("auth_token") == "filetoken"
        assert config.backend_config.get("neutrino_url") == "https://127.0.0.1:8334"
        assert config.backend_config.get("tls_cert_path") == str(tmp_path / "neutrino" / "tls.cert")

    def test_create_backend_neutrino_passes_tls_and_auth(self, sample_mnemonic: str) -> None:
        """create_backend() passes TLS cert and auth token to NeutrinoBackend."""
        from unittest.mock import MagicMock, patch

        config = MagicMock()
        config.backend_type = "neutrino"
        config.backend_config = {
            "neutrino_url": "https://127.0.0.1:8334",
            "scan_start_height": 123,
            "add_peers": ["bitcoin.sgn.space:38333"],
            "tls_cert_path": "/tmp/neutrino/tls.cert",
            "auth_token": "token-123",
        }
        config.bitcoin_network = NetworkType.SIGNET
        config.network = NetworkType.SIGNET
        config.creation_height = None

        mock_backend = MagicMock()
        with patch(
            "jmwallet.backends.neutrino.NeutrinoBackend", return_value=mock_backend
        ) as mock_cls:
            result = create_backend(config)

        mock_cls.assert_called_once_with(
            neutrino_url="https://127.0.0.1:8334",
            network="signet",
            scan_start_height=123,
            add_peers=["bitcoin.sgn.space:38333"],
            tls_cert_path="/tmp/neutrino/tls.cert",
            auth_token="token-123",
            include_mempool=True,
        )
        assert result is mock_backend

    def test_neutrino_include_mempool_flows_to_backend(
        self, sample_mnemonic: str, tmp_path, monkeypatch
    ) -> None:
        """The neutrino_include_mempool toggle reaches NeutrinoBackend so the
        documented chain-only opt-out is not silently ignored for the taker."""
        from unittest.mock import MagicMock, patch

        from jmcore.settings import JoinMarketSettings

        monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "missing.toml"))

        settings = JoinMarketSettings()
        settings.bitcoin.backend_type = "neutrino"
        settings.bitcoin.neutrino_include_mempool = False

        config = build_taker_config(
            settings=settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            data_dir=tmp_path,
        )
        assert config.backend_config.get("include_mempool") is False

        mock_backend = MagicMock()
        with patch(
            "jmwallet.backends.neutrino.NeutrinoBackend", return_value=mock_backend
        ) as mock_cls:
            create_backend(config)

        _, kwargs = mock_cls.call_args
        assert kwargs["include_mempool"] is False

    def test_data_dir_flows_to_config(self, sample_mnemonic: str, mock_settings: MagicMock) -> None:
        """Verify data_dir from settings flows into TakerConfig.

        Regression test: taker was creating WalletService without data_dir,
        which meant metadata_store was None and frozen UTXOs were ignored.
        """
        from pathlib import Path

        mock_settings.get_data_dir.return_value = Path("/tmp/jm-test-data")

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.data_dir == Path("/tmp/jm-test-data")

    def test_podle_settings_flow_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """PoDLE-related ``[taker]`` settings must reach ``TakerConfig``.

        Regression test: ``taker_utxo_age`` / ``taker_utxo_retries`` /
        ``taker_utxo_amtpercent`` were defined nowhere in ``TakerSettings``
        and never threaded into ``TakerConfig``, so the documented config
        keys were silently ignored and only the hardcoded defaults applied.
        """
        mock_settings.taker.taker_utxo_age = 7
        mock_settings.taker.taker_utxo_retries = 5
        mock_settings.taker.taker_utxo_amtpercent = 25

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.taker_utxo_age == 7
        assert config.taker_utxo_retries == 5
        assert config.taker_utxo_amtpercent == 25

    def test_gap_limit_flows_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """``[wallet].gap_limit`` must reach ``TakerConfig`` so it can be
        forwarded to ``WalletService`` and drive the descriptor scan range
        (issue #475 recovery for migrated wallets).
        """
        mock_settings.wallet.gap_limit = 50

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.gap_limit == 50
