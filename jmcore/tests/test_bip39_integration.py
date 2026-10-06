from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jmcore.cli_common import bip39_prompt_required, resolve_mnemonic
from jmcore.settings import JoinMarketSettings, WalletSettings, reset_settings
from jmcore.wallet_metadata import WalletIdentity, register_identity, select_identity

MNEMONIC = "abandon " * 11 + "about"


def test_preference_alias_is_one_value() -> None:
    settings = WalletSettings(wallet_with_passphrase=True)
    assert settings.bip39_passphrase_enabled
    assert settings.wallet_with_passphrase
    settings.wallet_with_passphrase = False
    assert not settings.bip39_passphrase_enabled
    assert "wallet_with_passphrase" not in settings.model_dump()
    with pytest.raises(ValidationError, match="Conflicting BIP39"):
        WalletSettings(wallet_with_passphrase=True, bip39_passphrase_enabled=False)
    equivalent = WalletSettings.model_validate(
        {
            "wallet_with_passphrase": True,
            "bip39_passphrase_enabled": "true",
        }
    )
    assert equivalent.bip39_passphrase_enabled


@pytest.mark.parametrize("spelling", ["bip39_passphrase_enabled", "wallet_with_passphrase"])
def test_preference_config_and_environment_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spelling: str,
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(f"[wallet]\n{spelling} = true\n")
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(config))
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
    reset_settings()
    try:
        settings = JoinMarketSettings()
        assert settings.wallet.bip39_passphrase_enabled
        config.write_text("[wallet]\n")
        monkeypatch.setenv(f"WALLET__{spelling.upper()}", "true")
        settings = JoinMarketSettings()
        assert settings.wallet.bip39_passphrase_enabled
    finally:
        reset_settings()


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("status", [None, "none", "required", "unknown"])
def test_identity_requirement_overrides_onboarding(enabled: bool, status: str | None) -> None:
    settings = JoinMarketSettings(wallet={"bip39_passphrase_enabled": enabled})
    identity = (
        WalletIdentity.model_validate({"fingerprint": "11223344", "bip39": status})
        if status
        else None
    )
    assert bip39_prompt_required(settings, identity) is (
        status == "required" or (status in {None, "unknown"} and enabled)
    )


def test_staged_fingerprint_is_enforced_by_real_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("MNEMONIC", "MNEMONIC_FILE", "BIP39_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("EXPECTED_FINGERPRINT", "11223344")
    source = tmp_path / "wallet.mnemonic"
    source.write_text(MNEMONIC)
    settings = JoinMarketSettings()
    with pytest.raises(ValueError, match="Staged wallet fingerprint mismatch"):
        resolve_mnemonic(settings, mnemonic_file=source, bip39_passphrase="test passphrase")
    assert not source.with_suffix(".mnemonic.meta").exists()


def test_enabled_alias_does_not_prompt_registered_empty_wallet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

    for name in ("MNEMONIC", "MNEMONIC_FILE", "BIP39_PASSPHRASE", "EXPECTED_FINGERPRINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "absent.toml"))
    source = tmp_path / "wallet.mnemonic"
    source.write_text(MNEMONIC)
    identity = WalletIdentity(fingerprint=get_mnemonic_fingerprint(MNEMONIC), bip39="none")
    register_identity(source, identity)
    select_identity(source, identity.fingerprint)
    settings = JoinMarketSettings(wallet={"wallet_with_passphrase": True})
    resolved = resolve_mnemonic(settings, mnemonic_file=source)
    assert resolved is not None and resolved.bip39_passphrase == ""
