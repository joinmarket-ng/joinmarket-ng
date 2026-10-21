"""Fresh-ledger authority is generation plus continuous exclusion, never absent files."""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from jmcore.crypto import mnemonic_to_seed
from jmcore.market_keys import MarketKeysClosedError, WalletMarketKeys
from jmcore.market_store import MarketStore
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.settings import validate_config_document
from jmswap.lightning_context import wallet_identity_of
from pydantic import SecretStr

from jmwallet import setup_market, setup_wallet
from jmwallet.setup_market import SetupMarketError, prepare_seller_ledger

PHRASE = " ".join(["all"] * 12)
PASSWORD = SecretStr("private-setup-market-password")


@pytest.fixture
def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    directory = tmp_path / "data"
    path = directory / "wallets/default.mnemonic"
    monkeypatch.setattr(setup_wallet, "generate_mnemonic_secure", Mock(return_value=PHRASE))
    publication = setup_wallet.publish_setup_wallet(path, PASSWORD, operation="generate")
    settings = validate_config_document(
        {
            "data_dir": str(directory),
            "network_config": {"network": "regtest"},
            "wallet": {"address_type": "p2tr"},
            "lightning": {"mode": "manual"},
        }
    )
    seed = mnemonic_to_seed(PHRASE, "")
    keys = WalletMarketKeys(seed, "regtest")
    wallet_id = keys.ledger_identity()
    keys.close()
    return {
        "directory": directory,
        "path": path,
        "publication": publication,
        "settings": settings,
        "wallet_id": wallet_id,
        "store": get_market_store_path(directory),
        "commitments": get_used_commitments_path(directory, create=False),
    }


def prepare(state: Any, **kwargs: Any) -> bool:
    options = {
        "publication": state["publication"],
        "activate": True,
        "history_excluded_since_generation": True,
        **kwargs,
    }
    return prepare_seller_ledger(
        state["settings"],
        SecretStr(PHRASE),
        SecretStr(""),
        data_dir=state["directory"],
        wallet_file=state["path"],
        **options,
    )


def diagnosis(state: Any) -> str:
    return MarketStore.diagnose_wallet(
        state["store"],
        wallet_id=state["wallet_id"],
        commitments_path=state["commitments"],
    )["state"]


def test_explicit_fresh_activation_uses_full_market_identity_and_exact_binding(
    fixture: Any,
) -> None:
    state = fixture
    original = state["path"].read_bytes()
    assert prepare(state) is True
    assert diagnosis(state) == "ready"
    assert state["path"].read_bytes() == original
    assert state["wallet_id"] != wallet_identity_of(mnemonic_to_seed(PHRASE, ""))
    with MarketStore(state["store"], wallet_id=state["wallet_id"], create=False) as store:
        store.check_commitments_path(state["commitments"])


@pytest.mark.parametrize("authority", ["activation", "history", "publication", "import", "resume"])
def test_absent_artifacts_do_not_replace_missing_authority(fixture: Any, authority: str) -> None:
    state = fixture
    options: dict[str, Any] = {}
    if authority == "activation":
        options["activate"] = False
    elif authority == "history":
        options["history_excluded_since_generation"] = False
    elif authority == "publication":
        options["publication"] = None
    elif authority == "resume":
        options["publication"] = setup_wallet.publish_setup_wallet(
            state["path"], PASSWORD, operation="generate", resume=True
        )
    else:
        path = state["directory"] / "imported.mnemonic"
        state["path"] = path
        options["publication"] = setup_wallet.publish_setup_wallet(
            path, PASSWORD, operation="import", mnemonic=SecretStr(PHRASE)
        )
    with pytest.raises(SetupMarketError):
        prepare(state, **options)
    assert not (state["directory"] / "market").exists()
    assert not (state["directory"] / "cmtdata").exists()


@pytest.mark.parametrize(
    "artifact",
    ["market", "cmtdata", "dangling-market", "dangling-history", "file", "sidecar", "old-history"],
)
def test_all_preexisting_artifacts_block_before_store_construction(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
) -> None:
    state = fixture
    if artifact in {"market", "cmtdata"}:
        (state["directory"] / artifact).mkdir()
    elif artifact.startswith("dangling"):
        name = "market" if artifact.endswith("market") else "cmtdata"
        (state["directory"] / name).symlink_to(state["directory"] / "missing-target")
    elif artifact == "file":
        (state["directory"] / "market").write_bytes(b"preserved")
    elif artifact == "sidecar":
        state["store"].parent.mkdir()
        state["store"].with_name("seller.sqlite-wal").write_bytes(b"preserved WAL evidence")
    else:
        state["commitments"].parent.mkdir()
        state["commitments"].write_bytes(b"preserved historical commitments")
    before = {str(p): p.read_bytes() for p in state["directory"].rglob("*") if p.is_file()}
    constructor = Mock(wraps=MarketStore)
    monkeypatch.setattr(setup_market, "MarketStore", constructor)
    with pytest.raises(SetupMarketError):
        prepare(state)
    constructor.assert_not_called()
    assert {str(p): p.read_bytes() for p in state["directory"].rglob("*") if p.is_file()} == before


def test_ready_existing_binding_is_checked_without_reactivation_or_history_claim(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    prepare(state)
    before = {str(p): p.read_bytes() for p in state["directory"].rglob("*") if p.is_file()}
    activate = Mock(side_effect=AssertionError("must not reactivate"))
    recover = Mock(side_effect=AssertionError("must not recover"))
    monkeypatch.setattr(MarketStore, "activate_wallet", activate)
    monkeypatch.setattr(MarketStore, "recover_wallet", recover)
    assert (
        prepare(state, publication=None, activate=False, history_excluded_since_generation=False)
        is False
    )
    assert {str(p): p.read_bytes() for p in state["directory"].rglob("*") if p.is_file()} == before
    activate.assert_not_called()
    recover.assert_not_called()


def test_changed_ciphertext_revokes_fresh_authority_without_repair(fixture: Any) -> None:
    state = fixture
    state["path"].write_bytes(b"changed wallet contents")
    with pytest.raises(SetupMarketError, match="changed"):
        prepare(state)
    assert state["path"].read_bytes() == b"changed wallet contents"
    assert not (state["directory"] / "market").exists()


def test_failed_activation_retains_recovery_required_state_and_never_recovers(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    original = state["path"].read_bytes()
    monkeypatch.setattr(
        MarketStore, "_read_commitments_history", Mock(side_effect=OSError("private-error-content"))
    )
    with pytest.raises(SetupMarketError) as error:
        prepare(state)
    assert "private-error-content" not in "".join(traceback.format_exception(error.value))
    assert diagnosis(state) == "recovery_required"
    assert state["path"].read_bytes() == original
    before = state["store"].read_bytes()
    recover = Mock(side_effect=AssertionError("no implicit recovery"))
    monkeypatch.setattr(MarketStore, "recover_wallet", recover)
    with pytest.raises(SetupMarketError):
        prepare(state, publication=None)
    assert state["store"].read_bytes() == before
    recover.assert_not_called()


def test_market_key_capabilities_are_closed_on_success_and_refusal(
    fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = fixture
    instances = []

    def keys(seed: bytes, network: str) -> WalletMarketKeys:
        result = WalletMarketKeys(seed, network)
        instances.append(result)
        return result

    monkeypatch.setattr(setup_market, "WalletMarketKeys", keys)
    with pytest.raises(SetupMarketError):
        prepare(state, activate=False)
    prepare(state)
    assert len(instances) == 2
    for instance in instances:
        with pytest.raises(MarketKeysClosedError):
            instance.ledger_identity()
