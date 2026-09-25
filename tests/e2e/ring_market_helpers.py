"""Regtest helpers that buy credentials with real Lightning payments.

The ring stack already runs Bitcoin Core, a directory server and four LND nodes.
This helper drives the production credential market end to end against them: it
funds a dedicated seller fidelity bond and an independent PoDLE backing UTXO
from the regtest funder wallet, buys the credential over real encrypted
:class:`MarketTransport` requests relayed by the ring directory, settles the
quote with an actual LND invoice paid over an existing channel, and imports the
delivered credential through the production verification and PoDLE pool.

Nothing here is mocked: the seller ledger is the production :class:`MarketStore`,
requests travel the real directory relay between two dedicated nick identities,
the settlement reference comes from the preimage the payer node observed
locally, and the import path is the taker's own :class:`PoDLEManager` after the
production backing-UTXO chain check.

Secret material (the PoDLE backing key, the invoice and its preimage, the
delivered credential) never leaves this module: it is not logged, not raised in
exceptions and never written outside ``work_dir``.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import httpx
from bitcointx.core.key import CKey
from jmcore.bitcoin import address_to_scriptpubkey_for_network, pubkey_to_p2wpkh_address
from jmcore.btc_script import derive_bond_address
from jmcore.credential_market import (
    BondCredential,
    BondReference,
    MarketAuthorization,
    MarketListing,
    PaymentTerms,
    SignedDocument,
    accept_listing,
    bond_resource,
    period_at_height,
    sign_document,
)
from jmcore.crypto import NickIdentity
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.market_store import MarketStore
from jmcore.podle import generate_podle
from jmcore.timenumber import get_nearest_valid_locktime
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.bond_registry import get_registry_path
from nacl.public import PrivateKey
from taker.market_cli import (
    MarketCLIError,
    _import_bond_credential,
    _verify_podle_chain,
)
from taker.market_payments import validate_payment_terms, verify_lightning_preimage
from taker.market_service import (
    DeliveryRequest,
    MarketService,
    QuoteRequest,
    accept_quote,
    make_bond_credential,
    open_delivery,
)
from taker.market_transport import ListingCallback, MarketTransport, ResponderCallback
from taker.podle_manager import PoDLEManager

NETWORK: Final[Literal["regtest"]] = "regtest"
FUNDER_WALLET = "ring-funder"
RPC_USER = os.getenv("RING_E2E_BITCOIN_RPC_USER", "test")
RPC_PASSWORD = os.getenv("RING_E2E_BITCOIN_RPC_PASSWORD", "test")

# One purchase, one fresh invoice. The price is deliberately tiny so it fits in
# any existing ring edge without disturbing the liquidity the ring test needs.
PRICE_SATS = 1_000
FEE_LIMIT_SATS = 100
INVOICE_EXPIRY_SECONDS = 1_800
QUOTE_TTL_SECONDS = 300

BOND_VALUE_SATS = 1_000_000
PODLE_VALUE_SATS = 5_000_000
# Above the taker_utxo_age the ring lifecycle configures, so the imported
# credential is immediately usable by a later CoinJoin.
FUNDING_CONFIRMATIONS = 6
BOND_LOCKTIME_SECONDS = 365 * 24 * 60 * 60
SETTLEMENT_POLL_SECONDS = 1.0
SETTLEMENT_POLL_ATTEMPTS = 15

DISCOVER_TIMEOUT_SECONDS = 5.0
DISCOVER_ATTEMPTS = 4
REQUEST_TIMEOUT_SECONDS = 20.0


class RingMarketError(RuntimeError):
    """Raised when the helper cannot complete a purchase.

    Messages are deliberately free of credential, key, invoice and preimage
    material so they stay safe to surface in a failing test report.
    """


@dataclass(frozen=True)
class PurchasedPoDLE:
    """Public evidence of one completed purchase.

    Holds only values a caller may assert on and publish: the commitment the
    taker will reveal, the backing outpoint that must never be spent by the
    CoinJoin, and the seller bond that authorized the sale.
    """

    commitment: str
    backing_txid: str
    backing_vout: int
    backing_value_sats: int
    seller_bond: BondReference


@dataclass(frozen=True)
class PurchasedBondRental:
    """Public rental evidence plus the private registry artifact to install.

    ``registry_path`` is an owner-only file beneath ``work_dir``. The renter's
    certificate private key stays there, while callers use the public bond
    identity and delegated certificate key to verify the maker proof.
    """

    bond: BondReference
    certificate_pubkey: str
    registry_path: Path


@dataclass(frozen=True)
class _ConfirmedOutput:
    txid: str
    vout: int
    value_sats: int
    blockheight: int


async def purchase_external_podle(
    *,
    rpc_url: str,
    directory_server: str,
    lncli: Callable[..., dict[str, Any]],
    payer_service: str,
    seller_service: str,
    buyer_data_dir: Path,
    work_dir: Path,
    socks_port: int = 29050,
    include_route_hints: bool = True,
) -> PurchasedPoDLE:
    """Buy one external PoDLE for real and import it into ``buyer_data_dir``.

    ``lncli(service, *args)`` must run ``lncli`` inside the named ring node and
    return its parsed JSON. ``payer_service`` and ``seller_service`` must
    already share a usable Lightning route: the seller issues a fresh invoice
    and the payer settles it over that route.

    ``directory_server`` is the ring directory the seller and the buyer relay
    their encrypted market messages through. Both sides use a dedicated nick
    identity, so this purchase is unlinkable to the CoinJoin identity of the
    taker that later spends the credential.

    The seller ledger and every other file this helper creates live under
    ``work_dir`` with owner-only permissions. The returned record carries only
    public evidence. ``include_route_hints`` retains LND's private-invoice
    behavior by default; direct-channel callers can disable it.
    """
    if buyer_data_dir.resolve() == work_dir.resolve():
        raise RingMarketError(
            "The seller work directory must not be the buyer data directory"
        )
    if payer_service == seller_service:
        raise RingMarketError("The payer and the seller must be different nodes")
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.chmod(0o700)

    client = httpx.AsyncClient(timeout=60)
    backend = DescriptorWalletBackend(
        rpc_url=rpc_url, rpc_user=RPC_USER, rpc_password=RPC_PASSWORD
    )
    try:
        await _require_regtest(client, rpc_url)
        bond_key = _new_secp_key()
        seller_key = _new_secp_key()
        locktime = get_nearest_valid_locktime(
            int(time.time()) + BOND_LOCKTIME_SECONDS, round_up=True
        )
        bond_address = derive_bond_address(bytes(bond_key.pub), locktime, NETWORK)

        podle_secret = secrets.token_bytes(32)
        podle_address = pubkey_to_p2wpkh_address(bytes(CKey(podle_secret).pub), NETWORK)

        bond_output, podle_output = await _fund_and_confirm(
            client,
            rpc_url,
            [
                (bond_address.address, BOND_VALUE_SATS),
                (podle_address, PODLE_VALUE_SATS),
            ],
        )

        height = await backend.get_block_height()
        bond_reference = BondReference(
            network=NETWORK,
            outpoint=ExternalPoDLEOutpoint(
                txid=bond_output.txid, vout=bond_output.vout
            ),
            pubkey=bytes(bond_key.pub).hex(),
            locktime=locktime,
        )
        authorization = sign_document(
            MarketAuthorization(
                bond=bond_reference,
                period=period_at_height(height),
                seller_pubkey=bytes(seller_key.pub).hex(),
            ),
            bond_key,
        )
        podle = _external_podle(podle_secret, podle_address, podle_output)

        invoice = await _fresh_invoice(
            lncli, seller_service, include_route_hints=include_route_hints
        )
        terms = PaymentTerms(
            rail="lightning", request=invoice.payment_request, amount_sats=PRICE_SATS
        )
        # MarketStore creates its database owner-only and keeps this directory
        # at 0700, so the sold credential never becomes world readable.
        store = MarketStore(work_dir / "ring-seller-market.sqlite")
        manager: PoDLEManager | None = None
        seller_transport: MarketTransport | None = None
        buyer_transport: MarketTransport | None = None
        try:
            now = int(time.time())
            store.add_inventory(
                "podle", podle.commitment, podle.model_dump(mode="json")
            )
            store.add_payment(
                terms,
                validate_payment_terms(terms, NETWORK, now, now + QUOTE_TTL_SECONDS),
            )
            # The transport seals requests to the very key the service signs and
            # decrypts with, so no plaintext market message exists on the wire.
            seller_encryption_key = PrivateKey.generate()
            service = MarketService(
                store,
                authorization,
                seller_key,
                seller_encryption_key,
                backend,
                products=["podle"],
                price_sats=PRICE_SATS,
                quote_ttl=QUOTE_TTL_SECONDS,
            )
            seller_transport = _market_transport(
                directory_server,
                socks_port,
                encryption_key=seller_encryption_key,
                listing_callback=service.listing,
                responder=service.respond,
            )
            buyer_transport = _market_transport(directory_server, socks_port)
            await seller_transport.start()
            await buyer_transport.start()

            buyer_key = PrivateKey.generate()
            _, listing = await _discover_listing(buyer_transport, seller_transport.nick)
            seller_encryption_pubkey = bytes.fromhex(listing.encryption_pubkey)
            request = QuoteRequest(
                request_id=secrets.token_hex(16),
                buyer_pubkey=bytes(buyer_key.public_key).hex(),
                product="podle",
                certificate_pubkey=None,
                rail="lightning",
                max_price_sats=PRICE_SATS,
            )
            response = await buyer_transport.request(
                seller_transport.nick,
                seller_encryption_pubkey,
                request.model_dump(mode="json"),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            quote_data = response.get("quote")
            if not isinstance(quote_data, dict):
                raise RingMarketError("The seller did not return a quote")
            quote_document = SignedDocument.model_validate(quote_data)
            # The buyer re-verifies the seller listing, the signed quote and the
            # advertised collateral against its own chain view before paying.
            quote = await accept_quote(
                quote_document, listing, request, backend, int(time.time())
            )

            preimage = await _settle_quoted_invoice(
                lncli, payer_service, seller_service, invoice, quote.payment.request
            )
            settlement_ref = verify_lightning_preimage(
                quote.payment, NETWORK, preimage, int(time.time()), quote.expires_at
            )
            store.attach_credential(quote.quote_id, podle.model_dump(mode="json"))
            store.finalize(
                quote.quote_id,
                seller_key,
                settlement_ref,
                int(time.time()),
                await backend.get_block_height(),
            )

            delivery = await buyer_transport.request(
                seller_transport.nick,
                seller_encryption_pubkey,
                DeliveryRequest(quote_id=quote.quote_id).model_dump(mode="json"),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            sealed = delivery.get("delivery")
            if not isinstance(sealed, str):
                raise RingMarketError(
                    "The seller did not deliver a package for the paid quote"
                )
            credential = open_delivery(sealed, buyer_key, quote_document).verify()
            if not isinstance(credential, ExternalPoDLE):
                raise RingMarketError(
                    "The delivered credential is not an external PoDLE"
                )
            await _verify_backing_utxo(credential, backend)

            manager = PoDLEManager(buyer_data_dir)
            if not manager.import_external(credential, seller_bond=bond_reference):
                raise RingMarketError(
                    "The purchased credential was already in the buyer pool"
                )
            if manager.external_count() < 1:
                raise RingMarketError(
                    "The buyer pool does not hold the purchased credential"
                )
            return PurchasedPoDLE(
                commitment=credential.commitment,
                backing_txid=credential.outpoint.txid,
                backing_vout=credential.outpoint.vout,
                backing_value_sats=podle_output.value_sats,
                seller_bond=bond_reference,
            )
        finally:
            if manager is not None:
                manager.close()
            if buyer_transport is not None:
                await buyer_transport.close()
            if seller_transport is not None:
                await seller_transport.close()
            store.close()
    finally:
        await backend.close()
        await client.aclose()


async def purchase_rented_bond(
    *,
    rpc_url: str,
    directory_server: str,
    lncli: Callable[..., dict[str, Any]],
    payer_service: str,
    seller_service: str,
    renter_wallet_fingerprint: str,
    work_dir: Path,
    socks_port: int = 29050,
    include_route_hints: bool = True,
) -> PurchasedBondRental:
    """Rent one fidelity bond and import its delegated certificate for a maker.

    The independent owner bond is funded and confirmed before it is listed as
    one period's exclusive market resource. The buyer's certificate private key
    is imported into an owner-only registry beneath ``work_dir`` through the
    same production path used by ``jm-market import``. The caller installs that
    resulting file into the maker container. ``include_route_hints`` retains
    LND's private-invoice behavior by default; direct-channel callers can
    disable it.
    """
    if payer_service == seller_service:
        raise RingMarketError("The payer and the seller must be different nodes")
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.chmod(0o700)

    client = httpx.AsyncClient(timeout=60)
    backend = DescriptorWalletBackend(
        rpc_url=rpc_url, rpc_user=RPC_USER, rpc_password=RPC_PASSWORD
    )
    try:
        await _require_regtest(client, rpc_url)
        bond_key = _new_secp_key()
        seller_key = _new_secp_key()
        locktime = get_nearest_valid_locktime(
            int(time.time()) + BOND_LOCKTIME_SECONDS, round_up=True
        )
        bond_address = derive_bond_address(bytes(bond_key.pub), locktime, NETWORK)
        (bond_output,) = await _fund_and_confirm(
            client, rpc_url, [(bond_address.address, BOND_VALUE_SATS)]
        )
        height = await backend.get_block_height()
        bond_reference = BondReference(
            network=NETWORK,
            outpoint=ExternalPoDLEOutpoint(
                txid=bond_output.txid, vout=bond_output.vout
            ),
            pubkey=bytes(bond_key.pub).hex(),
            locktime=locktime,
        )
        authorization = sign_document(
            MarketAuthorization(
                bond=bond_reference,
                period=period_at_height(height),
                seller_pubkey=bytes(seller_key.pub).hex(),
            ),
            bond_key,
        )
        invoice = await _fresh_invoice(
            lncli, seller_service, include_route_hints=include_route_hints
        )
        terms = PaymentTerms(
            rail="lightning", request=invoice.payment_request, amount_sats=PRICE_SATS
        )
        store = MarketStore(work_dir / "ring-bond-seller-market.sqlite")
        seller_transport: MarketTransport | None = None
        buyer_transport: MarketTransport | None = None
        try:
            now = int(time.time())
            store.add_inventory(
                "bond", bond_resource(bond_reference, period_at_height(height)), None
            )
            store.add_payment(
                terms,
                validate_payment_terms(terms, NETWORK, now, now + QUOTE_TTL_SECONDS),
            )
            seller_encryption_key = PrivateKey.generate()
            service = MarketService(
                store,
                authorization,
                seller_key,
                seller_encryption_key,
                backend,
                products=["bond"],
                price_sats=PRICE_SATS,
                quote_ttl=QUOTE_TTL_SECONDS,
            )
            seller_transport = _market_transport(
                directory_server,
                socks_port,
                encryption_key=seller_encryption_key,
                listing_callback=service.listing,
                responder=service.respond,
            )
            buyer_transport = _market_transport(directory_server, socks_port)
            await seller_transport.start()
            await buyer_transport.start()

            buyer_key = PrivateKey.generate()
            certificate_key = _new_secp_key()
            certificate_pubkey = bytes(certificate_key.pub).hex()
            _, listing = await _discover_listing(buyer_transport, seller_transport.nick)
            seller_encryption_pubkey = bytes.fromhex(listing.encryption_pubkey)
            request = QuoteRequest(
                request_id=secrets.token_hex(16),
                buyer_pubkey=bytes(buyer_key.public_key).hex(),
                product="bond",
                certificate_pubkey=certificate_pubkey,
                rail="lightning",
                max_price_sats=PRICE_SATS,
            )
            response = await buyer_transport.request(
                seller_transport.nick,
                seller_encryption_pubkey,
                request.model_dump(mode="json"),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            quote_data = response.get("quote")
            if not isinstance(quote_data, dict):
                raise RingMarketError("The seller did not return a quote")
            quote_document = SignedDocument.model_validate(quote_data)
            quote = await accept_quote(
                quote_document, listing, request, backend, int(time.time())
            )

            preimage = await _settle_quoted_invoice(
                lncli, payer_service, seller_service, invoice, quote.payment.request
            )
            settlement_ref = verify_lightning_preimage(
                quote.payment, NETWORK, preimage, int(time.time()), quote.expires_at
            )
            credential = make_bond_credential(
                quote.authorization, certificate_pubkey, bond_key
            )
            store.attach_credential(quote.quote_id, credential.model_dump(mode="json"))
            store.finalize(
                quote.quote_id,
                seller_key,
                settlement_ref,
                int(time.time()),
                await backend.get_block_height(),
            )

            delivery = await buyer_transport.request(
                seller_transport.nick,
                seller_encryption_pubkey,
                DeliveryRequest(quote_id=quote.quote_id).model_dump(mode="json"),
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            sealed = delivery.get("delivery")
            if not isinstance(sealed, str):
                raise RingMarketError(
                    "The seller did not deliver a package for the paid quote"
                )
            delivered = open_delivery(sealed, buyer_key, quote_document).verify()
            if not isinstance(delivered, BondCredential):
                raise RingMarketError("The delivered credential is not a fidelity bond")
            if not _import_bond_credential(
                delivered,
                certificate_key,
                work_dir / "renter",
                renter_wallet_fingerprint,
            ):
                raise RingMarketError(
                    "The rented bond was already in the maker registry"
                )
            registry_path = get_registry_path(
                work_dir / "renter", renter_wallet_fingerprint
            )
            if not registry_path.is_file() or registry_path.stat().st_size == 0:
                raise RingMarketError("The rented bond registry was not created")
            return PurchasedBondRental(
                bond=delivered.bond,
                certificate_pubkey=delivered.cert_pubkey,
                registry_path=registry_path,
            )
        finally:
            if buyer_transport is not None:
                await buyer_transport.close()
            if seller_transport is not None:
                await seller_transport.close()
            store.close()
    finally:
        await backend.close()
        await client.aclose()


@dataclass(frozen=True)
class _Invoice:
    payment_request: str
    payment_hash: str


def _market_transport(
    directory_server: str,
    socks_port: int,
    *,
    encryption_key: PrivateKey | None = None,
    listing_callback: ListingCallback | None = None,
    responder: ResponderCallback | None = None,
) -> MarketTransport:
    """One market endpoint on a fresh nick, relaying through the ring directory.

    Every purchase uses its own identities, so neither side is linkable to the
    CoinJoin nick of the taker that later spends the credential. No direct
    location is advertised and no listener is opened: requests take the real
    directory relay.
    """
    return MarketTransport(
        directory_servers=[directory_server],
        network=NETWORK,
        nick_identity=NickIdentity(),
        socks_port=socks_port,
        connection_timeout=REQUEST_TIMEOUT_SECONDS,
        encryption_private_key=encryption_key,
        listing_callback=listing_callback,
        responder=responder,
        allow_clearnet_connections=True,
    )


async def _discover_listing(
    buyer: MarketTransport, seller_nick: str
) -> tuple[SignedDocument, MarketListing]:
    """Collect and verify the seller listing the directory actually relayed."""
    for attempt in range(DISCOVER_ATTEMPTS):
        for sender, raw in await buyer.discover(timeout=DISCOVER_TIMEOUT_SECONDS):
            if sender != seller_nick:
                continue
            return accept_listing(raw, NETWORK, int(time.time()))
        if attempt + 1 < DISCOVER_ATTEMPTS:
            await asyncio.sleep(SETTLEMENT_POLL_SECONDS)
    raise RingMarketError("The seller listing was not discovered on the directory")


async def _verify_backing_utxo(
    record: ExternalPoDLE, backend: DescriptorWalletBackend
) -> None:
    """Run the production chain check the market import path always applies."""
    try:
        await _verify_podle_chain(record, backend)
    except MarketCLIError:
        # The original error can carry credential fields, so it is not chained.
        raise RingMarketError(
            "The purchased PoDLE backing UTXO failed chain validation"
        ) from None


def _new_secp_key() -> CKey:
    """An independent fixture key, unrelated to any wallet under test."""
    while True:
        try:
            return CKey(secrets.token_bytes(32))
        except ValueError:
            continue


async def _rpc(
    client: httpx.AsyncClient,
    rpc_url: str,
    method: str,
    params: list[Any] | None = None,
    *,
    wallet: str | None = None,
) -> Any:
    url = rpc_url if wallet is None else f"{rpc_url.rstrip('/')}/wallet/{wallet}"
    response = await client.post(
        url,
        auth=(RPC_USER, RPC_PASSWORD),
        json={
            "jsonrpc": "2.0",
            "id": "ring-market",
            "method": method,
            "params": params or [],
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RingMarketError(f"Bitcoin RPC {method} failed: {payload['error']}")
    return payload["result"]


async def _require_regtest(client: httpx.AsyncClient, rpc_url: str) -> None:
    """Refuse to fund anything unless the node really is a regtest node."""
    info = await _rpc(client, rpc_url, "getblockchaininfo")
    chain = str(info.get("chain", ""))
    if chain != NETWORK:
        raise RingMarketError(f"Refusing to fund market fixtures on chain {chain!r}")


async def _fund_and_confirm(
    client: httpx.AsyncClient, rpc_url: str, payments: list[tuple[str, int]]
) -> list[_ConfirmedOutput]:
    """Pay every ``(address, sats)`` from the funder wallet and confirm them together."""
    txids = [
        str(
            await _rpc(
                client,
                rpc_url,
                "sendtoaddress",
                [address, float(Decimal(amount_sats) / Decimal(100_000_000))],
                wallet=FUNDER_WALLET,
            )
        )
        for address, amount_sats in payments
    ]
    mining_address = str(
        await _rpc(client, rpc_url, "getnewaddress", [], wallet=FUNDER_WALLET)
    )
    await _rpc(
        client, rpc_url, "generatetoaddress", [FUNDING_CONFIRMATIONS, mining_address]
    )
    return [
        await _confirmed_output(client, rpc_url, txid, address, amount_sats)
        for txid, (address, amount_sats) in zip(txids, payments, strict=True)
    ]


async def _confirmed_output(
    client: httpx.AsyncClient, rpc_url: str, txid: str, address: str, amount_sats: int
) -> _ConfirmedOutput:
    raw = await _rpc(client, rpc_url, "getrawtransaction", [txid, True])
    expected_script = address_to_scriptpubkey_for_network(address, NETWORK).hex()
    matching = [
        output
        for output in raw.get("vout", [])
        if output.get("scriptPubKey", {}).get("hex") == expected_script
        and int(Decimal(str(output.get("value"))) * Decimal(100_000_000)) == amount_sats
    ]
    if len(matching) != 1:
        raise RingMarketError("The funding transaction has no unique matching output")
    header = await _rpc(client, rpc_url, "getblockheader", [str(raw["blockhash"])])
    return _ConfirmedOutput(
        txid=txid,
        vout=int(matching[0]["n"]),
        value_sats=amount_sats,
        blockheight=int(header["height"]),
    )


def _external_podle(
    private_key: bytes, address: str, output: _ConfirmedOutput
) -> ExternalPoDLE:
    """Build the strict external record the market sells for this backing UTXO."""
    proof = generate_podle(private_key, f"{output.txid}:{output.vout}", index=0)
    return ExternalPoDLE(
        version=1,
        network=NETWORK,
        outpoint=ExternalPoDLEOutpoint(txid=output.txid, vout=output.vout),
        P=proof.p.hex(),
        P2=proof.p2.hex(),
        sig=proof.sig.hex(),
        e=proof.e.hex(),
        commitment=proof.commitment.hex(),
        index=0,
        scriptpubkey=address_to_scriptpubkey_for_network(address, NETWORK).hex(),
        blockheight=output.blockheight,
    )


async def _lncli(
    lncli: Callable[..., dict[str, Any]], service: str, *args: str
) -> dict[str, Any]:
    """Run one ``lncli`` command, discarding output that may carry secrets."""
    try:
        return await asyncio.to_thread(lncli, service, *args)
    except (subprocess.SubprocessError, OSError, ValueError, json.JSONDecodeError):
        # The original error can echo the invoice or the preimage, so neither the
        # message nor the cause is propagated.
        raise RingMarketError(f"lncli {args[0]} failed on {service}") from None


async def _fresh_invoice(
    lncli: Callable[..., dict[str, Any]],
    seller_service: str,
    *,
    include_route_hints: bool = True,
) -> _Invoice:
    """Ask the seller node for a fresh market invoice at the market price."""
    invoice_args = [
        "addinvoice",
        f"--amt={PRICE_SATS}",
        f"--expiry={INVOICE_EXPIRY_SECONDS}",
        f"--memo=external-podle-{secrets.token_hex(8)}",
    ]
    if include_route_hints:
        invoice_args.append("--private")
    created = await _lncli(
        lncli,
        seller_service,
        *invoice_args,
    )
    payment_request = str(created.get("payment_request", ""))
    payment_hash = str(created.get("r_hash", "")).lower()
    if not payment_request or len(payment_hash) != 64:
        raise RingMarketError(f"{seller_service} did not issue a usable invoice")
    if not include_route_hints:
        decoded = await _lncli(lncli, seller_service, "decodepayreq", payment_request)
        if decoded.get("route_hints") != []:
            raise RingMarketError(
                f"{seller_service} direct market invoice contains route hints"
            )
    return _Invoice(payment_request=payment_request, payment_hash=payment_hash)


async def _settle_quoted_invoice(
    lncli: Callable[..., dict[str, Any]],
    payer_service: str,
    seller_service: str,
    invoice: _Invoice,
    quoted_request: str,
) -> bytes:
    """Pay the quoted invoice for real and return the locally observed preimage.

    The payer node is the one that learns the preimage, so the buyer never has
    to trust a seller-supplied secret. The payment is cross-checked against the
    amount and payment hash on both sides before it is accepted as settlement.
    """
    if quoted_request != invoice.payment_request:
        raise RingMarketError(
            "The quote does not reference the invoice that was issued"
        )

    payment = await _lncli(
        lncli,
        payer_service,
        "payinvoice",
        "--json",
        "--force",
        f"--fee_limit={FEE_LIMIT_SATS}",
        quoted_request,
    )
    if payment.get("payment_error"):
        raise RingMarketError(f"{payer_service} could not pay the market invoice")
    if str(payment.get("status", "")).upper() not in {"SUCCEEDED", "2"}:
        raise RingMarketError(f"{payer_service} did not reach a settled payment status")
    if str(payment.get("payment_hash", "")).lower() != invoice.payment_hash:
        raise RingMarketError("The payment settled a different invoice")
    if int(payment.get("value_sat", 0)) != PRICE_SATS:
        raise RingMarketError("The payment amount does not match the market price")

    await _await_settled_invoice(lncli, seller_service, invoice.payment_hash)
    preimage = bytes.fromhex(str(payment.get("payment_preimage", "")))
    if len(preimage) != 32:
        raise RingMarketError(f"{payer_service} did not report a usable payment proof")
    return preimage


async def _await_settled_invoice(
    lncli: Callable[..., dict[str, Any]], seller_service: str, payment_hash: str
) -> None:
    """Block until the seller node itself reports the invoice as paid in full."""
    for attempt in range(SETTLEMENT_POLL_ATTEMPTS):
        lookup = await _lncli(lncli, seller_service, "lookupinvoice", payment_hash)
        settled = (
            bool(lookup.get("settled"))
            or str(lookup.get("state", "")).upper() == "SETTLED"
        )
        if settled:
            if int(lookup.get("amt_paid_sat", 0)) != PRICE_SATS:
                raise RingMarketError("The seller was paid a different amount")
            return
        if attempt + 1 < SETTLEMENT_POLL_ATTEMPTS:
            await asyncio.sleep(SETTLEMENT_POLL_SECONDS)
    raise RingMarketError(f"{seller_service} never reported the invoice as settled")
