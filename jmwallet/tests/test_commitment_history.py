"""Commitment history is created only for a data directory's first generated wallet."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.commitment_history import (
    initialize_commitment_history_for_new_wallet,
    is_first_wallet_in_data_dir,
)

runner = CliRunner()


def _history(data_dir: Path) -> Path:
    return data_dir / "cmtdata" / "commitments.json"


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
    initialize_commitment_history_for_new_wallet(tmp_path)
    history = _history(tmp_path)
    assert json.loads(history.read_text()) == {"used": [], "external": {}}
    assert stat.S_IMODE(history.stat().st_mode) == 0o600


def test_initialization_never_replaces_existing_history(tmp_path: Path) -> None:
    history = _history(tmp_path)
    history.parent.mkdir(parents=True)
    history.write_text('{"used": ["ab"], "external": {}}')
    initialize_commitment_history_for_new_wallet(tmp_path)
    assert history.read_text() == '{"used": ["ab"], "external": {}}'


def test_initialization_failure_is_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_data_dir: Path) -> Path:
        raise PermissionError("read-only data directory")

    monkeypatch.setattr("jmwallet.commitment_history.create_commitment_history", fail)
    initialize_commitment_history_for_new_wallet(tmp_path)
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
    assert json.loads(_history(tmp_path).read_text()) == {"used": [], "external": {}}


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
