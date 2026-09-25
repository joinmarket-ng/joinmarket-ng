"""The market accepts Lightning only, and refuses another rail as an input.

Rejection must happen before the side effect it protects: no quote allocation,
no rate-limit slot, no queued payment request, and no payable URI. These are
input gates, not a decode-only tolerance for old documents.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import pubkey_to_p2wpkh_address
from jmcore.credential_market import (
    BondReference,
    MarketAuthorization,
    MarketError,
    MarketListing,
    MarketQuote,
    PaymentTerms,
    SignedDocument,
    bond_resource,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_keys import BoundMarketKeys, MarketKeyScope
from jmcore.market_store import MarketStore
from jmcore.models import NetworkType
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.settings import JoinMarketSettings, NetworkSettings, TorSettings
from jmwallet.backends.base import BondVerificationRequest
from jmwallet.wallet.service import WalletService
from nacl.public import PrivateKey
from pydantic import ValidationError
from test_market_payments import make_invoice

import taker.wallet_market as wallet_market
from taker.market_service import MarketService, QuoteRequest, accept_quote
from taker.wallet_market import WalletMarketSeller, WalletMarketSellerOptions

MNEMONIC = "all " * 11 + "all"
ONCHAIN_ADDRESS = pubkey_to_p2wpkh_address(b"\x02" + b"\x42" * 32, "regtest")
OWNER_KEY = CKey(b"\x02" * 32)
SELLER_KEY = CKey(b"\x22" * 32)
CERTIFICATE_KEY = CKey(b"\x44" * 32)


class _Backend:
    """Synthetic chain authority; never contacted by a rejected request."""

    def __init__(self) -> None:
        self.close = AsyncMock()
        self.get_block_height = AsyncMock(return_value=1)

    async def verify_bonds(self, bonds: list[BondVerificationRequest]) -> list[Any]:
        from types import SimpleNamespace

        return [
            SimpleNamespace(
                valid=True, txid=bond.txid, vout=bond.vout, confirmations=6, value=1_000_000
            )
            for bond in bonds
        ]


class _RefusingBackend:
    async def get_block_height(self) -> int:
        raise AssertionError("rejected quote must not reach the chain backend")

    async def verify_bonds(self, bonds: list[BondVerificationRequest]) -> list[Any]:
        raise AssertionError("rejected quote must not reach the chain backend")


class _Transport:
    def __init__(self, **kwargs: object) -> None:
        self.nick = "J5ABCDEFGHJKLMNP"

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _bond() -> BondReference:
    return BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid="11" * 32, vout=0),
        pubkey=bytes(OWNER_KEY.pub).hex(),
        locktime=int(time.time()) + 86_400,
    )


def _onchain_terms(address: str = ONCHAIN_ADDRESS) -> PaymentTerms:
    """Build non-Lightning terms without depending on the model's own rail set."""
    return PaymentTerms.model_construct(rail="onchain", request=address, amount_sats=1_000)


def _lightning_terms() -> PaymentTerms:
    invoice = make_invoice(amount_msat=1_000_000, date=int(time.time()) - 60, expiry=3_600)
    return PaymentTerms(rail="lightning", request=invoice, amount_sats=1_000)


def _quote_request_body(buyer_key: PrivateKey, rail: str, request_id: str) -> dict[str, Any]:
    return {
        "action": "quote",
        "request_id": request_id,
        "buyer_pubkey": bytes(buyer_key.public_key).hex(),
        "product": "bond",
        "certificate_pubkey": bytes(CERTIFICATE_KEY.pub).hex(),
        "rail": rail,
        "max_price_sats": 1_000,
    }


def _service(tmp_path: Path, bond: BondReference) -> tuple[MarketService, MarketStore]:
    store = MarketStore(tmp_path / "market.sqlite")
    store.add_inventory("bond", bond_resource(bond, 0), None)
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bytes(SELLER_KEY.pub).hex()),
        OWNER_KEY,
    )
    service = MarketService(
        store,
        authorization,
        SELLER_KEY,
        PrivateKey.generate(),
        _Backend(),
        products=["bond"],
        price_sats=1_000,
    )
    return service, store


def test_quote_request_schema_refuses_another_rail() -> None:
    with pytest.raises(ValidationError):
        QuoteRequest.model_validate(
            _quote_request_body(PrivateKey.generate(), "onchain", "01" * 16)
        )


def test_store_refuses_queueing_a_non_lightning_payment(tmp_path: Path) -> None:
    with MarketStore(tmp_path / "market.sqlite") as store:
        with pytest.raises(ValueError, match="invalid payment terms"):
            store.add_payment(_onchain_terms(), "onchain-1")
        assert (
            store._connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0  # noqa: SLF001
        )


def test_store_refuses_reserving_a_non_lightning_rail(tmp_path: Path) -> None:
    bond = _bond()
    service, store = _service(tmp_path, bond)
    try:
        store.add_payment(_lightning_terms(), "lightning-1")
        with pytest.raises(ValueError, match="rail must be lightning"):
            store.create_quote(
                service.authorization,
                SELLER_KEY,
                bytes(PrivateKey.generate().public_key).hex(),
                "bond",
                bytes(CERTIFICATE_KEY.pub).hex(),
                "onchain",  # type: ignore[arg-type]
                1_000,
                int(time.time()),
                1,
                ttl=300,
                request_id="02" * 16,
            )
        assert store.pending(int(time.time())) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_respond_refuses_another_rail_before_allocation(tmp_path: Path) -> None:
    bond = _bond()
    service, store = _service(tmp_path, bond)
    buyer_key = PrivateKey.generate()
    try:
        store.add_payment(_lightning_terms(), "lightning-1")
        with patch.object(store, "create_quote", side_effect=AssertionError) as allocation:
            response = await service.respond(
                "J5ABCDEFGHJKLMNP", _quote_request_body(buyer_key, "onchain", "01" * 16)
            )
            allocation.assert_not_called()
        assert response == {"error": "unavailable"}
        # Nothing was reserved and the per-request pacing slot was not consumed.
        assert store.pending(int(time.time())) == []
        assert service._next_quote == 0.0  # noqa: SLF001

        accepted = await service.respond(
            "J5ABCDEFGHJKLMNP", _quote_request_body(buyer_key, "lightning", "02" * 16)
        )
        quote = SignedDocument.model_validate(accepted["quote"]).verified(
            MarketQuote, bytes(SELLER_KEY.pub).hex()
        )
        assert quote.payment.rail == "lightning"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_accept_quote_refuses_another_rail_before_any_backend_use() -> None:
    bond = _bond()
    now = int(time.time())
    buyer_key = PrivateKey.generate()
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bytes(SELLER_KEY.pub).hex()),
        OWNER_KEY,
    )
    signed = sign_document(
        MarketQuote.model_construct(
            authorization=authorization,
            quote_id="0a" * 32,
            buyer_pubkey=bytes(buyer_key.public_key).hex(),
            product="bond",
            resource=bond_resource(bond, 0),
            certificate_pubkey=bytes(CERTIFICATE_KEY.pub).hex(),
            payment=_onchain_terms(),
            created_at=now,
            expires_at=now + 300,
        ),
        SELLER_KEY,
    )
    listing = MarketListing(
        network="regtest",
        period=0,
        seller_pubkey=bytes(SELLER_KEY.pub).hex(),
        encryption_pubkey=bytes(PrivateKey.generate().public_key).hex(),
        products=["bond"],
        price_sats=1_000,
        expires_at=now + 60,
    )
    request = QuoteRequest.model_validate(_quote_request_body(buyer_key, "lightning", "04" * 16))

    with pytest.raises(MarketError, match="Invalid signed market document"):
        await accept_quote(signed, listing, request, _RefusingBackend(), now)


def _settings(data_dir: Path) -> JoinMarketSettings:
    return JoinMarketSettings(
        data_dir=data_dir,
        network_config=NetworkSettings(network=NetworkType.REGTEST, directory_servers=[]),
        tor=TorSettings(connection_timeout=0.2),
    )


def _activate_store(data_dir: Path, wallet: WalletService) -> None:
    commitments = get_used_commitments_path(data_dir)
    commitments.parent.mkdir(parents=True, exist_ok=True)
    commitments.write_text(json.dumps({"external_v1": {}, "used": []}), encoding="ascii")
    with MarketStore(get_market_store_path(data_dir), wallet_id=wallet.market_wallet_id) as store:
        store.activate_wallet(commitments, history_confirmed=True)


def _bound_authorization(
    wallet: WalletService, bond: BondReference
) -> tuple[BoundMarketKeys, SignedDocument]:
    bound = wallet.market_keys.bind(
        MarketKeyScope(
            network="regtest",
            chain_hash="22" * 32,
            role="seller",
            period=0,
            bond=bond.outpoint,
        )
    )
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bound.signing_public_key().hex()),
        OWNER_KEY,
    )
    return bound, authorization


@pytest.mark.asyncio
async def test_wallet_seller_refuses_queueing_a_non_lightning_payment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _Backend()
    wallet = WalletService(MNEMONIC, backend, network="regtest", data_dir=tmp_path)
    _activate_store(tmp_path, wallet)
    bond = _bond()
    bound, authorization = _bound_authorization(wallet, bond)
    wallet.create_market_authorization = AsyncMock(return_value=(bound, authorization))  # type: ignore[method-assign]
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    seller = WalletMarketSeller(
        wallet,
        _settings(tmp_path),
        WalletMarketSellerOptions(bond=bond.outpoint, products=["bond"], price_sats=1_000),
    )
    try:
        await seller.start()
        store = seller._store  # noqa: SLF001
        assert store is not None

        with patch.object(store, "add_payment") as queued:
            with pytest.raises(ValueError, match="Unsupported payment rail"):
                seller.add_payment(_onchain_terms())
            queued.assert_not_called()
    finally:
        await seller.stop()
        await wallet.close()
