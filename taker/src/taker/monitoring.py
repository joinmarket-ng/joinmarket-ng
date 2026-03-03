"""
Taker monitoring mixin for background tasks.

Provides monitoring capabilities for pending transactions, periodic wallet
rescans, and directory connection status reporting. These are background
tasks that run concurrently with CoinJoin operations.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from jmwallet.history import (
    TransactionHistoryEntry,
    get_pending_transactions,
    update_transaction_confirmation,
)
from loguru import logger

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.service import WalletService

    from taker.config import TakerConfig


class TakerMonitoringMixin:
    """Mixin class providing background monitoring tasks for the Taker.

    Requires the following attributes on the host class:
    - self.running: bool
    - self.backend: BlockchainBackend
    - self.wallet: WalletService
    - self.config: TakerConfig
    - self.directory_client: MultiDirectoryClient
    """

    # Type hints for attributes provided by the host class
    running: bool
    backend: BlockchainBackend
    wallet: WalletService
    config: TakerConfig
    directory_client: Any  # MultiDirectoryClient

    async def _monitor_pending_transactions(self) -> None:
        """
        Background task to monitor pending transactions and update their status.

        Checks pending transactions every 60 seconds and updates their confirmation
        status in the history file. Transactions are marked as successful once they
        receive their first confirmation.

        Neutrino-specific behavior:
        - Neutrino cannot fetch arbitrary transactions by txid (get_transaction returns None)
        - Instead, we use verify_tx_output() with the destination address hint
        - This uses compact block filters to check if the output exists in confirmed blocks
        - For Neutrino, we must wait for confirmation before we can verify the transaction
        """
        logger.info("Starting pending transaction monitor...")
        check_interval = 60.0  # Check every 60 seconds
        has_mempool = self.backend.has_mempool_access()

        if not has_mempool:
            logger.info(
                "Backend has no mempool access (Neutrino). "
                "Pending transactions will be verified via block confirmation only."
            )

        while self.running:
            try:
                await asyncio.sleep(check_interval)

                if not self.running:
                    break

                pending = get_pending_transactions(data_dir=self.config.data_dir)
                if not pending:
                    continue

                logger.debug(f"Checking {len(pending)} pending transaction(s)...")

                for entry in pending:
                    if not entry.txid:
                        continue

                    try:
                        if has_mempool:
                            # Full node / Mempool API: can get transaction directly
                            await self._check_pending_with_mempool(entry)
                        else:
                            # Neutrino: must use address-based verification
                            await self._check_pending_without_mempool(entry)

                    except Exception as e:
                        logger.debug(f"Error checking transaction {entry.txid[:16]}...: {e}")

            except asyncio.CancelledError:
                logger.info("Pending transaction monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Error in pending transaction monitor: {e}")

        logger.info("Pending transaction monitor stopped")

    async def _check_pending_with_mempool(self, entry: TransactionHistoryEntry) -> None:
        """Check pending transaction status using get_transaction (requires mempool access)."""
        tx_info = await self.backend.get_transaction(entry.txid)

        if tx_info is None:
            # Transaction not found - might have been rejected/replaced
            from datetime import datetime

            timestamp = datetime.fromisoformat(entry.timestamp)
            age_hours = (datetime.now() - timestamp).total_seconds() / 3600

            if age_hours > 24:
                logger.warning(
                    f"Transaction {entry.txid[:16]}... not found after "
                    f"{age_hours:.1f} hours, may have been rejected"
                )
            return

        confirmations = tx_info.confirmations

        if confirmations > 0:
            # Update history with confirmation
            update_transaction_confirmation(
                txid=entry.txid,
                confirmations=confirmations,
                data_dir=self.config.data_dir,
            )

            logger.info(
                f"CoinJoin {entry.txid[:16]}... confirmed! "
                f"({confirmations} confirmation{'s' if confirmations != 1 else ''})"
            )

    async def _check_pending_without_mempool(self, entry: TransactionHistoryEntry) -> None:
        """Check pending transaction status without mempool access (Neutrino).

        Uses verify_tx_output() with the destination address to check if the
        CoinJoin output has been confirmed in a block. This works because
        Neutrino compact block filters can match on addresses.

        Note: This cannot detect unconfirmed transactions, so we must wait
        for block confirmation. The transaction may be in mempool but we
        won't know until it's mined.
        """
        from datetime import datetime

        # Need destination address for Neutrino verification
        if not entry.destination_address:
            logger.debug(
                f"Transaction {entry.txid[:16]}... has no destination_address, "
                "cannot verify with Neutrino"
            )
            return

        # Get current block height for efficient scanning
        try:
            current_height = await self.backend.get_block_height()
        except Exception:
            current_height = None

        # Try to verify the CJ output exists in a confirmed block
        # We use vout=0 as a guess for the CJ output position, but this may not be accurate
        # A more robust solution would store the vout in history
        # For now, we rely on the fact that if the address has a UTXO with this txid,
        # the transaction is confirmed
        verified = await self.backend.verify_tx_output(
            txid=entry.txid,
            vout=0,  # CJ outputs are typically first, but this is a guess
            address=entry.destination_address,
            start_height=current_height,
        )

        if verified:
            # Transaction output found in a confirmed block
            # We don't know exact confirmation count with Neutrino, assume 1
            update_transaction_confirmation(
                txid=entry.txid,
                confirmations=1,  # We know it's confirmed but not exact count
                data_dir=self.config.data_dir,
            )

            logger.info(
                f"CoinJoin {entry.txid[:16]}... confirmed! (verified via Neutrino block filters)"
            )
        else:
            # Not found yet - could be in mempool or not broadcast
            timestamp = datetime.fromisoformat(entry.timestamp)
            age_hours = (datetime.now() - timestamp).total_seconds() / 3600

            # For Neutrino, be more patient before warning since we can't see mempool
            # Only log at WARNING level if it's been a long time, otherwise DEBUG to reduce noise
            if age_hours > 10:  # 10 hour timeout for Neutrino
                logger.warning(
                    f"Transaction {entry.txid[:16]}... not confirmed after "
                    f"{age_hours:.1f} hours. May still be in mempool (not visible to Neutrino) "
                    "or may have been rejected/never broadcast."
                )
            elif age_hours > 1:  # Log at debug for txs older than 1 hour
                logger.debug(
                    f"Transaction {entry.txid[:16]}... not confirmed after "
                    f"{age_hours:.1f} hours (may be in mempool, waiting for confirmation)"
                )

    async def _update_pending_transaction_now(
        self, txid: str, destination_address: str | None = None
    ) -> None:
        """
        Immediately check and update a pending transaction's status.

        This is called right after recording a new transaction in history to check
        if it's already visible in mempool (for full nodes) or confirmed (for Neutrino).
        This is important for one-shot coinjoin CLI calls that exit immediately after
        broadcast without waiting for the background monitor.

        Args:
            txid: Transaction ID to check
            destination_address: Optional destination address (needed for Neutrino)
        """
        try:
            has_mempool = self.backend.has_mempool_access()

            if has_mempool:
                # Full node: can check mempool directly.
                # Wait a few seconds before the first check — the tx cannot be in the
                # mempool immediately after broadcast due to network propagation delay.
                await asyncio.sleep(5)
                tx_info = await self.backend.get_transaction(txid)
                if tx_info is not None:
                    confirmations = tx_info.confirmations
                    if confirmations >= 0:
                        # Transaction is in mempool (0 confs) or confirmed (>0 confs)
                        # Mark as success even with 0 confs (mempool visible)
                        update_transaction_confirmation(
                            txid=txid,
                            confirmations=max(confirmations, 1),
                            data_dir=self.config.data_dir,
                        )
                        if confirmations > 0:
                            logger.info(
                                f"CoinJoin {txid[:16]}... already confirmed "
                                f"({confirmations} confirmation{'s' if confirmations != 1 else ''})"
                            )
                        else:
                            logger.info(f"CoinJoin {txid[:16]}... visible in mempool")
            else:
                # Neutrino: can only check confirmed blocks, not mempool
                # For Neutrino, we need to wait for block confirmation
                # This will be handled by the background monitor on next startup
                if destination_address:
                    try:
                        current_height = await self.backend.get_block_height()
                    except Exception:
                        current_height = None

                    verified = await self.backend.verify_tx_output(
                        txid=txid,
                        vout=0,  # CJ outputs are typically first
                        address=destination_address,
                        start_height=current_height,
                    )

                    if verified:
                        update_transaction_confirmation(
                            txid=txid,
                            confirmations=1,
                            data_dir=self.config.data_dir,
                        )
                        logger.info(f"CoinJoin {txid[:16]}... confirmed via Neutrino block filters")
                    else:
                        logger.debug(
                            f"CoinJoin {txid[:16]}... not yet confirmed "
                            "(may be in mempool, Neutrino will verify on next block)"
                        )
        except Exception as e:
            logger.debug(f"Could not update transaction status immediately: {e}")

    async def _periodic_rescan(self) -> None:
        """Background task to periodically rescan wallet.

        This runs every `rescan_interval_sec` (default: 10 minutes) to:
        1. Detect confirmed transactions
        2. Update wallet balance after external transactions
        3. Update pending transaction status

        This is useful when running schedule/tumbler mode to ensure wallet
        state is fresh between CoinJoins.
        """
        logger.info(
            f"Starting periodic rescan task (interval: {self.config.rescan_interval_sec}s)..."
        )

        while self.running:
            try:
                await asyncio.sleep(self.config.rescan_interval_sec)

                if not self.running:
                    break

                logger.info("Periodic wallet rescan starting...")

                # Use fast descriptor wallet sync if available
                from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

                if isinstance(self.backend, DescriptorWalletBackend):
                    await self.wallet.sync_with_descriptor_wallet()
                else:
                    await self.wallet.sync_all()

                total_balance = await self.wallet.get_total_balance()
                logger.info(f"Wallet re-synced. Total balance: {total_balance:,} sats")

            except asyncio.CancelledError:
                logger.info("Periodic rescan task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in periodic rescan: {e}")

        logger.info("Periodic rescan task stopped")

    async def _periodic_directory_connection_status(self) -> None:
        """Background task to periodically log directory connection status.

        This runs every 10 minutes to provide visibility into orderbook
        connectivity. Shows:
        - Total directory servers configured
        - Currently connected servers
        - Disconnected servers (if any)
        """
        # First log after 5 minutes (give time for initial connection)
        await asyncio.sleep(300)

        while self.running:
            try:
                total_servers = len(self.directory_client.directory_servers)
                connected_servers = list(self.directory_client.clients.keys())
                connected_count = len(connected_servers)
                disconnected_servers = [
                    server
                    for server in self.directory_client.directory_servers
                    if server not in connected_servers
                ]

                if disconnected_servers:
                    disconnected_str = ", ".join(disconnected_servers[:5])
                    if len(disconnected_servers) > 5:
                        disconnected_str += f", ... and {len(disconnected_servers) - 5} more"
                    logger.warning(
                        f"Directory connection status: {connected_count}/{total_servers} "
                        f"connected. Disconnected: [{disconnected_str}]"
                    )
                else:
                    logger.info(
                        f"Directory connection status: {connected_count}/{total_servers} connected "
                        f"[{', '.join(connected_servers)}]"
                    )

                # Log again in 10 minutes
                await asyncio.sleep(600)

            except asyncio.CancelledError:
                logger.info("Directory connection status task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in directory connection status task: {e}")
                await asyncio.sleep(600)

        logger.info("Directory connection status task stopped")
