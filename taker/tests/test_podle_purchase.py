"""Automatic external PoDLE purchases for ``external_podle_mode = "prefer"``."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import hash160
from jmcore.channel_ring import ChannelRingNodeConfig
from jmcore.credential_market import (
    MARKET_LISTING_LIFETIME_SECONDS,
    MarketListing,
    SignedDocument,
    canonical,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLE
from jmcore.models import NetworkType
from jmcore.podle import generate_podle

from taker import podle_purchase
from taker.podle_purchase import (
    InteractivePayer,
    NodePayer,
    PaymentIdentity,
    PoDLEPurchaser,
    foreign_commitments,
    min_backing_sats,
    payer_for_mixdepth,
    purchase_cap_sats,
)

CJ_AMOUNT = 1_000_000


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


def _credential(value: int = 0x41) -> ExternalPoDLE:
    proof = generate_podle(bytes([value]) * 32, f"{value:02x}" * 32 + ":1", index=0)
    return ExternalPoDLE.model_validate(
        {
            "version": 1,
            "network": "regtest",
            "outpoint": {"txid": f"{value:02x}" * 32, "vout": 1},
            "P": proof.p.hex(),
            "P2": proof.p2.hex(),
            "sig": proof.sig.hex(),
            "e": proof.e.hex(),
            "commitment": proof.commitment.hex(),
            "index": 0,
            "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
            "blockheight": 101,
        }
    )


def _listing(seed: int, price: int, products: tuple[str, ...] = ("podle",)) -> bytes:
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


def _quote_document(quote_id: str) -> dict[str, Any]:
    signature = _key(0x77).sign(b"\x01" * 32).hex()
    return SignedDocument(body={"quote_id": quote_id}, signature=signature).model_dump(mode="json")


class _Transport:
    """Market relay stand-in: per-seller quote answers and a delivery switch."""

    def __init__(self, listings: list[tuple[str, bytes]], quotes: dict[str, dict[str, Any]]):
        self.listings = listings
        self.quotes = quotes
        self.deliver = True
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

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


def _backend(fee_rate: float = 1.0) -> Any:
    backend = AsyncMock()
    backend.can_estimate_fee = Mock(return_value=True)
    backend.estimate_fee = AsyncMock(return_value=fee_rate)
    return backend


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace seller-side verification (bonds, sealed boxes) with recorded fakes."""
    credential = _credential()
    quoted: list[Any] = []

    async def accept_quote(signed: SignedDocument, listing: Any, request: Any, *_: Any) -> Any:
        quoted.append(request)
        return SimpleNamespace(
            payment=SimpleNamespace(
                amount_sats=listing.price_sats, request=f"lnbcrt{signed.body['quote_id']}"
            ),
            expires_at=int(time.time()) + 300,
        )

    package = SimpleNamespace(verify=lambda: credential, authorization="authorization")
    monkeypatch.setattr(podle_purchase, "accept_quote", accept_quote)
    monkeypatch.setattr(podle_purchase, "open_delivery", Mock(return_value=package))
    monkeypatch.setattr(podle_purchase, "verify_podle_backing", AsyncMock())
    monkeypatch.setattr(
        podle_purchase, "verify_authorization", Mock(return_value=SimpleNamespace(bond="bond"))
    )
    monkeypatch.setattr(podle_purchase, "POLL_INTERVAL_SECONDS", 0.0)
    return SimpleNamespace(credential=credential, quoted=quoted)


def _records(tmp_path: Path) -> dict[str, dict[str, Any]]:
    directory = tmp_path / "market" / "purchases"
    return {path.stem: json.loads(path.read_text()) for path in directory.glob("*.json")}


def test_cap_is_twice_a_one_in_one_out_taproot_spend() -> None:
    # 111 vB at the 3-block rate, never below 1 sat/vB, doubled.
    assert purchase_cap_sats(0.2) == 222
    assert purchase_cap_sats(2.3) == 2 * 256
    assert min_backing_sats(CJ_AMOUNT, 20) == 200_000


def test_invoices_are_paid_only_by_the_source_mixdepth_node(tmp_path: Path) -> None:
    node = ChannelRingNodeConfig(
        lnd_grpc_url="https://127.0.0.1:10009",
        lnd_tls_cert_path=tmp_path / "tls.cert",
        lnd_macaroon_path=tmp_path / "admin.macaroon",
        onion_endpoint="a" * 56 + ".onion:9735",
    )
    unattended = payer_for_mixdepth({"m0": node}, {0: "m0"}, interactive=False)
    attended = payer_for_mixdepth({"m0": node}, {0: "m0"}, interactive=True)

    assert isinstance(unattended(0), NodePayer)
    # Another mixdepth's node would link the two mixdepths through the seller.
    assert unattended(1) is None
    assert isinstance(attended(1), InteractivePayer)
    # A node that cannot route still lets a person pay the invoice.
    node_payer = attended(0)
    assert isinstance(node_payer, NodePayer)
    assert isinstance(node_payer.fallback, InteractivePayer)


@pytest.mark.asyncio
async def test_node_payer_reports_what_its_node_knows_of_a_payment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jmswap.lnd_peer import PaymentResult, PaymentStatus

    payment_hash = b"\x11" * 32
    tracked: list[bytes] = []

    class _Client:
        result: PaymentResult | None = None

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def inspect_invoice(self, bolt11: str) -> Any:
            assert bolt11 == "lnbcrt1"
            return SimpleNamespace(payment_hash=payment_hash)

        async def node_info(self) -> Any:
            return SimpleNamespace(identity_pubkey="original")

        async def track_payment(self, value: bytes) -> PaymentResult | None:
            tracked.append(value)
            return self.result

    client = _Client()
    payer = NodePayer(Mock())
    monkeypatch.setattr(payer, "_client", lambda: client)
    identity = await payer.identify("lnbcrt1")
    assert identity == PaymentIdentity("original", payment_hash.hex())
    # Missing history and empty streams are not proof of failure.
    assert await payer.status(identity) == "pending"
    for status, expected in (
        (PaymentStatus.IN_FLIGHT, "pending"),
        (PaymentStatus.SUCCEEDED, "succeeded"),
        (PaymentStatus.FAILED, "failed"),
    ):
        client.result = PaymentResult(
            status=status, payment_hash=payment_hash, preimage=None, fee_sat=0
        )
        assert await payer.status(identity) == expected
    assert tracked == [payment_hash] * 4
    with pytest.raises(ValueError, match="original paying node"):
        await payer.status(PaymentIdentity("replacement", payment_hash.hex()))
    assert tracked == [payment_hash] * 4


@pytest.mark.asyncio
async def test_buys_the_cheapest_listing_within_the_cap(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _Transport(
        [
            ("J5dear", _listing(0x11, 150)),
            ("J5cheap", _listing(0x12, 100)),
            ("J5toodear", _listing(0x13, 200)),
            ("J5bonds", _listing(0x14, 50, ("bond",))),
        ],
        {nick: {"quote": _quote_document("ab" * 32)} for nick in ("J5dear", "J5cheap")},
    )
    payer = _Payer()
    manager = Mock()
    purchaser = PoDLEPurchaser(
        _settings(tmp_path), lambda _md: payer, transport_factory=lambda: transport
    )

    bought = await purchaser.buy(
        backend=_backend(), podle_manager=manager, mixdepth=0, cj_amount=CJ_AMOUNT, min_percent=20
    )

    assert bought is True
    # Cap 222 sats: a fifth is kept for routing, so quotes may cost at most 178.
    [(nick, request)] = [entry for entry in transport.requests if "max_price_sats" in entry[1]]
    assert nick == "J5cheap"
    assert (request["max_price_sats"], request["min_backing_sats"]) == (178, 200_000)
    assert payer.paid == [("lnbcrt" + "ab" * 32, 100, 122)]
    manager.import_external.assert_called_once_with(market.credential, seller_bond="bond")
    [record] = _records(tmp_path).values()
    assert record["state"] == "done" and "buyer_secret" not in record
    path = next((tmp_path / "market" / "purchases").glob("*.json"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert transport.closed
    # Only the other mixdepths must stay away from this credential.
    assert foreign_commitments(tmp_path, 0) == frozenset()
    assert foreign_commitments(tmp_path, 1) == {market.credential.commitment}


@pytest.mark.asyncio
async def test_a_seller_without_a_quote_is_skipped_and_nothing_is_paid_twice(
    tmp_path: Path, market: SimpleNamespace
) -> None:
    transport = _Transport(
        [("J5first", _listing(0x11, 90)), ("J5second", _listing(0x12, 100))],
        {"J5second": {"quote": _quote_document("cd" * 32)}},
    )
    payer = _Payer()
    purchaser = PoDLEPurchaser(
        _settings(tmp_path), lambda _md: payer, transport_factory=lambda: transport
    )

    assert await purchaser.buy(
        backend=_backend(), podle_manager=Mock(), mixdepth=0, cj_amount=CJ_AMOUNT, min_percent=20
    )

    assert [amount for _, amount, _ in payer.paid] == [100]
    states = sorted(record["state"] for record in _records(tmp_path).values())
    assert states == ["done", "failed"]


@pytest.mark.asyncio
async def test_an_undelivered_payment_stops_and_is_collected_on_the_next_run(
    tmp_path: Path, market: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(podle_purchase, "NODE_DELIVERY_SECONDS", 0.0)
    quotes = {nick: {"quote": _quote_document("ef" * 32)} for nick in ("J5a", "J5b")}
    transport = _Transport([("J5a", _listing(0x11, 90)), ("J5b", _listing(0x12, 100))], quotes)
    transport.deliver = False
    payer = _Payer()
    manager = Mock()
    first = PoDLEPurchaser(
        _settings(tmp_path), lambda _md: payer, transport_factory=lambda: transport
    )

    assert not await first.buy(
        backend=_backend(), podle_manager=manager, mixdepth=0, cj_amount=CJ_AMOUNT, min_percent=20
    )
    # The first payment may still settle, so no second seller is paid.
    assert len(payer.paid) == 1
    [record] = _records(tmp_path).values()
    assert record["state"] == "paying" and "buyer_secret" in record

    # Another mixdepth's run never collects it: its node did not pay for it.
    transport.deliver = True
    other = PoDLEPurchaser(
        _settings(tmp_path), lambda _md: payer, transport_factory=lambda: transport
    )
    transport.listings = []
    assert not await other.buy(
        backend=_backend(), podle_manager=manager, mixdepth=1, cj_amount=CJ_AMOUNT, min_percent=20
    )
    manager.import_external.assert_not_called()

    second = PoDLEPurchaser(
        _settings(tmp_path), lambda _md: payer, transport_factory=lambda: transport
    )
    assert await second.buy(
        backend=_backend(), podle_manager=manager, mixdepth=0, cj_amount=CJ_AMOUNT, min_percent=20
    )
    assert len(payer.paid) == 1
    manager.import_external.assert_called_once()
    [record] = _records(tmp_path).values()
    assert record["state"] == "done"


@pytest.mark.asyncio
async def test_no_payer_or_spent_budget_buys_nothing(tmp_path: Path) -> None:
    factory = Mock()
    unpaid = PoDLEPurchaser(_settings(tmp_path), lambda _md: None, transport_factory=factory)
    spent = PoDLEPurchaser(
        _settings(tmp_path), lambda _md: _Payer(), max_purchases=0, transport_factory=factory
    )

    for purchaser in (unpaid, spent):
        assert not await purchaser.buy(
            backend=_backend(),
            podle_manager=Mock(),
            mixdepth=0,
            cj_amount=CJ_AMOUNT,
            min_percent=20,
        )
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_interactive_payment_shows_the_invoice() -> None:
    shown: list[str] = []
    payer = InteractivePayer(write=shown.append)

    outcome = await payer.pay("lnbcrt1invoice", amount_sats=150, fee_limit_sats=0, expires_at=1)
    assert outcome == "shown"
    assert "lightning:lnbcrt1invoice" in shown[0]
    assert payer.routing_budget(222) == 0


class _Purchaser:
    """Stands in for PoDLEPurchaser inside a taker round."""

    def __init__(self, on_buy: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._on_buy = on_buy

    def can_pay(self, mixdepth: int) -> bool:
        return True

    async def buy(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return bool(self._on_buy(kwargs["podle_manager"])) if self._on_buy else False


def _prefer_taker(tmp_path: Path, record: ExternalPoDLE, purchaser: Any) -> Any:
    from _taker_test_helpers import make_taker_config, make_utxo
    from test_external_podle_seller_separation import _backend_for, _offer, _wallet

    from taker.taker import Taker

    utxo = make_utxo(txid_char="a", value=25_000_000, confirmations=10)
    config = make_taker_config(
        data_dir=tmp_path,
        counterparty_count=2,
        minimum_makers=2,
        taker_utxo_age=5,
        taker_utxo_amtpercent=20,
        fee_rate=1.0,
        external_podle_mode="prefer",
    )
    taker = Taker(_wallet([utxo]), _backend_for(record), config, podle_purchaser=purchaser)
    offers = [_offer("J5seller"), _offer("J5maker1"), _offer("J5maker2")]
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=offers)
    taker._update_offers_with_bond_values = AsyncMock(return_value=None)
    # The round stops right after maker selection and PoDLE allocation.
    taker._run_fill_with_replacements = AsyncMock(return_value=False)
    return taker


@pytest.fixture
def _no_blacklist(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_external_podle_seller_separation import _EmptyBlacklist

    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_blacklist")
async def test_prefer_buys_a_credential_and_commits_to_it(tmp_path: Path) -> None:
    from test_external_podle_seller_separation import _bond, _record

    record, seller_bond = _record(), _bond()
    purchaser = _Purchaser(lambda manager: manager.import_external(record, seller_bond=seller_bond))
    taker = _prefer_taker(tmp_path, record, purchaser)
    taker.podle_manager.generate_fresh_commitment = Mock()

    await taker.do_coinjoin(amount=5_000_000, destination="bcrt1q" + "x" * 38, mixdepth=0)

    [call] = purchaser.calls
    assert (call["mixdepth"], call["cj_amount"], call["min_percent"]) == (0, 5_000_000, 20)
    assert taker._session.podle_seller_bonds == [seller_bond]
    assert taker.podle_manager.external_count() == 0
    taker.podle_manager.generate_fresh_commitment.assert_not_called()
    # No own UTXO was forced into the inputs for the commitment.
    taker.wallet.get_all_utxos.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_blacklist")
async def test_prefer_falls_back_to_own_utxos_and_skips_other_mixdepths_purchases(
    tmp_path: Path,
) -> None:
    from test_external_podle_seller_separation import _bond, _record

    record = _record()
    purchaser = _Purchaser()
    taker = _prefer_taker(tmp_path, record, purchaser)
    # Bought earlier from mixdepth 1's node: using it here would link the mixdepths.
    taker.podle_manager.import_external(record, seller_bond=_bond())
    purchases = tmp_path / "market" / "purchases"
    purchases.mkdir(parents=True)
    (purchases / "old.json").write_text(
        json.dumps({"version": 1, "state": "done", "mixdepth": 1, "commitment": record.commitment})
    )
    own = Mock(name="own commitment")
    taker.podle_manager.get_fresh_commitment_utxos = Mock(
        return_value=taker.wallet.get_all_utxos.return_value
    )
    taker.podle_manager.generate_fresh_commitment = Mock(return_value=own)

    await taker.do_coinjoin(amount=5_000_000, destination="bcrt1q" + "x" * 38, mixdepth=0)

    assert len(purchaser.calls) == 1
    assert taker._session.podle_commitment is own
    assert taker._session.podle_seller_bonds == []
    assert taker.podle_manager.external_count() == 1


@pytest.mark.asyncio
async def test_prefer_relaxes_the_local_podle_size_check_only_with_a_payer(
    tmp_path: Path,
) -> None:
    from _taker_test_helpers import make_utxo
    from test_external_podle_seller_separation import _record

    small = [make_utxo(txid_char="b", value=600_000, confirmations=10)] * 10
    with_payer = _prefer_taker(tmp_path / "a", _record(), _Purchaser())
    without = _prefer_taker(tmp_path / "b", _record(), None)
    for taker in (with_payer, without):
        taker.wallet.get_utxos = AsyncMock(return_value=small)
        taker.wallet.get_all_utxos = Mock(return_value=small)
        taker.wallet.select_utxos = Mock(return_value=small)

    assert await with_payer.check_utxo_eligibility(5_000_000, 0) is None
    reason = await without.check_utxo_eligibility(5_000_000, 0)
    assert reason is not None and "PoDLE commitment" in reason


def test_cli_wires_the_purchaser_only_for_prefer(tmp_path: Path) -> None:
    from taker.cli import _external_podle_mode, _podle_purchaser

    p2tr: Any = SimpleNamespace(address_type="p2tr")
    assert _external_podle_mode("prefer", p2tr) == "prefer"
    with pytest.raises(ValueError, match="requires wallet.address_type"):
        _external_podle_mode("prefer", SimpleNamespace(address_type="p2wpkh"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="disabled, only or prefer"):
        _external_podle_mode("always", p2tr)

    ring = SimpleNamespace(nodes={}, mixdepth_nodes={})
    only: Any = SimpleNamespace(external_podle_mode="only", channel_ring=ring)
    prefer: Any = SimpleNamespace(external_podle_mode="prefer", channel_ring=ring)
    assert _podle_purchaser(_settings(tmp_path), only, interactive=True) is None
    unattended = _podle_purchaser(_settings(tmp_path), prefer, interactive=False)
    attended = _podle_purchaser(_settings(tmp_path), prefer, interactive=True)
    assert unattended is not None and not unattended.can_pay(0)
    assert attended is not None and attended.can_pay(0)
