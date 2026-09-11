"""Wallet-owned lifecycle for a native credential-market seller."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Literal

from jmcore.credential_market import (
    CredentialPackage,
    MarketError,
    MarketQuote,
    PaymentTerms,
    SignedDocument,
    bond_resource,
    verify_authorization,
)
from jmcore.crypto import NickIdentity
from jmcore.external_podle import ExternalPoDLE
from jmcore.market_faults import MarketFaultCache
from jmcore.market_store import MarketStore, MarketStoreUnavailableError
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.protocol import JM_VERSION, NOT_SERVING_ONION_HOSTNAME
from jmcore.settings import JoinMarketSettings
from jmcore.wallet_market import WalletMarketSellerOptions as WalletMarketSellerOptions
from jmwallet.wallet.service import WalletService

from taker.market_payments import (
    validate_payment_terms,
    verify_lightning_preimage,
    verify_onchain_payment,
)
from taker.market_service import MarketService
from taker.market_transport import MarketTransport


class WalletMarketSeller:
    """Own one seller runtime without taking ownership of its wallet or backend."""

    def __init__(
        self,
        wallet: WalletService,
        settings: JoinMarketSettings,
        options: WalletMarketSellerOptions,
    ) -> None:
        wallet_data_dir = wallet.data_dir
        if not isinstance(wallet_data_dir, Path):
            raise ValueError("Wallet market seller requires an explicit wallet data directory")
        settings_data_dir = settings.get_data_dir()
        if wallet_data_dir.resolve() != settings_data_dir.resolve():
            raise ValueError("Wallet and market seller must use the same data directory")

        settings_network = settings.network_config.network.value
        bitcoin_network = settings.network_config.bitcoin_network
        if wallet.network != (bitcoin_network.value if bitcoin_network else settings_network):
            raise ValueError("Wallet and market seller must use the same network")

        snapshot = WalletMarketSellerOptions.model_validate(options.model_dump(mode="json"))
        self.wallet = wallet
        self.settings = settings
        self._data_dir = wallet_data_dir.resolve()
        self._bond = snapshot.bond
        self._products = tuple(snapshot.products)
        self._price_sats = snapshot.price_sats
        self._quote_ttl = snapshot.quote_ttl

        self._closed = False
        self._start_attempted = False
        self._running = False
        self._starting_task: asyncio.Task[object] | None = None
        self._stop_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()

        self._store: MarketStore | None = None
        self._service: MarketService | None = None
        self._transport: MarketTransport | None = None
        self._fault_cache: MarketFaultCache | None = None
        self._nickname: str | None = None
        self._seller_public_key: str | None = None

    @property
    def running(self) -> bool:
        """Whether this runtime is currently able to publish seller responses."""
        return self._running and not self._closed

    def status(self) -> dict[str, str | None]:
        """Return only public seller runtime information."""
        state: Literal["stopped", "starting", "running"]
        if self.running:
            state = "running"
        elif self._starting_task is not None and not self._closed:
            state = "starting"
        else:
            state = "stopped"
        return {
            "state": state,
            "nickname": self._nickname,
            "seller_pubkey": self._seller_public_key,
        }

    async def start(self) -> None:
        """Open the already-activated ledger and begin relay-only service."""
        if self._closed:
            raise RuntimeError("Wallet market seller is stopped and cannot restart")
        if self._start_attempted or self._starting_task is not None or self._running:
            raise RuntimeError("Wallet market seller is already starting or running")
        task = asyncio.current_task()
        if task is None:  # pragma: no cover, asyncio always provides a task here.
            raise RuntimeError("Wallet market seller requires an asyncio task")
        self._start_attempted = True
        self._starting_task = task
        try:
            await self._start_owned_resources()
        except BaseException:
            self._closed = True
            # A concurrent stop must not cancel this task while it is performing
            # its own cleanup after startup failed or was cancelled.
            if self._starting_task is task:
                self._starting_task = None
            await self._cleanup_owned_resources()
            raise
        finally:
            if self._starting_task is task:
                self._starting_task = None

    async def stop(self) -> None:
        """Terminally stop this seller, retaining failed closes for a later retry."""
        self._closed = True
        self._running = False
        async with self._stop_lock:
            starting_task = self._starting_task
            current_task = asyncio.current_task()
            if (
                starting_task is not None
                and starting_task is not current_task
                and not starting_task.done()
            ):
                starting_task.cancel()
                await asyncio.gather(starting_task, return_exceptions=True)
            await self._cleanup_owned_resources()

    async def _start_owned_resources(self) -> None:
        store = MarketStore(
            get_market_store_path(self._data_dir),
            wallet_id=self.wallet.market_wallet_id,
            create=False,
        )
        self._store = store
        if not store.is_wallet_ledger or store.wallet_state() != "ready":
            raise MarketStoreUnavailableError("Wallet market ledger is not ready")
        store.check_commitments_path(get_used_commitments_path(self._data_dir, create=False))

        bound_keys, authorization = await self.wallet.create_market_authorization(self._bond)
        if self._closed:
            raise RuntimeError("Wallet market seller stopped during startup")

        fault_cache = MarketFaultCache(self._data_dir)
        service = MarketService(
            store,
            authorization,
            bound_keys,
            bound_keys,
            self.wallet.backend,
            products=list(self._products),
            price_sats=self._price_sats,
            quote_ttl=self._quote_ttl,
            fault_cache=fault_cache,
        )
        self._fault_cache = fault_cache
        self._service = service
        self._seller_public_key = bound_keys.signing_public_key().hex()

        transport = MarketTransport(
            directory_servers=self.settings.get_directory_servers(),
            network=self.settings.network_config.network.value,
            nick_identity=NickIdentity(JM_VERSION),
            socks_host=self.settings.tor.socks_host,
            socks_port=self.settings.tor.socks_port,
            connection_timeout=self.settings.tor.connection_timeout,
            stream_isolation=True,
            nick_auth_mode=self.settings.network_config.nick_auth_mode,
            nick_auth_directory_ids=self.settings.network_config.nick_auth_directory_ids,
            encryption_private_key=bound_keys,
            listing_callback=self._listing_callback,
            responder=self._respond_callback,
            on_fault=self._fault_callback,
            direct_location=NOT_SERVING_ONION_HOSTNAME,
            allow_clearnet_connections=self.settings.network_config.allow_clearnet_connections,
        )
        self._transport = transport
        self._nickname = transport.nick

        # The configured timeout governs Tor relay connection setup. Wallet bond
        # verification remains outside this window so it completes its full chain check.
        async with asyncio.timeout(self.settings.tor.connection_timeout):
            await transport.start()
        if self._closed:
            raise RuntimeError("Wallet market seller stopped during startup")
        self._running = True

    def _callbacks_available(self) -> bool:
        return self._running and not self._closed

    def _local_service(self) -> MarketService:
        if not self.running or self._service is None:
            raise MarketStoreUnavailableError("Wallet market seller is unavailable")
        self._service._validate_bound_keys()
        self._service.store.check_commitments_path(
            get_used_commitments_path(self._data_dir, create=False)
        )
        return self._service

    def add_podle_inventory(self, credential: ExternalPoDLE) -> None:
        """Record an externally supplied opening without exposing a spending key."""
        service = self._local_service()
        record = ExternalPoDLE.model_validate(credential.model_dump(mode="json"))
        if "podle" not in self._products or record.network != self.wallet.network:
            raise MarketError("PoDLE product or network does not match this seller")
        service.store.add_inventory("podle", record.commitment, record.model_dump(mode="json"))

    def add_bond_inventory(self) -> None:
        service = self._local_service()
        if "bond" not in self._products:
            raise MarketError("Bond product is unavailable")
        service.store.add_inventory(
            "bond", bond_resource(service.authority.bond, service.authority.period), None
        )

    def add_payment(self, terms: PaymentTerms) -> None:
        """Queue externally generated payment terms; never contact a payment wallet."""
        service = self._local_service()
        now = int(time.time())
        payment_id = validate_payment_terms(terms, self.wallet.network, now, now + self._quote_ttl)
        service.store.add_payment(terms, payment_id)

    def pending_quotes(self) -> list[SignedDocument]:
        service = self._local_service()
        return [
            document
            for document in service.store.pending(int(time.time()))
            if verify_authorization(MarketQuote.model_validate(document.body).authorization)
            == service.authority
        ]

    async def settle(
        self,
        quote_id: str,
        *,
        onchain_outpoint: str | None = None,
        preimage: bytes | None = None,
        acknowledge_ln_settlement: bool = False,
    ) -> CredentialPackage:
        """Finalize only after an authenticated local operator verifies settlement.

        Remote market messages cannot call this operation. Every awaited backend
        check is followed by runtime, key, and ledger validation before mutation.
        """
        service = self._local_service()
        if (onchain_outpoint is None) == (preimage is None):
            raise MarketError("Exactly one settlement form is required")
        document = service.store.get_quote(quote_id)
        quote = document.verified(MarketQuote, service.authority.seller_pubkey)
        if verify_authorization(quote.authorization) != service.authority:
            raise MarketError("Quote does not belong to this seller authority")
        height = await self.wallet.backend.get_block_height()
        self._local_service()
        now = int(time.time())
        quote.check(
            network=self.wallet.network,
            height=height,
            now=now,
            max_price_sats=quote.payment.amount_sats,
        )
        validate_payment_terms(quote.payment, self.wallet.network, now, quote.expires_at)
        if onchain_outpoint is not None:
            if quote.payment.rail != "onchain" or acknowledge_ln_settlement:
                raise MarketError("On-chain reference requires an on-chain quote")
            if self.wallet.backend.requires_neutrino_metadata():
                raise MarketError("On-chain settlement requires a full-node backend")
            reference = await verify_onchain_payment(
                self.wallet.backend, quote.payment, self.wallet.network, onchain_outpoint
            )
        else:
            if preimage is None or acknowledge_ln_settlement is not True:
                raise MarketError("Local Lightning settlement acknowledgment is required")
            reference = verify_lightning_preimage(
                quote.payment, self.wallet.network, preimage, now, quote.expires_at
            )
        self._local_service()
        if quote.product == "bond":
            if quote.certificate_pubkey is None:
                raise MarketError("Bond quote has no renter certificate key")
            credential = await self.wallet.create_market_bond_credential(
                quote.authorization, quote.certificate_pubkey
            )
            self._local_service()
            service.store.attach_credential(quote_id, credential.model_dump(mode="json"))
        # Do not trust a height or expiry read before an awaited payment/bond check.
        height = await self.wallet.backend.get_block_height()
        self._local_service()
        return service.store.finalize(
            quote_id, service.signing_key, reference, int(time.time()), height
        )

    async def _listing_callback(self) -> bytes:
        if not self._callbacks_available() or self._service is None:
            raise MarketError("Wallet market seller is unavailable")
        listing = await self._service.listing()
        if not self._callbacks_available():
            raise MarketError("Wallet market seller is unavailable")
        return listing

    async def _respond_callback(self, sender: str, body: dict[str, object]) -> dict[str, object]:
        if not self._callbacks_available() or self._service is None:
            return {"error": "unavailable"}
        response = await self._service.respond(sender, body)
        if not self._callbacks_available():
            return {"error": "unavailable"}
        return response

    def _fault_callback(self, raw: bytes) -> None:
        if self._callbacks_available() and self._fault_cache is not None:
            self._fault_cache.ingest(raw)

    async def _cleanup_owned_resources(self) -> None:
        """Close transport then store, preserving each failed close for retry."""
        async with self._cleanup_lock:
            close_error: BaseException | None = None
            try:
                transport = self._transport
                if transport is not None:
                    try:
                        await transport.close()
                    except BaseException as exc:
                        close_error = exc
                    else:
                        self._transport = None
            finally:
                store = self._store
                if store is not None:
                    try:
                        store.close()
                    except BaseException as exc:
                        if close_error is None:
                            close_error = exc
                    else:
                        self._store = None
            if self._transport is None and self._store is None:
                self._service = None
                self._fault_cache = None
            if close_error is not None:
                raise close_error
