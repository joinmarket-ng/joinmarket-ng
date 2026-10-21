"""Automatic external PoDLE purchases (``external_podle_mode = "prefer"``).

A purchased credential lets a CoinJoin commit without revealing one of the
taker's own UTXOs. The price is capped at twice the fee of a one-input,
one-output Taproot transaction at the 3-block fee rate, which is about what
the taker would otherwise pay to make a fresh UTXO with three new attempts.
Routing fees count against the same cap. At most a few purchases are made per
run, and a failed purchase falls back to the taker's own UTXOs.

Every purchase is recorded under ``<data dir>/market/purchases`` before each
step that can leave money in flight, so a credential paid for before a crash
is still collected on the next run.
"""

from __future__ import annotations

import asyncio
import errno
import json
import math
import os
import secrets
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from jmcore.channel_ring import ChannelRingNodeConfig
from jmcore.credential_market import (
    MarketError,
    MarketListing,
    SignedDocument,
    accept_listing,
    verify_authorization,
)
from jmcore.crypto import NickIdentity
from jmcore.external_podle import ExternalPoDLE
from jmcore.market_faults import MarketFaultCache
from jmcore.protocol import JM_VERSION
from jmcore.secure_files import atomic_write_private, ensure_private_directory
from jmcore.settings import JoinMarketSettings
from jmwallet.backends.base import BlockchainBackend
from loguru import logger
from nacl.public import PrivateKey

from taker.market_service import (
    DeliveryRequest,
    QuoteRequest,
    accept_quote,
    open_delivery,
    verify_podle_backing,
)
from taker.market_transport import MarketTransport
from taker.podle_manager import PoDLEManager

P2TR_SPEND_VBYTES = 111
CAP_FEE_MULTIPLE = 2
MAX_PURCHASES_PER_RUN = 3
DISCOVERY_SECONDS = 15.0
REQUEST_SECONDS = 60.0
NODE_DELIVERY_SECONDS = 120.0
POLL_INTERVAL_SECONDS = 5.0
PAYMENT_CLTV_LIMIT = 2016
# A paid quote is collected for this long after it expires (hold invoice grace).
RESUME_SECONDS = 24 * 3600


def purchase_cap_sats(fee_rate_sat_vb: float) -> int:
    """The most a PoDLE credential may cost in total, routing fees included."""
    return CAP_FEE_MULTIPLE * math.ceil(P2TR_SPEND_VBYTES * max(fee_rate_sat_vb, 1.0))


def seller_price_sats(fee_rate_sat_vb: float) -> int:
    """The default price of a sold PoDLE credential: half the buyer's default cap."""
    return purchase_cap_sats(fee_rate_sat_vb) // CAP_FEE_MULTIPLE


async def market_fee_rate(backend: BlockchainBackend) -> float:
    """The 3-block fee estimate that prices PoDLE credentials, at least 1 sat/vB."""
    try:
        if backend.can_estimate_fee():
            return max(1.0, float(await backend.estimate_fee(3)))
    except Exception:
        logger.debug("Fee estimation failed; pricing PoDLE credentials at 1 sat/vB")
    return 1.0


def min_backing_sats(cj_amount: int, min_percent: int) -> int:
    """The smallest backing UTXO makers accept for this CoinJoin amount."""
    return max(1, math.ceil(cj_amount * min_percent / 100))


#: "paid": the node paid (or may have paid); "shown": a person was asked to pay;
#: "failed": certainly nothing was paid.
PayOutcome = Literal["paid", "shown", "failed"]
# Missing history and an empty stream both leave the outcome unknown.
PaymentState = Literal["succeeded", "failed", "pending"]


@dataclass(frozen=True)
class PaymentIdentity:
    """The original paying node and the invoice's payment hash."""

    payer: str
    payment_hash: str


class InvoicePayer(Protocol):
    """Pays one market invoice; see :class:`NodePayer` and :class:`InteractivePayer`."""

    def routing_budget(self, cap_sats: int) -> int:
        """The part of the cap kept for routing fees."""
        ...

    async def pay(
        self, bolt11: str, *, amount_sats: int, fee_limit_sats: int, expires_at: int
    ) -> PayOutcome: ...


class TrackedPayer(InvoicePayer, Protocol):
    """A payer that can later tell whether an earlier payment of an invoice resolved."""

    async def identify(self, bolt11: str) -> PaymentIdentity: ...

    async def status(self, identity: PaymentIdentity) -> PaymentState: ...

    async def pay(
        self,
        bolt11: str,
        *,
        amount_sats: int,
        fee_limit_sats: int,
        expires_at: int,
        expected_payer: str | None = None,
    ) -> PayOutcome: ...


class NodePayer:
    """Pays from the Lightning node of the CoinJoin's source mixdepth.

    When the node cannot pay (no route or liquidity), ``fallback`` (an
    interactive payer) may show the invoice instead.
    """

    def __init__(
        self,
        node: ChannelRingNodeConfig,
        fallback: InvoicePayer | None = None,
        *,
        purpose: str = "a PoDLE credential",
    ) -> None:
        self.node = node
        self.fallback = fallback
        self.purpose = purpose

    def routing_budget(self, cap_sats: int) -> int:
        return max(1, cap_sats // 5)

    def _client(self) -> Any:
        from jmswap.lnd_peer import LndPeerClient

        return LndPeerClient(
            self.node.lnd_grpc_url.removeprefix("https://"),
            self.node.lnd_tls_cert_path.expanduser().read_bytes(),
            self.node.lnd_macaroon_path.expanduser().read_bytes(),
        )

    async def identify(self, bolt11: str) -> PaymentIdentity:
        """Identify the paying node and invoice before recording the payment intent."""
        async with self._client() as client:
            node = await client.node_info()
            invoice = await client.inspect_invoice(bolt11)
        return PaymentIdentity(node.identity_pubkey, invoice.payment_hash.hex())

    async def status(self, identity: PaymentIdentity) -> PaymentState:
        """Look up the payment only at its original node; missing history stays pending."""
        from jmswap.lnd_peer import PaymentStatus

        async with self._client() as client:
            if (await client.node_info()).identity_pubkey != identity.payer:
                raise ValueError("bond payment lookup requires its original paying node")
            result = await client.track_payment(bytes.fromhex(identity.payment_hash))
        if result is None:
            return "pending"
        if result.status is PaymentStatus.SUCCEEDED:
            return "succeeded"
        if result.status is PaymentStatus.FAILED:
            return "failed"
        return "pending"

    async def pay(
        self,
        bolt11: str,
        *,
        amount_sats: int,
        fee_limit_sats: int,
        expires_at: int,
        expected_payer: str | None = None,
    ) -> PayOutcome:
        from jmswap.lnd_peer import PaymentStatus

        timeout = max(1, min(60, expires_at - int(time.time()) - 5))
        async with self._client() as client:
            if expected_payer is not None:
                if (await client.node_info()).identity_pubkey != expected_payer:
                    raise ValueError("bond payment requires its recorded paying node")
                if self.fallback is not None:
                    raise ValueError("tracked bond payments cannot use an external payer")
            result = await client.pay(
                bolt11,
                fee_limit_sat=fee_limit_sats,
                cltv_limit=PAYMENT_CLTV_LIMIT,
                timeout_seconds=timeout,
            )
        if result.status is PaymentStatus.FAILED:
            if self.fallback is None:
                return "failed"
            logger.info("The mixdepth node cannot pay this invoice; showing it instead")
            return await self.fallback.pay(
                bolt11,
                amount_sats=amount_sats,
                fee_limit_sats=fee_limit_sats,
                expires_at=expires_at,
            )
        logger.info(
            "Paid {} sats for {} (routing {} sats)", amount_sats, self.purpose, result.fee_sat
        )
        return "paid"


class InteractivePayer:
    """Shows the invoice and a QR code for the user to pay from any wallet."""

    def __init__(self, write: Callable[[str], Any] | None = None) -> None:
        self._write = write if write is not None else sys.stderr.write

    def routing_budget(self, cap_sats: int) -> int:
        return 0

    async def pay(
        self, bolt11: str, *, amount_sats: int, fee_limit_sats: int, expires_at: int
    ) -> PayOutcome:
        lines = [
            "",
            f"Pay {amount_sats} sats for an external PoDLE credential (keeps your UTXOs private):",
            f"lightning:{bolt11}",
        ]
        qr = _qr_text(bolt11)
        if qr:
            lines.append(qr)
        until = time.strftime("%H:%M:%S", time.localtime(expires_at))
        lines.append(f"Waiting for the payment until {until}; Ctrl-C to cancel the CoinJoin.\n")
        self._write("\n".join(lines))
        return "shown"


def _qr_text(data: str) -> str:
    try:
        import io

        import segno
    except ImportError:
        return ""
    buffer = io.StringIO()
    segno.make(data.upper(), error="l").terminal(out=buffer, compact=True, border=2)
    return buffer.getvalue()


def payer_for_mixdepth(
    ring_nodes: dict[str, ChannelRingNodeConfig],
    mixdepth_nodes: dict[int, str],
    *,
    interactive: bool,
) -> Callable[[int], InvoicePayer | None]:
    """Pay from the source mixdepth's node, else (interactively) by invoice, else not at all.

    There is no cross-mixdepth fallback: paying from another mixdepth's node
    would link the two mixdepths through the seller.
    """

    def payer(mixdepth: int) -> InvoicePayer | None:
        fallback = InteractivePayer() if interactive else None
        name = mixdepth_nodes.get(mixdepth)
        if name is not None and name in ring_nodes:
            return NodePayer(ring_nodes[name], fallback)
        return fallback

    return payer


@dataclass
class PurchaseRecord:
    """One purchase as persisted; ``state`` advances requested, quoted, paying, done."""

    path: Path
    data: dict[str, Any]

    @classmethod
    def new(cls, directory: Path, **data: Any) -> PurchaseRecord:
        request_id = data["request"]["request_id"]
        record = cls(directory / f"{request_id}.json", {"version": 1, **data})
        record.save()
        return record

    @property
    def state(self) -> str:
        return str(self.data["state"])

    def advance(self, state: str, **data: Any) -> None:
        self.data.update(data, state=state)
        self.save()

    def save(self) -> None:
        encoded = json.dumps(self.data, sort_keys=True, separators=(",", ":")).encode()
        atomic_write_private(self.path, encoded)
        # The rename itself must survive a host crash, not only the contents.
        try:
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise


def read_records(directory: Path) -> tuple[list[PurchaseRecord], list[Path]]:
    """Persisted purchases, and the files (kept in place) that cannot be read."""
    records: list[PurchaseRecord] = []
    unreadable: list[Path] = []
    if not directory.is_dir():
        return records, unreadable
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_bytes())
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("state"), str)
            ):
                raise ValueError
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable market purchase record {}", path.name)
            unreadable.append(path)
            continue
        records.append(PurchaseRecord(path, data))
    return records, unreadable


def load_records(directory: Path) -> list[PurchaseRecord]:
    """Persisted purchases, skipping (and keeping) any file that cannot be read."""
    return read_records(directory)[0]


def purchases_dir(data_dir: Path) -> Path:
    path = data_dir / "market" / "purchases"
    ensure_private_directory(path)
    return path


def foreign_commitments(data_dir: Path, mixdepth: int) -> frozenset[str]:
    """Credentials bought from another mixdepth's node: using them would link the two."""
    return frozenset(
        str(record.data["commitment"])
        for record in load_records(data_dir / "market" / "purchases")
        if record.state == "done"
        and record.data.get("mixdepth") != mixdepth
        and record.data.get("commitment")
    )


def new_buyer_secret() -> str:
    return secrets.token_hex(32)


_TERMINAL_SECRETS = ("buyer_secret", "listing", "quote")


def _finish(record: PurchaseRecord, state: str, **data: Any) -> None:
    """End a purchase, dropping its buyer key and documents once nothing is owed."""
    for key in _TERMINAL_SECRETS:
        record.data.pop(key, None)
    record.advance(state, **data)


def buyer_transport(settings: JoinMarketSettings, cache: MarketFaultCache) -> MarketTransport:
    """A market transport for one purchase, under a fresh nick and stream isolation."""

    def receive_fault(raw: bytes) -> None:
        cache.ingest(raw)

    # A fresh nick per purchase: the seller cannot link it to any CoinJoin nick.
    return MarketTransport(
        directory_servers=settings.get_directory_servers(),
        network=settings.network_config.network.value,
        nick_identity=NickIdentity(JM_VERSION),
        socks_host=settings.tor.socks_host,
        socks_port=settings.tor.socks_port,
        connection_timeout=settings.tor.connection_timeout,
        stream_isolation=True,
        nick_auth_mode=settings.network_config.nick_auth_mode,
        nick_auth_directory_ids=settings.network_config.nick_auth_directory_ids,
        allow_clearnet_connections=settings.network_config.allow_clearnet_connections,
        on_fault=receive_fault,
    )


class PoDLEPurchaser:
    """Buys one external PoDLE credential for a CoinJoin round that has none.

    ``buy`` never raises for market or payment problems: a False result means
    the round uses the taker's own UTXOs. It never pays a second seller while
    an earlier payment may still be in flight.
    """

    def __init__(
        self,
        settings: JoinMarketSettings,
        payer_for: Callable[[int], InvoicePayer | None],
        *,
        max_purchases: int = MAX_PURCHASES_PER_RUN,
        transport_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._payer_for = payer_for
        self._remaining = max_purchases
        self._transport_factory = transport_factory or self._new_transport
        self._data_dir = Path(settings.get_data_dir())
        network = settings.network_config
        self.network = (network.bitcoin_network or network.network).value
        self._fault_cache = MarketFaultCache(self._data_dir)

    def can_pay(self, mixdepth: int) -> bool:
        """Whether a purchase for this mixdepth has a way to pay."""
        return self._payer_for(mixdepth) is not None

    def _new_transport(self) -> MarketTransport:
        return buyer_transport(self._settings, self._fault_cache)

    async def buy(
        self,
        *,
        backend: BlockchainBackend,
        podle_manager: PoDLEManager,
        mixdepth: int,
        cj_amount: int,
        min_percent: int,
    ) -> bool:
        """Import one purchased credential for ``cj_amount``; False means use own UTXOs."""
        payer = self._payer_for(mixdepth)
        if payer is None:
            logger.info(
                "No Lightning node pays for mixdepth {}; using own UTXOs for PoDLE", mixdepth
            )
            return False
        if self._remaining <= 0:
            logger.info("PoDLE purchase limit for this run reached; using own UTXOs")
            return False
        try:
            directory = purchases_dir(self._data_dir)
        except (OSError, ValueError):
            logger.warning("Cannot create the private PoDLE purchase directory")
            return False
        cap = purchase_cap_sats(await market_fee_rate(backend))
        max_price = cap - payer.routing_budget(cap)
        minimum = min_backing_sats(cj_amount, min_percent)
        transport = self._transport_factory()
        try:
            await transport.start()
            if await self._resume(directory, transport, backend, podle_manager, mixdepth):
                return True
            for nick, signed_listing, listing in await self._candidates(transport, max_price):
                if self._remaining <= 0:
                    break
                outcome = await self._purchase(
                    directory,
                    transport,
                    backend,
                    podle_manager,
                    payer,
                    nick=nick,
                    signed_listing=signed_listing,
                    listing=listing,
                    mixdepth=mixdepth,
                    cap=cap,
                    max_price=max_price,
                    minimum=minimum,
                )
                if outcome == "imported":
                    return True
                if outcome == "stop":
                    return False
            logger.info("No seller offered a usable PoDLE credential within {} sats", cap)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("PoDLE purchase failed ({}); using own UTXOs", type(exc).__name__)
            return False
        finally:
            with suppress(Exception):
                await transport.close()

    async def _candidates(
        self, transport: Any, max_price: int
    ) -> list[tuple[str, SignedDocument, MarketListing]]:
        """Authenticated PoDLE listings within the cap, cheapest first, one per seller."""
        now = int(time.time())
        best: dict[str, tuple[int, int, str, SignedDocument, MarketListing]] = {}
        for nick, raw in await transport.discover(DISCOVERY_SECONDS):
            try:
                signed, listing = accept_listing(raw, self.network, now)
            except Exception:
                continue
            if "podle" not in listing.products or listing.price_sats > max_price:
                continue
            entry = (listing.price_sats, secrets.randbits(32), nick, signed, listing)
            current = best.get(listing.seller_pubkey)
            if current is None or entry[:2] < current[:2]:
                best[listing.seller_pubkey] = entry
        return [entry[2:] for entry in sorted(best.values(), key=lambda entry: entry[:2])]

    async def _purchase(
        self,
        directory: Path,
        transport: Any,
        backend: BlockchainBackend,
        podle_manager: PoDLEManager,
        payer: InvoicePayer,
        *,
        nick: str,
        signed_listing: SignedDocument,
        listing: MarketListing,
        mixdepth: int,
        cap: int,
        max_price: int,
        minimum: int,
    ) -> str:
        """One seller: "imported", "next" (nothing paid) or "stop" (payment unresolved)."""
        secret = new_buyer_secret()
        buyer_key = PrivateKey(bytes.fromhex(secret))
        request = QuoteRequest(
            request_id=secrets.token_hex(16),
            buyer_pubkey=bytes(buyer_key.public_key).hex(),
            product="podle",
            rail="lightning",
            max_price_sats=max_price,
            min_backing_sats=minimum,
        )
        # The buyer key is durable before the seller sees the request.
        record = PurchaseRecord.new(
            directory,
            state="requested",
            mixdepth=mixdepth,
            seller_nick=nick,
            listing=signed_listing.model_dump(mode="json"),
            request=request.wire(),
            buyer_secret=secret,
            created_at=int(time.time()),
        )
        try:
            response = await transport.request(
                nick, bytes.fromhex(listing.encryption_pubkey), request.wire(), REQUEST_SECONDS
            )
            quote_data = response.get("quote")
            if not isinstance(quote_data, dict):
                raise MarketError(f"no quote ({response.get('error', 'no answer')})")
            signed_quote = SignedDocument.model_validate(quote_data)
            quote = await accept_quote(
                signed_quote, listing, request, backend, int(time.time()), self._fault_cache
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Seller {} did not quote a usable PoDLE: {}", nick, exc)
            _finish(record, "failed")
            return "next"
        price = quote.payment.amount_sats
        record.advance(
            "paying",
            quote=signed_quote.model_dump(mode="json"),
            price_sats=price,
            expires_at=quote.expires_at,
        )
        self._remaining -= 1
        try:
            outcome: PayOutcome = await payer.pay(
                quote.payment.request,
                amount_sats=price,
                fee_limit_sats=cap - price,
                expires_at=quote.expires_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The payment may or may not have left: only delivery can tell.
            logger.warning("PoDLE invoice payment outcome unknown ({})", type(exc).__name__)
            outcome = "paid"
        if outcome == "failed":
            logger.info("Payment to seller {} failed; nothing was spent", nick)
            _finish(record, "failed")
            return "next"
        # A node payment settles before it returns, so its delivery is ready;
        # a shown invoice is waited for until the quote expires.
        deadline = float(quote.expires_at)
        if outcome == "paid":
            deadline = min(deadline, time.time() + NODE_DELIVERY_SECONDS)
        if await self._collect(record, transport, backend, podle_manager, deadline=deadline):
            return "imported"
        logger.warning(
            "The paid PoDLE credential has not been delivered yet; it is collected on a "
            "later run, and an unsettled payment is refunded when its invoice expires"
        )
        return "stop"

    async def _resume(
        self,
        directory: Path,
        transport: Any,
        backend: BlockchainBackend,
        podle_manager: PoDLEManager,
        mixdepth: int,
    ) -> bool:
        """Collect credentials paid for by an earlier run, from this mixdepth's node only."""
        now = time.time()
        imported = False
        for record in load_records(directory):
            if record.state == "requested":
                # Nothing was paid: the run stopped before a quote was accepted.
                _finish(record, "failed")
                continue
            if record.state != "paying":
                continue
            if now > int(record.data.get("expires_at", 0)) + RESUME_SECONDS:
                _finish(record, "expired")
                continue
            if record.data.get("mixdepth") != mixdepth or imported:
                continue
            imported = await self._collect(record, transport, backend, podle_manager, deadline=now)
        return imported

    async def _collect(
        self,
        record: PurchaseRecord,
        transport: Any,
        backend: BlockchainBackend,
        podle_manager: PoDLEManager,
        *,
        deadline: float,
    ) -> bool:
        """Poll for the delivery until ``deadline`` (at least once), verify and import it."""
        try:
            signed_listing = SignedDocument.model_validate(record.data["listing"])
            listing = signed_listing.verified(
                MarketListing, MarketListing.model_validate(signed_listing.body).seller_pubkey
            )
            signed_quote = SignedDocument.model_validate(record.data["quote"])
            buyer_key = PrivateKey(bytes.fromhex(record.data["buyer_secret"]))
            delivery = DeliveryRequest(quote_id=signed_quote.body["quote_id"]).model_dump(
                mode="json"
            )
        except Exception:
            logger.error("PoDLE purchase record {} is damaged; keeping it", record.path.name)
            return False
        while True:
            try:
                response = await transport.request(
                    record.data["seller_nick"],
                    bytes.fromhex(listing.encryption_pubkey),
                    delivery,
                    REQUEST_SECONDS,
                )
                encrypted = response.get("delivery")
            except asyncio.CancelledError:
                raise
            except Exception:
                encrypted = None
            if isinstance(encrypted, str):
                break
            if time.time() + POLL_INTERVAL_SECONDS >= deadline:
                return False
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            package = open_delivery(encrypted, buyer_key, signed_quote)
            credential = package.verify()
            if not isinstance(credential, ExternalPoDLE) or credential.network != self.network:
                raise MarketError("delivery is not a PoDLE for this network")
            await verify_podle_backing(credential, backend)
            authority = verify_authorization(package.authorization)
            podle_manager.import_external(credential, seller_bond=authority.bond)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Keep the key and the sealed delivery as evidence for a fault report.
            logger.error("The delivered PoDLE credential is invalid: {}", exc)
            record.advance("invalid", delivery=encrypted)
            return False
        _finish(record, "done", commitment=credential.commitment)
        logger.info("Imported a purchased external PoDLE credential")
        return True
