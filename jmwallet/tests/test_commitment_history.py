"""Commitment history is created only for a data directory's first generated wallet."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.commitment_history import (
    generated_wallet_is_authorized,
    initialize_commitment_history_for_new_wallet,
    is_first_wallet_in_data_dir,
)

runner = CliRunner()
_MNEMONIC = "abandon " * 11 + "about"
_OTHER_MNEMONIC = "legal winner thank year wave sausage worth useful legal winner thank yellow"


def _history(data_dir: Path) -> Path:
    return data_dir / "cmtdata" / "commitments.json"


def _initialize(data_dir: Path, mnemonic: str = _MNEMONIC) -> None:
    wallet = data_dir / "wallets" / "new.mnemonic"
    first = is_first_wallet_in_data_dir(data_dir, wallet)
    wallet.parent.mkdir(parents=True, exist_ok=True)
    wallet.write_bytes(b"saved generated wallet")
    initialize_commitment_history_for_new_wallet(
        data_dir,
        mnemonic=mnemonic,
        wallet_file=wallet,
        first_wallet=first,
    )


@pytest.fixture(autouse=True)
def _offline_creation_height_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _unreachable(*args: object, **kwargs: object) -> int:
        raise ConnectionError("test: backend unreachable")

    monkeypatch.setattr("jmwallet.cli.wallet._fetch_current_block_height", _unreachable)


@pytest.mark.parametrize("wallets_dir_state", ["missing", "empty", "lock-only"])
def test_unused_data_dir_starts_with_first_wallet(tmp_path: Path, wallets_dir_state: str) -> None:
    wallets = tmp_path / "wallets"
    if wallets_dir_state != "missing":
        wallets.mkdir()
    if wallets_dir_state == "lock-only":
        (wallets / ".wallet-operation.lock").touch()
    (tmp_path / "cmtdata").mkdir()
    assert is_first_wallet_in_data_dir(tmp_path, wallets / "new.jmdat")


@pytest.mark.parametrize(
    "existing",
    [
        "wallets/old.jmdat",
        "wallets/default.mnemonic",
        "wallets/.old.jmdat",
        "wallets/.old.mnemonic",
        "wallets/.unknown-lock",
        "history.csv",
        "coinjoin_history.csv",
        "cmtdata/commitments.json",
        "cmtdata/commitments.json.corrupt.1790000000",
        "cmtdata/commitmentlist",
        "cmtdata/.commitments.json.tmp",
    ],
)
def test_prior_wallet_activity_blocks_initialization(tmp_path: Path, existing: str) -> None:
    path = tmp_path / existing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    assert not is_first_wallet_in_data_dir(tmp_path, tmp_path / "wallets" / "new.jmdat")


def test_wallet_outside_data_dir_wallets_is_not_first(tmp_path: Path) -> None:
    assert not is_first_wallet_in_data_dir(tmp_path, tmp_path / "elsewhere" / "new.mnemonic")


def test_wallets_path_that_is_not_a_directory_is_not_first(tmp_path: Path) -> None:
    (tmp_path / "wallets").write_text("x")
    assert not is_first_wallet_in_data_dir(tmp_path, tmp_path / "wallets" / "new.jmdat")


def test_initialization_writes_private_empty_history(tmp_path: Path) -> None:
    _initialize(tmp_path)
    history = _history(tmp_path)
    assert json.loads(history.read_text()) == {
        "used": [],
        "external": {},
        "generated_wallets_only": True,
    }
    assert stat.S_IMODE(history.stat().st_mode) == 0o600
    proof = next((history.parent / "generated-wallets").iterdir())
    assert stat.S_IMODE(proof.stat().st_mode) == 0o600
    assert stat.S_IMODE(proof.parent.stat().st_mode) == 0o700
    assert _MNEMONIC.encode() not in proof.read_bytes()
    assert generated_wallet_is_authorized(tmp_path, _MNEMONIC)
    assert not generated_wallet_is_authorized(tmp_path, _OTHER_MNEMONIC)


def test_initialization_never_replaces_existing_history(tmp_path: Path) -> None:
    history = _history(tmp_path)
    history.parent.mkdir(parents=True)
    history.write_text('{"used": ["ab"], "external": {}}')
    _initialize(tmp_path)
    assert history.read_text() == '{"used": ["ab"], "external": {}}'


def test_initialization_failure_is_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_data_dir: Path, **_kwargs: object) -> Path:
        raise PermissionError("read-only data directory")

    monkeypatch.setattr("jmwallet.commitment_history.create_commitment_history", fail)
    _initialize(tmp_path)
    assert not _history(tmp_path).exists()


def _generate(*args: str) -> None:
    result = runner.invoke(
        app,
        ["generate", "--no-prompt-password", "--no-register-identity", *args],
    )
    assert result.exit_code == 0, result.output


def test_generate_initializes_history_for_first_wallet(tmp_path: Path) -> None:
    _generate("--data-dir", str(tmp_path))
    assert (tmp_path / "wallets" / "default.mnemonic").exists()
    assert json.loads(_history(tmp_path).read_text()) == {
        "used": [],
        "external": {},
        "generated_wallets_only": True,
    }


def test_generate_skips_data_dir_with_another_wallet(tmp_path: Path) -> None:
    _generate("--data-dir", str(tmp_path), "--output", str(tmp_path / "wallets" / "a.mnemonic"))
    _history(tmp_path).unlink()
    _generate("--data-dir", str(tmp_path), "--output", str(tmp_path / "wallets" / "b.mnemonic"))
    assert not _history(tmp_path).exists()


def test_generate_skips_overwritten_wallet(tmp_path: Path) -> None:
    wallet = tmp_path / "wallets" / "default.mnemonic"
    wallet.parent.mkdir()
    wallet.write_text("previous wallet")
    _generate("--data-dir", str(tmp_path), "--force")
    assert not _history(tmp_path).exists()


def test_generate_skips_data_dir_with_history(tmp_path: Path) -> None:
    (tmp_path / "history.csv").write_text("")
    _generate("--data-dir", str(tmp_path))
    assert not _history(tmp_path).exists()


def test_generate_skips_unsaved_and_external_wallets(tmp_path: Path) -> None:
    _generate("--data-dir", str(tmp_path), "--no-save")
    _generate("--data-dir", str(tmp_path), "--output", str(tmp_path / "elsewhere.mnemonic"))
    assert not _history(tmp_path).exists()


def test_generate_keeps_history_missing_beside_hidden_wallet(tmp_path: Path) -> None:
    hidden = tmp_path / "wallets" / ".old.mnemonic"
    hidden.parent.mkdir()
    hidden.write_text(_OTHER_MNEMONIC)
    _generate("--data-dir", str(tmp_path))
    assert hidden.read_text() == _OTHER_MNEMONIC
    assert not _history(tmp_path).exists()


@pytest.mark.parametrize(
    "new_state", ["wallets/recovered.jmdat", "cmtdata/commitments.json.corrupt.1"]
)
def test_generation_rechecks_activity_after_awaited_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    new_state: str,
) -> None:
    late_file = tmp_path / new_state

    def record_height(_wallet_file: Path) -> None:
        late_file.parent.mkdir(parents=True, exist_ok=True)
        late_file.write_bytes(b"prior wallet evidence")

    monkeypatch.setattr("jmwallet.cli.wallet._record_wallet_creation_height", record_height)
    _generate("--data-dir", str(tmp_path))
    assert late_file.read_bytes() == b"prior wallet evidence"
    assert not _history(tmp_path).exists()


def test_new_generation_can_enroll_without_rewriting_existing_history(tmp_path: Path) -> None:
    _initialize(tmp_path)
    before = _history(tmp_path).read_bytes()
    _initialize(tmp_path, _OTHER_MNEMONIC)
    assert _history(tmp_path).read_bytes() == before
    assert generated_wallet_is_authorized(tmp_path, _MNEMONIC)
    assert generated_wallet_is_authorized(tmp_path, _OTHER_MNEMONIC)


def test_missing_history_and_surviving_authorization_cannot_trigger_reset(tmp_path: Path) -> None:
    _initialize(tmp_path)
    _history(tmp_path).unlink()
    _initialize(tmp_path, _OTHER_MNEMONIC)
    assert not _history(tmp_path).exists()


def test_legacy_history_does_not_gain_an_automatic_restriction(tmp_path: Path) -> None:
    path = _history(tmp_path)
    path.parent.mkdir()
    original = b'{"used": ["ab"], "external": {}, "extension": 7}'
    path.write_bytes(original)
    _initialize(tmp_path)
    assert path.read_bytes() == original
    assert not (path.parent / "generated-wallets").exists()
