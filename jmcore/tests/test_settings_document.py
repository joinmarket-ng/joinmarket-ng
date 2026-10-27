"""Persisted-candidate validation must not inherit process configuration."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jmcore import settings
from jmcore.config_file import ConfigFileError, update_config_values
from jmcore.settings import BitcoinSettings, validate_config_document


def test_ambient_taproot_cannot_make_a_partial_persisted_managed_config_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WALLET__ADDRESS_TYPE", "p2tr")
    with pytest.raises(ValidationError, match="requires wallet.address_type"):
        validate_config_document({"lightning": {"mode": "managed"}})


def test_invalid_ambient_settings_cannot_invalidate_document_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WALLET__ADDRESS_TYPE", "not-an-address-type")
    monkeypatch.setenv("LIGHTNING__MODE", "not-a-mode")
    monkeypatch.setenv("DIRECTORY_SERVERS", "[malformed-json")
    defaults = validate_config_document({})
    assert defaults.wallet.address_type == "p2wpkh"
    assert defaults.lightning.mode == "off"
    validate_config_document({"wallet": {"address_type": "p2wpkh"}, "lightning": {"mode": "off"}})


def test_document_validation_does_not_instantiate_the_toml_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("pure document validation must not discover or load a config file")

    monkeypatch.setattr(settings, "TomlConfigSettingsSource", forbidden)
    validate_config_document({"wallet": {"address_type": "p2tr"}, "lightning": {"mode": "managed"}})


def test_defaults_and_alias_normalization_do_not_mutate_the_document(tmp_path: Path) -> None:
    document = {
        "data_dir": str(tmp_path / "not-created"),
        "wallet": {"address_type": "p2tr", "wallet_with_passphrase": True},
        "lightning": {"mode": "managed"},
        "future_section": {"future_field": "preserved by the editor"},
    }
    before = copy.deepcopy(document)
    validate_config_document(document)
    assert document == before
    assert not (tmp_path / "not-created").exists()


def test_candidate_local_legacy_alias_conflicts_still_fail() -> None:
    with pytest.raises(ValidationError):
        validate_config_document(
            {
                "wallet": {"wallet_with_passphrase": True, "bip39_passphrase_enabled": False},
            }
        )


def test_unknown_root_names_cannot_become_settings_constructor_controls(tmp_path: Path) -> None:
    invalid_dotenv = tmp_path / "must-not-be-read.env"
    invalid_dotenv.write_bytes(b"\xff\xfe")
    validate_config_document(
        {
            "_env_file": str(invalid_dotenv),
            "_secrets_dir": str(tmp_path / "missing-secrets"),
            "_env_prefix": "MUST_NOT_APPLY_",
        }
    )


@pytest.mark.parametrize("field", ["rpc_cookie_file", "neutrino_auth_token_file"])
def test_document_validation_retains_credential_paths_without_reading_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    credential = tmp_path / "must-not-be-read"
    credential.write_bytes(b"\xff\xfe")
    real_read = Path.read_text

    def read(path: Path, *args: Any, **kwargs: Any) -> str:
        if path == credential:
            pytest.fail("document validation must not read credential files")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    result = validate_config_document({"bitcoin": {field: str(credential)}})
    assert getattr(result.bitcoin, field) == str(credential)
    assert result.bitcoin.rpc_user == ""
    assert result.bitcoin.rpc_password.get_secret_value() == ""
    assert result.bitcoin.neutrino_auth_token is None


def test_runtime_settings_still_load_credentials_and_ignore_internal_policy_input(
    tmp_path: Path,
) -> None:
    cookie = tmp_path / ".cookie"
    cookie.write_text("__cookie__:runtime-password")
    token = tmp_path / "token"
    token.write_text("runtime-token")
    values: Any = {
        "rpc_cookie_file": str(cookie),
        "neutrino_auth_token_file": str(token),
        "_load_credential_files": False,
    }
    result = BitcoinSettings(**values)
    assert result.rpc_user == "__cookie__"
    assert result.rpc_password.get_secret_value() == "runtime-password"
    assert result.neutrino_auth_token == "runtime-token"


def test_atomic_editor_uses_schema_without_environment_masking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "config.toml"
    original = '# Existing comment\n[lightning]\nmode = "off"\n'
    path.write_text(original)
    monkeypatch.setenv("WALLET__ADDRESS_TYPE", "p2tr")
    with pytest.raises(ConfigFileError, match="candidate validation failed"):
        update_config_values(
            path, {"lightning": {"mode": "managed"}}, validate=validate_config_document
        )
    assert path.read_text() == original
    update_config_values(
        path,
        {
            "wallet": {"address_type": "p2tr"},
            "lightning": {"mode": "managed"},
        },
        validate=validate_config_document,
    )
    parsed = tomllib.loads(path.read_text())
    assert parsed == {"wallet": {"address_type": "p2tr"}, "lightning": {"mode": "managed"}}
    assert "# Existing comment" in path.read_text()
