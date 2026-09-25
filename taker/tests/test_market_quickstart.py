"""The documented buyer quickstart must work without hand-editing any JSON.

A buyer discovers sellers, picks one by nick, requests a quote, pays the printed
Lightning invoice outside this tool, polls the delivery and imports it. Only the
seller nick, the listing file and the request file are ever typed: every other
path is derived from the request file, and rerunning a step retries the same
purchase instead of starting a second one.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from bitcointx.core.key import CKey  # type: ignore[import-not-found]
from jmcore.bitcoin import hash160
from jmcore.credential_market import (
    BondReference,
    MarketAuthorization,
    PaymentTerms,
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
from taker.podle_manager import PoDLEManager

SELLER_NICK = "J5ABCDEFGHJKLMNP"
OTHER_NICK = "J5bcdefghjkmnpqr"
PREIMAGE = bytes(range(1, 33))
PAYMENT_HASH = hashlib.sha256(PREIMAGE).hexdigest()
PODLE_TXID = "11" * 32


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def _invoice() -> str:
    now = int(time.time())
    return encode(
        Bolt11(
            currency="bcrt",
            date=now - 1,
            amount_msat=MilliSatoshi(100_000),
            tags=Tags(
                [
                    Tag(TagChar.payment_hash, PAYMENT_HASH),
                    Tag(TagChar.payment_secret, "22" * 32),
                    Tag(TagChar.description, "quickstart"),
                    Tag(TagChar.expire_time, 3_600),
                    Tag(TagChar.min_final_cltv_expiry, 18),
                ]
            ),
        ),
        private_key="11" * 32,
    )


def _podle_record(owner: CKey) -> dict[str, Any]:
    proof = generate_podle(owner.secret_bytes, f"{PODLE_TXID}:0", 0)
    return {
        "version": 1,
        "network": "regtest",
        "outpoint": {"txid": PODLE_TXID, "vout": 0},
        "P": proof.p.hex(),
        "P2": proof.p2.hex(),
        "sig": proof.sig.hex(),
        "e": proof.e.hex(),
        "commitment": proof.commitment.hex(),
        "index": 0,
        "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
        "blockheight": 1,
    }


class _Backend:
    """Chain authority for the seller bond and for the sold PoDLE's own UTXO."""

    def __init__(self, scriptpubkey: str) -> None:
        self.scriptpubkey = scriptpubkey

    async def get_block_height(self) -> int:
        return 1

    async def get_block_time(self, _height: int) -> int:
        return int(time.time()) - 3600

    async def verify_bonds(self, bonds: list[BondVerificationRequest]) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                valid=True, txid=bond.txid, vout=bond.vout, confirmations=6, value=1_000_000
            )
            for bond in bonds
        ]

    async def get_utxo(self, txid: str, vout: int) -> SimpleNamespace | None:
        if txid != PODLE_TXID:
            return None
        return SimpleNamespace(
            txid=txid,
            vout=vout,
            value=1_000_000,
            confirmations=6,
            height=1,
            scriptpubkey=self.scriptpubkey,
        )

    def requires_neutrino_metadata(self) -> bool:
        return False

    async def close(self) -> None:
        return None


class _Transport:
    """Loopback directory: discovery returns raw listings, requests hit the seller."""

    def __init__(self, service: MarketService, listings: list[tuple[str, bytes]]) -> None:
        self.service = service
        self.listings = listings

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def discover(self, _timeout: float) -> list[tuple[str, bytes]]:
        return list(self.listings)

    async def request(
        self,
        sender: str,
        _encryption_key: bytes,
        payload: dict[str, object],
        _timeout: float,
    ) -> dict[str, object]:
        return await self.service.respond(sender, payload)


class _Quickstart:
    """One seller reachable over a loopback transport, plus an empty buyer data dir."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.buyer_dir = tmp_path / "buyer"
        self.buyer_dir.mkdir()
        seller_dir = tmp_path / "seller"
        seller_dir.mkdir()
        self.settings = JoinMarketSettings(
            data_dir=self.buyer_dir,
            network_config=NetworkSettings(network=NetworkType.REGTEST, directory_servers=[]),
        )
        monkeypatch.setattr(market_cli, "_settings", lambda _args: self.settings)

        self.bundle = market_cli._generate_key_bundle()
        self.seller_key = CKey(bytes.fromhex(self.bundle["seller_signing_key"]))
        owner = CKey(b"\x51" * 32)
        self.bond = BondReference(
            network="regtest",
            outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=1),
            pubkey=bytes(owner.pub).hex(),
            locktime=int(time.time()) + 86_400,
        )
        authorization = sign_document(
            MarketAuthorization(
                bond=self.bond, period=0, seller_pubkey=bytes(self.seller_key.pub).hex()
            ),
            owner,
        )
        self.podle = _podle_record(CKey(b"\x61" * 32))
        self.store = MarketStore(seller_dir / "seller.sqlite")
        self.store.add_inventory("podle", cast(str, self.podle["commitment"]), self.podle)
        self.store.add_payment(
            PaymentTerms(rail="lightning", request=_invoice(), amount_sats=100),
            f"ln:{PAYMENT_HASH}",
        )
        self.backend = _Backend(cast(str, self.podle["scriptpubkey"]))
        self.service = MarketService(
            self.store,
            authorization,
            self.seller_key,
            PrivateKey(bytes.fromhex(self.bundle["encryption_key"])),
            cast(BlockchainBackend, self.backend),
            products=["podle"],
            price_sats=100,
        )
        listing = asyncio.run(self.service.listing())
        self.transport = _Transport(self.service, [(SELLER_NICK, listing)])
        monkeypatch.setattr(market_cli, "_new_backend", lambda _settings: self.backend)
        monkeypatch.setattr(
            market_cli, "_new_transport", lambda _settings, **_kwargs: self.transport
        )

    def run(self, *argv: str) -> int:
        """Run one CLI command against the buyer data directory."""
        return market_cli.run([*argv, "--data-dir", str(self.buyer_dir)])

    def settle(self, quote_id: str) -> None:
        """Release the credential the way a seller does once the invoice is paid."""
        self.store.attach_credential(quote_id, self.podle)
        self.store.finalize(quote_id, self.seller_key, f"ln:{PAYMENT_HASH}", int(time.time()), 1)

    def close(self) -> None:
        self.store.close()


def _buy(market: _Quickstart, tmp_path: Path) -> tuple[Path, Path]:
    """Walk the documented quickstart up to a polled delivery."""
    seller_file = tmp_path / "seller.json"
    assert market.run("discover", "--seller", SELLER_NICK, "--output", str(seller_file)) == 0
    request_file = tmp_path / "buy.json"
    assert (
        market.run(
            "request",
            "--listing",
            str(seller_file),
            "--request-file",
            str(request_file),
            "--max-price-sats",
            "100",
            "--require-experimental-risk-ack",
        )
        == 0
    )
    quote_file = Path(str(request_file) + ".quote")
    quote_id = json.loads(quote_file.read_text(encoding="ascii"))["quote"]["body"]["quote_id"]
    market.settle(quote_id)
    assert (
        market.run("poll", "--seller", str(seller_file), "--request-file", str(request_file)) == 0
    )
    return seller_file, request_file


def test_quickstart_derives_every_path_from_the_request_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Only the nick, the listing file and the request file are ever typed."""
    market = _Quickstart(tmp_path, monkeypatch)
    try:
        assert market.run("discover", "--human") == 0
        human = capsys.readouterr().out
        assert SELLER_NICK in human and "podle" in human and "100" in human

        seller_file, request_file = _buy(market, tmp_path)
        seller_document = json.loads(seller_file.read_text(encoding="ascii"))
        assert set(seller_document) == {"seller_nick", "listing"}
        assert seller_document["seller_nick"] == SELLER_NICK

        quote_file = Path(str(request_file) + ".quote")
        keys_file = Path(str(request_file) + ".keys")
        delivery_file = Path(str(request_file) + ".delivery")
        for produced in (request_file, quote_file, keys_file, delivery_file):
            assert produced.stat().st_mode & 0o777 == 0o600
        # The delivery is a real credential package for this purchase.
        market_cli._credential_package(delivery_file)
        assert "lightning:" in capsys.readouterr().out

        # Rerunning either step retries the same purchase instead of buying twice.
        quote_bytes = quote_file.read_bytes()
        delivery_bytes = delivery_file.read_bytes()
        keys_bytes = keys_file.read_bytes()
        market.service._next_quote = 0
        assert (
            market.run(
                "request",
                "--listing",
                str(seller_file),
                "--request-file",
                str(request_file),
                "--max-price-sats",
                "100",
                "--require-experimental-risk-ack",
            )
            == 0
        )
        assert (
            market.run("poll", "--seller", str(seller_file), "--request-file", str(request_file))
            == 0
        )
        assert quote_file.read_bytes() == quote_bytes
        assert delivery_file.read_bytes() == delivery_bytes
        # A retry must keep the buyer key the seller already quoted.
        assert keys_file.read_bytes() == keys_bytes
    finally:
        market.close()


def test_quickstart_import_records_the_seller_that_sold_the_podle(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """A purchased PoDLE must carry the seller bond so that seller is excluded later."""
    market = _Quickstart(tmp_path, monkeypatch)
    try:
        _, request_file = _buy(market, tmp_path)
        capsys.readouterr()
        assert (
            market.run(
                "import",
                "--package",
                str(request_file) + ".delivery",
                "--quote",
                str(request_file) + ".quote",
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert result["product"] == "podle" and result["imported"] is True

        manager = PoDLEManager(market.buyer_dir)
        commitment = cast(str, market.podle["commitment"])
        assert commitment in manager.external_v1
        assert manager.external_v1_sellers[commitment] == market.bond

        # A repeated import is a no-op, not a second credential.
        assert (
            market.run(
                "import",
                "--package",
                str(request_file) + ".delivery",
                "--quote",
                str(request_file) + ".quote",
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["imported"] is False
    finally:
        market.close()


def test_quickstart_never_prints_a_private_key(tmp_path: Path, monkeypatch, capsys) -> None:
    """Nothing the buyer sees may contain a secret from either side of the trade."""
    market = _Quickstart(tmp_path, monkeypatch)
    try:
        _, request_file = _buy(market, tmp_path)
        assert (
            market.run(
                "import",
                "--package",
                str(request_file) + ".delivery",
                "--quote",
                str(request_file) + ".quote",
            )
            == 0
        )
    finally:
        market.close()
    captured = capsys.readouterr()
    buyer_bundle = json.loads(Path(str(request_file) + ".keys").read_text(encoding="ascii"))
    secrets = [
        *(value for field, value in buyer_bundle.items() if field != "version"),
        *(value for field, value in market.bundle.items()),
        PREIMAGE.hex(),
    ]
    for secret in secrets:
        assert secret not in captured.out
        assert secret not in captured.err


def test_discover_selection_refuses_an_unknown_or_duplicated_seller(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Selection is explicit: no match and an ambiguous match are both errors."""
    market = _Quickstart(tmp_path, monkeypatch)
    try:
        listing = market.transport.listings[0][1]
        market.transport.listings = [
            (SELLER_NICK, listing),
            # An unauthenticated or malformed listing never becomes selectable.
            ("J5MALFORMEDNICK1", b'{"body":{"network":"regtest"},"sig":"00"}'),
            (OTHER_NICK, listing),
            (OTHER_NICK, listing),
        ]
        output = tmp_path / "seller.json"
        assert market.run("discover", "--seller", "J5MALFORMEDNICK1", "--output", str(output)) == 1
        assert "no authenticated seller listing matched" in capsys.readouterr().err
        assert not output.exists()

        assert market.run("discover", "--seller", "J5NOSUCHSELLER01", "--output", str(output)) == 1
        assert not output.exists()

        assert market.run("discover", "--seller", OTHER_NICK, "--output", str(output)) == 1
        assert "more than one listing" in capsys.readouterr().err
        assert not output.exists()

        # The unambiguous seller is still selectable from the same discovery set.
        assert market.run("discover", "--seller", SELLER_NICK, "--output", str(output)) == 0
        assert json.loads(output.read_text(encoding="ascii"))["seller_nick"] == SELLER_NICK
    finally:
        market.close()


def test_bare_invocation_shows_the_quickstart_commands(capsys) -> None:
    """``jm-market`` with no arguments must show the whole purchase, not just a command list."""
    assert market_cli.run([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    for fragment in (
        "jm-market discover --human",
        "jm-market discover --seller",
        "--request-file buy.json",
        "buy.json.quote",
        "buy.json.delivery",
    ):
        assert fragment in captured.out
    # Raw key encodings belong in the command that writes them, not in the entry help.
    assert "secret_key" not in captured.out
    parser = market_cli.build_parser()
    commands = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    assert "secret_key" in commands.choices["keygen"].format_help()
