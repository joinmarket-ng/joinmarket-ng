from __future__ import annotations

import json
from pathlib import Path

import pytest

from jmcore.wallet_metadata import (
    WalletIdentity,
    load_mnemonic_meta,
    meta_path,
    register_identity,
    registered_identities,
    select_identity,
    selected_identity,
)


def test_registration_preserves_legacy_recovery_and_selection(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    legacy = {
        "fingerprint": "11223344",
        "creation_height": 7,
        "fidelity_bond_recovery": "pending",
        "fidelity_bond_recovery.11223344": "started",
    }
    meta_path(source).write_text(json.dumps(legacy))
    register_identity(source, WalletIdentity(fingerprint="aabbccdd", bip39="required"))
    with pytest.raises(ValueError, match="No wallet identity selected"):
        selected_identity(source)
    select_identity(source, "aabbccdd")
    register_identity(source, WalletIdentity(fingerprint="55667788", bip39="none"))
    assert selected_identity(source) == WalletIdentity(fingerprint="aabbccdd", bip39="required")
    meta = load_mnemonic_meta(source)
    for key, value in legacy.items():
        if key != "fingerprint":
            assert meta[key] == value
    assert set(registered_identities(source)) == {"aabbccdd", "55667788"}
    assert meta_path(source).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "contents",
    [
        "broken",
        "[]",
        '{"identity_version": 99}',
        '{"identity_version": null}',
        '{"identities":{"aabbccdd":{"bip39":"required"}}}',
        '{"identity_version":null,"identities":{"aabbccdd":{"bip39":"required"}}}',
    ],
)
def test_invalid_metadata_is_not_repaired(tmp_path: Path, contents: str) -> None:
    source = tmp_path / "wallet.mnemonic"
    meta_path(source).write_text(contents)
    with pytest.raises(ValueError):
        register_identity(source, WalletIdentity(fingerprint="aabbccdd", bip39="none"))
    with pytest.raises(ValueError):
        selected_identity(source)
    assert meta_path(source).read_text() == contents


def test_legacy_fingerprint_does_not_prove_passphrase_requirement(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    assert selected_identity(source) is None
    meta_path(source).write_text('{"fingerprint":"aabbccdd"}')
    assert selected_identity(source) is None
    assert registered_identities(source) == {}


def test_partial_collection_does_not_invent_default(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    meta_path(source).write_text(
        json.dumps(
            {
                "identity_version": 1,
                "identities": {"aabbccdd": {"bip39": "required"}, "11223344": {"bip39": "invalid"}},
            }
        )
    )
    assert set(registered_identities(source)) == {"aabbccdd"}
    with pytest.raises(ValueError, match="No wallet identity selected"):
        selected_identity(source)


def test_older_cache_writer_cannot_override_selection(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    register_identity(source, WalletIdentity(fingerprint="aabbccdd", bip39="required"))
    select_identity(source, "aabbccdd")
    meta = load_mnemonic_meta(source)
    meta["fingerprint"] = "11223344"
    meta_path(source).write_text(json.dumps(meta))
    assert selected_identity(source).fingerprint == "aabbccdd"  # type: ignore[union-attr]
