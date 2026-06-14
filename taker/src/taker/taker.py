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
from typing import Any

from jmcore.bitcoin import calculate_tx_vsize, get_address_type
from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.btc_script import derive_bond_address
from jmcore.commitment_blacklist import set_blacklist_path
from jmcore.crypto import NickIdentity
from jmcore.encryption import CryptoSession
from jmcore.notifications import get_notifier
from jmcore.paths import read_nick_state
from jmcore.protocol import FEATURE_NEUTRINO_COMPAT, JM_VERSION
from jmwallet.backends.base import BlockchainBackend, BondVerificationRequest
from jmwallet.history import (
    update_taker_awaiting_transaction_broadcast,
)
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import (
    deserialize_transaction,
)
from loguru import logger

from taker.coinjoin_session import CoinJoinSession
from taker.config import Schedule, TakerConfig, resolve_counterparty_count
from taker.eligibility import (
    classify_utxos,
    podle_threshold_met,
    selectable_for_interactive,
)
from taker.models import MakerSession, PhaseResult, TakerState
from taker.monitoring import TakerMonitoringMixin
from taker.multi_directory import MultiDirectoryClient
from taker.orderbook import OrderbookManager, calculate_cj_fee
from taker.podle_manager import PoDLEManager

# Backward-compatible re-exports: many tests and modules import these from taker.taker
__all__ = [
    "MultiDirectoryClient",
    "TakerState",
    "MakerSession",
    "PhaseResult",
    "Taker",
    "warn_if_destination_script_mismatch",
]


# JM-NG wallets are uniformly wpkh descriptors, so any non-p2wpkh destination
# mixes script types in the CoinJoin output and acts as a fingerprint linking
# the taker output back to its inputs.
_WALLET_OUTPUT_SCRIPT_TYPE = "p2wpkh"


def _append_confirmation_hint(message: str, taker_utxo_age: int) -> str:
    """Append the standard ``taker_utxo_age`` guidance to a selection error.

    Used so insufficient-funds errors from coin selection consistently tell the
    user that CoinJoin inputs need confirmations and how to relax the setting.
    """
    return (
        f"{message}. CoinJoin requires UTXOs with at least "
        f"{taker_utxo_age} confirmation(s) (taker_utxo_age setting). "
        f"Wait for more confirmations or lower taker_utxo_age in your config."
    )


def warn_if_destination_script_mismatch(destination: str) -> str | None:
    """
    Emit a warning when the destination address does not match the wallet's
    native script type. Returns the detected destination type on mismatch,
    None otherwise (matched type, or unparseable address - the canonical
    validation error is produced later in the pipeline).

    See issue #113.
    """
    try:
        dest_type = get_address_type(destination)
    except ValueError:
        return None
    if dest_type == _WALLET_OUTPUT_SCRIPT_TYPE:
        return None
    logger.warning(
        f"Destination address {destination} is {dest_type} but wallet is "
        f"{_WALLET_OUTPUT_SCRIPT_TYPE} (native segwit). Mixing script types "
        "in CoinJoin outputs fingerprints your output and reduces the "
        "effective anonymity set. Consider sending to a bech32 "
        "(bc1q.../tb1q...) address instead."
    )
    return dest_type


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

        # Per-CoinJoin state and protocol phases live in a dedicated
        # ``CoinJoinSession``. ``Taker`` owns persistent infrastructure
        # (wallet, backend, config, directory client, orderbook manager,
        # PoDLE manager, schedule) and delegates the protocol phases to the
        # session, which is reset at the start of each ``do_coinjoin`` call.
        self._session = CoinJoinSession()
        self._session.attach(self)

        # Schedule for tumbler-style operations
        self.schedule: Schedule | None = None

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
            # Use standard sync (BIP157/158 for neutrino, mempool API, etc.)
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

    def release_input_locks(self) -> None:
        """Release persisted CoinJoin locks held on this round's taker inputs.

        Call on failure so the inputs become selectable again immediately
        instead of waiting for the lock TTL. On success the inputs are spent,
        so locks are left to auto-expire. Safe to call when nothing is reserved.
        """
        if self._session and self._session.reserved_inputs:
            try:
                self.wallet.release_coinjoin_inputs(self._session.reserved_inputs)
            except Exception as e:  # pragma: no cover - best-effort cleanup
                logger.debug(f"Failed to release taker input locks: {e}")
            self._session.reserved_inputs = set()

    @property
    def last_failure_reason(self) -> str | None:
        """Reason the most recent ``do_coinjoin`` call failed (or ``None``).

        Forwarded from the per-round :class:`CoinJoinSession` so external
        consumers (e.g. the tumbler runner) can surface why a round did not
        broadcast without reaching into private session state.
        """
        return self._session.last_failure_reason

    @property
    def last_used_nicks(self) -> set[str]:
        """Maker nicks used by the most recent ``do_coinjoin`` call."""
        return self._session.last_used_nicks

    async def check_utxo_eligibility(self, amount: int, mixdepth: int) -> str | None:
        """Validate that ``mixdepth`` can fund a CoinJoin of ``amount``.

        Runs the same eligibility filters used later in :meth:`do_coinjoin`
        (confirmations, frozen, fidelity bonds, in-flight locks, the mixdepth-0
        merge restriction and the PoDLE size requirement) *before* any network
        operation, so an ineligible wallet fails fast instead of after a long
        directory/orderbook/bond cycle (issue #528).

        Args:
            amount: Target amount in satoshis (``0`` for sweep).
            mixdepth: Source mixdepth.

        Returns:
            ``None`` when a CoinJoin can proceed, otherwise a human-readable
            reason describing why it cannot.
        """
        utxos = await self.wallet.get_utxos(mixdepth)
        min_conf = self.config.taker_utxo_age

        # Interactive selection follows different rules (the user may pick
        # unlocked fidelity bonds and is not bound to auto-selection), so only
        # require that *something* is selectable here.
        if self.config.select_utxos:
            if not selectable_for_interactive(utxos, min_conf):
                breakdown = classify_utxos(utxos, mixdepth, min_conf)
                return breakdown.no_eligible_reason()
            return None

        reserved = self.wallet.get_locked_input_outpoints()
        breakdown = classify_utxos(utxos, mixdepth, min_conf, reserved_outpoints=reserved)

        if not breakdown.eligible:
            return breakdown.no_eligible_reason()

        # Sweep spends every eligible UTXO; a non-empty pool is sufficient.
        is_sweep = amount == 0
        if is_sweep:
            return None

        # PoDLE necessary condition: a commitment needs a UTXO worth at least
        # ``taker_utxo_amtpercent`` of the amount. Without one the round always
        # fails at commitment generation, so reject early with a clear message.
        if not podle_threshold_met(
            breakdown.eligible, amount, min_conf, self.config.taker_utxo_amtpercent
        ):
            min_value = int(amount * self.config.taker_utxo_amtpercent / 100)
            return (
                f"No eligible UTXO in mixdepth {mixdepth} is large enough for the "
                f"PoDLE commitment: need at least {min_value:,} sats "
                f"({self.config.taker_utxo_amtpercent}% of {amount:,} sats, "
                f"taker_utxo_amtpercent). Use a larger UTXO or lower the amount."
            )

        # Amount coverage: dry-run the exact selection used later so the verdict
        # matches reality (including the mixdepth-0 merge restriction).
        try:
            self.wallet.select_utxos(
                mixdepth,
                amount,
                min_conf,
                exclude=reserved,
            )
        except ValueError as exc:
            return _append_confirmation_hint(str(exc), min_conf)

        return None

    async def stop(self, *, close_wallet: bool = True) -> None:
        """Stop the taker and close connections.

        Args:
            close_wallet: If ``True`` (the default), also close the wallet's
                backend connection. Pass ``False`` when the wallet is shared
                with another component (e.g. a jmwalletd tumbler runner that
                will reuse the same :class:`~jmwallet.wallet.service.WalletService`
                instance across multiple taker phases) to avoid tearing down a
                still-in-use wallet.
        """
        logger.info("Stopping taker...")
        self.running = False

        # Cancel all background tasks
        for task in self._background_tasks:
            task.cancel()

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        await self.directory_client.close_all()
        if close_wallet:
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
        exclude_nicks: set[str] | None = None,
    ) -> str | None:
        """
        Execute a single CoinJoin transaction.

        Args:
            amount: Amount in satoshis (0 for sweep)
            destination: Destination address ("INTERNAL" for next mixdepth)
            mixdepth: Source mixdepth
            counterparty_count: Number of makers (default from config)
            exclude_nicks: Additional maker nicks to exclude from selection
                (on top of ``orderbook_manager.ignored_makers`` and
                ``own_wallet_nicks``). Tumbler uses this to prevent the
                same maker from re-appearing across consecutive plan phases.

        Returns:
            Transaction ID if successful, None otherwise
        """
        try:
            # Reset per-call state so callers reading ``last_used_nicks`` after
            # a failure don't pick up nicks from a previous successful round.
            self._session.last_used_nicks = set()
            # When the caller does not pin a counterparty count, fall back to
            # the configured value (which may itself be ``None`` to request a
            # random draw from the upstream-aligned [8, 10] range).
            self._session.last_failure_reason = None

            # Re-read maker nick state on every coinjoin attempt.  The maker
            # may have been started *after* this Taker was constructed (common
            # in tumbler runs), so the nick read at __init__ time would be
            # stale.  Refreshing here ensures the hard exclusion is always
            # current regardless of startup order.
            current_maker_nick = read_nick_state(self.config.data_dir, "maker")
            if current_maker_nick:
                if current_maker_nick not in self.orderbook_manager.own_wallet_nicks:
                    logger.info(
                        f"Self-CoinJoin protection: adding maker nick {current_maker_nick} "
                        "to exclusion set (detected after taker init)"
                    )
                self.orderbook_manager.own_wallet_nicks.add(current_maker_nick)

            requested = (
                counterparty_count
                if counterparty_count is not None
                else self.config.counterparty_count
            )
            n_makers = resolve_counterparty_count(requested)

            # Pre-flight: reject ineligible UTXOs before any orderbook/bond work
            # so the user is not kept waiting on a doomed round (issue #528).
            # Callers that already validate (the CLI) will simply re-confirm a
            # passing verdict; jmwalletd, run_schedule and the tumbler rely on
            # this check since they go straight to do_coinjoin.
            eligibility_reason = await self.check_utxo_eligibility(amount, mixdepth)
            if eligibility_reason is not None:
                logger.error(eligibility_reason)
                self._session.last_failure_reason = eligibility_reason
                self.state = TakerState.FAILED
                return None

            # Determine destination address
            if destination == "INTERNAL":
                dest_mixdepth = (mixdepth + 1) % self.wallet.mixdepth_count
                # Use internal chain (/1) for CoinJoin outputs, not external (/0)
                # This matches the reference implementation behavior where all JM-generated
                # addresses (CJ outputs and change) use the internal branch
                dest_index = self.wallet.get_next_address_index(dest_mixdepth, 1)
                destination = self.wallet.get_change_address(dest_mixdepth, dest_index)
                logger.info(f"Using internal address: {destination}")
            else:
                # Warn when the user-supplied destination does not match the
                # wallet's native script type (#113).
                warn_if_destination_script_mismatch(destination)

            # Resolve fee rate early (before any fee estimation calls)
            try:
                await self._session._resolve_fee_rate()
            except ValueError as e:
                logger.error(str(e))
                self._session.last_failure_reason = str(e)
                self.state = TakerState.FAILED
                return None

            # Track if this is a sweep (no change) transaction
            self._session.is_sweep = amount == 0

            # Select UTXOs from wallet BEFORE fetching orderbook to avoid wasting user's time
            logger.info(f"Selecting UTXOs from mixdepth {mixdepth}...")

            manually_selected_utxos = await self._maybe_select_utxos_interactively(
                amount=amount,
                mixdepth=mixdepth,
            )
            if self.config.select_utxos and self.state in (TakerState.CANCELLED, TakerState.FAILED):
                return None

            # Now fetch orderbook after UTXO selection is done
            self.state = TakerState.FETCHING_ORDERBOOK
            logger.info("Fetching orderbook...")
            offers = await self.directory_client.fetch_orderbook(
                max_wait=self.config.order_wait_time,
                min_wait=self.config.orderbook_min_wait,
                quiet_period=self.config.orderbook_quiet_period,
            )

            # Determine required features for maker selection.
            # Neutrino takers require makers that support extended UTXO metadata
            # (scriptPubKey + blockheight) via the neutrino_compat feature.
            required_features: set[str] | None = None
            if self.backend.requires_neutrino_metadata():
                required_features = {FEATURE_NEUTRINO_COMPAT}

            # Early compatibility pre-check for neutrino takers: count how many offers
            # are from makers known to support neutrino_compat (via peerlist_features or
            # the deprecated !neutrino flag). This lets us fail fast before the expensive
            # fidelity bond verification, which can take 20+ minutes on neutrino backends.
            #
            # Feature detection comes from two sources:
            # 1. peerlist_features: directories that support it report per-peer features
            # 2. !neutrino flag in offers (deprecated but still parsed)
            #
            # Offers with empty features dicts (unknown status) are NOT rejected here --
            # they pass through and will be verified during _phase_auth(). Only offers
            # where we KNOW the maker lacks the feature are filtered out.
            if required_features:
                known_compatible = sum(
                    1
                    for o in offers
                    if o.features.get(FEATURE_NEUTRINO_COMPAT) or o.neutrino_compat
                )
                known_incompatible = sum(
                    1
                    for o in offers
                    if o.features
                    and not o.features.get(FEATURE_NEUTRINO_COMPAT)
                    and not o.neutrino_compat
                )
                unknown = len(offers) - known_compatible - known_incompatible
                logger.info(
                    f"Neutrino compatibility pre-check: {known_compatible} compatible, "
                    f"{known_incompatible} incompatible, {unknown} unknown "
                    f"(from {len(offers)} total offers)"
                )

                # If even the most optimistic count (compatible + unknown) can't meet
                # the requirement, fail immediately before bond verification.
                if known_compatible + unknown < n_makers:
                    reason = (
                        f"Not enough potentially compatible makers for neutrino taker: "
                        f"need {n_makers}, but only {known_compatible} known compatible + "
                        f"{unknown} unknown = {known_compatible + unknown} possible. "
                        f"{known_incompatible} offers filtered as incompatible (no "
                        f"neutrino_compat). Bond verification skipped."
                    )
                    logger.error(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.FAILED
                    return None

                if known_compatible < n_makers and unknown > 0:
                    logger.warning(
                        f"Only {known_compatible} offers confirmed neutrino_compat, "
                        f"need {n_makers}. {unknown} offers have unknown feature status "
                        f"and will be checked during handshake. Not all directory servers "
                        f"support peerlist_features."
                    )

            # Verify and calculate fidelity bond values
            await self._update_offers_with_bond_values(offers)

            self.orderbook_manager.update_offers(offers)

            if len(offers) < n_makers:
                reason = f"Not enough offers: need {n_makers}, found {len(offers)}"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None

            if required_features:
                logger.info(
                    "Neutrino backend: requiring neutrino_compat in offer filtering, "
                    "will also negotiate during handshake"
                )

            self.state = TakerState.SELECTING_MAKERS

            if self._session.is_sweep:
                # SWEEP MODE: Select ALL UTXOs and calculate exact cj_amount for zero change
                logger.info("Sweep mode: selecting UTXOs from mixdepth")

                # Use manually selected UTXOs if available, otherwise get all UTXOs
                if manually_selected_utxos:
                    self._session.preselected_utxos = manually_selected_utxos
                    logger.info(
                        f"Sweep using {len(manually_selected_utxos)} manually selected UTXOs "
                        f"(--select-utxos was used)"
                    )
                else:
                    # Get ALL UTXOs from the mixdepth (default sweep behavior)
                    self._session.preselected_utxos = self.wallet.get_all_utxos(
                        mixdepth, self.config.taker_utxo_age
                    )
                    logger.info(
                        f"Sweep using all {len(self._session.preselected_utxos)} UTXOs "
                        f"from mixdepth (no --select-utxos)"
                    )

                if not self._session.preselected_utxos:
                    reason = f"No eligible UTXOs in mixdepth {mixdepth}"
                    logger.error(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.FAILED
                    return None

                total_input_value = sum(u.value for u in self._session.preselected_utxos)
                logger.info(
                    f"Sweep: {len(self._session.preselected_utxos)} UTXOs, "
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
                    len(self._session.preselected_utxos)
                    + n_makers * maker_inputs_per_maker
                    + maker_inputs_buffer
                )
                # CJ outputs + maker changes (no taker change in sweep!)
                estimated_outputs = 1 + n_makers + n_makers
                # For sweeps, use base rate for deterministic budget calculation.
                # The cj_amount is calculated based on this budget, so it must match
                # exactly at build time. Using randomized rate would cause residual fees.
                estimated_tx_fee = self._session._estimate_tx_fee(
                    estimated_inputs, estimated_outputs, use_base_rate=True
                )

                # Store the tx fee budget for use at build time.
                # This is critical: the cj_amount is calculated based on this budget,
                # so we MUST use this same value at build time to avoid residual fees.
                self._session._sweep_tx_fee_budget = estimated_tx_fee

                # Use sweep order selection - this calculates exact cj_amount for zero change
                selected_offers, self._session.cj_amount, total_fee = (
                    self.orderbook_manager.select_makers_for_sweep(
                        total_input_value=total_input_value,
                        my_txfee=estimated_tx_fee,
                        n=n_makers,
                        required_features=required_features,
                        exclude_nicks=exclude_nicks,
                    )
                )

                if len(selected_offers) < self.config.minimum_makers:
                    reason = f"Not enough makers for sweep: {len(selected_offers)}"
                    logger.error(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.FAILED
                    return None

                logger.info(
                    f"Sweep: cj_amount={self._session.cj_amount:,} sats calculated for zero change"
                )
                # Record initial counterparties so callers (e.g. the tumbler)
                # can avoid reusing them in the next round, even if a
                # replacement maker is later swapped in.
                self._session.last_used_nicks = set(selected_offers.keys())

            else:
                # NORMAL MODE: Select minimum UTXOs needed
                self._session.cj_amount = amount
                logger.info(f"Selecting {n_makers} makers for {self._session.cj_amount:,} sats...")

                selected_offers, total_fee = self.orderbook_manager.select_makers(
                    cj_amount=self._session.cj_amount,
                    n=n_makers,
                    required_features=required_features,
                    exclude_nicks=exclude_nicks,
                )

                if len(selected_offers) < self.config.minimum_makers:
                    reason = f"Not enough makers selected: {len(selected_offers)}"
                    logger.error(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.FAILED
                    return None

                # Record initial counterparties so callers (e.g. the tumbler)
                # can avoid reusing them in the next round, even if a
                # replacement maker is later swapped in.
                self._session.last_used_nicks = set(selected_offers.keys())

                # Pre-select UTXOs for CoinJoin, then generate PoDLE from one of them
                # This ensures the PoDLE UTXO is one we'll actually use in the transaction
                logger.info("Selecting UTXOs and generating PoDLE commitment...")

                # Use manually selected UTXOs if available
                if manually_selected_utxos:
                    self._session.preselected_utxos = manually_selected_utxos
                    logger.info(
                        f"Using {len(manually_selected_utxos)} manually selected UTXOs "
                        f"(total: {sum(u.value for u in manually_selected_utxos):,} sats)"
                    )
                else:
                    # Estimate required amount (conservative estimate for UTXO pre-selection)
                    # We'll refine this in _phase_build_tx once we have exact maker UTXOs
                    estimated_inputs = 2 + len(selected_offers) * 2  # Rough estimate
                    estimated_outputs = 2 + len(selected_offers) * 2
                    estimated_tx_fee = self._session._estimate_tx_fee(
                        estimated_inputs, estimated_outputs
                    )
                    estimated_required = self._session.cj_amount + total_fee + estimated_tx_fee

                    # Pre-select UTXOs for the CoinJoin, skipping any inputs
                    # locked by another in-flight round (this or another process
                    # on the same wallet) so we don't build a conflicting tx.
                    locked_inputs = self.wallet.get_locked_input_outpoints()
                    try:
                        self._session.preselected_utxos = self.wallet.select_utxos(
                            mixdepth,
                            estimated_required,
                            self.config.taker_utxo_age,
                            exclude=locked_inputs,
                        )
                        preselected = self._session.preselected_utxos
                        logger.info(
                            f"Pre-selected {len(preselected)} UTXOs for CoinJoin "
                            f"(total: {sum(u.value for u in preselected):,} sats)"
                        )
                    except ValueError as e:
                        reason = _append_confirmation_hint(str(e), self.config.taker_utxo_age)
                        logger.error(reason)
                        self._session.last_failure_reason = reason
                        self.state = TakerState.FAILED
                        return None

            # "Block first, then continue": persist a lock on our chosen inputs
            # before negotiating with makers, so a concurrent round on the same
            # wallet cannot pick the same UTXO and produce a conflicting
            # transaction. The lock auto-expires (and is released on failure),
            # so a crash never blocks these funds permanently.
            to_reserve = {(u.txid, u.vout) for u in self._session.preselected_utxos}
            if not self.wallet.reserve_coinjoin_inputs(to_reserve):
                reason = (
                    "Selected UTXOs are locked by another in-flight CoinJoin on "
                    "this wallet (avoid running concurrent rounds on one wallet)."
                )
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None
            self._session.reserved_inputs |= to_reserve

            # Initialize maker sessions - neutrino_compat will be detected during handshake
            # when we receive the !pubkey response with features field
            self._session.maker_sessions = {
                nick: MakerSession(nick=nick, offer=offer, supports_neutrino_compat=False)
                for nick, offer in selected_offers.items()
            }

            logger.info(
                f"Selected {len(self._session.maker_sessions)} makers, "
                f"total fee: {total_fee:,} sats"
            )

            # Log estimated transaction fee before prompting for confirmation
            # Conservative estimate: assume 1 input per maker + 20% buffer, rounded up
            import math

            estimated_maker_inputs = math.ceil(n_makers * 1.2)
            estimated_inputs = len(self._session.preselected_utxos) + estimated_maker_inputs
            # Outputs: 1 CJ output per participant + change outputs (assume all have change)
            estimated_outputs = (1 + n_makers) + (1 + n_makers)
            estimated_tx_fee = self._session._estimate_tx_fee(estimated_inputs, estimated_outputs)
            logger.info(
                f"Estimated transaction (mining) fee: {estimated_tx_fee:,} sats "
                f"(~{self._session._fee_rate:.2f} sat/vB for ~{estimated_inputs} inputs, "
                f"{estimated_outputs} outputs)"
            )

            # Prompt for confirmation after maker selection
            if hasattr(self, "confirmation_callback") and self.confirmation_callback:
                try:
                    # Build maker details for confirmation
                    maker_details = []
                    for nick, session in self._session.maker_sessions.items():
                        fee = session.offer.calculate_fee(self._session.cj_amount)
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
                        cj_amount=self._session.cj_amount,
                        total_fee=total_fee + estimated_tx_fee,
                        destination=destination,
                        mining_fee=estimated_tx_fee,
                        fee_rate=self._session._fee_rate,
                        stage="initial",
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
                return key.get_private_key_bytes()

            # Generate PoDLE from pre-selected UTXOs only
            # This ensures the commitment is from a UTXO that will be in the transaction
            self._session.podle_commitment = self.podle_manager.generate_fresh_commitment(
                wallet_utxos=self._session.preselected_utxos,  # Only from pre-selected UTXOs!
                cj_amount=self._session.cj_amount,
                private_key_getter=get_private_key,
                min_confirmations=self.config.taker_utxo_age,
                min_percent=self.config.taker_utxo_amtpercent,
                max_retries=self.config.taker_utxo_retries,
            )

            if not self._session.podle_commitment:
                reason = "Failed to generate PoDLE commitment"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None

            max_replacement_attempts = self.config.max_maker_replacement_attempts
            if not await self._run_fill_with_replacements(
                destination=destination,
                selected_offers=selected_offers,
                required_features=required_features,
                mixdepth=mixdepth,
                get_private_key=get_private_key,
                max_replacement_attempts=max_replacement_attempts,
            ):
                return None

            if not await self._run_auth_with_replacements(
                required_features=required_features,
                max_replacement_attempts=max_replacement_attempts,
            ):
                return None

            # Phase 3: Build transaction
            self.state = TakerState.BUILDING_TX
            logger.info("Phase 3: Building transaction...")

            tx_success = await self._session._phase_build_tx(
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

            sig_success = await self._session._phase_collect_signatures()
            if not sig_success:
                logger.error("Signature collection failed")
                self.state = TakerState.FAILED
                return None

            return await self._finalize_and_broadcast(destination)

        except Exception as e:
            logger.error(f"CoinJoin failed: {e}")
            # Fire-and-forget notification for failed CoinJoin
            phase = self.state.value if hasattr(self, "state") else ""
            amount = self._session.cj_amount
            asyncio.create_task(get_notifier().notify_coinjoin_failed(str(e), phase, amount))
            self.state = TakerState.FAILED
            return None

    async def _maybe_select_utxos_interactively(
        self, amount: int, mixdepth: int
    ) -> list[UTXOInfo] | None:
        if not self.config.select_utxos:
            logger.debug("Interactive UTXO selection not requested (--select-utxos not set)")
            return None

        from jmwallet.history import get_utxo_label
        from jmwallet.utxo_selector import select_utxos_interactive

        try:
            # Get ALL UTXOs including frozen ones for display in the
            # interactive selector. Frozen/locked UTXOs are shown but
            # rendered as unselectable ([-]) so the user sees the full
            # picture of their wallet.
            available_utxos = await self.wallet.get_utxos(mixdepth)
            # Also filter by minimum age (confirmations) -- but keep
            # frozen ones regardless so they're visible in the TUI.
            min_age = self.config.taker_utxo_age
            available_utxos = [u for u in available_utxos if u.confirmations >= min_age or u.frozen]
            if not available_utxos:
                reason = f"No UTXOs in mixdepth {mixdepth}"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None

            # Check that at least some UTXOs are selectable (not frozen/locked)
            selectable = [
                u
                for u in available_utxos
                if not u.frozen and not (u.is_fidelity_bond and u.is_locked)
            ]
            if not selectable:
                reason = (
                    f"No eligible UTXOs in mixdepth {mixdepth} "
                    f"(all {len(available_utxos)} UTXOs are frozen or locked)"
                )
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None

            # Populate labels for each UTXO based on history
            for utxo in available_utxos:
                utxo.label = get_utxo_label(
                    utxo.address,
                    self.config.data_dir,
                    wallet_fingerprint=self.wallet.wallet_fingerprint,
                )

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

        return manually_selected_utxos

    async def _run_auth_with_replacements(
        self, required_features: set[str] | None, max_replacement_attempts: int
    ) -> bool:
        self.state = TakerState.AUTHENTICATING
        logger.info("Phase 2: Sending !auth and receiving !ioauth...")

        auth_replacement_attempt = 0
        while True:
            auth_result = await self._session._phase_auth()

            if auth_result.success:
                return True

            for failed_nick in auth_result.failed_makers:
                self.orderbook_manager.add_ignored_maker(failed_nick)
                logger.debug(f"Added {failed_nick} to ignored makers (failed auth)")

            if (
                auth_result.needs_replacement
                and auth_replacement_attempt < max_replacement_attempts
            ):
                auth_replacement_attempt += 1
                needed = self.config.minimum_makers - len(self._session.maker_sessions)
                logger.info(
                    f"Attempting maker replacement in auth phase "
                    f"(attempt {auth_replacement_attempt}/{max_replacement_attempts}): "
                    f"need {needed} more makers"
                )

                current_session_nicks = set(self._session.maker_sessions.keys())
                hard_excludes = current_session_nicks | set(auth_result.failed_makers)
                replacement_offers, _ = self.orderbook_manager.select_makers(
                    cj_amount=self._session.cj_amount,
                    n=needed,
                    hard_exclude_nicks=hard_excludes,
                    required_features=required_features,
                )

                if len(replacement_offers) < needed:
                    logger.error(
                        f"Not enough replacement makers for auth phase: "
                        f"found {len(replacement_offers)}, need {needed}"
                    )
                    self.state = TakerState.FAILED
                    return False

                for nick, offer in replacement_offers.items():
                    self._session.maker_sessions[nick] = MakerSession(
                        nick=nick, offer=offer, supports_neutrino_compat=False
                    )
                    logger.info(f"Added replacement maker for auth: {nick}")

                logger.info("Running fill phase for replacement makers...")
                new_maker_nicks = list(replacement_offers.keys())

                if not self._session.podle_commitment or not self._session.crypto_session:
                    logger.error("Missing commitment or crypto session for replacement")
                    self.state = TakerState.FAILED
                    return False

                commitment_hex = self._session.podle_commitment.to_commitment_str()
                taker_pubkey = self._session.crypto_session.get_pubkey_hex()

                for nick in new_maker_nicks:
                    binding = self.directory_client.bind_session(nick)
                    session = self._session.maker_sessions[nick]
                    if binding is None:
                        logger.warning(
                            f"No communication channel available for replacement maker {nick}"
                        )
                        continue
                    session.comm_channel = binding.channel_id
                    if binding.is_direct:
                        logger.debug(f"Will use DIRECT connection for replacement maker {nick}")
                    else:
                        logger.debug(f"Will use {binding.channel_id} for replacement maker {nick}")

                for nick in new_maker_nicks:
                    session = self._session.maker_sessions[nick]
                    fill_data = (
                        f"{session.offer.oid} {self._session.cj_amount} "
                        f"{taker_pubkey} {commitment_hex}"
                    )
                    await self.directory_client.send_privmsg(
                        nick,
                        "fill",
                        fill_data,
                        log_routing=True,
                        force_channel=session.comm_channel,
                    )

                responses = await self.directory_client.wait_for_responses(
                    expected_nicks=new_maker_nicks,
                    expected_command="!pubkey",
                    timeout=self.config.maker_timeout_sec,
                )

                new_makers_ready = 0
                for nick in new_maker_nicks:
                    if nick in responses and not responses[nick].get("error"):
                        try:
                            response_data = responses[nick]["data"].strip()
                            parts = response_data.split()
                            if parts:
                                nacl_pubkey = parts[0]
                                self._session.maker_sessions[nick].pubkey = nacl_pubkey
                                self._session.maker_sessions[nick].responded_fill = True

                                crypto = CryptoSession.__new__(CryptoSession)
                                crypto.keypair = self._session.crypto_session.keypair
                                crypto.box = None
                                crypto.counterparty_pubkey = ""
                                crypto.setup_encryption(nacl_pubkey)
                                self._session.maker_sessions[nick].crypto = crypto
                                new_makers_ready += 1
                                logger.debug(f"Replacement maker {nick} ready")
                        except Exception as e:
                            logger.warning(f"Failed to process {nick}: {e}")
                            del self._session.maker_sessions[nick]
                    else:
                        logger.warning(f"Replacement maker {nick} didn't respond")
                        if nick in self._session.maker_sessions:
                            del self._session.maker_sessions[nick]

                if new_makers_ready == 0:
                    logger.error("No replacement makers responded to fill")
                    self.state = TakerState.FAILED
                    return False

                continue

            logger.error("Auth phase failed")
            self.state = TakerState.FAILED
            return False

    async def _run_fill_with_replacements(
        self,
        destination: str,
        selected_offers: dict[str, Any],
        required_features: set[str] | None,
        mixdepth: int,
        get_private_key: Any,
        max_replacement_attempts: int,
    ) -> bool:
        self.state = TakerState.FILLING
        logger.info("Phase 1: Sending !fill to makers...")
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
            logger.debug("Direct connections disabled - all messages relayed through directories")

        asyncio.create_task(
            get_notifier().notify_coinjoin_start(
                self._session.cj_amount, len(self._session.maker_sessions), destination
            )
        )

        max_podle_retries = self.config.taker_utxo_retries
        replacement_attempt = 0
        for podle_retry in range(max_podle_retries):
            session_size_before_fill = len(self._session.maker_sessions)
            fill_result = await self._session._phase_fill()
            if fill_result.success:
                return True

            if fill_result.blacklist_makers and self._session.podle_commitment is not None:
                commitment_hex = self._session.podle_commitment.commitment.commitment.hex()
                try:
                    from jmcore.commitment_blacklist import add_commitment

                    add_commitment(commitment_hex)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        f"Could not persist remotely-reported blacklisted commitment: {exc}"
                    )

            n_blacklisted = len(fill_result.blacklist_makers)
            majority_blacklist = (
                fill_result.blacklist_error
                and session_size_before_fill > 0
                and n_blacklisted * 2 >= session_size_before_fill
            )

            if fill_result.blacklist_error and not majority_blacklist:
                logger.warning(
                    f"Minority blacklist rejection from {fill_result.blacklist_makers} "
                    f"({n_blacklisted}/{session_size_before_fill}). Ignoring those makers "
                    "and trying replacement with the same commitment."
                )
                for failed_nick in fill_result.failed_makers:
                    self.orderbook_manager.add_ignored_maker(failed_nick)
                    logger.debug(f"Added {failed_nick} to ignored makers (minority blacklist)")
            elif fill_result.blacklist_error:
                logger.warning(
                    f"Majority blacklist rejection ({n_blacklisted}/{session_size_before_fill}) "
                    f"from {fill_result.blacklist_makers}. Rotating commitment and retrying."
                )
            elif fill_result.failed_makers:
                for failed_nick in fill_result.failed_makers:
                    self.orderbook_manager.add_ignored_maker(failed_nick)
                    logger.debug(f"Added {failed_nick} to ignored makers (failed fill)")

            if majority_blacklist:
                if podle_retry < max_podle_retries - 1:
                    logger.warning(
                        f"Commitment blacklisted, retrying with new NUMS index "
                        f"(attempt {podle_retry + 2}/{max_podle_retries})..."
                    )
                    new_commitment = self.podle_manager.generate_fresh_commitment(
                        wallet_utxos=self._session.preselected_utxos,
                        cj_amount=self._session.cj_amount,
                        private_key_getter=get_private_key,
                        min_confirmations=self.config.taker_utxo_age,
                        min_percent=self.config.taker_utxo_amtpercent,
                        max_retries=self.config.taker_utxo_retries,
                    )
                    if new_commitment is None:
                        added = self._session._expand_preselected_utxos_same_mixdepth(mixdepth)
                        if added > 0:
                            logger.info(
                                f"Preselected UTXOs exhausted for PoDLE; added {added} "
                                f"additional UTXO(s) from mixdepth {mixdepth}, which will "
                                "also be spent in the CoinJoin."
                            )
                            new_commitment = self.podle_manager.generate_fresh_commitment(
                                wallet_utxos=self._session.preselected_utxos,
                                cj_amount=self._session.cj_amount,
                                private_key_getter=get_private_key,
                                min_confirmations=self.config.taker_utxo_age,
                                min_percent=self.config.taker_utxo_amtpercent,
                                max_retries=self.config.taker_utxo_retries,
                            )
                    if new_commitment is None:
                        logger.error(
                            "No more PoDLE commitments available: all indices exhausted "
                            f"across all eligible UTXOs in mixdepth {mixdepth}"
                        )
                        self.state = TakerState.FAILED
                        return False

                    self._session.podle_commitment = new_commitment
                    self._session.maker_sessions = {
                        nick: MakerSession(nick=nick, offer=offer, supports_neutrino_compat=False)
                        for nick, offer in selected_offers.items()
                        if nick not in self.orderbook_manager.ignored_makers
                    }
                    continue

                logger.error(
                    f"Fill phase failed after {max_podle_retries} PoDLE commitment attempts"
                )
                self.state = TakerState.FAILED
                return False

            if fill_result.needs_replacement and replacement_attempt < max_replacement_attempts:
                replacement_attempt += 1
                needed = self.config.minimum_makers - len(self._session.maker_sessions)
                logger.info(
                    f"Attempting maker replacement (attempt {replacement_attempt}/"
                    f"{max_replacement_attempts}): need {needed} more makers"
                )

                current_session_nicks = set(self._session.maker_sessions.keys())
                hard_excludes = current_session_nicks | set(fill_result.failed_makers)
                replacement_offers, _ = self.orderbook_manager.select_makers(
                    cj_amount=self._session.cj_amount,
                    n=needed,
                    hard_exclude_nicks=hard_excludes,
                    required_features=required_features,
                )

                if len(replacement_offers) < needed:
                    logger.error(
                        "Not enough replacement makers available: "
                        f"found {len(replacement_offers)}, need {needed}"
                    )
                    self.state = TakerState.FAILED
                    return False

                for nick, offer in replacement_offers.items():
                    self._session.maker_sessions[nick] = MakerSession(
                        nick=nick, offer=offer, supports_neutrino_compat=False
                    )
                    logger.info(f"Added replacement maker: {nick}")
                selected_offers.update(replacement_offers)
                self._session.last_used_nicks.update(replacement_offers.keys())
                continue

            logger.error("Fill phase failed")
            self.state = TakerState.FAILED
            return False

        logger.error("Fill phase failed")
        self.state = TakerState.FAILED
        return False

    async def _finalize_and_broadcast(self, destination: str) -> str | None:
        # Final confirmation before broadcast
        num_taker_inputs = len(self._session.selected_utxos)
        num_maker_inputs = sum(len(s.utxos) for s in self._session.maker_sessions.values())
        total_inputs = num_taker_inputs + num_maker_inputs

        tx = deserialize_transaction(self._session.final_tx)
        total_outputs = len(tx.outputs)
        total_output_value = sum(out.value for out in tx.outputs)

        taker_input_value = sum(utxo.value for utxo in self._session.selected_utxos)
        maker_input_value = sum(
            utxo["value"]
            for session in self._session.maker_sessions.values()
            for utxo in session.utxos
        )
        total_input_value = taker_input_value + maker_input_value
        actual_mining_fee = total_input_value - total_output_value

        total_maker_fees = sum(
            calculate_cj_fee(session.offer, self._session.cj_amount)
            for session in self._session.maker_sessions.values()
        )
        total_cost = total_maker_fees + actual_mining_fee
        actual_vsize = calculate_tx_vsize(self._session.final_tx)
        actual_fee_rate = actual_mining_fee / actual_vsize if actual_vsize > 0 else 0.0

        logger.info("=" * 70)
        logger.info("FINAL TRANSACTION SUMMARY - Ready to broadcast")
        logger.info("=" * 70)
        logger.info(f"CoinJoin amount:      {self._session.cj_amount:,} sats")
        logger.info(f"Makers participating: {len(self._session.maker_sessions)}")
        logger.info(
            f"  Makers: {', '.join(nick[:10] + '...' for nick in self._session.maker_sessions)}"
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
        logger.info(
            f"Transaction size:     {actual_vsize} vbytes ({len(self._session.final_tx)} bytes)"
        )
        logger.info("-" * 70)
        logger.info("Transaction hex (for manual verification/broadcast):")
        logger.info(self._session.final_tx.hex())
        logger.info("=" * 70)

        if hasattr(self, "confirmation_callback") and self.confirmation_callback:
            try:
                maker_details = []
                for nick, session in self._session.maker_sessions.items():
                    fee = calculate_cj_fee(session.offer, self._session.cj_amount)
                    bond_value = session.offer.fidelity_bond_value
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
                    cj_amount=self._session.cj_amount,
                    total_fee=total_cost,
                    destination=destination,
                    mining_fee=actual_mining_fee,
                    fee_rate=actual_fee_rate,
                    stage="broadcast",
                )
                if not confirmed:
                    logger.warning("User declined final broadcast confirmation")
                    self._session._log_manual_csv_entry(
                        total_maker_fees, actual_mining_fee, destination
                    )
                    self.state = TakerState.FAILED
                    return None
            except Exception as e:
                logger.error(f"Final confirmation callback failed: {e}")
                self.state = TakerState.FAILED
                return None

        self.state = TakerState.BROADCASTING
        logger.info("Phase 5: Broadcasting transaction...")

        self._session.txid = await self._session._phase_broadcast()
        if not self._session.txid:
            logger.error("Broadcast failed")
            self.state = TakerState.FAILED
            return None

        self.state = TakerState.COMPLETE
        logger.info(f"CoinJoin COMPLETE! txid: {self._session.txid}")

        try:
            updated = update_taker_awaiting_transaction_broadcast(
                destination_address=self._session.cj_destination,
                change_address=self._session.taker_change_address,  # Empty string if no change
                txid=self._session.txid,
                mining_fee=actual_mining_fee,
                data_dir=self.config.data_dir,
                wallet_fingerprint=self.wallet.wallet_fingerprint,
            )
            if updated:
                logger.debug(
                    f"Updated history entry for CJ txid {self._session.txid[:16]}..., "
                    f"mining_fee={actual_mining_fee} sats"
                )
            else:
                logger.warning(
                    f"No matching 'Awaiting transaction' entry found for "
                    f"{self._session.cj_destination[:20]}... - history may be inconsistent"
                )

            await self._update_pending_transaction_now(
                self._session.txid, self._session.cj_destination
            )
        except Exception as e:
            logger.warning(f"Failed to update CoinJoin history: {e}")

        total_fees = total_maker_fees + actual_mining_fee
        asyncio.create_task(
            get_notifier().notify_coinjoin_complete(
                self._session.txid,
                self._session.cj_amount,
                len(self._session.maker_sessions),
                total_fees,
            )
        )

        return self._session.txid

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
