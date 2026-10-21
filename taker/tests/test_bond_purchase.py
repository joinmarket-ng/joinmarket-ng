"""Automatic fidelity bond leases for a maker without a usable bond."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.credential_market import (
    MARKET_LISTING_LIFETIME_SECONDS,
    BondCredential,
    BondReference,
    MarketListing,
    SignedDocument,
    canonical,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.models import NetworkType
from jmwallet.wallet.bond_registry import load_registry

from taker import bond_purchase
from taker.bond_purchase import (
    BondImportError,
    BondPurchaser,
    LeasedBond,
    import_bond_credential,
    max_lease_price_sats,
)

FINGERPRINT = "deadbeef"
CERTIFICATE = CKey(bytes([0x55]) * 32)
VALUE = 10_000_000


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


def _bond(seed: int) -> BondReference:
    return BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=f"{seed:02x}" * 32, vout=1),
        pubkey=bytes(_key(seed).pub).hex(),
        locktime=int(time.time()) + 365 * 86_400,
    )


def _credential(seed: int, *, expiry: int = 1, certificate: CKey = CERTIFICATE) -> BondCredential:
    # Only the fields the registry records; signatures are verified upstream.
    return BondCredential.model_construct(
        bond=_bond(seed),
        cert_pubkey=bytes(certificate.pub).hex(),
        cert_expiry=expiry,
        cert_signature=f"{expiry:02x}" * 70,
    )


def _registry_entry(tmp_path: Path, seed: int) -> Any:
    registry = load_registry(tmp_path, FINGERPRINT, allow_legacy_fallback=False, fail_closed=True)
    return next(b for b in registry.bonds if b.pubkey == _bond(seed).pubkey)


def test_a_lease_for_a_later_period_renews_only_an_external_entry(tmp_path: Path) -> None:
    first = _credential(0x61, expiry=1)
    assert import_bond_credential(first, CERTIFICATE, tmp_path, FINGERPRINT)
    assert not import_bond_credential(first, CERTIFICATE, tmp_path, FINGERPRINT)

    # Another certificate for the same period would be a second lease: refused.
    other = _key(0x56)
    with pytest.raises(BondImportError, match="conflicts"):
        import_bond_credential(
            _credential(0x61, expiry=1, certificate=other), other, tmp_path, FINGERPRINT
        )
    renewed = _credential(0x61, expiry=2, certificate=other)
    assert import_bond_credential(renewed, other, tmp_path, FINGERPRINT)
    entry = _registry_entry(tmp_path, 0x61)
    assert (entry.cert_pubkey, entry.cert_privkey, entry.cert_expiry) == (
        renewed.cert_pubkey,
        other.secret_bytes.hex(),
        2,
    )
    assert entry.index == -1
    # Never back to an earlier period.
    with pytest.raises(BondImportError, match="conflicts"):
        import_bond_credential(first, CERTIFICATE, tmp_path, FINGERPRINT)
    with pytest.raises(BondImportError, match="does not match"):
        import_bond_credential(_credential(0x62), other, tmp_path, FINGERPRINT)


def test_the_certificate_of_an_owned_bond_is_never_replaced(tmp_path: Path) -> None:
    import_bond_credential(_credential(0x61, expiry=1), CERTIFICATE, tmp_path, FINGERPRINT)
    registry = load_registry(tmp_path, FINGERPRINT, allow_legacy_fallback=False, fail_closed=True)
    registry.bonds[0].index = 0
    registry.bonds[0].path = "m/84'/1'/0'/2/0:1"
    from jmwallet.wallet.bond_registry import save_registry

    save_registry(registry, tmp_path, FINGERPRINT)
    with pytest.raises(BondImportError, match="conflicts"):
        import_bond_credential(_credential(0x61, expiry=2), CERTIFICATE, tmp_path, FINGERPRINT)
    assert _registry_entry(tmp_path, 0x61).cert_expiry == 1


def test_the_rate_cap_covers_the_rest_of_the_period() -> None:
    # 2% a year on 0.1 BTC, for a whole period, then for its last block.
    assert max_lease_price_sats(VALUE, 0.02, 1) == 7671
    assert max_lease_price_sats(VALUE, 0.02, 2016) == 3


def _listing(seed: int, price: int, products: tuple[str, ...] = ("bond",)) -> bytes:
    key = _key(seed)
    listing = MarketListing(
        network="regtest",
        period=0,
        seller_pubkey=bytes(key.pub).hex(),
        encryption_pubkey=f"{seed:02x}" * 32,
        products=list(products),  # type: ignore[arg-type]
        price_sats=price,
        expires_at=int(time.time()) + MARKET_LISTING_LIFETIME_SECONDS // 2,
    )
    return canonical(sign_document(listing, key))


def _quote_document(seed: int) -> dict[str, Any]:
    signature = _key(0x77).sign(b"\x01" * 32).hex()
    return SignedDocument(body={"quote_id": f"{seed:02x}" * 32}, signature=signature).model_dump(
        mode="json"
    )


class _Transport:
    """Market relay stand-in: per-seller quotes and a delivery switch."""

    def __init__(self, listings: list[tuple[str, bytes]], quotes: dict[str, dict[str, Any]]):
        self.listings = listings
        self.quotes = quotes
        self.deliver = True
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def discover(self, timeout: float) -> list[tuple[str, bytes]]:
        return self.listings

    async def request(
        self, nick: str, _key: bytes, payload: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        self.requests.append((nick, payload))
        if payload.get("action") == "delivery":
            return {"delivery": f"sealed-{payload['quote_id']}"} if self.deliver else {}
        return self.quotes.get(nick, {"error": "unavailable"})


class _Payer:
    def __init__(self, result: str = "paid") -> None:
        self.result = result
        self.paid: list[tuple[str, int, int]] = []

    def routing_budget(self, cap_sats: int) -> int:
        return cap_sats // 5

    async def pay(
        self, bolt11: str, *, amount_sats: int, fee_limit_sats: int, expires_at: int
    ) -> Any:
        self.paid.append((bolt11, amount_sats, fee_limit_sats))
        return self.result


def _settings(tmp_path: Path) -> Any:
    return SimpleNamespace(
        get_data_dir=lambda: tmp_path,
        network_config=SimpleNamespace(bitcoin_network=None, network=NetworkType.REGTEST),
    )


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fake seller-side verification: each quote id names the seller's bond seed."""
    state = SimpleNamespace(height=1, imported=[], import_error=None)

    def seed_of(document: Any) -> int:
        return int(document[:2], 16)

    async def accept_quote(signed: SignedDocument, listing: Any, request: Any, *_: Any) -> Any:
        assert request.product == "bond" and request.certificate_pubkey is not None
        quote_id = signed.body["quote_id"]
        return SimpleNamespace(
            quote_id=quote_id,
            authorization=quote_id,
            payment=SimpleNamespace(amount_sats=listing.price_sats, request=f"lnbcrt{quote_id}"),
            expires_at=int(time.time()) + 300,
        )

    def open_delivery(encrypted: str, _buyer: Any, quote: SignedDocument) -> Any:
        seed = seed_of(quote.body["quote_id"])
        return SimpleNamespace(
            verify=lambda: _credential(seed), authorization=quote.body["quote_id"]
        )

    def import_credential(credential: Any, certificate: CKey, *_: Any) -> bool:
        if state.import_error is not None:
            raise BondImportError(state.import_error)
        state.imported.append((credential, certificate))
        return True

    backend = AsyncMock()
    backend.get_block_height = AsyncMock(side_effect=lambda: state.height)
    backend.get_utxo = AsyncMock(
        side_effect=lambda txid, vout: SimpleNamespace(value=VALUE, confirmations=50)
    )
    monkeypatch.setattr(bond_purchase, "accept_quote", accept_quote)
    monkeypatch.setattr(bond_purchase, "open_delivery", open_delivery)
    monkeypatch.setattr(bond_purchase, "verify_market_bond", AsyncMock())
    monkeypatch.setattr(
        bond_purchase,
        "verify_authorization",
        lambda auth: SimpleNamespace(bond=_bond(seed_of(auth)), period=0),
    )
    monkeypatch.setattr(
        bond_purchase,
        "MarketQuote",
        SimpleNamespace(
            model_validate=lambda body: SimpleNamespace(
                quote_id=body["quote_id"], authorization=body["quote_id"]
            )
        ),
    )
    monkeypatch.setattr(bond_purchase, "import_bond_credential", import_credential)
    monkeypatch.setattr(bond_purchase, "new_certificate_key", lambda: CERTIFICATE)
    monkeypatch.setattr(bond_purchase, "POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(bond_purchase, "NODE_DELIVERY_SECONDS", 0.0)
    state.backend = backend
    return state


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    directory = tmp_path / "market" / "bond-purchases"
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def _purchaser(tmp_path: Path, transport: _Transport, payer: _Payer) -> BondPurchaser:
    return BondPurchaser(
        _settings(tmp_path),
        payer,
        wallet_fingerprint=FINGERPRINT,
        transport_factory=lambda: transport,
    )


def _market(*sellers: tuple[int, int]) -> _Transport:
    return _Transport(
        [(f"J5s{seed:02x}", _listing(seed, price)) for seed, price in sellers],
        {f"J5s{seed:02x}": {"quote": _quote_document(seed)} for seed, _ in sellers},
    )


@pytest.mark.asyncio
async def test_leases_the_largest_bond_within_both_caps(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    # Cap 10_000 with a 2_000 routing budget leaves 8_000; 2% a year on the
    # 0.1 BTC bonds allows 7_671 for the whole period.
    transport = _market((0xA1, 9_000), (0xB2, 7_900), (0xC3, 5_000), (0xE5, 4_000))
    transport.listings.append(("J5sd4", _listing(0xD4, 6_000, ("podle",))))
    payer = _Payer()

    leased = await _purchaser(tmp_path, transport, payer).lease(
        market.backend, cap_sats=10_000, max_rate=0.02
    )

    assert isinstance(leased, LeasedBond)
    assert leased.credential.bond == _bond(0xC3)
    assert leased.certificate.secret_bytes == CERTIFICATE.secret_bytes
    assert (leased.value, leased.confirmations) == (VALUE, 50)
    quoted = [nick for nick, payload in transport.requests if payload.get("action") == "quote"]
    assert quoted == ["J5sb2", "J5sc3"]
    assert payer.paid == [(f"lnbcrt{'c3' * 32}", 5_000, 5_000)]
    assert [credential.bond for credential, _ in market.imported] == [_bond(0xC3)]
    records = sorted(_records(tmp_path), key=lambda record: record["state"])
    assert [record["state"] for record in records] == ["done", "failed"]
    done = records[0]
    assert done["bond"] == f"{'c3' * 32}:1" and done["cert_expiry"] == 1
    for record in records:
        assert not {"buyer_secret", "certificate_secret", "quote", "listing"} & record.keys()


@pytest.mark.asyncio
async def test_an_undelivered_lease_is_collected_later_and_no_other_seller_is_paid(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000), (0xC3, 5_000))
    transport.deliver = False
    payer = _Payer()
    purchaser = _purchaser(tmp_path, transport, payer)

    assert await purchaser.lease(market.backend, cap_sats=10_000, max_rate=0.02) is None
    (record,) = _records(tmp_path)
    assert record["state"] == "paying"
    assert {"buyer_secret", "certificate_secret"} <= record.keys()
    # Still undelivered: the other seller is not even asked.
    transport.requests.clear()
    assert await purchaser.lease(market.backend, cap_sats=10_000, max_rate=0.02) is None
    assert all(payload.get("action") == "delivery" for _, payload in transport.requests)

    transport.deliver = True
    leased = await purchaser.lease(market.backend, cap_sats=10_000, max_rate=0.02)
    assert leased is not None and leased.credential.bond == _bond(0xB2)
    assert len(payer.paid) == 1
    assert [record["state"] for record in _records(tmp_path)] == ["done"]


@pytest.mark.asyncio
async def test_a_lease_for_an_earlier_period_is_not_collected(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000))
    transport.deliver = False
    purchaser = _purchaser(tmp_path, transport, _Payer())
    assert await purchaser.lease(market.backend, cap_sats=10_000, max_rate=0.02) is None

    market.height = 2017
    transport.listings, transport.deliver = [], True
    assert await purchaser.lease(market.backend, cap_sats=10_000, max_rate=0.02) is None
    (record,) = _records(tmp_path)
    assert record["state"] == "expired" and "certificate_secret" not in record
    assert market.imported == []


@pytest.mark.asyncio
async def test_a_failed_payment_moves_on_and_an_unusable_lease_stops(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000), (0xC3, 5_000))
    assert (
        await _purchaser(tmp_path, transport, _Payer("failed")).lease(
            market.backend, cap_sats=10_000, max_rate=0.02
        )
        is None
    )
    assert [record["state"] for record in _records(tmp_path)] == ["failed", "failed"]

    market.import_error = "existing bond registry entry conflicts with delivered credential"
    payer = _Payer()
    assert (
        await _purchaser(tmp_path, transport, payer).lease(
            market.backend, cap_sats=10_000, max_rate=0.02
        )
        is None
    )
    # Paid for, so its keys and the sealed delivery are kept; nobody else is paid.
    assert len(payer.paid) == 1
    (kept,) = [record for record in _records(tmp_path) if record["state"] == "unimported"]
    assert kept["delivery"] == f"sealed-{'b2' * 32}"
    assert {"buyer_secret", "certificate_secret", "quote"} <= kept.keys()
