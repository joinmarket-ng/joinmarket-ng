from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from jmcore.cli_common import resolve_mnemonic, select_mnemonic_source
from jmcore.settings import JoinMarketSettings
from jmcore.wallet_metadata import (
    WalletIdentity,
    load_mnemonic_meta,
    meta_path,
    register_identity,
    select_identity,
    selected_identity,
)
from pydantic import SecretStr
from typer.testing import CliRunner

from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
from jmwallet.cli import app
from jmwallet.cli._wallet_selection import resolve_wallet_fingerprint
from jmwallet.cli.mnemonic import (
    load_mnemonic_meta_fingerprint,
    save_mnemonic_meta,
    update_mnemonic_meta_fingerprint,
)

MNEMONIC = "abandon " * 11 + "about"
PASSPHRASE = "test identity passphrase"


@pytest.fixture
def wallet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, JoinMarketSettings]:
    for key in ("MNEMONIC", "MNEMONIC_FILE", "BIP39_PASSPHRASE", "WALLET__BIP39_PASSPHRASE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(tmp_path / "config.toml"))
    source = tmp_path / "wallet.mnemonic"
    source.write_text(MNEMONIC)
    settings = JoinMarketSettings()
    settings.wallet.mnemonic_file = str(source)
    return source, settings


def _select(source: Path, passphrase: str) -> str:
    fp = get_mnemonic_fingerprint(MNEMONIC, passphrase)
    register_identity(
        source, WalletIdentity(fingerprint=fp, bip39="required" if passphrase else "none")
    )
    select_identity(source, fp)
    return fp


def _offline(settings: JoinMarketSettings, **kwargs: object) -> str | None:
    return resolve_wallet_fingerprint(
        settings,
        mnemonic_file=kwargs.get("file"),  # type: ignore[arg-type]
        wallet_fingerprint=None,
        prompt_bip39_passphrase=bool(kwargs.get("prompt")),
        list_known_fingerprints=lambda: [],
        command_label="test",
        fall_back_to_configured_mnemonic=True,
    )


def test_cached_configured_wallet_honors_prompt(wallet: tuple[Path, JoinMarketSettings]) -> None:
    source, settings = wallet
    old = get_mnemonic_fingerprint(MNEMONIC)
    save_mnemonic_meta(source, fingerprint=old)
    with patch("typer.prompt", return_value=PASSPHRASE), patch("typer.confirm", return_value=True):
        assert _offline(settings, prompt=True) == get_mnemonic_fingerprint(MNEMONIC, PASSPHRASE)
    assert load_mnemonic_meta_fingerprint(source) == old


def test_supplied_passphrase_bypasses_legacy_cache(wallet: tuple[Path, JoinMarketSettings]) -> None:
    source, settings = wallet
    save_mnemonic_meta(source, fingerprint=get_mnemonic_fingerprint(MNEMONIC))
    settings.wallet.bip39_passphrase = SecretStr(PASSPHRASE)
    assert _offline(settings) == get_mnemonic_fingerprint(MNEMONIC, PASSPHRASE)


def test_registered_explicit_file_read_does_not_unlock(
    wallet: tuple[Path, JoinMarketSettings],
) -> None:
    source, settings = wallet
    fp = _select(source, PASSPHRASE)
    source.write_bytes(b"encrypted wallet placeholder")
    with patch(
        "jmwallet.cli._wallet_selection.resolve_mnemonic", side_effect=AssertionError("unlocked")
    ):
        assert _offline(settings, file=source) == fp


def test_registered_required_prompts_with_setting_disabled(
    wallet: tuple[Path, JoinMarketSettings],
) -> None:
    source, settings = wallet
    _select(source, PASSPHRASE)
    assert not settings.wallet.bip39_passphrase_enabled
    with patch("typer.prompt", return_value=PASSPHRASE), patch("typer.confirm", return_value=True):
        resolved = resolve_mnemonic(settings)
    assert resolved is not None and resolved.bip39_passphrase == PASSPHRASE


def test_registered_none_skips_enabled_prompt(wallet: tuple[Path, JoinMarketSettings]) -> None:
    source, settings = wallet
    _select(source, "")
    settings.wallet.bip39_passphrase_enabled = True
    with patch("typer.prompt", side_effect=AssertionError("prompted")):
        resolved = resolve_mnemonic(settings)
    assert resolved is not None and resolved.bip39_passphrase == ""


def test_identity_mismatch_never_rewrites(wallet: tuple[Path, JoinMarketSettings]) -> None:
    source, settings = wallet
    _select(source, PASSPHRASE)
    before = meta_path(source).read_bytes()
    with pytest.raises(ValueError, match="does not match"):
        resolve_mnemonic(settings, bip39_passphrase="wrong passphrase")
    assert meta_path(source).read_bytes() == before


def test_raw_mnemonic_has_no_unrelated_file_provenance(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, settings = wallet
    save_mnemonic_meta(source, fingerprint="11223344")
    before = meta_path(source).read_bytes()
    monkeypatch.setenv("MNEMONIC", MNEMONIC)
    selected = select_mnemonic_source(settings)
    assert selected is not None and selected.path is None
    assert _offline(settings) == get_mnemonic_fingerprint(MNEMONIC)
    assert meta_path(source).read_bytes() == before


def test_register_and_select_are_separate(wallet: tuple[Path, JoinMarketSettings]) -> None:
    source, _ = wallet
    runner = CliRunner()
    result = runner.invoke(
        app, ["identity", "register", "-f", str(source), "--no-bip39-passphrase", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "Selection unchanged" in result.output
    result = runner.invoke(
        app, ["identity", "select", get_mnemonic_fingerprint(MNEMONIC), "-f", str(source)]
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["identity", "list", "-f", str(source), "--json"])
    assert result.exit_code == 0, result.output
    assert '"selected": true' in result.output


@pytest.mark.parametrize("command", ["history", "list-bonds", "registry-show"])
def test_offline_commands_honor_prompt_with_cached_default(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    source, _ = wallet
    save_mnemonic_meta(source, fingerprint=get_mnemonic_fingerprint(MNEMONIC))
    monkeypatch.setenv("MNEMONIC_FILE", str(source))
    config = source.parent / "config.toml"
    config.write_text('[bitcoin]\nnetwork = "regtest"\n')
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(config))
    with (
        patch("typer.prompt", return_value=PASSPHRASE) as prompt,
        patch("typer.confirm", return_value=True),
    ):
        args = [command, "--data-dir", str(source.parent), "--prompt-bip39-passphrase"]
        if command == "registry-show":
            args.append("bcrt1qtest-bond-address")
        result = CliRunner().invoke(app, args)
    assert result.exit_code == (1 if command == "registry-show" else 0), result.output
    if command == "registry-show":
        assert "Bond not found" in result.output
    prompt.assert_called_once()


@pytest.mark.parametrize("enabled", [True, False])
def test_import_onboarding_setting_roundtrip(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    source, _ = wallet
    output = source.parent / "imported.mnemonic"
    config = source.parent / "config.toml"
    config.write_text(
        '[bitcoin]\nnetwork = "regtest"\n[wallet]\nbip39_passphrase_enabled = '
        + str(enabled).lower()
        + "\n"
    )
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(config))
    monkeypatch.setenv("MNEMONIC", MNEMONIC)
    monkeypatch.setenv("BIP39_PASSPHRASE", PASSPHRASE)
    result = CliRunner().invoke(
        app,
        ["import", "--no-prompt-password", "--output", str(output)],
        input=PASSPHRASE + "\ny\n" if enabled else "",
    )
    assert result.exit_code == 0, result.output
    assert PASSPHRASE not in result.output
    assert output.read_text() == MNEMONIC
    assert load_mnemonic_meta(output)["fidelity_bond_recovery"] == "pending"
    identity = selected_identity(output)
    if enabled:
        assert identity is not None and identity.bip39 == "required"
        assert identity.fingerprint == get_mnemonic_fingerprint(MNEMONIC, PASSPHRASE)
    else:
        assert identity is None


def test_canceled_onboarding_preserves_saved_seed(
    wallet: tuple[Path, JoinMarketSettings],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = wallet
    output = source.parent / "imported.mnemonic"
    monkeypatch.setenv("MNEMONIC", MNEMONIC)
    result = CliRunner().invoke(
        app,
        ["import", "--no-prompt-password", "--register-identity", "--output", str(output)],
        input="n\n",
    )
    assert result.exit_code == 1, result.output
    assert output.read_text() == MNEMONIC
    assert "saved seed was not deleted" in result.output
    assert selected_identity(output) is None


@pytest.mark.parametrize("enabled", [True, False])
def test_selected_unknown_respects_prompt_preference(
    wallet: tuple[Path, JoinMarketSettings],
    enabled: bool,
) -> None:
    source, settings = wallet
    fp = get_mnemonic_fingerprint(MNEMONIC, PASSPHRASE)
    register_identity(source, WalletIdentity(fingerprint=fp, bip39="unknown"))
    select_identity(source, fp)
    settings.wallet.bip39_passphrase_enabled = enabled
    with (
        patch("jmcore.confirmation.is_interactive_mode", return_value=True),
        patch("typer.prompt", return_value=PASSPHRASE) as prompt,
        patch("typer.confirm", return_value=True),
    ):
        if enabled:
            assert resolve_mnemonic(settings).bip39_passphrase == PASSPHRASE  # type: ignore[union-attr]
            prompt.assert_called_once()
        else:
            with pytest.raises(ValueError, match="does not match"):
                resolve_mnemonic(settings)
            prompt.assert_not_called()
    settings.wallet.bip39_passphrase = SecretStr(PASSPHRASE)
    with patch("typer.prompt", side_effect=AssertionError("prompted")):
        assert resolve_mnemonic(settings).bip39_passphrase == PASSPHRASE  # type: ignore[union-attr]


def test_competing_selection_cannot_be_overwritten_by_backfill(
    wallet: tuple[Path, JoinMarketSettings],
) -> None:
    source, _ = wallet
    from jmwallet.cli.mnemonic import _mnemonic_meta_lock

    @contextmanager
    def competing_selection(path: Path) -> Iterator[None]:
        _select(path, PASSPHRASE)
        with _mnemonic_meta_lock(path):
            yield

    with patch("jmwallet.cli.mnemonic._mnemonic_meta_lock", competing_selection):
        update_mnemonic_meta_fingerprint(source, get_mnemonic_fingerprint(MNEMONIC))
    expected = get_mnemonic_fingerprint(MNEMONIC, PASSPHRASE)
    assert load_mnemonic_meta_fingerprint(source) == expected
    assert selected_identity(source).fingerprint == expected  # type: ignore[union-attr]


def test_shared_seed_deletion_is_rejected(wallet: tuple[Path, JoinMarketSettings]) -> None:
    source, _ = wallet
    _select(source, "")
    register_identity(
        source,
        WalletIdentity(
            fingerprint=get_mnemonic_fingerprint(MNEMONIC, PASSPHRASE), bip39="required"
        ),
    )
    before = meta_path(source).read_bytes()
    result = CliRunner().invoke(app, ["delete", "-f", str(source), "--yes"])
    assert result.exit_code == 1, result.output
    assert "multiple registered wallet identities" in result.output
    assert source.read_text() == MNEMONIC
    assert meta_path(source).read_bytes() == before


def test_partial_collection_cannot_authorize_seed_deletion(
    wallet: tuple[Path, JoinMarketSettings],
) -> None:
    import json

    source, _ = wallet
    _select(source, "")
    meta = load_mnemonic_meta(source)
    meta["identities"]["11223344"] = {"bip39": "invalid"}
    meta_path(source).write_text(json.dumps(meta))
    before = meta_path(source).read_bytes()
    result = CliRunner().invoke(
        app, ["delete", "-f", str(source), "--yes", "--allow-fingerprint-mismatch"]
    )
    assert result.exit_code == 1, result.output
    assert "unconfirmed entries" in result.output
    assert source.read_text() == MNEMONIC
    assert meta_path(source).read_bytes() == before
