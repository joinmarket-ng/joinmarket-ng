"""
Tests for the unified settings module.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from jmcore.constants import DUST_THRESHOLD
from jmcore.models import NetworkType
from jmcore.settings import (
    JoinMarketSettings,
    MakerSettings,
    ensure_config_file,
    generate_config_template,
    get_config_path,
    get_settings,
    reset_settings,
)


@pytest.fixture(autouse=True)
def reset_settings_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Reset settings before and after each test.

    Also redirects JOINMARKET_DATA_DIR to an empty temp directory so that
    tests that do not explicitly write a config.toml always see the defaults,
    regardless of the developer's live ~/.joinmarket-ng/config.toml.
    """
    empty_data_dir = tmp_path / ".joinmarket-ng-defaults"
    empty_data_dir.mkdir(parents=True)
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(empty_data_dir))
    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary data directory and set it as JOINMARKET_DATA_DIR."""
    data_dir = tmp_path / ".joinmarket-ng"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(data_dir))
    return data_dir


class TestConfigTemplate:
    """Tests for config template generation."""

    def test_generate_config_template(self) -> None:
        """Test that config template is generated correctly."""
        template = generate_config_template()

        # Check header
        assert "# JoinMarket NG Configuration" in template
        assert "# Priority (highest to lowest):" in template

        # Check sections exist
        assert "[tor]" in template
        assert "[bitcoin]" in template
        assert "[network_config]" in template
        assert "[wallet]" in template
        assert "[notifications]" in template
        assert "[maker]" in template
        assert "[taker]" in template
        assert "[directory_server]" in template
        assert "[orderbook_watcher]" in template

        # Check that settings are commented out
        assert "# socks_host = " in template
        assert "# socks_port = " in template
        assert "# rpc_url = " in template

    def test_ensure_config_file_creates_template(self, temp_data_dir: Path) -> None:
        """Test that ensure_config_file creates the config file."""
        config_path = temp_data_dir / "config.toml"
        assert not config_path.exists()

        result = ensure_config_file(temp_data_dir)

        assert result == config_path
        assert config_path.exists()
        content = config_path.read_text()
        assert "# JoinMarket NG Configuration" in content

    def test_ensure_config_file_does_not_overwrite(self, temp_data_dir: Path) -> None:
        """Test that ensure_config_file does not overwrite existing file."""
        config_path = temp_data_dir / "config.toml"
        config_path.write_text("# Custom config\ntor.socks_host = 'custom'\n")

        ensure_config_file(temp_data_dir)

        content = config_path.read_text()
        assert "# Custom config" in content
        assert "custom" in content


class TestSettingsDefaults:
    """Tests for default settings values."""

    def test_default_tor_settings(self) -> None:
        """Test default Tor settings."""
        settings = JoinMarketSettings()

        assert settings.tor.socks_host == "127.0.0.1"
        assert settings.tor.socks_port == 9050

    def test_default_bitcoin_settings(self) -> None:
        """Test default Bitcoin settings."""
        settings = JoinMarketSettings()

        assert settings.bitcoin.backend_type == "descriptor_wallet"
        assert settings.bitcoin.rpc_url == "http://127.0.0.1:8332"
        assert settings.bitcoin.rpc_user == ""
        assert settings.bitcoin.rpc_password.get_secret_value() == ""

    def test_default_network_settings(self) -> None:
        """Test default network settings."""
        settings = JoinMarketSettings()

        assert settings.network_config.network == NetworkType.MAINNET
        assert settings.network_config.bitcoin_network is None
        assert settings.network_config.directory_servers == []

    def test_default_wallet_settings(self) -> None:
        """Test default wallet settings."""
        settings = JoinMarketSettings()

        assert settings.wallet.mixdepth_count == 5
        assert settings.wallet.gap_limit == 20
        assert settings.wallet.dust_threshold == 27300

    def test_default_maker_settings(self) -> None:
        """Test default maker settings."""
        settings = JoinMarketSettings()

        assert settings.maker.min_size == DUST_THRESHOLD
        assert settings.maker.offer_type == "sw0reloffer"
        assert settings.maker.cj_fee_relative == "0.001"
        assert settings.maker.cj_fee_absolute == 500
        assert settings.maker.merge_algorithm == "default"

    def test_default_taker_settings(self) -> None:
        """Test default taker settings."""
        settings = JoinMarketSettings()

        assert settings.taker.counterparty_count == 10
        assert settings.taker.max_cj_fee_abs == 500
        assert settings.taker.max_cj_fee_rel == "0.001"
        assert settings.taker.tx_broadcast == "random-peer"


class TestSettingsFromEnv:
    """Tests for loading settings from environment variables."""

    def test_env_override_tor_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override Tor settings."""
        monkeypatch.setenv("TOR__SOCKS_HOST", "tor")
        monkeypatch.setenv("TOR__SOCKS_PORT", "9150")

        settings = JoinMarketSettings()

        assert settings.tor.socks_host == "tor"
        assert settings.tor.socks_port == 9150

    def test_env_override_bitcoin_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override Bitcoin settings."""
        monkeypatch.setenv("BITCOIN__RPC_URL", "http://bitcoind:8332")
        monkeypatch.setenv("BITCOIN__RPC_USER", "jm")
        monkeypatch.setenv("BITCOIN__RPC_PASSWORD", "secret")

        settings = JoinMarketSettings()

        assert settings.bitcoin.rpc_url == "http://bitcoind:8332"
        assert settings.bitcoin.rpc_user == "jm"
        assert settings.bitcoin.rpc_password.get_secret_value() == "secret"

    def test_env_override_network_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override network settings."""
        monkeypatch.setenv("NETWORK_CONFIG__NETWORK", "signet")

        settings = JoinMarketSettings()

        assert settings.network_config.network == NetworkType.SIGNET

    def test_env_override_maker_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override maker settings."""
        monkeypatch.setenv("MAKER__MIN_SIZE", "50000")
        monkeypatch.setenv("MAKER__CJ_FEE_RELATIVE", "0.002")
        monkeypatch.setenv("MAKER__MERGE_ALGORITHM", "greedy")

        settings = JoinMarketSettings()

        assert settings.maker.min_size == 50000
        assert settings.maker.cj_fee_relative == "0.002"
        assert settings.maker.merge_algorithm == "greedy"

    def test_env_override_maker_offer_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables can set maker offer_type."""
        monkeypatch.setenv("MAKER__OFFER_TYPE", "sw0absoffer")

        settings = JoinMarketSettings()

        assert settings.maker.offer_type == "sw0absoffer"


class TestSettingsFromToml:
    """Tests for loading settings from TOML config file."""

    def test_toml_override_settings(
        self, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that TOML config file overrides default settings."""
        config_path = temp_data_dir / "config.toml"
        config_path.write_text("""
[tor]
socks_host = "tor-proxy"
socks_port = 9055

[bitcoin]
rpc_url = "http://my-bitcoin:8332"
backend_type = "neutrino"

[maker]
min_size = 200000
""")

        settings = JoinMarketSettings()

        assert settings.tor.socks_host == "tor-proxy"
        assert settings.tor.socks_port == 9055
        assert settings.bitcoin.rpc_url == "http://my-bitcoin:8332"
        assert settings.bitcoin.backend_type == "neutrino"
        assert settings.maker.min_size == 200000

    def test_toml_maker_offer_type_absolute(
        self, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that offer_type can be set to absolute via TOML config."""
        config_path = temp_data_dir / "config.toml"
        config_path.write_text("""
[maker]
offer_type = "sw0absoffer"
cj_fee_absolute = 1000
""")

        settings = JoinMarketSettings()

        assert settings.maker.offer_type == "sw0absoffer"
        assert settings.maker.cj_fee_absolute == 1000

    def test_env_overrides_toml(self, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override TOML config."""
        config_path = temp_data_dir / "config.toml"
        config_path.write_text("""
[tor]
socks_host = "tor-proxy"
socks_port = 9055
""")

        # Environment should override TOML
        monkeypatch.setenv("TOR__SOCKS_HOST", "env-tor")

        settings = JoinMarketSettings()

        # Environment wins
        assert settings.tor.socks_host == "env-tor"
        # TOML value is used when no env override
        assert settings.tor.socks_port == 9055

    def test_invalid_toml_exits(self, temp_data_dir: Path) -> None:
        """Test that invalid TOML syntax causes exit."""
        config_path = temp_data_dir / "config.toml"
        # Missing closing bracket
        config_path.write_text('[bitcoin\nbackend_type = "neutrino"')

        with pytest.raises(SystemExit) as exc_info:
            JoinMarketSettings()

        assert exc_info.value.code == 1


class TestDirectoryServers:
    """Tests for directory server configuration."""

    def test_default_directory_servers_mainnet(self) -> None:
        """Test that mainnet has default directory servers."""
        settings = JoinMarketSettings()

        servers = settings.get_directory_servers()
        assert len(servers) >= 2
        assert all(".onion:" in s for s in servers)

    def test_custom_directory_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test custom directory servers."""
        # Use init override
        settings = JoinMarketSettings(
            network_config={"directory_servers": ["custom1.onion:5222", "custom2.onion:5222"]}
        )

        servers = settings.get_directory_servers()
        assert servers == ["custom1.onion:5222", "custom2.onion:5222"]

    def test_signet_directory_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test signet network directory servers (currently empty, must be user-configured)."""
        settings = JoinMarketSettings(network_config={"network": "signet"})

        servers = settings.get_directory_servers()
        assert len(servers) >= 1
        assert all(".onion:" in s for s in servers)


class TestGetSettings:
    """Tests for the get_settings helper function."""

    def test_get_settings_caches(self) -> None:
        """Test that get_settings returns cached instance."""
        settings1 = get_settings()
        settings2 = get_settings()

        assert settings1 is settings2

    def test_get_settings_with_overrides(self) -> None:
        """Test that get_settings with overrides creates new instance."""
        settings1 = get_settings()
        settings2 = get_settings(tor={"socks_host": "new-host"})

        # With overrides, we get a new instance
        assert settings1 is not settings2
        assert settings2.tor.socks_host == "new-host"

    def test_reset_settings(self) -> None:
        """Test that reset_settings clears the cache."""
        settings1 = get_settings()
        reset_settings()
        settings2 = get_settings()

        assert settings1 is not settings2


class TestConfigPath:
    """Tests for config path resolution."""

    def test_default_config_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test default config path is in home directory."""
        # Clear any existing env var
        monkeypatch.delenv("JOINMARKET_DATA_DIR", raising=False)
        monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)

        config_path = get_config_path()
        assert config_path == Path.home() / ".joinmarket-ng" / "config.toml"

    def test_custom_data_dir_config_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test config path with custom data directory."""
        monkeypatch.setenv("JOINMARKET_DATA_DIR", "/custom/data")
        monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)

        config_path = get_config_path()
        assert config_path == Path("/custom/data/config.toml")


class TestMakerSettingsCjFeeNormalization:
    """Tests for MakerSettings cj_fee_relative scientific notation normalization."""

    def test_float_converted_to_decimal_notation(self) -> None:
        """Test that float values are converted to decimal notation."""
        settings = MakerSettings(cj_fee_relative=0.00001)  # type: ignore[arg-type]
        assert settings.cj_fee_relative == "0.00001"
        assert "e" not in settings.cj_fee_relative.lower()

    def test_scientific_notation_string_normalized(self) -> None:
        """Test that scientific notation strings are normalized."""
        settings = MakerSettings(cj_fee_relative="1e-05")
        assert settings.cj_fee_relative == "0.00001"
        assert "e" not in settings.cj_fee_relative.lower()

    def test_toml_float_normalized(
        self, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that TOML float values are normalized to decimal notation."""
        config_path = temp_data_dir / "config.toml"
        # TOML parses 0.00001 as a float, which could become "1e-05" when stringified
        config_path.write_text("""
[maker]
cj_fee_relative = 0.00001
""")

        settings = JoinMarketSettings()

        assert settings.maker.cj_fee_relative == "0.00001"
        assert "e" not in settings.maker.cj_fee_relative.lower()

    def test_env_var_float_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variable float values are normalized."""
        # When set via env var, the value comes as a string
        monkeypatch.setenv("MAKER__CJ_FEE_RELATIVE", "1e-05")

        settings = JoinMarketSettings()

        assert settings.maker.cj_fee_relative == "0.00001"
        assert "e" not in settings.maker.cj_fee_relative.lower()

    def test_various_small_values(self) -> None:
        """Test normalization for various small fee values."""
        test_cases = [
            (0.0001, "0.0001"),
            (0.00001, "0.00001"),
            ("1e-4", "0.0001"),
            ("1e-5", "0.00001"),
            ("2.5e-5", "0.000025"),
        ]
        for input_val, expected in test_cases:
            settings = MakerSettings(cj_fee_relative=input_val)  # type: ignore[arg-type]
            assert settings.cj_fee_relative == expected, f"Failed for {input_val}"
