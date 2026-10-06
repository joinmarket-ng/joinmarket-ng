from __future__ import annotations

import json
from pathlib import Path

import pytest

from jmcore.wallet_metadata import (
    WalletIdentity,
    adopt_legacy_identity,
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
    # The first explicit registration becomes the selection; later ones never replace it.
    assert register_identity(source, WalletIdentity(fingerprint="aabbccdd", bip39="required"))
    assert selected_identity(source) == WalletIdentity(fingerprint="aabbccdd", bip39="required")
    assert not register_identity(source, WalletIdentity(fingerprint="55667788", bip39="none"))
    assert selected_identity(source) == WalletIdentity(fingerprint="aabbccdd", bip39="required")
    select_identity(source, "55667788")
    assert selected_identity(source) == WalletIdentity(fingerprint="55667788", bip39="none")
    select_identity(source, "aabbccdd")
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


LEGACY_FIELDS = {"creation_height": 7, "fidelity_bond_recovery": "complete", "unknown": [1]}


@pytest.mark.parametrize("bip39", ["none", "required"])
def test_matching_legacy_hint_is_adopted_preserving_fields(tmp_path: Path, bip39: str) -> None:
    source = tmp_path / "wallet.mnemonic"
    meta_path(source).write_text(json.dumps({"fingerprint": " AABBCCDD\n", **LEGACY_FIELDS}))
    identity = WalletIdentity(fingerprint="aabbccdd", bip39=bip39)  # type: ignore[arg-type]
    assert adopt_legacy_identity(source, identity)
    assert selected_identity(source) == identity
    assert registered_identities(source) == {"aabbccdd": identity}
    meta = load_mnemonic_meta(source)
    assert {key: meta[key] for key in LEGACY_FIELDS} == LEGACY_FIELDS
    assert meta["fingerprint"] == "aabbccdd"  # Older releases still read the same wallet.
    assert meta_path(source).stat().st_mode & 0o777 == 0o600
    assert not adopt_legacy_identity(source, identity)  # Already confirmed: idempotent no-op.


@pytest.mark.parametrize(
    "contents",
    [
        None,  # Absent metadata proves nothing about BIP39 use.
        "{}",
        '{"creation_height": 7}',
        '{"fingerprint": "11223344"}',  # A different wallet was used last.
        '{"fingerprint": "not hex"}',
        '{"fingerprint": 7}',
        '{"identity_version":1,"identities":{"11223344":{"bip39":"none"}}}',  # Explicit state wins.
        '{"identity_version":1,"fingerprint":"aabbccdd","identities":{}}',
    ],
)
def test_non_matching_or_explicit_metadata_adopts_nothing(
    tmp_path: Path, contents: str | None
) -> None:
    source = tmp_path / "wallet.mnemonic"
    if contents is not None:
        meta_path(source).write_text(contents)
    assert not adopt_legacy_identity(source, WalletIdentity(fingerprint="aabbccdd", bip39="none"))
    assert (meta_path(source).read_text() if meta_path(source).exists() else None) == contents


@pytest.mark.parametrize("contents", ["broken", "[]", '{"identity_version": 99}'])
def test_adoption_never_repairs_invalid_metadata(tmp_path: Path, contents: str) -> None:
    source = tmp_path / "wallet.mnemonic"
    meta_path(source).write_text(contents)
    with pytest.raises(ValueError):
        adopt_legacy_identity(source, WalletIdentity(fingerprint="aabbccdd", bip39="none"))
    assert meta_path(source).read_text() == contents


def test_older_cache_writer_cannot_override_selection(tmp_path: Path) -> None:
    source = tmp_path / "wallet.mnemonic"
    register_identity(source, WalletIdentity(fingerprint="aabbccdd", bip39="required"))
    select_identity(source, "aabbccdd")
    meta = load_mnemonic_meta(source)
    meta["fingerprint"] = "11223344"
    meta_path(source).write_text(json.dumps(meta))
    assert selected_identity(source).fingerprint == "aabbccdd"  # type: ignore[union-attr]
