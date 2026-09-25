"""Offline coverage for the file-oriented native market CLI."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from bitcointx.core.key import CKey  # type: ignore[import-not-found]
from jmcore.bitcoin import address_to_scriptpubkey_for_network, hash160
from jmcore.credential_market import (
    Allocation,
    BondReference,
    CredentialPackage,
    Delivery,
    FaultProof,
    MarketAuthorization,
    MarketListing,
    MarketQuote,
    PaymentTerms,
    SignedDocument,
    bond_resource,
    canonical,
    document_hash,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_faults import MarketFaultCache
from jmcore.market_store import MarketStore
from jmcore.models import NetworkType
from jmcore.podle import generate_podle
from jmcore.settings import JoinMarketSettings, NetworkSettings
from jmwallet.backends.base import BlockchainBackend, BondVerificationRequest
from nacl.public import PrivateKey

from taker import market_cli
from taker._vendor.bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, encode
from taker.market_service import MarketService


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _json(path: Path, value: dict[str, object]) -> None:
    _write(path, canonical(value))


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


class _Backend:
    def __init__(
        self, payment: PaymentTerms | None = None, *, amount: int = 100, neutrino: bool = False
    ) -> None:
        self.payment = payment
        self.amount = amount
        self.neutrino = neutrino

    async def get_block_height(self) -> int:
        return 1

    async def get_block_time(self, _height: int) -> int:
        return int(time.time()) - 3600

    async def verify_bonds(self, bonds: list[BondVerificationRequest]) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                valid=True,
                txid=bond.txid,
                vout=bond.vout,
                confirmations=6,
                value=1_000_000,
            )
            for bond in bonds
        ]

    async def get_utxo(self, txid: str, vout: int) -> SimpleNamespace | None:
        if self.payment is None:
            return None
        return SimpleNamespace(
            txid=txid,
            vout=vout,
            value=self.amount,
            confirmations=2,
            scriptpubkey=address_to_scriptpubkey_for_network(self.payment.request, "regtest").hex(),
        )

    def requires_neutrino_metadata(self) -> bool:
        return self.neutrino

    async def close(self) -> None:
        return None


class _LoopbackTransport:
    def __init__(self, service: MarketService) -> None:
        self.service = service
        self.request_senders: list[str] = []

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def request(
        self,
        sender: str,
        _encryption_key: bytes,
        payload: dict[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        self.request_senders.append(sender)
        return await self.service.respond(sender, payload)


def _settings(data_dir: Path) -> JoinMarketSettings:
    return JoinMarketSettings(
        data_dir=data_dir,
        network_config=NetworkSettings(network=NetworkType.REGTEST, directory_servers=[]),
    )


def _bond(owner: CKey) -> BondReference:
    return BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=1),
        pubkey=bytes(owner.pub).hex(),
        locktime=int(time.time()) + 86_400,
    )


PREIMAGE = bytes(range(32))


def _payment_terms() -> PaymentTerms:
    """Lightning terms a seller can settle with PREIMAGE; the only served rail."""
    return PaymentTerms(
        rail="lightning",
        request=_lightning_invoice(int(time.time()), PREIMAGE),
        amount_sats=100,
    )


def _podle_metadata(owner: CKey) -> dict[str, object]:
    proof = generate_podle(owner.secret_bytes, f"{'11' * 32}:2", 0)
    return {
        "network": "regtest",
        "outpoint": {"txid": "11" * 32, "vout": 2},
        "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
        "blockheight": 1,
        "index": 0,
    }


def test_offline_seller_buyer_lifecycle_uses_real_crypto(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(market_cli, "_settings", lambda _args: settings)

    seller_keys = tmp_path / "seller-keys.json"
    assert market_cli.run(["keygen", "--output", str(seller_keys)]) == 0
    assert capsys.readouterr().out == ""
    seller_public = tmp_path / "seller-public.json"
    assert (
        market_cli.run(["public", "--keys", str(seller_keys), "--output", str(seller_public)]) == 0
    )

    owner = _key(0x51)
    owner_file = tmp_path / "owner.key"
    _write(owner_file, owner.secret_bytes.hex().encode("ascii"))
    bond_ref = tmp_path / "bond.json"
    _json(bond_ref, _bond(owner).model_dump(mode="json"))
    authorization = tmp_path / "authorization.json"
    assert (
        market_cli.run(
            [
                "authorize",
                "--bond-ref",
                str(bond_ref),
                "--seller-pub",
                str(seller_public),
                "--owner-key",
                str(owner_file),
                "--period",
                "0",
                "--output",
                str(authorization),
            ]
        )
        == 0
    )

    metadata = tmp_path / "podle-metadata.json"
    _json(metadata, _podle_metadata(owner))
    podle = tmp_path / "podle.json"
    assert (
        market_cli.run(
            [
                "export-podle",
                "--owner-key",
                str(owner_file),
                "--metadata",
                str(metadata),
                "--output",
                str(podle),
            ]
        )
        == 0
    )
    terms = _payment_terms()
    terms_file = tmp_path / "terms.json"
    _json(terms_file, terms.model_dump(mode="json"))
    assert (
        market_cli.run(
            ["seller", "add-inventory", "--data-dir", str(tmp_path), "--credential", str(podle)]
        )
        == 0
    )
    assert (
        market_cli.run(
            [
                "seller",
                "add-payment",
                "--data-dir",
                str(tmp_path),
                "--terms",
                str(terms_file),
            ]
        )
        == 0
    )

    seller_bundle = json.loads(seller_keys.read_text(encoding="ascii"))
    service_backend = _Backend(terms)
    service_store = MarketStore(tmp_path / "market" / "seller.sqlite")
    service = MarketService(
        service_store,
        market_cli._signed_document(authorization),
        CKey(bytes.fromhex(seller_bundle["seller_signing_key"])),
        PrivateKey(bytes.fromhex(seller_bundle["encryption_key"])),
        cast(BlockchainBackend, service_backend),
        products=["podle"],
        price_sats=100,
    )
    listing = asyncio.run(service.listing())
    listing_file = tmp_path / "listing.json"
    _json(
        listing_file,
        {
            "seller_nick": "J5ABCDEFGHJKLMNP",
            "listing": json.loads(listing.decode("ascii")),
        },
    )
    monkeypatch.setattr(market_cli, "_new_backend", lambda _settings: service_backend)
    loopback = _LoopbackTransport(service)
    monkeypatch.setattr(market_cli, "_new_transport", lambda _settings, **_kwargs: loopback)

    request_file = tmp_path / "request.json"
    quote_file = tmp_path / "quote.json"
    assert (
        market_cli.run(
            [
                "request",
                "--data-dir",
                str(tmp_path),
                "--listing",
                str(listing_file),
                "--product",
                "podle",
                "--max-price-sats",
                "100",
                "--request-file",
                str(request_file),
                "--output",
                str(quote_file),
                "--require-experimental-risk-ack",
            ]
        )
        == 0
    )
    quote = json.loads(quote_file.read_text(encoding="ascii"))["quote"]
    quote_id = quote["body"]["quote_id"]
    assert request_file.stat().st_mode & 0o777 == 0o600
    request_data = json.loads(request_file.read_text(encoding="ascii"))
    assert request_data["version"] == 2

    listing_document = SignedDocument.model_validate(json.loads(listing.decode("ascii")))
    seller_key = CKey(bytes.fromhex(seller_bundle["seller_signing_key"]))
    listing_model = listing_document.verified(MarketListing, bytes(seller_key.pub).hex())
    refreshed_listing = sign_document(
        listing_model.model_copy(update={"expires_at": listing_model.expires_at + 1}), seller_key
    )
    refreshed_listing_file = tmp_path / "refreshed-listing.json"
    _json(
        refreshed_listing_file,
        {
            "seller_nick": "J5bcdefghjkmnpqr",
            "listing": refreshed_listing.model_dump(mode="json"),
        },
    )
    service._next_quote = 0
    retry_quote_file = tmp_path / "retry-quote.json"
    assert (
        market_cli.run(
            [
                "request",
                "--data-dir",
                str(tmp_path),
                "--listing",
                str(refreshed_listing_file),
                "--product",
                "podle",
                "--max-price-sats",
                "100",
                "--request-file",
                str(request_file),
                "--output",
                str(retry_quote_file),
                "--require-experimental-risk-ack",
            ]
        )
        == 0
    )
    assert json.loads(retry_quote_file.read_text(encoding="ascii"))["quote"] == quote
    assert loopback.request_senders[-1] == "J5bcdefghjkmnpqr"

    assert (
        market_cli.run(
            [
                "seller",
                "attach",
                "--data-dir",
                str(tmp_path),
                "--quote-id",
                quote_id,
                "--credential",
                str(podle),
            ]
        )
        == 0
    )
    package_file = tmp_path / "package.json"
    preimage_file = tmp_path / "preimage"
    _write(preimage_file, PREIMAGE)
    assert (
        market_cli.run(
            [
                "seller",
                "settle",
                "--data-dir",
                str(tmp_path),
                "--quote-id",
                quote_id,
                "--keys",
                str(seller_keys),
                "--preimage-file",
                str(preimage_file),
                "--acknowledge-ln-settlement",
                "--output",
                str(package_file),
            ]
        )
        == 0
    )
    raw_delivery = tmp_path / "delivery.json"
    assert (
        market_cli.run(
            [
                "poll",
                "--data-dir",
                str(tmp_path),
                "--quote",
                str(quote_file),
                "--seller",
                str(listing_file),
                "--request-file",
                str(request_file),
                "--raw-output",
                str(raw_delivery),
            ]
        )
        == 0
    )
    market_cli._credential_package(raw_delivery)

    restarted_delivery = tmp_path / "restarted-delivery.json"
    assert (
        market_cli.run(
            [
                "poll",
                "--data-dir",
                str(tmp_path),
                "--quote",
                str(quote_file),
                "--seller",
                str(refreshed_listing_file),
                "--request-file",
                str(request_file),
                "--raw-output",
                str(restarted_delivery),
            ]
        )
        == 0
    )
    assert restarted_delivery.read_bytes() == raw_delivery.read_bytes()

    expired_listing = sign_document(
        listing_model.model_copy(update={"expires_at": int(time.time()) - 1}), seller_key
    )
    expired_listing_file = tmp_path / "expired-listing.json"
    _json(
        expired_listing_file,
        {
            "seller_nick": "J5bcdefghjkmnpqr",
            "listing": expired_listing.model_dump(mode="json"),
        },
    )
    expired_delivery = tmp_path / "expired-delivery.json"
    assert (
        market_cli.run(
            [
                "poll",
                "--data-dir",
                str(tmp_path),
                "--quote",
                str(quote_file),
                "--seller",
                str(expired_listing_file),
                "--request-file",
                str(request_file),
                "--raw-output",
                str(expired_delivery),
            ]
        )
        == 0
    )
    assert expired_delivery.read_bytes() == raw_delivery.read_bytes()

    wrong_seller = _key(0x70)
    wrong_listing = MarketListing(
        network=listing_model.network,
        period=listing_model.period,
        seller_pubkey=bytes(wrong_seller.pub).hex(),
        encryption_pubkey=bytes(PrivateKey.generate().public_key).hex(),
        products=listing_model.products,
        price_sats=listing_model.price_sats,
        expires_at=int(time.time()) + 60,
    )
    wrong_listing_file = tmp_path / "wrong-listing.json"
    _json(
        wrong_listing_file,
        {
            "seller_nick": "J5bcdefghjkmnpqr",
            "listing": sign_document(wrong_listing, wrong_seller).model_dump(mode="json"),
        },
    )
    requests_before = len(loopback.request_senders)
    assert (
        market_cli.run(
            [
                "poll",
                "--data-dir",
                str(tmp_path),
                "--quote",
                str(quote_file),
                "--seller",
                str(wrong_listing_file),
                "--request-file",
                str(request_file),
                "--raw-output",
                str(tmp_path / "wrong-seller-delivery.json"),
            ]
        )
        == 1
    )
    assert len(loopback.request_senders) == requests_before

    after_expiry = int(time.time()) + 3600
    monkeypatch.setattr(market_cli.time, "time", lambda: after_expiry)
    exported_package = tmp_path / "exported-package.json"
    assert (
        market_cli.run(
            [
                "seller",
                "export",
                "--data-dir",
                str(tmp_path),
                "--quote-id",
                quote_id,
                "--output",
                str(exported_package),
            ]
        )
        == 0
    )
    assert exported_package.read_bytes() == package_file.read_bytes()
    assert exported_package.stat().st_mode & 0o777 == 0o600
    assert seller_bundle["seller_signing_key"] not in capsys.readouterr().out

    # A delivery sealed to this purchase must not open with another purchase's key.
    foreign_request = tmp_path / "foreign-request.json"
    _write(foreign_request, b"{}")
    _json(
        Path(str(foreign_request) + ".keys"),
        {"version": 1, **market_cli._generate_key_bundle()},
    )
    wrong_delivery = tmp_path / "wrong-delivery.json"
    assert (
        market_cli.run(
            [
                "poll",
                "--data-dir",
                str(tmp_path),
                "--quote",
                str(quote_file),
                "--seller",
                str(listing_file),
                "--request-file",
                str(foreign_request),
                "--raw-output",
                str(wrong_delivery),
            ]
        )
        == 1
    )
    assert not wrong_delivery.exists()
    service_store.close()


def _lightning_invoice(now: int, preimage: bytes) -> str:
    invoice = Bolt11(
        currency="bcrt",
        date=now - 1,
        amount_msat=MilliSatoshi(100_000),
        tags=Tags(
            [
                Tag(TagChar.payment_hash, hashlib.sha256(preimage).hexdigest()),
                Tag(TagChar.payment_secret, "22" * 32),
                Tag(TagChar.description, "offline CLI test"),
                Tag(TagChar.expire_time, 600),
                Tag(TagChar.min_final_cltv_expiry, 18),
            ]
        ),
    )
    return encode(invoice, private_key="11" * 32)


def test_lightning_settlement_requires_explicit_acknowledgement(
    tmp_path: Path, monkeypatch
) -> None:
    now = int(time.time())
    settings = _settings(tmp_path)
    monkeypatch.setattr(market_cli, "_settings", lambda _args: settings)
    owner, seller, buyer = _key(0x31), _key(0x32), PrivateKey(bytes([0x33]) * 32)
    bond = _bond(owner)
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bytes(seller.pub).hex()), owner
    )
    proof = generate_podle(b"\x34" * 32, f"{'34' * 32}:1", 0)
    podle = {
        "version": 1,
        "network": "regtest",
        "outpoint": {"txid": "34" * 32, "vout": 1},
        "P": proof.p.hex(),
        "P2": proof.p2.hex(),
        "sig": proof.sig.hex(),
        "e": proof.e.hex(),
        "commitment": proof.commitment.hex(),
        "index": 0,
        "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
        "blockheight": 1,
    }
    preimage = bytes(range(32))
    terms = PaymentTerms(
        rail="lightning",
        request=_lightning_invoice(now, preimage),
        amount_sats=100,
    )
    store = MarketStore(tmp_path / "market" / "seller.sqlite")
    store.add_inventory("podle", proof.commitment.hex(), podle)
    store.add_payment(terms, "ln:test-payment")
    quote = store.create_quote(
        authorization,
        seller,
        bytes(buyer.public_key).hex(),
        "podle",
        None,
        "lightning",
        100,
        now,
        1,
        request_id="01" * 16,
    )
    store.attach_credential(quote.body["quote_id"], podle)
    store.close()

    keys = tmp_path / "seller-keys.json"
    _json(
        keys,
        {
            "version": 1,
            "seller_signing_key": seller.secret_bytes.hex(),
            "encryption_key": bytes(PrivateKey.generate()).hex(),
            "renter_certificate_key": _key(0x35).secret_bytes.hex(),
        },
    )
    preimage_file = tmp_path / "preimage"
    _write(preimage_file, preimage)
    monkeypatch.setattr(market_cli, "_new_backend", lambda _settings: _Backend())
    assert (
        market_cli.run(
            [
                "seller",
                "settle",
                "--data-dir",
                str(tmp_path),
                "--quote-id",
                quote.body["quote_id"],
                "--keys",
                str(keys),
                "--preimage-file",
                str(preimage_file),
                "--output",
                str(tmp_path / "package.json"),
            ]
        )
        == 1
    )


def test_removed_compatibility_flags_are_rejected(tmp_path: Path) -> None:
    """The unpublished compatibility flags must not be silently accepted again."""
    parser = market_cli.build_parser()
    request = [
        "request",
        "--listing",
        str(tmp_path / "listing.json"),
        "--request-file",
        str(tmp_path / "buy.json"),
        "--max-price-sats",
        "100",
        "--require-experimental-risk-ack",
    ]
    poll = [
        "poll",
        "--seller",
        str(tmp_path / "listing.json"),
        "--request-file",
        str(tmp_path / "buy.json"),
    ]
    settle = [
        "seller",
        "settle",
        "--quote-id",
        "01" * 16,
        "--keys",
        str(tmp_path / "seller-keys.json"),
        "--output",
        str(tmp_path / "package.json"),
    ]
    removed = [
        [*request, "--keys", str(tmp_path / "buyer-keys.json")],
        [*request, "--rail", "onchain"],
        [*poll, "--keys", str(tmp_path / "buyer-keys.json")],
        [*poll, "--buyer-key", str(tmp_path / "buyer.key")],
        ["poll", "--seller-info", str(tmp_path / "listing.json")],
        [*settle, "--onchain-outpoint", f"{'ee' * 32}:0"],
    ]
    for argv in removed:
        with pytest.raises(SystemExit) as failure:
            parser.parse_args(argv)
        assert failure.value.code == 2


def test_poll_and_settle_require_their_remaining_arguments(tmp_path: Path) -> None:
    """poll is request-file bound and settle is preimage bound; neither is optional."""
    parser = market_cli.build_parser()
    for argv in (
        ["poll", "--seller", str(tmp_path / "listing.json")],
        [
            "seller",
            "settle",
            "--quote-id",
            "01" * 16,
            "--keys",
            str(tmp_path / "seller-keys.json"),
            "--output",
            str(tmp_path / "package.json"),
        ],
    ):
        with pytest.raises(SystemExit) as failure:
            parser.parse_args(argv)
        assert failure.value.code == 2

    settle = parser.parse_args(
        [
            "seller",
            "settle",
            "--quote-id",
            "01" * 16,
            "--keys",
            str(tmp_path / "seller-keys.json"),
            "--preimage-file",
            str(tmp_path / "preimage"),
            "--output",
            str(tmp_path / "package.json"),
        ]
    )
    assert settle.preimage_file == tmp_path / "preimage"
    assert settle.acknowledge_ln_settlement is False


def test_serve_refuses_a_quote_ttl_beyond_the_payment_window(tmp_path: Path) -> None:
    """A served quote may not outlive the fifteen-minute payment TTL ceiling."""
    argv = [
        "serve",
        "--authorization",
        str(tmp_path / "authorization.json"),
        "--keys",
        str(tmp_path / "seller-keys.json"),
        "--products",
        "podle",
        "--price-sats",
        "100",
        "--quote-ttl",
    ]
    assert market_cli.run([*argv, "901"]) == 1
    assert market_cli.run([*argv, "0"]) == 1


def test_injected_rail_in_a_persisted_request_cannot_issue_a_quote(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Hand-editing the rail of a persisted request fails before anything is sent."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(market_cli, "_settings", lambda _args: settings)

    def _refusing_transport(_settings: JoinMarketSettings, **_kwargs: object) -> None:
        raise AssertionError("an invalid request must not open a transport")

    monkeypatch.setattr(market_cli, "_new_transport", _refusing_transport)

    seller = _key(0x46)
    listing = MarketListing(
        network="regtest",
        period=0,
        seller_pubkey=bytes(seller.pub).hex(),
        encryption_pubkey=bytes(PrivateKey.generate().public_key).hex(),
        products=["podle"],
        price_sats=100,
        expires_at=int(time.time()) + 60,
    )
    listing_file = tmp_path / "listing.json"
    _json(
        listing_file,
        {
            "seller_nick": "J5ABCDEFGHJKLMNP",
            "listing": sign_document(listing, seller).model_dump(mode="json"),
        },
    )
    buyer_bundle = market_cli._generate_key_bundle()
    request_file = tmp_path / "buy.json"
    _json(Path(str(request_file) + ".keys"), {"version": 1, **buyer_bundle})
    buyer_pubkey = bytes(PrivateKey(bytes.fromhex(buyer_bundle["encryption_key"])).public_key).hex()
    _json(
        request_file,
        {
            "version": 2,
            "seller": {
                "seller_pubkey": listing.seller_pubkey,
                "network": listing.network,
                "encryption_pubkey": listing.encryption_pubkey,
            },
            "request": {
                "action": "quote",
                "request_id": "01" * 16,
                "buyer_pubkey": buyer_pubkey,
                "product": "podle",
                "certificate_pubkey": None,
                "rail": "onchain",
                "max_price_sats": 100,
            },
        },
    )
    quote_file = tmp_path / "quote.json"
    assert (
        market_cli.run(
            [
                "request",
                "--data-dir",
                str(tmp_path),
                "--listing",
                str(listing_file),
                "--request-file",
                str(request_file),
                "--max-price-sats",
                "100",
                "--output",
                str(quote_file),
                "--require-experimental-risk-ack",
            ]
        )
        == 1
    )
    assert "does not match this command" in capsys.readouterr().err
    assert not quote_file.exists()


def test_key_output_and_errors_never_echo_private_key(tmp_path: Path, capsys) -> None:
    keys = tmp_path / "keys.json"
    assert market_cli.run(["keygen", "--output", str(keys)]) == 0
    captured = capsys.readouterr()
    bundle = json.loads(keys.read_text(encoding="ascii"))
    assert captured.out == ""
    assert bundle["seller_signing_key"] not in captured.out
    assert keys.stat().st_mode & 0o777 == 0o600

    secret = _key(0x41).secret_bytes.hex()
    owner_file = tmp_path / "redaction.key"
    _write(owner_file, secret.encode("ascii"))
    seller_public = tmp_path / "seller.pub"
    _write(seller_public, bytes(_key(0x42).pub).hex().encode("ascii"))
    bond_ref = tmp_path / "bad-owner-bond.json"
    _json(bond_ref, _bond(_key(0x43)).model_dump(mode="json"))
    assert (
        market_cli.run(
            [
                "authorize",
                "--bond-ref",
                str(bond_ref),
                "--seller-pub",
                str(seller_public),
                "--owner-key",
                str(owner_file),
                "--period",
                "0",
                "--output",
                str(tmp_path / "authorization.json"),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_request_requires_experimental_seller_risk_ack(tmp_path: Path) -> None:
    assert (
        market_cli.run(
            [
                "request",
                "--listing",
                str(tmp_path / "listing.json"),
                "--product",
                "podle",
                "--max-price-sats",
                "1",
                "--request-file",
                str(tmp_path / "request.json"),
                "--output",
                str(tmp_path / "quote.json"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "request.json").exists()


def test_discovery_writes_transport_bounded_listing_set(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(market_cli, "_settings", lambda _args: settings)
    listing: dict[str, object] = {
        "seller_nick": "J5ABCDEFGHJKLMNP",
        "listing": {"padding": "x" * 2000},
    }

    async def large_discovery(
        _args: argparse.Namespace, _settings: JoinMarketSettings
    ) -> list[dict[str, object]]:
        return [listing] * 9

    monkeypatch.setattr(market_cli, "_discover", large_discovery)
    output = tmp_path / "listings.json"
    assert market_cli.run(["discover", "--output", str(output)]) == 0
    assert output.stat().st_size > 16_384
    assert output.stat().st_mode & 0o777 == 0o600
    assert len(json.loads(output.read_text(encoding="ascii"))["listings"]) == 9


def _bond_package(
    authorization: SignedDocument, seller: CKey, certificate: CKey, owner: CKey
) -> CredentialPackage:
    credential = market_cli.make_bond_credential(authorization, bytes(certificate.pub).hex(), owner)
    authority = authorization.verified(MarketAuthorization, bytes(owner.pub).hex())
    allocation = sign_document(
        Allocation(
            authorization=document_hash(authorization.body),
            allocation_id="71" * 32,
            buyer_tag="72" * 32,
            product="bond",
            resource=bond_resource(authority.bond, authority.period),
            certificate_pubkey=credential.cert_pubkey,
        ),
        seller,
    )
    delivery = sign_document(
        Delivery(
            allocation=document_hash(allocation.body),
            credential=credential.model_dump(mode="json"),
        ),
        seller,
    )
    return CredentialPackage(
        authorization=authorization,
        allocation=allocation,
        delivery=delivery,
    )


def test_proof_build_uses_structural_invalid_delivery_package(tmp_path: Path) -> None:
    owner, seller, certificate = _key(0x59), _key(0x5A), _key(0x5B)
    authorization = sign_document(
        MarketAuthorization(bond=_bond(owner), period=0, seller_pubkey=bytes(seller.pub).hex()),
        owner,
    )
    package = _bond_package(authorization, seller, certificate, owner)
    invalid_delivery = sign_document(
        Delivery(
            allocation=document_hash(package.allocation.body),
            credential={"version": 1},
        ),
        seller,
    )
    raw_package = CredentialPackage(
        authorization=package.authorization,
        allocation=package.allocation,
        delivery=invalid_delivery,
    )
    raw_file = tmp_path / "invalid-delivery.json"
    _json(raw_file, raw_package.model_dump(mode="json"))
    proof_file = tmp_path / "proof.json"
    assert (
        market_cli.run(
            [
                "proof",
                "build",
                "--first-package",
                str(raw_file),
                "--output",
                str(proof_file),
            ]
        )
        == 0
    )
    proof = FaultProof.model_validate(market_cli._document(proof_file))
    assert proof.reason == "invalid-delivery"
    proof.verify()


def test_import_existing_bond_registry_conflict_is_not_overwritten(
    tmp_path: Path, monkeypatch
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(market_cli, "_settings", lambda _args: settings)
    monkeypatch.setattr(market_cli, "_new_backend", lambda _settings: _Backend())
    owner, certificate, other_certificate = _key(0x61), _key(0x62), _key(0x63)
    seller = _key(0x64)
    bond = _bond(owner)
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bytes(seller.pub).hex()), owner
    )
    package = _bond_package(authorization, seller, certificate, owner)
    package_file = tmp_path / "package.json"
    _json(package_file, package.model_dump(mode="json"))
    certificate_file = tmp_path / "certificate.key"
    _write(certificate_file, certificate.secret_bytes.hex().encode("ascii"))
    assert (
        market_cli.run(
            [
                "import",
                "--data-dir",
                str(tmp_path),
                "--package",
                str(package_file),
                "--certificate-key",
                str(certificate_file),
                "--wallet-fingerprint",
                "deadbeef",
            ]
        )
        == 0
    )
    from jmwallet.wallet.bond_registry import load_registry, save_registry
    from jmwallet.wallet.service import WalletService
    from maker.fidelity import find_fidelity_bonds

    stored = load_registry(tmp_path, "deadbeef", allow_legacy_fallback=False, fail_closed=True)
    assert len(stored.bonds) == 1
    assert stored.bonds[0].index == -1
    assert stored.bonds[0].path == "external"
    assert stored.bonds[0].txid is None
    maker_wallet = SimpleNamespace(
        data_dir=tmp_path,
        wallet_fingerprint="deadbeef",
        utxo_cache={
            0: [
                SimpleNamespace(
                    path=f"m/84'/1'/0'/2/-1:{bond.locktime}",
                    value=1_000_000,
                    height=1,
                    address=stored.bonds[0].address,
                    txid=bond.outpoint.txid,
                    vout=bond.outpoint.vout,
                )
            ]
        },
        backend=_Backend(),
    )
    maker_bonds = asyncio.run(find_fidelity_bonds(cast(WalletService, maker_wallet)))
    assert len(maker_bonds) == 1
    assert maker_bonds[0].txid == bond.outpoint.txid
    assert maker_bonds[0].cert_pubkey == bytes(certificate.pub)
    assert maker_bonds[0].cert_privkey is not None

    conflicting = _bond_package(authorization, seller, other_certificate, owner)
    conflicting_file = tmp_path / "conflicting-package.json"
    _json(conflicting_file, conflicting.model_dump(mode="json"))
    other_certificate_file = tmp_path / "other-certificate.key"
    _write(other_certificate_file, other_certificate.secret_bytes.hex().encode("ascii"))
    assert (
        market_cli.run(
            [
                "import",
                "--data-dir",
                str(tmp_path),
                "--package",
                str(conflicting_file),
                "--certificate-key",
                str(other_certificate_file),
                "--wallet-fingerprint",
                "deadbeef",
            ]
        )
        == 1
    )

    stored.bonds[0].txid = bond.outpoint.txid
    stored.bonds[0].vout = bond.outpoint.vout
    save_registry(stored, tmp_path, "deadbeef")
    conflicting_bond = bond.model_copy(
        update={"outpoint": ExternalPoDLEOutpoint(txid="bb" * 32, vout=2)}
    )
    conflicting_authorization = sign_document(
        MarketAuthorization(
            bond=conflicting_bond,
            period=0,
            seller_pubkey=bytes(seller.pub).hex(),
        ),
        owner,
    )
    conflicting_outpoint = _bond_package(conflicting_authorization, seller, certificate, owner)
    conflicting_outpoint_file = tmp_path / "conflicting-outpoint-package.json"
    _json(conflicting_outpoint_file, conflicting_outpoint.model_dump(mode="json"))
    assert (
        market_cli.run(
            [
                "import",
                "--data-dir",
                str(tmp_path),
                "--package",
                str(conflicting_outpoint_file),
                "--certificate-key",
                str(certificate_file),
                "--wallet-fingerprint",
                "deadbeef",
            ]
        )
        == 1
    )

    stored = load_registry(tmp_path, "deadbeef", allow_legacy_fallback=False, fail_closed=True)
    assert len(stored.bonds) == 1
    assert stored.bonds[0].index == -1
    assert stored.bonds[0].path == "external"
    assert stored.bonds[0].cert_pubkey == bytes(certificate.pub).hex()
    assert stored.bonds[0].txid == bond.outpoint.txid
    assert stored.bonds[0].vout == bond.outpoint.vout


def _buyer_bound_bond_package(
    authorization: SignedDocument,
    seller: CKey,
    certificate: CKey,
    owner: CKey,
    buyer_key: PrivateKey,
    now: int,
) -> tuple[CredentialPackage, SignedDocument]:
    """Build a delivered package plus the signed quote it settles, bound to one buyer key."""
    credential = market_cli.make_bond_credential(authorization, bytes(certificate.pub).hex(), owner)
    authority = authorization.verified(MarketAuthorization, bytes(owner.pub).hex())
    quote_id = "73" * 32
    resource = bond_resource(authority.bond, authority.period)
    allocation = sign_document(
        Allocation(
            authorization=document_hash(authorization.body),
            allocation_id=quote_id,
            buyer_tag=hashlib.sha256(bytes(buyer_key.public_key)).hexdigest(),
            product="bond",
            resource=resource,
            certificate_pubkey=credential.cert_pubkey,
        ),
        seller,
    )
    delivery = sign_document(
        Delivery(
            allocation=document_hash(allocation.body),
            credential=credential.model_dump(mode="json"),
        ),
        seller,
    )
    quote = sign_document(
        MarketQuote(
            authorization=authorization,
            quote_id=quote_id,
            buyer_pubkey=bytes(buyer_key.public_key).hex(),
            product="bond",
            resource=resource,
            certificate_pubkey=credential.cert_pubkey,
            payment=_payment_terms(),
            created_at=now,
            expires_at=now + 300,
        ),
        seller,
    )
    package = CredentialPackage(
        authorization=authorization, allocation=allocation, delivery=delivery
    )
    return package, quote


def test_import_quote_binding_blocks_mismatched_delivery(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """With --quote, a package delivered for another buyer's quote is rejected."""
    now = int(time.time())
    settings = _settings(tmp_path)
    monkeypatch.setattr(market_cli, "_settings", lambda _args: settings)
    monkeypatch.setattr(market_cli, "_new_backend", lambda _settings: _Backend())
    owner, seller, certificate = _key(0x65), _key(0x66), _key(0x67)
    buyer_key = PrivateKey.generate()
    authorization = sign_document(
        MarketAuthorization(bond=_bond(owner), period=0, seller_pubkey=bytes(seller.pub).hex()),
        owner,
    )
    package, quote = _buyer_bound_bond_package(
        authorization, seller, certificate, owner, buyer_key, now
    )
    package_file = tmp_path / "package.json"
    _json(package_file, package.model_dump(mode="json"))
    quote_file = tmp_path / "quote.json"
    _json(quote_file, quote.model_dump(mode="json"))
    certificate_file = tmp_path / "certificate.key"
    _write(certificate_file, certificate.secret_bytes.hex().encode("ascii"))

    _, other_quote = _buyer_bound_bond_package(
        authorization, seller, certificate, owner, PrivateKey.generate(), now
    )
    other_quote_file = tmp_path / "other-quote.json"
    _json(other_quote_file, other_quote.model_dump(mode="json"))
    assert (
        market_cli.run(
            [
                "import",
                "--data-dir",
                str(tmp_path),
                "--package",
                str(package_file),
                "--quote",
                str(other_quote_file),
                "--certificate-key",
                str(certificate_file),
                "--wallet-fingerprint",
                "deadbeef",
            ]
        )
        == 1
    )
    assert "does not match the purchased quote" in capsys.readouterr().err

    assert (
        market_cli.run(
            [
                "import",
                "--data-dir",
                str(tmp_path),
                "--package",
                str(package_file),
                "--quote",
                str(quote_file),
                "--certificate-key",
                str(certificate_file),
                "--wallet-fingerprint",
                "deadbeef",
            ]
        )
        == 0
    )


def test_import_rejects_bond_with_persisted_fault_evidence(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A bond package whose collateral has promoted fault evidence must not import."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(market_cli, "_settings", lambda _args: settings)
    monkeypatch.setattr(market_cli, "_new_backend", lambda _settings: _Backend())
    owner, seller, certificate = _key(0x68), _key(0x69), _key(0x6A)
    bond = _bond(owner)
    authorization = sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bytes(seller.pub).hex()), owner
    )
    package = _bond_package(authorization, seller, certificate, owner)
    package_file = tmp_path / "package.json"
    _json(package_file, package.model_dump(mode="json"))
    certificate_file = tmp_path / "certificate.key"
    _write(certificate_file, certificate.secret_bytes.hex().encode("ascii"))

    invalid_delivery = sign_document(
        Delivery(allocation=document_hash(package.allocation.body), credential={"version": 1}),
        seller,
    )
    proof = FaultProof(
        reason="invalid-delivery",
        authorization=authorization,
        first=package.allocation,
        second=invalid_delivery,
    )
    cache = MarketFaultCache(tmp_path)
    assert cache.ingest(canonical(proof))
    assert cache.excludes_verified_bond(bond, height=1)

    assert (
        market_cli.run(
            [
                "import",
                "--data-dir",
                str(tmp_path),
                "--package",
                str(package_file),
                "--certificate-key",
                str(certificate_file),
                "--wallet-fingerprint",
                "deadbeef",
            ]
        )
        == 1
    )
    assert "fault evidence" in capsys.readouterr().err
    from jmwallet.wallet.bond_registry import load_registry

    assert not load_registry(
        tmp_path, "deadbeef", allow_legacy_fallback=False, fail_closed=True
    ).bonds


def test_bare_invocation_prints_full_help_and_succeeds(capsys) -> None:
    """``jm-market`` with no arguments must behave like the other module entry points."""
    assert market_cli.run([]) == 0
    captured = capsys.readouterr()

    assert captured.err == ""
    assert captured.out.startswith("usage: jm-market")
    # The command list is the point of a bare invocation, so it must be present.
    for command in ("discover", "request", "poll", "import", "seller", "serve", "proof"):
        assert command in captured.out


def test_entry_help_explains_the_purchase_flow_and_points_at_the_overview() -> None:
    """The top-level description must orient a new user before they pick a command."""
    help_text = market_cli.build_parser().format_help()

    assert "docs/credential-market-overview.md" in help_text
    assert "Lightning" in help_text
    # Buyer and seller roles must both be named.
    assert "Buying a credential:" in help_text
    assert "Selling a credential:" in help_text
    # The buyer steps must appear in the order they are performed.
    positions = [help_text.index(step) for step in ("discover", "request", "poll", "import")]
    assert positions == sorted(positions)
    # The payment is explicitly outside this tool.
    assert "never sends a payment" in help_text


def test_bare_seller_and_proof_print_their_own_help(capsys) -> None:
    """Group commands without a subcommand show their own help instead of failing."""
    assert market_cli.run(["seller"]) == 0
    seller_out = capsys.readouterr().out
    assert seller_out.startswith("usage: jm-market seller")
    assert "add-inventory" in seller_out and "settle" in seller_out

    assert market_cli.run(["proof"]) == 0
    proof_out = capsys.readouterr().out
    assert proof_out.startswith("usage: jm-market proof")
    assert "build" in proof_out and "broadcast" in proof_out


def test_help_default_does_not_shadow_command_handlers() -> None:
    """Every subcommand must keep its own handler despite the parser-level help default."""
    parser = market_cli.build_parser()
    expected = {
        ("keygen", "--output", "keys.json"): market_cli._command_keygen,
        ("discover",): market_cli._command_discover,
        ("seller", "pending"): market_cli._command_seller_pending,
        ("seller", "export", "--quote-id", "q", "--output", "o"): market_cli._command_seller_export,
        ("proof", "broadcast", "--proof", "p"): market_cli._command_proof_broadcast,
    }
    for argv, handler in expected.items():
        assert parser.parse_args(list(argv)).handler is handler


def test_unknown_command_still_fails(capsys) -> None:
    """Relaxing the required subcommand must not accept a bogus command."""
    with pytest.raises(SystemExit) as failure:
        market_cli.build_parser().parse_args(["not-a-command"])
    assert failure.value.code == 2
