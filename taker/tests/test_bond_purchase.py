"""Automatic fidelity bond leases for a maker without a usable bond."""

from __future__ import annotations

import asyncio
import errno
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

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
from taker.podle_purchase import PaymentIdentity, PurchaseRecord

FINGERPRINT = "deadbeef"
CERTIFICATE = CKey(bytes([0x55]) * 32)
VALUE = 10_000_000
LOCKTIME = int(time.time()) + 365 * 86_400


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


def _bond(seed: int) -> BondReference:
    return BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=f"{seed:02x}" * 32, vout=1),
        pubkey=bytes(_key(seed).pub).hex(),
        locktime=LOCKTIME,
    )


def _credential(seed: int, *, expiry: int = 1, certificate: CKey = CERTIFICATE) -> BondCredential:
    # Signatures are verified upstream; only the formats must hold.
    return BondCredential.model_construct(
        bond=_bond(seed),
        cert_pubkey=bytes(certificate.pub).hex(),
        cert_expiry=expiry,
        cert_signature=f"{expiry:02x}" * 70,
        lease=SignedDocument(body={}, signature="00" * 64),
    )


def _registry_entry(tmp_path: Path, seed: int) -> Any:
    registry = load_registry(tmp_path, FINGERPRINT, allow_legacy_fallback=False, fail_closed=True)
    return next(b for b in registry.bonds if b.pubkey == _bond(seed).pubkey)


def test_only_a_lease_bought_here_is_renewed_for_a_later_period(tmp_path: Path) -> None:
    first = _credential(0x61, expiry=1)
    assert import_bond_credential(first, CERTIFICATE, tmp_path, FINGERPRINT)
    assert not import_bond_credential(first, CERTIFICATE, tmp_path, FINGERPRINT)
    ours = {first.cert_pubkey}

    # Another certificate for the same period would be a second lease: refused.
    other = _key(0x56)
    with pytest.raises(BondImportError, match="conflicts"):
        import_bond_credential(
            _credential(0x61, expiry=1, certificate=other),
            other,
            tmp_path,
            FINGERPRINT,
            renewable=ours,
        )
    renewed = _credential(0x61, expiry=2, certificate=other)
    # Without provenance (a cold owner's own certificate, or a `jm-market
    # import`), an existing entry is never replaced.
    with pytest.raises(BondImportError, match="conflicts"):
        import_bond_credential(renewed, other, tmp_path, FINGERPRINT)
    assert import_bond_credential(renewed, other, tmp_path, FINGERPRINT, renewable=ours)
    entry = _registry_entry(tmp_path, 0x61)
    assert (entry.cert_pubkey, entry.cert_privkey, entry.cert_expiry) == (
        renewed.cert_pubkey,
        other.secret_bytes.hex(),
        2,
    )
    assert entry.index == -1
    # Never back to an earlier period.
    with pytest.raises(BondImportError, match="conflicts"):
        import_bond_credential(
            first, CERTIFICATE, tmp_path, FINGERPRINT, renewable=ours | {renewed.cert_pubkey}
        )
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
        import_bond_credential(
            _credential(0x61, expiry=2),
            CERTIFICATE,
            tmp_path,
            FINGERPRINT,
            renewable={bytes(CERTIFICATE.pub).hex()},
        )
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
    def __init__(self, result: str | Exception = "paid") -> None:
        self.result = result
        # What the node reports of an earlier payment.
        self.state: str | Exception = "pending"
        self.paid: list[tuple[str, int, int]] = []

    def routing_budget(self, cap_sats: int) -> int:
        return cap_sats // 5

    async def pay(
        self,
        bolt11: str,
        *,
        amount_sats: int,
        fee_limit_sats: int,
        expires_at: int,
        expected_payer: str | None = None,
    ) -> Any:
        assert expected_payer == bytes(_key(0x77).pub).hex()
        self.paid.append((bolt11, amount_sats, fee_limit_sats))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def identify(self, bolt11: str) -> PaymentIdentity:
        return PaymentIdentity(bytes(_key(0x77).pub).hex(), bolt11[-64:])

    async def status(self, identity: PaymentIdentity) -> Any:
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


def _settings(tmp_path: Path) -> Any:
    return SimpleNamespace(
        get_data_dir=lambda: tmp_path,
        network_config=SimpleNamespace(bitcoin_network=None, network=NetworkType.REGTEST),
    )


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Fake seller-side verification: each quote id names the seller's bond seed."""
    state = SimpleNamespace(
        height=1, imported=[], import_error=None, renewable=[], quote_lifetime=300
    )

    def seed_of(document: Any) -> int:
        return int(document[:2], 16)

    async def accept_quote(signed: SignedDocument, listing: Any, request: Any, *_: Any) -> Any:
        assert request.product == "bond" and request.certificate_pubkey is not None
        quote_id = signed.body["quote_id"]
        return SimpleNamespace(
            quote_id=quote_id,
            authorization=quote_id,
            payment=SimpleNamespace(amount_sats=listing.price_sats, request=f"lnbcrt{quote_id}"),
            expires_at=int(time.time()) + state.quote_lifetime,
        )

    def open_delivery(encrypted: str, _buyer: Any, quote: SignedDocument) -> Any:
        seed = seed_of(quote.body["quote_id"])
        return SimpleNamespace(
            verify=lambda: _credential(seed), authorization=quote.body["quote_id"]
        )

    def import_credential(credential: Any, certificate: CKey, *_: Any, renewable: Any = ()) -> bool:
        state.renewable.append(set(renewable))
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
    state.verify_market_bond = AsyncMock()
    monkeypatch.setattr(bond_purchase, "verify_market_bond", state.verify_market_bond)
    monkeypatch.setattr(
        bond_purchase,
        "verify_authorization",
        lambda auth: SimpleNamespace(
            bond=_bond(seed_of(auth)), period=bond_purchase.period_at_height(state.height)
        ),
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


def _lease(purchaser: BondPurchaser, market: SimpleNamespace) -> Any:
    return purchaser.lease(market.backend, cap_sats=10_000, max_rate=0.02)


def _quoted(transport: _Transport) -> list[str]:
    return [nick for nick, payload in transport.requests if payload.get("action") == "quote"]


@pytest.mark.asyncio
async def test_leases_the_highest_priced_bond_within_both_caps(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    # Cap 10_000 with a 2_000 routing budget leaves 8_000; 2% a year on the
    # 0.1 BTC bonds allows 7_671 for the whole period.
    transport = _market((0xA1, 9_000), (0xB2, 7_900), (0xC3, 5_000), (0xE5, 4_000))
    transport.listings.append(("J5sd4", _listing(0xD4, 6_000, ("podle",))))
    payer = _Payer()

    leased = await _lease(_purchaser(tmp_path, transport, payer), market)

    assert isinstance(leased, LeasedBond)
    assert leased.credential.bond == _bond(0xC3)
    assert leased.certificate.secret_bytes == CERTIFICATE.secret_bytes
    assert (leased.value, leased.confirmations) == (VALUE, 50)
    assert _quoted(transport) == ["J5sb2", "J5sc3"]
    assert payer.paid == [(f"lnbcrt{'c3' * 32}", 5_000, 5_000)]
    assert [credential.bond for credential, _ in market.imported] == [_bond(0xC3)]
    records = sorted(_records(tmp_path), key=lambda record: record["state"])
    assert [record["state"] for record in records] == ["done", "failed"]
    done, failed = records
    assert done["bond"] == f"{'c3' * 32}:1" and done["cert_expiry"] == 1
    assert done["cert_pubkey"] == bytes(CERTIFICATE.pub).hex() and done["period"] == 0
    # The lease stays recoverable through its period; nothing else is kept.
    assert {"certificate_secret", "credential"} <= done.keys()
    assert not {"buyer_secret", "quote", "listing", "delivery"} & done.keys()
    assert not {"buyer_secret", "certificate_secret", "quote", "listing"} & failed.keys()


@pytest.mark.asyncio
async def test_a_delivered_lease_is_handed_out_again_until_its_period_ends(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000))
    payer = _Payer()
    purchaser = _purchaser(tmp_path, transport, payer)
    first = await _lease(purchaser, market)
    assert first is not None

    # The maker could not adopt it (say a backend error): no second purchase.
    transport.requests.clear()
    again = await _lease(purchaser, market)
    assert again is not None and again.credential == first.credential
    assert again.certificate.secret_bytes == first.certificate.secret_bytes
    assert len(payer.paid) == 1 and transport.requests == []

    # Next period: its keys are dropped, and only a lease bought here renews.
    payer.state = "succeeded"
    market.height = 2017
    renewed = await _lease(purchaser, market)
    assert renewed is not None and len(payer.paid) == 2
    assert market.renewable[-1] == {bytes(CERTIFICATE.pub).hex()}
    old, new = sorted(_records(tmp_path), key=lambda record: record["period"])
    assert old["state"] == "done" and not {"certificate_secret", "credential"} & old.keys()
    assert new["state"] == "done" and new["period"] == 1


@pytest.mark.asyncio
async def test_an_undelivered_lease_is_collected_later_and_no_other_seller_is_paid(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000), (0xC3, 5_000))
    transport.deliver = False
    payer = _Payer()
    purchaser = _purchaser(tmp_path, transport, payer)

    assert await _lease(purchaser, market) is None
    (record,) = _records(tmp_path)
    assert record["state"] == "paying"
    assert {"buyer_secret", "certificate_secret"} <= record.keys()
    # Still undelivered and in flight: the other seller is not even asked.
    for state in ("pending", "succeeded", RuntimeError("node down")):
        payer.state = state
        transport.requests.clear()
        assert await _lease(purchaser, market) is None
        assert [payload.get("action") for _, payload in transport.requests] == ["delivery"]

    transport.deliver = True
    leased = await _lease(purchaser, market)
    assert leased is not None and leased.credential.bond == _bond(0xB2)
    assert len(payer.paid) == 1
    assert [record["state"] for record in _records(tmp_path)] == ["done"]


@pytest.mark.asyncio
async def test_a_payment_from_an_earlier_period_blocks_until_the_node_resolves_it(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000), (0xC3, 5_000))
    transport.deliver = False
    payer = _Payer()
    purchaser = _purchaser(tmp_path, transport, payer)
    assert await _lease(purchaser, market) is None

    # Its certificate is useless now, but a held payment can still settle.
    market.height = 2017
    transport.deliver = True
    transport.requests.clear()
    assert await _lease(purchaser, market) is None
    assert transport.requests == [] and len(payer.paid) == 1
    (record,) = _records(tmp_path)
    assert record["state"] == "paying" and "certificate_secret" in record

    # It settled without a usable delivery: evidence kept, seller never again.
    payer.state = "succeeded"
    leased = await _lease(purchaser, market)
    assert leased is not None and leased.credential.bond == _bond(0xC3)
    assert _quoted(transport) == ["J5sc3"]
    unused = next(record for record in _records(tmp_path) if record["state"] == "unused")
    assert {"buyer_secret", "certificate_secret", "quote"} <= unused.keys()
    assert market.imported[-1][0].bond == _bond(0xC3)


@pytest.mark.asyncio
async def test_a_settled_undelivered_lease_expires_at_the_next_boundary(
    tmp_path: Path,
    market: SimpleNamespace,
) -> None:
    transport = _market((0xB2, 6_000), (0xC3, 5_000))
    transport.deliver = False
    payer = _Payer()
    purchaser = _purchaser(tmp_path, transport, payer)
    assert await _lease(purchaser, market) is None
    payer.state = "succeeded"
    assert await _lease(purchaser, market) is None
    assert _records(tmp_path)[0]["payment_state"] == "succeeded"
    # Settlement is already durable when the period changes. No new lookup
    # is necessary to retire the unusable lease and exclude its seller.
    payer.state = RuntimeError("node offline")
    market.height = 2017
    transport.deliver = True
    result = await _lease(purchaser, market)
    assert result is not None and result.credential.bond == _bond(0xC3)
    assert len(payer.paid) == 2


@pytest.mark.asyncio
async def test_missing_payment_history_never_proves_failure_even_after_expiry(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000))
    transport.deliver = False
    # The payment call dies without an answer: it may have left.
    payer = _Payer(RuntimeError("connection lost"))
    purchaser = _purchaser(tmp_path, transport, payer)
    assert await _lease(purchaser, market) is None
    payer.state = "pending"
    # Missing history cannot prove that an accepted HTLC cannot settle.
    assert await _lease(purchaser, market) is None
    assert [record["state"] for record in _records(tmp_path)] == ["paying"]

    market.quote_lifetime = -1
    for path in (tmp_path / "market" / "bond-purchases").glob("*.json"):
        data = json.loads(path.read_text())
        data["expires_at"] = int(time.time()) - 1
        path.write_text(json.dumps(data))
    payer.result = "failed"
    assert await _lease(purchaser, market) is None
    assert [record["state"] for record in _records(tmp_path)] == ["paying"]
    assert len(payer.paid) == 1
    payer.state = "failed"
    assert await _lease(purchaser, market) is None
    assert [record["state"] for record in _records(tmp_path)] == ["failed", "failed"]
    assert len(payer.paid) == 2


@pytest.mark.asyncio
async def test_an_unusable_lease_blocks_its_period_and_its_seller(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000), (0xC3, 5_000))
    assert await _lease(_purchaser(tmp_path, transport, _Payer("failed")), market) is None
    assert [record["state"] for record in _records(tmp_path)] == ["failed", "failed"]

    market.import_error = "existing bond registry entry conflicts with delivered credential"
    payer = _Payer()
    purchaser = _purchaser(tmp_path, transport, payer)
    assert await _lease(purchaser, market) is None
    # Paid for, so its keys and the sealed delivery are kept; nobody else is paid.
    (kept,) = [record for record in _records(tmp_path) if record["state"] == "unimported"]
    assert kept["delivery"] == f"sealed-{'b2' * 32}"
    assert {"buyer_secret", "certificate_secret", "quote"} <= kept.keys()
    market.import_error = None
    assert await _lease(purchaser, market) is None
    assert len(payer.paid) == 1

    market.height = 2017
    payer.state = "succeeded"
    leased = await _lease(purchaser, market)
    assert leased is not None and leased.credential.bond == _bond(0xC3)
    assert [paid[1] for paid in payer.paid] == [6_000, 5_000]


@pytest.mark.asyncio
async def test_a_failing_chain_check_is_retried_with_the_kept_delivery(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _market((0xB2, 6_000), (0xC3, 5_000))
    market.verify_market_bond.side_effect = [RuntimeError("backend down"), None]
    payer = _Payer()
    purchaser = _purchaser(tmp_path, transport, payer)
    assert await _lease(purchaser, market) is None
    (record,) = _records(tmp_path)
    assert record["state"] == "paying" and record["delivery"] == f"sealed-{'b2' * 32}"

    transport.deliver = False
    transport.requests.clear()
    leased = await _lease(purchaser, market)
    assert leased is not None and leased.credential.bond == _bond(0xB2)
    assert transport.requests == [] and len(payer.paid) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        "null",
        '{"version": 1, "state": "later"}',
        '{"version": 1, "state": "paying"}',
        '{"version": 1, "state": "done"}',
    ],
)
async def test_an_unreadable_record_blocks_every_purchase(
    tmp_path: Path, market: SimpleNamespace, content: str
) -> None:
    directory = tmp_path / "market" / "bond-purchases"
    directory.mkdir(parents=True)
    (directory / "unknown.json").write_text(content)
    transport = _market((0xB2, 6_000))
    payer = _Payer()
    assert await _lease(_purchaser(tmp_path, transport, payer), market) is None
    assert transport.requests == [] and payer.paid == []
    assert (directory / "unknown.json").read_text() == content


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_state", ["done", "invalid", "unimported"])
async def test_delivery_never_proves_settlement_across_periods(
    tmp_path: Path,
    market: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    delivery_state: str,
) -> None:
    transport = _market((0xB2, 6_000), (0xC3, 5_000))
    payer = _Payer()
    if delivery_state == "invalid":
        monkeypatch.setattr(bond_purchase, "open_delivery", Mock(side_effect=ValueError("junk")))
    elif delivery_state == "unimported":
        market.import_error = "conflict"
    purchaser = _purchaser(tmp_path, transport, payer)
    await _lease(purchaser, market)
    assert _records(tmp_path)[0]["state"] == delivery_state
    # Both invoice aging and a period boundary leave settlement unresolved.
    for path in (tmp_path / "market" / "bond-purchases").glob("*.json"):
        data = json.loads(path.read_text())
        data["expires_at"] = int(time.time()) - 7 * 86_400
        path.write_text(json.dumps(data))
    market.height = 2017
    transport.requests.clear()
    assert await _lease(purchaser, market) is None
    assert len(payer.paid) == 1 and transport.requests == []
    kept = _records(tmp_path)[0]
    assert kept["payment_state"] == "pending"
    assert "certificate_secret" in kept
    payer.state = "succeeded"
    # A terminal status finally permits the next period's purchase.
    await _lease(purchaser, market)
    assert len(payer.paid) == 2


@pytest.mark.asyncio
async def test_cancellation_during_payment_keeps_a_blocking_record(
    tmp_path: Path,
    market: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _market((0xB2, 6_000))
    payer = _Payer()
    payment = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(payer, "pay", payment)
    purchaser = _purchaser(tmp_path, transport, payer)
    with pytest.raises(asyncio.CancelledError):
        await _lease(purchaser, market)
    (record,) = _records(tmp_path)
    assert record["state"] == "paying" and record["payment_state"] == "pending"
    transport.deliver = False
    assert await _lease(purchaser, market) is None
    assert payment.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_intent_write_prevents_payment(
    tmp_path: Path,
    market: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = PurchaseRecord.save

    def fail_paying(record: PurchaseRecord) -> None:
        if record.state == "paying":
            raise OSError(errno.EIO, "disk failure")
        original(record)

    monkeypatch.setattr(PurchaseRecord, "save", fail_paying)
    payer = _Payer()
    assert await _lease(_purchaser(tmp_path, _market((0xB2, 6_000)), payer), market) is None
    assert payer.paid == []


def test_directory_durability_errors_are_not_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from taker import podle_purchase

    record = PurchaseRecord(tmp_path / "purchase.json", {"version": 1, "state": "requested"})
    # Bypass the file fsync, isolating the post-rename directory fsync.
    monkeypatch.setattr(podle_purchase, "atomic_write_private", Mock())
    monkeypatch.setattr(podle_purchase.os, "fsync", Mock(side_effect=OSError(errno.EIO, "I/O")))
    with pytest.raises(OSError) as failure:
        record.save()
    assert failure.value.errno == errno.EIO
