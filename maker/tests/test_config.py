"""
Tests for maker configuration validation.
"""

from pathlib import Path

import pytest
from jmcore.models import OfferType
from pydantic import ValidationError

from maker.config import MakerConfig, MergeAlgorithm, OfferConfig, TorControlConfig

# Test mnemonic (BIP39 test vector)
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)


def test_valid_config() -> None:
    """Test that valid configuration is accepted."""
    config = MakerConfig(
        mnemonic=TEST_MNEMONIC,
        cj_fee_relative="0.001",
        offer_type=OfferType.SW0_RELATIVE,
    )
    assert config.cj_fee_relative == "0.001"


def test_zero_cj_fee_relative_fails() -> None:
    """Test that zero cj_fee_relative fails for relative offer types."""
    with pytest.raises(ValidationError, match="cj_fee_relative must be > 0"):
        MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="0",
            offer_type=OfferType.SW0_RELATIVE,
        )


def test_negative_cj_fee_relative_fails() -> None:
    """Test that negative cj_fee_relative fails for relative offer types."""
    with pytest.raises(ValidationError, match="cj_fee_relative must be > 0"):
        MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="-0.001",
            offer_type=OfferType.SW0_RELATIVE,
        )


def test_invalid_cj_fee_relative_string_fails() -> None:
    """Test that invalid string for cj_fee_relative fails."""
    with pytest.raises(ValidationError, match="cj_fee_relative must be a valid number"):
        MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="not_a_number",
            offer_type=OfferType.SW0_RELATIVE,
        )


def test_zero_cj_fee_relative_ok_for_absolute_offers() -> None:
    """Test that zero cj_fee_relative is OK for absolute offer types."""
    config = MakerConfig(
        mnemonic=TEST_MNEMONIC,
        cj_fee_relative="0",
        offer_type=OfferType.SW0_ABSOLUTE,
        cj_fee_absolute=500,
    )
    assert config.cj_fee_relative == "0"
    assert config.offer_type == OfferType.SW0_ABSOLUTE


class TestTorControlConfig:
    """Tests for TorControlConfig."""

    def test_default_values(self) -> None:
        """Test default values are applied."""
        config = TorControlConfig()
        assert config.enabled is True
        assert config.host == "127.0.0.1"
        assert config.port == 9051
        assert config.cookie_path is None
        assert config.password is None

    def test_with_cookie_path(self, tmp_path: Path) -> None:
        """Test configuration with cookie path."""
        cookie_path = tmp_path / "control_auth_cookie"
        config = TorControlConfig(
            enabled=True,
            cookie_path=cookie_path,
        )
        assert config.enabled is True
        assert config.cookie_path == cookie_path

    def test_with_password(self) -> None:
        """Test configuration with password."""
        config = TorControlConfig(
            enabled=True,
            password="mysecret",
        )
        assert config.enabled is True
        assert config.password.get_secret_value() == "mysecret"


class TestMakerConfigTorControl:
    """Tests for MakerConfig tor_control integration."""

    def test_default_tor_control(self) -> None:
        """Test that tor_control defaults to enabled."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
        )
        assert config.tor_control.enabled is True

    def test_tor_control_enabled(self, tmp_path: Path) -> None:
        """Test enabling tor_control via nested config."""
        cookie_path = tmp_path / "control_auth_cookie"
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            tor_control=TorControlConfig(
                enabled=True,
                host="127.0.0.1",
                port=9051,
                cookie_path=cookie_path,
            ),
        )
        assert config.tor_control.enabled is True
        assert config.tor_control.port == 9051
        assert config.tor_control.cookie_path == cookie_path

    def test_tor_control_from_dict(self) -> None:
        """Test creating config from dict (JSON/YAML parsing)."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            tor_control={
                "enabled": True,
                "host": "tor",
                "port": 9051,
                "cookie_path": "/var/lib/tor/control_auth_cookie",
            },  # type: ignore[arg-type]
        )
        assert config.tor_control.enabled is True
        assert config.tor_control.host == "tor"
        assert config.tor_control.cookie_path == Path("/var/lib/tor/control_auth_cookie")


class TestMergeAlgorithm:
    """Tests for MergeAlgorithm configuration."""

    def test_default_merge_algorithm(self) -> None:
        """Test that default merge algorithm is 'default'."""
        config = MakerConfig(mnemonic=TEST_MNEMONIC)
        assert config.merge_algorithm == MergeAlgorithm.DEFAULT

    def test_set_merge_algorithm_gradual(self) -> None:
        """Test setting merge algorithm to gradual."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            merge_algorithm=MergeAlgorithm.GRADUAL,
        )
        assert config.merge_algorithm == MergeAlgorithm.GRADUAL

    def test_set_merge_algorithm_greedy(self) -> None:
        """Test setting merge algorithm to greedy."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            merge_algorithm=MergeAlgorithm.GREEDY,
        )
        assert config.merge_algorithm == MergeAlgorithm.GREEDY

    def test_set_merge_algorithm_random(self) -> None:
        """Test setting merge algorithm to random."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            merge_algorithm=MergeAlgorithm.RANDOM,
        )
        assert config.merge_algorithm == MergeAlgorithm.RANDOM

    def test_merge_algorithm_from_string(self) -> None:
        """Test creating config with string value (JSON/YAML parsing)."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            merge_algorithm="greedy",  # type: ignore[arg-type]
        )
        assert config.merge_algorithm == MergeAlgorithm.GREEDY

    def test_merge_algorithm_value(self) -> None:
        """Test accessing the string value of the enum."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            merge_algorithm=MergeAlgorithm.GRADUAL,
        )
        assert config.merge_algorithm.value == "gradual"

    def test_invalid_merge_algorithm(self) -> None:
        """Test that invalid merge algorithm raises error."""
        with pytest.raises(ValidationError):
            MakerConfig(
                mnemonic=TEST_MNEMONIC,
                merge_algorithm="invalid_algo",  # type: ignore[arg-type]
            )


class TestCjFeeRelativeNormalization:
    """Tests for cj_fee_relative scientific notation normalization."""

    def test_float_converted_to_decimal_notation(self) -> None:
        """Test that float values are converted to decimal notation, not scientific."""
        # When pydantic coerces a float like 0.00001 to str, it becomes "1e-05"
        # Our validator should normalize this to "0.00001"
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative=0.00001,  # type: ignore[arg-type]
        )
        assert config.cj_fee_relative == "0.00001"
        assert "e" not in config.cj_fee_relative.lower()

    def test_scientific_notation_string_normalized(self) -> None:
        """Test that scientific notation strings are normalized to decimal."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="1e-05",
        )
        assert config.cj_fee_relative == "0.00001"
        assert "e" not in config.cj_fee_relative.lower()

    def test_uppercase_scientific_notation_normalized(self) -> None:
        """Test that uppercase scientific notation is also handled."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="1E-05",
        )
        assert config.cj_fee_relative == "0.00001"

    def test_regular_decimal_unchanged(self) -> None:
        """Test that regular decimal strings pass through unchanged."""
        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="0.001",
        )
        assert config.cj_fee_relative == "0.001"

    def test_offer_config_normalizes_float(self) -> None:
        """Test that OfferConfig also normalizes float values."""
        config = OfferConfig(
            cj_fee_relative=0.00001,  # type: ignore[arg-type]
        )
        assert config.cj_fee_relative == "0.00001"
        assert "e" not in config.cj_fee_relative.lower()

    def test_offer_config_normalizes_scientific_string(self) -> None:
        """Test that OfferConfig normalizes scientific notation strings."""
        config = OfferConfig(
            cj_fee_relative="1e-5",
        )
        assert config.cj_fee_relative == "0.00001"

    def test_various_small_values(self) -> None:
        """Test normalization for various small fee values."""
        test_cases = [
            (0.0001, "0.0001"),
            (0.00001, "0.00001"),
            (0.000001, "0.000001"),
            ("1e-4", "0.0001"),
            ("1e-5", "0.00001"),
            ("1e-6", "0.000001"),
            ("2.5e-5", "0.000025"),
        ]
        for input_val, expected in test_cases:
            config = OfferConfig(
                cj_fee_relative=input_val,  # type: ignore[arg-type]
            )
            assert config.cj_fee_relative == expected, f"Failed for {input_val}"
            assert "e" not in config.cj_fee_relative.lower()

    def test_integer_input_normalized(self) -> None:
        """Test that integer inputs are converted to string."""
        config = OfferConfig(
            cj_fee_relative=1,  # type: ignore[arg-type]
        )
        # Integer 1 should become "1"
        assert config.cj_fee_relative == "1"


class TestBuildMakerConfig:
    """Tests for build_maker_config function."""

    def test_absolute_fee_cli_sets_offer_type(self) -> None:
        """Test that --cj-fee-absolute on CLI sets offer_type to absolute."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
            cj_fee_absolute=1000,  # CLI override
        )
        assert config.offer_type == OfferType.SW0_ABSOLUTE
        assert config.cj_fee_absolute == 1000

    def test_relative_fee_cli_sets_offer_type(self) -> None:
        """Test that --cj-fee-relative on CLI sets offer_type to relative."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
            cj_fee_relative="0.002",  # CLI override
        )
        assert config.offer_type == OfferType.SW0_RELATIVE
        assert config.cj_fee_relative == "0.002"

    def test_dual_offers_creates_two_configs(self) -> None:
        """Test that --dual-offers creates both relative and absolute offer configs."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
            dual_offers=True,
        )
        assert len(config.offer_configs) == 2
        assert config.offer_configs[0].offer_type == OfferType.SW0_RELATIVE
        assert config.offer_configs[1].offer_type == OfferType.SW0_ABSOLUTE

    def test_dual_offers_with_custom_fees(self) -> None:
        """Test that --dual-offers uses custom fee values from CLI."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
            dual_offers=True,
            cj_fee_relative="0.005",
            cj_fee_absolute=2000,
        )
        assert len(config.offer_configs) == 2
        # Both configs have both fee values, but offer_type determines which is used
        assert config.offer_configs[0].cj_fee_relative == "0.005"
        assert config.offer_configs[1].cj_fee_absolute == 2000

    def test_both_fees_without_dual_offers_raises(self) -> None:
        """Test that specifying both fees without --dual-offers raises error."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        with pytest.raises(ValueError, match="Cannot specify both"):
            build_maker_config(
                settings=settings,
                mnemonic=TEST_MNEMONIC,
                passphrase="",
                cj_fee_relative="0.001",
                cj_fee_absolute=500,
            )

    def test_no_cli_overrides_uses_settings_offer_type(self) -> None:
        """Test that without CLI overrides, settings.maker.offer_type is used."""
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings()
        # Default offer_type is sw0reloffer

        from maker.cli import build_maker_config

        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
        )
        assert config.offer_type == OfferType.SW0_RELATIVE
        assert config.cj_fee_relative == settings.maker.cj_fee_relative

    def test_no_fidelity_bond_sets_flag(self) -> None:
        """Test that no_fidelity_bond=True is stored in the config."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
            no_fidelity_bond=True,
        )
        assert config.no_fidelity_bond is True

    def test_no_fidelity_bond_false_by_default(self) -> None:
        """Test that no_fidelity_bond defaults to False."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
        )
        assert config.no_fidelity_bond is False

    def test_no_fidelity_bond_with_locktime_raises(self) -> None:
        """Test that combining no_fidelity_bond with fidelity_bond_locktimes raises ValueError."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        with pytest.raises(ValueError, match="--no-fidelity-bond cannot be combined"):
            build_maker_config(
                settings=settings,
                mnemonic=TEST_MNEMONIC,
                passphrase="",
                no_fidelity_bond=True,
                fidelity_bond_locktimes=[1700000000],
            )

    def test_no_fidelity_bond_with_index_raises(self) -> None:
        """Test that combining no_fidelity_bond with fidelity_bond_index raises ValueError."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        with pytest.raises(ValueError, match="--no-fidelity-bond cannot be combined"):
            build_maker_config(
                settings=settings,
                mnemonic=TEST_MNEMONIC,
                passphrase="",
                no_fidelity_bond=True,
                fidelity_bond_index=0,
                fidelity_bond_locktimes=[1700000000],
            )

    def test_allow_mixdepth_zero_merge_passed_from_settings(self) -> None:
        """Test that allow_mixdepth_zero_merge is passed from settings to MakerConfig.

        Regression: the setting was defined on MakerSettings but never wired
        through build_maker_config, so the user's config was silently ignored.
        """
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        # Default should be False
        settings = JoinMarketSettings()
        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
        )
        assert config.allow_mixdepth_zero_merge is False

        # When enabled in settings, it should propagate
        settings.maker.allow_mixdepth_zero_merge = True
        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
        )
        assert config.allow_mixdepth_zero_merge is True

    def test_neutrino_tls_and_auth_in_backend_config(self) -> None:
        """Test that neutrino TLS cert and auth token flow into maker backend_config."""
        from jmcore.settings import JoinMarketSettings

        from maker.cli import build_maker_config

        settings = JoinMarketSettings()
        settings.bitcoin.backend_type = "neutrino"
        settings.bitcoin.neutrino_url = "https://127.0.0.1:8334"
        settings.bitcoin.neutrino_tls_cert = "/tmp/neutrino/tls.cert"
        settings.bitcoin.neutrino_auth_token = "token-123"

        config = build_maker_config(
            settings=settings,
            mnemonic=TEST_MNEMONIC,
            passphrase="",
        )

        assert config.backend_type == "neutrino"
        assert config.backend_config.get("tls_cert_path") == "/tmp/neutrino/tls.cert"
        assert config.backend_config.get("auth_token") == "token-123"


class TestCreateWalletService:
    """Tests for create_wallet_service function.

    No mocking needed: BitcoinCoreBackend.__init__ only stores params and creates
    httpx clients (no network calls), and WalletService.__init__ only derives keys.
    """

    def test_data_dir_passed_to_wallet_service(self, tmp_path: Path) -> None:
        """Verify create_wallet_service passes data_dir so metadata_store is initialized.

        Regression test: maker was creating WalletService without data_dir,
        which meant metadata_store was None and frozen UTXOs were ignored.
        """
        from maker.cli import create_wallet_service

        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="0.001",
            data_dir=tmp_path,
            backend_type="scantxoutset",
            backend_config={
                "rpc_url": "http://127.0.0.1:18443",
                "rpc_user": "test",
                "rpc_password": "test",
            },
        )

        wallet = create_wallet_service(config)

        assert wallet.data_dir == tmp_path
        assert wallet.metadata_store is not None

    def test_data_dir_none_still_works(self) -> None:
        """Verify create_wallet_service works when data_dir is None (no metadata)."""
        from maker.cli import create_wallet_service

        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="0.001",
            data_dir=None,
            backend_type="scantxoutset",
            backend_config={
                "rpc_url": "http://127.0.0.1:18443",
                "rpc_user": "test",
                "rpc_password": "test",
            },
        )

        wallet = create_wallet_service(config)

        assert wallet.data_dir is None
        assert wallet.metadata_store is None

    def test_neutrino_backend_receives_tls_and_auth(self, tmp_path: Path) -> None:
        """create_wallet_service() passes TLS cert and auth token to NeutrinoBackend."""
        from unittest.mock import MagicMock, patch

        from maker.cli import create_wallet_service

        config = MakerConfig(
            mnemonic=TEST_MNEMONIC,
            cj_fee_relative="0.001",
            data_dir=tmp_path,
            backend_type="neutrino",
            backend_config={
                "neutrino_url": "https://127.0.0.1:8334",
                "add_peers": ["bitcoin.sgn.space:38333"],
                "scan_start_height": 123,
                "tls_cert_path": "/tmp/neutrino/tls.cert",
                "auth_token": "token-123",
            },
        )

        mock_backend = MagicMock()
        with patch(
            "jmwallet.backends.neutrino.NeutrinoBackend", return_value=mock_backend
        ) as mock_cls:
            wallet = create_wallet_service(config)

        mock_cls.assert_called_once_with(
            neutrino_url="https://127.0.0.1:8334",
            network="mainnet",
            add_peers=["bitcoin.sgn.space:38333"],
            data_dir="/data/neutrino",
            scan_start_height=123,
            tls_cert_path="/tmp/neutrino/tls.cert",
            auth_token="token-123",
        )
        assert wallet.backend is mock_backend
