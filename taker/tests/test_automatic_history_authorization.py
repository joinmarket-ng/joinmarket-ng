"""Automatic initialization cannot authorize an unrelated recovered mnemonic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config, make_utxo
from jmcore.paths import GENERATED_WALLETS_ONLY
from jmcore.podle import generate_podle
from jmwallet.commitment_history import (
    initialize_commitment_history_for_new_wallet,
    is_first_wallet_in_data_dir,
)
from jmwallet.wallet.service import WalletService

from taker.podle_manager import CommitmentHistoryError, PoDLEManager
from taker.taker import Taker

_NEW_MNEMONIC = "legal winner thank year wave sausage worth useful legal winner thank yellow"
_RECOVERED_MNEMONIC = "abandon " * 11 + "about"


def _initialize(data_dir: Path, mnemonic: str, name: str = "new.mnemonic") -> Path:
    wallet = data_dir / "wallets" / name
    first = is_first_wallet_in_data_dir(data_dir, wallet)
    wallet.parent.mkdir(parents=True, exist_ok=True)
    wallet.write_bytes(b"newly generated wallet")
    initialize_commitment_history_for_new_wallet(
        data_dir,
        mnemonic=mnemonic,
        wallet_file=wallet,
        first_wallet=first,
    )
    return data_dir / "cmtdata" / "commitments.json"


def test_previously_used_recovered_wallet_cannot_use_another_wallets_empty_history(
    tmp_path: Path,
) -> None:
    path = _initialize(tmp_path, _NEW_MNEMONIC)
    utxo = make_utxo(value=25_000_000, confirmations=100)
    prior = generate_podle(b"\x01" * 32, f"{utxo.txid}:{utxo.vout}", 0)
    before = path.read_bytes()

    with pytest.raises(CommitmentHistoryError, match="does not authorize this wallet"):
        PoDLEManager(tmp_path, mnemonic=_RECOVERED_MNEMONIC)
    with pytest.raises(CommitmentHistoryError, match="does not authorize this wallet"):
        PoDLEManager(tmp_path)

    # The authorized new wallet remains usable and its later save preserves the
    # restriction, not just the initial empty file.
    manager = PoDLEManager(tmp_path, mnemonic=_NEW_MNEMONIC)
    assert prior.commitment.hex() not in manager.used_commitments
    assert path.read_bytes() == before
    manager.used_commitments.add("ab" * 32)
    manager._save()
    with pytest.raises(CommitmentHistoryError, match="does not authorize this wallet"):
        PoDLEManager(tmp_path, mnemonic=_RECOVERED_MNEMONIC)
    assert json.loads(path.read_text())[GENERATED_WALLETS_ONLY] is True


@pytest.mark.parametrize("passphrase", ["", "a distinct generated identity"])
def test_taker_accepts_passphrase_identities_of_a_freshly_generated_mnemonic(
    tmp_path: Path,
    passphrase: str,
) -> None:
    _initialize(tmp_path, _NEW_MNEMONIC)
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    wallet = WalletService(
        _NEW_MNEMONIC,
        backend,
        network="regtest",
        data_dir=tmp_path,
        passphrase=passphrase,
    )
    config = make_taker_config(data_dir=tmp_path, mnemonic=_NEW_MNEMONIC, passphrase=passphrase)
    taker = Taker(wallet, backend, config)
    assert taker.podle_manager.used_commitments == set()


def test_multiple_generated_wallets_do_not_rewrite_shared_commitments(tmp_path: Path) -> None:
    path = _initialize(tmp_path, _NEW_MNEMONIC)
    manager = PoDLEManager(tmp_path, mnemonic=_NEW_MNEMONIC)
    manager.used_commitments.add("ab" * 32)
    manager._save()
    before = path.read_bytes()
    _initialize(tmp_path, _RECOVERED_MNEMONIC, "second-generated.mnemonic")
    assert path.read_bytes() == before
    assert PoDLEManager(tmp_path, mnemonic=_RECOVERED_MNEMONIC).used_commitments == {"ab" * 32}


def test_reference_style_json_rewrite_does_not_authorize_an_unrelated_wallet(
    tmp_path: Path,
) -> None:
    path = _initialize(tmp_path, _NEW_MNEMONIC)
    # The reference serializer retains only used/external. Durable generation
    # records must continue restricting this directory after such a rewrite.
    path.write_text(json.dumps({"used": ["ab"], "external": {}}))
    before = path.read_bytes()
    assert PoDLEManager(tmp_path, mnemonic=_NEW_MNEMONIC).used_commitments == {"ab"}
    with pytest.raises(CommitmentHistoryError, match="does not authorize this wallet"):
        PoDLEManager(tmp_path, mnemonic=_RECOVERED_MNEMONIC)
    _initialize(tmp_path, _RECOVERED_MNEMONIC, "another-generated.mnemonic")
    assert path.read_bytes() == before
    assert PoDLEManager(tmp_path, mnemonic=_RECOVERED_MNEMONIC).used_commitments == {"ab"}


@pytest.mark.parametrize(
    "state", ["missing", "missing-directory", "partial", "symlink", "directory-symlink"]
)
def test_missing_or_untrusted_generation_proof_fails_closed(tmp_path: Path, state: str) -> None:
    path = _initialize(tmp_path, _NEW_MNEMONIC)
    proof = next((path.parent / "generated-wallets").iterdir())
    original = path.read_bytes()
    if state in {"missing", "missing-directory"}:
        proof.unlink()
        if state == "missing-directory":
            proof.parent.rmdir()
    elif state == "partial":
        proof.write_bytes(b"partial")
    elif state == "symlink":
        proof.unlink()
        proof.symlink_to(tmp_path / "missing-target")
    else:
        target = tmp_path / "other-authorizations"
        proof.parent.rename(target)
        proof.parent.symlink_to(target, target_is_directory=True)
    with pytest.raises(CommitmentHistoryError, match="does not authorize this wallet"):
        PoDLEManager(tmp_path, mnemonic=_NEW_MNEMONIC)
    assert path.read_bytes() == original


@pytest.mark.parametrize("restriction", [False, "true", None, 1, 2])
def test_unknown_restriction_is_not_treated_as_legacy_history(
    tmp_path: Path,
    restriction: object,
) -> None:
    path = _initialize(tmp_path, _NEW_MNEMONIC)
    data = json.loads(path.read_text())
    data[GENERATED_WALLETS_ONLY] = restriction
    path.write_text(json.dumps(data))
    with pytest.raises(CommitmentHistoryError, match="Could not load"):
        PoDLEManager(tmp_path, mnemonic=_NEW_MNEMONIC)


def test_valid_previous_release_history_needs_no_generation_proof(tmp_path: Path) -> None:
    path = tmp_path / "cmtdata" / "commitments.json"
    path.parent.mkdir()
    original = b'{"used": ["ab"], "external": {}, "unrecognized-extension": [1, 2]}'
    path.write_bytes(original)
    assert PoDLEManager(tmp_path, mnemonic=_RECOVERED_MNEMONIC).used_commitments == {"ab"}
    assert path.read_bytes() == original
    assert not (path.parent / "generated-wallets").exists()
