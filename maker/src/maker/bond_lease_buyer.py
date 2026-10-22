"""Fidelity bond leases for a maker without a usable bond (``[maker] lease_bond``).

While the maker runs without a bond (none owned, all leased out, or only an
expired certificate), a lease for the rest of the current retarget period is
bought over the credential market, paid from mixdepth 0's Lightning node, and
advertised. A lease ends at the period boundary, where its certificate
expires: the maker then advertises no bond until the next lease is bought.
Owned bonds are chosen only at startup, so a lease never replaces one. Price
caps and crash safety are the purchaser's (``taker.bond_purchase``).
"""

from __future__ import annotations

import asyncio
import stat
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.paths import get_bond_purchases_path
from jmcore.settings import JoinMarketSettings
from loguru import logger

from maker.fidelity import FIDELITY_BOND_MIXDEPTH, RETARGET_INTERVAL, FidelityBondInfo

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from taker.bond_purchase import LeasedBond

    from maker.bot import MakerBot

CHECK_SECONDS = 60.0
RETRY_SECONDS = 600.0


class BondLeaseUnavailableError(Exception):
    """A precondition for leasing a bond is missing."""


def needs_lease(bond: FidelityBondInfo | None, height: int) -> bool:
    """True without a bond, or when the advertised certificate expired by ``height``.

    A bond advertised with its own owner key never needs one.
    """
    if bond is None:
        return True
    return (
        bond.private_key is None
        and bond.cert_expiry is not None
        and height > bond.cert_expiry * RETARGET_INTERVAL
    )


async def leased_bond_info(
    leased: LeasedBond, backend: BlockchainBackend, height: int
) -> FidelityBondInfo:
    """The maker's view of a delivered lease, as the bond registry would give it."""
    bond = leased.credential.bond
    confirmation_time = await backend.get_block_time(height - leased.confirmations + 1)
    return FidelityBondInfo(
        txid=bond.outpoint.txid,
        vout=bond.outpoint.vout,
        value=leased.value,
        locktime=bond.locktime,
        confirmation_time=confirmation_time,
        bond_value=calculate_timelocked_fidelity_bond_value(
            utxo_value=leased.value, confirmation_time=confirmation_time, locktime=bond.locktime
        ),
        pubkey=bytes.fromhex(bond.pubkey),
        private_key=None,
        cert_pubkey=bytes.fromhex(leased.credential.cert_pubkey),
        cert_privkey=leased.certificate,
        cert_signature=bytes.fromhex(leased.credential.cert_signature),
        cert_expiry=leased.credential.cert_expiry,
    )


class BondLeaseBuyer:
    """Keeps a leased bond advertised while the maker has no usable bond of its own."""

    def __init__(
        self, settings: JoinMarketSettings, bot: MakerBot, purchaser: Any | None = None
    ) -> None:
        self._settings = settings
        self._bot = bot
        self._purchaser = purchaser
        self._task: asyncio.Task[None] | None = None
        self._last_warning: str | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def run(self) -> None:
        # Leases start once the maker chose (or failed to find) its own bond.
        while not self._bot.running:
            await asyncio.sleep(1.0)
        while True:
            try:
                delay = CHECK_SECONDS if await self.check() else RETRY_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                known = isinstance(exc, BondLeaseUnavailableError | OSError | ValueError)
                message = f"Fidelity bond leasing paused: {exc if known else type(exc).__name__}"
                if message != self._last_warning:
                    logger.warning(message)
                    self._last_warning = message
                delay = RETRY_SECONDS
            await asyncio.sleep(delay)

    async def check(self) -> bool:
        """Lease a bond if the maker needs one; False when one was needed but not leased."""
        bot = self._bot
        height = await bot.backend.get_block_height()
        has_history = self._has_purchase_history()
        if self._purchaser is not None or has_history:
            # A usable owned or imported bond closes purchase admission, not
            # the original payment monitor. Reconcile without opening a market
            # transport, including after restart with a valid imported lease.
            await self._get_purchaser().reconcile_payments(height)
        if not needs_lease(bot.fidelity_bond, height):
            return True
        if bot.fidelity_bond is not None:
            bot.release_expired_bond()
        leased = await self._get_purchaser().lease(
            bot.backend,
            cap_sats=bot.config.lease_bond_max_sats,
            max_rate=bot.config.lease_bond_max_rate,
        )
        if leased is None:
            return False
        height = await bot.backend.get_block_height()
        await bot.adopt_leased_bond(await leased_bond_info(leased, bot.backend, height), height)
        self._last_warning = None
        return True

    def _has_purchase_history(self) -> bool:
        """Probe affirmative record files without importing the optional taker."""
        directory = get_bond_purchases_path(self._settings.data_dir)
        try:
            mode = directory.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(mode):
            raise BondLeaseUnavailableError("bond purchase history needs directory review")
        # Unlike glob(), iterdir() reports scan errors rather than silently
        # turning inaccessible history into apparent absence.
        return any(path.name.endswith(".json") for path in directory.iterdir())

    def _get_purchaser(self) -> Any:
        if self._purchaser is not None:
            return self._purchaser
        try:
            from taker.bond_purchase import BondPurchaser
            from taker.podle_purchase import NodePayer
        except ImportError as exc:
            raise BondLeaseUnavailableError("leasing needs the jm-taker package") from exc
        if self._settings.lightning.mode == "managed":
            from jmswap.managed_payments import ManagedPaymentSource

            wallet = self._bot.wallet
            if wallet.data_dir is None:
                raise BondLeaseUnavailableError("the wallet has no managed node data directory")
            source = ManagedPaymentSource.for_wallet(
                wallet.data_dir,
                wallet.network,
                wallet.master_key.get_public_key_bytes(),
                FIDELITY_BOND_MIXDEPTH,
            )
            # History remains queryable without an ACTIVE replacement. Only
            # fresh payment admission depends on the current active node.
            payer = NodePayer(managed=source, purpose="a fidelity bond lease")
        else:
            ring = self._bot.config.channel_ring
            name = ring.mixdepth_nodes.get(FIDELITY_BOND_MIXDEPTH)
            node = None if name is None else ring.nodes.get(name)
            if node is None:
                raise BondLeaseUnavailableError("no Lightning node for mixdepth 0 to pay leases")
            payer = NodePayer(node, purpose="a fidelity bond lease")
        fingerprint = self._bot.wallet.wallet_fingerprint
        if fingerprint is None:
            raise BondLeaseUnavailableError("the wallet has no fingerprint for its bond registry")
        self._purchaser = BondPurchaser(
            self._settings,
            payer,
            wallet_fingerprint=fingerprint,
        )
        return self._purchaser
