"""The maker's market sales: PoDLE commitments and idle bond leases.

With ``[maker] sell_podle`` the maker sells commitments for confirmed coins of
the mixdepth its offers are built from. Each sale is invoiced from the node of
the sold coin's own mixdepth, so a buyer never sees two mixdepths together,
and its backing is checked again before the payment settles. A maker never
uses its own commitments, and takers do not judge maker inputs by their
remaining PoDLE indices, so a sale only reveals the coin to its buyer. Seller
authority is the bond the maker advertises.

With ``[maker] rent_bonds`` the maker also rents out its other owned bonds,
one retarget period at a time, each under its own seller authority and nick,
invoiced from mixdepth 0's node where bonds live. A bond is never rented in a
period this data directory may have advertised it in (see
:mod:`maker.bond_leases`). New leases are stocked only in the first half of a
period, and the price follows the rest of the period a lease lasts.

The seller runtime lives in the taker package; a maker installed without it
keeps making and logs why it does not sell.
"""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.channel_ring import ChannelRingNodeConfig
from jmcore.credential_market import (
    BLOCKS_PER_PERIOD,
    BLOCKS_PER_YEAR,
    MARKET_PODLE_RETRIES,
    MarketError,
    lease_blocks,
    period_at_height,
)
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.market_store import (
    MarketStore,
    MarketStoreConflictError,
    MarketStoreError,
    blocked_bond_outpoints,
)
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.podle import generate_podle
from jmcore.settings import JoinMarketSettings
from jmcore.wallet_market import WalletMarketSellerOptions
from jmwallet.wallet.models import UTXOInfo
from loguru import logger

from maker.bond_leases import (
    AdvertisementRecordError,
    advertisement_record_path,
    bond_references,
    record_advertisement,
)
from maker.fidelity import FidelityBondInfo, find_fidelity_bonds

if TYPE_CHECKING:
    from jmcore.market_store import HoldInvoice
    from jmswap.lnd_invoices import LndInvoiceClient
    from jmwallet.wallet.service import WalletService
    from taker.wallet_market import WalletMarketSeller

    from maker.bot import MakerBot

#: Makers reject commitments younger than their taker_utxo_age (5 by default),
#: so a younger coin would sell a credential its buyer cannot use.
SALE_MIN_CONFIRMATIONS = 5
REFRESH_SECONDS = 60.0
RETRY_SECONDS = 300.0
#: The ``[market]`` seller node, probed for earlier invoices but never routed to.
MARKET_NODE = "[market]"
RETARGET_BLOCKS = BLOCKS_PER_PERIOD
#: A rented bond must stay locked this many blocks past its lease, for slow blocks.
LEASE_LOCK_MARGIN_BLOCKS = 2016
#: Bonds live in mixdepth 0, whose node invoices their leases.
BOND_MIXDEPTH = 0


class MarketSalesUnavailableError(Exception):
    """A precondition for selling is missing; making continues without sales."""


def sellable(utxo: UTXOInfo) -> bool:
    """A coin whose commitments a buyer can use: confirmed enough, spendable, not a bond."""
    return (
        utxo.confirmations >= SALE_MIN_CONFIRMATIONS
        and not utxo.frozen
        and utxo.locktime is None
        and utxo.value > 0
    )


def podle_credential(
    private_key: bytes, utxo: UTXOInfo, index: int, *, network: str, blockheight: int
) -> ExternalPoDLE:
    """The external PoDLE opening of ``utxo`` at NUMS ``index``."""
    proof = generate_podle(private_key, f"{utxo.txid}:{utxo.vout}", index, p2tr=utxo.is_p2tr)
    return ExternalPoDLE.model_validate(
        {
            "version": 1,
            "network": network,
            "outpoint": {"txid": utxo.txid, "vout": utxo.vout},
            "P": proof.p.hex(),
            "P2": proof.p2.hex(),
            "sig": proof.sig.hex(),
            "e": proof.e.hex(),
            "commitment": proof.commitment.hex(),
            "index": index,
            "scriptpubkey": utxo.scriptpubkey.lower(),
            "blockheight": blockheight,
        }
    )


def lease_price_sats(value: int, annual_rate: float, height: int) -> int:
    """Rent for the rest of the period: ``annual_rate`` per year on the locked amount."""
    return max(1, math.ceil(value * annual_rate * lease_blocks(height) / BLOCKS_PER_YEAR))


def lease_admission_open(height: int) -> bool:
    """Whether new leases are stocked at ``height``: only in a period's first half."""
    return lease_blocks(height) > RETARGET_BLOCKS // 2


def _seller_options(bond: FidelityBondInfo, product: str, price: int) -> WalletMarketSellerOptions:
    return WalletMarketSellerOptions.model_validate(
        {"bond": {"txid": bond.txid, "vout": bond.vout}, "products": [product], "price_sats": price}
    )


def _invoice_client(node: ChannelRingNodeConfig) -> LndInvoiceClient:
    from jmswap.lnd_invoices import LndInvoiceClient

    return LndInvoiceClient(
        node.lnd_grpc_url.removeprefix("https://"),
        Path(node.lnd_tls_cert_path).expanduser().read_bytes(),
        Path(node.lnd_macaroon_path).expanduser().read_bytes(),
    )


class MarketSales:
    """Runs the wallet's seller beside a maker: PoDLE sales and idle bond leases.

    A missing precondition (no owned bond, no activated ledger, no node for the
    offer mixdepth) pauses sales with a warning and is retried; it never stops
    the maker. The configuration is read from the maker's ``sell_podle``,
    ``rent_bonds`` and ``bond_rent_rate``.
    """

    def __init__(self, settings: JoinMarketSettings, bot: MakerBot) -> None:
        self._settings = settings
        self._bot = bot
        self._seller: WalletMarketSeller | None = None
        self._period: int | None = None
        # The only mixdepth whose coins are quoted.
        self._mixdepth: int | None = None
        self._stocked: set[tuple[str, int]] = set()
        # Whether the running seller's first authority sells PoDLEs.
        self._podle_served: bool = bot.config.sell_podle
        # The advertised bond sold under, if any, and the bonds rented out.
        self._served: tuple[tuple[str, int] | None, tuple[tuple[str, int], ...]] | None = None
        self._rented: list[FidelityBondInfo] = []
        self._stocked_bonds: set[tuple[str, int]] = set()
        self._task: asyncio.Task[None] | None = None
        self._last_warning: str | None = None
        self._last_note: str | None = None

    @property
    def mixdepth(self) -> int | None:
        """The mixdepth currently sold from, if any."""
        return self._mixdepth

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="maker-market-sales")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_seller()

    async def run(self) -> None:
        """Sell while the maker runs: wait for its bond and offers, then refresh."""
        # The maker runs only once it selected and recorded its advertised bond,
        # so no rental is chosen before that bond is known.
        while not self._bot.running:
            await asyncio.sleep(1.0)
        while True:
            try:
                await self._ensure_seller()
                await self.refresh()
                delay = REFRESH_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._warn(exc)
                await self._stop_seller()
                delay = RETRY_SECONDS
            await asyncio.sleep(delay)

    def _warn(self, exc: Exception) -> None:
        known = isinstance(
            exc,
            MarketSalesUnavailableError | MarketError | MarketStoreError | OSError | ValueError,
        )
        reason = str(exc) if known else type(exc).__name__
        message = f"Market sales paused: {reason}; retrying in {RETRY_SECONDS:.0f}s"
        if message != self._last_warning:
            logger.warning(message)
            self._last_warning = message
        else:
            logger.debug(message)

    async def _ensure_seller(self) -> None:
        """Start a seller, or replace one whose period or served bonds changed."""
        wallet = self._bot.wallet
        config = self._bot.config
        height = await wallet.backend.get_block_height()
        period = period_at_height(height)
        podle_bond = self._bot.fidelity_bond if config.sell_podle else None
        rentals = await self._rentals(height) if config.rent_bonds else []
        served = (
            None if podle_bond is None else (podle_bond.txid, podle_bond.vout),
            tuple((bond.txid, bond.vout) for bond in rentals),
        )
        if (
            self._seller is not None
            and self._seller.running
            and self._period == period
            and self._served == served
        ):
            return
        await self._stop_seller()
        try:
            from taker.market_settlement import MultiNodeHoldInvoices, seller_invoice_client
            from taker.podle_purchase import market_fee_rate, seller_price_sats
            from taker.wallet_market import WalletMarketSeller
        except ImportError as exc:
            raise MarketSalesUnavailableError("selling needs the jm-taker package") from exc
        if podle_bond is None and not rentals:
            raise MarketSalesUnavailableError(
                "selling needs an owned fidelity bond as seller authority"
                if config.sell_podle
                else "no idle fidelity bond can be rented now"
            )
        if config.sell_podle and podle_bond is None:
            self._note("PoDLE sales need an advertised fidelity bond; only renting bonds")
        self._require_ledger(wallet)
        nodes = config.channel_ring.nodes
        if not nodes:
            raise MarketSalesUnavailableError("no Lightning node is configured")
        options: list[WalletMarketSellerOptions] = []
        price: int | None = None
        if podle_bond is not None:
            price = config.podle_price_sats or seller_price_sats(
                await market_fee_rate(wallet.backend)
            )
            options.append(_seller_options(podle_bond, "podle", price))
        options.extend(
            _seller_options(
                bond, "bond", lease_price_sats(bond.value, config.bond_rent_rate, height)
            )
            for bond in rentals
        )
        clients = {name: _invoice_client(node) for name, node in nodes.items()}
        if self._settings.market.lnd_grpc_url and MARKET_NODE not in clients:
            # Never invoices, but may hold invoices of a seller that used this
            # store before; an unknown invoice must be unknown to every node.
            clients[MARKET_NODE] = seller_invoice_client(self._settings)
        invoices = MultiNodeHoldInvoices(clients, self._invoice_node)
        seller = WalletMarketSeller(
            wallet,
            self._settings,
            options[0],
            invoices=invoices,
            backing_sats=self._backing_sats,
            sale_check=self._sale_check,
            additional=options[1:],
        )
        self._seller = seller
        self._podle_served = podle_bond is not None
        self._stocked.clear()
        self._stocked_bonds.clear()
        await seller.start()
        self._period = period
        self._served = served
        self._rented = rentals
        self._last_warning = None
        if price is not None:
            logger.info("PoDLE seller started at {} sats per commitment", price)
        if rentals:
            logger.info("Renting out {} idle fidelity bond(s) for period {}", len(rentals), period)

    async def _rentals(self, height: int) -> list[FidelityBondInfo]:
        """The owned bonds to rent out now: never the advertised one.

        A bond is admitted once every period this data directory may have
        advertised it in has ended, while its key is in this wallet and it stays
        locked well past the lease; new bonds are admitted only in the first
        half of a period. A bond served or stocked in this period stays until
        the period ends, whatever its admission state, so a held payment for it
        is always settled or refunded by its own authority.
        """
        wallet = self._bot.wallet
        data_dir = wallet.data_dir
        if data_dir is None:
            return []
        if BOND_MIXDEPTH not in self._bot.config.channel_ring.mixdepth_nodes:
            self._note("Bond rental paused: no Lightning node for mixdepth 0 to invoice leases")
            return []
        period = period_at_height(height)
        advertised = self._bot.fidelity_bond
        skip = None if advertised is None else (advertised.txid, advertised.vout)
        owned = [
            bond
            for bond in await find_fidelity_bonds(wallet, BOND_MIXDEPTH)
            if bond.private_key is not None and (bond.txid, bond.vout) != skip
        ]
        kept = {(b.txid, b.vout) for b in self._rented} if self._period == period else set()
        try:
            kept |= await asyncio.to_thread(
                blocked_bond_outpoints,
                get_market_store_path(data_dir),
                bond_references(owned, str(wallet.network)),
                height,
            )
        except MarketStoreError:
            pass
        admitted: set[tuple[str, int]] = set()
        try:
            # Created when absent, counting every bond as advertised through the
            # next period.
            record = await asyncio.to_thread(
                record_advertisement, advertisement_record_path(data_dir), period
            )
        except AdvertisementRecordError as exc:
            self._note(f"Bond rental paused: {exc}")
        else:
            if lease_admission_open(height):
                # Probabilistic: fails only if the lease's blocks average over
                # twice the target interval.
                locked_until = time.time() + (lease_blocks(height) + LEASE_LOCK_MARGIN_BLOCKS) * 600
                admitted = {
                    (bond.txid, bond.vout)
                    for bond in owned
                    if bond.locktime > locked_until
                    and record.rentable((bond.txid, bond.vout), period)
                }
        served = kept | admitted
        return sorted(
            (bond for bond in owned if (bond.txid, bond.vout) in served),
            key=lambda bond: (bond.txid, bond.vout),
        )

    def _note(self, message: str) -> None:
        """Warn once about a condition that limits sales without pausing them."""
        if message != self._last_note:
            logger.warning(message)
            self._last_note = message

    def _require_ledger(self, wallet: WalletService) -> None:
        data_dir = wallet.data_dir
        if data_dir is None:
            raise MarketSalesUnavailableError("selling needs a wallet data directory")
        diagnosis = MarketStore.diagnose_wallet(
            get_market_store_path(data_dir),
            wallet_id=wallet.market_wallet_id,
            commitments_path=get_used_commitments_path(data_dir, create=False),
        )
        if diagnosis["state"] != "ready":
            raise MarketSalesUnavailableError(
                f"the wallet market ledger is {diagnosis['state']} ({diagnosis['reason']}); "
                "activate it once with jm-maker activate-market-ledger"
            )

    async def refresh(self) -> None:
        """Follow the offer mixdepth, reprice, and stock new coins and leases."""
        seller = self._seller
        if seller is None:
            return
        if self._podle_served:
            await self._refresh_podle(seller)
        if self._rented:
            await self._refresh_leases(seller)

    async def _refresh_podle(self, seller: WalletMarketSeller) -> None:
        offer_mixdepth = await self._bot.offer_manager.current_offer_mixdepth()
        mapping = self._bot.config.channel_ring.mixdepth_nodes
        has_node = offer_mixdepth is not None and offer_mixdepth in mapping
        target = offer_mixdepth if has_node else None
        if target != self._mixdepth:
            # A quote already reserved keeps its coin, and so its coin's node.
            self._mixdepth = target
            if target is None:
                logger.info("PoDLE sales idle: no offer mixdepth with a Lightning node")
            else:
                logger.info("Selling PoDLE commitments of mixdepth {}", target)
        if self._bot.config.podle_price_sats is None:
            from taker.podle_purchase import market_fee_rate, seller_price_sats

            seller.set_price_sats(
                seller_price_sats(await market_fee_rate(self._bot.wallet.backend))
            )
        if target is not None:
            await self._stock(seller, target)

    async def _refresh_leases(self, seller: WalletMarketSeller) -> None:
        """Reprice each rented bond for the rest of the period and stock its lease once."""
        height = await self._bot.wallet.backend.get_block_height()
        for bond in self._rented:
            outpoint = ExternalPoDLEOutpoint(txid=bond.txid, vout=bond.vout)
            seller.set_price_sats(
                lease_price_sats(bond.value, self._bot.config.bond_rent_rate, height),
                bond=outpoint,
            )
            if (bond.txid, bond.vout) in self._stocked_bonds:
                continue
            # A lease stocked (or sold) by an earlier run of this period conflicts.
            with suppress(MarketStoreConflictError):
                seller.add_bond_inventory(outpoint)
            self._stocked_bonds.add((bond.txid, bond.vout))

    async def _stock(self, seller: WalletMarketSeller, mixdepth: int) -> None:
        """Add every NUMS index of each new sellable coin, once its backing verifies."""
        from taker.market_service import verify_podle_backing

        wallet = self._bot.wallet
        tip: int | None = None
        for utxo in list(wallet.utxo_cache.get(mixdepth, [])):
            outpoint = (utxo.txid, utxo.vout)
            if outpoint in self._stocked or not sellable(utxo):
                continue
            key = wallet.get_key_for_address(utxo.address)
            if key is None:
                continue
            height = utxo.height
            if height is None:
                if tip is None:
                    tip = await wallet.backend.get_block_height()
                height = tip - utxo.confirmations + 1
            secret = key.get_private_key_bytes()
            credentials = [
                podle_credential(secret, utxo, index, network=wallet.network, blockheight=height)
                for index in range(MARKET_PODLE_RETRIES)
            ]
            try:
                # The buyer runs this exact check on delivery: never sell what fails it.
                await verify_podle_backing(credentials[0], wallet.backend)
            except MarketError:
                logger.debug("A coin's backing does not verify yet; it is stocked later")
                continue
            for credential in credentials:
                with suppress(MarketStoreConflictError):
                    seller.add_podle_inventory(credential)
            self._stocked.add(outpoint)

    def _backing_sats(self, txid: str, vout: int) -> int | None:
        """A coin's value while it is sellable in the mixdepth sold from, else unknown."""
        coin = self._sellable_coin(txid, vout)
        if coin is None or coin.mixdepth != self._mixdepth:
            return None
        return coin.value

    def _invoice_node(self, payment_hash: bytes) -> str | None:
        """The node of the sold coin's own mixdepth, also after the coin is spent.

        Derived from the reservation, never from the current offer mixdepth, so
        no retry or restart invoices one mixdepth's coin from another's node,
        and a reservation whose coin was spent can still be canceled there.
        A bond lease is invoiced from mixdepth 0's node, where bonds live.
        """
        seller = self._seller
        if seller is None:
            return None
        credential = seller.sold_podle(payment_hash)
        if credential is None:
            if seller.sold_product(payment_hash) == "bond":
                return self._bot.config.channel_ring.mixdepth_nodes.get(BOND_MIXDEPTH)
            return None
        mixdepth = self._coin_mixdepth(credential)
        if mixdepth is None:
            logger.warning("A sold PoDLE coin is not in this wallet; it cannot be invoiced")
            return None
        return self._bot.config.channel_ring.mixdepth_nodes.get(mixdepth)

    def _coin_mixdepth(self, credential: ExternalPoDLE) -> int | None:
        """The mixdepth of a credential's coin, from the UTXO set or its address."""
        wallet = self._bot.wallet
        outpoint = (credential.outpoint.txid, credential.outpoint.vout)
        for mixdepth, coins in wallet.utxo_cache.items():
            if any((utxo.txid, utxo.vout) == outpoint for utxo in coins):
                return mixdepth
        try:
            address = scriptpubkey_to_address(
                bytes.fromhex(credential.scriptpubkey), credential.network
            )
        except ValueError:
            return None
        path = wallet.address_cache.get(address)
        return None if path is None else path[0]

    async def _sale_check(self, hold: HoldInvoice) -> bool:
        """Refund a held payment whose coin was spent since it was quoted."""
        from taker.market_service import podle_backing_matches

        seller = self._seller
        if seller is None:
            raise MarketSalesUnavailableError("the market seller stopped")
        credential = seller.sold_podle(hold.payment_hash)
        if credential is None:
            return hold.product != "podle"
        return await podle_backing_matches(credential, self._bot.wallet.backend)

    def _sellable_coin(self, txid: str, vout: int) -> UTXOInfo | None:
        for coins in self._bot.wallet.utxo_cache.values():
            for utxo in coins:
                if (utxo.txid, utxo.vout) == (txid, vout):
                    return utxo if sellable(utxo) else None
        return None

    async def _stop_seller(self) -> None:
        seller, self._seller = self._seller, None
        self._mixdepth = None
        self._period = None
        self._served = None
        self._rented = []
        if seller is not None:
            try:
                await seller.stop()
            except Exception as exc:
                logger.warning("The market seller did not stop cleanly: {}", type(exc).__name__)
