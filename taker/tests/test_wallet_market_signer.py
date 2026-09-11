"""Wallet-bound seller capability coverage for the native market store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from bitcointx.core.key import CKey
from jmcore.credential_market import (
    BondCredential,
    BondReference,
    CredentialPackage,
    MarketAuthorization,
    MarketModel,
    MarketQuote,
    PaymentTerms,
    SignedDocument,
    bond_resource,
    sign_document,
)
from jmcore.crypto import bitcoin_message_hash_bytes, get_cert_msg
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_keys import (
    BoundMarketKeys,
    MarketKeyError,
    MarketKeysClosedError,
    MarketKeyScope,
    WalletMarketKeys,
)
from nacl.public import PrivateKey

from taker.market_store import MarketStore, MarketStoreConflictError, MarketStoreUnavailableError

SEED = bytes.fromhex(
    "c76c4ac4f4e4a00d6b274d5c39c700bb4a7ddc04fbc6f78e85ca75007b5b495f74a9"
    "043eeb77bdd53aa6fc3a0e31462270316fa04b8c19114c8798706cd02ac8"
)


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


def _bond(txid_byte: int = 0xAA) -> BondReference:
    return BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=f"{txid_byte:02x}" * 32, vout=1),
        pubkey=bytes(_key(0x11).pub).hex(),
        locktime=600_000_000,
    )


def _scope(authority_bond: BondReference, **changes: object) -> MarketKeyScope:
    return MarketKeyScope.model_validate(
        {
            "network": "regtest",
            "chain_hash": "11" * 32,
            "role": "seller",
            "period": 0,
            "bond": authority_bond.outpoint.model_dump(mode="json"),
        }
        | changes
    )


def _authorization(bound: BoundMarketKeys, bond: BondReference) -> SignedDocument:
    return sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bound.signing_public_key().hex()),
        _key(0x11),
    )


def _payment() -> PaymentTerms:
    return PaymentTerms(
        rail="onchain",
        request="regtest-wallet-bound-payment",
        amount_sats=1_000,
        min_confirmations=1,
    )


def _bond_credential(bond: BondReference, certificate: CKey) -> dict[str, object]:
    signature = _key(0x11).sign(
        bitcoin_message_hash_bytes(get_cert_msg(bytes(certificate.pub), 1)),
        _ecdsa_sig_grind_low_r=False,
    )
    return BondCredential(
        bond=bond,
        cert_pubkey=bytes(certificate.pub).hex(),
        cert_expiry=1,
        cert_signature=signature.hex(),
    ).model_dump(mode="json")


def _activate(tmp_path: Path, wallet_id: str) -> MarketStore:
    tmp_path.mkdir(parents=True, exist_ok=True)
    commitments = tmp_path / "commitments.json"
    commitments.write_text(json.dumps({"external_v1": {}, "used": []}), encoding="ascii")
    store = MarketStore(tmp_path / "market.sqlite", wallet_id=wallet_id)
    store.activate_wallet(commitments, history_confirmed=True)
    return store


def _quote(
    store: MarketStore,
    authorization: SignedDocument,
    seller: BoundMarketKeys,
    *,
    request_id: str = "01" * 16,
    height: int = 1,
) -> SignedDocument:
    return store.create_quote(
        authorization,
        seller,
        bytes(PrivateKey(bytes([0x33]) * 32).public_key).hex(),
        "bond",
        bytes(_key(0x44).pub).hex(),
        "onchain",
        10_000,
        100,
        height,
        request_id=request_id,
    )


def test_bound_seller_can_issue_and_idempotently_replay_package(tmp_path: Path) -> None:
    bond = _bond()
    parent = WalletMarketKeys(SEED, "regtest")
    bound = parent.bind(_scope(bond))
    store = _activate(tmp_path, bound.wallet_id)
    certificate = _key(0x44)
    authority = _authorization(bound, bond)
    try:
        store.add_inventory("bond", bond_resource(bond, 0), None)
        store.add_payment(_payment(), "payment-1")
        quote = _quote(store, authority, bound)
        verified_quote = quote.verified(MarketQuote, bound.signing_public_key().hex())
        assert verified_quote == MarketQuote.model_validate(quote.body)
        assert _quote(store, authority, bound, height=2017) == quote

        quote_id = str(quote.body["quote_id"])
        store.attach_credential(quote_id, _bond_credential(bond, certificate))
        package = store.finalize(quote_id, bound, "settlement-1", 101, 1)
        assert package.verify() == BondCredential.model_validate(
            _bond_credential(bond, certificate)
        )
        assert store.finalize(quote_id, bound, "settlement-1", 10_000, 2017) == package
        assert store.get_package(quote_id) == package
    finally:
        store.close()


def test_bound_seller_rejects_wrong_wallet_and_nonready_stores(tmp_path: Path) -> None:
    bond = _bond()
    matching_parent = WalletMarketKeys(SEED, "regtest")
    matching = matching_parent.bind(_scope(bond))
    other_parent = WalletMarketKeys(b"\x01" * 64, "regtest")
    other = other_parent.bind(_scope(bond))
    authority = _authorization(other, bond)
    legacy = MarketStore(tmp_path / "legacy.sqlite")
    ready = _activate(tmp_path / "ready", matching.wallet_id)
    disabled = MarketStore(ready.path, wallet_id=other.wallet_id)
    try:
        with pytest.raises(MarketStoreUnavailableError, match="ready wallet ledger"):
            _quote(legacy, authority, other)
        with pytest.raises(MarketStoreConflictError, match="does not match"):
            _quote(ready, authority, other)
        with pytest.raises(MarketStoreUnavailableError, match="not ready"):
            _quote(disabled, authority, other)
    finally:
        legacy.close()
        disabled.close()
        ready.close()


@pytest.mark.parametrize(
    ("scope_changes", "network"),
    (
        ({"role": "buyer"}, "regtest"),
        ({"network": "signet"}, "signet"),
        ({"period": 1}, "regtest"),
        ({"bond": {"txid": "bb" * 32, "vout": 1}}, "regtest"),
        ({"trade_id": "cc" * 32}, "regtest"),
    ),
)
def test_bound_seller_quote_rejects_non_seller_authority_scopes(
    tmp_path: Path, scope_changes: dict[str, object], network: str
) -> None:
    bond = _bond()
    parent = WalletMarketKeys(SEED, network)
    bound = parent.bind(_scope(bond, **scope_changes))
    store = _activate(tmp_path, bound.wallet_id)
    try:
        with pytest.raises(MarketKeyError):
            _quote(store, _authorization(bound, bond), bound)
    finally:
        store.close()


def test_revoked_bound_seller_rolls_back_before_and_during_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bond = _bond()
    scope = _scope(bond)
    parent = WalletMarketKeys(SEED, "regtest")
    bound = parent.bind(scope)
    store = _activate(tmp_path, bound.wallet_id)
    certificate = _key(0x44)
    try:
        store.add_inventory("bond", bond_resource(bond, 0), None)
        store.add_payment(_payment(), "payment-1")
        authority = _authorization(bound, bond)
        parent.close()
        with pytest.raises(MarketKeysClosedError):
            _quote(store, authority, bound)
        assert store.pending(100) == []

        restored_parent = WalletMarketKeys(SEED, "regtest")
        restored = restored_parent.bind(scope)
        quote = _quote(store, authority, restored)
        quote_id = str(quote.body["quote_id"])
        store.attach_credential(quote_id, _bond_credential(bond, certificate))
        original_sign_document = BoundMarketKeys.sign_document

        def close_after_first_signature(self: BoundMarketKeys, body: MarketModel) -> SignedDocument:
            signed = original_sign_document(self, body)
            restored_parent.close()
            return signed

        monkeypatch.setattr(BoundMarketKeys, "sign_document", close_after_first_signature)
        with pytest.raises(MarketKeysClosedError):
            store.finalize(quote_id, restored, "settlement-1", 101, 1)
        assert store.get_package(quote_id) is None
        with sqlite3.connect(store.path) as connection:
            state, settlement_ref, package = connection.execute(
                "SELECT state, settlement_ref, package FROM quotes WHERE quote_id = ?", (quote_id,)
            ).fetchone()
        assert (state, settlement_ref, package) == ("live", None, None)

        recovered = WalletMarketKeys(SEED, "regtest").bind(scope)
        package = store.finalize(quote_id, recovered, "settlement-1", 101, 1)
        assert isinstance(package, CredentialPackage)
    finally:
        store.close()
