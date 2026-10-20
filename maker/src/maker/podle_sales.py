"""PoDLE sales from the maker's current offer mixdepth (``[maker] sell_podle``).

The maker sells commitments for confirmed coins of the mixdepth its offers are
built from. Each sale is invoiced from the node of the sold coin's own
mixdepth, so a buyer never sees two mixdepths together, and its backing is
checked again before the payment settles. A maker never uses its own
commitments, and takers do not judge maker inputs by their remaining PoDLE
indices, so a sale only reveals the coin to its buyer. Seller authority is the
bond the maker advertises.

The seller runtime lives in the taker package; a maker installed without it
keeps making and logs why it does not sell.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.channel_ring import ChannelRingNodeConfig
from jmcore.credential_market import MARKET_PODLE_RETRIES, MarketError, period_at_height
from jmcore.external_podle import ExternalPoDLE
from jmcore.market_store import MarketStore, MarketStoreConflictError, MarketStoreError
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.podle import generate_podle
from jmcore.settings import JoinMarketSettings
from jmwallet.wallet.models import UTXOInfo
from loguru import logger

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


class PoDLESalesUnavailableError(Exception):
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


def _invoice_client(node: ChannelRingNodeConfig) -> LndInvoiceClient:
    from jmswap.lnd_invoices import LndInvoiceClient

    return LndInvoiceClient(
        node.lnd_grpc_url.removeprefix("https://"),
        Path(node.lnd_tls_cert_path).expanduser().read_bytes(),
        Path(node.lnd_macaroon_path).expanduser().read_bytes(),
    )


class PoDLESales:
    """Runs the wallet's PoDLE seller beside a maker and keeps it on the offer mixdepth.

    A missing precondition (no owned bond, no activated ledger, no node for the
    offer mixdepth) pauses sales with a warning and is retried; it never stops
    the maker.
    """

    def __init__(self, settings: JoinMarketSettings, bot: MakerBot) -> None:
        self._settings = settings
        self._bot = bot
        self._seller: WalletMarketSeller | None = None
        self._period: int | None = None
        # The only mixdepth whose coins are quoted.
        self._mixdepth: int | None = None
        self._stocked: set[tuple[str, int]] = set()
        self._task: asyncio.Task[None] | None = None
        self._last_warning: str | None = None

    @property
    def mixdepth(self) -> int | None:
        """The mixdepth currently sold from, if any."""
        return self._mixdepth

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="maker-podle-sales")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._stop_seller()

    async def run(self) -> None:
        """Sell while the maker runs: wait for its bond and offers, then refresh."""
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
            PoDLESalesUnavailableError | MarketError | MarketStoreError | OSError | ValueError,
        )
        reason = str(exc) if known else type(exc).__name__
        message = f"PoDLE sales paused: {reason}; retrying in {RETRY_SECONDS:.0f}s"
        if message != self._last_warning:
            logger.warning(message)
            self._last_warning = message
        else:
            logger.debug(message)

    async def _ensure_seller(self) -> None:
        """Start a seller, or replace one whose bond authorization period ended."""
        wallet = self._bot.wallet
        period = period_at_height(await wallet.backend.get_block_height())
        if self._seller is not None and self._seller.running and self._period == period:
            return
        await self._stop_seller()
        try:
            from taker.market_settlement import MultiNodeHoldInvoices, seller_invoice_client
            from taker.podle_purchase import market_fee_rate, seller_price_sats
            from taker.wallet_market import WalletMarketSeller, WalletMarketSellerOptions
        except ImportError as exc:
            raise PoDLESalesUnavailableError("selling needs the jm-taker package") from exc
        bond = self._bot.fidelity_bond
        if bond is None:
            raise PoDLESalesUnavailableError(
                "selling needs an owned fidelity bond as seller authority"
            )
        self._require_ledger(wallet)
        nodes = self._bot.config.channel_ring.nodes
        if not nodes:
            raise PoDLESalesUnavailableError("no Lightning node is configured")
        price = self._bot.config.podle_price_sats or seller_price_sats(
            await market_fee_rate(wallet.backend)
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
            WalletMarketSellerOptions.model_validate(
                {
                    "bond": {"txid": bond.txid, "vout": bond.vout},
                    "products": ["podle"],
                    "price_sats": price,
                }
            ),
            invoices=invoices,
            backing_sats=self._backing_sats,
            sale_check=self._sale_check,
        )
        self._seller = seller
        self._stocked.clear()
        await seller.start()
        self._period = period
        self._last_warning = None
        logger.info("PoDLE seller started at {} sats per commitment", price)

    def _require_ledger(self, wallet: WalletService) -> None:
        data_dir = wallet.data_dir
        if data_dir is None:
            raise PoDLESalesUnavailableError("selling needs a wallet data directory")
        diagnosis = MarketStore.diagnose_wallet(
            get_market_store_path(data_dir),
            wallet_id=wallet.market_wallet_id,
            commitments_path=get_used_commitments_path(data_dir, create=False),
        )
        if diagnosis["state"] != "ready":
            raise PoDLESalesUnavailableError(
                f"the wallet market ledger is {diagnosis['state']} ({diagnosis['reason']}); "
                "activate it once with jm-maker activate-market-ledger"
            )

    async def refresh(self) -> None:
        """Follow the offer mixdepth, reprice, and stock its new coins."""
        seller = self._seller
        if seller is None:
            return
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
        """
        seller = self._seller
        credential = None if seller is None else seller.sold_podle(payment_hash)
        if credential is None:
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
            raise PoDLESalesUnavailableError("the PoDLE seller stopped")
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
        if seller is not None:
            try:
                await seller.stop()
            except Exception as exc:
                logger.warning("The PoDLE seller did not stop cleanly: {}", type(exc).__name__)
