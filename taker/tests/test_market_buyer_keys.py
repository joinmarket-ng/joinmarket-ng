"""Buyer key hygiene for the native market CLI.

Every purchase must present a buyer pubkey that cannot be correlated with any
other purchase, while a retry of the same persisted request must keep using the
key the seller already quoted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from bitcointx.core.key import CKey  # type: ignore[import-not-found]
from jmcore.bitcoin import hash160
from jmcore.credential_market import (
    BondReference,
    MarketAuthorization,
    PaymentTerms,
    canonical,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_store import MarketStore
from jmcore.models import NetworkType
from jmcore.podle import generate_podle
from jmcore.settings import JoinMarketSettings, NetworkSettings
from jmwallet.backends.base import BlockchainBackend, BondVerificationRequest
from nacl.public import PrivateKey

from taker import market_cli
from taker._vendor.bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, encode
from taker.market_service import MarketService


class _Backend:
    """Chain authority for collateral only; Lightning payments never reach it."""

    async def get_block_height(self) -> int:
        return 1

    async def get_block_time(self, _height: int) -> int:
        return int(time.time()) - 3600

    async def verify_bonds(self, bonds: list[BondVerificationRequest]) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                valid=True, txid=bond.txid, vout=bond.vout, confirmations=6, value=1000000
            )
            for bond in bonds
        ]

    async def get_utxo(self, _txid: str, _vout: int) -> SimpleNamespace | None:
        return None

    def requires_neutrino_metadata(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _LoopbackTransport:
    def __init__(self, service: MarketService) -> None:
        self.service = service

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
        return await self.service.respond(sender, payload)


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


def _preimage(tag: int) -> bytes:
    return bytes([tag]) * 32


def _payment_terms(tag: int) -> PaymentTerms:
    """One distinct Lightning invoice per queued payment, settled by _preimage(tag)."""
    now = int(time.time())
    invoice = Bolt11(
        currency="bcrt",
        date=now - 1,
        amount_msat=MilliSatoshi(100_000),
        tags=Tags(
            [
                Tag(TagChar.payment_hash, hashlib.sha256(_preimage(tag)).hexdigest()),
                Tag(TagChar.payment_secret, "22" * 32),
                Tag(TagChar.description, "buyer key test"),
                Tag(TagChar.expire_time, 3_600),
                Tag(TagChar.min_final_cltv_expiry, 18),
            ]
        ),
    )
    return PaymentTerms(
        rail="lightning",
        request=encode(invoice, private_key="11" * 32),
        amount_sats=100,
    )


def _podle_record(owner: CKey, txid: str, vout: int) -> dict[str, object]:
    proof = generate_podle(owner.secret_bytes, f"{txid}:{vout}", 0)
    return {
        "version": 1,
        "network": "regtest",
        "outpoint": {"txid": txid, "vout": vout},
        "P": proof.p.hex(),
        "P2": proof.p2.hex(),
        "sig": proof.sig.hex(),
        "e": proof.e.hex(),
        "commitment": proof.commitment.hex(),
        "index": 0,
        "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
        "blockheight": 1,
    }


class _Market:
    """One loopback seller plus the buyer files the CLI needs to talk to it."""

    def __init__(self, tmp_path: Path, monkeypatch, *, products: list[str], payments: int) -> None:
        self.tmp_path = tmp_path
        self.settings = JoinMarketSettings(
            data_dir=tmp_path,
            network_config=NetworkSettings(network=NetworkType.REGTEST, directory_servers=[]),
        )
        monkeypatch.setattr(market_cli, "_settings", lambda _args: self.settings)

        bundle = market_cli._generate_key_bundle()
        self.seller_keys_file = tmp_path / "seller-keys.json"
        _write(self.seller_keys_file, canonical({"version": 1, **bundle}))
        seller_key = CKey(bytes.fromhex(bundle["seller_signing_key"]))
        owner = _key(0x51)
        bond = BondReference(
            network="regtest",
            outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=1),
            pubkey=bytes(owner.pub).hex(),
            locktime=int(time.time()) + 86_400,
        )
        authorization = sign_document(
            MarketAuthorization(bond=bond, period=0, seller_pubkey=bytes(seller_key.pub).hex()),
            owner,
        )
        self.authorization_file = tmp_path / "authorization.json"
        _write(self.authorization_file, canonical(authorization))

        self.terms = [_payment_terms(0x40 + index) for index in range(payments)]
        self.backend = _Backend()
        store = MarketStore(tmp_path / "market" / "seller.sqlite")
        for index, terms in enumerate(self.terms):
            now = int(time.time())
            from taker.market_payments import validate_payment_terms

            store.add_payment(terms, validate_payment_terms(terms, "regtest", now, now + 600))
        if "podle" in products:
            for index in range(2):
                record = _podle_record(_key(0x61 + index), str(index + 11) * 32, index)
                store.add_inventory("podle", cast(str, record["commitment"]), record)
        if "bond" in products:
            from jmcore.credential_market import bond_resource

            store.add_inventory("bond", bond_resource(bond, 0), None)
        self.store = store
        self.service = MarketService(
            store,
            authorization,
            seller_key,
            PrivateKey(bytes.fromhex(bundle["encryption_key"])),
            cast(BlockchainBackend, self.backend),
            products=cast(list, products),
            price_sats=100,
        )
        self.listing_file = tmp_path / "listing.json"
        self._refresh_listing()
        monkeypatch.setattr(market_cli, "_new_backend", lambda _settings: self.backend)
        monkeypatch.setattr(
            market_cli,
            "_new_transport",
            lambda _settings, **_kwargs: _LoopbackTransport(self.service),
        )

    def _refresh_listing(self) -> None:
        listing = asyncio.run(self.service.listing())
        self.listing_file.unlink(missing_ok=True)
        _write(
            self.listing_file,
            canonical(
                {
                    "seller_nick": "J5ABCDEFGHJKLMNP",
                    "listing": json.loads(listing.decode("ascii")),
                }
            ),
        )

    def request(self, name: str, *, product: str = "podle") -> tuple[int, Path, Path]:
        """Run one buyer request, returning its exit code, request and quote paths."""
        self.service._next_quote = 0
        request_file = self.tmp_path / f"{name}-request.json"
        quote_file = self.tmp_path / f"{name}-quote.json"
        argv = [
            "request",
            "--data-dir",
            str(self.tmp_path),
            "--listing",
            str(self.listing_file),
            "--product",
            product,
            "--max-price-sats",
            "100",
            "--request-file",
            str(request_file),
            "--output",
            str(quote_file),
            "--require-experimental-risk-ack",
        ]
        return market_cli.run(argv), request_file, quote_file

    def close(self) -> None:
        self.store.close()


def _buyer_pubkey(request_file: Path) -> str:
    return cast(
        str, json.loads(request_file.read_text(encoding="ascii"))["request"]["buyer_pubkey"]
    )


def _bundle_pubkey(path: Path) -> str:
    bundle = json.loads(path.read_text(encoding="ascii"))
    return bytes(PrivateKey(bytes.fromhex(bundle["encryption_key"])).public_key).hex()


def test_default_request_uses_a_fresh_buyer_key_per_request(tmp_path: Path, monkeypatch) -> None:
    """Two purchases must not share a buyer pubkey when no key bundle is supplied."""
    market = _Market(tmp_path, monkeypatch, products=["podle"], payments=2)
    try:
        first_code, first_request, _ = market.request("first")
        second_code, second_request, _ = market.request("second")
    finally:
        market.close()
    assert (first_code, second_code) == (0, 0)

    first_keys = Path(str(first_request) + ".keys")
    second_keys = Path(str(second_request) + ".keys")
    assert first_keys.stat().st_mode & 0o777 == 0o600
    assert second_keys.stat().st_mode & 0o777 == 0o600
    # Every secret of the second purchase is independent of the first.
    first_bundle = json.loads(first_keys.read_text(encoding="ascii"))
    second_bundle = json.loads(second_keys.read_text(encoding="ascii"))
    assert set(first_bundle) == set(second_bundle)
    for field in ("seller_signing_key", "encryption_key", "renter_certificate_key"):
        assert first_bundle[field] != second_bundle[field]
    assert _buyer_pubkey(first_request) != _buyer_pubkey(second_request)
    assert _buyer_pubkey(first_request) == _bundle_pubkey(first_keys)
    assert _buyer_pubkey(second_request) == _bundle_pubkey(second_keys)


def test_default_request_retry_reuses_the_persisted_buyer_key(tmp_path: Path, monkeypatch) -> None:
    """A retry must present the key the seller already quoted, not a rotated one."""
    market = _Market(tmp_path, monkeypatch, products=["podle"], payments=2)
    try:
        code, request_file, quote_file = market.request("retry")
        assert code == 0
        keys_file = Path(str(request_file) + ".keys")
        original_bundle = keys_file.read_bytes()
        original_request = request_file.read_bytes()
        quote = json.loads(quote_file.read_text(encoding="ascii"))["quote"]

        market.service._next_quote = 0
        retry_quote = tmp_path / "retry-again-quote.json"
        argv = [
            "request",
            "--data-dir",
            str(tmp_path),
            "--listing",
            str(market.listing_file),
            "--product",
            "podle",
            "--max-price-sats",
            "100",
            "--request-file",
            str(request_file),
            "--output",
            str(retry_quote),
            "--require-experimental-risk-ack",
        ]
        assert market_cli.run(argv) == 0
    finally:
        market.close()
    assert keys_file.read_bytes() == original_bundle
    assert request_file.read_bytes() == original_request
    # The seller returns the same signed quote for the same request identity.
    assert json.loads(retry_quote.read_text(encoding="ascii"))["quote"] == quote


def test_request_fails_closed_when_the_persisted_bundle_is_missing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A persisted request without its key must never silently rotate to a new key."""
    market = _Market(tmp_path, monkeypatch, products=["podle"], payments=2)
    try:
        code, request_file, _ = market.request("lost")
        assert code == 0
        keys_file = Path(str(request_file) + ".keys")
        keys_file.unlink()
        capsys.readouterr()

        market.service._next_quote = 0
        retry_code, _, _ = market.request("lost")
    finally:
        market.close()
    assert retry_code == 1
    assert "buyer key bundle" in capsys.readouterr().err
    assert not keys_file.exists()


def test_request_fails_closed_on_a_corrupt_bundle(tmp_path: Path, monkeypatch, capsys) -> None:
    """Unreadable key state is an error, not a reason to generate replacement keys."""
    market = _Market(tmp_path, monkeypatch, products=["podle"], payments=2)
    try:
        code, request_file, _ = market.request("corrupt")
        assert code == 0
        keys_file = Path(str(request_file) + ".keys")
        _write(keys_file, b"{not json")
        capsys.readouterr()

        market.service._next_quote = 0
        retry_code, _, _ = market.request("corrupt")
    finally:
        market.close()
    assert retry_code == 1
    assert "key bundle is invalid" in capsys.readouterr().err
    assert keys_file.read_bytes() == b"{not json"


def test_request_has_no_way_to_reuse_one_buyer_identity(tmp_path: Path) -> None:
    """There is no flag that makes two purchases share a buyer key bundle."""
    parser = market_cli.build_parser()
    argv = [
        "request",
        "--listing",
        str(tmp_path / "listing.json"),
        "--request-file",
        str(tmp_path / "buy.json"),
        "--max-price-sats",
        "100",
        "--require-experimental-risk-ack",
    ]
    assert parser.parse_args(argv).request_file == tmp_path / "buy.json"
    with pytest.raises(SystemExit) as failure:
        parser.parse_args([*argv, "--keys", str(tmp_path / "buyer-keys.json")])
    assert failure.value.code == 2


def test_bond_request_uses_the_generated_renter_certificate_key(
    tmp_path: Path, monkeypatch
) -> None:
    """The implicit bundle must also supply a per-request renter certificate key."""
    market = _Market(tmp_path, monkeypatch, products=["podle", "bond"], payments=1)
    try:
        code, request_file, quote_file = market.request("bond", product="bond")
    finally:
        market.close()
    assert code == 0

    bundle = json.loads(Path(str(request_file) + ".keys").read_text(encoding="ascii"))
    expected = bytes(CKey(bytes.fromhex(bundle["renter_certificate_key"])).pub).hex()
    request = json.loads(request_file.read_text(encoding="ascii"))["request"]
    assert request["certificate_pubkey"] == expected
    quote = json.loads(quote_file.read_text(encoding="ascii"))["quote"]
    assert quote["body"]["certificate_pubkey"] == expected


def test_poll_opens_the_delivery_with_the_request_file_bundle(tmp_path: Path, monkeypatch) -> None:
    """Delivery recovery only needs the request file the buyer already keeps."""
    market = _Market(tmp_path, monkeypatch, products=["podle"], payments=1)
    try:
        code, request_file, quote_file = market.request("poll")
        assert code == 0
        quote_id = json.loads(quote_file.read_text(encoding="ascii"))["quote"]["body"]["quote_id"]
        credential = tmp_path / "podle.json"
        _write(credential, canonical(_podle_record(_key(0x61), "11" * 32, 0)))
        market.store.attach_credential(quote_id, json.loads(credential.read_text("ascii")))
        package = tmp_path / "package.json"
        preimage_file = tmp_path / "preimage"
        _write(preimage_file, _preimage(0x40))
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
                    str(market.seller_keys_file),
                    "--preimage-file",
                    str(preimage_file),
                    "--acknowledge-ln-settlement",
                    "--output",
                    str(package),
                ]
            )
            == 0
        )

        delivery = tmp_path / "delivery.json"
        assert (
            market_cli.run(
                [
                    "poll",
                    "--data-dir",
                    str(tmp_path),
                    "--quote",
                    str(quote_file),
                    "--seller",
                    str(market.listing_file),
                    "--request-file",
                    str(request_file),
                    "--raw-output",
                    str(delivery),
                ]
            )
            == 0
        )
        market_cli._credential_package(delivery)

        # Another request's bundle must not open this delivery.
        other_code, other_request, _ = market.request("poll-other")
        foreign = tmp_path / "foreign-delivery.json"
        assert (
            market_cli.run(
                [
                    "poll",
                    "--data-dir",
                    str(tmp_path),
                    "--quote",
                    str(quote_file),
                    "--seller",
                    str(market.listing_file),
                    "--request-file",
                    str(other_request),
                    "--raw-output",
                    str(foreign),
                ]
            )
            == 1
        )
        assert not foreign.exists()
    finally:
        market.close()


def test_poll_only_accepts_the_request_file_bundle() -> None:
    """The delivery key comes from the request file alone; no other source exists."""
    parser = market_cli.build_parser()
    common = ["poll", "--quote", "q", "--seller", "s", "--raw-output", "r"]
    assert parser.parse_args([*common, "--request-file", "x"]).request_file == Path("x")
    with pytest.raises(SystemExit) as missing:
        parser.parse_args(common)
    assert missing.value.code == 2
    for removed in (["--keys", "k"], ["--buyer-key", "b"]):
        with pytest.raises(SystemExit) as failure:
            parser.parse_args([*common, "--request-file", "x", *removed])
        assert failure.value.code == 2


def test_poll_fails_closed_without_the_request_bundle(tmp_path: Path, monkeypatch, capsys) -> None:
    """A missing per-request bundle must fail instead of falling back to any key."""
    monkeypatch.setattr(
        market_cli,
        "_settings",
        lambda _args: JoinMarketSettings(
            data_dir=tmp_path,
            network_config=NetworkSettings(network=NetworkType.REGTEST, directory_servers=[]),
        ),
    )
    assert (
        market_cli.run(
            [
                "poll",
                "--data-dir",
                str(tmp_path),
                "--quote",
                str(tmp_path / "quote.json"),
                "--seller",
                str(tmp_path / "listing.json"),
                "--request-file",
                str(tmp_path / "missing-request.json"),
                "--raw-output",
                str(tmp_path / "delivery.json"),
            ]
        )
        == 1
    )
    assert not (tmp_path / "missing-request.json.keys").exists()
    assert capsys.readouterr().err.startswith("market command failed")
