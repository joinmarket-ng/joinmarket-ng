"""File-oriented CLI wiring for the native credential market.

The command intentionally has no wallet creation, payment sending, or Bitcoin
transaction broadcast path. Secret values are read only from local files.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import secrets
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from bitcointx.core.key import CKey, CPubKey  # type: ignore[import-not-found]
from jmcore.btc_script import derive_bond_address
from jmcore.cli_common import resolve_backend_settings, setup_cli
from jmcore.credential_market import (
    MAX_MARKET_BYTES,
    BondCredential,
    BondReference,
    CredentialPackage,
    FaultProof,
    MarketAuthorization,
    MarketError,
    MarketListing,
    MarketQuote,
    Network,
    PaymentTerms,
    Product,
    SignedDocument,
    bond_resource,
    canonical,
    decode_document,
    sign_document,
    verify_allocation,
    verify_authorization,
)
from jmcore.crypto import NickIdentity
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.market_faults import MarketFaultCache
from jmcore.podle import generate_podle
from jmcore.protocol import JM_VERSION
from jmcore.secure_files import (
    atomic_write_private,
    ensure_private_directory,
    ensure_private_file,
    read_private_file,
)
from jmcore.settings import JoinMarketSettings
from nacl.public import PrivateKey, SealedBox

from taker.market_payments import (
    payment_uri,
    validate_payment_terms,
    verify_lightning_preimage,
    verify_onchain_payment,
)
from taker.market_service import (
    DeliveryRequest,
    QuoteRequest,
    accept_listing,
    accept_quote,
    make_bond_credential,
    open_delivery,
    verify_market_bond,
)
from taker.market_store import MarketStore
from taker.market_transport import MAX_LISTING_BYTES, MAX_LISTINGS, MarketTransport
from taker.podle_manager import PoDLEManager

_MAX_KEY_BYTES = 4096
_MAX_DELIVERY_BYTES = 22_000
_MAX_PRICE_SATS = 2_100_000_000_000_000
_MAX_DISCOVERY_OUTPUT_BYTES = MAX_LISTINGS * (MAX_LISTING_BYTES + 128)
_KEY_BUNDLE_FIELDS = frozenset(
    {"version", "seller_signing_key", "encryption_key", "renter_certificate_key"}
)


class MarketCLIError(Exception):
    """A command failed without exposing supplied data in its error text."""


def _read_limited(path: Path, limit: int = MAX_MARKET_BYTES) -> bytes:
    try:
        with path.open("rb") as source:
            raw = source.read(limit + 1)
    except OSError as exc:
        raise MarketCLIError("could not read input file") from exc
    if len(raw) > limit:
        raise MarketCLIError("input file exceeds market size limit")
    return raw


def _read_secret(path: Path, limit: int = _MAX_KEY_BYTES) -> bytes:
    try:
        if path.is_symlink() or path.stat().st_size > limit:
            raise OSError("invalid secret file")
        raw = read_private_file(path)
    except OSError as exc:
        raise MarketCLIError("could not read private key file") from exc
    if len(raw) > limit:
        raise MarketCLIError("private key file exceeds size limit")
    return raw


def _read_preimage(path: Path) -> bytes:
    raw = _read_secret(path, 128)
    if len(raw) == 32:
        return raw
    return _secret_bytes_from_file(path, ("preimage",))


def _document(path: Path) -> dict[str, Any]:
    try:
        return decode_document(_read_limited(path))
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("invalid market document") from exc


def _signed_document(path: Path) -> SignedDocument:
    try:
        return SignedDocument.model_validate(_document(path))
    except (ValueError, TypeError) as exc:
        raise MarketCLIError("invalid signed market document") from exc


def _structural_credential_package(path: Path) -> CredentialPackage:
    try:
        return CredentialPackage.model_validate(_document(path))
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("invalid credential package") from exc


def _credential_package(path: Path) -> CredentialPackage:
    package = _structural_credential_package(path)
    try:
        package.verify()
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("invalid credential package") from exc
    return package


def _serialized(value: dict[str, Any] | list[Any]) -> bytes:
    try:
        if isinstance(value, dict):
            return canonical(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "ascii"
        )
    except (MarketError, TypeError, ValueError) as exc:
        raise MarketCLIError("could not encode market output") from exc


def _write_output(path: Path, data: bytes, *, idempotent: bool) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise MarketCLIError("refusing to replace output symlink")
        existing = _read_limited(path, max(len(data), MAX_MARKET_BYTES))
        if idempotent and existing == data:
            ensure_private_file(path)
            return
        raise MarketCLIError("refusing to overwrite existing output")
    try:
        atomic_write_private(path, data)
    except OSError as exc:
        raise MarketCLIError("could not write private output") from exc


def _emit_or_write(value: dict[str, Any] | list[Any], output: Path | None) -> None:
    data = _serialized(value)
    if output is None:
        sys.stdout.write(data.decode("ascii") + "\n")
        return
    _write_output(output, data, idempotent=True)


def _secret_bytes_from_file(path: Path, fields: Sequence[str] = ()) -> bytes:
    raw = _read_secret(path).strip()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MarketCLIError("private key file is not valid ASCII") from exc
    if len(text) == 64:
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise MarketCLIError("private key file is invalid") from exc
    try:
        data = decode_document(raw)
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("private key file is invalid") from exc
    value: object | None = data.get("secret_key")
    if value is None:
        for field in fields:
            if field in data:
                value = data[field]
                break
    if not isinstance(value, str) or len(value) != 64:
        raise MarketCLIError("private key file is invalid")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise MarketCLIError("private key file is invalid") from exc


def _secp_key(path: Path, fields: Sequence[str] = ()) -> CKey:
    try:
        return CKey(_secret_bytes_from_file(path, fields))
    except (MarketCLIError, ValueError) as exc:
        if isinstance(exc, MarketCLIError):
            raise
        raise MarketCLIError("private secp256k1 key is invalid") from exc


def _encryption_key(path: Path, fields: Sequence[str] = ()) -> PrivateKey:
    try:
        return PrivateKey(_secret_bytes_from_file(path, fields))
    except (MarketCLIError, ValueError, TypeError) as exc:
        if isinstance(exc, MarketCLIError):
            raise
        raise MarketCLIError("private encryption key is invalid") from exc


def _key_bundle(path: Path) -> dict[str, str]:
    try:
        data = decode_document(_read_secret(path))
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("key bundle is invalid") from exc
    if set(data) != _KEY_BUNDLE_FIELDS or data.get("version") != 1:
        raise MarketCLIError("key bundle is invalid")
    values: dict[str, str] = {}
    for field in _KEY_BUNDLE_FIELDS - {"version"}:
        value = data.get(field)
        if not isinstance(value, str) or len(value) != 64:
            raise MarketCLIError("key bundle is invalid")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise MarketCLIError("key bundle is invalid") from exc
        values[field] = value
    return values


def _bundle_secp(bundle: dict[str, str], field: str) -> CKey:
    try:
        return CKey(bytes.fromhex(bundle[field]))
    except (KeyError, ValueError) as exc:
        raise MarketCLIError("key bundle is invalid") from exc


def _bundle_encryption(bundle: dict[str, str]) -> PrivateKey:
    try:
        return PrivateKey(bytes.fromhex(bundle["encryption_key"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise MarketCLIError("key bundle is invalid") from exc


def _public_key(path: Path, fields: Sequence[str]) -> str:
    raw = _read_limited(path, _MAX_KEY_BYTES).strip()
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MarketCLIError("public key file is invalid") from exc
    value: object = text
    if text.startswith("{"):
        try:
            data = decode_document(raw)
        except (MarketError, ValueError, TypeError) as exc:
            raise MarketCLIError("public key file is invalid") from exc
        value = next((data[field] for field in fields if field in data), None)
    if not isinstance(value, str) or len(value) != 66:
        raise MarketCLIError("public key file is invalid")
    try:
        public = bytes.fromhex(value)
    except ValueError as exc:
        raise MarketCLIError("public key file is invalid") from exc
    if value != value.lower() or not CPubKey(public).is_fullyvalid():
        raise MarketCLIError("public key file is invalid")
    return value


def _network(settings: JoinMarketSettings) -> str:
    return (settings.network_config.bitcoin_network or settings.network_config.network).value


def _market_dir(settings: JoinMarketSettings) -> Path:
    path = settings.get_data_dir() / "market"
    try:
        ensure_private_directory(path)
    except (OSError, ValueError) as exc:
        raise MarketCLIError("could not initialize private market state") from exc
    return path


def _settings(args: argparse.Namespace) -> JoinMarketSettings:
    return setup_cli(None, data_dir=args.data_dir, config_file=args.config_file)


def _new_backend(settings: JoinMarketSettings) -> Any:
    resolved = resolve_backend_settings(settings, data_dir=settings.get_data_dir())
    if resolved.backend_type == "descriptor_wallet":
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        # This name is never created or loaded by this CLI. get_utxo falls back
        # to Core's non-wallet gettxout RPC for all market verification.
        return DescriptorWalletBackend(
            rpc_url=resolved.rpc_url,
            rpc_user=resolved.rpc_user,
            rpc_password=resolved.rpc_password,
            wallet_name="jm_market_readonly",
        )
    if resolved.backend_type == "neutrino":
        from jmwallet.backends.neutrino import NeutrinoBackend

        return NeutrinoBackend(
            neutrino_url=resolved.neutrino_url,
            network=resolved.bitcoin_network,
            data_dir=str(resolved.data_dir / "neutrino"),
            scan_start_height=resolved.scan_start_height,
            add_peers=resolved.neutrino_add_peers,
            tls_cert_path=resolved.neutrino_tls_cert,
            auth_token=resolved.neutrino_auth_token,
            include_mempool=settings.bitcoin.neutrino_include_mempool,
            fee_estimate_url=resolved.fee_estimate_url,
            fee_estimate_proxy=resolved.fee_estimate_proxy,
        )
    raise MarketCLIError("configured blockchain backend is unsupported")


async def _close_backend(backend: Any) -> None:
    with suppress(Exception):
        await backend.close()


def _new_transport(
    settings: JoinMarketSettings,
    *,
    encryption_key: PrivateKey | None = None,
    listing_callback: Any = None,
    responder: Any = None,
    direct_location: str = "NOT-SERVING-ONION",
    listen_host: str | None = None,
    listen_port: int = 0,
    fault_cache: MarketFaultCache | None = None,
) -> MarketTransport:
    cache = fault_cache if fault_cache is not None else MarketFaultCache(settings.get_data_dir())

    def receive_fault(raw: bytes) -> None:
        cache.ingest(raw)

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
        encryption_private_key=encryption_key,
        listing_callback=listing_callback,
        responder=responder,
        allow_clearnet_connections=settings.network_config.allow_clearnet_connections,
        direct_location=direct_location,
        listen_host=listen_host,
        listen_port=listen_port,
        on_fault=receive_fault,
    )


def _authorization(path: Path) -> tuple[SignedDocument, MarketAuthorization]:
    document = _signed_document(path)
    try:
        return document, verify_authorization(document)
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("market authorization is invalid") from exc


def _quote(path: Path) -> SignedDocument:
    data = _document(path)
    if "quote" in data:
        data = data["quote"]
    try:
        return SignedDocument.model_validate(data)
    except (ValueError, TypeError) as exc:
        raise MarketCLIError("quote document is invalid") from exc


def _verified_quote(document: SignedDocument) -> tuple[MarketAuthorization, MarketQuote]:
    try:
        candidate = MarketQuote.model_validate(document.body)
        authority = verify_authorization(candidate.authorization)
        return authority, document.verified(MarketQuote, authority.seller_pubkey)
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("quote document is invalid") from exc


def _listing(
    path: Path, network: str, *, allow_expired: bool = False
) -> tuple[str, SignedDocument, MarketListing]:
    data = _document(path)
    listings = data.get("listings")
    if isinstance(listings, list) and len(listings) == 1 and isinstance(listings[0], dict):
        data = listings[0]
    sender = data.get("seller_nick")
    signed_data = data.get("listing", data)
    if not isinstance(sender, str):
        sender = data.get("sender")
    if not isinstance(sender, str):
        raise MarketCLIError("listing file does not identify a seller")
    try:
        signed = SignedDocument.model_validate(signed_data)
        if allow_expired:
            candidate = MarketListing.model_validate(signed.body)
            listing = signed.verified(MarketListing, candidate.seller_pubkey)
            if listing.network != network:
                raise MarketError("Listing network mismatch")
        else:
            _, listing = accept_listing(canonical(signed), network, int(time.time()))
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("listing document is invalid") from exc
    return sender, signed, listing


def _parse_products(value: str) -> list[Product]:
    products = [product.strip() for product in value.split(",") if product.strip()]
    if not products or len(products) > 2 or len(set(products)) != len(products):
        raise MarketCLIError("products are invalid")
    if any(product not in ("podle", "bond") for product in products):
        raise MarketCLIError("products are invalid")
    return cast(list[Product], products)


def _command_keygen(args: argparse.Namespace) -> None:
    while True:
        try:
            seller = CKey(secrets.token_bytes(32))
            certificate = CKey(secrets.token_bytes(32))
        except ValueError:
            continue
        encryption = PrivateKey.generate()
        if len({seller.secret_bytes, certificate.secret_bytes, bytes(encryption)}) == 3:
            break
    _write_output(
        args.output,
        _serialized(
            {
                "version": 1,
                "seller_signing_key": seller.secret_bytes.hex(),
                "encryption_key": bytes(encryption).hex(),
                "renter_certificate_key": certificate.secret_bytes.hex(),
            }
        ),
        idempotent=False,
    )


def _command_public(args: argparse.Namespace) -> None:
    bundle = _key_bundle(args.keys)
    seller = _bundle_secp(bundle, "seller_signing_key")
    certificate = _bundle_secp(bundle, "renter_certificate_key")
    encryption = _bundle_encryption(bundle)
    _emit_or_write(
        {
            "version": 1,
            "seller_pubkey": bytes(seller.pub).hex(),
            "encryption_pubkey": bytes(encryption.public_key).hex(),
            "renter_certificate_pubkey": bytes(certificate.pub).hex(),
        },
        args.output,
    )


def _command_authorize(args: argparse.Namespace) -> None:
    try:
        bond = BondReference.model_validate(_document(args.bond_ref))
        seller_pubkey = _public_key(args.seller_pub, ("seller_pubkey", "seller_signing_pubkey"))
        owner = _secp_key(args.owner_key)
        if bytes(owner.pub).hex() != bond.pubkey:
            raise MarketCLIError("owner key does not authorize the bond reference")
        authorization = sign_document(
            MarketAuthorization(bond=bond, period=args.period, seller_pubkey=seller_pubkey), owner
        )
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("could not create market authorization") from exc
    _write_output(args.output, _serialized(authorization.model_dump(mode="json")), idempotent=True)


def _command_sign_bond(args: argparse.Namespace) -> None:
    authorization, _ = _authorization(args.authorization)
    certificate = _public_key(
        args.certificate_pub,
        ("renter_certificate_pubkey", "certificate_pubkey"),
    )
    credential = make_bond_credential(authorization, certificate, _secp_key(args.owner_key))
    _write_output(args.output, _serialized(credential.model_dump(mode="json")), idempotent=True)


def _command_export_podle(args: argparse.Namespace) -> None:
    metadata = _document(args.metadata)
    try:
        outpoint = ExternalPoDLEOutpoint.model_validate(metadata["outpoint"])
        network = metadata["network"]
        scriptpubkey = metadata["scriptpubkey"]
        blockheight = metadata["blockheight"]
        index = metadata.get("index", 0)
        if not isinstance(network, str) or not isinstance(scriptpubkey, str):
            raise ValueError
        proof = generate_podle(_secp_key(args.owner_key).secret_bytes, outpoint.to_string(), index)
        record = ExternalPoDLE(
            version=1,
            network=cast(Network, network),
            outpoint=outpoint,
            P=proof.p.hex(),
            P2=proof.p2.hex(),
            sig=proof.sig.hex(),
            e=proof.e.hex(),
            commitment=proof.commitment.hex(),
            index=index,
            scriptpubkey=scriptpubkey,
            blockheight=blockheight,
        )
    except (KeyError, MarketCLIError, ValueError, TypeError) as exc:
        if isinstance(exc, MarketCLIError):
            raise
        raise MarketCLIError("PoDLE metadata is invalid") from exc
    _write_output(args.output, _serialized(record.model_dump(mode="json")), idempotent=True)


def _seller_store(settings: JoinMarketSettings) -> MarketStore:
    return MarketStore(_market_dir(settings) / "seller.sqlite")


def _command_seller_add_inventory(args: argparse.Namespace) -> None:
    settings = _settings(args)
    store = _seller_store(settings)
    try:
        if args.authorization is not None:
            _, authority = _authorization(args.authorization)
            store.add_inventory("bond", bond_resource(authority.bond, authority.period), None)
        else:
            credential = _document(args.credential)
            try:
                podle = ExternalPoDLE.model_validate(credential)
                store.add_inventory("podle", podle.commitment, podle.model_dump(mode="json"))
            except ValueError:
                bond = BondCredential.model_validate(credential)
                bond.verify()
                store.add_inventory(
                    "bond",
                    bond_resource(bond.bond, bond.cert_expiry - 1),
                    bond.model_dump(mode="json"),
                )
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("inventory credential is invalid") from exc
    finally:
        store.close()


def _command_seller_add_payment(args: argparse.Namespace) -> None:
    settings = _settings(args)
    if not 1 <= args.ttl <= 900:
        raise MarketCLIError("payment TTL is invalid")
    try:
        terms = PaymentTerms.model_validate(_document(args.terms))
        now = int(time.time())
        payment_id = validate_payment_terms(terms, _network(settings), now, now + args.ttl)
    except (ValueError, TypeError) as exc:
        raise MarketCLIError("payment terms are invalid") from exc
    store = _seller_store(settings)
    try:
        store.add_payment(terms, payment_id)
    finally:
        store.close()
    _emit_or_write({"payment_id": payment_id}, args.output)


def _command_seller_pending(args: argparse.Namespace) -> None:
    settings = _settings(args)
    store = _seller_store(settings)
    try:
        quotes = [quote.model_dump(mode="json") for quote in store.pending(int(time.time()))]
    finally:
        store.close()
    _emit_or_write({"quotes": quotes}, args.output)


def _command_seller_attach(args: argparse.Namespace) -> None:
    settings = _settings(args)
    credential = _document(args.credential)
    store = _seller_store(settings)
    try:
        store.attach_credential(args.quote_id, credential)
    finally:
        store.close()


async def _settle(args: argparse.Namespace, settings: JoinMarketSettings) -> CredentialPackage:
    store = _seller_store(settings)
    backend = _new_backend(settings)
    try:
        document = store.get_quote(args.quote_id)
        _, quote = _verified_quote(document)
        now = int(time.time())
        height = await backend.get_block_height()
        quote.check(
            network=_network(settings),
            height=height,
            now=now,
            max_price_sats=_MAX_PRICE_SATS,
        )
        validate_payment_terms(quote.payment, _network(settings), now, quote.expires_at)
        if args.onchain_outpoint is not None:
            if quote.payment.rail != "onchain":
                raise MarketCLIError("on-chain reference cannot settle a Lightning quote")
            settlement_ref = await verify_onchain_payment(
                backend, quote.payment, _network(settings), args.onchain_outpoint
            )
        else:
            if quote.payment.rail != "lightning" or not args.acknowledge_ln_settlement:
                raise MarketCLIError("Lightning settlement acknowledgement is required")
            settlement_ref = verify_lightning_preimage(
                quote.payment,
                _network(settings),
                _read_preimage(args.preimage_file),
                now,
                quote.expires_at,
            )
        bundle = _key_bundle(args.keys)
        return store.finalize(
            args.quote_id,
            _bundle_secp(bundle, "seller_signing_key"),
            settlement_ref,
            now,
            height,
        )
    finally:
        store.close()
        await _close_backend(backend)


def _command_seller_settle(args: argparse.Namespace) -> None:
    package = asyncio.run(_settle(args, _settings(args)))
    _write_output(args.output, _serialized(package.model_dump(mode="json")), idempotent=True)


def _command_seller_export(args: argparse.Namespace) -> None:
    store = _seller_store(_settings(args))
    try:
        package = store.get_package(args.quote_id)
    finally:
        store.close()
    if package is None:
        raise MarketCLIError("seller has no finalized package for this quote")
    _write_output(args.output, _serialized(package.model_dump(mode="json")), idempotent=True)


async def _serve(args: argparse.Namespace, settings: JoinMarketSettings) -> None:
    authorization, authority = _authorization(args.authorization)
    if authority.bond.network != _network(settings):
        raise MarketCLIError("authorization network differs from configured network")
    bundle = _key_bundle(args.keys)
    store = _seller_store(settings)
    backend = _new_backend(settings)
    transport: MarketTransport | None = None
    fault_cache = MarketFaultCache(settings.get_data_dir())
    try:
        from taker.market_service import MarketService

        service = MarketService(
            store,
            authorization,
            _bundle_secp(bundle, "seller_signing_key"),
            _bundle_encryption(bundle),
            backend,
            products=_parse_products(args.products),
            price_sats=args.price_sats,
            quote_ttl=args.quote_ttl,
            fault_cache=fault_cache,
        )
        transport = _new_transport(
            settings,
            encryption_key=_bundle_encryption(bundle),
            listing_callback=service.listing,
            responder=service.respond,
            direct_location=args.direct_location,
            listen_host=args.listen_host,
            listen_port=args.listen_port,
            fault_cache=fault_cache,
        )
        await transport.start()
        await asyncio.Event().wait()
    finally:
        if transport is not None:
            await transport.close()
        store.close()
        await _close_backend(backend)


def _command_serve(args: argparse.Namespace) -> None:
    if not 1 <= args.quote_ttl <= 86400 or args.price_sats < 1:
        raise MarketCLIError("serve price or quote TTL is invalid")
    asyncio.run(_serve(args, _settings(args)))


async def _discover(args: argparse.Namespace, settings: JoinMarketSettings) -> list[dict[str, Any]]:
    transport = _new_transport(settings)
    try:
        await transport.start()
        found: list[dict[str, Any]] = []
        for sender, raw in await transport.discover(args.timeout):
            try:
                signed, _ = accept_listing(raw, _network(settings), int(time.time()))
            except (MarketError, ValueError, TypeError):
                continue
            found.append({"seller_nick": sender, "listing": signed.model_dump(mode="json")})
        return found
    finally:
        await transport.close()


def _command_discover(args: argparse.Namespace) -> None:
    listings = asyncio.run(_discover(args, _settings(args)))
    if len(listings) > MAX_LISTINGS:
        raise MarketCLIError("discovery result exceeds listing limit")
    try:
        data = json.dumps(
            {"listings": listings}, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise MarketCLIError("could not encode discovery output") from exc
    if len(data) > _MAX_DISCOVERY_OUTPUT_BYTES:
        raise MarketCLIError("discovery output exceeds listing limit")
    if args.output is None:
        sys.stdout.write(data.decode("ascii") + "\n")
    else:
        _write_output(args.output, data, idempotent=True)


def _request_payload(
    path: Path,
    *,
    listing: MarketListing,
    buyer_key: PrivateKey,
    product: str,
    rail: str,
    max_price_sats: int,
    certificate_pubkey: str | None,
) -> QuoteRequest:
    expected = {
        "buyer_pubkey": bytes(buyer_key.public_key).hex(),
        "product": product,
        "certificate_pubkey": certificate_pubkey,
        "rail": rail,
        "max_price_sats": max_price_sats,
    }
    seller = {
        "seller_pubkey": listing.seller_pubkey,
        "network": listing.network,
        "encryption_pubkey": listing.encryption_pubkey,
    }
    if path.exists() or path.is_symlink():
        data = _document(path)
        try:
            if set(data) != {"version", "seller", "request"}:
                raise ValueError
            if data["version"] != 2 or data["seller"] != seller:
                raise ValueError
            request = QuoteRequest.model_validate(data["request"])
            if request.model_dump(exclude={"request_id", "action"}) != expected:
                raise ValueError
            return request
        except (ValueError, TypeError) as exc:
            raise MarketCLIError("persisted request does not match this command") from exc
    request = QuoteRequest.model_validate({"request_id": secrets.token_hex(16), **expected})
    _write_output(
        path,
        _serialized(
            {
                "version": 2,
                "seller": seller,
                "request": request.model_dump(mode="json"),
            }
        ),
        idempotent=False,
    )
    return request


async def _request_quote(
    args: argparse.Namespace, settings: JoinMarketSettings
) -> tuple[SignedDocument, str]:
    seller_nick, _, listing = _listing(args.listing, _network(settings))
    bundle = _key_bundle(args.keys)
    buyer_key = _bundle_encryption(bundle)
    certificate = bytes(_bundle_secp(bundle, "renter_certificate_key").pub).hex()
    certificate_pubkey = certificate if args.product == "bond" else None
    request = _request_payload(
        args.request_file,
        listing=listing,
        buyer_key=buyer_key,
        product=args.product,
        rail=args.rail,
        max_price_sats=args.max_price_sats,
        certificate_pubkey=certificate_pubkey,
    )
    fault_cache = MarketFaultCache(settings.get_data_dir())
    transport = _new_transport(settings, fault_cache=fault_cache)
    backend = _new_backend(settings)
    try:
        await transport.start()
        response = await transport.request(
            seller_nick,
            bytes.fromhex(listing.encryption_pubkey),
            request.model_dump(mode="json"),
            args.timeout,
        )
        quote_data = response.get("quote")
        if not isinstance(quote_data, dict):
            raise MarketCLIError("seller did not provide a quote")
        signed_quote = SignedDocument.model_validate(quote_data)
        quote = await accept_quote(
            signed_quote, listing, request, backend, int(time.time()), fault_cache
        )
        return signed_quote, payment_uri(quote.payment)
    except (MarketCLIError, MarketError, ValueError, TypeError) as exc:
        if isinstance(exc, MarketCLIError):
            raise
        raise MarketCLIError("market quote was rejected") from exc
    finally:
        await transport.close()
        await _close_backend(backend)


def _command_request(args: argparse.Namespace) -> None:
    if args.product not in ("podle", "bond") or args.rail not in ("onchain", "lightning"):
        raise MarketCLIError("request product or payment rail is invalid")
    if not args.require_experimental_risk_ack:
        raise MarketCLIError("experimental seller risk acknowledgement is required")
    signed_quote, uri = asyncio.run(_request_quote(args, _settings(args)))
    _write_output(
        args.output,
        _serialized({"quote": signed_quote.model_dump(mode="json"), "payment_uri": uri}),
        idempotent=True,
    )
    _emit_or_write({"quote_id": signed_quote.body["quote_id"], "payment_uri": uri}, None)


async def _poll(args: argparse.Namespace, settings: JoinMarketSettings) -> CredentialPackage:
    seller_nick, _, listing = _listing(args.seller, _network(settings), allow_expired=True)
    quote_document = _quote(args.quote)
    authority, quote = _verified_quote(quote_document)
    if listing.seller_pubkey != authority.seller_pubkey:
        raise MarketCLIError("seller listing is not authorized for this quote")
    buyer_key = (
        _bundle_encryption(_key_bundle(args.keys))
        if args.keys is not None
        else _encryption_key(args.buyer_key, ("encryption_key",))
    )
    transport = _new_transport(settings)
    try:
        await transport.start()
        response = await transport.request(
            seller_nick,
            bytes.fromhex(listing.encryption_pubkey),
            DeliveryRequest(quote_id=quote.quote_id).model_dump(mode="json"),
            args.timeout,
        )
        encrypted = response.get("delivery")
        if not isinstance(encrypted, str):
            raise MarketCLIError("seller has no delivery for this quote")
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
            if len(ciphertext) > _MAX_DELIVERY_BYTES:
                raise ValueError
            plaintext = SealedBox(buyer_key).decrypt(ciphertext)
            if len(plaintext) > MAX_MARKET_BYTES:
                raise ValueError
        except Exception as exc:
            raise MarketCLIError("could not decrypt seller delivery") from exc
        # Preserve exactly what the seller signed before package validation so a
        # malformed delivery can later become signed invalid-delivery evidence.
        _write_output(args.raw_output, plaintext, idempotent=True)
        return open_delivery(encrypted, buyer_key, quote_document)
    except (MarketCLIError, MarketError, ValueError, TypeError) as exc:
        if isinstance(exc, MarketCLIError):
            raise
        raise MarketCLIError("seller delivery is invalid") from exc
    finally:
        await transport.close()


def _command_poll(args: argparse.Namespace) -> None:
    package = asyncio.run(_poll(args, _settings(args)))
    _emit_or_write({"quote_id": package.allocation.body["allocation_id"]}, None)


async def _verify_podle_chain(record: ExternalPoDLE, backend: Any) -> None:
    try:
        if backend.requires_neutrino_metadata():
            result = await backend.verify_utxo_with_metadata(
                txid=record.outpoint.txid,
                vout=record.outpoint.vout,
                scriptpubkey=record.scriptpubkey,
                blockheight=record.blockheight,
            )
            if not result.valid or not result.scriptpubkey_matches or result.confirmations < 1:
                raise ValueError
            return
        utxo = await backend.get_utxo(record.outpoint.txid, record.outpoint.vout)
        if (
            utxo is None
            or utxo.confirmations < 1
            or utxo.scriptpubkey.lower() != record.scriptpubkey
            or (utxo.height is not None and utxo.height != record.blockheight)
        ):
            raise ValueError
    except Exception as exc:
        raise MarketCLIError("PoDLE backing UTXO could not be verified") from exc


def _wallet_fingerprint(value: str) -> str:
    fingerprint = value.lower()
    if len(fingerprint) != 8:
        raise MarketCLIError("wallet fingerprint is invalid")
    try:
        bytes.fromhex(fingerprint)
    except ValueError as exc:
        raise MarketCLIError("wallet fingerprint is invalid") from exc
    return fingerprint


def _import_bond_credential(
    credential: BondCredential,
    certificate: CKey,
    data_dir: Path,
    fingerprint: str,
) -> bool:
    if bytes(certificate.pub).hex() != credential.cert_pubkey:
        raise MarketCLIError("certificate key does not match delivered bond credential")
    from jmwallet.wallet.bond_registry import create_bond_info, load_registry, save_registry

    address = derive_bond_address(
        bytes.fromhex(credential.bond.pubkey), credential.bond.locktime, credential.bond.network
    )
    try:
        registry = load_registry(
            data_dir,
            fingerprint,
            allow_legacy_fallback=False,
            fail_closed=True,
        )
    except ValueError as exc:
        raise MarketCLIError("could not safely load bond registry") from exc
    existing = registry.get_bond_by_address(address.address)
    if existing is not None:
        if (
            existing.pubkey != credential.bond.pubkey
            or existing.locktime != credential.bond.locktime
            or existing.network != credential.bond.network
            or existing.cert_pubkey != credential.cert_pubkey
            or existing.cert_privkey != certificate.secret_bytes.hex()
            or existing.cert_signature != credential.cert_signature
            or existing.cert_expiry != credential.cert_expiry
            or (existing.txid is not None and existing.txid != credential.bond.outpoint.txid)
            or (existing.vout is not None and existing.vout != credential.bond.outpoint.vout)
        ):
            raise MarketCLIError("existing bond registry entry conflicts with delivered credential")
        return False
    imported = create_bond_info(
        address=address.address,
        locktime=credential.bond.locktime,
        index=-1,
        path="external",
        pubkey_hex=credential.bond.pubkey,
        witness_script=address.witness_script,
        network=credential.bond.network,
    )
    imported.cert_pubkey = credential.cert_pubkey
    imported.cert_privkey = certificate.secret_bytes.hex()
    imported.cert_signature = credential.cert_signature
    imported.cert_expiry = credential.cert_expiry
    registry.add_bond(imported)
    save_registry(registry, data_dir, fingerprint)
    return True


async def _import_package(args: argparse.Namespace, settings: JoinMarketSettings) -> dict[str, Any]:
    package = _credential_package(args.package)
    authority, allocation = verify_allocation(package.authorization, package.allocation)
    if authority.bond.network != _network(settings):
        raise MarketCLIError("package network differs from configured network")
    credential = package.verify()
    backend = _new_backend(settings)
    try:
        if isinstance(credential, ExternalPoDLE):
            await _verify_podle_chain(credential, backend)
            imported = PoDLEManager(settings.get_data_dir()).import_external(credential)
            return {"product": "podle", "imported": imported, "resource": allocation.resource}
        await verify_market_bond(authority, backend, _network(settings), int(time.time()))
        if args.certificate_key is None or args.wallet_fingerprint is None:
            raise MarketCLIError("bond import requires certificate key and wallet fingerprint")
        imported = _import_bond_credential(
            credential,
            _secp_key(args.certificate_key),
            settings.get_data_dir(),
            _wallet_fingerprint(args.wallet_fingerprint),
        )
        return {"product": "bond", "imported": imported, "resource": allocation.resource}
    finally:
        await _close_backend(backend)


def _command_import(args: argparse.Namespace) -> None:
    _emit_or_write(asyncio.run(_import_package(args, _settings(args))), args.output)


def _command_proof_build(args: argparse.Namespace) -> None:
    try:
        if args.first_package is not None:
            if args.second_package is None:
                package = _structural_credential_package(args.first_package)
                proof = FaultProof(
                    reason="invalid-delivery",
                    authorization=package.authorization,
                    first=package.allocation,
                    second=package.delivery,
                )
            else:
                first = _credential_package(args.first_package)
                second = _credential_package(args.second_package)
                proof = FaultProof(
                    reason="double-allocation",
                    authorization=first.authorization,
                    first=first.allocation,
                    second=second.allocation,
                    second_authorization=(
                        None
                        if second.authorization == first.authorization
                        else second.authorization
                    ),
                )
        else:
            if (
                args.second_package is not None
                or args.allocation is None
                or args.invalid_delivery is None
            ):
                raise ValueError
            authorization = _signed_document(args.authorization)
            allocation = _signed_document(args.allocation)
            delivery = _signed_document(args.invalid_delivery)
            proof = FaultProof(
                reason="invalid-delivery",
                authorization=authorization,
                first=allocation,
                second=delivery,
            )
        proof.verify()
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("fault proof is invalid") from exc
    _write_output(args.output, _serialized(proof.model_dump(mode="json")), idempotent=True)


async def _broadcast_proof(args: argparse.Namespace, settings: JoinMarketSettings) -> int:
    try:
        proof = FaultProof.model_validate(_document(args.proof))
        proof.verify()
        raw = canonical(proof)
    except (MarketError, ValueError, TypeError) as exc:
        raise MarketCLIError("fault proof is invalid") from exc
    transport = _new_transport(settings)
    try:
        return await transport.broadcast_fault(raw)
    finally:
        await transport.close()


def _command_proof_broadcast(args: argparse.Namespace) -> None:
    sent = asyncio.run(_broadcast_proof(args, _settings(args)))
    _emit_or_write({"sent": sent}, args.output)


def _common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--config-file", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jm-market",
        description=(
            "Native credential-market commands. Secret key files are either 32-byte raw hex or "
            'JSON {"version":1,"secret_key":"..."}; keygen writes a private key bundle.'
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser("keygen", help="generate a private market key bundle")
    keygen.add_argument("--output", type=Path, required=True)
    keygen.set_defaults(handler=_command_keygen)

    public = commands.add_parser("public", help="export public keys from a private key bundle")
    public.add_argument("--keys", type=Path, required=True)
    public.add_argument("--output", type=Path)
    public.set_defaults(handler=_command_public)

    authorize = commands.add_parser("authorize", help="offline sign seller authorization")
    authorize.add_argument("--bond-ref", type=Path, required=True)
    authorize.add_argument("--seller-pub", type=Path, required=True)
    authorize.add_argument("--owner-key", type=Path, required=True)
    authorize.add_argument("--period", type=int, required=True)
    authorize.add_argument("--output", type=Path, required=True)
    authorize.set_defaults(handler=_command_authorize)

    sign_bond = commands.add_parser("sign-bond", help="offline sign a renter bond credential")
    sign_bond.add_argument("--authorization", type=Path, required=True)
    sign_bond.add_argument("--certificate-pub", type=Path, required=True)
    sign_bond.add_argument("--owner-key", type=Path, required=True)
    sign_bond.add_argument("--output", type=Path, required=True)
    sign_bond.set_defaults(handler=_command_sign_bond)

    export_podle = commands.add_parser("export-podle", help="export one external PoDLE")
    export_podle.add_argument("--owner-key", type=Path, required=True)
    export_podle.add_argument("--metadata", type=Path, required=True)
    export_podle.add_argument("--output", type=Path, required=True)
    export_podle.set_defaults(handler=_command_export_podle)

    seller = commands.add_parser("seller", help="manage local seller inventory and settlements")
    seller_commands = seller.add_subparsers(dest="seller_command", required=True)
    add_inventory = seller_commands.add_parser("add-inventory")
    _common_options(add_inventory)
    inventory_source = add_inventory.add_mutually_exclusive_group(required=True)
    inventory_source.add_argument("--credential", type=Path)
    inventory_source.add_argument("--authorization", type=Path)
    add_inventory.set_defaults(handler=_command_seller_add_inventory)

    add_payment = seller_commands.add_parser("add-payment")
    _common_options(add_payment)
    add_payment.add_argument("--terms", type=Path, required=True)
    add_payment.add_argument("--ttl", type=int, default=300)
    add_payment.add_argument("--output", type=Path)
    add_payment.set_defaults(handler=_command_seller_add_payment)

    pending = seller_commands.add_parser("pending")
    _common_options(pending)
    pending.add_argument("--output", type=Path)
    pending.set_defaults(handler=_command_seller_pending)

    attach = seller_commands.add_parser("attach")
    _common_options(attach)
    attach.add_argument("--quote-id", required=True)
    attach.add_argument("--credential", type=Path, required=True)
    attach.set_defaults(handler=_command_seller_attach)

    settle = seller_commands.add_parser("settle")
    _common_options(settle)
    settle.add_argument("--quote-id", required=True)
    settle.add_argument("--keys", type=Path, required=True)
    settlement_source = settle.add_mutually_exclusive_group(required=True)
    settlement_source.add_argument("--onchain-outpoint")
    settlement_source.add_argument("--preimage-file", type=Path)
    settle.add_argument("--acknowledge-ln-settlement", action="store_true")
    settle.add_argument("--output", type=Path, required=True)
    settle.set_defaults(handler=_command_seller_settle)

    export = seller_commands.add_parser("export", help="export one finalized credential package")
    _common_options(export)
    export.add_argument("--quote-id", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.set_defaults(handler=_command_seller_export)

    serve = commands.add_parser("serve", help="serve signed listings and sealed market responses")
    _common_options(serve)
    serve.add_argument("--authorization", type=Path, required=True)
    serve.add_argument("--keys", type=Path, required=True)
    serve.add_argument("--products", required=True)
    serve.add_argument("--price-sats", type=int, required=True)
    serve.add_argument("--quote-ttl", type=int, default=300)
    serve.add_argument("--direct-location", default="NOT-SERVING-ONION")
    serve.add_argument("--listen-host")
    serve.add_argument("--listen-port", type=int, default=0)
    serve.set_defaults(handler=_command_serve)

    discover = commands.add_parser("discover", help="discover signed seller listings")
    _common_options(discover)
    discover.add_argument("--timeout", type=float, default=30.0)
    discover.add_argument("--output", type=Path)
    discover.set_defaults(handler=_command_discover)

    request = commands.add_parser("request", help="persist and submit a buyer quote request")
    _common_options(request)
    request.add_argument("--listing", type=Path, required=True)
    request.add_argument("--keys", type=Path, required=True)
    request.add_argument("--product", required=True)
    request.add_argument("--rail", required=True)
    request.add_argument("--max-price-sats", type=int, required=True)
    request.add_argument("--request-file", type=Path, required=True)
    request.add_argument("--output", type=Path, required=True)
    request.add_argument("--timeout", type=float, default=30.0)
    request.add_argument(
        "--require-experimental-risk-ack",
        action="store_true",
        help="acknowledge that seller credential delivery is not fair exchange",
    )
    request.set_defaults(handler=_command_request)

    poll = commands.add_parser("poll", help="retrieve and preserve one sealed seller delivery")
    _common_options(poll)
    poll.add_argument("--quote", type=Path, required=True)
    poll.add_argument("--seller", "--seller-info", dest="seller", type=Path, required=True)
    poll_keys = poll.add_mutually_exclusive_group(required=True)
    poll_keys.add_argument("--keys", type=Path)
    poll_keys.add_argument("--buyer-key", type=Path)
    poll.add_argument("--raw-output", type=Path, required=True)
    poll.add_argument("--timeout", type=float, default=30.0)
    poll.set_defaults(handler=_command_poll)

    import_command = commands.add_parser(
        "import", help="validate and import a delivered credential"
    )
    _common_options(import_command)
    import_command.add_argument("--package", type=Path, required=True)
    import_command.add_argument("--certificate-key", type=Path)
    import_command.add_argument("--wallet-fingerprint")
    import_command.add_argument("--output", type=Path)
    import_command.set_defaults(handler=_command_import)

    proof = commands.add_parser("proof", help="build or publish signed market fault evidence")
    proof_commands = proof.add_subparsers(dest="proof_command", required=True)
    build = proof_commands.add_parser("build")
    build_source = build.add_mutually_exclusive_group(required=True)
    build_source.add_argument("--first-package", type=Path)
    build_source.add_argument("--authorization", type=Path)
    build.add_argument("--second-package", type=Path)
    build.add_argument("--allocation", type=Path)
    build.add_argument("--invalid-delivery", type=Path)
    build.add_argument("--output", type=Path, required=True)
    build.set_defaults(handler=_command_proof_build)

    broadcast = proof_commands.add_parser("broadcast")
    _common_options(broadcast)
    broadcast.add_argument("--proof", type=Path, required=True)
    broadcast.add_argument("--output", type=Path)
    broadcast.set_defaults(handler=_command_proof_broadcast)
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """Run the CLI without logging user-supplied market material."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except KeyboardInterrupt:
        return 130
    except Exception:
        sys.stderr.write("market command failed\n")
        return 1
    return 0


def main() -> None:
    """Console-script entry point."""
    from jmcore.process_hardening import harden_current_process

    harden_current_process()
    raise SystemExit(run())


if __name__ == "__main__":
    main()
