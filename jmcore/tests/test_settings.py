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
    BitcoinSettings,
    JoinMarketSettings,
    MakerSettings,
    NetworkSettings,
    TakerSettings,
    _get_user_sections,
    config_diff,
    ensure_config_file,
    generate_config_template,
    get_config_path,
    get_settings,
    migrate_config,
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
    data_dir.mkdir(parents=True)
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
        assert "# JoinMarket" in content
        assert "[tor]" in content

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

    def test_default_bitcoin_neutrino_settings(self) -> None:
        """Test default Bitcoin neutrino-specific settings."""
        settings = JoinMarketSettings()

        assert settings.bitcoin.neutrino_clearnet_initial_sync is True
        assert settings.bitcoin.neutrino_prefetch_filters is True
        assert settings.bitcoin.neutrino_prefetch_lookback_blocks == 105120
        assert settings.bitcoin.neutrino_scan_lookback_blocks == 105120

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

    def test_env_override_neutrino_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that environment variables override neutrino-specific settings."""
        monkeypatch.setenv("BITCOIN__NEUTRINO_CLEARNET_INITIAL_SYNC", "false")
        monkeypatch.setenv("BITCOIN__NEUTRINO_PREFETCH_FILTERS", "true")
        monkeypatch.setenv("BITCOIN__NEUTRINO_PREFETCH_LOOKBACK_BLOCKS", "50000")
        monkeypatch.setenv("BITCOIN__NEUTRINO_SCAN_LOOKBACK_BLOCKS", "75000")

        settings = JoinMarketSettings()

        assert settings.bitcoin.neutrino_clearnet_initial_sync is False
        assert settings.bitcoin.neutrino_prefetch_filters is True
        assert settings.bitcoin.neutrino_prefetch_lookback_blocks == 50000
        assert settings.bitcoin.neutrino_scan_lookback_blocks == 75000


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

    def test_toml_neutrino_settings(
        self, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that neutrino settings can be loaded from TOML config."""
        config_path = temp_data_dir / "config.toml"
        config_path.write_text("""
[bitcoin]
backend_type = "neutrino"
neutrino_clearnet_initial_sync = false
neutrino_prefetch_filters = true
neutrino_prefetch_lookback_blocks = 50000
neutrino_scan_lookback_blocks = 75000
""")

        settings = JoinMarketSettings()

        assert settings.bitcoin.backend_type == "neutrino"
        assert settings.bitcoin.neutrino_clearnet_initial_sync is False
        assert settings.bitcoin.neutrino_prefetch_filters is True
        assert settings.bitcoin.neutrino_prefetch_lookback_blocks == 50000
        assert settings.bitcoin.neutrino_scan_lookback_blocks == 75000

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

    def test_invalid_scientific_notation_passthrough(self) -> None:
        """Invalid scientific notation string is passed through for pydantic validation."""
        # "not_a_number_e5" contains 'e' but is not valid Decimal
        settings = MakerSettings(cj_fee_relative="not_a_number_e5")
        # Should be passed through as-is (pydantic doesn't enforce numeric strings on str field)
        assert settings.cj_fee_relative == "not_a_number_e5"


class TestTakerSettingsMaxCjFeeRelNormalization:
    """Tests for TakerSettings max_cj_fee_rel scientific notation normalization."""

    def test_float_converted_to_decimal_notation(self) -> None:
        """Float values are converted to decimal notation."""
        settings = TakerSettings(max_cj_fee_rel=0.00001)  # type: ignore[arg-type]
        assert settings.max_cj_fee_rel == "0.00001"
        assert "e" not in settings.max_cj_fee_rel.lower()

    def test_scientific_notation_string_normalized(self) -> None:
        """Scientific notation strings are normalized."""
        settings = TakerSettings(max_cj_fee_rel="1e-05")
        assert settings.max_cj_fee_rel == "0.00001"

    def test_various_small_values(self) -> None:
        """Normalization for various small fee values."""
        test_cases = [
            (0.0001, "0.0001"),
            (0.00001, "0.00001"),
            ("1e-4", "0.0001"),
            ("1e-5", "0.00001"),
            ("1E-9", "0.000000001"),
        ]
        for input_val, expected in test_cases:
            settings = TakerSettings(max_cj_fee_rel=input_val)  # type: ignore[arg-type]
            assert settings.max_cj_fee_rel == expected, f"Failed for {input_val}"

    def test_normal_string_unchanged(self) -> None:
        """Normal decimal string is not modified."""
        settings = TakerSettings(max_cj_fee_rel="0.001")
        assert settings.max_cj_fee_rel == "0.001"


class TestParseDirectoryServers:
    """Tests for NetworkSettings.parse_directory_servers validator."""

    def test_json_list_string(self) -> None:
        """JSON array string should be parsed."""
        settings = NetworkSettings(directory_servers='["host1:5222", "host2:5222"]')
        assert settings.directory_servers == ["host1:5222", "host2:5222"]

    def test_json_single_string(self) -> None:
        """JSON single string should be parsed."""
        settings = NetworkSettings(directory_servers='"host1:5222"')
        assert settings.directory_servers == ["host1:5222"]

    def test_comma_separated_string(self) -> None:
        """Comma-separated plain string should be parsed."""
        settings = NetworkSettings(directory_servers="host1:5222,host2:5222")
        assert settings.directory_servers == ["host1:5222", "host2:5222"]

    def test_single_plain_string(self) -> None:
        """Single plain string should be parsed as one-element list."""
        settings = NetworkSettings(directory_servers="host1:5222")
        assert settings.directory_servers == ["host1:5222"]

    def test_list_passthrough(self) -> None:
        """An actual list should pass through unchanged."""
        settings = NetworkSettings(directory_servers=["host1:5222"])
        assert settings.directory_servers == ["host1:5222"]

    def test_empty_json_string(self) -> None:
        """JSON empty string should produce empty list."""
        settings = NetworkSettings(directory_servers='""')
        assert settings.directory_servers == []


class TestJoinMarketSettingsHelpers:
    """Tests for JoinMarketSettings helper methods."""

    def test_get_data_dir_with_explicit(
        self, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_data_dir returns explicit data_dir when set."""
        settings = JoinMarketSettings(data_dir=temp_data_dir)
        assert settings.get_data_dir() == temp_data_dir

    def test_get_data_dir_default(self) -> None:
        """get_data_dir returns default when not set."""
        settings = JoinMarketSettings()
        result = settings.get_data_dir()
        assert isinstance(result, Path)

    def test_get_neutrino_add_peers(self) -> None:
        """get_neutrino_add_peers returns configured peers."""
        settings = JoinMarketSettings()
        peers = settings.get_neutrino_add_peers()
        assert isinstance(peers, list)


class TestConfigPathEnvVar:
    """Tests for JOINMARKET_CONFIG_FILE environment variable."""

    def test_explicit_config_file_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """JOINMARKET_CONFIG_FILE should override default config path."""
        config_file = tmp_path / "custom_config.toml"
        config_file.write_text("[tor]\nsocks_port = 9999\n")
        monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(config_file))

        settings = JoinMarketSettings()
        assert settings.tor.socks_port == 9999

    def test_config_file_not_found_uses_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-existent config file should use defaults."""
        monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "nonexistent.toml"))
        settings = JoinMarketSettings()
        # Should still work with defaults
        assert settings.tor.socks_host == "127.0.0.1"


class TestTomlLoadErrorHandling:
    """Tests for TOML config loading error handling."""

    def test_generic_exception_exits(
        self, temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file that causes a non-TOML error during loading should exit."""
        config_path = temp_data_dir / "config.toml"
        # Write binary garbage that won't parse as TOML
        config_path.write_bytes(b"\x00\x01\x02\x03")

        with pytest.raises(SystemExit) as exc_info:
            JoinMarketSettings()
        assert exc_info.value.code == 1


class TestCommaListEnvSettingsSource:
    """Tests for _CommaListEnvSettingsSource."""

    def test_comma_separated_directory_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Comma-separated env var for list[str] field should work."""
        monkeypatch.setenv("NETWORK_CONFIG__DIRECTORY_SERVERS", "host1.onion:5222,host2.onion:5222")
        settings = JoinMarketSettings()
        servers = settings.network_config.directory_servers
        assert "host1.onion:5222" in servers
        assert "host2.onion:5222" in servers

    def test_json_array_directory_servers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """JSON array env var for list[str] field should work."""
        monkeypatch.setenv(
            "NETWORK_CONFIG__DIRECTORY_SERVERS", '["host1.onion:5222","host2.onion:5222"]'
        )
        settings = JoinMarketSettings()
        servers = settings.network_config.directory_servers
        assert servers == ["host1.onion:5222", "host2.onion:5222"]


class TestNeutrinoAuthTokenFile:
    """Tests for the neutrino_auth_token_file setting."""

    def test_token_loaded_from_file(self, tmp_path: Path) -> None:
        """Auth token should be read from file when neutrino_auth_token_file is set."""
        token_file = tmp_path / "auth_token"
        token_file.write_text("deadbeef1234\n")
        settings = BitcoinSettings(neutrino_auth_token_file=str(token_file))
        assert settings.neutrino_auth_token == "deadbeef1234"

    def test_explicit_token_takes_priority(self, tmp_path: Path) -> None:
        """Explicit neutrino_auth_token should not be overridden by file."""
        token_file = tmp_path / "auth_token"
        token_file.write_text("from-file")
        settings = BitcoinSettings(
            neutrino_auth_token="from-env",
            neutrino_auth_token_file=str(token_file),
        )
        assert settings.neutrino_auth_token == "from-env"

    def test_missing_file_ignored(self) -> None:
        """Missing token file should not cause an error."""
        settings = BitcoinSettings(neutrino_auth_token_file="/nonexistent/path")
        assert settings.neutrino_auth_token is None

    def test_no_file_no_token(self) -> None:
        """Without file or token, auth_token stays None."""
        settings = BitcoinSettings()
        assert settings.neutrino_auth_token is None
        assert settings.neutrino_auth_token_file is None

    def test_token_loaded_from_tilde_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Token file path should support ~ expansion."""
        fake_home = tmp_path / "home"
        token_dir = fake_home / ".joinmarket-ng" / "neutrino"
        token_dir.mkdir(parents=True)
        token_file = token_dir / "auth_token"
        token_file.write_text("tilde-token")

        monkeypatch.setenv("HOME", str(fake_home))

        settings = BitcoinSettings(neutrino_auth_token_file="~/.joinmarket-ng/neutrino/auth_token")
        assert settings.neutrino_auth_token == "tilde-token"


class TestRpcCookieFile:
    """Tests for the rpc_cookie_file setting."""

    def test_cookie_loaded_from_file(self, tmp_path: Path) -> None:
        """RPC credentials should be read from cookie file."""
        cookie_file = tmp_path / ".cookie"
        cookie_file.write_text("__cookie__:abc123def456\n")
        settings = BitcoinSettings(rpc_cookie_file=str(cookie_file))
        assert settings.rpc_user == "__cookie__"
        assert settings.rpc_password.get_secret_value() == "abc123def456"

    def test_cookie_password_with_colons(self, tmp_path: Path) -> None:
        """Cookie password containing colons should be preserved."""
        cookie_file = tmp_path / ".cookie"
        cookie_file.write_text("__cookie__:abc:def:123\n")
        settings = BitcoinSettings(rpc_cookie_file=str(cookie_file))
        assert settings.rpc_user == "__cookie__"
        assert settings.rpc_password.get_secret_value() == "abc:def:123"

    def test_explicit_credentials_take_priority(self, tmp_path: Path) -> None:
        """Explicit rpc_user/rpc_password should not be overridden by cookie file."""
        cookie_file = tmp_path / ".cookie"
        cookie_file.write_text("__cookie__:from-cookie")
        settings = BitcoinSettings(
            rpc_user="myuser",
            rpc_password="mypassword",
            rpc_cookie_file=str(cookie_file),
        )
        assert settings.rpc_user == "myuser"
        assert settings.rpc_password.get_secret_value() == "mypassword"

    def test_missing_cookie_file_ignored(self) -> None:
        """Missing cookie file should not cause an error."""
        settings = BitcoinSettings(rpc_cookie_file="/nonexistent/.cookie")
        assert settings.rpc_user == ""
        assert settings.rpc_password.get_secret_value() == ""

    def test_no_cookie_file_no_change(self) -> None:
        """Without cookie file, defaults remain unchanged."""
        settings = BitcoinSettings()
        assert settings.rpc_cookie_file is None
        assert settings.rpc_user == ""
        assert settings.rpc_password.get_secret_value() == ""

    def test_malformed_cookie_file_ignored(self, tmp_path: Path) -> None:
        """Cookie file without colon separator should be handled gracefully."""
        cookie_file = tmp_path / ".cookie"
        cookie_file.write_text("malformed-content")
        settings = BitcoinSettings(rpc_cookie_file=str(cookie_file))
        assert settings.rpc_user == ""
        assert settings.rpc_password.get_secret_value() == ""

    def test_cookie_loaded_from_tilde_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cookie file path should support ~ expansion."""
        fake_home = tmp_path / "home"
        cookie_dir = fake_home / ".bitcoin"
        cookie_dir.mkdir(parents=True)
        cookie_file = cookie_dir / ".cookie"
        cookie_file.write_text("__cookie__:tilde-cookie-value")

        monkeypatch.setenv("HOME", str(fake_home))

        settings = BitcoinSettings(rpc_cookie_file="~/.bitcoin/.cookie")
        assert settings.rpc_user == "__cookie__"
        assert settings.rpc_password.get_secret_value() == "tilde-cookie-value"

    def test_env_var_sets_cookie_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BITCOIN__RPC_COOKIE_FILE env var should populate credentials from cookie."""
        cookie_file = tmp_path / ".cookie"
        cookie_file.write_text("__cookie__:envvar-cookie")
        monkeypatch.setenv("BITCOIN__RPC_COOKIE_FILE", str(cookie_file))

        settings = JoinMarketSettings()

        assert settings.bitcoin.rpc_user == "__cookie__"
        assert settings.bitcoin.rpc_password.get_secret_value() == "envvar-cookie"

    def test_empty_cookie_file(self, tmp_path: Path) -> None:
        """Empty cookie file should be handled gracefully."""
        cookie_file = tmp_path / ".cookie"
        cookie_file.write_text("")
        settings = BitcoinSettings(rpc_cookie_file=str(cookie_file))
        assert settings.rpc_user == ""
        assert settings.rpc_password.get_secret_value() == ""


# ============================================================================
# Config Migration Tests
# ============================================================================

MINI_TEMPLATE = """\
# JoinMarket-NG Configuration

# ============================================================================
# Tor Settings
# ============================================================================

[tor]
# socks_host = "127.0.0.1"
# socks_port = 9050

# ============================================================================
# Bitcoin Settings
# ============================================================================

[bitcoin]
# rpc_url = "http://127.0.0.1:8332"

# ============================================================================
# Maker Settings
# ============================================================================

[maker]
# cjfee_a = 500
# cjfee_r = 0.00002
"""


class TestGetUserSections:
    """Tests for _get_user_sections."""

    def test_detects_uncommented_sections(self) -> None:
        text = "[tor]\nsocks_host = '127.0.0.1'\n\n[bitcoin]\nrpc_url = 'x'\n"
        assert _get_user_sections(text) == {"tor", "bitcoin"}

    def test_includes_commented_legacy_sections(self) -> None:
        text = "# [tor]\n[bitcoin]\nrpc_url = 'x'\n"
        assert _get_user_sections(text) == {"tor", "bitcoin"}

    def test_empty_text(self) -> None:
        assert _get_user_sections("") == set()

    def test_no_sections(self) -> None:
        assert _get_user_sections("# just comments\n") == set()

    def test_regex_fallback_on_invalid_toml(self) -> None:
        """Even with broken TOML, we fall back to regex."""
        text = "[bitcoin]\n= invalid toml\n[maker]\n"
        sections = _get_user_sections(text)
        assert "bitcoin" in sections
        assert "maker" in sections


class TestMigrateConfig:
    """Tests for migrate_config (create-only, no file modification)."""

    def test_creates_config_from_template_if_missing(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert result == []
        assert config_path.exists()
        content = config_path.read_text()
        assert "[tor]" in content
        assert "[bitcoin]" in content
        assert "[maker]" in content

    def test_does_not_modify_existing_config(self, tmp_path: Path) -> None:
        """Existing config files are never modified."""
        config_path = tmp_path / "config.toml"
        original = "[tor]\nsocks_port = 9050\n"
        config_path.write_text(original)

        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert result == []
        assert config_path.read_text() == original

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        config_path = tmp_path / "deep" / "nested" / "config.toml"

        migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert config_path.exists()

    def test_returns_empty_when_no_template(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\n")

        result = migrate_config(config_path, template_text="")

        assert result == []


class TestConfigDiff:
    """Tests for config_diff (read-only comparison)."""

    def test_reports_missing_sections(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\nsocks_port = 9050\n")

        result = config_diff(config_path, template_text=MINI_TEMPLATE)

        assert "section:bitcoin" in result
        assert "section:maker" in result

    def test_reports_missing_keys(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[tor]\n# socks_host = "127.0.0.1"\n\n'
            '[bitcoin]\n# rpc_url = "http://127.0.0.1:8332"\n\n'
            "[maker]\n# cjfee_a = 500\n"
        )

        result = config_diff(config_path, template_text=MINI_TEMPLATE)

        key_diffs = [r for r in result if r.startswith("key:")]
        assert "key:tor.socks_port" in key_diffs
        assert "key:maker.cjfee_r" in key_diffs

    def test_no_diff_when_all_present(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[tor]\n# socks_host = "127.0.0.1"\n# socks_port = 9050\n\n'
            '[bitcoin]\n# rpc_url = "http://127.0.0.1:8332"\n\n'
            "[maker]\n# cjfee_a = 500\n# cjfee_r = 0.00002\n"
        )

        result = config_diff(config_path, template_text=MINI_TEMPLATE)

        assert result == []

    def test_does_not_modify_file(self, tmp_path: Path) -> None:
        """config_diff must never write to the config file."""
        config_path = tmp_path / "config.toml"
        original = "[tor]\nsocks_port = 9050\n"
        config_path.write_text(original)

        config_diff(config_path, template_text=MINI_TEMPLATE)

        assert config_path.read_text() == original

    def test_empty_for_missing_file(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"

        result = config_diff(config_path, template_text=MINI_TEMPLATE)

        assert result == []

    def test_empty_for_empty_template(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\n")

        result = config_diff(config_path, template_text="")

        assert result == []

    def test_detects_both_missing_sections_and_keys(self, tmp_path: Path) -> None:
        """Mixed: some sections missing, some keys missing in existing sections."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[tor]\nsocks_host = "127.0.0.1"\n')

        result = config_diff(config_path, template_text=MINI_TEMPLATE)

        section_diffs = [r for r in result if r.startswith("section:")]
        key_diffs = [r for r in result if r.startswith("key:")]
        assert "section:bitcoin" in section_diffs
        assert "section:maker" in section_diffs
        assert "key:tor.socks_port" in key_diffs

    def test_commented_section_headers_counted_as_existing(self, tmp_path: Path) -> None:
        """Legacy '# [section]' placeholders should count as existing."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '# [tor]\n# socks_host = "127.0.0.1"\n\n'
            '[bitcoin]\n# rpc_url = "http://127.0.0.1:8332"\n\n'
            "# [maker]\n# cjfee_a = 500\n# cjfee_r = 0.00002\n"
        )

        result = config_diff(config_path, template_text=MINI_TEMPLATE)

        assert "section:tor" not in result
        assert "section:maker" not in result

    def test_with_bundled_template(self, tmp_path: Path) -> None:
        """Test using the real bundled template."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\nsocks_port = 9050\n")

        result = config_diff(config_path)

        # Should report missing sections from bundled template
        section_diffs = [r for r in result if r.startswith("section:")]
        assert not any(r == "section:tor" for r in section_diffs)
        assert len(result) > 0


class TestEnsureConfigFile:
    """Tests for ensure_config_file."""

    def test_creates_config_on_first_run(self, temp_data_dir: Path) -> None:
        config_path = temp_data_dir / "config.toml"
        assert not config_path.exists()

        result = ensure_config_file(temp_data_dir)

        assert result == config_path
        assert config_path.exists()
        content = config_path.read_text()
        assert "[tor]" in content
        assert "[bitcoin]" in content

    def test_does_not_modify_existing_config(self, temp_data_dir: Path) -> None:
        """Existing config files are never touched at startup."""
        config_path = temp_data_dir / "config.toml"
        original = "[tor]\nsocks_port = 9050\n"
        config_path.write_text(original)

        result = ensure_config_file(temp_data_dir)

        assert result == config_path
        assert config_path.read_text() == original
