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
    _extract_key_groups,
    _extract_section_blocks,
    _get_section_keys,
    _get_user_section_ranges,
    _get_user_sections,
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


class TestExtractSectionBlocks:
    """Tests for _extract_section_blocks."""

    def test_extracts_all_sections(self) -> None:
        blocks = _extract_section_blocks(MINI_TEMPLATE)
        assert set(blocks.keys()) == {"tor", "bitcoin", "maker"}

    def test_block_includes_header(self) -> None:
        blocks = _extract_section_blocks(MINI_TEMPLATE)
        assert "# Tor Settings" in blocks["tor"]
        assert "[tor]" in blocks["tor"]

    def test_block_includes_commented_keys(self) -> None:
        blocks = _extract_section_blocks(MINI_TEMPLATE)
        assert '# socks_host = "127.0.0.1"' in blocks["tor"]
        assert "# socks_port = 9050" in blocks["tor"]

    def test_block_does_not_include_other_sections(self) -> None:
        blocks = _extract_section_blocks(MINI_TEMPLATE)
        assert "[bitcoin]" not in blocks["tor"]
        assert "[tor]" not in blocks["bitcoin"]

    def test_empty_text(self) -> None:
        assert _extract_section_blocks("") == {}

    def test_no_sections(self) -> None:
        assert _extract_section_blocks("# just a comment\nfoo = 1\n") == {}

    def test_single_section(self) -> None:
        text = """\
# ============================================================================
# Only Section
# ============================================================================

[only]
# key = "value"
"""
        blocks = _extract_section_blocks(text)
        assert set(blocks.keys()) == {"only"}
        assert '# key = "value"' in blocks["only"]

    def test_blocks_cover_entire_body(self) -> None:
        """Concatenating all blocks reproduces the section content."""
        blocks = _extract_section_blocks(MINI_TEMPLATE)
        # Each section block should start with a separator header or section name
        for name, block in blocks.items():
            assert f"[{name}]" in block


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
    """Tests for migrate_config."""

    def test_legacy_commented_placeholders_do_not_duplicate_sections(self, tmp_path: Path) -> None:
        """Legacy '# [section]' placeholders should count as existing sections."""
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '# [tor]\n# socks_host = "127.0.0.1"\n\n'
            '[bitcoin]\n# rpc_url = "http://127.0.0.1:8332"\n\n'
            "# [maker]\n# cjfee_a = 500\n# cjfee_r = 0.00002\n"
        )

        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert "section:tor" not in result
        assert "section:maker" not in result
        content = config_path.read_text()
        assert "\n[tor]\n" not in content
        assert not content.startswith("[tor]\n")
        assert "\n[maker]\n" not in content

    def test_creates_config_from_template_if_missing(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert result == []
        assert config_path.exists()
        content = config_path.read_text()
        assert "[tor]" in content
        assert "[bitcoin]" in content
        assert "[maker]" in content

    def test_adds_missing_sections(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        # User has tor and bitcoin, missing maker
        config_path.write_text("[tor]\nsocks_port = 9050\n\n[bitcoin]\nrpc_url = 'x'\n")

        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert "section:maker" in result
        content = config_path.read_text()
        assert "[maker]" in content
        # Original content preserved
        assert "socks_port = 9050" in content
        assert "rpc_url = 'x'" in content

    def test_preserves_existing_values(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\nsocks_port = 9999\n\n[bitcoin]\nrpc_url = 'custom'\n")

        migrate_config(config_path, template_text=MINI_TEMPLATE)

        content = config_path.read_text()
        # User values are preserved
        assert "socks_port = 9999" in content
        assert "rpc_url = 'custom'" in content

    def test_no_changes_when_all_sections_present(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        original = (
            '[tor]\n# socks_host = "127.0.0.1"\n# socks_port = 9050\n\n'
            '[bitcoin]\n# rpc_url = "http://127.0.0.1:8332"\n\n'
            "[maker]\n# cjfee_a = 500\n# cjfee_r = 0.00002\n"
        )
        config_path.write_text(original)

        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert result == []
        assert config_path.read_text() == original

    def test_idempotent(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\nsocks_port = 9050\n")

        # First migration adds sections and keys
        result1 = migrate_config(config_path, template_text=MINI_TEMPLATE)
        content_after_first = config_path.read_text()

        # Second migration changes nothing
        result2 = migrate_config(config_path, template_text=MINI_TEMPLATE)
        content_after_second = config_path.read_text()

        assert len(result1) > 0
        assert result2 == []
        assert content_after_first == content_after_second

    def test_adds_multiple_missing_sections(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[tor]\n# socks_host = "127.0.0.1"\n# socks_port = 9050\n')

        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        section_changes = {r for r in result if r.startswith("section:")}
        assert section_changes == {"section:bitcoin", "section:maker"}
        content = config_path.read_text()
        assert "[bitcoin]" in content
        assert "[maker]" in content

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        config_path = tmp_path / "deep" / "nested" / "config.toml"

        migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert config_path.exists()

    def test_handles_user_file_without_trailing_newline(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\nsocks_port = 9050")  # no trailing newline

        migrate_config(config_path, template_text=MINI_TEMPLATE)

        content = config_path.read_text()
        # Should still be valid and parseable
        assert "[tor]" in content
        assert "[bitcoin]" in content
        assert "[maker]" in content

    def test_returns_empty_when_no_template(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\n")

        result = migrate_config(config_path, template_text="")

        assert result == []

    def test_added_blocks_include_comment_headers(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\nsocks_port = 9050\n")

        migrate_config(config_path, template_text=MINI_TEMPLATE)

        content = config_path.read_text()
        # The added sections should include their descriptive headers
        assert "# Bitcoin Settings" in content
        assert "# Maker Settings" in content

    def test_with_bundled_template(self, tmp_path: Path) -> None:
        """Test using the real bundled template (no explicit template_text)."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[tor]\nsocks_port = 9050\n")

        result = migrate_config(config_path)

        # Should add sections from bundled template (not tor, it exists)
        section_changes = [r for r in result if r.startswith("section:")]
        assert not any(r == "section:tor" for r in section_changes)
        assert len(result) > 0
        content = config_path.read_text()
        assert "[bitcoin]" in content


class TestExtractKeyGroups:
    """Tests for _extract_key_groups."""

    def test_extracts_commented_keys(self) -> None:
        block = "[maker]\n# cjfee_a = 500\n# cjfee_r = 0.00002\n"
        groups = _extract_key_groups(block)
        keys = [k for k, _ in groups]
        assert keys == ["cjfee_a", "cjfee_r"]

    def test_includes_preceding_comments(self) -> None:
        block = "[maker]\n# Fee settings\n# cjfee_a = 500\n"
        groups = _extract_key_groups(block)
        assert len(groups) == 1
        key, text = groups[0]
        assert key == "cjfee_a"
        assert "# Fee settings" in text
        assert "# cjfee_a = 500" in text

    def test_blank_line_resets_comment_buffer(self) -> None:
        block = "[maker]\n# Unrelated comment\n\n# cjfee_a = 500\n"
        groups = _extract_key_groups(block)
        assert len(groups) == 1
        _, text = groups[0]
        assert "Unrelated comment" not in text

    def test_skips_section_headers(self) -> None:
        block = "[maker]\n# cjfee_a = 500\n"
        groups = _extract_key_groups(block)
        keys = [k for k, _ in groups]
        assert "maker" not in keys

    def test_empty_block(self) -> None:
        assert _extract_key_groups("") == []

    def test_no_keys(self) -> None:
        assert _extract_key_groups("[maker]\n# just a comment\n") == []


class TestGetSectionKeys:
    """Tests for _get_section_keys."""

    def test_finds_uncommented_keys(self) -> None:
        text = "[tor]\nsocks_host = '127.0.0.1'\nsocks_port = 9050\n"
        assert _get_section_keys(text) == {"socks_host", "socks_port"}

    def test_finds_commented_keys(self) -> None:
        text = "[tor]\n# socks_host = '127.0.0.1'\n# socks_port = 9050\n"
        assert _get_section_keys(text) == {"socks_host", "socks_port"}

    def test_finds_mixed_keys(self) -> None:
        text = "[tor]\nsocks_host = '127.0.0.1'\n# socks_port = 9050\n"
        assert _get_section_keys(text) == {"socks_host", "socks_port"}

    def test_empty_section(self) -> None:
        assert _get_section_keys("[tor]\n") == set()


class TestGetUserSectionRanges:
    """Tests for _get_user_section_ranges."""

    def test_single_section(self) -> None:
        text = "[tor]\nsocks_port = 9050\n"
        ranges = _get_user_section_ranges(text)
        assert "tor" in ranges
        assert text[ranges["tor"][0] : ranges["tor"][1]] == text

    def test_multiple_sections(self) -> None:
        text = "[tor]\nsocks_port = 9050\n\n[bitcoin]\nrpc_url = 'x'\n"
        ranges = _get_user_section_ranges(text)
        assert set(ranges.keys()) == {"tor", "bitcoin"}
        # tor section ends where bitcoin starts
        tor_text = text[ranges["tor"][0] : ranges["tor"][1]]
        assert "[tor]" in tor_text
        assert "[bitcoin]" not in tor_text

    def test_empty_text(self) -> None:
        assert _get_user_section_ranges("") == {}

    def test_commented_section_acts_as_boundary(self) -> None:
        """A commented section header should end the preceding uncommented section."""
        text = "[bitcoin]\nbackend_type = 'neutrino'\n\n# [network_config]\n# network = 'mainnet'\n"
        ranges = _get_user_section_ranges(text)
        assert set(ranges.keys()) == {"bitcoin"}
        bitcoin_text = text[ranges["bitcoin"][0] : ranges["bitcoin"][1]]
        assert "[bitcoin]" in bitcoin_text
        assert "# [network_config]" not in bitcoin_text

    def test_only_uncommented_sections_returned(self) -> None:
        """Commented section headers are boundaries but not in the returned ranges."""
        text = (
            "# [tor]\n# socks_host = '127.0.0.1'\n\n"
            "[bitcoin]\nrpc_url = 'x'\n\n"
            "# [maker]\n# cjfee_a = 500\n"
        )
        ranges = _get_user_section_ranges(text)
        assert set(ranges.keys()) == {"bitcoin"}
        bitcoin_text = text[ranges["bitcoin"][0] : ranges["bitcoin"][1]]
        assert "rpc_url" in bitcoin_text
        assert "# [maker]" not in bitcoin_text


class TestKeyLevelMigration:
    """Tests for key-level migration within existing sections."""

    def test_adds_missing_key_to_existing_section(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[tor]\n# socks_host = "127.0.0.1"\n\n'
            '[bitcoin]\n# rpc_url = "http://127.0.0.1:8332"\n\n'
            "[maker]\n# cjfee_a = 500\n"
        )

        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        # socks_port is missing from tor, cjfee_r from maker, rpc_url already exists
        key_changes = [r for r in result if r.startswith("key:")]
        assert "key:tor.socks_port" in key_changes
        assert "key:maker.cjfee_r" in key_changes
        content = config_path.read_text()
        assert "# socks_port = 9050" in content
        assert "# cjfee_r = 0.00002" in content

    def test_does_not_duplicate_existing_keys(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[tor]\n# socks_host = "127.0.0.1"\n# socks_port = 9050\n\n'
            '[bitcoin]\n# rpc_url = "http://127.0.0.1:8332"\n\n'
            "[maker]\n# cjfee_a = 500\n# cjfee_r = 0.00002\n"
        )

        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        assert result == []

    def test_preserves_user_uncommented_keys(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[tor]\nsocks_host = "custom"\n\n'
            '[bitcoin]\nrpc_url = "custom"\n\n'
            "[maker]\ncjfee_a = 999\n"
        )

        migrate_config(config_path, template_text=MINI_TEMPLATE)

        content = config_path.read_text()
        # Original values preserved
        assert 'socks_host = "custom"' in content
        assert 'rpc_url = "custom"' in content
        assert "cjfee_a = 999" in content
        # Missing keys added
        assert "# socks_port = 9050" in content
        assert "# cjfee_r = 0.00002" in content

    def test_key_level_idempotent(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[tor]\nsocks_host = "127.0.0.1"\n')

        result1 = migrate_config(config_path, template_text=MINI_TEMPLATE)
        content_after_first = config_path.read_text()

        result2 = migrate_config(config_path, template_text=MINI_TEMPLATE)
        content_after_second = config_path.read_text()

        assert len(result1) > 0
        assert result2 == []
        assert content_after_first == content_after_second

    def test_mixed_section_and_key_migration(self, tmp_path: Path) -> None:
        """Both new sections and new keys within existing sections."""
        config_path = tmp_path / "config.toml"
        config_path.write_text('[tor]\nsocks_host = "127.0.0.1"\n')

        result = migrate_config(config_path, template_text=MINI_TEMPLATE)

        section_changes = [r for r in result if r.startswith("section:")]
        key_changes = [r for r in result if r.startswith("key:")]
        # bitcoin and maker are new sections
        assert "section:bitcoin" in section_changes
        assert "section:maker" in section_changes
        # socks_port is a new key in existing tor section
        assert "key:tor.socks_port" in key_changes

    def test_key_with_comment_block_appended(self, tmp_path: Path) -> None:
        """New keys should include their preceding comment documentation."""
        template = """\
# ============================================================================
# Test Section
# ============================================================================

[test]
# Simple key
# simple = 1

# This is a detailed description
# of the new key
# new_key = "value"
"""
        config_path = tmp_path / "config.toml"
        config_path.write_text("[test]\n# simple = 1\n")

        migrate_config(config_path, template_text=template)

        content = config_path.read_text()
        assert '# new_key = "value"' in content
        assert "# This is a detailed description" in content

    def test_new_keys_placed_within_section_not_at_eof(self, tmp_path: Path) -> None:
        """New keys for [bitcoin] must not be appended after later commented sections.

        Regression test: when the user config has ``[bitcoin]`` followed by
        ``# [network_config]``, ``# [wallet]``, etc., new bitcoin keys were
        appended at EOF instead of within the ``[bitcoin]`` section because
        ``_get_user_section_ranges`` did not use commented headers as boundaries.
        """
        config_path = tmp_path / "config.toml"
        config_path.write_text(
            '[bitcoin]\n# rpc_url = "http://127.0.0.1:8332"\n\n# [maker]\n# cjfee_a = 500\n'
        )

        template = """\
# ============================================================================
# Bitcoin Settings
# ============================================================================

[bitcoin]
# rpc_url = "http://127.0.0.1:8332"
# new_bitcoin_key = "new_value"

# ============================================================================
# Maker Settings
# ============================================================================

[maker]
# cjfee_a = 500
"""
        result = migrate_config(config_path, template_text=template)

        assert "key:bitcoin.new_bitcoin_key" in result
        content = config_path.read_text()
        # The new key must appear BEFORE the ``# [maker]`` line.
        new_key_pos = content.index('# new_bitcoin_key = "new_value"')
        maker_pos = content.index("# [maker]")
        assert new_key_pos < maker_pos, (
            f"new_bitcoin_key at {new_key_pos} should be before # [maker] at {maker_pos}"
        )


class TestEnsureConfigFileMigration:
    """Tests for ensure_config_file migration integration."""

    def test_creates_config_on_first_run(self, temp_data_dir: Path) -> None:
        config_path = temp_data_dir / "config.toml"
        assert not config_path.exists()

        result = ensure_config_file(temp_data_dir)

        assert result == config_path
        assert config_path.exists()
        content = config_path.read_text()
        assert "[tor]" in content
        assert "[bitcoin]" in content

    def test_migrates_on_subsequent_runs(self, temp_data_dir: Path) -> None:
        config_path = temp_data_dir / "config.toml"
        config_path.write_text("[tor]\nsocks_port = 9050\n")

        result = ensure_config_file(temp_data_dir)

        assert result == config_path
        content = config_path.read_text()
        # Original preserved
        assert "socks_port = 9050" in content
        # New sections added
        assert "[bitcoin]" in content
