from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from jmcore.settings import JoinMarketSettings
from jmcore.wallet_metadata import (
    WalletIdentity,
    register_identity,
    select_identity,
    selected_identity,
)
from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
from typer.testing import CliRunner

from maker.cli import app

MNEMONIC = "abandon " * 11 + "about"
PASSPHRASE = "test staged passphrase"


@pytest.fixture
def wallet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, JoinMarketSettings]:
    for name in (
        "MNEMONIC",
        "MNEMONIC_FILE",
        "BIP39_PASSPHRASE",
        "EXPECTED_FINGERPRINT",
        "JOINMARKET_STAGED_CREDENTIALS_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "absent.toml"))
    source = tmp_path / "wallet.mnemonic"
    source.write_text(MNEMONIC)
    return source, JoinMarketSettings(data_dir=tmp_path)


def _invoke(
    source: Path,
    settings: JoinMarketSettings,
    stage: Path | None,
) -> tuple[bool, str]:
    """Exercise real credential resolution, stopping at config construction."""
    args = ["start", "--mnemonic-file", str(source)]
    if stage is not None:
        args += ["--staged-credentials-file", str(stage)]
    with (
        patch("maker.cli.setup_cli", return_value=settings),
        patch("maker.cli.ensure_config_file"),
        patch("jmcore.process_hardening.harden_current_process"),
        patch(
            "maker.cli.build_maker_config", side_effect=ValueError("TEST_CONFIG_BOUNDARY")
        ) as build,
        patch("maker.cli.create_wallet_service") as backend,
    ):
        result = CliRunner().invoke(app, args)
    backend.assert_not_called()
    assert result.exit_code == 1, result.output
    assert PASSPHRASE not in result.output
    return build.called, result.output


@pytest.mark.parametrize(
    "content",
    [
        "",
        "# interrupted staging\n",
        'UNRELATED_KEY="value"\n',
        'BIP39_PASSPHRASE=""\n',
        f'BIP39_PASSPHRASE="{PASSPHRASE}"\n',
        'BIP39_PASSPHRASE=""\nEXPECTED_FINGERPRINT="invalid"\n',
        'BIP39_PASSPHRASE=""\nBIP39_PASSPHRASE="other"\n',
        'EXPECTED_FINGERPRINT="11223344"\nEXPECTED_FINGERPRINT="55667788"\n',
        'BIP39_PASSPHRASE="unterminated\n',
        "export BIP39_PASSPHRASE=example\n",
    ],
)
def test_declared_unbound_or_malformed_staging_rejects_before_backend(
    wallet: tuple[Path, JoinMarketSettings],
    content: str,
) -> None:
    source, settings = wallet
    stage = source.parent / ".maker.env"
    stage.write_text(content)
    assert not _invoke(source, settings, stage)[0]


@pytest.mark.parametrize("passphrase", ["", PASSPHRASE])
@pytest.mark.parametrize(
    "fingerprint_state", ["matching", "mismatch", "env-mismatch", "selected-mismatch"]
)
def test_declared_fingerprint_checks_effective_identity(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
    fingerprint_state: str,
) -> None:
    source, settings = wallet
    stage = source.parent / ".maker.env"
    actual = get_mnemonic_fingerprint(MNEMONIC, passphrase)
    expected = "11223344" if fingerprint_state == "mismatch" else actual
    stage.write_text(f'BIP39_PASSPHRASE="{passphrase}"\nEXPECTED_FINGERPRINT="{expected}"\n')
    monkeypatch.setenv("BIP39_PASSPHRASE", passphrase)
    if fingerprint_state == "env-mismatch":
        monkeypatch.setenv("EXPECTED_FINGERPRINT", "11223344")
    if fingerprint_state == "selected-mismatch":
        register_identity(source, WalletIdentity(fingerprint="11223344", bip39="unknown"))
        select_identity(source, "11223344")
    assert _invoke(source, settings, stage)[0] is (fingerprint_state == "matching")


@pytest.mark.parametrize("separator", ["\n", "\u0085", "\u2028", "\r\n"])
@pytest.mark.parametrize("duplicate", [True, False])
def test_quoted_staging_separators_preserve_wallet_and_duplicate_checks(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
    separator: str,
    duplicate: bool,
) -> None:
    source, settings = wallet
    passphrase = f'one  {separator}  "two" \\ three  '
    fingerprint = get_mnemonic_fingerprint(MNEMONIC, passphrase)
    escaped = passphrase.replace("\\", "\\\\").replace('"', '\\"')
    content = f'BIP39_PASSPHRASE="{escaped}"\nEXPECTED_FINGERPRINT="{fingerprint}"\n'
    if duplicate:
        content += f'EXPECTED_FINGERPRINT="{fingerprint}"\n'
    stage = source.parent / ".maker.env"
    # Preserve CR inside quoted values instead of translating text-mode newlines.
    stage.write_bytes(content.encode("utf-8"))
    monkeypatch.setenv("BIP39_PASSPHRASE", passphrase)
    built, output = _invoke(source, settings, stage)
    assert built is not duplicate
    assert passphrase not in output


@pytest.mark.parametrize("configured", [None, "", PASSPHRASE])
def test_missing_declared_staging_requires_affirmative_identity(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
    configured: str | None,
) -> None:
    from pydantic import SecretStr

    source, settings = wallet
    if configured is not None:
        settings.wallet.bip39_passphrase = SecretStr(configured)
    # Inherited stage credentials alone do not authorize the missing-file fallback.
    monkeypatch.setenv("BIP39_PASSPHRASE", PASSPHRASE)
    assert _invoke(source, settings, source.parent / ".maker.env")[0] is (configured == PASSPHRASE)


@pytest.mark.parametrize("configured", ["", PASSPHRASE])
@pytest.mark.parametrize("environment", [None, "", PASSPHRASE, "different test credential"])
def test_config_authorizes_only_effective_config_credential_when_staging_missing(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
    environment: str | None,
    configured: str,
) -> None:
    from pydantic import SecretStr

    source, settings = wallet
    settings.wallet.bip39_passphrase = SecretStr(configured)
    if environment is not None:
        monkeypatch.setenv("BIP39_PASSPHRASE", environment)
    assert _invoke(source, settings, source.parent / ".maker.env")[0] is (
        environment in {None, "", configured}
    )


def test_missing_stage_conflict_comparison_uses_bip39_normalization(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    source, settings = wallet
    settings.wallet.bip39_passphrase = SecretStr("caf\u00e9")
    monkeypatch.setenv("BIP39_PASSPHRASE", "cafe\u0301")
    assert _invoke(source, settings, source.parent / ".maker.env")[0]


def test_selected_identity_can_authorize_environment_over_config(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    source, settings = wallet
    settings.wallet.bip39_passphrase = SecretStr("different configured credential")
    monkeypatch.setenv("BIP39_PASSPHRASE", PASSPHRASE)
    assert _invoke(source, settings, None)[0]  # Undeclared manual override still works.
    fingerprint = get_mnemonic_fingerprint(MNEMONIC, PASSPHRASE)
    register_identity(source, WalletIdentity(fingerprint=fingerprint, bip39="required"))
    select_identity(source, fingerprint)
    monkeypatch.setenv("BIP39_PASSPHRASE", PASSPHRASE)
    assert _invoke(source, settings, source.parent / ".maker.env")[0]


@pytest.mark.parametrize("status", ["none", "required"])
@pytest.mark.parametrize("provided", [False, True])
def test_missing_declared_staging_validates_selected_identity(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    provided: bool,
) -> None:
    source, settings = wallet
    passphrase = PASSPHRASE if status == "required" else ""
    fingerprint = get_mnemonic_fingerprint(MNEMONIC, passphrase)
    register_identity(source, WalletIdentity(fingerprint=fingerprint, bip39=status))
    select_identity(source, fingerprint)
    if provided:
        monkeypatch.setenv("BIP39_PASSPHRASE", passphrase)
    assert _invoke(source, settings, source.parent / ".maker.env")[0] is (
        provided or status == "none"
    )


def test_legacy_rejection_then_cleanup_cannot_silently_fallback(
    wallet: tuple[Path, JoinMarketSettings],
) -> None:
    source, settings = wallet
    stage = source.parent / ".maker.env"
    stage.write_text(f'BIP39_PASSPHRASE="{PASSPHRASE}"\n')
    assert not _invoke(source, settings, stage)[0]
    stage.unlink()  # Appliance ExecStopPost, followed by automatic retry.
    assert not _invoke(source, settings, stage)[0]


def test_password_only_legacy_and_manual_invocation_preserve_behavior(
    wallet: tuple[Path, JoinMarketSettings],
) -> None:
    source, settings = wallet
    stage = source.parent / ".maker.env"
    stage.write_text('MNEMONIC_PASSWORD="test password"\n')
    assert _invoke(source, settings, stage)[0]
    stage.write_text(f'BIP39_PASSPHRASE="{PASSPHRASE}"\n')
    assert _invoke(source, settings, None)[0]


@pytest.mark.parametrize("passphrase", ["", PASSPHRASE])
@pytest.mark.parametrize("hint", ["matching", "different", "absent"])
def test_existing_headless_maker_keeps_starting_after_upgrade(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
    passphrase: str,
    hint: str,
) -> None:
    """Units written for earlier releases declare no staging and need no operator action."""
    source, settings = wallet
    fingerprint = get_mnemonic_fingerprint(MNEMONIC, passphrase)
    if hint != "absent":
        recorded = fingerprint if hint == "matching" else "11223344"
        source.with_name(source.name + ".meta").write_text(f'{{"fingerprint":"{recorded}"}}')
    if passphrase:
        monkeypatch.setenv("BIP39_PASSPHRASE", passphrase)
    for _ in range(2):  # Upgrade start, then an automatic restart.
        assert _invoke(source, settings, None)[0]
    identity = selected_identity(source)
    if hint == "matching":
        assert identity == WalletIdentity(
            fingerprint=fingerprint, bip39="required" if passphrase else "none"
        )
    else:
        assert identity is None


def test_service_environment_declares_source_even_without_cli_option(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, settings = wallet
    stage = source.parent / ".maker.env"
    stage.write_text(f'BIP39_PASSPHRASE="{PASSPHRASE}"\n')
    monkeypatch.setenv("JOINMARKET_STAGED_CREDENTIALS_FILE", str(stage))
    assert not _invoke(source, settings, None)[0]


@pytest.mark.parametrize("kind", ["directory", "symlink", "invalid-utf8"])
def test_unreadable_declared_staging_rejects_before_backend(
    wallet: tuple[Path, JoinMarketSettings],
    kind: str,
) -> None:
    source, settings = wallet
    stage = source.parent / ".maker.env"
    if kind == "directory":
        stage.mkdir()
    elif kind == "symlink":
        stage.symlink_to(source)
    else:
        stage.write_bytes(b"\xff")
    assert not _invoke(source, settings, stage)[0]
