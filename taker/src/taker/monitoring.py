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
    expire_pending_transaction_monitoring,
    get_pending_transactions,
    update_transaction_confirmation,
    verify_history_destination_output,
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

    async def _verify_destination_output(
        self,
        txid: str,
        destination_address: str,
        destination_vout: int,
        peer_count: int | None,
        start_height: int | None,
    ) -> bool:
        """Verify an exact destination output, or a bounded legacy candidate set."""
        return await verify_history_destination_output(
            self.backend,
            txid=txid,
            destination_address=destination_address,
            destination_vout=destination_vout,
            peer_count=peer_count,
            start_height=start_height,
        )

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
        logger.debug("Starting pending transaction monitor...")
        check_interval = 60.0  # Check every 60 seconds
        # Confirmation tracking needs a backend that reports confirmation depth
        # by txid. Neutrino cannot (get_transaction is mempool-only, even with
        # the watched mempool tracker), so it is verified via block-level
        # address matching instead. has_mempool_access() is not the right
        # signal here -- neutrino+tracker has mempool access yet still cannot
        # confirm by txid.
        can_confirm_by_txid = self.backend.can_get_confirmations_by_txid()

        if not can_confirm_by_txid:
            logger.debug(
                "Backend cannot confirm transactions by txid (Neutrino). "
                "Pending transactions will be verified via block confirmation only."
            )

        while self.running:
            try:
                await asyncio.sleep(check_interval)

                if not self.running:
                    break

                pending = get_pending_transactions(
                    data_dir=self.config.data_dir,
                    wallet_fingerprint=self.wallet.wallet_fingerprint,
                )
                if not pending:
                    continue

                logger.debug(f"Checking {len(pending)} pending transaction(s)...")

                for entry in pending:
                    if not entry.txid:
                        continue

                    try:
                        if can_confirm_by_txid:
                            # Full node / Mempool API: can get transaction directly
                            await self._check_pending_with_mempool(entry)
                        else:
                            # Neutrino: must use address-based verification
                            await self._check_pending_without_mempool(entry)

                    except Exception as e:
                        logger.debug("Error checking pending transaction")
                        logger.bind(sensitive=True).debug(
                            "Pending transaction {} check detail: {}", entry.txid, e
                        )

            except asyncio.CancelledError:
                logger.debug("Pending transaction monitor cancelled")
                break
            except Exception as e:
                logger.error("Error in pending transaction monitor")
                logger.bind(sensitive=True).error("Pending transaction monitor detail: {}", e)

        logger.debug("Pending transaction monitor stopped")

    async def _check_pending_with_mempool(self, entry: TransactionHistoryEntry) -> None:
        """Check pending transaction status using get_transaction (requires mempool access)."""
        if expire_pending_transaction_monitoring(
            entry,
            max_age_minutes=self.config.pending_tx_abandon_hours * 60,
            data_dir=self.config.data_dir,
            wallet_fingerprint=self.wallet.wallet_fingerprint,
        ):
            return

        tx_info = await self.backend.get_transaction(entry.txid)

        if tx_info is None:
            from datetime import datetime

            timestamp = datetime.fromisoformat(entry.timestamp)
            age_hours = (datetime.now() - timestamp).total_seconds() / 3600

            if age_hours > 24:
                logger.warning("Pending transaction remains unobserved")
                logger.bind(sensitive=True).warning(
                    "Transaction {} remains unobserved after {:.1f} hours", entry.txid, age_hours
                )
            return

        confirmations = tx_info.confirmations

        if confirmations > 0:
            # Update history with confirmation
            update_transaction_confirmation(
                txid=entry.txid,
                confirmations=confirmations,
                data_dir=self.config.data_dir,
                wallet_fingerprint=self.wallet.wallet_fingerprint,
            )

            logger.info("CoinJoin confirmed")
            logger.bind(sensitive=True).info(
                "CoinJoin {} confirmed ({} confirmation{})",
                entry.txid,
                confirmations,
                "s" if confirmations != 1 else "",
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
        if expire_pending_transaction_monitoring(
            entry,
            max_age_minutes=self.config.pending_tx_abandon_hours * 60,
            data_dir=self.config.data_dir,
            wallet_fingerprint=self.wallet.wallet_fingerprint,
        ):
            return

        from datetime import datetime

        # Need destination address for Neutrino verification
        if not entry.destination_address:
            logger.debug("Pending transaction cannot be verified with Neutrino")
            logger.bind(sensitive=True).debug(
                "Transaction {} has no destination address", entry.txid
            )
            return

        # Get current block height for efficient scanning
        try:
            current_height = await self.backend.get_block_height()
        except Exception:
            current_height = None

        # Newly written entries retain the shuffled output index. Legacy rows
        # fall back to a bounded, deterministic scan of plausible outputs.
        verified = await self._verify_destination_output(
            txid=entry.txid,
            destination_address=entry.destination_address,
            destination_vout=entry.destination_vout,
            peer_count=entry.peer_count,
            start_height=current_height,
        )

        if verified:
            # Transaction output found in a confirmed block
            # We don't know exact confirmation count with Neutrino, assume 1
            update_transaction_confirmation(
                txid=entry.txid,
                confirmations=1,  # We know it's confirmed but not exact count
                data_dir=self.config.data_dir,
                wallet_fingerprint=self.wallet.wallet_fingerprint,
            )

            logger.info("CoinJoin confirmed via Neutrino block filters")
            logger.bind(sensitive=True).info(
                "CoinJoin {} confirmed via Neutrino block filters", entry.txid
            )
        else:
            # Not found yet - could be in mempool or not broadcast
            timestamp = datetime.fromisoformat(entry.timestamp)
            age_hours = (datetime.now() - timestamp).total_seconds() / 3600

            # For Neutrino, be more patient before warning since we can't see mempool
            # Only log at WARNING level if it's been a long time, otherwise DEBUG to reduce noise
            if age_hours > 2:  # 2 hour threshold for Neutrino
                logger.warning("Pending transaction remains unconfirmed")
                logger.bind(sensitive=True).warning(
                    "Transaction {} remains unconfirmed after {:.1f} hours", entry.txid, age_hours
                )
            elif age_hours > 0.5:  # Log at debug for txs older than 30 min
                logger.debug("Pending transaction is awaiting confirmation")
                logger.bind(sensitive=True).debug(
                    "Transaction {} not confirmed after {:.1f} hours", entry.txid, age_hours
                )

    async def _update_pending_transaction_now(
        self,
        txid: str,
        destination_address: str | None = None,
        destination_vout: int = -1,
        peer_count: int | None = None,
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
            destination_vout: Destination output index, or -1 for legacy fallback
            peer_count: CoinJoin maker count used to bound the legacy fallback
        """
        try:
            can_confirm_by_txid = self.backend.can_get_confirmations_by_txid()

            if can_confirm_by_txid:
                # Full node / mempool API: check confirmation depth directly.
                # Wait a few seconds before the first check — the tx cannot be in the
                # mempool immediately after broadcast due to network propagation delay.
                await asyncio.sleep(5)
                tx_info = await self.backend.get_transaction(txid)
                if tx_info is not None and tx_info.confirmations > 0:
                    confirmations = tx_info.confirmations
                    update_transaction_confirmation(
                        txid=txid,
                        confirmations=confirmations,
                        data_dir=self.config.data_dir,
                        wallet_fingerprint=self.wallet.wallet_fingerprint,
                    )
                    logger.info("CoinJoin already confirmed")
                    logger.bind(sensitive=True).info(
                        "CoinJoin {} already confirmed ({} confirmation{})",
                        txid,
                        confirmations,
                        "s" if confirmations != 1 else "",
                    )
                elif tx_info is not None:
                    logger.info("CoinJoin visible in mempool, awaiting confirmation")
                    logger.bind(sensitive=True).info("CoinJoin {} visible in mempool", txid)
            else:
                # Neutrino cannot report confirmation depth by txid. Its UTXO
                # endpoint can explicitly exclude the mempool overlay.
                if destination_address:
                    try:
                        current_height = await self.backend.get_block_height()
                    except Exception:
                        current_height = None

                    verified = await self._verify_destination_output(
                        txid=txid,
                        destination_address=destination_address,
                        destination_vout=destination_vout,
                        peer_count=peer_count,
                        start_height=current_height,
                    )

                    if verified:
                        update_transaction_confirmation(
                            txid=txid,
                            confirmations=1,
                            data_dir=self.config.data_dir,
                            wallet_fingerprint=self.wallet.wallet_fingerprint,
                        )
                        logger.info("CoinJoin confirmed via Neutrino block filters")
                        logger.bind(sensitive=True).info(
                            "CoinJoin {} confirmed via Neutrino block filters", txid
                        )
                    else:
                        logger.debug("CoinJoin not yet confirmed")
                        logger.bind(sensitive=True).debug("CoinJoin {} is not yet confirmed", txid)
        except Exception as e:
            logger.debug("Could not update transaction status immediately")
            logger.bind(sensitive=True).debug("Transaction status update detail: {}", e)

    async def _periodic_rescan(self) -> None:
        """Background task to periodically rescan wallet.

        This runs every `rescan_interval_sec` (default: 10 minutes) to:
        1. Detect confirmed transactions
        2. Update wallet balance after external transactions
        3. Update pending transaction status

        This is useful when running schedule/tumbler mode to ensure wallet
        state is fresh between CoinJoins.
        """
        logger.debug(
            f"Starting periodic rescan task (interval: {self.config.rescan_interval_sec}s)..."
        )

        while self.running:
            try:
                await asyncio.sleep(self.config.rescan_interval_sec)

                if not self.running:
                    break

                logger.debug("Periodic wallet rescan starting...")

                # Use fast descriptor wallet sync if available
                from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

                if isinstance(self.backend, DescriptorWalletBackend):
                    await self.wallet.sync_with_descriptor_wallet()
                else:
                    await self.wallet.sync_all()
                await self.wallet.reconstruct_imported_state_safe()

                total_balance = await self.wallet.get_total_balance()
                logger.bind(sensitive=True).debug(
                    "Wallet re-synced. Total balance: {:,} sats", total_balance
                )

            except asyncio.CancelledError:
                logger.debug("Periodic rescan task cancelled")
                break
            except Exception as e:
                logger.error("Error in periodic rescan")
                logger.bind(sensitive=True).error("Periodic rescan detail: {}", e)

        logger.debug("Periodic rescan task stopped")

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
                    logger.debug(
                        f"Directory connection status: {connected_count}/{total_servers} connected "
                        f"[{', '.join(connected_servers)}]"
                    )

                # Log again in 10 minutes
                await asyncio.sleep(600)

            except asyncio.CancelledError:
                logger.debug("Directory connection status task cancelled")
                break
            except Exception as e:
                logger.error("Error in directory connection status task")
                logger.bind(sensitive=True).error("Directory connection status detail: {}", e)
                await asyncio.sleep(600)

        logger.debug("Directory connection status task stopped")
