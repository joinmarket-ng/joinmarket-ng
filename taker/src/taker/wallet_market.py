"""Wallet-owned lifecycle for a native credential-market seller."""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jmcore import experimental
from jmcore.credential_market import (
    MarketAuthorization,
    MarketError,
    MarketQuote,
    SignedDocument,
    bond_resource,
    verify_authorization,
)
from jmcore.crypto import NickIdentity
from jmcore.experimental import warn_experimental
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.market_faults import MarketFaultCache
from jmcore.market_keys import BoundMarketKeys
from jmcore.market_store import (
    BackingLookup,
    HoldInvoice,
    MarketStore,
    MarketStoreError,
    MarketStoreUnavailableError,
)
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.protocol import JM_VERSION, NOT_SERVING_ONION_HOSTNAME
from jmcore.settings import JoinMarketSettings
from jmcore.wallet_market import WalletMarketSellerOptions as WalletMarketSellerOptions
from jmwallet.wallet.service import WalletService

from taker.market_service import MarketService
from taker.market_settlement import (
    HoldInvoiceBackend,
    MarketSettlement,
    SaleCheck,
    seller_invoice_client,
)
from taker.market_transport import MarketTransport


@dataclass
class _Authority:
    """One additional bond authority: its own service and its own relay nick."""

    options: WalletMarketSellerOptions
    keys: BoundMarketKeys
    service: MarketService
    transport: MarketTransport


class WalletMarketSeller:
    """Own one seller runtime without taking ownership of its wallet or backend.

    The runtime serves ``options`` and any ``additional`` bond authorities, each
    under its own nick, over one store, one invoice client, and one settlement
    reconciler, which finalizes each hold with the key of the authority that
    quoted it.
    """

    def __init__(
        self,
        wallet: WalletService,
        settings: JoinMarketSettings,
        options: WalletMarketSellerOptions,
        *,
        invoices: AbstractAsyncContextManager[HoldInvoiceBackend] | None = None,
        backing_sats: BackingLookup | None = None,
        sale_check: SaleCheck | None = None,
        additional: Sequence[WalletMarketSellerOptions] = (),
    ) -> None:
        """``invoices`` replaces the ``[market]`` LND node; this seller enters and closes it.

        ``backing_sats`` replaces the wallet-cache backing lookup and restricts
        PoDLE sales to inventory it values, for example one mixdepth's coins.
        ``sale_check`` is asked before each held payment is settled.
        ``additional`` lists further bonds to serve, one authority and nick each.
        """
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
        extra = tuple(
            WalletMarketSellerOptions.model_validate(option.model_dump(mode="json"))
            for option in additional
        )
        bonds = [snapshot.bond, *(option.bond for option in extra)]
        if len({(bond.txid, bond.vout) for bond in bonds}) != len(bonds):
            raise ValueError("Each seller authority needs its own bond")
        self.wallet = wallet
        self.settings = settings
        self._data_dir = wallet_data_dir.resolve()
        self._bond = snapshot.bond
        self._products = tuple(snapshot.products)
        self._price_sats = snapshot.price_sats
        self._quote_ttl = snapshot.quote_ttl
        self._additional_options = extra
        self._invoice_client = invoices
        self._custom_backing = backing_sats
        self._sale_check = sale_check

        self._closed = False
        self._start_attempted = False
        self._running = False
        self._admission_closed = False
        self._active_admissions = 0
        self._admissions_idle = asyncio.Event()
        self._admissions_idle.set()
        self._starting_task: asyncio.Task[object] | None = None
        self._stop_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()

        self._store: MarketStore | None = None
        self._service: MarketService | None = None
        self._transport: MarketTransport | None = None
        self._keys: BoundMarketKeys | None = None
        self._additional: list[_Authority] = []
        self._fault_cache: MarketFaultCache | None = None
        self._nickname: str | None = None
        self._seller_public_key: str | None = None
        self._resources = AsyncExitStack()
        self._settlement: MarketSettlement | None = None
        self._settlement_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Whether this runtime is currently able to publish seller responses."""
        return self._running and not self._closed

    @property
    def accepting(self) -> bool:
        """Whether callbacks can allocate new quotes or generate listings."""
        return self.running and not self._admission_closed

    async def close_admission(self) -> None:
        """Stop new callback allocations and await previously admitted callbacks.

        The sticky boundary takes effect when this method executes, not when
        its coroutine is created. Canceling this wait never cancels admitted
        callbacks or settlement. Startup does not reopen admission.

        Transport caches may replay old quotes, and publication may follow a
        callback's completion. Existing quotes remain payable; unresolved RPC
        effects remain unknown. This is neither financial quiescence nor
        clearance to stop nodes, release ownership, or remove history.
        """
        self._admission_closed = True
        await self._admissions_idle.wait()

    def _admit_callback(self) -> bool:
        if not self.accepting:
            return False
        # No await separates the gate from registration on this event loop.
        self._active_admissions += 1
        self._admissions_idle.clear()
        return True

    def _admission_finished(self) -> None:
        self._active_admissions -= 1
        if self._active_admissions == 0:
            self._admissions_idle.set()

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
        invoice_client = self._invoice_client or seller_invoice_client(self.settings)
        store = MarketStore(
            get_market_store_path(self._data_dir),
            wallet_id=self.wallet.market_wallet_id,
            create=False,
        )
        self._store = store
        if not store.is_wallet_ledger or store.wallet_state() != "ready":
            raise MarketStoreUnavailableError("Wallet market ledger is not ready")
        store.check_commitments_path(get_used_commitments_path(self._data_dir, create=False))
        # One seller runtime per store owns hold-invoice settlement.
        self._resources.enter_context(store.seller_runtime())
        invoices = await self._resources.enter_async_context(invoice_client)

        bound_keys, authorization = await self.wallet.create_market_authorization(self._bond)
        if self._closed:
            raise RuntimeError("Wallet market seller stopped during startup")

        fault_cache = MarketFaultCache(self._data_dir)
        self._fault_cache = fault_cache
        primary = WalletMarketSellerOptions(
            bond=self._bond,
            products=list(self._products),
            price_sats=self._price_sats,
            quote_ttl=self._quote_ttl,
        )
        service = self._new_service(
            store, invoices, fault_cache, authorization, bound_keys, primary
        )
        self._service = service
        self._keys = bound_keys
        self._seller_public_key = bound_keys.signing_public_key().hex()
        settlement = MarketSettlement(
            store,
            invoices,
            self._seller_key_for,
            lambda: self.wallet.backend.get_block_height(),
            credential_provider=self._provide_credential,
            sale_check=self._sale_check,
        )

        transport = self._new_transport(bound_keys, self._listing_callback, self._respond_callback)
        self._transport = transport
        self._nickname = transport.nick
        for options in self._additional_options:
            keys, document = await self.wallet.create_market_authorization(options.bond)
            if self._closed:
                raise RuntimeError("Wallet market seller stopped during startup")
            extra = self._new_service(store, invoices, fault_cache, document, keys, options)
            self._additional.append(
                _Authority(
                    options=options,
                    keys=keys,
                    service=extra,
                    transport=self._new_transport(
                        keys,
                        functools.partial(self._listing_for, extra),
                        functools.partial(self._respond_for, extra),
                    ),
                )
            )
        warn_experimental([experimental.CREDENTIAL_MARKET], str(self.wallet.network))

        # The configured timeout governs Tor relay connection setup. Wallet bond
        # verification remains outside this window so it completes its full chain check.
        for relay in [transport, *(record.transport for record in self._additional)]:
            async with asyncio.timeout(self.settings.tor.connection_timeout):
                await relay.start()
            if self._closed:
                raise RuntimeError("Wallet market seller stopped during startup")
        self._running = True
        self._settlement = settlement
        self._settlement_task = asyncio.create_task(settlement.run())

    def _new_service(
        self,
        store: MarketStore,
        invoices: HoldInvoiceBackend,
        fault_cache: MarketFaultCache,
        authorization: SignedDocument,
        keys: BoundMarketKeys,
        options: WalletMarketSellerOptions,
    ) -> MarketService:
        return MarketService(
            store,
            authorization,
            keys,
            keys,
            self.wallet.backend,
            invoices,
            products=list(options.products),
            price_sats=options.price_sats,
            quote_ttl=options.quote_ttl,
            fault_cache=fault_cache,
            backing_sats=self._custom_backing or self._backing_sats,
            require_backing=self._custom_backing is not None,
        )

    def _new_transport(
        self,
        keys: BoundMarketKeys,
        listing: Callable[[], Awaitable[bytes]],
        responder: Callable[[str, dict[str, object]], Awaitable[dict[str, object]]],
    ) -> MarketTransport:
        """A relay-only transport under a fresh nick, so authorities stay unlinked."""
        return MarketTransport(
            directory_servers=self.settings.get_directory_servers(),
            network=self.settings.network_config.network.value,
            nick_identity=NickIdentity(JM_VERSION),
            socks_host=self.settings.tor.socks_host,
            socks_port=self.settings.tor.socks_port,
            connection_timeout=self.settings.tor.connection_timeout,
            stream_isolation=True,
            nick_auth_mode=self.settings.network_config.nick_auth_mode,
            nick_auth_directory_ids=self.settings.network_config.nick_auth_directory_ids,
            encryption_private_key=keys,
            listing_callback=listing,
            responder=responder,
            on_fault=self._fault_callback,
            direct_location=NOT_SERVING_ONION_HOSTNAME,
            allow_clearnet_connections=self.settings.network_config.allow_clearnet_connections,
        )

    def _backing_sats(self, txid: str, vout: int) -> int | None:
        """The value of a confirmed wallet UTXO backing a PoDLE, from the wallet cache."""
        for utxos in self.wallet.utxo_cache.values():
            for utxo in utxos:
                if utxo.txid == txid and utxo.vout == vout:
                    return utxo.value if utxo.confirmations > 0 else None
        return None

    def set_price_sats(self, price_sats: int, *, bond: ExternalPoDLEOutpoint | None = None) -> None:
        """Price later listings and quotes of ``bond``'s authority (the first by default)."""
        if bond is None or (bond.txid, bond.vout) == (self._bond.txid, self._bond.vout):
            snapshot = WalletMarketSellerOptions(
                bond=self._bond, products=list(self._products), price_sats=price_sats
            )
            self._price_sats = snapshot.price_sats
            if self._service is not None:
                self._service.price_sats = snapshot.price_sats
            return
        record = self._additional_authority(bond)
        record.options = WalletMarketSellerOptions(
            bond=bond,
            products=list(record.options.products),
            price_sats=price_sats,
            quote_ttl=record.options.quote_ttl,
        )
        record.service.price_sats = record.options.price_sats

    def _additional_authority(self, bond: ExternalPoDLEOutpoint) -> _Authority:
        for record in self._additional:
            if (record.options.bond.txid, record.options.bond.vout) == (bond.txid, bond.vout):
                return record
        raise MarketError("This seller does not serve that bond")

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

    def _services(self) -> list[MarketService]:
        """Every served authority's service, the first one first."""
        primary = self._local_service()
        return [primary, *(record.service for record in self._additional)]

    def _served(self, authority: MarketAuthorization) -> MarketService | None:
        for service in self._services():
            if service.authority == authority:
                return service
        return None

    def add_podle_inventory(self, credential: ExternalPoDLE) -> None:
        """Record an externally supplied opening without exposing a spending key."""
        service = self._local_service()
        record = ExternalPoDLE.model_validate(credential.model_dump(mode="json"))
        if "podle" not in self._products or record.network != self.wallet.network:
            raise MarketError("PoDLE product or network does not match this seller")
        service.store.add_inventory("podle", record.commitment, record.model_dump(mode="json"))

    def sold_podle(self, payment_hash: bytes) -> ExternalPoDLE | None:
        """The stocked PoDLE credential one of this seller's hold invoices sells, if any."""
        store = self._store
        if store is None or self._closed:
            raise MarketStoreUnavailableError("Wallet market seller is unavailable")
        credential = store.hold_inventory_credential(payment_hash)
        return None if credential is None else ExternalPoDLE.model_validate(credential)

    def sold_product(self, payment_hash: bytes) -> str | None:
        """The product one of this seller's hold invoices sells, if any."""
        store = self._store
        if store is None or self._closed:
            raise MarketStoreUnavailableError("Wallet market seller is unavailable")
        return store.hold_product(payment_hash)

    def add_bond_inventory(self, bond: ExternalPoDLEOutpoint | None = None) -> None:
        """Stock one lease of ``bond``'s authority (the first by default) for its period."""
        service = self._local_service()
        if bond is not None and (bond.txid, bond.vout) != (self._bond.txid, self._bond.vout):
            record = self._additional_authority(bond)
            record.service._validate_bound_keys()
            products: Sequence[str] = record.options.products
            service = record.service
        else:
            products = self._products
        if "bond" not in products:
            raise MarketError("Bond product is unavailable")
        service.store.add_inventory(
            "bond", bond_resource(service.authority.bond, service.authority.period), None
        )

    def pending_quotes(self) -> list[SignedDocument]:
        services = self._services()
        authorities = [service.authority for service in services]
        return [
            document
            for document in services[0].store.pending(int(time.time()))
            if verify_authorization(MarketQuote.model_validate(document.body).authorization)
            in authorities
        ]

    def _seller_key_for(self, hold: HoldInvoice) -> BoundMarketKeys:
        """The key of the authority that quoted ``hold``.

        A quote of an authority not served now (an earlier period or run) gets
        the first authority's key, so the store still refunds an expired one.
        """
        keys, store = self._keys, self._store
        if keys is None or store is None:
            raise MarketStoreUnavailableError("Wallet market seller is unavailable")
        if self._additional:
            try:
                document = store.get_quote(hold.quote_id)
                authority = verify_authorization(
                    MarketQuote.model_validate(document.body).authorization
                )
            except (MarketError, MarketStoreError, ValueError):
                return keys
            for record in self._additional:
                if record.service.authority == authority:
                    return record.keys
        return keys

    async def _provide_credential(self, quote_id: str) -> None:
        """Sign the bond lease for an accepted bond quote with this wallet.

        Called by the settlement reconciler once the buyer's payment is held.
        Every await is followed by runtime, key, and ledger validation.
        """
        store = self._local_service().store
        document = store.get_quote(quote_id)
        authority = verify_authorization(MarketQuote.model_validate(document.body).authorization)
        quote = document.verified(MarketQuote, authority.seller_pubkey)
        if quote.product != "bond":
            return
        service = self._served(authority)
        if service is None:
            raise MarketError("Quote does not belong to this seller authority")
        service._validate_bound_keys()
        if quote.certificate_pubkey is None:
            raise MarketError("Bond quote has no renter certificate key")
        credential = await self.wallet.create_market_bond_credential(
            quote.authorization, quote.certificate_pubkey
        )
        self._local_service()
        service._validate_bound_keys()
        store.attach_credential(quote_id, credential.model_dump(mode="json"))

    async def _listing_callback(self) -> bytes:
        return await self._listing_for(self._service)

    async def _listing_for(self, service: MarketService | None) -> bytes:
        if service is None or not self._admit_callback():
            raise MarketError("Wallet market seller is unavailable")
        try:
            listing = await service.listing()
            if not self._callbacks_available():
                raise MarketError("Wallet market seller is unavailable")
            return listing
        finally:
            self._admission_finished()

    async def _respond_callback(self, sender: str, body: dict[str, object]) -> dict[str, object]:
        return await self._respond_for(self._service, sender, body)

    async def _respond_for(
        self, service: MarketService | None, sender: str, body: dict[str, object]
    ) -> dict[str, object]:
        if not self._callbacks_available() or service is None:
            return {"error": "unavailable"}
        admission = body.get("action") != "delivery"
        if admission and not self._admit_callback():
            return {"error": "unavailable"}
        try:
            response = await service.respond(sender, body)
            if not self._callbacks_available():
                return {"error": "unavailable"}
            return response
        finally:
            if admission:
                self._admission_finished()

    def _fault_callback(self, raw: bytes) -> None:
        if self._callbacks_available() and self._fault_cache is not None:
            self._fault_cache.ingest(raw)

    async def _cleanup_owned_resources(self) -> None:
        """Stop settlement, close transports then store, keeping failed closes for retry."""
        async with self._cleanup_lock:
            close_error: BaseException | None = None
            settlement_task, self._settlement_task = self._settlement_task, None
            if settlement_task is not None:
                settlement_task.cancel()
                await asyncio.gather(settlement_task, return_exceptions=True)
            try:
                transport = self._transport
                if transport is not None:
                    try:
                        await transport.close()
                    except BaseException as exc:
                        close_error = exc
                    else:
                        self._transport = None
                failed: list[_Authority] = []
                for record in self._additional:
                    try:
                        await record.transport.close()
                    except BaseException as exc:
                        close_error = close_error or exc
                        failed.append(record)
                self._additional = failed
            finally:
                try:
                    # Release the invoice client and the seller-runtime lock.
                    await self._resources.aclose()
                except BaseException as exc:
                    close_error = close_error or exc
                store = self._store
                if store is not None:
                    try:
                        store.close()
                    except BaseException as exc:
                        if close_error is None:
                            close_error = exc
                    else:
                        self._store = None
            if self._transport is None and not self._additional and self._store is None:
                self._service = None
                self._keys = None
                self._fault_cache = None
            if close_error is not None:
                raise close_error
