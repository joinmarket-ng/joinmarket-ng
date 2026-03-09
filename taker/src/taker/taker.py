"""
Main Taker class for CoinJoin execution.

Orchestrates the complete CoinJoin protocol:
1. Fetch orderbook from directory nodes
2. Select makers and generate PoDLE commitment
3. Send !fill requests and receive !pubkey responses
4. Send !auth with PoDLE proof and receive !ioauth (maker UTXOs)
5. Build unsigned transaction and send !tx
6. Collect !sig responses and broadcast

Reference: Original joinmarket-clientserver/src/jmclient/taker.py
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from jmcore.bitcoin import (
    address_to_scriptpubkey,
    calculate_tx_vsize,
    decode_varint,
    get_txid,
    is_p2tr_address,
    parse_transaction,
)
from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.btc_script import derive_bond_address
from jmcore.commitment_blacklist import set_blacklist_path
from jmcore.crypto import NickIdentity
from jmcore.encryption import CryptoSession
from jmcore.notifications import get_notifier
from jmcore.paths import read_nick_state
from jmcore.protocol import JM_VERSION, parse_utxo_list
from jmwallet.backends.base import BlockchainBackend, BondVerificationRequest
from jmwallet.history import (
    append_history_entry,
    create_taker_history_entry,
    update_taker_awaiting_transaction_broadcast,
)
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.signing import (
    TransactionSigningError,
    create_p2wpkh_script_code,
    create_witness_stack,
    deserialize_transaction,
    sign_p2tr_input,
    sign_p2wpkh_input,
    verify_p2tr_signature,
    verify_p2wpkh_signature,
)
from loguru import logger

from taker.config import BroadcastPolicy, Schedule, TakerConfig
from taker.models import MakerSession, PhaseResult, TakerState
from taker.monitoring import TakerMonitoringMixin
from taker.multi_directory import MultiDirectoryClient
from taker.orderbook import OrderbookManager, calculate_cj_fee
from taker.podle import ExtendedPoDLECommitment
from taker.podle_manager import PoDLEManager
from taker.tx_builder import CoinJoinTxBuilder, build_coinjoin_tx

if TYPE_CHECKING:
    from jmwallet.wallet.service import WalletService

# Backward-compatible re-exports: many tests and modules import these from taker.taker
__all__ = [
    "MultiDirectoryClient",
    "TakerState",
    "MakerSession",
    "PhaseResult",
    "Taker",
]


class Taker(TakerMonitoringMixin):
    """
    Main Taker class for executing CoinJoin transactions.
    """

    def __init__(
        self,
        wallet: WalletService,
        backend: BlockchainBackend,
        config: TakerConfig,
        confirmation_callback: Any | None = None,
    ):
        """
        Initialize the Taker.

        Args:
            wallet: Wallet service for UTXO management and signing
            backend: Blockchain backend for broadcasting
            config: Taker configuration
            confirmation_callback: Optional callback for user confirmation before proceeding
        """
        self.wallet = wallet
        self.backend = backend
        self.config = config
        self.confirmation_callback = confirmation_callback

        self.nick_identity = NickIdentity(JM_VERSION)
        self.nick = self.nick_identity.nick
        self.state = TakerState.IDLE

        # Advertise neutrino_compat if our backend can provide extended UTXO metadata.
        # This tells other peers that we can provide scriptpubkey and blockheight.
        # Full nodes (Bitcoin Core) can provide this; light clients (Neutrino) cannot.
        neutrino_compat = backend.can_provide_neutrino_metadata()

        # Directory client
        self.directory_client = MultiDirectoryClient(
            directory_servers=config.directory_servers,
            network=config.network.value,
            nick_identity=self.nick_identity,
            socks_host=config.socks_host,
            socks_port=config.socks_port,
            connection_timeout=config.connection_timeout,
            neutrino_compat=neutrino_compat,
            stream_isolation=config.stream_isolation,
        )

        # Orderbook manager
        # Read maker nick from state file to exclude from peer selection (self-CoinJoin protection)
        own_wallet_nicks: set[str] = set()
        maker_nick = read_nick_state(config.data_dir, "maker")
        if maker_nick:
            own_wallet_nicks.add(maker_nick)
            logger.info(f"Self-CoinJoin protection: excluding maker nick {maker_nick}")

        self.orderbook_manager = OrderbookManager(
            config.max_cj_fee,
            bondless_makers_allowance=config.bondless_makers_allowance,
            bondless_require_zero_fee=config.bondless_makers_allowance_require_zero_fee,
            data_dir=config.data_dir,
            own_wallet_nicks=own_wallet_nicks,
        )

        # PoDLE manager for commitment tracking
        self.podle_manager = PoDLEManager(config.data_dir)

        # Current CoinJoin session data
        self.cj_amount = 0
        self.is_sweep = False  # True when amount=0 (sweep mode, no change output)
        self.maker_sessions: dict[str, MakerSession] = {}
        self.podle_commitment: ExtendedPoDLECommitment | None = None
        self.unsigned_tx: bytes = b""
        self.tx_metadata: dict[str, Any] = {}
        self.final_tx: bytes = b""
        self.txid: str = ""
        self.preselected_utxos: list[UTXOInfo] = []  # UTXOs pre-selected for CoinJoin
        self.selected_utxos: list[UTXOInfo] = []  # Taker's final selected UTXOs for signing
        self.cj_destination: str = ""  # Taker's CJ destination address for broadcast verification
        self.taker_change_address: str = ""  # Taker's change address for broadcast verification
        # For sweeps: store the tx_fee budget calculated at order selection time
        # This is the amount reserved for tx fees when calculating cj_amount.
        # At build time, we use this budget (not a new estimate) to ensure the
        # actual tx fee matches what was budgeted, preventing residual fee issues.
        self._sweep_tx_fee_budget: int = 0

        # E2E encryption session for communication with makers
        self.crypto_session: CryptoSession | None = None

        # Schedule for tumbler-style operations
        self.schedule: Schedule | None = None

        # Cached fee rate for the current CoinJoin (set in _resolve_fee_rate)
        # This is the base rate from backend estimation or manual config
        self._fee_rate: float | None = None
        # Randomized fee rate for this CoinJoin session (set once in _resolve_fee_rate)
        # This applies tx_fee_factor randomization and is used for all fee calculations
        self._randomized_fee_rate: float | None = None

        # Background task tracking
        self.running = False
        self._background_tasks: list[asyncio.Task[None]] = []

    async def sync_wallet(self) -> int:
        """
        Sync the wallet and return total balance.

        This method is separated from start() to allow callers to check
        funds before connecting to directory servers (avoiding unnecessary
        network connections when funds are insufficient).

        Returns:
            Total wallet balance in satoshis.
        """
        logger.info(f"Starting taker (nick: {self.nick})")

        # Log wallet name if using descriptor wallet backend
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        if isinstance(self.backend, DescriptorWalletBackend):
            logger.info(f"Using wallet: {self.backend.wallet_name}")

        # Initialize commitment blacklist with configured data directory
        set_blacklist_path(data_dir=self.config.data_dir)

        # Sync wallet
        logger.info("Syncing wallet...")

        # Setup descriptor wallet if needed (one-time operation)
        if isinstance(self.backend, DescriptorWalletBackend):
            if not await self.wallet.is_descriptor_wallet_ready():
                logger.info("Descriptor wallet not set up. Importing descriptors...")
                await self.wallet.setup_descriptor_wallet(rescan=True)
                logger.info("Descriptor wallet setup complete")

            # Use fast descriptor wallet sync
            await self.wallet.sync_with_descriptor_wallet()
        else:
            # Use standard sync (scantxoutset for scantxoutset, BIP157/158 for neutrino)
            await self.wallet.sync_all()

        total_balance = await self.wallet.get_total_balance()
        logger.info(f"Wallet synced. Total balance: {total_balance:,} sats")

        return total_balance

    async def connect(self) -> None:
        """
        Connect to directory servers and start background tasks.

        This should be called after sync_wallet() and any fund validation.
        """
        # Connect to directory servers
        logger.info("Connecting to directory servers...")
        connected = await self.directory_client.connect_all()

        if connected == 0:
            raise RuntimeError("Failed to connect to any directory server")

        logger.info(f"Connected to {connected} directory servers")

        # Mark as running and start background tasks
        self.running = True

        # Start pending transaction monitor
        monitor_task = asyncio.create_task(self._monitor_pending_transactions())
        self._background_tasks.append(monitor_task)

        # Start periodic rescan task (useful for schedule mode)
        rescan_task = asyncio.create_task(self._periodic_rescan())
        self._background_tasks.append(rescan_task)

        # Start periodic directory connection status logging task
        conn_status_task = asyncio.create_task(self._periodic_directory_connection_status())
        self._background_tasks.append(conn_status_task)

    async def start(self) -> None:
        """
        Start the taker: sync wallet and connect to directory servers.

        This is a convenience method that calls sync_wallet() followed by connect().
        For early fund validation, call sync_wallet() first, validate, then call connect().
        """
        await self.sync_wallet()
        await self.connect()

    async def stop(self) -> None:
        """Stop the taker and close connections."""
        logger.info("Stopping taker...")
        self.running = False

        # Cancel all background tasks
        for task in self._background_tasks:
            task.cancel()

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        await self.directory_client.close_all()
        await self.wallet.close()
        logger.info("Taker stopped")

    async def _update_offers_with_bond_values(self, offers: list) -> None:
        """
        Verify fidelity bonds and calculate their values.

        Uses the backend's ``verify_bonds()`` method for efficient bulk verification
        that works correctly on all backends (Bitcoin Core, neutrino, mempool).

        For each offer with a fidelity bond proof, derives the P2WSH bond address
        from the UTXO public key and locktime, then delegates verification to the
        backend which can batch the lookups optimally.
        """
        # Collect offers that need bond verification, deduplicating by (txid, vout)
        bond_key_to_request: dict[tuple[str, int], BondVerificationRequest] = {}
        bond_key_to_locktime: dict[tuple[str, int], int] = {}

        for offer in offers:
            if offer.fidelity_bond_data and offer.fidelity_bond_value == 0:
                txid = offer.fidelity_bond_data["utxo_txid"]
                vout = offer.fidelity_bond_data["utxo_vout"]
                key = (txid, vout)

                if key in bond_key_to_request:
                    continue

                locktime = offer.fidelity_bond_data["locktime"]
                utxo_pub = offer.fidelity_bond_data.get("utxo_pub")

                if not utxo_pub:
                    logger.debug(f"Bond {txid}:{vout} missing utxo_pub, skipping")
                    continue

                # Ensure utxo_pub is bytes
                if isinstance(utxo_pub, str):
                    utxo_pub_bytes = bytes.fromhex(utxo_pub)
                else:
                    utxo_pub_bytes = utxo_pub

                try:
                    bond_addr = derive_bond_address(utxo_pub_bytes, locktime, self.config.network)
                except Exception as e:
                    logger.debug(f"Failed to derive bond address for {txid}:{vout}: {e}")
                    continue

                bond_key_to_request[key] = BondVerificationRequest(
                    txid=txid,
                    vout=vout,
                    utxo_pub=utxo_pub_bytes,
                    locktime=locktime,
                    address=bond_addr.address,
                    scriptpubkey=bond_addr.scriptpubkey.hex(),
                )
                bond_key_to_locktime[key] = locktime

        if not bond_key_to_request:
            return

        logger.info(f"Verifying {len(bond_key_to_request)} fidelity bonds...")

        # Bulk verify via the backend (batched for efficiency)
        try:
            requests = list(bond_key_to_request.values())
            results = await self.backend.verify_bonds(requests)
        except Exception as e:
            logger.warning(f"Bond verification failed: {e}")
            return

        # Build lookup map from results
        current_time = int(time.time())
        bond_values: dict[tuple[str, int], int] = {}

        for result in results:
            if not result.valid:
                logger.debug(f"Bond {result.txid}:{result.vout} invalid: {result.error}")
                continue

            key = (result.txid, result.vout)
            locktime = bond_key_to_locktime[key]

            bond_value = calculate_timelocked_fidelity_bond_value(
                utxo_value=result.value,
                confirmation_time=result.block_time,
                locktime=locktime,
                current_time=current_time,
            )

            if bond_value > 0:
                bond_values[key] = bond_value

        # Update offers with calculated bond values
        updated_count = 0
        for offer in offers:
            if offer.fidelity_bond_data and offer.fidelity_bond_value == 0:
                txid = offer.fidelity_bond_data["utxo_txid"]
                vout = offer.fidelity_bond_data["utxo_vout"]
                key = (txid, vout)

                if key in bond_values:
                    offer.fidelity_bond_value = bond_values[key]
                    updated_count += 1

        logger.info(f"Updated {updated_count} offers with verified fidelity bond values")

    async def do_coinjoin(
        self,
        amount: int,
        destination: str,
        mixdepth: int = 0,
        counterparty_count: int | None = None,
    ) -> str | None:
        """
        Execute a single CoinJoin transaction.

        Args:
            amount: Amount in satoshis (0 for sweep)
            destination: Destination address ("INTERNAL" for next mixdepth)
            mixdepth: Source mixdepth
            counterparty_count: Number of makers (default from config)

        Returns:
            Transaction ID if successful, None otherwise
        """
        try:
            n_makers = counterparty_count or self.config.counterparty_count

            # Determine destination address
            if destination == "INTERNAL":
                dest_mixdepth = (mixdepth + 1) % self.wallet.mixdepth_count
                # Use internal chain (/1) for CoinJoin outputs, not external (/0)
                # This matches the reference implementation behavior where all JM-generated
                # addresses (CJ outputs and change) use the internal branch
                dest_index = self.wallet.get_next_address_index(dest_mixdepth, 1)
                destination = self.wallet.get_change_address(dest_mixdepth, dest_index)
                logger.info(f"Using internal address: {destination}")

            # Resolve fee rate early (before any fee estimation calls)
            try:
                await self._resolve_fee_rate()
            except ValueError as e:
                logger.error(str(e))
                self.state = TakerState.FAILED
                return None

            # Track if this is a sweep (no change) transaction
            self.is_sweep = amount == 0

            # Select UTXOs from wallet BEFORE fetching orderbook to avoid wasting user's time
            logger.info(f"Selecting UTXOs from mixdepth {mixdepth}...")

            # Interactive UTXO selection if requested
            manually_selected_utxos: list[UTXOInfo] | None = None
            if self.config.select_utxos:
                from jmwallet.history import get_utxo_label
                from jmwallet.utxo_selector import select_utxos_interactive

                try:
                    # Get ALL UTXOs including frozen ones for display in the
                    # interactive selector.  Frozen/locked UTXOs are shown but
                    # rendered as unselectable ([-]) so the user sees the full
                    # picture of their wallet.
                    available_utxos = await self.wallet.get_utxos(mixdepth)
                    # Also filter by minimum age (confirmations) -- but keep
                    # frozen ones regardless so they're visible in the TUI.
                    min_age = self.config.taker_utxo_age
                    available_utxos = [
                        u for u in available_utxos if u.confirmations >= min_age or u.frozen
                    ]
                    if not available_utxos:
                        logger.error(f"No UTXOs in mixdepth {mixdepth}")
                        self.state = TakerState.FAILED
                        return None

                    # Check that at least some UTXOs are selectable (not frozen/locked)
                    selectable = [
                        u
                        for u in available_utxos
                        if not u.frozen and not (u.is_fidelity_bond and u.is_locked)
                    ]
                    if not selectable:
                        logger.error(
                            f"No eligible UTXOs in mixdepth {mixdepth} "
                            f"(all {len(available_utxos)} UTXOs are frozen or locked)"
                        )
                        self.state = TakerState.FAILED
                        return None

                    # Populate labels for each UTXO based on history
                    for utxo in available_utxos:
                        utxo.label = get_utxo_label(utxo.address, self.config.data_dir)

                    logger.info(
                        f"Launching interactive UTXO selector ({len(available_utxos)} available, "
                        f"target amount: {amount} sats, sweep: {amount == 0})..."
                    )
                    manually_selected_utxos = select_utxos_interactive(available_utxos, amount)

                    if not manually_selected_utxos:
                        logger.info("UTXO selection cancelled by user")
                        self.state = TakerState.CANCELLED
                        return None

                    total_selected = sum(u.value for u in manually_selected_utxos)
                    logger.info(
                        f"Manually selected {len(manually_selected_utxos)} UTXOs "
                        f"(total: {total_selected:,} sats)"
                    )

                    # Validate selected UTXOs have sufficient funds (for non-sweep)
                    if amount > 0 and total_selected < amount:
                        logger.error(
                            f"Insufficient funds in selected UTXOs: "
                            f"have {total_selected:,} sats, need at least {amount:,} sats"
                        )
                        self.state = TakerState.FAILED
                        return None
                except RuntimeError as e:
                    logger.error(f"Interactive UTXO selection failed: {e}")
                    self.state = TakerState.FAILED
                    return None
            else:
                logger.debug("Interactive UTXO selection not requested (--select-utxos not set)")

            # Now fetch orderbook after UTXO selection is done
            self.state = TakerState.FETCHING_ORDERBOOK
            logger.info("Fetching orderbook...")
            offers = await self.directory_client.fetch_orderbook(
                max_wait=self.config.order_wait_time,
                min_wait=self.config.orderbook_min_wait,
                quiet_period=self.config.orderbook_quiet_period,
            )

            # Verify and calculate fidelity bond values
            await self._update_offers_with_bond_values(offers)

            self.orderbook_manager.update_offers(offers)

            if len(offers) < n_makers:
                logger.error(f"Not enough offers: need {n_makers}, found {len(offers)}")
                self.state = TakerState.FAILED
                return None

            # NOTE: Neutrino takers require makers that support extended UTXO metadata
            # (scriptPubKey + blockheight). This is negotiated during the CoinJoin handshake
            # via the neutrino_compat feature in the !pubkey response. All peers use v5
            # protocol; feature support is advertised separately for smooth rollout.
            # Incompatible makers (no neutrino_compat) are filtered during _phase_auth().
            if self.backend.requires_neutrino_metadata():
                logger.info("Neutrino backend: will negotiate neutrino_compat during handshake")

            self.state = TakerState.SELECTING_MAKERS

            if self.is_sweep:
                # SWEEP MODE: Select ALL UTXOs and calculate exact cj_amount for zero change
                logger.info("Sweep mode: selecting UTXOs from mixdepth")

                # Use manually selected UTXOs if available, otherwise get all UTXOs
                if manually_selected_utxos:
                    self.preselected_utxos = manually_selected_utxos
                    logger.info(
                        f"Sweep using {len(manually_selected_utxos)} manually selected UTXOs "
                        f"(--select-utxos was used)"
                    )
                else:
                    # Get ALL UTXOs from the mixdepth (default sweep behavior)
                    self.preselected_utxos = self.wallet.get_all_utxos(
                        mixdepth, self.config.taker_utxo_age
                    )
                    logger.info(
                        f"Sweep using all {len(self.preselected_utxos)} UTXOs from mixdepth "
                        f"(no --select-utxos)"
                    )

                if not self.preselected_utxos:
                    logger.error(f"No eligible UTXOs in mixdepth {mixdepth}")
                    self.state = TakerState.FAILED
                    return None

                total_input_value = sum(u.value for u in self.preselected_utxos)
                logger.info(
                    f"Sweep: {len(self.preselected_utxos)} UTXOs, "
                    f"total value: {total_input_value:,} sats"
                )

                # Estimate tx fee for sweep order calculation
                # Conservative estimate: 2 inputs per maker + buffer for edge cases
                # Most makers have 1-2 inputs, but occasionally one might have 6+.
                # The buffer (5 inputs) covers the edge case without being excessive.
                # If actual < estimated: extra goes to miner (acceptable)
                # If actual > estimated: CoinJoin fails with negative residual error
                maker_inputs_per_maker = 2
                maker_inputs_buffer = 5  # Extra inputs to handle edge cases
                estimated_inputs = (
                    len(self.preselected_utxos)
                    + n_makers * maker_inputs_per_maker
                    + maker_inputs_buffer
                )
                # CJ outputs + maker changes (no taker change in sweep!)
                estimated_outputs = 1 + n_makers + n_makers
                # For sweeps, use base rate for deterministic budget calculation.
                # The cj_amount is calculated based on this budget, so it must match
                # exactly at build time. Using randomized rate would cause residual fees.
                estimated_tx_fee = self._estimate_tx_fee(
                    estimated_inputs, estimated_outputs, use_base_rate=True
                )

                # Store the tx fee budget for use at build time.
                # This is critical: the cj_amount is calculated based on this budget,
                # so we MUST use this same value at build time to avoid residual fees.
                self._sweep_tx_fee_budget = estimated_tx_fee

                # Use sweep order selection - this calculates exact cj_amount for zero change
                selected_offers, self.cj_amount, total_fee = (
                    self.orderbook_manager.select_makers_for_sweep(
                        total_input_value=total_input_value,
                        my_txfee=estimated_tx_fee,
                        n=n_makers,
                    )
                )

                if len(selected_offers) < self.config.minimum_makers:
                    logger.error(f"Not enough makers for sweep: {len(selected_offers)}")
                    self.state = TakerState.FAILED
                    return None

                logger.info(f"Sweep: cj_amount={self.cj_amount:,} sats calculated for zero change")

            else:
                # NORMAL MODE: Select minimum UTXOs needed
                self.cj_amount = amount
                logger.info(f"Selecting {n_makers} makers for {self.cj_amount:,} sats...")

                selected_offers, total_fee = self.orderbook_manager.select_makers(
                    cj_amount=self.cj_amount,
                    n=n_makers,
                )

                if len(selected_offers) < self.config.minimum_makers:
                    logger.error(f"Not enough makers selected: {len(selected_offers)}")
                    self.state = TakerState.FAILED
                    return None

                # Pre-select UTXOs for CoinJoin, then generate PoDLE from one of them
                # This ensures the PoDLE UTXO is one we'll actually use in the transaction
                logger.info("Selecting UTXOs and generating PoDLE commitment...")

                # Use manually selected UTXOs if available
                if manually_selected_utxos:
                    self.preselected_utxos = manually_selected_utxos
                    logger.info(
                        f"Using {len(manually_selected_utxos)} manually selected UTXOs "
                        f"(total: {sum(u.value for u in manually_selected_utxos):,} sats)"
                    )
                else:
                    # Estimate required amount (conservative estimate for UTXO pre-selection)
                    # We'll refine this in _phase_build_tx once we have exact maker UTXOs
                    estimated_inputs = 2 + len(selected_offers) * 2  # Rough estimate
                    estimated_outputs = 2 + len(selected_offers) * 2
                    estimated_tx_fee = self._estimate_tx_fee(estimated_inputs, estimated_outputs)
                    estimated_required = self.cj_amount + total_fee + estimated_tx_fee

                    # Pre-select UTXOs for the CoinJoin
                    try:
                        self.preselected_utxos = self.wallet.select_utxos(
                            mixdepth, estimated_required, self.config.taker_utxo_age
                        )
                        logger.info(
                            f"Pre-selected {len(self.preselected_utxos)} UTXOs for CoinJoin "
                            f"(total: {sum(u.value for u in self.preselected_utxos):,} sats)"
                        )
                    except ValueError as e:
                        logger.error(str(e))
                        logger.error(
                            f"CoinJoin requires UTXOs with at least "
                            f"{self.config.taker_utxo_age} confirmation(s) "
                            f"(taker_utxo_age setting). Wait for more confirmations or "
                            f"lower taker_utxo_age in your config."
                        )
                        self.state = TakerState.FAILED
                        return None

            # Initialize maker sessions - neutrino_compat will be detected during handshake
            # when we receive the !pubkey response with features field
            self.maker_sessions = {
                nick: MakerSession(nick=nick, offer=offer, supports_neutrino_compat=False)
                for nick, offer in selected_offers.items()
            }

            logger.info(
                f"Selected {len(self.maker_sessions)} makers, total fee: {total_fee:,} sats"
            )

            # Log estimated transaction fee before prompting for confirmation
            # Conservative estimate: assume 1 input per maker + 20% buffer, rounded up
            import math

            estimated_maker_inputs = math.ceil(n_makers * 1.2)
            estimated_inputs = len(self.preselected_utxos) + estimated_maker_inputs
            # Outputs: 1 CJ output per participant + change outputs (assume all have change)
            estimated_outputs = (1 + n_makers) + (1 + n_makers)
            estimated_tx_fee = self._estimate_tx_fee(estimated_inputs, estimated_outputs)
            logger.info(
                f"Estimated transaction (mining) fee: {estimated_tx_fee:,} sats "
                f"(~{self._fee_rate:.2f} sat/vB for ~{estimated_inputs} inputs, "
                f"{estimated_outputs} outputs)"
            )

            # Prompt for confirmation after maker selection
            if hasattr(self, "confirmation_callback") and self.confirmation_callback:
                try:
                    # Build maker details for confirmation
                    maker_details = []
                    for nick, session in self.maker_sessions.items():
                        fee = session.offer.calculate_fee(self.cj_amount)
                        bond_value = session.offer.fidelity_bond_value
                        # Get maker's location from any connected directory
                        location = None
                        for client in self.directory_client.clients.values():
                            location = client._active_peers.get(nick)
                            if location and location != "NOT-SERVING-ONION":
                                break
                        maker_details.append(
                            {
                                "nick": nick,
                                "fee": fee,
                                "bond_value": bond_value,
                                "location": location,
                            }
                        )

                    confirmed = self.confirmation_callback(
                        maker_details=maker_details,
                        cj_amount=self.cj_amount,
                        total_fee=total_fee + estimated_tx_fee,
                        destination=destination,
                        mining_fee=estimated_tx_fee,
                        fee_rate=self._fee_rate,
                    )
                    if not confirmed:
                        logger.info("CoinJoin cancelled by user")
                        self.state = TakerState.CANCELLED
                        return None
                except Exception as e:
                    logger.error(f"Confirmation failed: {e}")
                    self.state = TakerState.FAILED
                    return None

            def get_private_key(addr: str) -> bytes | None:
                key = self.wallet.get_key_for_address(addr)
                if key is None:
                    return None

                priv_bytes = key.get_private_key_bytes()
                # If it's a Taproot address, we must use the tweaked private key
                # so the revealed public key P matches the UTXO's scriptPubKey.
                if is_p2tr_address(addr):
                    from jmcore.bitcoin import taproot_tweak_privkey

                    priv_bytes = taproot_tweak_privkey(priv_bytes)

                return priv_bytes

            # Generate PoDLE from pre-selected UTXOs only
            # This ensures the commitment is from a UTXO that will be in the transaction
            self.podle_commitment = self.podle_manager.generate_fresh_commitment(
                wallet_utxos=self.preselected_utxos,  # Only from pre-selected UTXOs!
                cj_amount=self.cj_amount,
                private_key_getter=get_private_key,
                min_confirmations=self.config.taker_utxo_age,
                min_percent=self.config.taker_utxo_amtpercent,
                max_retries=self.config.taker_utxo_retries,
            )

            if not self.podle_commitment:
                logger.error("Failed to generate PoDLE commitment")
                self.state = TakerState.FAILED
                return None

            # Phase 1: Fill orders (with retry logic for blacklisted commitments)
            self.state = TakerState.FILLING
            logger.info("Phase 1: Sending !fill to makers...")
            # Log directory routing info
            directory_count = len(self.directory_client.clients)
            directories = [
                f"{client.host}:{client.port}" for client in self.directory_client.clients.values()
            ]
            logger.info(
                f"Routing via {directory_count} director{'y' if directory_count == 1 else 'ies'}: "
                f"{', '.join(directories)}"
            )
            if self.directory_client.prefer_direct_connections:
                logger.debug(
                    "Direct connections preferred - will attempt to connect directly to makers"
                )
            else:
                logger.debug(
                    "Direct connections disabled - all messages relayed through directories"
                )

            # Fire-and-forget notification for CoinJoin start
            asyncio.create_task(
                get_notifier().notify_coinjoin_start(
                    self.cj_amount, len(self.maker_sessions), destination
                )
            )

            # Retry loop for blacklisted commitments and maker replacement
            max_podle_retries = self.config.taker_utxo_retries
            max_replacement_attempts = self.config.max_maker_replacement_attempts
            replacement_attempt = 0

            for podle_retry in range(max_podle_retries):
                fill_result = await self._phase_fill()

                if fill_result.success:
                    break  # Success, proceed to next phase

                if fill_result.blacklist_error:
                    # Don't add makers to ignored list when commitment is blacklisted
                    # The maker may accept a different commitment, so we should retry
                    # with a new NUMS index or different UTXO
                    logger.debug(
                        f"Commitment blacklisted by makers: {fill_result.failed_makers}. "
                        "Will retry with new commitment."
                    )
                elif fill_result.failed_makers:
                    # Add failed makers to ignore list for non-blacklist failures
                    for failed_nick in fill_result.failed_makers:
                        self.orderbook_manager.add_ignored_maker(failed_nick)
                        logger.debug(f"Added {failed_nick} to ignored makers (failed fill)")

                if fill_result.blacklist_error:
                    # Commitment was blacklisted - try with a new commitment
                    if podle_retry < max_podle_retries - 1:
                        logger.warning(
                            f"Commitment blacklisted, retrying with new NUMS index "
                            f"(attempt {podle_retry + 2}/{max_podle_retries})..."
                        )

                        # The current commitment is already marked as used
                        # Generate a new one (will use next index automatically)
                        self.podle_commitment = self.podle_manager.generate_fresh_commitment(
                            wallet_utxos=self.preselected_utxos,
                            cj_amount=self.cj_amount,
                            private_key_getter=get_private_key,
                            min_confirmations=self.config.taker_utxo_age,
                            min_percent=self.config.taker_utxo_amtpercent,
                            max_retries=self.config.taker_utxo_retries,
                        )

                        if not self.podle_commitment:
                            logger.error(
                                "No more PoDLE commitments available - all indices exhausted"
                            )
                            self.state = TakerState.FAILED
                            return None

                        # Reset maker sessions for retry (excluding ignored makers)
                        self.maker_sessions = {
                            nick: MakerSession(
                                nick=nick, offer=offer, supports_neutrino_compat=False
                            )
                            for nick, offer in selected_offers.items()
                            if nick not in self.orderbook_manager.ignored_makers
                        }
                        continue
                    else:
                        logger.error(
                            f"Fill phase failed after {max_podle_retries} PoDLE commitment attempts"
                        )
                        self.state = TakerState.FAILED
                        return None

                # Not a blacklist error - try maker replacement if enabled
                if fill_result.needs_replacement and replacement_attempt < max_replacement_attempts:
                    replacement_attempt += 1
                    needed = self.config.minimum_makers - len(self.maker_sessions)
                    logger.info(
                        f"Attempting maker replacement (attempt {replacement_attempt}/"
                        f"{max_replacement_attempts}): need {needed} more makers"
                    )

                    # Select replacement makers from orderbook
                    # Exclude makers already in current session to avoid reusing them
                    current_session_nicks = set(self.maker_sessions.keys())
                    replacement_offers, _ = self.orderbook_manager.select_makers(
                        cj_amount=self.cj_amount,
                        n=needed,
                        exclude_nicks=current_session_nicks,
                    )

                    if len(replacement_offers) < needed:
                        logger.error(
                            f"Not enough replacement makers available: "
                            f"found {len(replacement_offers)}, need {needed}"
                        )
                        self.state = TakerState.FAILED
                        return None

                    # Add replacement makers to session
                    for nick, offer in replacement_offers.items():
                        self.maker_sessions[nick] = MakerSession(
                            nick=nick, offer=offer, supports_neutrino_compat=False
                        )
                        logger.info(f"Added replacement maker: {nick}")

                    # Update selected_offers for potential future retries
                    selected_offers.update(replacement_offers)
                    continue

                # Failed and no replacement possible
                logger.error("Fill phase failed")
                self.state = TakerState.FAILED
                return None

            # Phase 2: Auth and get maker UTXOs (with maker replacement)
            self.state = TakerState.AUTHENTICATING
            logger.info("Phase 2: Sending !auth and receiving !ioauth...")

            auth_replacement_attempt = 0
            while True:
                auth_result = await self._phase_auth()

                if auth_result.success:
                    break  # Success, proceed to next phase

                # Add failed makers to ignore list
                for failed_nick in auth_result.failed_makers:
                    self.orderbook_manager.add_ignored_maker(failed_nick)
                    logger.debug(f"Added {failed_nick} to ignored makers (failed auth)")

                # Try maker replacement if enabled
                if (
                    auth_result.needs_replacement
                    and auth_replacement_attempt < max_replacement_attempts
                ):
                    auth_replacement_attempt += 1
                    needed = self.config.minimum_makers - len(self.maker_sessions)
                    logger.info(
                        f"Attempting maker replacement in auth phase "
                        f"(attempt {auth_replacement_attempt}/{max_replacement_attempts}): "
                        f"need {needed} more makers"
                    )

                    # Select replacement makers
                    # Exclude makers already in current session to avoid reusing them
                    current_session_nicks = set(self.maker_sessions.keys())
                    replacement_offers, _ = self.orderbook_manager.select_makers(
                        cj_amount=self.cj_amount,
                        n=needed,
                        exclude_nicks=current_session_nicks,
                    )

                    if len(replacement_offers) < needed:
                        logger.error(
                            f"Not enough replacement makers for auth phase: "
                            f"found {len(replacement_offers)}, need {needed}"
                        )
                        self.state = TakerState.FAILED
                        return None

                    # Add replacement makers - they need to go through fill first
                    for nick, offer in replacement_offers.items():
                        self.maker_sessions[nick] = MakerSession(
                            nick=nick, offer=offer, supports_neutrino_compat=False
                        )
                        logger.info(f"Added replacement maker for auth: {nick}")

                    # Run fill phase for new makers only
                    logger.info("Running fill phase for replacement makers...")
                    new_maker_nicks = list(replacement_offers.keys())

                    # Send !fill to new makers
                    if not self.podle_commitment or not self.crypto_session:
                        logger.error("Missing commitment or crypto session for replacement")
                        self.state = TakerState.FAILED
                        return None

                    commitment_hex = self.podle_commitment.to_commitment_str()
                    taker_pubkey = self.crypto_session.get_pubkey_hex()

                    # Establish communication channels for replacement makers
                    # (Same logic as in _phase_fill)
                    for nick in new_maker_nicks:
                        peer = self.directory_client._get_connected_peer(nick)
                        session = self.maker_sessions[nick]
                        if peer and self.directory_client.prefer_direct_connections:
                            session.comm_channel = "direct"
                            logger.debug(f"Will use DIRECT connection for replacement maker {nick}")
                        else:
                            # Use directory relay
                            target_directories = []

                            if nick in self.directory_client._active_nicks:
                                active_nicks_dict = self.directory_client._active_nicks[nick]
                                for server, is_active in active_nicks_dict.items():
                                    if is_active and server in self.directory_client.clients:
                                        target_directories.append(server)

                            if not target_directories:
                                for server, client in self.directory_client.clients.items():
                                    if nick in client._active_peers:
                                        target_directories.append(server)

                            if not target_directories:
                                target_directories = list(self.directory_client.clients.keys())

                            if target_directories:
                                chosen_dir = target_directories[0]
                                session.comm_channel = f"directory:{chosen_dir}"
                                logger.debug(
                                    f"Will use DIRECTORY relay {chosen_dir} "
                                    f"for replacement maker {nick}"
                                )

                    # Send !fill to replacement makers using their designated channels
                    for nick in new_maker_nicks:
                        session = self.maker_sessions[nick]
                        fill_data = (
                            f"{session.offer.oid} {self.cj_amount} {taker_pubkey} {commitment_hex}"
                        )
                        await self.directory_client.send_privmsg(
                            nick,
                            "fill",
                            fill_data,
                            log_routing=True,
                            force_channel=session.comm_channel,
                        )

                    # Wait for !pubkey responses from new makers
                    responses = await self.directory_client.wait_for_responses(
                        expected_nicks=new_maker_nicks,
                        expected_command="!pubkey",
                        timeout=self.config.maker_timeout_sec,
                    )

                    # Process responses for new makers
                    new_makers_ready = 0
                    for nick in new_maker_nicks:
                        if nick in responses and not responses[nick].get("error"):
                            try:
                                response_data = responses[nick]["data"].strip()
                                parts = response_data.split()
                                if parts:
                                    nacl_pubkey = parts[0]
                                    self.maker_sessions[nick].pubkey = nacl_pubkey
                                    self.maker_sessions[nick].responded_fill = True

                                    # Set up encryption (reuse taker keypair)
                                    crypto = CryptoSession.__new__(CryptoSession)
                                    crypto.keypair = self.crypto_session.keypair
                                    crypto.box = None
                                    crypto.counterparty_pubkey = ""
                                    crypto.setup_encryption(nacl_pubkey)
                                    self.maker_sessions[nick].crypto = crypto
                                    new_makers_ready += 1
                                    logger.debug(f"Replacement maker {nick} ready")
                            except Exception as e:
                                logger.warning(f"Failed to process {nick}: {e}")
                                del self.maker_sessions[nick]
                        else:
                            logger.warning(f"Replacement maker {nick} didn't respond")
                            if nick in self.maker_sessions:
                                del self.maker_sessions[nick]

                    if new_makers_ready == 0:
                        logger.error("No replacement makers responded to fill")
                        self.state = TakerState.FAILED
                        return None

                    # Continue to retry auth with all makers
                    continue

                # Failed and no replacement possible
                logger.error("Auth phase failed")
                self.state = TakerState.FAILED
                return None

            # Phase 3: Build transaction
            self.state = TakerState.BUILDING_TX
            logger.info("Phase 3: Building transaction...")

            tx_success = await self._phase_build_tx(
                destination=destination,
                mixdepth=mixdepth,
            )
            if not tx_success:
                logger.error("Transaction build failed")
                self.state = TakerState.FAILED
                return None

            # Phase 4: Collect signatures
            self.state = TakerState.COLLECTING_SIGNATURES
            logger.info("Phase 4: Collecting signatures...")

            sig_success = await self._phase_collect_signatures()
            if not sig_success:
                logger.error("Signature collection failed")
                self.state = TakerState.FAILED
                return None

            # Final confirmation before broadcast
            # Calculate exact transaction details
            num_taker_inputs = len(self.selected_utxos)
            num_maker_inputs = sum(len(s.utxos) for s in self.maker_sessions.values())
            total_inputs = num_taker_inputs + num_maker_inputs

            # Parse transaction to count outputs and sum output values
            tx = deserialize_transaction(self.final_tx)
            total_outputs = len(tx.outputs)
            total_output_value = sum(out.value for out in tx.outputs)

            # Calculate total input value (taker + maker UTXOs)
            taker_input_value = sum(utxo.value for utxo in self.selected_utxos)
            maker_input_value = sum(
                utxo["value"] for session in self.maker_sessions.values() for utxo in session.utxos
            )
            total_input_value = taker_input_value + maker_input_value

            # Calculate actual mining fee from the transaction (includes any residual/dust)
            actual_mining_fee = total_input_value - total_output_value

            # Calculate maker fees
            total_maker_fees = sum(
                calculate_cj_fee(session.offer, self.cj_amount)
                for session in self.maker_sessions.values()
            )
            total_cost = total_maker_fees + actual_mining_fee

            # Calculate actual fee rate from the final signed transaction
            actual_vsize = calculate_tx_vsize(self.final_tx)
            actual_fee_rate = actual_mining_fee / actual_vsize if actual_vsize > 0 else 0.0

            # Log final transaction details
            logger.info("=" * 70)
            logger.info("FINAL TRANSACTION SUMMARY - Ready to broadcast")
            logger.info("=" * 70)
            logger.info(f"CoinJoin amount:      {self.cj_amount:,} sats")
            logger.info(f"Makers participating: {len(self.maker_sessions)}")
            logger.info(
                f"  Makers: {', '.join(nick[:10] + '...' for nick in self.maker_sessions.keys())}"
            )
            logger.info(
                f"Transaction inputs:   {total_inputs} ({num_taker_inputs} yours, "
                f"{num_maker_inputs} makers)"
            )
            logger.info(f"Transaction outputs:  {total_outputs}")
            logger.info(f"Maker fees:           {total_maker_fees:,} sats")
            logger.info(
                f"Mining fee:           {actual_mining_fee:,} sats ({actual_fee_rate:.2f} sat/vB)"
            )
            logger.info(f"Total cost:           {total_cost:,} sats")
            logger.info(f"Transaction size:     {actual_vsize} vbytes ({len(self.final_tx)} bytes)")
            logger.info("-" * 70)
            logger.info("Transaction hex (for manual verification/broadcast):")
            logger.info(self.final_tx.hex())
            logger.info("=" * 70)

            # Prompt for final confirmation if callback is set
            if hasattr(self, "confirmation_callback") and self.confirmation_callback:
                try:
                    # Build maker details for final confirmation
                    maker_details = []
                    for nick, session in self.maker_sessions.items():
                        fee = calculate_cj_fee(session.offer, self.cj_amount)
                        bond_value = session.offer.fidelity_bond_value
                        # Get maker's location from any connected directory
                        location = None
                        for client in self.directory_client.clients.values():
                            location = client._active_peers.get(nick)
                            if location and location != "NOT-SERVING-ONION":
                                break
                        maker_details.append(
                            {
                                "nick": nick,
                                "fee": fee,
                                "bond_value": bond_value,
                                "location": location,
                            }
                        )

                    confirmed = self.confirmation_callback(
                        maker_details=maker_details,
                        cj_amount=self.cj_amount,
                        total_fee=total_cost,
                        destination=destination,
                        mining_fee=actual_mining_fee,
                        fee_rate=actual_fee_rate,
                    )
                    if not confirmed:
                        logger.warning("User declined final broadcast confirmation")
                        # Log CSV entry for manual tracking/broadcast
                        self._log_manual_csv_entry(total_maker_fees, actual_mining_fee, destination)
                        self.state = TakerState.FAILED
                        return None
                except Exception as e:
                    logger.error(f"Final confirmation callback failed: {e}")
                    self.state = TakerState.FAILED
                    return None

            # Phase 5: Broadcast
            self.state = TakerState.BROADCASTING
            logger.info("Phase 5: Broadcasting transaction...")

            self.txid = await self._phase_broadcast()
            if not self.txid:
                logger.error("Broadcast failed")
                self.state = TakerState.FAILED
                return None

            self.state = TakerState.COMPLETE
            logger.info(f"CoinJoin COMPLETE! txid: {self.txid}")

            # Update the "Awaiting transaction" history entry with txid and mining fee
            # The entry was created before sending !tx to preserve address privacy
            try:
                # Use actual_mining_fee (total_inputs - total_outputs) computed from the
                # signed transaction, NOT tx_metadata["fee"] which is just the estimated
                # fee used during transaction construction. The actual fee includes any
                # residual from sweep rounding and reflects the real cost to the taker.
                updated = update_taker_awaiting_transaction_broadcast(
                    destination_address=self.cj_destination,
                    change_address=self.taker_change_address,  # Empty string if no change
                    txid=self.txid,
                    mining_fee=actual_mining_fee,
                    data_dir=self.config.data_dir,
                )
                if updated:
                    logger.debug(
                        f"Updated history entry for CJ txid {self.txid[:16]}..., "
                        f"mining_fee={actual_mining_fee} sats"
                    )
                else:
                    logger.warning(
                        f"No matching 'Awaiting transaction' entry found for "
                        f"{self.cj_destination[:20]}... - history may be inconsistent"
                    )

                # Immediately check if tx is confirmed/in mempool and update history
                # This is important for one-shot coinjoin CLI calls that exit immediately
                await self._update_pending_transaction_now(self.txid, self.cj_destination)
            except Exception as e:
                logger.warning(f"Failed to update CoinJoin history: {e}")

            # Fire-and-forget notification for successful CoinJoin
            total_fees = total_maker_fees + actual_mining_fee
            asyncio.create_task(
                get_notifier().notify_coinjoin_complete(
                    self.txid, self.cj_amount, len(self.maker_sessions), total_fees
                )
            )

            return self.txid

        except Exception as e:
            logger.error(f"CoinJoin failed: {e}")
            # Fire-and-forget notification for failed CoinJoin
            phase = self.state.value if hasattr(self, "state") else ""
            amount = self.cj_amount if hasattr(self, "cj_amount") else 0
            asyncio.create_task(get_notifier().notify_coinjoin_failed(str(e), phase, amount))
            self.state = TakerState.FAILED
            return None

    async def _phase_fill(self) -> PhaseResult:
        """Send !fill to all selected makers and wait for !pubkey responses.

        Returns:
            PhaseResult with success status, failed makers list, and blacklist flag.
        """
        if not self.podle_commitment:
            return PhaseResult(success=False)

        # Create a new crypto session for this CoinJoin
        self.crypto_session = CryptoSession()
        taker_pubkey = self.crypto_session.get_pubkey_hex()
        commitment_hex = self.podle_commitment.to_commitment_str()

        # CRITICAL: Establish communication channels BEFORE sending !fill
        # We must use the SAME channel for ALL messages to each maker in this session
        # Mixing channels (e.g., !fill via directory, !auth via direct) causes makers to reject
        #
        # Strategy:
        # 1. Try to establish direct connections (with reasonable timeout)
        # 2. Choose ONE channel per maker (direct OR specific directory)
        # 3. Record the channel in maker_session.comm_channel
        # 4. Use only that channel for all subsequent messages

        # Start direct connection attempts for all makers
        if self.directory_client.prefer_direct_connections:
            for nick in self.maker_sessions.keys():
                maker_location = self.directory_client._get_peer_location(nick)
                if maker_location:
                    self.directory_client._try_direct_connect(nick)

        # Wait up to 5 seconds for direct connections to establish
        # This timeout balances privacy (prefer direct) vs latency (don't wait too long)
        if self.directory_client.prefer_direct_connections:
            pending_tasks = []
            for nick in self.maker_sessions.keys():
                if nick in self.directory_client._pending_connect_tasks:
                    task = self.directory_client._pending_connect_tasks[nick]
                    if not task.done():
                        pending_tasks.append(task)

            if pending_tasks:
                logger.info(
                    f"Waiting up to 5s for direct connections to {len(pending_tasks)} makers..."
                )
                done, pending = await asyncio.wait(
                    pending_tasks, timeout=5.0, return_when=asyncio.ALL_COMPLETED
                )
                connected_count = len([t for t in done if not t.exception()])
                if connected_count > 0:
                    logger.info(
                        f"Established {connected_count}/{len(pending_tasks)} direct connections"
                    )

        # Determine and record communication channel for each maker
        for nick, session in self.maker_sessions.items():
            # Check if direct connection is available
            peer = self.directory_client._get_connected_peer(nick)
            if peer and self.directory_client.prefer_direct_connections:
                session.comm_channel = "direct"
                logger.debug(f"Will use DIRECT connection for {nick}")
            else:
                # Use directory relay - pick one directory for this maker
                maker_location = self.directory_client._get_peer_location(nick)
                target_directories = []

                # Check active nicks tracking first
                if nick in self.directory_client._active_nicks:
                    for server, is_active in self.directory_client._active_nicks[nick].items():
                        if is_active and server in self.directory_client.clients:
                            target_directories.append(server)

                # If not found, try all clients that list the peer
                if not target_directories:
                    for server, client in self.directory_client.clients.items():
                        if nick in client._active_peers:
                            target_directories.append(server)

                # If still not found, use all connected clients
                if not target_directories:
                    target_directories = list(self.directory_client.clients.keys())

                # Pick first directory (already shuffled during orderbook fetch)
                if target_directories:
                    chosen_dir = target_directories[0]
                    session.comm_channel = f"directory:{chosen_dir}"
                    logger.debug(
                        f"Will use DIRECTORY relay {chosen_dir} for {nick} "
                        f"(onion: {maker_location or 'unknown'})"
                    )
                else:
                    # This should never happen if we're connected to directories
                    raise RuntimeError(f"No communication channel available for {nick}")

        # Send !fill to all makers using their designated channels
        # Format: fill <oid> <amount> <taker_pubkey> <commitment> [address_type=p2tr]
        for nick, session in self.maker_sessions.items():
            fill_data = f"{session.offer.oid} {self.cj_amount} {taker_pubkey} {commitment_hex}"
            # Signal desired address type to makers (backwards compatible — old makers ignore it)
            if self.wallet.address_type == "p2tr":
                fill_data += " address_type=p2tr"
            channel = await self.directory_client.send_privmsg(
                nick, "fill", fill_data, log_routing=True, force_channel=session.comm_channel
            )
            # Verify the channel used matches what we recorded
            assert channel == session.comm_channel, f"Channel mismatch for {nick}"

        # Wait for all !pubkey responses at once
        timeout = self.config.maker_timeout_sec
        expected_nicks = list(self.maker_sessions.keys())

        responses = await self.directory_client.wait_for_responses(
            expected_nicks=expected_nicks,
            expected_command="!pubkey",
            timeout=timeout,
        )

        # Track failed makers and blacklist errors
        failed_makers: list[str] = []
        blacklist_error = False

        # Process responses
        # Maker sends: "<nacl_pubkey> [features=...] <signing_pubkey> <signature>"
        # Directory client strips command, we get the data part
        # Note: responses may include error responses with {"error": True, "data": "reason"}
        for nick in list(self.maker_sessions.keys()):
            if nick in responses:
                # Check if this is an error response
                if responses[nick].get("error"):
                    error_msg = responses[nick].get("data", "Unknown error")
                    logger.error(f"Maker {nick} rejected !fill: {error_msg}")
                    # Check if this is a blacklist error
                    if "blacklist" in error_msg.lower():
                        blacklist_error = True
                        logger.warning(
                            f"Commitment was blacklisted by {nick} - may need retry with new index"
                        )
                    failed_makers.append(nick)
                    del self.maker_sessions[nick]
                    continue

                try:
                    response_data = responses[nick]["data"].strip()
                    # Format: "<nacl_pubkey_hex> [features=...] <signing_pk> <sig>"
                    # We need the first part (nacl_pubkey_hex) and optionally features
                    parts = response_data.split()
                    if parts:
                        nacl_pubkey = parts[0]
                        self.maker_sessions[nick].pubkey = nacl_pubkey
                        self.maker_sessions[nick].responded_fill = True

                        # Parse optional features (e.g., "features=neutrino_compat")
                        for part in parts[1:]:
                            if part.startswith("features="):
                                features_str = part[9:]  # Skip "features="
                                features = set(features_str.split(",")) if features_str else set()
                                if "neutrino_compat" in features:
                                    self.maker_sessions[nick].supports_neutrino_compat = True
                                    logger.debug(f"Maker {nick} supports neutrino_compat")
                                break

                        # Set up encryption session with this maker using their NaCl pubkey
                        # IMPORTANT: Reuse the same keypair from self.crypto_session
                        # that was sent in !fill, just set up new box with maker's pubkey
                        crypto = CryptoSession.__new__(CryptoSession)
                        crypto.keypair = self.crypto_session.keypair  # Reuse taker keypair!
                        crypto.box = None
                        crypto.counterparty_pubkey = ""
                        crypto.setup_encryption(nacl_pubkey)
                        self.maker_sessions[nick].crypto = crypto
                        logger.debug(
                            f"Processed !pubkey from {nick}: {nacl_pubkey[:16]}..., "
                            f"encryption set up"
                        )
                    else:
                        logger.warning(f"Empty !pubkey response from {nick}")
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                except Exception as e:
                    logger.warning(f"Invalid !pubkey response from {nick}: {e}")
                    failed_makers.append(nick)
                    del self.maker_sessions[nick]
            else:
                logger.warning(f"No !pubkey response from {nick}")
                failed_makers.append(nick)
                del self.maker_sessions[nick]

        if len(self.maker_sessions) < self.config.minimum_makers:
            logger.error(f"Not enough makers responded: {len(self.maker_sessions)}")
            return PhaseResult(
                success=False, failed_makers=failed_makers, blacklist_error=blacklist_error
            )

        return PhaseResult(
            success=True, failed_makers=failed_makers, blacklist_error=blacklist_error
        )

    def _validate_maker_address_types(self, nick: str, cj_addr: str, change_addr: str) -> bool:
        """
        Validate that a maker's CoinJoin and change addresses match the taker's
        address type. Mixed address types (P2WPKH + P2TR) in a CoinJoin make it
        trivial to distinguish taker vs maker outputs.

        Returns:
            True if addresses are valid/compatible, False if they should be rejected.
        """
        taker_is_taproot = self.wallet.address_type == "p2tr"
        p2tr_prefixes = ("bc1p", "tb1p", "bcrt1p")
        maker_cj_is_taproot = cj_addr.startswith(p2tr_prefixes)
        maker_change_is_taproot = change_addr.startswith(p2tr_prefixes)

        if taker_is_taproot and (not maker_cj_is_taproot or not maker_change_is_taproot):
            logger.warning(
                f"Rejecting maker {nick}: taker uses P2TR but maker "
                f"sent non-Taproot addresses (cj={cj_addr[:16]}..., "
                f"change={change_addr[:16]}...)"
            )
            return False
        elif not taker_is_taproot and (maker_cj_is_taproot or maker_change_is_taproot):
            logger.warning(
                f"Rejecting maker {nick}: taker uses P2WPKH but maker "
                f"sent Taproot addresses (cj={cj_addr[:16]}..., "
                f"change={change_addr[:16]}...)"
            )
            return False
        return True

    async def _phase_auth(self) -> PhaseResult:
        """Send !auth with PoDLE proof and wait for !ioauth responses.

        Returns:
            PhaseResult with success status and failed makers list.
        """
        if not self.podle_commitment:
            return PhaseResult(success=False)

        # Send !auth to each maker with format based on their feature support.
        # - Makers with neutrino_compat: MUST receive extended format
        #   (txid:vout:scriptpubkey:blockheight)
        # - Legacy makers: Receive legacy format (txid:vout)
        #
        # Feature detection happens via handshake - makers advertise neutrino_compat
        # in their !pubkey response's features field. This is backwards compatible:
        # legacy JoinMarket makers don't send features, so they default to legacy format.
        #
        # Compatibility matrix:
        # | Taker Backend | Maker neutrino_compat | Action |
        # |---------------|----------------------|--------|
        # | Full node     | False                | Send legacy format |
        # | Full node     | True                 | Send extended format (maker requires it) |
        # | Neutrino      | False                | FAIL - incompatible, maker filtered out |
        # | Neutrino      | True                 | Send extended format (both support it) |
        has_metadata = self.podle_commitment.has_neutrino_metadata()
        taker_requires_extended = self.backend.requires_neutrino_metadata()

        for nick, session in list(self.maker_sessions.items()):
            if session.crypto is None:
                logger.error(f"No encryption session for {nick}")
                continue

            maker_requires_extended = session.supports_neutrino_compat

            # Fail early if taker needs extended format but maker doesn't support it
            # This happens when taker uses Neutrino backend but maker uses full node
            # The maker won't be able to verify our UTXO without extended metadata
            if taker_requires_extended and not maker_requires_extended:
                logger.error(
                    f"Incompatible maker {nick}: taker uses Neutrino backend but maker "
                    f"doesn't support neutrino_compat. Maker cannot verify our UTXOs."
                )
                del self.maker_sessions[nick]
                continue

            # Send extended format if:
            # 1. We have the metadata AND
            # 2. Either maker requires it OR we (taker) need it for our verification
            use_extended = has_metadata and (maker_requires_extended or taker_requires_extended)
            revelation = self.podle_commitment.to_revelation(extended=use_extended)

            # Create pipe-separated revelation format:
            # Legacy: txid:vout|P|P2|sig|e
            # Extended: txid:vout:scriptpubkey:blockheight|P|P2|sig|e
            revelation_str = "|".join(
                [
                    revelation["utxo"],
                    revelation["P"],
                    revelation["P2"],
                    revelation["sig"],
                    revelation["e"],
                ]
            )

            if use_extended:
                logger.debug(f"Sending extended UTXO format to maker {nick}")
            else:
                logger.debug(f"Sending legacy UTXO format to maker {nick}")

            # Encrypt and send (using same channel as !fill)
            encrypted_revelation = session.crypto.encrypt(revelation_str)
            await self.directory_client.send_privmsg(
                nick,
                "auth",
                encrypted_revelation,
                log_routing=True,
                force_channel=session.comm_channel,
            )

        # Track makers filtered due to incompatibility (not the same as failed)
        incompatible_makers: list[str] = []

        # Check if we still have enough makers after filtering incompatible ones
        if len(self.maker_sessions) < self.config.minimum_makers:
            logger.error(
                f"Not enough compatible makers: {len(self.maker_sessions)} "
                f"< {self.config.minimum_makers}. Neutrino takers require neutrino_compat."
            )
            return PhaseResult(success=False, failed_makers=incompatible_makers)

        # Wait for all !ioauth responses at once
        timeout = self.config.maker_timeout_sec
        expected_nicks = list(self.maker_sessions.keys())

        responses = await self.directory_client.wait_for_responses(
            expected_nicks=expected_nicks,
            expected_command="!ioauth",
            timeout=timeout,
        )

        # Track failed makers for potential replacement
        failed_makers: list[str] = []

        # Process responses
        # Maker sends !ioauth as ENCRYPTED space-separated:
        # <utxo_list> <auth_pub> <cj_addr> <change_addr> <btc_sig>
        # where utxo_list can be:
        # - Legacy format: txid:vout,txid:vout,...
        # - Extended format (neutrino_compat): txid:vout:scriptpubkey:blockheight,...
        # Response format from directory: "<encrypted_data> <signing_pubkey> <signature>"
        for nick in list(self.maker_sessions.keys()):
            if nick in responses:
                try:
                    session = self.maker_sessions[nick]
                    if session.crypto is None:
                        logger.warning(f"No encryption session for {nick}")
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    # Extract encrypted data (first part of response)
                    response_data = responses[nick]["data"].strip()
                    parts = response_data.split()
                    if not parts:
                        logger.warning(f"Empty !ioauth response from {nick}")
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    encrypted_data = parts[0]

                    # Decrypt the ioauth message
                    decrypted = session.crypto.decrypt(encrypted_data)
                    logger.debug(f"Decrypted !ioauth from {nick}: {decrypted[:50]}...")

                    # Parse: <utxo_list> <auth_pub> <cj_addr> <change_addr> <btc_sig>
                    ioauth_parts = decrypted.split()
                    if len(ioauth_parts) < 4:
                        logger.warning(
                            f"Invalid !ioauth format from {nick}: expected 5 parts, "
                            f"got {len(ioauth_parts)}"
                        )
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    utxo_list_str = ioauth_parts[0]
                    auth_pub = ioauth_parts[1]
                    cj_addr = ioauth_parts[2]
                    change_addr = ioauth_parts[3]

                    # Verify btc_sig if present - proves maker owns the UTXO
                    # NOTE: BTC sig verification is OPTIONAL per JoinMarket protocol
                    # It provides additional security by proving maker controls the UTXO
                    # but not all makers may provide it
                    if len(ioauth_parts) >= 5:
                        btc_sig = ioauth_parts[4]
                        # The signature is over the maker's NaCl pubkey
                        from jmcore.crypto import ecdsa_verify

                        maker_nacl_pk = session.pubkey  # Maker's NaCl pubkey from !pubkey
                        auth_pub_bytes = bytes.fromhex(auth_pub)
                        logger.debug(
                            f"Verifying BTC sig from {nick}: "
                            f"message={maker_nacl_pk[:32]}..., "
                            f"sig={btc_sig[:32]}..., "
                            f"pubkey={auth_pub[:16]}..."
                        )
                        if not ecdsa_verify(maker_nacl_pk, btc_sig, auth_pub_bytes):
                            logger.warning(
                                f"BTC signature verification failed from {nick} - "
                                f"continuing anyway (optional security feature)"
                            )
                            # NOTE: We don't delete the session here - BTC sig is optional
                            # The transaction verification will still protect against fraud
                        else:
                            logger.info(f"BTC signature verified for {nick}")

                    # Parse utxo_list using protocol helper
                    # (handles both legacy and extended format)
                    # Then verify each UTXO using the appropriate backend method
                    session.utxos = []
                    utxo_metadata_list = parse_utxo_list(utxo_list_str)

                    # Track if maker sent extended format
                    has_extended = any(u.has_neutrino_metadata() for u in utxo_metadata_list)
                    if has_extended:
                        session.supports_neutrino_compat = True
                        logger.debug(f"Maker {nick} sent extended UTXO format (neutrino_compat)")

                    for utxo_meta in utxo_metadata_list:
                        txid = utxo_meta.txid
                        vout = utxo_meta.vout

                        # Verify UTXO and get value/address
                        try:
                            if (
                                self.backend.requires_neutrino_metadata()
                                and utxo_meta.has_neutrino_metadata()
                            ):
                                # Use Neutrino-compatible verification with metadata
                                result = await self.backend.verify_utxo_with_metadata(
                                    txid=txid,
                                    vout=vout,
                                    scriptpubkey=utxo_meta.scriptpubkey,  # type: ignore
                                    blockheight=utxo_meta.blockheight,  # type: ignore
                                )
                                if result.valid:
                                    value = result.value
                                    address = ""  # Not available from verification
                                    logger.debug(
                                        f"Neutrino-verified UTXO {txid}:{vout} = {value} sats"
                                    )
                                else:
                                    logger.warning(
                                        f"Neutrino UTXO verification failed for "
                                        f"{txid}:{vout}: {result.error}"
                                    )
                                    continue
                            else:
                                # Full node: direct UTXO lookup
                                utxo_info = await self.backend.get_utxo(txid, vout)
                                if utxo_info:
                                    value = utxo_info.value
                                    address = utxo_info.address
                                else:
                                    # Fallback: get raw transaction and parse it
                                    tx_info = await self.backend.get_transaction(txid)
                                    if tx_info and tx_info.raw:
                                        parsed_tx = parse_transaction(tx_info.raw)
                                        if parsed_tx and len(parsed_tx.outputs) > vout:
                                            value = parsed_tx.outputs[vout].value
                                            try:
                                                address = parsed_tx.outputs[vout].address(
                                                    self.config.network
                                                )
                                            except (ValueError, Exception):
                                                address = ""
                                        else:
                                            logger.warning(
                                                f"Could not parse output {vout} from tx {txid}"
                                            )
                                            value = 0
                                            address = ""
                                    else:
                                        logger.warning(f"Could not fetch transaction {txid}")
                                        value = 0
                                        address = ""
                        except Exception as e:
                            logger.warning(f"Error verifying UTXO {txid}:{vout}: {e}")
                            value = 0
                            address = ""

                        session.utxos.append(
                            {
                                "txid": txid,
                                "vout": vout,
                                "value": value,
                                "address": address,
                            }
                        )
                        logger.debug(f"Added UTXO from {nick}: {txid}:{vout} = {value} sats")

                    session.cj_address = cj_addr
                    session.change_address = change_addr
                    session.auth_pubkey = auth_pub  # Store for later verification
                    session.responded_auth = True

                    if not self._validate_maker_address_types(nick, cj_addr, change_addr):
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    logger.debug(
                        f"Processed !ioauth from {nick}: {len(session.utxos)} UTXOs, "
                        f"cj_addr={cj_addr[:16]}..."
                    )
                except Exception as e:
                    logger.warning(f"Invalid !ioauth response from {nick}: {e}")
                    failed_makers.append(nick)
                    del self.maker_sessions[nick]
            else:
                logger.warning(f"No !ioauth response from {nick}")
                failed_makers.append(nick)
                del self.maker_sessions[nick]

        if len(self.maker_sessions) < self.config.minimum_makers:
            logger.error(f"Not enough makers sent UTXOs: {len(self.maker_sessions)}")
            return PhaseResult(success=False, failed_makers=failed_makers)

        return PhaseResult(success=True, failed_makers=failed_makers)

    def _parse_utxos(self, utxos_dict: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse UTXO data from !ioauth response."""
        result = []
        for utxo_str, info in utxos_dict.items():
            try:
                txid, vout_str = utxo_str.split(":")
                result.append(
                    {
                        "txid": txid,
                        "vout": int(vout_str),
                        "value": info.get("value", 0),
                        "address": info.get("address", ""),
                    }
                )
            except (ValueError, KeyError):
                continue
        return result

    async def _phase_build_tx(self, destination: str, mixdepth: int) -> bool:
        """Build the unsigned CoinJoin transaction."""
        try:
            # Store destination for broadcast verification
            self.cj_destination = destination

            # Calculate total input needed (now with exact maker UTXOs)
            total_maker_fee = sum(
                calculate_cj_fee(s.offer, self.cj_amount) for s in self.maker_sessions.values()
            )

            # Estimate tx fee with actual input counts
            num_taker_inputs = len(self.preselected_utxos)
            num_maker_inputs = sum(len(s.utxos) for s in self.maker_sessions.values())
            num_inputs = num_taker_inputs + num_maker_inputs

            # Output count depends on sweep mode:
            # - Normal: CJ outputs (1 + n_makers) + change outputs (1 + n_makers)
            # - Sweep: CJ outputs (1 + n_makers) + maker changes only (n_makers)
            if self.is_sweep:
                # No taker change output in sweep mode
                num_outputs = 1 + len(self.maker_sessions) + len(self.maker_sessions)
            else:
                # Normal mode: include taker change
                num_outputs = 1 + len(self.maker_sessions) + 1 + len(self.maker_sessions)

            # Calculate actual tx fee based on real transaction size
            actual_tx_fee = self._estimate_tx_fee(num_inputs, num_outputs)

            preselected_total = sum(u.value for u in self.preselected_utxos)

            if self.is_sweep:
                # SWEEP MODE: Use ALL preselected UTXOs, preserve cj_amount from !fill
                selected_utxos = self.preselected_utxos
                logger.info(
                    f"Sweep mode: using all {len(selected_utxos)} UTXOs, "
                    f"total {preselected_total:,} sats"
                )

                # For sweeps, we MUST use the tx_fee_budget that was calculated at order
                # selection time. The equation that determined cj_amount was:
                #   total_input = cj_amount + maker_fees + tx_fee_budget
                #
                # Using any other value for tx_fee would create a residual:
                #   residual = total_input - cj_amount - maker_fees - tx_fee
                #            = tx_fee_budget - tx_fee
                #
                # If tx_fee < budget: positive residual goes to miners (overpaying!)
                # If tx_fee > budget: negative residual fails the CJ (underfunded)
                #
                # By using the budget as tx_fee, we ensure:
                #   - The taker pays exactly what was stated at the start
                #   - The fee rate may differ based on actual tx size
                #   - No funds are lost to unexpected miner fees
                #
                # Calculate actual vsize for fee rate logging
                actual_tx_vsize = num_inputs * 68 + num_outputs * 31 + 11

                # Use the budget as the tx_fee
                tx_fee = self._sweep_tx_fee_budget

                # Calculate residual (should be minimal - just from integer division)
                residual = preselected_total - self.cj_amount - total_maker_fee - tx_fee
                actual_fee_rate = tx_fee / actual_tx_vsize if actual_tx_vsize > 0 else 0

                logger.info(
                    f"Sweep: cj_amount={self.cj_amount:,} (from !fill), "
                    f"maker_fees={total_maker_fee:,}, "
                    f"tx_fee={tx_fee:,} (budget), "
                    f"residual={residual} sats, "
                    f"actual_vsize={actual_tx_vsize}, "
                    f"effective_rate={actual_fee_rate:.2f} sat/vB"
                )

                if residual < 0:
                    # Negative residual means the budget was insufficient
                    # This should only happen if there's a bug in the calculation
                    logger.error(
                        f"Sweep failed: negative residual of {residual} sats. "
                        f"This indicates a bug in cj_amount calculation. "
                        f"total_input={preselected_total}, cj_amount={self.cj_amount}, "
                        f"maker_fees={total_maker_fee}, tx_fee_budget={tx_fee}"
                    )
                    return False

                # Small positive residual (typically < 100 sats) is expected from integer
                # division in calculate_sweep_amount. This goes to miners.
                if residual > 100:
                    # Larger residual indicates a calculation issue
                    logger.warning(
                        f"Sweep: unexpected residual of {residual} sats. "
                        f"Expected < 100 sats from integer rounding. "
                        "This may indicate a fee calculation mismatch."
                    )

                # The residual becomes additional miner fee (no taker change in sweep)

            else:
                # NORMAL MODE: Use pre-selected UTXOs, add more if needed
                # For normal mode, we use the actual tx_fee estimate
                tx_fee = actual_tx_fee
                required = self.cj_amount + total_maker_fee + tx_fee

                # Use pre-selected UTXOs (which include the PoDLE UTXO)
                # These were selected during PoDLE generation to ensure the commitment
                # UTXO is one we'll actually use in the transaction
                if preselected_total >= required:
                    # Pre-selected UTXOs are sufficient
                    selected_utxos = self.preselected_utxos
                    logger.info(
                        f"Using pre-selected UTXOs: {len(selected_utxos)} UTXOs, "
                        f"total {preselected_total:,} sats (need {required:,})"
                    )
                else:
                    # Need additional UTXOs beyond pre-selection
                    # This can happen if actual fees were higher than estimated
                    logger.warning(
                        f"Pre-selected UTXOs insufficient: have {preselected_total:,}, "
                        f"need {required:,}. Selecting additional UTXOs..."
                    )
                    selected_utxos = self.wallet.select_utxos(
                        mixdepth,
                        required,
                        self.config.taker_utxo_age,
                        include_utxos=self.preselected_utxos,  # Include pre-selected (PoDLE UTXO)
                    )

            if not selected_utxos:
                logger.error("Failed to select enough UTXOs")
                return False

            # Store selected UTXOs for signing later
            self.selected_utxos = selected_utxos

            taker_total = sum(u.value for u in selected_utxos)

            # Calculate expected change to determine if we need a change address
            # Change = total_input - cj_amount - maker_fees - tx_fee
            expected_change = taker_total - self.cj_amount - total_maker_fee - tx_fee

            # Only generate change address if we'll actually have a change output
            # This avoids recording unused addresses in history
            if expected_change > self.config.dust_threshold:
                change_index = self.wallet.get_next_address_index(mixdepth, 1)
                taker_change_address = self.wallet.get_change_address(mixdepth, change_index)
                self.taker_change_address = taker_change_address
                logger.debug(f"Generated change address (expected: {expected_change} sats)")
            else:
                # No change output needed (sweep or change is dust)
                taker_change_address = ""  # Will be ignored by tx builder
                self.taker_change_address = ""
                if expected_change > 0:
                    logger.debug(
                        f"No change address needed: change {expected_change} sats "
                        f"is below dust threshold ({self.config.dust_threshold})"
                    )
                else:
                    logger.debug("No change address needed: sweep mode (exact spend)")

            # Build maker data
            maker_data = {}
            for nick, session in self.maker_sessions.items():
                cjfee = calculate_cj_fee(session.offer, self.cj_amount)
                # JoinMarket protocol: txfee in offer is the total transaction fee
                # the maker contributes (in satoshis), not a per-input/output fee
                maker_txfee = session.offer.txfee

                maker_data[nick] = {
                    "utxos": session.utxos,
                    "cj_addr": session.cj_address,
                    "change_addr": session.change_address,
                    "cjfee": cjfee,
                    "txfee": maker_txfee,
                }

            # Build transaction
            network = self.config.network.value
            self.unsigned_tx, self.tx_metadata = build_coinjoin_tx(
                taker_utxos=[
                    {
                        "txid": u.txid,
                        "vout": u.vout,
                        "value": u.value,
                        "scriptpubkey": u.scriptpubkey,
                    }
                    for u in selected_utxos
                ],
                taker_cj_address=destination,
                taker_change_address=taker_change_address,
                taker_total_input=taker_total,
                maker_data=maker_data,
                cj_amount=self.cj_amount,
                tx_fee=tx_fee,
                network=network,
                dust_threshold=self.config.dust_threshold,
            )

            logger.info(f"Built unsigned tx: {len(self.unsigned_tx)} bytes")

            # Log final transaction details
            logger.info(
                f"Final CoinJoin transaction details: "
                f"{num_inputs} inputs ({num_taker_inputs} taker, {num_maker_inputs} maker), "
                f"{num_outputs} outputs"
            )
            logger.info(
                f"Transaction amounts: cj_amount={self.cj_amount:,} sats, "
                f"total_maker_fees={total_maker_fee:,} sats, "
                f"mining_fee={tx_fee:,} sats "
                f"({self._fee_rate:.2f} sat/vB)"
            )
            logger.info(f"Participating makers: {', '.join(self.maker_sessions.keys())}")

            return True

        except Exception as e:
            logger.error(f"Failed to build transaction: {e}")
            return False

    def _estimate_tx_fee(
        self, num_inputs: int, num_outputs: int, *, use_base_rate: bool = False
    ) -> int:
        """Estimate transaction fee.

        Uses the fee rate from _resolve_fee_rate() which must be called before
        this method. By default, uses the session's randomized fee rate for
        privacy. For sweep budget calculations, use_base_rate=True to get
        a deterministic estimate.

        Uses estimate_vsize() from jmcore.bitcoin which handles P2WPKH, P2TR,
        and P2WSH input/output weights correctly. The wallet's address_type
        determines the input/output type used for estimation.

        Args:
            num_inputs: Number of transaction inputs
            num_outputs: Number of transaction outputs
            use_base_rate: If True, use the base fee rate instead of the
                          session's randomized rate. Used for sweep cj_amount
                          calculations where determinism is required.

        Returns:
            Estimated fee in satoshis
        """
        import math

        from jmcore.bitcoin import estimate_vsize

        addr_type = getattr(self.wallet, "address_type", "p2wpkh")
        input_types = [addr_type] * num_inputs
        output_types = [addr_type] * num_outputs
        vsize = estimate_vsize(input_types, output_types)

        # Use base rate for deterministic calculations (sweeps),
        # otherwise use the session's randomized rate for privacy
        if use_base_rate:
            rate = self._fee_rate if self._fee_rate is not None else 1.0
        else:
            rate = self._randomized_fee_rate if self._randomized_fee_rate is not None else 1.0

        return math.ceil(vsize * rate)

    async def _resolve_fee_rate(self) -> float:
        """
        Resolve the fee rate to use for the current CoinJoin.

        Priority:
        1. Manual fee_rate from config
        2. Backend fee estimation with fee_block_target
        3. Default 3-block estimation if backend supports it
        4. Fallback to 1 sat/vB

        The resolved fee rate is also checked against mempool minimum fee
        (if available) to ensure transactions are accepted.

        Returns:
            Fee rate in sat/vB (cached in self._fee_rate)

        Raises:
            ValueError: If fee_block_target specified with neutrino backend
        """
        # If already resolved, return cached value
        if self._fee_rate is not None:
            return self._fee_rate

        # Get mempool minimum fee (if available) as a floor
        mempool_min_fee: float | None = None
        try:
            mempool_min_fee = await self.backend.get_mempool_min_fee()
            if mempool_min_fee is not None:
                logger.debug(f"Mempool min fee: {mempool_min_fee:.2f} sat/vB")
        except Exception:
            # Backend may not support this method
            pass

        # 1. Manual fee rate takes priority
        if self.config.fee_rate is not None:
            self._fee_rate = self.config.fee_rate
            # Check against mempool min fee
            if mempool_min_fee is not None and self._fee_rate < mempool_min_fee:
                logger.warning(
                    f"Manual fee rate {self._fee_rate:.2f} sat/vB is below mempool min "
                    f"{mempool_min_fee:.2f} sat/vB, using mempool min"
                )
                self._fee_rate = mempool_min_fee
            logger.info(f"Using manual fee rate: {self._fee_rate:.2f} sat/vB")
            self._apply_fee_randomization()
            return self._fee_rate

        # 2. Block target specified - check backend capability
        if self.config.fee_block_target is not None:
            if not self.backend.can_estimate_fee():
                raise ValueError(
                    "Cannot use --block-target with neutrino backend. "
                    "Fee estimation requires a full node. "
                    "Use --fee-rate to specify a manual rate instead."
                )
            self._fee_rate = await self.backend.estimate_fee(self.config.fee_block_target)
            # Check against mempool min fee
            if mempool_min_fee is not None and self._fee_rate < mempool_min_fee:
                logger.info(
                    f"Estimated fee {self._fee_rate:.2f} sat/vB is below mempool min "
                    f"{mempool_min_fee:.2f} sat/vB, using mempool min"
                )
                self._fee_rate = mempool_min_fee
            logger.info(
                f"Fee estimation for {self.config.fee_block_target} blocks: "
                f"{self._fee_rate:.2f} sat/vB"
            )
            self._apply_fee_randomization()
            return self._fee_rate

        # 3. Default: 3-block estimation if backend supports it
        if self.backend.can_estimate_fee():
            default_target = 3
            self._fee_rate = await self.backend.estimate_fee(default_target)
            # Check against mempool min fee
            if mempool_min_fee is not None and self._fee_rate < mempool_min_fee:
                logger.info(
                    f"Estimated fee {self._fee_rate:.2f} sat/vB is below mempool min "
                    f"{mempool_min_fee:.2f} sat/vB, using mempool min"
                )
                self._fee_rate = mempool_min_fee
            logger.info(
                f"Fee estimation for {default_target} blocks (default): {self._fee_rate:.2f} sat/vB"
            )
            self._apply_fee_randomization()
            return self._fee_rate

        # 4. Neutrino backend without manual fee - fall back to 1.0 sat/vB
        fallback_rate = 1.0
        logger.warning(
            f"Fee estimation is not available with the neutrino backend and no --fee-rate "
            f"was specified. Falling back to {fallback_rate} sat/vB."
        )
        self._fee_rate = fallback_rate
        self._apply_fee_randomization()
        return self._fee_rate

    def _apply_fee_randomization(self) -> None:
        """Apply tx_fee_factor randomization to get the session's fee rate.

        This is called once per CoinJoin session to determine the randomized
        fee rate used for all fee calculations. The randomization provides
        privacy by varying the fee rate within the configured range.

        The randomized rate is stored in self._randomized_fee_rate and used
        by _estimate_tx_fee() for all calculations.
        """
        import random

        if self._fee_rate is None:
            return

        base_rate = self._fee_rate

        if self.config.tx_fee_factor > 0:
            # Randomize between base and base * (1 + factor)
            self._randomized_fee_rate = random.uniform(
                base_rate, base_rate * (1 + self.config.tx_fee_factor)
            )
            logger.debug(
                f"Fee rate randomized: base={base_rate:.2f}, "
                f"randomized={self._randomized_fee_rate:.2f} sat/vB "
                f"(factor={self.config.tx_fee_factor})"
            )
        else:
            self._randomized_fee_rate = base_rate

    def _get_taker_cj_output_index(self) -> int | None:
        """
        Find the index of the taker's CoinJoin output in the transaction.

        Uses tx_metadata["output_owners"] which tracks (owner, type) for each output.
        The taker's CJ output is marked as ("taker", "cj").

        Returns:
            Output index (vout) or None if not found
        """
        output_owners = self.tx_metadata.get("output_owners", [])
        for idx, (owner, out_type) in enumerate(output_owners):
            if owner == "taker" and out_type == "cj":
                return idx
        return None

    def _get_taker_change_output_index(self) -> int | None:
        """
        Find the index of the taker's change output in the transaction.

        Uses tx_metadata["output_owners"] which tracks (owner, type) for each output.
        The taker's change output is marked as ("taker", "change").

        Returns:
            Output index (vout) or None if not found
        """
        output_owners = self.tx_metadata.get("output_owners", [])
        for idx, (owner, out_type) in enumerate(output_owners):
            if owner == "taker" and out_type == "change":
                return idx
        return None

    async def _phase_collect_signatures(self) -> bool:
        """Send !tx and collect !sig responses from makers.

        The reference maker sends signatures in TRANSACTION INPUT ORDER, not in the
        order UTXOs were originally provided. We must match signatures to transaction
        inputs by verifying which UTXO each signature is valid for, not by index.
        """
        # Encode transaction as base64 (expected by maker after decryption)
        import base64

        tx_b64 = base64.b64encode(self.unsigned_tx).decode("ascii")

        # Record history BEFORE sending !tx to makers.
        # This ensures addresses are persisted before they're revealed in the transaction.
        # If we crash after sending !tx but before broadcast, the addresses won't be reused.
        try:
            total_maker_fees = sum(
                calculate_cj_fee(session.offer, self.cj_amount)
                for session in self.maker_sessions.values()
            )
            maker_nicks = list(self.maker_sessions.keys())

            history_entry = create_taker_history_entry(
                maker_nicks=maker_nicks,
                cj_amount=self.cj_amount,
                total_maker_fees=total_maker_fees,
                mining_fee=0,  # Will be updated after signing
                destination=self.cj_destination,
                change_address=self.taker_change_address,  # Empty string if no change needed
                source_mixdepth=self.tx_metadata.get("source_mixdepth", 0),
                selected_utxos=[(utxo.txid, utxo.vout) for utxo in self.selected_utxos],
                txid="",  # Will be updated after broadcast
                broadcast_method=self.config.tx_broadcast.value,
                network=self.config.network.value,
                failure_reason="Awaiting transaction",
            )
            append_history_entry(history_entry, data_dir=self.config.data_dir)
            logger.debug(
                f"Recorded pre-broadcast history entry for CJ to {self.cj_destination[:20]}..."
                + (" (no change)" if not self.taker_change_address else "")
            )
        except Exception as e:
            logger.warning(f"Failed to record pre-broadcast history: {e}")
            # Continue anyway - the CoinJoin can still proceed

        # Send ENCRYPTED !tx to each maker
        for nick, session in self.maker_sessions.items():
            if session.crypto is None:
                logger.error(f"No encryption session for {nick}")
                continue

            encrypted_tx = session.crypto.encrypt(tx_b64)
            await self.directory_client.send_privmsg(
                nick, "tx", encrypted_tx, log_routing=True, force_channel=session.comm_channel
            )

        # Build expected signature counts for early termination
        expected_counts = {
            nick: len(session.utxos) for nick, session in self.maker_sessions.items()
        }

        # Wait for all !sig responses at once
        timeout = self.config.maker_timeout_sec
        expected_nicks = list(self.maker_sessions.keys())
        signatures: dict[str, list[dict[str, Any]]] = {}

        responses = await self.directory_client.wait_for_responses(
            expected_nicks=expected_nicks,
            expected_command="!sig",
            timeout=timeout,
            expected_counts=expected_counts,
        )

        # Deserialize transaction for signature verification
        # We use verification-based matching: verify each signature against inputs
        # to find the correct match, rather than relying on ordering.
        try:
            tx = deserialize_transaction(self.unsigned_tx)
        except Exception as e:
            logger.error(f"Failed to deserialize transaction: {e}")
            return False

        # Build a map of input_index -> (txid_hex, vout)
        input_map: dict[int, tuple[str, int]] = {}
        for idx, tx_input in enumerate(tx.inputs):
            txid_hex = tx_input.txid_le[::-1].hex()
            input_map[idx] = (txid_hex, tx_input.vout)

        # Process responses
        for nick in list(self.maker_sessions.keys()):
            if nick in responses:
                try:
                    session = self.maker_sessions[nick]
                    if session.crypto is None:
                        logger.warning(f"No encryption session for {nick}")
                        del self.maker_sessions[nick]
                        continue

                    # Get all signature messages for this maker
                    response_data_list = responses[nick]["data"]
                    if not isinstance(response_data_list, list):
                        response_data_list = [response_data_list]

                    if not response_data_list:
                        logger.warning(f"Empty !sig response from {nick}")
                        del self.maker_sessions[nick]
                        continue

                    # Identify this maker's input indices in the transaction
                    maker_utxo_map = {(u["txid"], u["vout"]): u for u in session.utxos}
                    maker_input_indices: list[int] = []

                    for idx, (txid, vout) in input_map.items():
                        if (txid, vout) in maker_utxo_map:
                            maker_input_indices.append(idx)

                    if len(maker_input_indices) != len(session.utxos):
                        logger.warning(
                            f"UTXO count mismatch for {nick}: found {len(maker_input_indices)} "
                            f"inputs in tx, expected {len(session.utxos)}"
                        )
                        # Continue anyway, maybe some UTXOs were excluded (though shouldn't happen)

                    # Process signatures with verification
                    sig_infos: list[dict[str, Any]] = []
                    matched_indices: set[int] = set()

                    # Pre-check if any maker UTXOs are P2TR, so we can fetch
                    # prevout data needed for Taproot sighash verification.
                    has_p2tr_maker = any(
                        is_p2tr_address(u.get("address", "")) for u in session.utxos
                    )
                    all_prevouts_values: list[int] = []
                    all_prevouts_scripts: list[bytes] = []
                    if has_p2tr_maker:
                        for inp in tx.inputs:
                            inp_txid = inp.txid_le[::-1].hex()
                            prevout = await self.backend.get_utxo(inp_txid, inp.vout)
                            if prevout:
                                all_prevouts_values.append(prevout.value)
                                all_prevouts_scripts.append(bytes.fromhex(prevout.scriptpubkey))
                            else:
                                # Can't fetch prevout -- clear to disable verification
                                all_prevouts_values = []
                                all_prevouts_scripts = []
                                break

                    for sig_idx, response_data in enumerate(response_data_list):
                        parts = response_data.strip().split()
                        if not parts:
                            continue

                        encrypted_data = parts[0]
                        decrypted_sig = session.crypto.decrypt(encrypted_data)

                        # Decode the base64 signature data
                        padding_needed = (4 - len(decrypted_sig) % 4) % 4
                        padded_sig = decrypted_sig + "=" * padding_needed
                        sig_bytes = base64.b64decode(padded_sig)

                        # Try to verify against each of maker's inputs
                        matched_input_idx = None
                        matched_is_p2tr = False
                        matched_signature = b""
                        matched_pubkey = b""

                        for idx in maker_input_indices:
                            if idx in matched_indices:
                                continue

                            txid, vout = input_map[idx]
                            utxo = maker_utxo_map[(txid, vout)]
                            value = utxo["value"]
                            address = utxo.get("address", "")

                            is_p2tr = is_p2tr_address(address)

                            if is_p2tr:
                                # P2TR: sig_bytes = varint(sig_len) + sig (no pubkey)
                                sig_len, offset = decode_varint(sig_bytes, 0)
                                signature = sig_bytes[offset : offset + sig_len]

                                # Verify with Schnorr signature against the
                                # x-only pubkey from the P2TR scriptPubKey.
                                # We need all prevout data for the BIP341 sighash.
                                if all_prevouts_values and all_prevouts_scripts:
                                    spk = address_to_scriptpubkey(address)
                                    x_only_pubkey = spk[2:]  # Strip OP_1 PUSH32

                                    if verify_p2tr_signature(
                                        tx,
                                        idx,
                                        all_prevouts_values,
                                        all_prevouts_scripts,
                                        signature,
                                        x_only_pubkey,
                                    ):
                                        matched_input_idx = idx
                                        matched_is_p2tr = True
                                        matched_signature = signature
                                        break
                                else:
                                    # No prevout data available for verification,
                                    # accept signature without verification.
                                    # This can happen when none of our own inputs
                                    # are P2TR (so prevouts weren't fetched).
                                    logger.warning(
                                        "Cannot verify P2TR signature: prevout data not available"
                                    )
                                    matched_input_idx = idx
                                    matched_is_p2tr = True
                                    matched_signature = signature
                                    break
                            else:
                                # P2WPKH: [sig_len][sig][pub_len][pubkey]
                                sig_len = sig_bytes[0]
                                signature = sig_bytes[1 : 1 + sig_len]
                                pub_len = sig_bytes[1 + sig_len]
                                pubkey = sig_bytes[2 + sig_len : 2 + sig_len + pub_len]

                                script_code = create_p2wpkh_script_code(pubkey)
                                if verify_p2wpkh_signature(
                                    tx, idx, script_code, value, signature, pubkey
                                ):
                                    matched_input_idx = idx
                                    matched_signature = signature
                                    matched_pubkey = pubkey
                                    break

                        if matched_input_idx is not None:
                            matched_indices.add(matched_input_idx)
                            txid, vout = input_map[matched_input_idx]
                            if matched_is_p2tr:
                                witness = [matched_signature.hex()]
                            else:
                                witness = [matched_signature.hex(), matched_pubkey.hex()]

                            sig_infos.append({"txid": txid, "vout": vout, "witness": witness})
                            logger.debug(
                                f"Verified signature from {nick} matches input {matched_input_idx} "
                                f"({txid[:16]}...:{vout})"
                            )
                        else:
                            logger.warning(
                                f"Signature #{sig_idx + 1} from {nick} "
                                "did not verify against any input"
                            )

                    if len(sig_infos) != len(session.utxos):
                        logger.warning(
                            f"Signature count mismatch for {nick}: "
                            f"verified {len(sig_infos)}, expected {len(session.utxos)}"
                        )
                        del self.maker_sessions[nick]
                        continue

                    signatures[nick] = sig_infos
                    session.signature = {"signatures": sig_infos}
                    session.responded_sig = True
                    logger.debug(f"Processed {len(sig_infos)} verified signatures from {nick}")

                except Exception as e:
                    logger.warning(f"Invalid !sig response from {nick}: {e}")
                    del self.maker_sessions[nick]
            else:
                logger.warning(f"No !sig response from {nick}")
                del self.maker_sessions[nick]

        # Every maker whose inputs are in the transaction MUST provide valid
        # signatures. Unlike the filling phase where minimum_makers is relevant for
        # selecting counterparties, once the transaction is built with specific inputs,
        # ALL those inputs need signatures or the transaction is invalid.
        required_makers = {
            owner for owner in self.tx_metadata.get("input_owners", []) if owner != "taker"
        }
        signed_makers = set(signatures.keys())
        missing_makers = required_makers - signed_makers

        if missing_makers:
            logger.error(
                f"Missing signatures from {len(missing_makers)} maker(s) "
                f"whose inputs are in the transaction: {missing_makers}. "
                f"Cannot proceed - transaction would be invalid."
            )
            return False

        # Add signatures to transaction
        builder = CoinJoinTxBuilder(self.config.network.value)

        # Add taker's signatures
        taker_sigs = await self._sign_our_inputs()
        signatures["taker"] = taker_sigs

        self.final_tx = builder.add_signatures(
            self.unsigned_tx,
            signatures,
            self.tx_metadata,
        )

        logger.info(f"Signed tx: {len(self.final_tx)} bytes")
        return True

    async def _sign_our_inputs(self) -> list[dict[str, Any]]:
        """
        Sign taker's inputs in the transaction.

        Finds the correct input indices in the shuffled transaction by matching
        txid:vout from selected UTXOs, then signs each input.

        Returns:
            List of signature info dicts with txid, vout, signature, pubkey, witness
        """
        try:
            if not self.unsigned_tx:
                logger.error("No unsigned transaction to sign")
                return []

            if not self.selected_utxos:
                logger.error("No selected UTXOs to sign")
                return []

            tx = deserialize_transaction(self.unsigned_tx)
            signatures_info: list[dict[str, Any]] = []

            # Build a map of (txid, vout) -> input index for the transaction
            # Note: txid in tx.inputs is little-endian bytes, need to convert
            input_index_map: dict[tuple[str, int], int] = {}
            for idx, tx_input in enumerate(tx.inputs):
                # Convert little-endian txid bytes to big-endian hex string (RPC format)
                txid_hex = tx_input.txid_le[::-1].hex()
                input_index_map[(txid_hex, tx_input.vout)] = idx

            # For Taproot (BIP341), we need ALL prevouts to compute the sighash
            all_prevouts_values = []
            all_prevouts_scripts = []
            has_p2tr = any(is_p2tr_address(utxo.address) for utxo in self.selected_utxos)

            # Check if any maker inputs are taproot
            if not has_p2tr:
                for session in self.maker_sessions.values():
                    if any(is_p2tr_address(u.get("address", "")) for u in session.utxos):
                        has_p2tr = True
                        break

            if has_p2tr:
                logger.debug("Taproot inputs detected in CJ, fetching all prevouts for sighash")
                for inp in tx.inputs:
                    txid = inp.txid_le[::-1].hex()
                    prevout = await self.backend.get_utxo(txid, inp.vout)
                    if not prevout:
                        raise TransactionSigningError(
                            f"Could not fetch prevout for {txid}:{inp.vout}"
                        )
                    all_prevouts_values.append(prevout.value)
                    all_prevouts_scripts.append(bytes.fromhex(prevout.scriptpubkey))

            # Sign each of our UTXOs
            for utxo in self.selected_utxos:
                # Find the input index in the transaction
                utxo_key = (utxo.txid, utxo.vout)
                if utxo_key not in input_index_map:
                    logger.error(f"UTXO {utxo.txid}:{utxo.vout} not found in transaction inputs")
                    continue

                input_index = input_index_map[utxo_key]

                # Safety check: Fidelity bond (P2WSH) UTXOs should never be in CoinJoins
                if utxo.is_p2wsh:
                    raise TransactionSigningError(
                        f"Cannot sign P2WSH UTXO {utxo.txid}:{utxo.vout} in CoinJoin - "
                        f"fidelity bond UTXOs cannot be used in CoinJoins"
                    )

                # Get the key for this address
                key = self.wallet.get_key_for_address(utxo.address)
                if not key:
                    raise TransactionSigningError(f"Missing key for address {utxo.address}")

                priv_key = key.private_key

                # Check if it's P2TR
                if is_p2tr_address(utxo.address):
                    logger.debug(f"Signing taker P2TR input {input_index}")

                    import coincurve
                    from jmcore.bitcoin import taproot_tweak_privkey, taproot_tweak_pubkey

                    # Tweak private key for key-path spend
                    tweaked_priv_bytes = taproot_tweak_privkey(priv_key.secret)
                    tweaked_priv = coincurve.PrivateKey(tweaked_priv_bytes)

                    signature = sign_p2tr_input(
                        tx=tx,
                        input_index=input_index,
                        prevouts_values=all_prevouts_values,
                        prevouts_scripts=all_prevouts_scripts,
                        private_key=tweaked_priv,
                        sighash_type=0x00,  # SIGHASH_DEFAULT
                    )

                    witness = [signature]

                    # For info/logging
                    internal_pub = key.get_public_key_bytes(compressed=True)[1:]
                    _, pubkey_bytes = taproot_tweak_pubkey(internal_pub)
                else:
                    pubkey_bytes = key.get_public_key_bytes(compressed=True)

                    # Create script code and sign
                    script_code = create_p2wpkh_script_code(pubkey_bytes)
                    signature = sign_p2wpkh_input(
                        tx=tx,
                        input_index=input_index,
                        script_code=script_code,
                        value=utxo.value,
                        private_key=priv_key,
                    )

                    # Create witness stack
                    witness = create_witness_stack(signature, pubkey_bytes)

                signatures_info.append(
                    {
                        "txid": utxo.txid,
                        "vout": utxo.vout,
                        "signature": signature.hex(),
                        "pubkey": pubkey_bytes.hex(),
                        "witness": [item.hex() for item in witness],
                    }
                )

                logger.debug(f"Signed input {input_index} for UTXO {utxo.txid}:{utxo.vout}")

            logger.info(f"Signed {len(signatures_info)} taker inputs")
            return signatures_info

        except TransactionSigningError as e:
            logger.error(f"Signing error: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to sign transaction: {e}")
            return []

    def _log_manual_csv_entry(
        self, total_maker_fees: int, mining_fee: int, destination: str
    ) -> None:
        """
        Log a CSV entry that can be manually added for tracking unbroadcast transactions.

        When users decline to broadcast or want to broadcast manually, this logs
        the CSV entry they can add to coinjoin_history.csv for tracking.
        """
        try:
            txid = get_txid(self.final_tx.hex())
            maker_nicks = list(self.maker_sessions.keys())
            broadcast_method = self.config.tx_broadcast.value

            history_entry = create_taker_history_entry(
                maker_nicks=maker_nicks,
                cj_amount=self.cj_amount,
                total_maker_fees=total_maker_fees,
                mining_fee=mining_fee,
                destination=destination,
                change_address=self.taker_change_address,  # May be empty string if no change
                source_mixdepth=self.tx_metadata.get("source_mixdepth", 0),
                selected_utxos=[(utxo.txid, utxo.vout) for utxo in self.selected_utxos],
                txid=txid,
                broadcast_method=broadcast_method,
                network=self.config.network.value,
                failure_reason="User declined broadcast (manual broadcast pending)",
            )

            # Format as CSV line for manual addition
            from dataclasses import fields

            fieldnames = [f.name for f in fields(history_entry)]
            values = [str(getattr(history_entry, f)) for f in fieldnames]

            logger.info("-" * 70)
            logger.info("MANUAL CSV ENTRY - Add to coinjoin_history.csv if broadcasting manually:")
            logger.info(f"txid: {txid}")
            logger.info(f"CSV line: {','.join(values)}")
            logger.info("-" * 70)
        except Exception as e:
            logger.warning(f"Failed to generate manual CSV entry: {e}")

    async def _phase_broadcast(self) -> str:
        """
        Broadcast the signed transaction based on the configured policy.

        Privacy implications:
        - SELF: Taker broadcasts via own node. Links taker's IP to the transaction.
        - RANDOM_PEER: Random maker selected. Falls back to next maker on failure,
                       then self as last resort. Good balance of privacy and reliability.
        - MULTIPLE_PEERS: Broadcast to N random makers simultaneously (default 3).
                          Falls back to self if all fail. Recommended for Neutrino.
        - NOT_SELF: Try makers sequentially, never self. Maximum privacy.
                    WARNING: No fallback if all makers fail!

        Neutrino notes:
        - Cannot verify mempool transactions (only confirmed blocks)
        - Self-fallback allowed but verification skipped (trusts broadcast succeeded)

        Returns:
            Transaction ID if successful, empty string otherwise
        """
        import base64
        import random

        policy = self.config.tx_broadcast
        has_mempool = self.backend.has_mempool_access()
        logger.info(f"Broadcasting with policy: {policy.value}, mempool_access: {has_mempool}")

        # Encode transaction as base64 for !push message
        tx_b64 = base64.b64encode(self.final_tx).decode("ascii")

        # Calculate expected txid upfront (needed for Neutrino)
        builder = CoinJoinTxBuilder(self.config.bitcoin_network or self.config.network)
        expected_txid = builder.get_txid(self.final_tx)

        # Build list of broadcast candidates based on policy
        maker_nicks = list(self.maker_sessions.keys())

        if policy == BroadcastPolicy.SELF:
            # Always broadcast via own node
            return await self._broadcast_self()

        elif policy == BroadcastPolicy.RANDOM_PEER:
            # Try makers in random order, fall back to self as last resort
            if not maker_nicks:
                logger.warning("RANDOM_PEER policy but no makers available, using self")
                return await self._broadcast_self()

            random.shuffle(maker_nicks)

            for candidate in maker_nicks:
                txid = await self._broadcast_via_maker(candidate, tx_b64)
                if txid:
                    return txid

            # Last resort: self-broadcast
            logger.warning("All makers failed, falling back to self-broadcast")
            return await self._broadcast_self()

        elif policy == BroadcastPolicy.MULTIPLE_PEERS:
            # Broadcast to N random makers simultaneously, fall back to self
            if not maker_nicks:
                logger.warning("MULTIPLE_PEERS policy but no makers available, using self")
                return await self._broadcast_self()

            # Select N random makers (or all if less than N)
            peer_count = min(self.config.broadcast_peer_count, len(maker_nicks))
            selected_peers = random.sample(maker_nicks, peer_count)

            success_count = await self._broadcast_to_all_makers(selected_peers, tx_b64)

            if success_count > 0:
                if has_mempool:
                    logger.info(
                        f"Broadcast sent to {success_count}/{peer_count} makers "
                        "(MULTIPLE_PEERS policy)."
                    )
                else:
                    logger.info(
                        f"Broadcast sent to {success_count}/{peer_count} makers "
                        f"(MULTIPLE_PEERS policy). Transaction {expected_txid} will be "
                        "confirmed via block monitoring (Neutrino cannot verify mempool)"
                    )
                return expected_txid

            # All peers failed, fall back to self
            logger.warning(f"All {peer_count} peer broadcast attempts failed, falling back to self")
            return await self._broadcast_self()

        elif policy == BroadcastPolicy.NOT_SELF:
            # Only makers can broadcast - no self fallback
            if not maker_nicks:
                logger.error("NOT_SELF policy but no makers available")
                return ""

            # Try makers in random order with verification
            random.shuffle(maker_nicks)

            for maker_nick in maker_nicks:
                txid = await self._broadcast_via_maker(maker_nick, tx_b64)
                if txid:
                    return txid

            # No fallback for NOT_SELF - log the transaction for manual broadcast
            logger.error(
                "All maker broadcast attempts failed. "
                "Transaction hex (for manual broadcast): "
                f"{self.final_tx.hex()}"
            )
            return ""

        else:
            # Unknown policy, fallback to self
            logger.warning(f"Unknown broadcast policy {policy}, falling back to self")
            return await self._broadcast_self()

    async def _broadcast_to_all_makers(self, maker_nicks: list[str], tx_b64: str) -> int:
        """
        Send !push to all makers simultaneously for redundant broadcast.

        This is used by Neutrino takers who cannot verify mempool transactions.
        By broadcasting to all makers, we maximize the chance that at least one
        will successfully broadcast the transaction to the Bitcoin network.

        Privacy note: All makers already participated in the CoinJoin, so they
        all know the transaction. Sending !push to all of them doesn't reveal
        any new information.

        Args:
            maker_nicks: List of maker nicks to send !push to
            tx_b64: Base64-encoded signed transaction

        Returns:
            Number of makers that successfully received the !push message
        """

        async def send_push(nick: str) -> bool:
            """Send !push to a single maker, return True if no exception."""
            try:
                # Get the comm_channel from maker_sessions if available
                session = self.maker_sessions.get(nick)
                force_channel = session.comm_channel if session else None
                await self.directory_client.send_privmsg(
                    nick, "push", tx_b64, log_routing=True, force_channel=force_channel
                )
                return True
            except Exception as e:
                logger.warning(f"Failed to send !push to {nick}: {e}")
                return False

        # Send to all makers concurrently
        results = await asyncio.gather(*[send_push(nick) for nick in maker_nicks])

        success_count = sum(1 for r in results if r)
        logger.info(f"!push sent to {success_count}/{len(maker_nicks)} makers")

        return success_count

    async def _broadcast_self(self) -> str:
        """
        Broadcast transaction via our own backend.

        Handles the case where a maker may have already broadcast the transaction,
        which would cause our broadcast to fail with "inputs already spent" or
        "already in mempool". In these cases, we verify the transaction exists
        and treat it as success.
        """
        try:
            txid = await self.backend.broadcast_transaction(self.final_tx.hex())
            logger.info(f"Broadcast via self successful: {txid}")
            return txid
        except Exception as e:
            error_str = str(e).lower()

            # Check if error indicates the transaction was already broadcast
            # This can happen in multi-node setups where a maker broadcast to a
            # different node that hasn't synced with ours yet, but then syncs
            # before we try to self-broadcast.
            already_broadcast_indicators = [
                "bad-txns-inputs-missingorspent",  # Inputs already spent
                "txn-already-in-mempool",  # Already in our mempool
                "txn-mempool-conflict",  # Conflicts with mempool tx
                "missing-inputs",  # Alternative wording for spent inputs
            ]

            if any(ind in error_str for ind in already_broadcast_indicators):
                logger.info(
                    f"Self-broadcast rejected ({e}), checking if transaction "
                    "was already broadcast by a maker..."
                )

                # Calculate expected txid and verify the CoinJoin output exists
                builder = CoinJoinTxBuilder(self.config.bitcoin_network or self.config.network)
                expected_txid = builder.get_txid(self.final_tx)

                # Get taker's CJ output index for verification
                taker_cj_vout = self._get_taker_cj_output_index()
                if taker_cj_vout is None:
                    logger.warning("Could not find taker CJ output index for verification")
                    return ""

                # Get block height for verification hint
                try:
                    current_height = await self.backend.get_block_height()
                except Exception:
                    current_height = None

                # Verify the CoinJoin output exists (transaction was broadcast)
                cj_verified = await self.backend.verify_tx_output(
                    txid=expected_txid,
                    vout=taker_cj_vout,
                    address=self.cj_destination,
                    start_height=current_height,
                )

                if cj_verified:
                    logger.info(f"Transaction was already broadcast by maker: {expected_txid}")
                    return expected_txid

                # Not verified - could be a race condition or actual failure
                # Wait a bit and try once more (transaction might be propagating)
                await asyncio.sleep(3)
                cj_verified = await self.backend.verify_tx_output(
                    txid=expected_txid,
                    vout=taker_cj_vout,
                    address=self.cj_destination,
                    start_height=current_height,
                )

                if cj_verified:
                    logger.info(f"Transaction confirmed after propagation delay: {expected_txid}")
                    return expected_txid

                logger.warning(f"Self-broadcast failed and transaction not found: {e}")
                return ""

            logger.warning(f"Self-broadcast failed: {e}")
            return ""

    async def _broadcast_via_maker(self, maker_nick: str, tx_b64: str) -> str:
        """
        Request a maker to broadcast the transaction.

        Sends !push command and waits briefly for the transaction to appear.
        We don't expect a response from the maker - they broadcast unquestioningly.

        Verification is done using verify_tx_output() which works with all backends
        including Neutrino (which can't fetch arbitrary transactions by txid).
        We verify both CJ and change outputs for extra confidence.

        Args:
            maker_nick: The maker's nick to send the push request to
            tx_b64: Base64-encoded signed transaction

        Returns:
            Transaction ID if broadcast detected, empty string otherwise
        """
        try:
            start_time = time.time()
            logger.info(f"Requesting broadcast via maker: {maker_nick}")

            # Send !push to the maker (unencrypted, like reference implementation)
            # Use the same comm_channel as the rest of the session
            session = self.maker_sessions.get(maker_nick)
            force_channel = session.comm_channel if session else None
            await self.directory_client.send_privmsg(
                maker_nick, "push", tx_b64, log_routing=True, force_channel=force_channel
            )

            # Wait and check if the transaction was broadcast
            await asyncio.sleep(2)  # Give maker time to broadcast

            # Calculate the expected txid
            builder = CoinJoinTxBuilder(self.config.bitcoin_network or self.config.network)
            expected_txid = builder.get_txid(self.final_tx)

            # Get current block height for Neutrino optimization
            try:
                current_height = await self.backend.get_block_height()
            except Exception as e:
                logger.debug(f"Could not get block height: {e}, proceeding without hint")
                current_height = None

            # Get taker's CJ output index for verification
            taker_cj_vout = self._get_taker_cj_output_index()
            if taker_cj_vout is None:
                logger.warning("Could not find taker CJ output index for verification")
                # Can't verify without output index - treat as unverified failure
                return ""

            # Also get change output for additional verification
            taker_change_vout = self._get_taker_change_output_index()

            # Verify the transaction was broadcast by checking our CJ output exists
            # This works with all backends including Neutrino (uses address-based lookup)
            verify_start = time.time()
            cj_verified = await self.backend.verify_tx_output(
                txid=expected_txid,
                vout=taker_cj_vout,
                address=self.cj_destination,
                start_height=current_height,
            )
            verify_time = time.time() - verify_start

            # Also verify change output if it exists (extra confidence)
            change_verified = True  # Default to True if no change output
            if taker_change_vout is not None and self.taker_change_address:
                change_verify_start = time.time()
                change_verified = await self.backend.verify_tx_output(
                    txid=expected_txid,
                    vout=taker_change_vout,
                    address=self.taker_change_address,
                    start_height=current_height,
                )
                change_verify_time = time.time() - change_verify_start
                logger.debug(
                    f"Change output verification: {change_verified} "
                    f"(took {change_verify_time:.2f}s)"
                )

            if cj_verified and change_verified:
                total_time = time.time() - start_time
                logger.info(
                    f"Transaction broadcast via {maker_nick} confirmed: {expected_txid} "
                    f"(CJ verify: {verify_time:.2f}s, total: {total_time:.2f}s)"
                )
                return expected_txid

            # Wait longer and try once more
            await asyncio.sleep(self.config.broadcast_timeout_sec - 2)

            verify_start = time.time()
            cj_verified = await self.backend.verify_tx_output(
                txid=expected_txid,
                vout=taker_cj_vout,
                address=self.cj_destination,
                start_height=current_height,
            )
            verify_time = time.time() - verify_start

            # Verify change output again if it exists
            if taker_change_vout is not None and self.taker_change_address:
                change_verified = await self.backend.verify_tx_output(
                    txid=expected_txid,
                    vout=taker_change_vout,
                    address=self.taker_change_address,
                    start_height=current_height,
                )

            if cj_verified and change_verified:
                total_time = time.time() - start_time
                logger.info(
                    f"Transaction broadcast via {maker_nick} confirmed: {expected_txid} "
                    f"(CJ verify: {verify_time:.2f}s, total: {total_time:.2f}s)"
                )
                return expected_txid

            # Could not verify broadcast
            total_time = time.time() - start_time
            logger.debug(
                f"Could not confirm broadcast via {maker_nick} - "
                f"CJ output {expected_txid}:{taker_cj_vout} verified={cj_verified}, "
                f"change output verified={change_verified} (took {total_time:.2f}s)"
            )
            return ""

        except Exception as e:
            logger.warning(f"Broadcast via maker {maker_nick} failed: {e}")
            return ""

    async def run_schedule(self, schedule: Schedule) -> bool:
        """
        Run a tumbler-style schedule of CoinJoins.

        Args:
            schedule: Schedule with multiple CoinJoin entries

        Returns:
            True if all entries completed successfully
        """
        self.schedule = schedule

        while not schedule.is_complete():
            entry = schedule.current_entry()
            if not entry:
                break

            logger.info(
                f"Running schedule entry {schedule.current_index + 1}/{len(schedule.entries)}"
            )

            # Calculate actual amount
            if entry.amount_fraction is not None:
                # Fraction of balance
                balance = await self.wallet.get_balance(entry.mixdepth)
                amount = int(balance * entry.amount_fraction)
            else:
                assert entry.amount is not None
                amount = entry.amount

            # Execute CoinJoin
            txid = await self.do_coinjoin(
                amount=amount,
                destination=entry.destination,
                mixdepth=entry.mixdepth,
                counterparty_count=entry.counterparty_count,
            )

            if not txid:
                logger.error(f"Schedule entry {schedule.current_index + 1} failed")
                return False

            # Advance schedule
            schedule.advance()

            # Wait between CoinJoins
            if entry.wait_time > 0 and not schedule.is_complete():
                logger.info(f"Waiting {entry.wait_time}s before next CoinJoin...")
                await asyncio.sleep(entry.wait_time)

        logger.info("Schedule complete!")
        return True
