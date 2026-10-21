"""Automatic fidelity bond leases for a maker without a usable bond.

A lease lasts the rest of the current retarget period. Listings are tried from
the highest price within the absolute cap down (routing fees count against
it), and a quote is accepted only when its price is within the annual rate cap
on the quoted bond's locked amount, checked on chain before paying. Selection
is by price: listings carry no bond value, and under the rate cap a higher
price only raises the smallest bond it can buy.

Every purchase is recorded under ``<data dir>/market/bond-purchases`` before
each step that can leave money in flight, with its buyer and renter
certificate keys. At most one lease is paid for per period:

- a payment the paying node has not resolved blocks every other purchase,
  across period boundaries, until the node reports it succeeded or failed;
- a lease paid for in the current period, usable or not, blocks buying
  another one in that period, and a seller whose paid lease was unusable is
  never bought from again;
- a delivered lease keeps its keys through its period, so it is handed out
  again instead of being bought twice;
- an unreadable record blocks every purchase until it is reviewed.

Delivered leases are imported into the wallet's bond registry, where a lease
for a later period renews the certificate of a bond leased here before.
"""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bitcointx.core.key import CKey  # type: ignore[import-not-found]
from jmcore.btc_script import derive_bond_address
from jmcore.credential_market import (
    BLOCKS_PER_YEAR,
    BondCredential,
    MarketError,
    MarketListing,
    MarketQuote,
    SignedDocument,
    accept_listing,
    lease_blocks,
    period_at_height,
    verify_authorization,
)
from jmcore.market_faults import MarketFaultCache
from jmcore.secure_files import ensure_private_directory
from jmcore.settings import JoinMarketSettings
from jmwallet.backends.base import BlockchainBackend
from loguru import logger
from nacl.public import PrivateKey

from taker.market_service import (
    DeliveryRequest,
    QuoteRequest,
    accept_quote,
    open_delivery,
    verify_market_bond,
)
from taker.podle_purchase import (
    DISCOVERY_SECONDS,
    NODE_DELIVERY_SECONDS,
    POLL_INTERVAL_SECONDS,
    REQUEST_SECONDS,
    PaymentIdentity,
    PayOutcome,
    PurchaseRecord,
    TrackedPayer,
    buyer_transport,
    new_buyer_secret,
    read_records,
)

# Dropped once nothing more can come of a purchase.
_TERMINAL_SECRETS = (
    "buyer_secret",
    "certificate_secret",
    "credential",
    "delivery",
    "listing",
    "quote",
)
# Paid for (or possibly paid for) without a usable lease; kept as evidence.
_PAID_UNUSABLE = frozenset({"invalid", "unimported", "unused"})
_STATES = frozenset({"requested", "paying", "done", "failed"}) | _PAID_UNUSABLE


class BondImportError(Exception):
    """A delivered lease cannot be recorded in the bond registry safely."""


@dataclass(frozen=True)
class LeasedBond:
    """A delivered, verified lease, already recorded in the bond registry."""

    credential: BondCredential
    certificate: CKey
    value: int
    confirmations: int


@dataclass
class _Review:
    """What earlier purchases leave for this attempt."""

    leased: LeasedBond | None = None
    blocked: bool = False
    excluded_sellers: set[str] = field(default_factory=set)


def bond_purchases_dir(data_dir: Path) -> Path:
    path = data_dir / "market" / "bond-purchases"
    ensure_private_directory(path)
    return path


def max_lease_price_sats(value: int, annual_rate: float, height: int) -> int:
    """The most a lease of ``value`` locked sats sold at ``height`` may cost."""
    return math.floor(value * annual_rate * lease_blocks(height) / BLOCKS_PER_YEAR)


def new_certificate_key() -> CKey:
    """A fresh renter certificate key."""
    while True:
        with suppress(ValueError):
            return CKey(secrets.token_bytes(32))


def import_bond_credential(
    credential: BondCredential,
    certificate: CKey,
    data_dir: Path,
    fingerprint: str,
    *,
    renewable: Collection[str] = (),
) -> bool:
    """Record a delivered lease in the bond registry; False when already recorded.

    ``renewable`` holds the certificate pubkeys of leases this data directory
    bought: an external entry still carrying one of them takes the
    certificate of a later period of the same bond. Any other difference from
    an existing entry, a cold owner's own certificate included, is refused and
    never overwritten.
    """
    if bytes(certificate.pub).hex() != credential.cert_pubkey:
        raise BondImportError("certificate key does not match delivered bond credential")
    from jmwallet.wallet.bond_registry import create_bond_info, load_registry, save_registry

    bond = credential.bond
    address = derive_bond_address(bytes.fromhex(bond.pubkey), bond.locktime, bond.network)
    try:
        registry = load_registry(
            data_dir,
            fingerprint,
            allow_legacy_fallback=False,
            fail_closed=True,
        )
    except ValueError as exc:
        raise BondImportError("could not safely load bond registry") from exc
    certificate_secret = certificate.secret_bytes.hex()
    entry = registry.get_bond_by_address(address.address)
    if entry is not None:
        same_bond = (
            entry.pubkey == bond.pubkey
            and entry.locktime == bond.locktime
            and entry.network == bond.network
            and entry.txid in (None, bond.outpoint.txid)
            and entry.vout in (None, bond.outpoint.vout)
        )
        if same_bond and (
            entry.cert_pubkey,
            entry.cert_privkey,
            entry.cert_signature,
            entry.cert_expiry,
        ) == (
            credential.cert_pubkey,
            certificate_secret,
            credential.cert_signature,
            credential.cert_expiry,
        ):
            return False
        renewal = (
            same_bond
            and entry.index == -1
            and entry.cert_pubkey in renewable
            and entry.cert_expiry is not None
            and credential.cert_expiry > entry.cert_expiry
        )
        if not renewal:
            raise BondImportError(
                "existing bond registry entry conflicts with delivered credential"
            )
    else:
        entry = create_bond_info(
            address=address.address,
            locktime=bond.locktime,
            index=-1,
            path="external",
            pubkey_hex=bond.pubkey,
            witness_script=address.witness_script,
            network=bond.network,
        )
        registry.add_bond(entry)
    entry.cert_pubkey = credential.cert_pubkey
    entry.cert_privkey = certificate_secret
    entry.cert_signature = credential.cert_signature
    entry.cert_expiry = credential.cert_expiry
    save_registry(registry, data_dir, fingerprint)
    return True


def _finish(record: PurchaseRecord, state: str, **data: Any) -> None:
    """End a purchase, dropping its keys and documents once nothing is owed."""
    for key in _TERMINAL_SECRETS:
        record.data.pop(key, None)
    record.advance(state, **data)


def _period(record: PurchaseRecord) -> int:
    return int(record.data["period"])


def _record_problem(record: PurchaseRecord) -> str | None:
    """Validate payment-relevant fields before any record can permit another purchase."""
    data = record.data
    if record.state not in _STATES:
        return "unsupported state"
    if record.state in {"requested", "failed"}:
        return None
    if type(data.get("period")) is not int or data["period"] < 0:
        return "missing or invalid lease period"
    if data.get("payment_state") not in {"pending", "succeeded", "failed"}:
        return "missing or invalid payment state"
    for key, size in (("payer", 33), ("payment_hash", 32), ("seller_pubkey", 33)):
        try:
            if not isinstance(data.get(key), str) or len(bytes.fromhex(data[key])) != size:
                return f"missing or invalid {key}"
        except ValueError:
            return f"missing or invalid {key}"
    if record.state == "paying":
        for key in ("buyer_secret", "certificate_secret"):
            try:
                if len(bytes.fromhex(data.get(key, ""))) != 32:
                    return f"missing or invalid {key}"
            except (ValueError, TypeError):
                return f"missing or invalid {key}"
        for key in ("listing", "quote"):
            if not isinstance(data.get(key), dict):
                return f"missing or invalid {key}"
        if not isinstance(data.get("seller_nick"), str):
            return "missing seller nick"
    if record.state == "done":
        if not isinstance(data.get("cert_pubkey"), str):
            return "missing certificate provenance"
        # Retained only until the period ends, then removed together.
        if "credential" in data or "certificate_secret" in data:
            try:
                BondCredential.model_validate(data["credential"])
                CKey(bytes.fromhex(data["certificate_secret"]))
            except Exception:
                return "invalid saved lease"
    return None


def _renewable(records: list[PurchaseRecord]) -> frozenset[str]:
    """Certificate pubkeys of every lease delivered here: the provenance of renewals."""
    return frozenset(
        record.data["cert_pubkey"]
        for record in records
        if record.state == "done" and isinstance(record.data.get("cert_pubkey"), str)
    )


class BondPurchaser:
    """Leases one fidelity bond for the rest of the current retarget period.

    ``lease`` never raises for market or payment problems: None means no lease
    now. It pays at most one lease per period, and never while the paying
    node has not resolved an earlier payment.
    """

    def __init__(
        self,
        settings: JoinMarketSettings,
        payer: TrackedPayer,
        *,
        wallet_fingerprint: str,
        transport_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._payer = payer
        self._fingerprint = wallet_fingerprint
        self._data_dir = Path(settings.get_data_dir())
        network = settings.network_config
        self.network = (network.bitcoin_network or network.network).value
        self._fault_cache = MarketFaultCache(self._data_dir)
        self._transport_factory = transport_factory or (
            lambda: buyer_transport(settings, self._fault_cache)
        )

    async def lease(
        self, backend: BlockchainBackend, *, cap_sats: int, max_rate: float
    ) -> LeasedBond | None:
        """Lease a bond within ``cap_sats`` (routing included) and ``max_rate``, by price."""
        try:
            directory = bond_purchases_dir(self._data_dir)
        except (OSError, ValueError):
            logger.warning("Cannot create the private bond purchase directory")
            return None
        max_price = cap_sats - self._payer.routing_budget(cap_sats)
        if max_price < 1:
            return None
        records, unreadable = read_records(directory)
        for record in records:
            if problem := _record_problem(record):
                logger.error("Bond purchase record {} needs review: {}", record.path.name, problem)
                unreadable.append(record.path)
        if unreadable:
            # Such a record proves neither that nothing was paid nor that it resolved.
            logger.error(
                "Unreadable bond purchase records ({}); no lease is bought until they are reviewed",
                ", ".join(path.name for path in unreadable),
            )
            return None
        renewable = _renewable(records)
        transport = self._transport_factory()
        try:
            await transport.start()
            period = period_at_height(await backend.get_block_height())
            review = await self._review(records, transport, backend, period, renewable)
            if review.leased is not None:
                return review.leased
            if review.blocked:
                return None
            candidates = await self._candidates(transport, max_price, review.excluded_sellers)
            for nick, signed_listing, listing in candidates:
                outcome = await self._purchase(
                    directory,
                    transport,
                    backend,
                    nick=nick,
                    signed_listing=signed_listing,
                    listing=listing,
                    cap=cap_sats,
                    max_price=max_price,
                    max_rate=max_rate,
                    renewable=renewable,
                )
                if isinstance(outcome, LeasedBond):
                    return outcome
                if outcome == "stop":
                    return None
            logger.info(
                "No seller offered a fidelity bond lease within {} sats and {:.2%} a year",
                cap_sats,
                max_rate,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Fidelity bond lease purchase failed ({})", type(exc).__name__)
            return None
        finally:
            with suppress(Exception):
                await transport.close()

    async def _review(
        self,
        records: list[PurchaseRecord],
        transport: Any,
        backend: BlockchainBackend,
        period: int,
        renewable: Collection[str],
    ) -> _Review:
        """Settle what earlier purchases left: a lease to hand out, or a reason to buy none."""
        review = _Review()
        for record in records:
            state = record.state
            if state not in {"requested", "failed"}:
                await self._resolve(record, period)
                state = record.state
                if record.data["payment_state"] == "pending":
                    logger.warning(
                        "Bond payment in {} is unresolved; retain its original LND history "
                        "and keep its settlement monitor running. No new lease is bought",
                        record.path.name,
                    )
                    review.blocked = True
            if state == "requested":
                # Nothing was paid: the attempt stopped before a quote was accepted.
                _finish(record, "failed")
            elif state == "paying":
                if _period(record) == period:
                    leased = await self._collect(
                        record, transport, backend, deadline=time.time(), renewable=renewable
                    )
                    if leased is not None:
                        review.leased = leased
                if record.state in _PAID_UNUSABLE:
                    review.excluded_sellers.add(str(record.data["seller_pubkey"]))
                if record.state == "paying":
                    logger.info("An earlier bond lease payment is unresolved; paying no seller")
                    review.blocked = True
                elif record.state != "failed" and _period(record) == period:
                    review.blocked = True
            elif state == "done":
                if _period(record) != period:
                    if (
                        record.data["payment_state"] != "pending"
                        and "certificate_secret" in record.data
                    ):
                        _finish(record, "done")
                    continue
                leased = await self._recall(record, backend)
                if leased is not None:
                    review.leased = leased
                review.blocked = True
            elif state in _PAID_UNUSABLE:
                review.excluded_sellers.add(str(record.data["seller_pubkey"]))
                if _period(record) == period:
                    review.blocked = True
        return review

    async def _resolve(self, record: PurchaseRecord, period: int) -> None:
        """Reconcile settlement independently of delivery, including invalid and done leases."""
        status = record.data["payment_state"]
        if status == "pending":
            try:
                status = await self._payer.status(
                    PaymentIdentity(record.data["payer"], record.data["payment_hash"])
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Cannot look up a bond lease payment ({})", type(exc).__name__)
                return
            if status in {"failed", "succeeded"}:
                record.advance(record.state, payment_state=status)
        if status == "failed":
            logger.info("An earlier bond lease payment failed; nothing was spent")
            _finish(record, "failed", payment_state="failed")
        if status == "succeeded" and _period(record) != period and record.state == "paying":
            # Paid, but its period passed without a usable delivery.
            logger.warning(
                "A bond lease paid in period {} was never delivered usable; its seller is "
                "not bought from again",
                _period(record),
            )
            record.advance("unused")

    async def _recall(
        self, record: PurchaseRecord, backend: BlockchainBackend
    ) -> LeasedBond | None:
        """The lease delivered earlier in this period, if its bond is still on chain."""
        try:
            credential = BondCredential.model_validate(record.data["credential"])
            certificate = CKey(bytes.fromhex(record.data["certificate_secret"]))
            outpoint = credential.bond.outpoint
            utxo = await backend.get_utxo(outpoint.txid, outpoint.vout)
        except asyncio.CancelledError:
            raise
        except Exception:
            utxo = None
        if utxo is None:
            logger.warning(
                "The bond lease in {} is unavailable; no other lease is bought "
                "before the next period",
                record.path.name,
            )
            return None
        return LeasedBond(
            credential=credential,
            certificate=certificate,
            value=utxo.value,
            confirmations=utxo.confirmations,
        )

    async def _candidates(
        self, transport: Any, max_price: int, excluded_sellers: Collection[str]
    ) -> list[tuple[str, SignedDocument, MarketListing]]:
        """Authenticated bond listings within the cap, highest price first, one per seller."""
        now = int(time.time())
        best: dict[str, tuple[int, int, str, SignedDocument, MarketListing]] = {}
        for nick, raw in await transport.discover(DISCOVERY_SECONDS):
            try:
                signed, listing = accept_listing(raw, self.network, now)
            except Exception:
                continue
            if (
                "bond" not in listing.products
                or listing.price_sats > max_price
                or listing.seller_pubkey in excluded_sellers
            ):
                continue
            entry = (-listing.price_sats, secrets.randbits(32), nick, signed, listing)
            current = best.get(listing.seller_pubkey)
            if current is None or entry[:2] < current[:2]:
                best[listing.seller_pubkey] = entry
        return [entry[2:] for entry in sorted(best.values(), key=lambda entry: entry[:2])]

    async def _purchase(
        self,
        directory: Path,
        transport: Any,
        backend: BlockchainBackend,
        *,
        nick: str,
        signed_listing: SignedDocument,
        listing: MarketListing,
        cap: int,
        max_price: int,
        max_rate: float,
        renewable: Collection[str],
    ) -> LeasedBond | str:
        """One seller: a lease, "next" (nothing paid) or "stop" (paid, or possibly paid)."""
        secret = new_buyer_secret()
        buyer_key = PrivateKey(bytes.fromhex(secret))
        certificate = new_certificate_key()
        request = QuoteRequest(
            request_id=secrets.token_hex(16),
            buyer_pubkey=bytes(buyer_key.public_key).hex(),
            product="bond",
            certificate_pubkey=bytes(certificate.pub).hex(),
            rail="lightning",
            max_price_sats=max_price,
        )
        # Both keys are durable before the seller sees the request.
        record = PurchaseRecord.new(
            directory,
            state="requested",
            seller_nick=nick,
            seller_pubkey=listing.seller_pubkey,
            listing=signed_listing.model_dump(mode="json"),
            request=request.wire(),
            buyer_secret=secret,
            certificate_secret=certificate.secret_bytes.hex(),
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
            authority = verify_authorization(quote.authorization)
            bond = authority.bond
            utxo = await backend.get_utxo(bond.outpoint.txid, bond.outpoint.vout)
            if utxo is None:
                raise MarketError("the bond is not on chain")
            price = quote.payment.amount_sats
            limit = max_lease_price_sats(utxo.value, max_rate, await backend.get_block_height())
            if price > limit:
                raise MarketError(f"{price} sats is above the rate cap of {limit} sats")
            identity = await self._payer.identify(quote.payment.request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("Seller {} did not quote a usable bond lease: {}", nick, exc)
            _finish(record, "failed")
            return "next"
        record.advance(
            "paying",
            quote=signed_quote.model_dump(mode="json"),
            invoice=quote.payment.request,
            payer=identity.payer,
            payment_hash=identity.payment_hash,
            payment_state="pending",
            period=authority.period,
            price_sats=price,
            expires_at=quote.expires_at,
        )
        try:
            outcome: PayOutcome = await self._payer.pay(
                quote.payment.request,
                amount_sats=price,
                fee_limit_sats=cap - price,
                expires_at=quote.expires_at,
                expected_payer=identity.payer,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The payment may or may not have left: the node tells later.
            logger.warning("Bond lease payment outcome unknown ({})", type(exc).__name__)
            outcome = "paid"
        if outcome == "failed":
            logger.info("Payment to seller {} failed; nothing was spent", nick)
            _finish(record, "failed", payment_state="failed")
            return "next"
        deadline = float(quote.expires_at)
        if outcome == "paid":
            deadline = min(deadline, time.time() + NODE_DELIVERY_SECONDS)
        leased = await self._collect(
            record, transport, backend, deadline=deadline, renewable=renewable
        )
        if leased is not None:
            return leased
        if record.state == "paying":
            logger.warning(
                "The paid bond lease is not usable yet; a later attempt collects it, and no "
                "other lease is bought before its payment resolves"
            )
        return "stop"

    async def _collect(
        self,
        record: PurchaseRecord,
        transport: Any,
        backend: BlockchainBackend,
        *,
        deadline: float,
        renewable: Collection[str],
    ) -> LeasedBond | None:
        """Fetch (until ``deadline``, at least once) or reuse the delivery, verify, import.

        A delivery that proves unusable becomes "invalid" or "unimported"; a
        failing chain check keeps the record "paying" with its delivery, so a
        later attempt retries without the seller.
        """
        try:
            signed_listing = SignedDocument.model_validate(record.data["listing"])
            listing = signed_listing.verified(
                MarketListing, MarketListing.model_validate(signed_listing.body).seller_pubkey
            )
            signed_quote = SignedDocument.model_validate(record.data["quote"])
            quote = MarketQuote.model_validate(signed_quote.body)
            buyer_key = PrivateKey(bytes.fromhex(record.data["buyer_secret"]))
            certificate = CKey(bytes.fromhex(record.data["certificate_secret"]))
            delivery_request = DeliveryRequest(quote_id=quote.quote_id).model_dump(mode="json")
        except Exception:
            logger.error("Bond purchase record {} is damaged; keeping it", record.path.name)
            return None
        encrypted = record.data.get("delivery")
        while not isinstance(encrypted, str):
            try:
                response = await transport.request(
                    record.data["seller_nick"],
                    bytes.fromhex(listing.encryption_pubkey),
                    delivery_request,
                    REQUEST_SECONDS,
                )
                encrypted = response.get("delivery")
            except asyncio.CancelledError:
                raise
            except Exception:
                encrypted = None
            if isinstance(encrypted, str):
                # Kept before it is checked, so a later attempt needs no seller.
                record.advance("paying", delivery=encrypted)
            elif time.time() + POLL_INTERVAL_SECONDS >= deadline:
                return None
            else:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            package = open_delivery(encrypted, buyer_key, signed_quote)
            credential = package.verify()
            if (
                not isinstance(credential, BondCredential)
                or credential.bond.network != self.network
                or credential.cert_pubkey != bytes(certificate.pub).hex()
            ):
                raise MarketError("delivery is not this bond lease")
            delivered = verify_authorization(package.authorization)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The keys and the sealed delivery stay as evidence for a fault report.
            logger.error("The delivered bond lease is invalid: {}", exc)
            record.advance("invalid")
            return None
        try:
            await verify_market_bond(
                delivered, backend, self.network, int(time.time()), self._fault_cache
            )
            utxo = await backend.get_utxo(
                delivered.bond.outpoint.txid, delivered.bond.outpoint.vout
            )
            if utxo is None:
                raise MarketError("the bond is not on chain")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("The delivered bond lease does not verify on chain yet: {}", exc)
            return None
        try:
            await asyncio.to_thread(
                import_bond_credential,
                credential,
                certificate,
                self._data_dir,
                self._fingerprint,
                renewable=renewable,
            )
        except BondImportError as exc:
            logger.error("The delivered bond lease cannot be recorded: {}", exc)
            record.advance("unimported")
            return None
        # The certificate key stays through the period, so this lease is
        # handed out again instead of bought twice; _review drops it later.
        for key in ("buyer_secret", "delivery", "listing", "quote"):
            record.data.pop(key, None)
        outpoint = credential.bond.outpoint
        record.advance(
            "done",
            credential=credential.model_dump(mode="json"),
            cert_pubkey=credential.cert_pubkey,
            bond=f"{outpoint.txid}:{outpoint.vout}",
            cert_expiry=credential.cert_expiry,
        )
        logger.info(
            "Leased a fidelity bond of {} sats until period {}", utxo.value, delivered.period
        )
        return LeasedBond(
            credential=credential,
            certificate=certificate,
            value=utxo.value,
            confirmations=utxo.confirmations,
        )
