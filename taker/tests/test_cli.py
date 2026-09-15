"""
Tests for taker CLI module.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest
from jmcore.models import NetworkType
from jmcore.nick_auth import NickAuthMode
from typer.testing import CliRunner

from taker.cli import _run_coinjoin, app, build_taker_config, create_backend

runner = CliRunner()


def test_root_help_shows_completion_options() -> None:
    """Taker CLI should expose Typer shell completion options."""
    result = runner.invoke(app, ["--help"], prog_name="jm-taker")
    output = click.unstyle(result.stdout)

    assert result.exit_code == 0
    assert "--install-completion" in output
    assert "--show-completion" in output


def test_legacy_tumble_command_is_not_exposed() -> None:
    help_result = runner.invoke(app, ["--help"], prog_name="jm-taker")
    command_result = runner.invoke(app, ["tumble", "schedule.json"], prog_name="jm-taker")

    assert "tumble" not in click.unstyle(help_result.stdout)
    assert command_result.exit_code == 2
    assert "No such command 'tumble'" in click.unstyle(command_result.output)


def test_help_output_is_alphabetically_sorted() -> None:
    """Subcommands and options must be listed alphabetically in --help."""
    from jmcore.cli_help import find_unsorted_help

    assert find_unsorted_help(app) == []


def test_coinjoin_help_includes_fee_quantization_and_explicit_input_options() -> None:
    result = runner.invoke(app, ["coinjoin", "--help"], prog_name="jm-taker")
    output = click.unstyle(result.stdout)

    assert result.exit_code == 0
    assert "--input-utxo" in output
    assert "round-up-cj" in output


def test_coinjoin_rejects_interactive_and_explicit_selection_together() -> None:
    with patch("taker.cli.setup_cli") as mock_setup:
        result = runner.invoke(
            app,
            [
                "coinjoin",
                "--amount",
                "100000",
                "--select-utxos",
                "--input-utxo",
                f"{'aa' * 32}:0",
            ],
        )

    assert result.exit_code == 1
    mock_setup.assert_not_called()


def test_coinjoin_select_utxos_does_not_require_amount_or_mixdepth() -> None:
    settings = MagicMock()
    config = MagicMock()
    config.network.value = "regtest"
    config.backend_type = "descriptor_wallet"
    config.socks_host = "127.0.0.1"
    config.socks_port = 9050
    config.counterparty_count = 3
    resolved = MagicMock(mnemonic="test mnemonic", bip39_passphrase="", creation_height=None)

    with (
        patch("taker.cli.setup_cli", return_value=settings),
        patch("taker.cli.ensure_config_file"),
        patch("taker.cli.resolve_mnemonic", return_value=resolved),
        patch("taker.cli.build_taker_config", return_value=config) as mock_build_config,
        patch("taker.cli._run_coinjoin", new_callable=AsyncMock) as mock_run,
    ):
        result = runner.invoke(app, ["coinjoin", "--select-utxos"])

    assert result.exit_code == 0, result.output
    assert mock_build_config.call_args.kwargs["amount"] == 0
    assert mock_run.await_args is not None
    assert mock_run.await_args.kwargs["amount"] == 0
    assert mock_run.await_args.kwargs["mixdepth"] is None


def test_coinjoin_requires_amount_without_interactive_selection() -> None:
    from loguru import logger

    messages: list[str] = []
    handler_id = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        with patch("taker.cli.setup_cli") as mock_setup:
            result = runner.invoke(app, ["coinjoin"])
    finally:
        logger.remove(handler_id)

    assert result.exit_code == 1
    assert "--amount is required unless --select-utxos is used" in messages
    mock_setup.assert_not_called()


def test_coinjoin_forwards_repeated_explicit_inputs() -> None:
    first = f"{'aa' * 32}:0"
    second = f"{'bb' * 32}:1"
    settings = MagicMock()
    config = MagicMock()
    config.network.value = "regtest"
    config.backend_type = "descriptor_wallet"
    config.socks_host = "127.0.0.1"
    config.socks_port = 9050
    config.counterparty_count = 3
    resolved = MagicMock(mnemonic="test mnemonic", bip39_passphrase="", creation_height=None)

    with (
        patch("taker.cli.setup_cli", return_value=settings),
        patch("taker.cli.ensure_config_file"),
        patch("taker.cli.resolve_mnemonic", return_value=resolved),
        patch("taker.cli.build_taker_config", return_value=config),
        patch("taker.cli._run_coinjoin", new_callable=AsyncMock) as mock_run,
    ):
        result = runner.invoke(
            app,
            [
                "coinjoin",
                "--amount",
                "100000",
                "--input-utxo",
                first,
                "--input-utxo",
                second,
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.await_args is not None
    assert mock_run.await_args.kwargs["input_utxos"] == [first, second]


def test_unexpected_coinjoin_traceback_is_sensitive() -> None:
    from loguru import logger

    marker = "private-coinjoin-marker"
    settings = MagicMock()
    config = MagicMock()
    config.network.value = "regtest"
    config.backend_type = "descriptor_wallet"
    config.socks_host = "127.0.0.1"
    config.socks_port = 9050
    config.counterparty_count = 3
    resolved = MagicMock(mnemonic="test mnemonic", bip39_passphrase="", creation_height=None)
    records: list[tuple[str, bool, str]] = []
    handler_id = logger.add(
        lambda message: records.append(
            (
                message.record["message"],
                bool(message.record["extra"].get("sensitive", False)),
                str(message.record["exception"]),
            )
        )
    )
    try:
        with (
            patch("taker.cli.setup_cli", return_value=settings),
            patch("taker.cli.ensure_config_file"),
            patch("taker.cli.resolve_mnemonic", return_value=resolved),
            patch("taker.cli.build_taker_config", return_value=config),
            patch(
                "taker.cli._run_coinjoin", new_callable=AsyncMock, side_effect=ValueError(marker)
            ),
        ):
            result = runner.invoke(app, ["coinjoin", "--amount", "100000"])
    finally:
        logger.remove(handler_id)

    assert result.exit_code == 1
    assert any(
        message == "Unexpected CoinJoin error" and not sensitive
        for message, sensitive, _ in records
    )
    assert any(sensitive and marker in exception for _, sensitive, exception in records)
    assert not any(not sensitive and marker in exception for _, sensitive, exception in records)


@pytest.mark.asyncio
async def test_coinjoin_success_prints_txid_and_tags_log_details(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from loguru import logger

    txid = "a" * 64
    settings = MagicMock()
    config = MagicMock()
    config.network.value = "regtest"
    config.bitcoin_network = None
    config.data_dir = MagicMock()
    config.mnemonic.get_secret_value.return_value = "test mnemonic"
    config.passphrase.get_secret_value.return_value = ""
    backend = MagicMock()
    backend.get_block_height = AsyncMock()
    taker = MagicMock()
    taker.nick = "J5test"
    taker.sync_wallet = AsyncMock()
    taker.check_utxo_eligibility = AsyncMock(return_value=None)
    taker.connect = AsyncMock()
    taker.do_coinjoin = AsyncMock(return_value=txid)
    taker.stop = AsyncMock()
    taker.last_broadcast_method = "self"
    taker.last_broadcast_fallback_reason = ""
    notifier = MagicMock()
    notifier.notify_startup = AsyncMock()
    records: list[tuple[str, dict[str, object]]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["message"], dict(message.record["extra"])))
    )
    try:
        with (
            patch("taker.cli.create_backend", return_value=backend),
            patch("taker.cli.WalletService"),
            patch("taker.cli.get_notifier", return_value=notifier),
            patch("taker.cli.write_nick_state"),
            patch("taker.cli.remove_nick_state"),
            patch("taker.taker.Taker", return_value=taker),
        ):
            await _run_coinjoin(
                settings=settings,
                config=config,
                amount=100_000,
                destination="bcrt1qdestination",
                mixdepth=0,
                counterparties=3,
                skip_confirmation=True,
            )
    finally:
        logger.remove(handler_id)

    assert txid in capsys.readouterr().out
    completion_records = [
        record for record in records if record[0].startswith("CoinJoin successful")
    ]
    assert ("CoinJoin successful", {}) in completion_records
    assert (f"CoinJoin successful: txid={txid}", {"sensitive": True}) in completion_records


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
        settings.network_config.allow_clearnet_connections = False
        settings.network_config.nick_auth_mode = NickAuthMode.PREFER_VERIFIED
        settings.network_config.nick_auth_directory_ids = {}

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
        settings.taker.max_sweep_fee_change = 0.8
        settings.taker.round_up_cj_fees = True
        settings.taker.require_quantized_cj_fees = False
        settings.taker.equalize_cj_fees = False
        settings.taker.fee_rate = None  # Not set in config
        settings.taker.fee_block_target = None  # Not set in config
        settings.taker.bondless_makers_allowance = 0.1
        settings.taker.bond_value_exponent = 1.3
        settings.taker.bondless_require_zero_fee = True
        settings.taker.tx_broadcast = "MULTIPLE_PEERS"
        settings.taker.broadcast_peer_count = 4
        settings.taker.minimum_makers = 4
        settings.taker.max_maker_replacement_attempts = 3
        settings.taker.tx_fee_factor = 0.2
        settings.taker.maker_timeout_sec = 60
        settings.taker.initial_confirmation_timeout_sec = 300
        settings.taker.order_wait_time = 10.0
        settings.taker.orderbook_min_wait = 30.0
        settings.taker.orderbook_quiet_period = 15.0
        settings.taker.rescan_interval_sec = 600
        settings.taker.taker_utxo_age = 5
        settings.taker.taker_utxo_retries = 3
        settings.taker.taker_utxo_amtpercent = 20
        settings.taker.external_podle_mode = "disabled"
        settings.taker.max_maker_utxos = 15

        # Wallet config
        settings.wallet.mixdepth_count = 5
        settings.wallet.gap_limit = 6
        settings.wallet.scan_range = 1000
        settings.wallet.dust_threshold = 27300
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

    def test_tx_fee_factor_override(self, sample_mnemonic: str, mock_settings: MagicMock) -> None:
        """A caller-supplied ``tx_fee_factor`` override wins over settings."""
        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            tx_fee_factor=0.5,
        )
        assert config.tx_fee_factor == 0.5

    def test_tx_fee_factor_defaults_to_settings(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )
        assert config.tx_fee_factor == 0.2

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

    def test_reconstruct_history_forwarded(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """The wallet history-reconstruction toggle must reach TakerConfig."""
        mock_settings.wallet.reconstruct_history = False
        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )
        assert config.reconstruct_history is False

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

    def test_max_maker_utxos_forwarded(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """``taker.max_maker_utxos`` must reach the TakerConfig.

        The cap bounds the mining fee a counterparty can force us to pay, so a
        setting that silently falls back to the default would be a security
        regression for anyone who tightened it.
        """
        mock_settings.taker.max_maker_utxos = 4

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.max_maker_utxos == 4

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

    def test_descriptor_scan_settings_reach_backend(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Descriptor scan settings must survive the taker config round trip."""
        mock_settings.wallet.scan_start_height = 765_432
        mock_settings.wallet.scan_lookback_blocks = 12_345

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.backend_config["scan_start_height"] == 765_432
        assert config.backend_config["scan_lookback_blocks"] == 12_345

        mock_backend = MagicMock()
        with patch(
            "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend",
            return_value=mock_backend,
        ) as mock_cls:
            create_backend(config)

        assert mock_cls.call_args is not None
        assert mock_cls.call_args.kwargs["scan_start_height"] == 765_432
        assert mock_cls.call_args.kwargs["scan_lookback_blocks"] == 12_345

    def test_neutrino_fee_source_in_backend_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """``bitcoin.fee_estimate_url`` and the Tor fee proxy must flow into
        backend_config so neutrino can estimate fees without a full node."""
        mock_settings.bitcoin.backend_type = "neutrino"
        mock_settings.get_neutrino_add_peers.return_value = []
        mock_settings.bitcoin.fee_estimate_url = "https://example.com/fee-estimates"
        mock_settings.tor.socks_host = "127.0.0.1"
        mock_settings.tor.socks_port = 9050
        mock_settings.tor.stream_isolation = False

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.backend_config.get("fee_estimate_url") == "https://example.com/fee-estimates"
        assert config.backend_config.get("fee_estimate_proxy") == "socks5h://127.0.0.1:9050"

    def test_neutrino_fee_source_kwargs_reach_backend(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """create_backend must forward the fee source kwargs to NeutrinoBackend."""
        mock_settings.bitcoin.backend_type = "neutrino"
        mock_settings.get_neutrino_add_peers.return_value = []
        mock_settings.bitcoin.fee_estimate_url = None
        mock_settings.tor.socks_host = "127.0.0.1"
        mock_settings.tor.socks_port = 9050
        mock_settings.tor.stream_isolation = False

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        mock_backend = MagicMock()
        with patch(
            "jmwallet.backends.neutrino.NeutrinoBackend", return_value=mock_backend
        ) as mock_cls:
            create_backend(config)

        _, kwargs = mock_cls.call_args
        assert kwargs["fee_estimate_url"] is None
        assert kwargs["fee_estimate_proxy"] == "socks5h://127.0.0.1:9050"

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

    def test_minimum_fee_policy_flows_from_settings(self, sample_mnemonic: str, tmp_path) -> None:
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings()
        settings.taker.min_fee_rate_sat_vb = 2.5
        settings.taker.min_fee_block_target = 20
        config = build_taker_config(
            settings=settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            data_dir=tmp_path,
        )

        assert config.min_fee_rate_sat_vb == 2.5
        assert config.min_fee_block_target == 20

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
            fee_estimate_url=None,
            fee_estimate_proxy=None,
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

    def test_external_podle_mode_flows_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        mock_settings.taker.external_podle_mode = "only"

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.external_podle_mode == "only"

    def test_initial_confirmation_timeout_flows_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        mock_settings.taker.initial_confirmation_timeout_sec = 900

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.initial_confirmation_timeout_sec == 900

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

    def test_nick_auth_mode_flows_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        mock_settings.network_config.nick_auth_mode = NickAuthMode.REQUIRE_VERIFIED

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.nick_auth_mode is NickAuthMode.REQUIRE_VERIFIED

    def test_clearnet_development_override_flows_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        mock_settings.network_config.allow_clearnet_connections = True

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.allow_clearnet_connections is True

    def test_nick_auth_directory_ids_flow_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        expected = {"directory.internal:5222": "test:directory-a"}
        mock_settings.network_config.nick_auth_directory_ids = expected

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.nick_auth_directory_ids == expected

    def test_max_sweep_fee_change_flow_and_override(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """max_sweep_fee_change flows from settings to TakerConfig and accepts override."""
        config_default = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )
        assert config_default.max_sweep_fee_change == 0.8

        config_override = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            max_sweep_fee_change=0.5,
        )
        assert config_override.max_sweep_fee_change == 0.5

    def test_fee_quantization_policy_flows_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        mock_settings.taker.round_up_cj_fees = False
        mock_settings.taker.require_quantized_cj_fees = True
        mock_settings.taker.equalize_cj_fees = True

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.round_up_cj_fees is False
        assert config.require_quantized_cj_fees is True
        assert config.equalize_cj_fees is True

        overridden = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
            equalize_cj_fees=False,
        )
        assert overridden.equalize_cj_fees is False

    def test_bondless_policy_flows_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        mock_settings.taker.bondless_makers_allowance = 0.05
        mock_settings.taker.bondless_require_zero_fee = True

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.bondless_makers_allowance == 0.05
        assert config.bondless_makers_allowance_require_zero_fee is True

    def test_max_maker_replacement_attempts_flows_into_config(
        self, sample_mnemonic: str, mock_settings: MagicMock
    ) -> None:
        """Replacement attempt settings reach the runtime taker config."""
        mock_settings.taker.max_maker_replacement_attempts = 7

        config = build_taker_config(
            settings=mock_settings,
            mnemonic=sample_mnemonic,
            passphrase="",
            destination="bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
            amount=100000,
            mixdepth=0,
        )

        assert config.max_maker_replacement_attempts == 7
