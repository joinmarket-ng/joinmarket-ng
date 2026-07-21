"""
Tests for the CLI common module.
"""

from __future__ import annotations

import base64
import os
import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from loguru import logger

from jmcore.cli_common import load_mnemonic_from_file, setup_cli, setup_logging
from jmcore.settings import reset_settings


@pytest.fixture(autouse=True)
def reset_settings_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Generator[None, None, None]:
    """Reset settings and isolate tests from user config files."""
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)
    reset_settings()
    yield
    # setup_cli() mutates os.environ directly (not via monkeypatch), so clear
    # JOINMARKET_CONFIG_FILE on teardown to avoid leaking into other tests.
    os.environ.pop("JOINMARKET_CONFIG_FILE", None)
    reset_settings()


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_sets_level(self) -> None:
        """Test that setup_logging configures the log level."""
        setup_logging("DEBUG")
        # Verify handler is configured (loguru doesn't expose level directly,
        # but we can check that the handler was added)
        handlers = logger._core.handlers
        assert len(handlers) > 0

    def test_setup_logging_case_insensitive(self) -> None:
        """Test that log level is case-insensitive."""
        # Should not raise
        setup_logging("trace")
        setup_logging("TRACE")
        setup_logging("Trace")


class TestSetupCli:
    """Tests for setup_cli function."""

    def test_setup_cli_returns_settings(self) -> None:
        """Test that setup_cli returns JoinMarketSettings."""
        from jmcore.settings import JoinMarketSettings

        settings = setup_cli()
        assert isinstance(settings, JoinMarketSettings)

    def test_setup_cli_cli_arg_overrides_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that CLI log level argument overrides settings."""
        # Set log level in env (settings)
        monkeypatch.setenv("LOGGING__LEVEL", "DEBUG")

        with patch.object(logger, "remove"), patch.object(logger, "add") as mock_add:
            setup_cli(log_level="TRACE")

            # setup_cli now performs two passes (early + final). The final
            # pass reflects the resolved level.
            assert mock_add.call_count >= 1
            assert mock_add.call_args_list[-1][1]["level"] == "TRACE"

    def test_setup_cli_uses_settings_when_no_cli_arg(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test that setup_cli uses settings.logging.level when no CLI arg."""
        # Set log level in env (settings)
        monkeypatch.setenv("LOGGING__LEVEL", "TRACE")

        with patch.object(logger, "remove"), patch.object(logger, "add") as mock_add:
            setup_cli(log_level=None)

            assert mock_add.call_count >= 1
            assert mock_add.call_args_list[-1][1]["level"] == "TRACE"

    def test_setup_cli_defaults_to_info(self) -> None:
        """Test that setup_cli defaults to INFO when no CLI arg and no settings."""
        with patch.object(logger, "remove"), patch.object(logger, "add") as mock_add:
            setup_cli(log_level=None)

            assert mock_add.call_count >= 1
            assert mock_add.call_args_list[-1][1]["level"] == "INFO"

    def test_setup_cli_early_logging_honors_env_level(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for issue #459.

        The early ``setup_logging`` call inside ``setup_cli`` must honor
        ``LOGGING__LEVEL`` from the environment *before* settings are
        loaded, so that informational messages emitted during settings
        loading (e.g. "Loaded config from ...") are suppressed when the
        user requests a quieter log level (as the TUI does).
        """
        monkeypatch.setenv("LOGGING__LEVEL", "WARNING")

        with patch.object(logger, "remove"), patch.object(logger, "add") as mock_add:
            setup_cli(log_level=None)

            # setup_cli performs TWO configurations: an early one (before
            # settings load) and a final one (after). Both must use
            # WARNING given the env var. The first call is what matters
            # for #459 -- if it uses INFO, the "Loaded config from ..."
            # line leaks into TUI output.
            assert mock_add.call_count == 2
            assert mock_add.call_args_list[0][1]["level"] == "WARNING"
            assert mock_add.call_args_list[1][1]["level"] == "WARNING"

    def test_setup_cli_early_logging_cli_arg_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An explicit CLI --log-level must still override LOGGING__LEVEL
        during the early-logging pass."""
        monkeypatch.setenv("LOGGING__LEVEL", "WARNING")

        with patch.object(logger, "remove"), patch.object(logger, "add") as mock_add:
            setup_cli(log_level="DEBUG")

            assert mock_add.call_count == 2
            # CLI arg overrides env in both passes.
            assert mock_add.call_args_list[0][1]["level"] == "DEBUG"
            assert mock_add.call_args_list[1][1]["level"] == "DEBUG"

    def test_setup_cli_data_dir_sets_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CLI --data-dir should propagate via JOINMARKET_DATA_DIR before settings load."""
        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        # Start from an unrelated env value to confirm setup_cli overrides it.
        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path / "other"))

        settings = setup_cli(data_dir=custom_dir)

        assert os.environ["JOINMARKET_DATA_DIR"] == str(custom_dir)
        assert settings.get_data_dir() == custom_dir

    def test_setup_cli_no_data_dir_keeps_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When --data-dir is not given, the existing env var must be respected."""
        env_dir = tmp_path / "env_dir"
        env_dir.mkdir()
        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(env_dir))

        settings = setup_cli()

        assert os.environ["JOINMARKET_DATA_DIR"] == str(env_dir)
        assert settings.get_data_dir() == env_dir

    def test_setup_cli_config_file_sets_env_var(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CLI --config-file should set JOINMARKET_CONFIG_FILE before settings load (#537)."""
        config_file = tmp_path / "etc" / "config.toml"
        config_file.parent.mkdir()
        config_file.write_text("[tor]\nsocks_port = 9555\n")
        monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)

        settings = setup_cli(config_file=config_file)

        assert os.environ["JOINMARKET_CONFIG_FILE"] == str(config_file)
        # The decoupled config file is actually read.
        assert settings.tor.socks_port == 9555

    def test_setup_cli_config_file_decoupled_from_data_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--config-file and --data-dir resolve independently (FHS layout, #537)."""
        config_file = tmp_path / "etc" / "joinmarket" / "config.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text("[maker]\nmin_size = 222222\n")
        data_dir = tmp_path / "var" / "lib" / "joinmarket"
        data_dir.mkdir(parents=True)
        monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)

        settings = setup_cli(data_dir=data_dir, config_file=config_file)

        assert settings.get_data_dir() == data_dir
        assert settings.maker.min_size == 222222

    def test_setup_cli_config_file_expands_tilde(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A ``~`` in --config-file is expanded before being exported."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)

        setup_cli(config_file=Path("~/custom.toml"))

        assert os.environ["JOINMARKET_CONFIG_FILE"] == str(fake_home / "custom.toml")
        assert "~" not in os.environ["JOINMARKET_CONFIG_FILE"]

    def test_setup_cli_invalid_config_exits_cleanly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An out-of-range config value exits with code 1, not a raw traceback.

        Guards the friendly ValidationError handler: a bad value in
        config.toml must produce an actionable message and a clean
        ``SystemExit(1)`` instead of letting pydantic's traceback propagate.
        """
        config_file = tmp_path / "config.toml"
        # summary_interval_hours is bounded to 1-168; 9999 fails validation.
        config_file.write_text("[notifications]\nsummary_interval_hours = 9999\n")
        monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            setup_cli(config_file=config_file)

        assert exc_info.value.code == 1

    def test_setup_cli_scalar_notification_url(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A bare string notifications url loads via setup_cli without crashing.

        End-to-end regression for the reported maker startup crash where
        ``urls = "tgram://..."`` (a scalar) caused ``status=1/FAILURE``.
        """
        config_file = tmp_path / "config.toml"
        config_file.write_text('[notifications]\nurls = "tgram://bottoken/ChatID"\n')
        monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)

        settings = setup_cli(config_file=config_file)

        assert settings.notifications.urls == ["tgram://bottoken/ChatID"]


class TestLoadMnemonicFromFile:
    """Tests for load_mnemonic_from_file function."""

    def test_load_plaintext_mnemonic(self) -> None:
        """Test loading a plaintext mnemonic file."""
        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".mnemonic") as f:
            f.write(mnemonic)
            temp_path = Path(f.name)

        try:
            result = load_mnemonic_from_file(temp_path)
            assert result == mnemonic
        finally:
            os.unlink(temp_path)

    def test_load_encrypted_mnemonic(self) -> None:
        """Test loading an encrypted mnemonic file."""
        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        password = "test_password"

        # Encrypt the mnemonic
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        encrypted_token = fernet.encrypt(mnemonic.encode("utf-8"))

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mnemonic") as f:
            f.write(salt + encrypted_token)
            temp_path = Path(f.name)

        try:
            result = load_mnemonic_from_file(temp_path, password=password, auto_prompt=False)
            assert result == mnemonic
        finally:
            os.unlink(temp_path)

    def test_load_encrypted_mnemonic_wrong_password(self) -> None:
        """Test that wrong password raises ValueError."""
        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        password = "correct_password"

        # Encrypt the mnemonic
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        encrypted_token = fernet.encrypt(mnemonic.encode("utf-8"))

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mnemonic") as f:
            f.write(salt + encrypted_token)
            temp_path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="Decryption failed"):
                load_mnemonic_from_file(temp_path, password="wrong_password", auto_prompt=False)
        finally:
            os.unlink(temp_path)

    def test_load_encrypted_with_invalid_utf8_content(self) -> None:
        """Test that decrypted invalid UTF-8 raises ValueError with clear message."""
        password = "test_password"

        # Encrypt invalid UTF-8 bytes
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        # Encrypt invalid UTF-8 bytes
        invalid_utf8 = b"\x80\x81\x82\x83"
        encrypted_token = fernet.encrypt(invalid_utf8)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mnemonic") as f:
            f.write(salt + encrypted_token)
            temp_path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="not valid UTF-8"):
                load_mnemonic_from_file(temp_path, password=password, auto_prompt=False)
        finally:
            os.unlink(temp_path)

    def test_load_file_not_found(self) -> None:
        """Test that missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_mnemonic_from_file(Path("/nonexistent/path/mnemonic.txt"))

    def test_load_encrypted_no_password_no_prompt(self) -> None:
        """Test that encrypted file without password raises ValueError when auto_prompt=False."""
        # Create a file with random bytes (looks encrypted)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mnemonic") as f:
            f.write(os.urandom(100))
            temp_path = Path(f.name)

        try:
            with pytest.raises(ValueError, match="appears to be encrypted"):
                load_mnemonic_from_file(temp_path, password=None, auto_prompt=False)
        finally:
            os.unlink(temp_path)

    @staticmethod
    def _write_encrypted(mnemonic: str, password: str) -> Path:
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        token = fernet.encrypt(mnemonic.encode("utf-8"))
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mnemonic") as f:
            f.write(salt + token)
            return Path(f.name)

    def test_interactive_prompt_retries_on_wrong_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wrong password at the interactive prompt retries up to max_prompt_attempts (#456)."""
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        password = "correct_password"
        temp_path = self._write_encrypted(mnemonic, password)

        attempts = {"n": 0}

        def fake_prompt(path: Path | None = None) -> str:
            attempts["n"] += 1
            # First two prompts return wrong passwords; third returns correct.
            if attempts["n"] < 3:
                return f"wrong{attempts['n']}"
            return password

        monkeypatch.setattr("jmcore.cli_common._prompt_for_password", fake_prompt)

        try:
            result = load_mnemonic_from_file(temp_path, password=None, auto_prompt=True)
            assert result == mnemonic
            assert attempts["n"] == 3
        finally:
            os.unlink(temp_path)

    def test_interactive_prompt_gives_up_after_max_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After max_prompt_attempts wrong passwords, the last error is raised."""
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        temp_path = self._write_encrypted(mnemonic, "correct_password")

        calls = {"n": 0}

        def fake_prompt(path: Path | None = None) -> str:
            calls["n"] += 1
            return "still_wrong"

        monkeypatch.setattr("jmcore.cli_common._prompt_for_password", fake_prompt)

        try:
            with pytest.raises(ValueError, match="Decryption failed"):
                load_mnemonic_from_file(
                    temp_path, password=None, auto_prompt=True, max_prompt_attempts=3
                )
            assert calls["n"] == 3
        finally:
            os.unlink(temp_path)

    def test_explicit_wrong_password_does_not_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An explicit password argument must fail fast without prompting (#456)."""
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        temp_path = self._write_encrypted(mnemonic, "correct_password")

        def fake_prompt(path: Path | None = None) -> str:
            raise AssertionError("interactive prompt must not run for explicit password")

        monkeypatch.setattr("jmcore.cli_common._prompt_for_password", fake_prompt)

        try:
            with pytest.raises(ValueError, match="Decryption failed"):
                load_mnemonic_from_file(
                    temp_path, password="wrong", auto_prompt=True, max_prompt_attempts=5
                )
        finally:
            os.unlink(temp_path)

    def test_env_var_wrong_password_does_not_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MNEMONIC_PASSWORD env var must fail fast without prompting (#456)."""
        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        temp_path = self._write_encrypted(mnemonic, "correct_password")

        def fake_prompt(path: Path | None = None) -> str:
            raise AssertionError("interactive prompt must not run when env var is set")

        monkeypatch.setattr("jmcore.cli_common._prompt_for_password", fake_prompt)
        monkeypatch.setenv("MNEMONIC_PASSWORD", "wrong_env_password")

        try:
            with pytest.raises(ValueError, match="Decryption failed"):
                load_mnemonic_from_file(temp_path, password=None, auto_prompt=True)
        finally:
            os.unlink(temp_path)

    def test_corrupted_encrypted_file_does_not_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Decrypted-but-invalid-UTF-8 (corruption) must not trigger retry."""
        password = "test_password"
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        token = fernet.encrypt(b"\x80\x81\x82\x83")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mnemonic") as f:
            f.write(salt + token)
            temp_path = Path(f.name)

        calls = {"n": 0}

        def fake_prompt(path: Path | None = None) -> str:
            calls["n"] += 1
            return password

        monkeypatch.setattr("jmcore.cli_common._prompt_for_password", fake_prompt)

        try:
            with pytest.raises(ValueError, match="not valid UTF-8"):
                load_mnemonic_from_file(temp_path, password=None, auto_prompt=True)
            # Prompt was called once; corruption did not trigger another attempt.
            assert calls["n"] == 1
        finally:
            os.unlink(temp_path)


class TestResolveBackendSettings:
    """Tests for resolve_backend_settings() populating neutrino_add_peers."""

    def test_resolve_backend_settings_neutrino_add_peers_from_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_backend_settings() picks up neutrino_add_peers from settings."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings(
            bitcoin={"neutrino_add_peers": ["peer1.example.com:38333", "peer2.example.com:38333"]}
        )
        result = resolve_backend_settings(settings)
        assert result.neutrino_add_peers == [
            "peer1.example.com:38333",
            "peer2.example.com:38333",
        ]

    def test_resolve_backend_settings_empty_add_peers_by_default(self) -> None:
        """resolve_backend_settings() returns empty list when no peers configured."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings()
        result = resolve_backend_settings(settings)
        assert result.neutrino_add_peers == []

    def test_fee_estimate_settings_resolved(self) -> None:
        """resolve_backend_settings() carries fee_estimate_url and builds the
        Tor fee proxy from [tor] settings (settings -> config round-trip)."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings(
            bitcoin={"fee_estimate_url": "https://example.com/fee-estimates"},
            tor={"socks_host": "127.0.0.1", "socks_port": 9050, "stream_isolation": False},
        )
        result = resolve_backend_settings(settings)
        assert result.fee_estimate_url == "https://example.com/fee-estimates"
        assert result.fee_estimate_proxy == "socks5h://127.0.0.1:9050"

    def test_neutrino_tls_cert_relative_resolved_against_data_dir(self, tmp_path: Path) -> None:
        """Relative TLS cert paths in config join onto the resolved data dir."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings(bitcoin={"neutrino_tls_cert": "neutrino/tls.cert"})
        result = resolve_backend_settings(settings, data_dir=tmp_path)
        assert result.neutrino_tls_cert == str(tmp_path / "neutrino" / "tls.cert")

    def test_neutrino_tls_cert_absolute_preserved(self, tmp_path: Path) -> None:
        """Absolute TLS cert paths are returned unchanged."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        cert = tmp_path / "elsewhere" / "tls.cert"
        settings = JoinMarketSettings(bitcoin={"neutrino_tls_cert": str(cert)})
        result = resolve_backend_settings(settings, data_dir=tmp_path)
        assert result.neutrino_tls_cert == str(cert)

    def test_neutrino_tls_cert_tilde_expanded(self, tmp_path: Path) -> None:
        """``~``-prefixed TLS cert paths are expanded to home, not data_dir."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings(bitcoin={"neutrino_tls_cert": "~/joinmarket/tls.cert"})
        result = resolve_backend_settings(settings, data_dir=tmp_path)
        assert result.neutrino_tls_cert == str(Path("~/joinmarket/tls.cert").expanduser())

    def test_neutrino_auth_token_file_relative_loaded_from_data_dir(self, tmp_path: Path) -> None:
        """Auth-token file with a relative path is read relative to data_dir."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        token_dir = tmp_path / "neutrino"
        token_dir.mkdir()
        (token_dir / "auth_token").write_text("supersecret\n")

        settings = JoinMarketSettings(bitcoin={"neutrino_auth_token_file": "neutrino/auth_token"})
        result = resolve_backend_settings(settings, data_dir=tmp_path)
        assert result.neutrino_auth_token == "supersecret"

    def test_neutrino_url_upgraded_to_https_when_auth_token(self, tmp_path: Path) -> None:
        """An auth token forces the neutrino URL to HTTPS (auth implies TLS)."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings(
            bitcoin={
                "neutrino_url": "http://127.0.0.1:8334",
                "neutrino_auth_token": "tok",
            }
        )
        result = resolve_backend_settings(settings, data_dir=tmp_path)
        assert result.neutrino_url == "https://127.0.0.1:8334"

    def test_neutrino_url_kept_http_without_auth_token(self, tmp_path: Path) -> None:
        """Without an auth token the URL stays HTTP (backwards compatible)."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings(
            bitcoin={
                "neutrino_url": "http://127.0.0.1:8334",
                "neutrino_auth_token_file": "",
            }
        )
        result = resolve_backend_settings(settings, data_dir=tmp_path)
        assert result.neutrino_url == "http://127.0.0.1:8334"

    def test_neutrino_missing_auth_token_file_is_silent(self, tmp_path: Path) -> None:
        """A missing default auth-token file leaves the token unset without error."""
        from jmcore.cli_common import resolve_backend_settings
        from jmcore.settings import JoinMarketSettings

        settings = JoinMarketSettings(bitcoin={"neutrino_auth_token_file": "neutrino/auth_token"})
        result = resolve_backend_settings(settings, data_dir=tmp_path)
        assert result.neutrino_auth_token is None
        assert result.neutrino_url == "http://127.0.0.1:8334"


class TestCreateBackend:
    """Tests for create_backend() passing add_peers to NeutrinoBackend."""

    def test_create_backend_neutrino_passes_add_peers(self) -> None:
        """create_backend() passes neutrino_add_peers to NeutrinoBackend."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from jmcore.cli_common import ResolvedBackendSettings, create_backend

        peers = ["peer1.example.com:38333"]
        backend_settings = ResolvedBackendSettings(
            network="signet",
            bitcoin_network="signet",
            backend_type="neutrino",
            rpc_url="",
            rpc_user="",
            rpc_password="",
            neutrino_url="http://127.0.0.1:8334",
            neutrino_add_peers=peers,
            data_dir=Path("/tmp"),
        )

        mock_backend = MagicMock()
        with patch(
            "jmwallet.backends.neutrino.NeutrinoBackend", return_value=mock_backend
        ) as mock_cls:
            result = create_backend(backend_settings)

        mock_cls.assert_called_once_with(
            neutrino_url="http://127.0.0.1:8334",
            network="signet",
            scan_start_height=None,
            add_peers=peers,
            tls_cert_path=None,
            auth_token=None,
            fee_estimate_url=None,
            fee_estimate_proxy=None,
        )
        assert result is mock_backend

    def test_create_backend_neutrino_empty_add_peers(self) -> None:
        """create_backend() passes empty list to NeutrinoBackend when no peers set."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from jmcore.cli_common import ResolvedBackendSettings, create_backend

        backend_settings = ResolvedBackendSettings(
            network="mainnet",
            bitcoin_network="mainnet",
            backend_type="neutrino",
            rpc_url="",
            rpc_user="",
            rpc_password="",
            neutrino_url="http://127.0.0.1:8334",
            neutrino_add_peers=[],
            data_dir=Path("/tmp"),
        )

        mock_backend = MagicMock()
        with patch(
            "jmwallet.backends.neutrino.NeutrinoBackend", return_value=mock_backend
        ) as mock_cls:
            create_backend(backend_settings)

        mock_cls.assert_called_once_with(
            neutrino_url="http://127.0.0.1:8334",
            network="mainnet",
            scan_start_height=None,
            add_peers=[],
            tls_cert_path=None,
            auth_token=None,
            fee_estimate_url=None,
            fee_estimate_proxy=None,
        )


class TestResolveMnemonic:
    """Tests for resolve_mnemonic function."""

    def test_resolve_mnemonic_from_default_wallet_with_config_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that config password is used when loading default wallet."""
        from jmcore.cli_common import resolve_mnemonic
        from jmcore.settings import JoinMarketSettings

        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        password = "config_password"

        # Create encrypted mnemonic at default wallet location
        wallets_dir = tmp_path / "wallets"
        wallets_dir.mkdir(parents=True)
        default_wallet = wallets_dir / "default.mnemonic"

        # Encrypt the mnemonic
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        encrypted_token = fernet.encrypt(mnemonic.encode("utf-8"))
        default_wallet.write_bytes(salt + encrypted_token)

        # Create settings with mnemonic_password but no mnemonic_file
        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
        settings = JoinMarketSettings(
            data_dir=tmp_path,
            wallet={"mnemonic_password": password},
        )

        # Resolve mnemonic - should use default wallet with config password
        result = resolve_mnemonic(settings)
        assert result is not None
        assert result.mnemonic == mnemonic
        assert "default wallet" in result.source

    def test_resolve_mnemonic_cli_file_uses_config_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for issue #498.

        When --mnemonic-file is passed (as in the documented systemd unit)
        and wallet.mnemonic_password is set in config.toml, the config
        password must be used instead of falling through to an interactive
        prompt (which fails under systemd).
        """
        from jmcore.cli_common import resolve_mnemonic
        from jmcore.settings import JoinMarketSettings

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        password = "systemd_config_password"

        # Encrypt mnemonic at an explicit (non-default) location
        encrypted_path = tmp_path / "custom" / "wallet.mnemonic"
        encrypted_path.parent.mkdir(parents=True)

        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        encrypted_token = fernet.encrypt(mnemonic.encode("utf-8"))
        encrypted_path.write_bytes(salt + encrypted_token)

        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
        settings = JoinMarketSettings(
            data_dir=tmp_path,
            wallet={"mnemonic_password": password},
        )

        # Simulate the maker/taker CLI: pass --mnemonic-file without password.
        # If the config password is not picked up, load_mnemonic_from_file
        # would try to prompt and fail (or decrypt with no password and fail).
        # Patch the prompt so any attempt to use it raises loudly.
        with patch(
            "jmcore.cli_common._prompt_for_password",
            side_effect=AssertionError(
                "Should not prompt; config password must be used (issue #498)"
            ),
        ):
            result = resolve_mnemonic(settings, mnemonic_file=encrypted_path)

        assert result is not None
        assert result.mnemonic == mnemonic
        assert "--mnemonic-file" in result.source

    def test_resolve_mnemonic_env_file_uses_config_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MNEMONIC_FILE env var path also honors wallet.mnemonic_password."""
        from jmcore.cli_common import resolve_mnemonic
        from jmcore.settings import JoinMarketSettings

        mnemonic = (
            "abandon abandon abandon abandon abandon abandon "
            "abandon abandon abandon abandon abandon about"
        )
        password = "env_config_password"

        encrypted_path = tmp_path / "env_wallet.mnemonic"
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))
        fernet = Fernet(key)
        encrypted_token = fernet.encrypt(mnemonic.encode("utf-8"))
        encrypted_path.write_bytes(salt + encrypted_token)

        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("MNEMONIC_FILE", str(encrypted_path))
        settings = JoinMarketSettings(
            data_dir=tmp_path,
            wallet={"mnemonic_password": password},
        )

        with patch(
            "jmcore.cli_common._prompt_for_password",
            side_effect=AssertionError("Should not prompt; config password must be used"),
        ):
            result = resolve_mnemonic(settings)

        assert result is not None
        assert result.mnemonic == mnemonic
        assert "MNEMONIC_FILE env" in result.source


class TestCreateBackendCreationHeight:
    """Tests for create_backend() with creation_height parameter."""

    def test_create_backend_neutrino_with_creation_height(self) -> None:
        """create_backend() calls set_wallet_creation_height when height is provided."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from jmcore.cli_common import ResolvedBackendSettings, create_backend

        backend_settings = ResolvedBackendSettings(
            network="mainnet",
            bitcoin_network="mainnet",
            backend_type="neutrino",
            rpc_url="",
            rpc_user="",
            rpc_password="",
            neutrino_url="http://127.0.0.1:8334",
            neutrino_add_peers=[],
            data_dir=Path("/tmp"),
        )

        mock_backend = MagicMock()
        with patch("jmwallet.backends.neutrino.NeutrinoBackend", return_value=mock_backend):
            create_backend(backend_settings, creation_height=800000)

        mock_backend.set_wallet_creation_height.assert_called_once_with(800000)

    def test_create_backend_neutrino_without_creation_height(self) -> None:
        """create_backend() does NOT call set_wallet_creation_height when None."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from jmcore.cli_common import ResolvedBackendSettings, create_backend

        backend_settings = ResolvedBackendSettings(
            network="mainnet",
            bitcoin_network="mainnet",
            backend_type="neutrino",
            rpc_url="",
            rpc_user="",
            rpc_password="",
            neutrino_url="http://127.0.0.1:8334",
            neutrino_add_peers=[],
            data_dir=Path("/tmp"),
        )

        mock_backend = MagicMock()
        with patch("jmwallet.backends.neutrino.NeutrinoBackend", return_value=mock_backend):
            create_backend(backend_settings)

        mock_backend.set_wallet_creation_height.assert_not_called()

    def test_create_backend_descriptor_with_creation_height(self) -> None:
        """create_backend() calls set_wallet_creation_height on descriptor backend."""
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from jmcore.cli_common import ResolvedBackendSettings, create_backend

        backend_settings = ResolvedBackendSettings(
            network="mainnet",
            bitcoin_network="mainnet",
            backend_type="descriptor_wallet",
            rpc_url="http://127.0.0.1:8332",
            rpc_user="user",
            rpc_password="pass",
            neutrino_url="",
            neutrino_add_peers=[],
            data_dir=Path("/tmp"),
        )

        mock_backend = MagicMock()
        with patch(
            "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend",
            return_value=mock_backend,
        ):
            create_backend(
                backend_settings,
                wallet_name="test-wallet",
                creation_height=790000,
            )

        mock_backend.set_wallet_creation_height.assert_called_once_with(790000)


class TestMnemonicMeta:
    """Tests for mnemonic metadata (.meta file) functions."""

    def test_save_and_load_mnemonic_meta(self, tmp_path: Path) -> None:
        """save_mnemonic_meta and load_mnemonic_meta round-trip."""
        from jmwallet.cli.mnemonic import load_mnemonic_meta, save_mnemonic_meta

        mnemonic_file = tmp_path / "default.mnemonic"
        mnemonic_file.write_text("abandon " * 11 + "about")

        save_mnemonic_meta(mnemonic_file, creation_height=850000)

        meta = load_mnemonic_meta(mnemonic_file)
        assert meta["creation_height"] == 850000

    def test_meta_path_convention(self, tmp_path: Path) -> None:
        """Meta file uses .meta suffix appended to the mnemonic file name."""
        from jmwallet.cli.mnemonic import _meta_path

        mnemonic_file = tmp_path / "default.mnemonic"
        assert _meta_path(mnemonic_file) == tmp_path / "default.mnemonic.meta"

    def test_load_mnemonic_meta_missing_file(self, tmp_path: Path) -> None:
        """load_mnemonic_meta returns empty dict when .meta file does not exist."""
        from jmwallet.cli.mnemonic import load_mnemonic_meta

        mnemonic_file = tmp_path / "no_such.mnemonic"
        meta = load_mnemonic_meta(mnemonic_file)
        assert meta == {}

    def test_load_mnemonic_meta_corrupted_json(self, tmp_path: Path) -> None:
        """load_mnemonic_meta returns empty dict on corrupted JSON."""
        from jmwallet.cli.mnemonic import load_mnemonic_meta

        mnemonic_file = tmp_path / "default.mnemonic"
        mnemonic_file.write_text("dummy")
        meta_path = tmp_path / "default.mnemonic.meta"
        meta_path.write_text("not valid json {{{")

        meta = load_mnemonic_meta(mnemonic_file)
        assert meta == {}

    def test_save_mnemonic_meta_no_data_is_noop(self, tmp_path: Path) -> None:
        """save_mnemonic_meta with no creation_height does not create a file."""
        from jmwallet.cli.mnemonic import _meta_path, save_mnemonic_meta

        mnemonic_file = tmp_path / "default.mnemonic"
        mnemonic_file.write_text("dummy")

        save_mnemonic_meta(mnemonic_file)

        assert not _meta_path(mnemonic_file).exists()

    def test_save_fingerprint_round_trips_and_lowercases(self, tmp_path: Path) -> None:
        """fingerprint is persisted (normalized to lowercase) and read back."""
        from jmwallet.cli.mnemonic import (
            load_mnemonic_meta_fingerprint,
            save_mnemonic_meta,
        )

        mnemonic_file = tmp_path / "default.mnemonic"
        mnemonic_file.write_text("dummy")

        save_mnemonic_meta(mnemonic_file, fingerprint="AABBCCDD")

        assert load_mnemonic_meta_fingerprint(mnemonic_file) == "aabbccdd"

    def test_save_fingerprint_preserves_creation_height(self, tmp_path: Path) -> None:
        """Writing fingerprint must not drop a previously stored creation_height
        and vice versa (fields are merged, not overwritten)."""
        from jmwallet.cli.mnemonic import load_mnemonic_meta, save_mnemonic_meta

        mnemonic_file = tmp_path / "default.mnemonic"
        mnemonic_file.write_text("dummy")

        save_mnemonic_meta(mnemonic_file, creation_height=820000)
        save_mnemonic_meta(mnemonic_file, fingerprint="aabbccdd")

        meta = load_mnemonic_meta(mnemonic_file)
        assert meta["creation_height"] == 820000
        assert meta["fingerprint"] == "aabbccdd"

    def test_load_fingerprint_rejects_invalid(self, tmp_path: Path) -> None:
        """Non-hex / wrong-length fingerprints in metadata resolve to None so a
        corrupt cache never drives wallet scoping."""
        from jmwallet.cli.mnemonic import (
            _meta_path,
            load_mnemonic_meta_fingerprint,
        )

        mnemonic_file = tmp_path / "default.mnemonic"
        mnemonic_file.write_text("dummy")
        for bad in ('{"fingerprint": "nothex!!"}', '{"fingerprint": "aabb"}', '{"fingerprint": 5}'):
            _meta_path(mnemonic_file).write_text(bad)
            assert load_mnemonic_meta_fingerprint(mnemonic_file) is None

    def test_update_fingerprint_backfills_and_is_idempotent(self, tmp_path: Path) -> None:
        """update_mnemonic_meta_fingerprint writes a missing fingerprint and is a
        no-op (no needless rewrite) when it already matches."""
        from jmwallet.cli.mnemonic import (
            _meta_path,
            load_mnemonic_meta_fingerprint,
            update_mnemonic_meta_fingerprint,
        )

        mnemonic_file = tmp_path / "default.mnemonic"
        mnemonic_file.write_text("dummy")

        update_mnemonic_meta_fingerprint(mnemonic_file, "aabbccdd")
        assert load_mnemonic_meta_fingerprint(mnemonic_file) == "aabbccdd"

        meta_path = _meta_path(mnemonic_file)
        before = meta_path.stat().st_mtime_ns
        update_mnemonic_meta_fingerprint(mnemonic_file, "aabbccdd")
        assert meta_path.stat().st_mtime_ns == before  # idempotent: not rewritten


class TestResolveMnemonicCreationHeight:
    """Tests for resolve_mnemonic() loading creation_height from .meta files."""

    def test_resolve_mnemonic_loads_creation_height_from_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_mnemonic populates creation_height when .meta file exists."""
        import json

        from jmcore.cli_common import resolve_mnemonic
        from jmcore.settings import JoinMarketSettings

        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

        # Create plaintext mnemonic file
        wallets_dir = tmp_path / "wallets"
        wallets_dir.mkdir(parents=True)
        mnemonic_file = wallets_dir / "default.mnemonic"
        mnemonic_file.write_text(mnemonic)

        # Create companion .meta file
        meta_path = wallets_dir / "default.mnemonic.meta"
        meta_path.write_text(json.dumps({"creation_height": 820000}))

        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
        settings = JoinMarketSettings(data_dir=tmp_path)

        result = resolve_mnemonic(settings)
        assert result is not None
        assert result.mnemonic == mnemonic
        assert result.creation_height == 820000

    def test_resolve_mnemonic_no_meta_file_returns_none_creation_height(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_mnemonic returns creation_height=None when no .meta file."""
        from jmcore.cli_common import resolve_mnemonic
        from jmcore.settings import JoinMarketSettings

        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

        wallets_dir = tmp_path / "wallets"
        wallets_dir.mkdir(parents=True)
        mnemonic_file = wallets_dir / "default.mnemonic"
        mnemonic_file.write_text(mnemonic)

        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
        settings = JoinMarketSettings(data_dir=tmp_path)

        result = resolve_mnemonic(settings)
        assert result is not None
        assert result.creation_height is None

    def test_resolve_mnemonic_direct_mnemonic_has_no_creation_height(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_mnemonic from --mnemonic argument has no creation_height."""
        from jmcore.cli_common import resolve_mnemonic
        from jmcore.settings import JoinMarketSettings

        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
        settings = JoinMarketSettings()

        result = resolve_mnemonic(settings, mnemonic=mnemonic)
        assert result is not None
        assert result.creation_height is None

    def test_resolve_mnemonic_ignores_invalid_meta_creation_height(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """resolve_mnemonic ignores non-integer creation_height values in .meta."""
        import json

        from jmcore.cli_common import resolve_mnemonic
        from jmcore.settings import JoinMarketSettings

        mnemonic = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"

        wallets_dir = tmp_path / "wallets"
        wallets_dir.mkdir(parents=True)
        mnemonic_file = wallets_dir / "default.mnemonic"
        mnemonic_file.write_text(mnemonic)

        meta_path = wallets_dir / "default.mnemonic.meta"
        meta_path.write_text(json.dumps({"creation_height": "820000"}))

        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
        settings = JoinMarketSettings(data_dir=tmp_path)

        result = resolve_mnemonic(settings)
        assert result is not None
        assert result.creation_height is None
