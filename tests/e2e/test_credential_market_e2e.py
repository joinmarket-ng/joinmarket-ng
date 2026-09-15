"""Docker/regtest lifecycles for the native credential market."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import socket
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import address_to_scriptpubkey_for_network, pubkey_to_p2wpkh_address
from jmcore.btc_script import derive_bond_address
from jmcore.credential_market import (
    Allocation,
    BondReference,
    FaultProof,
    MarketAuthorization,
    MarketQuote,
    PaymentTerms,
    SignedDocument,
    bond_resource,
    canonical,
    document_hash,
    period_at_height,
    sign_document,
)
from jmcore.crypto import NickIdentity, verify_fidelity_bond_proof
from jmcore.directory_client import DirectoryClient, parse_fidelity_bond_proof
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.models import NetworkType, Offer, OfferType
import jmcore.network as network_module
from jmcore.podle import generate_podle
from jmcore.timenumber import get_nearest_valid_locktime
from jmwallet.wallet.bond_registry import BondRegistry, load_registry, save_registry
from jmwallet.wallet.service import WalletService
from maker.fidelity import (
    FidelityBondInfo as MakerBondInfo,
    create_fidelity_bond_proof,
    ensure_fidelity_bond_certificate_valid,
)
from nacl.public import PrivateKey
from taker._vendor.bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, encode
from taker.config import TakerConfig
from taker.market_cli import _import_bond_credential
from taker.market_payments import (
    validate_payment_terms,
    verify_lightning_preimage,
    verify_onchain_payment,
)
from taker.market_service import (
    DeliveryRequest,
    MarketService,
    QuoteRequest,
    accept_listing,
    accept_quote,
    make_bond_credential,
    open_delivery,
)
from taker.market_store import MarketStore
from taker.market_transport import MarketTransport
from taker.podle_manager import PoDLEManager
from taker.taker import Taker

from tests.e2e.rpc_utils import (
    TEST_FUNDER_WALLET,
    ensure_wallet_funded,
    mine_blocks,
    rpc_call,
)

pytestmark = pytest.mark.e2e

_DIRECTORY_PORT = int(os.environ.get("DIRECTORY_PORT", "5222"))
_DIRECTORY_SERVER = f"127.0.0.1:{_DIRECTORY_PORT}"
_DIRECTORY_ID = "test:jm-directory-5222"
_MINING_ADDRESS = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
_TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)
_MARKET_PRICE_SATS = 100_000


@dataclass(frozen=True)
class _ConfirmedOutput:
    txid: str
    vout: int
    value_sats: int
    blockheight: int


@dataclass(frozen=True)
class _AuthorizedBond:
    owner_key: CKey
    seller_key: CKey
    reference: BondReference
    authorization: SignedDocument
    value_sats: int
    confirmation_time: int
    height: int


@dataclass
class _PoDLEMarket:
    bond: _AuthorizedBond
    podle: ExternalPoDLE
    terms: PaymentTerms
    store: MarketStore
    service: MarketService
    seller_encryption_key: PrivateKey


def _new_secp_key() -> CKey:
    while True:
        try:
            return CKey(secrets.token_bytes(32))
        except ValueError:
            continue


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _output_value_sats(value: object) -> int:
    return int(Decimal(str(value)) * Decimal(100_000_000))


async def _send_and_confirm(
    address: str, amount_sats: int, confirmations: int = 1
) -> _ConfirmedOutput:
    txid = await rpc_call(
        "sendtoaddress",
        [address, amount_sats / 100_000_000],
        wallet=TEST_FUNDER_WALLET,
    )
    assert isinstance(txid, str) and len(txid) == 64
    await mine_blocks(confirmations, _MINING_ADDRESS)

    raw = await rpc_call("getrawtransaction", [txid, True])
    assert isinstance(raw, dict)
    expected_script = address_to_scriptpubkey_for_network(address, "regtest").hex()
    matching = [
        output
        for output in raw.get("vout", [])
        if output.get("scriptPubKey", {}).get("hex") == expected_script
        and _output_value_sats(output.get("value")) == amount_sats
    ]
    assert len(matching) == 1, f"Could not find the exact payment output in {txid}"
    blockhash = raw.get("blockhash")
    assert isinstance(blockhash, str)
    header = await rpc_call("getblockheader", [blockhash])
    assert isinstance(header, dict) and isinstance(header.get("height"), int)
    return _ConfirmedOutput(
        txid=txid,
        vout=int(matching[0]["n"]),
        value_sats=amount_sats,
        blockheight=int(header["height"]),
    )


async def _create_authorized_bond(backend: object) -> _AuthorizedBond:
    owner_key = _new_secp_key()
    seller_key = _new_secp_key()
    locktime = get_nearest_valid_locktime(
        int(time.time()) + 365 * 24 * 60 * 60, round_up=True
    )
    address = derive_bond_address(bytes(owner_key.pub), locktime, "regtest")
    output = await _send_and_confirm(address.address, 1_000_000)

    height = await backend.get_block_height()  # type: ignore[attr-defined]
    reference = BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=output.txid, vout=output.vout),
        pubkey=bytes(owner_key.pub).hex(),
        locktime=locktime,
    )
    authorization = sign_document(
        MarketAuthorization(
            bond=reference,
            period=period_at_height(height),
            seller_pubkey=bytes(seller_key.pub).hex(),
        ),
        owner_key,
    )
    confirmation_time = await backend.get_block_time(output.blockheight)  # type: ignore[attr-defined]
    return _AuthorizedBond(
        owner_key=owner_key,
        seller_key=seller_key,
        reference=reference,
        authorization=authorization,
        value_sats=output.value_sats,
        confirmation_time=confirmation_time,
        height=height,
    )


async def _create_external_podle(amount_sats: int = 1_000_000) -> ExternalPoDLE:
    private_key = secrets.token_bytes(32)
    public_key = bytes(CKey(private_key).pub)
    address = pubkey_to_p2wpkh_address(public_key, "regtest")
    output = await _send_and_confirm(address, amount_sats, confirmations=6)
    proof = generate_podle(private_key, f"{output.txid}:{output.vout}", index=0)
    return ExternalPoDLE(
        version=1,
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=output.txid, vout=output.vout),
        P=proof.p.hex(),
        P2=proof.p2.hex(),
        sig=proof.sig.hex(),
        e=proof.e.hex(),
        commitment=proof.commitment.hex(),
        index=0,
        scriptpubkey=address_to_scriptpubkey_for_network(address, "regtest").hex(),
        blockheight=output.blockheight,
    )


async def _onchain_terms() -> PaymentTerms:
    address = await rpc_call(
        "getnewaddress", ["market-payment", "bech32"], wallet=TEST_FUNDER_WALLET
    )
    assert isinstance(address, str)
    return PaymentTerms(
        rail="onchain",
        request=address,
        amount_sats=_MARKET_PRICE_SATS,
        min_confirmations=1,
    )


def _lightning_terms(now: int, preimage: bytes) -> PaymentTerms:
    invoice = Bolt11(
        currency="bcrt",
        date=now - 1,
        amount_msat=MilliSatoshi(_MARKET_PRICE_SATS * 1_000),
        tags=Tags(
            [
                Tag(TagChar.payment_hash, hashlib.sha256(preimage).hexdigest()),
                Tag(TagChar.payment_secret, "22" * 32),
                Tag(TagChar.description, "native market manual settlement"),
                Tag(TagChar.expire_time, 600),
                Tag(TagChar.min_final_cltv_expiry, 18),
            ]
        ),
    )
    return PaymentTerms(
        rail="lightning",
        request=encode(invoice, private_key="11" * 32),
        amount_sats=_MARKET_PRICE_SATS,
        min_confirmations=1,
    )


async def _podle_market(
    tmp_path: Path,
    backend: object,
    terms: PaymentTerms,
    podle_amount_sats: int = 1_000_000,
) -> _PoDLEMarket:
    bond = await _create_authorized_bond(backend)
    podle = await _create_external_podle(podle_amount_sats)
    store = MarketStore(tmp_path / "seller.sqlite")
    now = int(time.time())
    store.add_inventory("podle", podle.commitment, podle.model_dump(mode="json"))
    store.add_payment(
        terms,
        validate_payment_terms(terms, "regtest", now, now + 180),
    )
    encryption_key = PrivateKey.generate()
    service = MarketService(
        store,
        bond.authorization,
        bond.seller_key,
        encryption_key,
        backend,  # type: ignore[arg-type]
        products=["podle"],
        price_sats=_MARKET_PRICE_SATS,
        quote_ttl=180,
    )
    return _PoDLEMarket(
        bond=bond,
        podle=podle,
        terms=terms,
        store=store,
        service=service,
        seller_encryption_key=encryption_key,
    )


def _market_transport(
    *,
    encryption_key: PrivateKey | None = None,
    listing_callback: object | None = None,
    responder: object | None = None,
    direct_location: str = "NOT-SERVING-ONION",
    listen_host: str | None = None,
    listen_port: int = 0,
) -> MarketTransport:
    return MarketTransport(
        directory_servers=[_DIRECTORY_SERVER],
        # The Docker directory retains JoinMarket's testnet protocol namespace
        # while Bitcoin Core itself runs regtest.
        network="testnet",
        allow_clearnet_connections=True,
        nick_identity=NickIdentity(),
        connection_timeout=15.0,
        nick_auth_directory_ids={_DIRECTORY_SERVER: _DIRECTORY_ID},
        encryption_private_key=encryption_key,
        listing_callback=listing_callback,  # type: ignore[arg-type]
        responder=responder,  # type: ignore[arg-type]
        direct_location=direct_location,
        listen_host=listen_host,
        listen_port=listen_port,
    )


async def _request_quote(
    buyer: MarketTransport,
    seller: MarketTransport,
    backend: object,
) -> tuple[SignedDocument, QuoteRequest, MarketQuote]:
    listing_document: SignedDocument | None = None
    listing = None
    for _ in range(3):
        for sender, raw in await buyer.discover(timeout=5.0):
            if sender == seller.nick:
                listing_document, listing = accept_listing(
                    raw, "regtest", int(time.time())
                )
                break
        if listing_document is not None:
            break
        await asyncio.sleep(0.5)
    assert listing_document is not None and listing is not None, (
        "Seller listing was not discovered"
    )

    request = QuoteRequest(
        request_id=secrets.token_hex(16),
        buyer_pubkey=bytes(PrivateKey.generate().public_key).hex(),
        product="podle",
        certificate_pubkey=None,
        rail="onchain",
        max_price_sats=_MARKET_PRICE_SATS,
    )
    response = await buyer.request(
        seller.nick,
        bytes.fromhex(listing.encryption_pubkey),
        request.model_dump(mode="json"),
        timeout=15.0,
    )
    quote_data = response.get("quote")
    assert isinstance(quote_data, dict)
    quote_document = SignedDocument.model_validate(quote_data)
    quote = await accept_quote(
        quote_document,
        listing,
        request,
        backend,  # type: ignore[arg-type]
        int(time.time()),
    )
    return quote_document, request, quote


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["relay", "direct"])
async def test_market_transport_uses_real_directory_for_relay_and_direct(
    route: Literal["relay", "direct"],
    bitcoin_core_backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signed MarketService quote survives both native transport routes."""
    market = await _podle_market(tmp_path, bitcoin_core_backend, await _onchain_terms())
    port = _free_loopback_port() if route == "direct" else 0
    synthetic_onion = f"{'a' * 56}.onion"
    location = f"{synthetic_onion}:{port}" if route == "direct" else "NOT-SERVING-ONION"
    seller: MarketTransport | None = None
    buyer: MarketTransport | None = None
    try:
        if route == "direct":
            # Tor stand-in: preserve the normal onion validation and protocol while
            # routing this isolated in-process listener to loopback.
            async def connect_synthetic_onion(
                onion_address: str,
                onion_port: int,
                _socks_host: str = "127.0.0.1",
                _socks_port: int = 9050,
                max_message_size: int = 2_097_152,
                timeout: float = 120.0,
                socks_username: str | None = None,
                socks_password: str | None = None,
            ) -> network_module.TCPConnection:
                assert onion_address == synthetic_onion
                assert socks_username is not None and socks_password is not None
                return await network_module.connect_direct(
                    "127.0.0.1",
                    onion_port,
                    max_message_size=max_message_size,
                    timeout=timeout,
                )

            monkeypatch.setattr(
                network_module, "connect_via_tor", connect_synthetic_onion
            )
        seller = _market_transport(
            encryption_key=market.seller_encryption_key,
            listing_callback=market.service.listing,
            responder=market.service.respond,
            direct_location=location,
            listen_host="127.0.0.1" if route == "direct" else None,
            listen_port=port,
        )
        buyer = _market_transport()
        relay_calls = 0
        original_relay = buyer._send_relay_once

        async def count_relay(recipient: str, command: str, data: str) -> None:
            nonlocal relay_calls
            relay_calls += 1
            await original_relay(recipient, command, data)

        monkeypatch.setattr(buyer, "_send_relay_once", count_relay)
        await seller.start()
        await buyer.start()
        if route == "direct":
            await buyer.clients[_DIRECTORY_SERVER].get_peerlist_with_features()
            assert buyer.get_peer_location(seller.nick) == location
        _, request, quote = await _request_quote(buyer, seller, bitcoin_core_backend)
        assert quote.product == request.product
        assert quote.resource == market.podle.commitment
        assert quote.authorization == market.bond.authorization
        assert relay_calls == (1 if route == "relay" else 0)
        if route == "direct":
            peer = buyer._peer_connections.get(seller.nick)
            assert peer is not None and peer.is_connected()
    finally:
        if buyer is not None:
            await buyer.close()
        if seller is not None:
            await seller.close()
        market.store.close()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_onchain_podle_purchase_authorizes_external_only_coinjoin(
    bitcoin_core_backend,
    fresh_docker_makers,
    tmp_path: Path,
) -> None:
    """A paid PoDLE is imported, consumed once, and never funds the CoinJoin."""
    market = await _podle_market(
        tmp_path,
        bitcoin_core_backend,
        await _onchain_terms(),
        podle_amount_sats=20_000_000,
    )
    seller = _market_transport(
        encryption_key=market.seller_encryption_key,
        listing_callback=market.service.listing,
        responder=market.service.respond,
    )
    buyer = _market_transport()
    buyer_key = PrivateKey.generate()
    try:
        await seller.start()
        await buyer.start()

        listing_document: SignedDocument | None = None
        listing = None
        for _ in range(3):
            for sender, raw in await buyer.discover(timeout=5.0):
                if sender == seller.nick:
                    listing_document, listing = accept_listing(
                        raw, "regtest", int(time.time())
                    )
                    break
            if listing_document is not None:
                break
            await asyncio.sleep(0.5)
        assert listing_document is not None and listing is not None

        request = QuoteRequest(
            request_id=secrets.token_hex(16),
            buyer_pubkey=bytes(buyer_key.public_key).hex(),
            product="podle",
            certificate_pubkey=None,
            rail="onchain",
            max_price_sats=_MARKET_PRICE_SATS,
        )
        response = await buyer.request(
            seller.nick,
            bytes.fromhex(listing.encryption_pubkey),
            request.model_dump(mode="json"),
            timeout=15.0,
        )
        quote_document = SignedDocument.model_validate(response["quote"])
        quote = await accept_quote(
            quote_document,
            listing,
            request,
            bitcoin_core_backend,
            int(time.time()),
        )

        paid = await _send_and_confirm(quote.payment.request, quote.payment.amount_sats)
        settlement_ref = await verify_onchain_payment(
            bitcoin_core_backend,
            quote.payment,
            "regtest",
            f"{paid.txid}:{paid.vout}",
        )
        market.store.attach_credential(
            quote.quote_id,
            market.podle.model_dump(mode="json"),
        )
        package = market.store.finalize(
            quote.quote_id,
            market.bond.seller_key,
            settlement_ref,
            int(time.time()),
            await bitcoin_core_backend.get_block_height(),
        )
        assert package.verify() == market.podle

        delivery = await buyer.request(
            seller.nick,
            bytes.fromhex(listing.encryption_pubkey),
            DeliveryRequest(quote_id=quote.quote_id).model_dump(mode="json"),
            timeout=15.0,
        )
        encrypted = delivery.get("delivery")
        assert isinstance(encrypted, str)
        purchased = open_delivery(encrypted, buyer_key, quote_document)
        assert purchased == package
        credential = purchased.verify()
        assert isinstance(credential, ExternalPoDLE)

        data_dir = tmp_path / "coinjoin-buyer"
        wallet = WalletService(
            mnemonic=_TAKER_MNEMONIC,
            backend=bitcoin_core_backend,
            network="regtest",
            mixdepth_count=5,
            data_dir=data_dir,
        )
        taker = Taker(
            wallet,
            bitcoin_core_backend,
            TakerConfig(
                mnemonic=_TAKER_MNEMONIC,
                network=NetworkType.TESTNET,
                bitcoin_network=NetworkType.REGTEST,
                backend_type="descriptor_wallet",
                backend_config={
                    "rpc_url": os.environ.get(
                        "BITCOIN_RPC_URL", "http://127.0.0.1:18443"
                    ),
                    "rpc_user": os.environ.get("BITCOIN_RPC_USER", "test"),
                    "rpc_password": os.environ.get("BITCOIN_RPC_PASSWORD", "test"),
                },
                directory_servers=[_DIRECTORY_SERVER],
                allow_clearnet_connections=True,
                counterparty_count=2,
                minimum_makers=2,
                maker_timeout_sec=60,
                order_wait_time=10.0,
                orderbook_min_wait=0.0,
                orderbook_quiet_period=1.0,
                bondless_makers_allowance_require_zero_fee=False,
                external_podle_mode="only",
                data_dir=data_dir,
            ),
        )
        try:
            await wallet.sync_all()
            assert await ensure_wallet_funded(
                wallet.get_receive_address(0, 0), amount_btc=2, confirmations=6
            ), "Could not fund the Docker e2e taker wallet"
            await wallet.sync_all()
            assert await wallet.get_total_balance() >= 100_000_000
            assert taker.podle_manager.import_external(credential) is True
            assert taker.podle_manager.external_count() == 1
            taker.directory_client.prefer_direct_connections = False
            await taker.start()
            txid = await taker.do_coinjoin(
                amount=50_000_000,
                destination=wallet.get_receive_address(1, 0),
                mixdepth=0,
            )
            assert txid is not None, taker.last_failure_reason

            raw = await rpc_call("getrawtransaction", [txid, True])
            inputs = {(item["txid"], item["vout"]) for item in raw["vin"]}
            assert (credential.outpoint.txid, credential.outpoint.vout) not in inputs
            assert (
                await bitcoin_core_backend.get_utxo(
                    credential.outpoint.txid, credential.outpoint.vout
                )
                is not None
            )
            assert taker.podle_manager.external_count() == 0
            assert credential.commitment in taker.podle_manager.used_commitments

            retry = await taker.do_coinjoin(
                amount=50_000_000,
                destination=wallet.get_receive_address(1, 1),
                mixdepth=0,
            )
            assert retry is None
            assert credential.commitment in taker.podle_manager.used_commitments
        finally:
            await taker.stop()
    finally:
        await buyer.close()
        await seller.close()
        market.store.close()


@pytest.mark.asyncio
async def test_lightning_manual_settlement_requires_local_acknowledgement(
    bitcoin_core_backend,
    tmp_path: Path,
) -> None:
    """A deterministic BOLT11 preimage needs a local acknowledgement before delivery."""
    preimage = bytes(range(32))
    market = await _podle_market(
        tmp_path,
        bitcoin_core_backend,
        _lightning_terms(int(time.time()), preimage),
    )
    buyer_key = PrivateKey.generate()
    try:
        listing_document = SignedDocument.model_validate(
            json.loads((await market.service.listing()).decode("ascii"))
        )
        _, listing = accept_listing(
            canonical(listing_document), "regtest", int(time.time())
        )
        request = QuoteRequest(
            request_id=secrets.token_hex(16),
            buyer_pubkey=bytes(buyer_key.public_key).hex(),
            product="podle",
            certificate_pubkey=None,
            rail="lightning",
            max_price_sats=_MARKET_PRICE_SATS,
        )
        response = await market.service.respond(
            "untrusted-buyer", request.model_dump(mode="json")
        )
        quote_document = SignedDocument.model_validate(response["quote"])
        quote = await accept_quote(
            quote_document,
            listing,
            request,
            bitcoin_core_backend,
            int(time.time()),
        )

        remote_claim = await market.service.respond(
            "untrusted-buyer",
            {
                "action": "settle",
                "quote_id": quote.quote_id,
                "preimage": preimage.hex(),
            },
        )
        assert remote_claim == {"error": "unavailable"}
        assert market.store.get_delivery(quote.quote_id) is None

        settlement_ref = verify_lightning_preimage(
            quote.payment,
            "regtest",
            preimage,
            int(time.time()),
            quote.expires_at,
        )
        market.store.attach_credential(
            quote.quote_id, market.podle.model_dump(mode="json")
        )
        package = market.store.finalize(
            quote.quote_id,
            market.bond.seller_key,
            settlement_ref,
            int(time.time()),
            await bitcoin_core_backend.get_block_height(),
        )
        assert package.verify() == market.podle
        encrypted = market.store.get_delivery(quote.quote_id)
        assert encrypted is not None
        opened = open_delivery(encrypted, buyer_key, quote_document)
        credential = opened.verify()
        assert isinstance(credential, ExternalPoDLE)

        manager = PoDLEManager(tmp_path / "lightning-buyer")
        assert manager.import_external(credential) is True
        assert manager.external_count() == 1
    finally:
        market.store.close()


@pytest.mark.asyncio
async def test_delegated_bond_package_imports_for_maker_proof_without_owner_key(
    bitcoin_core_backend,
    tmp_path: Path,
) -> None:
    """A renter certificate imports into the registry and signs a standard maker proof."""
    bond = await _create_authorized_bond(bitcoin_core_backend)
    renter_key = _new_secp_key()
    credential = make_bond_credential(
        bond.authorization,
        bytes(renter_key.pub).hex(),
        bond.owner_key,
    )
    assert credential.cert_expiry == period_at_height(bond.height) + 1

    payment = await _onchain_terms()
    store = MarketStore(tmp_path / "seller.sqlite")
    buyer_key = PrivateKey.generate()
    try:
        now = int(time.time())
        store.add_inventory(
            "bond",
            bond_resource(bond.reference, period_at_height(bond.height)),
            None,
        )
        store.add_payment(
            payment, validate_payment_terms(payment, "regtest", now, now + 180)
        )
        quote_document = store.create_quote(
            bond.authorization,
            bond.seller_key,
            bytes(buyer_key.public_key).hex(),
            "bond",
            credential.cert_pubkey,
            "onchain",
            _MARKET_PRICE_SATS,
            now,
            bond.height,
            request_id=secrets.token_hex(16),
        )
        quote_id = str(quote_document.body["quote_id"])
        store.attach_credential(quote_id, credential.model_dump(mode="json"))
        package = store.finalize(
            quote_id,
            bond.seller_key,
            "delegated-local-settlement",
            now,
            bond.height,
        )
        assert package.verify() == credential

        fingerprint = "de1e6a7e"
        save_registry(BondRegistry(), tmp_path, fingerprint)
        assert (
            _import_bond_credential(credential, renter_key, tmp_path, fingerprint)
            is True
        )
        imported = load_registry(
            tmp_path,
            fingerprint,
            allow_legacy_fallback=False,
            fail_closed=True,
        )
        registry_bond = imported.get_bond_by_address(
            derive_bond_address(
                bytes.fromhex(credential.bond.pubkey),
                credential.bond.locktime,
                credential.bond.network,
            ).address
        )
        assert registry_bond is not None and registry_bond.has_certificate

        maker_bond = MakerBondInfo(
            txid=bond.reference.outpoint.txid,
            vout=bond.reference.outpoint.vout,
            value=bond.value_sats,
            locktime=bond.reference.locktime,
            confirmation_time=bond.confirmation_time,
            bond_value=bond.value_sats,
            pubkey=bytes.fromhex(registry_bond.pubkey),
            private_key=None,
            cert_pubkey=bytes.fromhex(registry_bond.cert_pubkey or ""),
            cert_privkey=CKey(bytes.fromhex(registry_bond.cert_privkey or "")),
            cert_signature=bytes.fromhex(registry_bond.cert_signature or ""),
            cert_expiry=registry_bond.cert_expiry,
        )
        current_height = await bitcoin_core_backend.get_block_height()
        ensure_fidelity_bond_certificate_valid(maker_bond, current_height)
        proof = create_fidelity_bond_proof(
            maker_bond,
            maker_nick="J5delegatedmaker",
            taker_nick="J5delegatedtaker",
            current_block_height=current_height,
        )
        assert proof is not None
        valid, data, error = verify_fidelity_bond_proof(
            proof,
            maker_nick="J5delegatedmaker",
            taker_nick="J5delegatedtaker",
        )
        assert valid, error
        assert data is not None
        assert data["utxo_pub"] == credential.bond.pubkey
        assert data["cert_pub"] == credential.cert_pubkey
        assert data["cert_expiry"] == credential.cert_expiry * 2016
    finally:
        store.close()


@pytest.mark.asyncio
async def test_signed_market_fault_excludes_matching_verified_makers(
    bitcoin_core_backend,
    tmp_path: Path,
) -> None:
    """A directory-broadcast double allocation excludes both matching maker nicks."""
    bond = await _create_authorized_bond(bitcoin_core_backend)
    current_height = await bitcoin_core_backend.get_block_height()
    current_period = period_at_height(current_height)
    certificate_key = _new_secp_key()
    credential = make_bond_credential(
        bond.authorization,
        bytes(certificate_key.pub).hex(),
        bond.owner_key,
    )
    store = MarketStore(tmp_path / "fault-seller.sqlite")
    watcher = DirectoryClient(
        host="127.0.0.1",
        port=_DIRECTORY_PORT,
        network="testnet",
        allow_clearnet_connections=True,
        nick_auth_directory_id=_DIRECTORY_ID,
        timeout=15.0,
    )
    try:
        resource = bond_resource(bond.reference, current_period)
        terms = await _onchain_terms()
        store.add_inventory("bond", resource, None)
        store.add_payment(
            terms,
            validate_payment_terms(
                terms, "regtest", int(time.time()), int(time.time()) + 180
            ),
        )
        quote_document = store.create_quote(
            bond.authorization,
            bond.seller_key,
            bytes(PrivateKey.generate().public_key).hex(),
            "bond",
            credential.cert_pubkey,
            "onchain",
            _MARKET_PRICE_SATS,
            int(time.time()),
            current_height,
            request_id=secrets.token_hex(16),
        )
        quote = MarketQuote.model_validate(quote_document.body)
        store.attach_credential(quote.quote_id, credential.model_dump(mode="json"))
        package = store.finalize(
            quote.quote_id,
            bond.seller_key,
            f"fault-settlement-{secrets.token_hex(8)}",
            int(time.time()),
            current_height,
        )

        # The store created the honest allocation. Fabricate only the conflicting,
        # seller-signed allocation to produce evidence for the fault path.
        second = sign_document(
            Allocation(
                authorization=document_hash(bond.authorization.body),
                allocation_id=secrets.token_hex(32),
                buyer_tag=secrets.token_hex(32),
                product="bond",
                resource=resource,
                certificate_pubkey=credential.cert_pubkey,
            ),
            bond.seller_key,
        )
        raw_fault = canonical(
            FaultProof(
                reason="double-allocation",
                authorization=bond.authorization,
                first=package.allocation,
                second=second,
            )
        )

        maker_bond = MakerBondInfo(
            txid=bond.reference.outpoint.txid,
            vout=bond.reference.outpoint.vout,
            value=bond.value_sats,
            locktime=bond.reference.locktime,
            confirmation_time=bond.confirmation_time,
            bond_value=bond.value_sats,
            pubkey=bytes(bond.owner_key.pub),
            private_key=bond.owner_key,
        )
        maker_nicks = ("J5faultmakerone", "J5faultmakertwo")
        offers: list[Offer] = []
        for oid, maker_nick in enumerate(maker_nicks):
            proof = create_fidelity_bond_proof(
                maker_bond,
                maker_nick=maker_nick,
                taker_nick="J5faulttaker",
                current_block_height=current_height,
            )
            assert proof is not None
            bond_data = parse_fidelity_bond_proof(proof, maker_nick, "J5faulttaker")
            assert bond_data is not None
            offers.append(
                Offer(
                    counterparty=maker_nick,
                    oid=oid,
                    ordertype=OfferType.SW0_ABSOLUTE,
                    minsize=1,
                    maxsize=100_000_000,
                    txfee=0,
                    cjfee=0,
                    fidelity_bond_data=bond_data,
                )
            )

        wallet = WalletService(
            mnemonic=_TAKER_MNEMONIC,
            backend=bitcoin_core_backend,
            network="regtest",
            mixdepth_count=5,
            data_dir=tmp_path / "fault-taker-wallet",
        )
        taker = Taker(
            wallet,
            bitcoin_core_backend,
            TakerConfig(
                mnemonic=_TAKER_MNEMONIC,
                network=NetworkType.REGTEST,
                bitcoin_network=NetworkType.REGTEST,
                directory_servers=[_DIRECTORY_SERVER],
                allow_clearnet_connections=True,
                data_dir=tmp_path / "fault-taker",
            ),
        )
        verified_height = await taker._update_offers_with_bond_values(offers)
        assert verified_height == current_height
        assert all(offer.fidelity_bond_verified is True for offer in offers)

        await watcher.connect()
        publisher = _market_transport()
        assert await publisher.broadcast_fault(raw_fault) == 1
        messages = await watcher.listen_for_messages(duration=5.0)
        assert any(
            "!PUBLIC!mproof " in str(message.get("line")) for message in messages
        )
        assert len(watcher._market_faults) == 1

        taker.directory_client.clients = {_DIRECTORY_SERVER: watcher}
        assert taker._drain_and_apply_market_faults(offers, verified_height) == []
        next_period_height = (current_period + 1) * 2016 + 1
        assert period_at_height(next_period_height) == current_period + 1
        assert taker._drain_and_apply_market_faults(offers, next_period_height) == []
    finally:
        await watcher.close()
        store.close()
