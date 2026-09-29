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
import inspect
import math
import secrets
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jmcore import experimental
from jmcore.bitcoin import calculate_tx_vsize, get_address_type
from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.btc_script import derive_bond_address
from jmcore.channel_ring_store import RingParticipantStore
from jmcore.commitment_blacklist import set_blacklist_path
from jmcore.credential_market import BondReference
from jmcore.crypto import NickIdentity
from jmcore.experimental import warn_experimental
from jmcore.fee_policy import fee_rate_meets_minimum
from jmcore.logging_context import coinjoin_id_from_commitment, coinjoin_log_context
from jmcore.market_faults import MarketFaultCache
from jmcore.models import Offer, offer_output_script_type, offer_types_for_family
from jmcore.notifications import get_notifier
from jmcore.paths import get_default_data_dir, get_nick_state_component, read_nick_state
from jmcore.protocol import FEATURE_NEUTRINO_COMPAT, JM_VERSION
from jmcore.tasks import spawn_task
from jmwallet.backends.base import BlockchainBackend, BondVerificationRequest
from jmwallet.history import (
    update_taker_awaiting_transaction_broadcast,
)
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import (
    deserialize_transaction,
)
from jmwallet.wallet.spend import resolve_input_utxos
from loguru import logger

from taker.channel_ring_coordinator_store import CoordinatorStore
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
from taker.orderbook import (
    OrderbookManager,
    calculate_cj_fee,
    maker_selection_keys,
    offer_supports_private_channel_ring,
)
from taker.podle import ExtendedPoDLECommitment
from taker.podle_manager import BondKey, ExternalPoDLEPreview, PoDLEManager

if TYPE_CHECKING:
    from taker.buyout import ChannelBuyout

# Backward-compatible re-exports: many tests and modules import these from taker.taker
__all__ = [
    "MultiDirectoryClient",
    "TakerState",
    "MakerSession",
    "PhaseResult",
    "Taker",
    "warn_if_destination_script_mismatch",
]


# A channel ring spends every participant residual into shared channel outputs,
# while a buyout needs its own escrow change output from the same round. One
# round cannot satisfy both accountings, so the combination is rejected before
# any maker selection, input reservation or signing happens.
RING_BUYOUT_CONFLICT = "Channel-ring mode cannot fund a private channel buyout in the same round"


# JM-NG wallets are uniformly wpkh descriptors, so any non-p2wpkh destination
# mixes script types in the CoinJoin output and acts as a fingerprint linking
# the taker output back to its inputs.
_WALLET_OUTPUT_SCRIPT_TYPE = "p2wpkh"
ConfirmationCallback = Callable[..., bool | Awaitable[bool]]


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


def _estimate_initial_tx_shape(
    num_taker_inputs: int,
    num_makers: int,
    *,
    is_sweep: bool,
    max_maker_utxos: int,
) -> tuple[int, int]:
    """Estimate the pre-negotiation transaction shape shown to the user."""
    if is_sweep:
        if max_maker_utxos <= 0:
            raise ValueError("Sweep fee budgeting requires a positive max_maker_utxos limit")
        maker_inputs = num_makers * max_maker_utxos
        num_outputs = 1 + num_makers * 2
    else:
        maker_inputs = math.ceil(num_makers * 1.2)
        num_outputs = 2 + num_makers * 2
    return num_taker_inputs + maker_inputs, num_outputs


def _initial_fee_confirmation_values(
    session: CoinJoinSession,
    num_taker_inputs: int,
    num_makers: int,
    max_maker_utxos: int,
) -> tuple[int, int, int, float]:
    """Return the transaction shape, fee, and rate used in initial confirmation."""
    estimated_inputs, estimated_outputs = _estimate_initial_tx_shape(
        num_taker_inputs,
        num_makers,
        is_sweep=session.is_sweep,
        max_maker_utxos=max_maker_utxos,
    )
    if session.is_sweep:
        estimated_tx_fee = session._sweep_tx_fee_budget
        estimated_fee_rate = session._fee_rate
    else:
        estimated_tx_fee = session._estimate_tx_fee(estimated_inputs, estimated_outputs)
        estimated_fee_rate = session._randomized_fee_rate
    return (
        estimated_inputs,
        estimated_outputs,
        estimated_tx_fee,
        estimated_fee_rate if estimated_fee_rate is not None else 1.0,
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


def verified_bond_key(offer: Offer, network: str) -> BondKey | None:
    """Return the complete bond identity an offer independently proved, if any.

    Only a bond that passed normal backend verification and carries the whole
    tuple (outpoint, public key, locktime) yields a key. An unverified bond, a
    different key on the same outpoint, or incomplete proof data yields
    ``None``, so nothing is ever matched on partial evidence.
    """
    if offer.fidelity_bond_verified is not True:
        return None
    data = offer.fidelity_bond_data
    if not isinstance(data, dict):
        return None
    txid = data.get("utxo_txid")
    vout = data.get("utxo_vout")
    pubkey = data.get("utxo_pub")
    locktime = data.get("locktime")
    if (
        not isinstance(txid, str)
        or type(vout) is not int
        or not isinstance(pubkey, str)
        or type(locktime) is not int
    ):
        return None
    return (network, txid, vout, pubkey, locktime)


def bond_reference_key(bond: BondReference) -> BondKey:
    """Return the same identity tuple for a signed market bond reference."""
    return (bond.network, bond.outpoint.txid, bond.outpoint.vout, bond.pubkey, bond.locktime)


def nicks_proving_bonds(offers: Iterable[Offer], bond_keys: set[BondKey], network: str) -> set[str]:
    """Return the nicks currently proving one of these exact bond identities."""
    if not bond_keys:
        return set()
    return {
        offer.counterparty
        for offer in offers
        if verified_bond_key(offer, network) in bond_keys  # None never matches a key
    }


class _RoundSellerSeparation:
    """Keep every credential seller used in a round out of that round's makers.

    Both directions are permanent for the round and nick-independent, because
    knowledge cannot be taken back and a nick is not an identity:

    * every maker bond the round selected or contacted (including makers that
      later failed, were replaced, or stopped advertising) blocks any
      credential sold by that bond, and
    * every seller bond the round committed to blocks any maker nick currently
      proving that bond, recomputed on each selection pass so a nick that only
      starts advertising the bond later is still excluded.
    """

    def __init__(self, taker: Taker) -> None:
        self._taker = taker

    @property
    def _network(self) -> str:
        configured = self._taker.config.bitcoin_network or self._taker.config.network
        return str(configured.value)

    def _seller_key(self, seller_bond: BondReference) -> BondKey | None:
        """Return the seller identity, or ``None`` when it is off this network."""
        key = bond_reference_key(seller_bond)
        return key if key[0] == self._network else None

    def record_offered_makers(self, offers: Iterable[Offer]) -> None:
        """Remember the bond identity of makers this round selected or contacted.

        Candidates are recorded before they are contacted, so a credential can
        never be revealed to a maker the round is about to fill.
        """
        keys = {
            key
            for key in (verified_bond_key(offer, self._network) for offer in offers)
            if key is not None
        }
        self._taker._session.round_maker_bond_keys |= keys

    def _round_maker_keys(self) -> set[BondKey]:
        """Return every maker identity this round recorded or is still holding."""
        session = self._taker._session
        live = (maker.offer for maker in session.maker_sessions.values())
        return session.round_maker_bond_keys | {
            key
            for key in (verified_bond_key(offer, self._network) for offer in live)
            if key is not None
        }

    def allows(self, seller_bond: BondReference | None) -> bool:
        """Reject a credential sold by any maker identity seen in this round."""
        if seller_bond is None:
            # No verified provenance: this pool makes no seller claim, and the
            # pre-existing external credential contract is unchanged.
            return True
        key = self._seller_key(seller_bond)
        if key is None:
            logger.warning(
                "Rejecting an external PoDLE credential whose seller bond is on another network"
            )
            return False
        if key in self._round_maker_keys():
            logger.warning(
                "Rejecting an external PoDLE credential: its seller bond belongs to a maker "
                "selected or contacted in this CoinJoin round"
            )
            return False
        return True

    def record_claim(self, seller_bond: BondReference | None) -> None:
        """Retain the seller identity for every later selection pass of the round."""
        if seller_bond is None:
            return
        if seller_bond not in self._taker._session.podle_seller_bonds:
            self._taker._session.podle_seller_bonds.append(seller_bond)
            logger.info(
                "Excluding the fidelity bond of an external PoDLE credential seller from "
                "maker selection for the rest of this CoinJoin round"
            )

    def excluded_nicks(self) -> set[str]:
        """Return the nicks currently proving a seller bond used in this round."""
        keys = {
            key
            for key in (self._seller_key(bond) for bond in self._taker._session.podle_seller_bonds)
            if key is not None
        }
        return nicks_proving_bonds(self._taker.orderbook_manager.offers, keys, self._network)


class Taker(TakerMonitoringMixin):
    """
    Main Taker class for executing CoinJoin transactions.
    """

    def __init__(
        self,
        wallet: WalletService,
        backend: BlockchainBackend,
        config: TakerConfig,
        confirmation_callback: ConfirmationCallback | None = None,
    ):
        """
        Initialize the Taker.

        Args:
            wallet: Wallet service for UTXO management and signing
            backend: Blockchain backend for broadcasting
            config: Taker configuration
            confirmation_callback: Optional callback for user confirmation before proceeding
        """
        podle_data_dir = config.data_dir
        wallet_data_dir = getattr(wallet, "data_dir", None)
        if isinstance(wallet_data_dir, Path):
            podle_data_dir = (
                get_default_data_dir() if podle_data_dir is None else podle_data_dir
            ).resolve()
            # Bind before activation too: this wallet may activate while the taker is running.
            if wallet_data_dir.resolve() != podle_data_dir:
                raise ValueError("Wallet and taker must use the same data directory")

        self.wallet = wallet
        self.backend = backend
        self.config = config
        self.confirmation_callback = confirmation_callback

        pit_script_type = offer_output_script_type(config.preferred_offer_type)
        wallet_type = getattr(wallet, "address_type", None)
        if wallet_type in ("p2wpkh", "p2tr") and pit_script_type != wallet_type:
            raise ValueError(
                f"preferred_offer_type {config.preferred_offer_type.value!r} implies a "
                f"{pit_script_type!r} pit but the wallet is {wallet_type!r}; a rigid "
                f"JMP-0010 pit requires them to match (use a {pit_script_type!r} wallet or a "
                f"matching offer family)."
            )

        self.nick_identity = NickIdentity(JM_VERSION)
        self.nick = self.nick_identity.nick
        self.state = TakerState.IDLE
        # Source mixdepth of the most recent ``do_coinjoin`` call. With
        # interactive selection this is derived from the selected UTXOs, so
        # callers (e.g. the CLI confirmation prompt) can display it.
        self.last_source_mixdepth: int | None = None

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
            nick_auth_mode=config.nick_auth_mode,
            nick_auth_directory_ids=config.nick_auth_directory_ids,
            allow_clearnet_connections=config.allow_clearnet_connections,
            neutrino_compat=neutrino_compat,
            stream_isolation=config.stream_isolation,
        )

        # Orderbook manager
        # Read maker nick from state file to exclude from peer selection (self-CoinJoin protection)
        # Only the maker trading in the same pit can meet us in a CoinJoin.
        self._maker_nick_component = get_nick_state_component("maker", config.address_type)
        own_wallet_nicks: set[str] = set()
        maker_nick = read_nick_state(config.data_dir, self._maker_nick_component)
        if maker_nick:
            own_wallet_nicks.add(maker_nick)
            logger.info(f"Self-CoinJoin protection: excluding maker nick {maker_nick}")

        self.orderbook_manager = OrderbookManager(
            config.max_cj_fee,
            bondless_makers_allowance=config.bondless_makers_allowance,
            bondless_require_zero_fee=config.bondless_makers_allowance_require_zero_fee,
            data_dir=config.data_dir,
            own_wallet_nicks=own_wallet_nicks,
            require_quantized_cj_fees=config.require_quantized_cj_fees,
            round_up_cj_fees=config.round_up_cj_fees,
            equalize_cj_fees=config.equalize_cj_fees,
            allowed_types=offer_types_for_family(config.preferred_offer_type),
        )
        # Do not load or rewrite a previous run's fault evidence unless the
        # operator enabled the experimental maker-selection policy explicitly.
        self.market_fault_cache = MarketFaultCache(
            config.data_dir if config.market_fault_exclusion else None
        )

        # PoDLE manager for commitment tracking
        wallet_id = getattr(wallet, "market_wallet_id", None)
        self.podle_manager = PoDLEManager(
            podle_data_dir, wallet_id=wallet_id if isinstance(wallet_id, str) else None
        )

        # Per-CoinJoin state and protocol phases live in a dedicated
        # ``CoinJoinSession``. ``Taker`` owns persistent infrastructure
        # (wallet, backend, config, directory client, orderbook manager,
        # PoDLE manager, schedule) and delegates the protocol phases to the
        # session, which is reset at the start of each ``do_coinjoin`` call.
        self._session = CoinJoinSession()
        self._session.attach(self)
        self._round_lock = asyncio.Lock()
        self._coinjoin_log_context: Any | None = None

        # Schedule for tumbler-style operations
        self.schedule: Schedule | None = None

        # Background task tracking
        self.running = False
        self._background_tasks: list[asyncio.Task[None]] = []
        self._channel_ring_nodes: Any | None = None
        self._channel_ring_store: RingParticipantStore | None = None
        self._channel_ring_coordinator_store: CoordinatorStore | None = None
        self._channel_ring_recovery_tasks: dict[str, asyncio.Task[object]] = {}
        if config.channel_ring.enabled:
            data_directory = Path(config.data_dir or get_default_data_dir())
            self._channel_ring_store = RingParticipantStore(
                config.channel_ring.persistence_path(data_directory),
                max_active_sessions=config.channel_ring.max_active_sessions,
                max_verified_sessions=config.channel_ring.max_verified_sessions,
            )

    async def _request_confirmation(
        self,
        *,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> bool:
        """Run a synchronous or awaitable confirmation callback with freshness enforcement."""
        callback = self.confirmation_callback
        if callback is None:
            return True

        loop = asyncio.get_running_loop()
        started_at = loop.time()
        result = callback(**kwargs)
        if inspect.isawaitable(result):
            awaitable = cast(Awaitable[bool], result)
            confirmed = (
                await asyncio.wait_for(awaitable, timeout=timeout)
                if timeout is not None and timeout > 0
                else await awaitable
            )
        else:
            confirmed = result

        # Synchronous callbacks cannot be interrupted portably. Reject their
        # answer after return if the prepared preview has already expired.
        if timeout is not None and timeout > 0 and loop.time() - started_at >= timeout:
            raise TimeoutError
        return bool(confirmed)

    def _activate_coinjoin_log_context(self) -> str | None:
        """Replace this task's CoinJoin log context after a commitment rotation."""
        current_context = getattr(self, "_coinjoin_log_context", None)
        if current_context is not None:
            current_context.__exit__(None, None, None)
        self._coinjoin_log_context = None
        commitment = self._session.podle_commitment
        if commitment is None:
            raise RuntimeError("Cannot activate CoinJoin logging without a PoDLE commitment")
        commitment_hex = commitment.commitment.commitment.hex()
        if not isinstance(commitment_hex, str):
            return None
        try:
            coinjoin_id = coinjoin_id_from_commitment(commitment_hex)
        except ValueError:
            return None
        self._coinjoin_log_context = coinjoin_log_context(commitment_hex)
        self._coinjoin_log_context.__enter__()
        return coinjoin_id

    def _clear_coinjoin_log_context(self) -> None:
        """Clear the task-local CoinJoin context at the end of a round."""
        current_context = getattr(self, "_coinjoin_log_context", None)
        if current_context is not None:
            current_context.__exit__(None, None, None)
        self._coinjoin_log_context = None

    def _current_coinjoin_id(self) -> str | None:
        commitment = self._session.podle_commitment
        if commitment is None:
            return None
        commitment_hex = commitment.commitment.commitment.hex()
        if not isinstance(commitment_hex, str):
            return None
        try:
            return coinjoin_id_from_commitment(commitment_hex)
        except ValueError:
            return None

    async def sync_wallet(self) -> int:
        """
        Sync the wallet and return total balance.

        This method is separated from start() to allow callers to check
        funds before connecting to directory servers (avoiding unnecessary
        network connections when funds are insufficient).

        Returns:
            Total wallet balance in satoshis.
        """
        logger.info("Starting taker")
        logger.bind(sensitive=True).info(f"Starting taker (nick: {self.nick})")

        # Log wallet name if using descriptor wallet backend
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        if isinstance(self.backend, DescriptorWalletBackend):
            logger.info("Using descriptor wallet backend")
            logger.bind(sensitive=True).info(f"Using wallet: {self.backend.wallet_name}")

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
        await self.wallet.reconstruct_imported_state_safe()

        total_balance = await self.wallet.get_total_balance()
        logger.bind(sensitive=True).info("Wallet synced. Total balance: {:,} sats", total_balance)

        return total_balance

    async def connect(self) -> None:
        """
        Connect to directory servers and start background tasks.

        This should be called after sync_wallet() and any fund validation.
        """
        self.warn_experimental_features()
        if self.config.channel_ring.enabled or self.config.channel_ring.nodes:
            if not self.backend.has_mempool_access() or not (
                self.backend.can_get_confirmations_by_txid()
            ):
                raise RuntimeError(
                    "Channel-ring taker requires a full backend that can prove mempool "
                    "and chain absence"
                )
            from jmswap.channel_ring_nodes import (
                channel_ring_wallet_identity,
                initialize_channel_ring_nodes,
            )

            wallet_identity = channel_ring_wallet_identity(
                self.wallet.master_key.get_public_key_bytes()
            )
            data_directory = Path(self.config.data_dir or get_default_data_dir())
            # Preflight strict coordinator state before inspecting a local LND
            # node. Reload under the runtime lease after node initialization.
            from taker.channel_ring_coordinator_store import CoordinatorStore

            preflight = CoordinatorStore(
                self.config.channel_ring.persistence_path(data_directory),
                network=cast(Any, self.config.network.value),
                wallet_identity=wallet_identity,
                max_active_sessions=self.config.channel_ring.max_active_sessions,
                max_verified_sessions=self.config.channel_ring.max_verified_sessions,
            )
            preflight.load_all()

            self._channel_ring_nodes = await initialize_channel_ring_nodes(
                self.config.channel_ring,
                network=self.config.network.value,
                offer_type=self.config.preferred_offer_type.value,
                wallet_identity=wallet_identity,
                mixdepth_count=self.wallet.mixdepth_count,
                data_directory=data_directory,
            )
            self._channel_ring_store = self._channel_ring_nodes.store
            self._channel_ring_coordinator_store = CoordinatorStore(
                self._channel_ring_store.directory,
                network=cast(Any, self.config.network.value),
                wallet_identity=wallet_identity,
                participant_store=self._channel_ring_store,
                max_active_sessions=self.config.channel_ring.max_active_sessions,
                max_verified_sessions=self.config.channel_ring.max_verified_sessions,
            )
            # Validate ownership and all field states before any LND recovery or
            # funding action. Unknown coordinator evidence is never ignored.
            coordinator_records = self._channel_ring_coordinator_store.load_all()
            from taker.channel_ring import reconcile_taker_ring_records

            await reconcile_taker_ring_records(
                self._channel_ring_store,
                self._channel_ring_nodes,
                self.backend,
                self._channel_ring_recovery_tasks,
                acceptor_timeout_seconds=self.config.channel_ring.phase_timeout_seconds,
            )
            if not self._renew_channel_ring_input_locks():
                raise RuntimeError("could not restore an active channel-ring input reservation")
            from taker.channel_ring_maker_only import reconcile_maker_only_ring_records

            await reconcile_maker_only_ring_records(
                self._channel_ring_coordinator_store, self.backend
            )
            report = self._channel_ring_store.load_all()
            coordinator_records = self._channel_ring_coordinator_store.load_all()
            active = sum(record.active for record in report.records) + sum(
                record.active for record in coordinator_records
            )
            verified = sum(record.verified_unresolved for record in report.records) + sum(
                record.verified_unresolved for record in coordinator_records
            )
            if (
                report.corruptions
                or active >= self.config.channel_ring.max_active_sessions
                or verified >= self.config.channel_ring.max_verified_sessions
            ):
                raise RuntimeError(
                    "Channel-ring durable capacity is exhausted or contains corrupt records"
                )
            if self.config.channel_ring.enabled and self.config.channel_ring.taker_joins:
                await self._channel_ring_nodes.enable_funding()

        # Connect to directory servers
        logger.info("Connecting to directory servers...")
        connected = await self.directory_client.connect_all()
        connection_result = self.directory_client.last_connection_result

        if connected == 0:
            raise RuntimeError("Failed to connect to any directory server")

        if connection_result.failed:
            logger.warning(
                f"Connected to {connection_result.connected}/{connection_result.total} "
                f"directory servers ({connection_result.failed} unavailable)"
            )
        else:
            logger.info(
                "Connected to {}/{} directory servers",
                connection_result.connected,
                connection_result.total,
            )

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
        if self._channel_ring_nodes is not None:
            ring_task = asyncio.create_task(self._periodic_channel_ring_reconciliation())
            self._background_tasks.append(ring_task)

    def warn_experimental_features(self) -> None:
        """Warn once about every experimental feature this taker has enabled."""
        warn_experimental(
            [
                *([experimental.TAPROOT_PIT] if self.config.address_type == "p2tr" else []),
                *([experimental.CHANNEL_RING] if self.config.channel_ring.enabled else []),
                *(
                    [experimental.CREDENTIAL_MARKET]
                    if self.config.external_podle_mode == "only"
                    or self.config.market_fault_exclusion
                    else []
                ),
            ],
            str(self.config.bitcoin_network or self.config.network),
        )

    async def start(self) -> None:
        """
        Start the taker: sync wallet and connect to directory servers.

        This is a convenience method that calls sync_wallet() followed by connect().
        For early fund validation, call sync_wallet() first, validate, then call connect().
        """
        await self.sync_wallet()
        await self.connect()

    def release_input_locks(self) -> None:
        """Clean up persisted CoinJoin locks held on this round's taker inputs.

        Failures before local taker signing release immediately. Once local
        signatures may exist, owner-qualified leases are renewed through the
        pending window instead.
        """
        if self._session and self._session.signing_boundary_crossed:
            self._session.retain_input_locks()
            return
        if self._session and self._session.reserved_inputs:
            try:
                coordinator = self._session.ring_coordinator
                if (
                    coordinator is not None
                    and getattr(coordinator, "has_durable_record", True)
                    and coordinator._record().active
                ):
                    logger.warning("Retaining taker input locks for an active channel-ring record")
                    return
                self.wallet.release_coinjoin_inputs(
                    self._session.reserved_inputs,
                    owner=self._session.input_lock_owner,
                )
            except Exception as e:
                logger.bind(sensitive=True).debug("Failed to release taker input locks: {}", e)
            else:
                self._session.reserved_inputs = set()

    def _begin_input_lock_round(self) -> None:
        """Detach any prior reservation and create a unique owner for this round."""
        self.release_input_locks()
        self._session.reserved_inputs = set()
        self._session.input_lock_owner = f"taker:{secrets.token_hex(32)}"
        self._session.ring_coordinator = None

    @property
    def last_failure_reason(self) -> str | None:
        """Reason the most recent ``do_coinjoin`` call failed (or ``None``).

        Forwarded from the per-round :class:`CoinJoinSession` so external
        consumers (e.g. the tumbler runner) can surface why a round did not
        broadcast without reaching into private session state.
        """
        return self._session.last_failure_reason

    @property
    def txid(self) -> str:
        """Txid of the most recently broadcast CoinJoin, or ``""`` if none.

        Forwarded from the per-round :class:`CoinJoinSession` for the same
        reason as ``last_failure_reason``: external consumers (e.g. the
        jmwalletd taker status endpoint) need this after ``do_coinjoin``
        returns without reaching into private session state.
        """
        return self._session.txid

    @property
    def last_used_nicks(self) -> set[str]:
        """Maker nicks in the most recently broadcast CoinJoin."""
        return self._session.last_used_nicks

    @property
    def last_used_maker_keys(self) -> set[str]:
        """Maker nick and bond keys in the most recently broadcast CoinJoin."""
        return self._session.last_used_maker_keys

    @property
    def last_broadcast_policy(self) -> str:
        """Configured policy for the most recent broadcast attempt."""
        return self._session.broadcast_policy

    @property
    def last_broadcast_method(self) -> str:
        """Actual method used for the most recent successful broadcast."""
        return self._session.broadcast_method

    @property
    def last_broadcast_fallback_reason(self) -> str:
        """Stable reason a peer policy fell back to local broadcasting."""
        return self._session.broadcast_fallback_reason

    @property
    def failed_signer_nicks(self) -> set[str]:
        """Maker nicks that failed to provide valid signatures in the current round."""
        return self._session.failed_signer_nicks

    @property
    def minimum_fee_rate_sat_vb(self) -> float | None:
        """Resolved minimum miner fee rate for the current round."""
        return self._session._minimum_fee_rate_sat_vb

    def _wallet_funding_required(
        self,
        total_required: int,
        buyout: ChannelBuyout | None = None,
    ) -> int:
        """Wallet-only funding requirement, for a prepared or attached buyout.

        Preflight runs before a round exists, so the caller may pass the adapter
        it is about to use; otherwise the current round's adapter (or none) decides.
        """
        if buyout is None:
            return self._session.wallet_funding_required(total_required)
        return buyout.wallet_funding_required(total_required)

    async def _resolve_explicit_input_utxos(
        self,
        input_utxos: list[str],
        mixdepth: int,
        amount: int,
        buyout: ChannelBuyout | None = None,
    ) -> list[UTXOInfo]:
        """Resolve and validate a strict set of taker CoinJoin inputs."""
        selected, _locktime_cutoff = await resolve_input_utxos(
            wallet=self.wallet,
            backend=self.backend,
            mixdepth=mixdepth,
            input_utxos=input_utxos,
            allow_fidelity_bonds=False,
        )

        reserved = self.wallet.get_locked_input_outpoints()
        for utxo in selected:
            outpoint = (utxo.txid, utxo.vout)
            if outpoint in reserved:
                msg = f"Input UTXO {utxo.txid}:{utxo.vout} is locked by another in-flight CoinJoin"
                raise ValueError(msg)
            if utxo.confirmations < self.config.taker_utxo_age:
                msg = (
                    f"Input UTXO {utxo.txid}:{utxo.vout} has {utxo.confirmations} "
                    f"confirmation(s); CoinJoin requires at least "
                    f"{self.config.taker_utxo_age} (taker_utxo_age)"
                )
                raise ValueError(msg)

        if amount > 0:
            total = sum(utxo.value for utxo in selected)
            required = self._wallet_funding_required(amount, buyout)
            if total < required:
                msg = (
                    f"Insufficient funds in explicit input UTXOs: have {total:,} sats, "
                    f"need at least {required:,} sats before fees"
                )
                raise ValueError(msg)
            # The PoDLE commitment covers the CoinJoin amount itself, so channel
            # value never lowers the wallet UTXO size this requires.
            if (
                not podle_threshold_met(
                    selected,
                    amount,
                    self.config.taker_utxo_age,
                    self.config.taker_utxo_amtpercent,
                )
                and self.config.external_podle_mode != "only"
            ):
                min_value = int(amount * self.config.taker_utxo_amtpercent / 100)
                msg = (
                    "No explicit input UTXO is large enough for the PoDLE commitment: "
                    f"need at least {min_value:,} sats "
                    f"({self.config.taker_utxo_amtpercent}% of {amount:,} sats)"
                )
                raise ValueError(msg)

        return selected

    async def check_utxo_eligibility(
        self,
        amount: int,
        mixdepth: int | None,
        input_utxos: list[str] | None = None,
        buyout: ChannelBuyout | None = None,
    ) -> str | None:
        """Validate that ``mixdepth`` can fund a CoinJoin of ``amount``.

        Runs the same eligibility filters used later in :meth:`do_coinjoin`
        (confirmations, frozen, fidelity bonds, in-flight locks, the mixdepth-0
        merge restriction and the PoDLE size requirement) *before* any network
        operation, so an ineligible wallet fails fast instead of after a long
        directory/orderbook/bond cycle (issue #528).

        Args:
            amount: Target amount in satoshis (``0`` for sweep).
            mixdepth: Source mixdepth. ``None`` is only meaningful with
                interactive selection (``select_utxos``), where it means "any
                mixdepth" (the source is derived from the selection later);
                otherwise it falls back to mixdepth 0.
            input_utxos: Optional exact ``txid:vout`` set to validate instead
                of automatic or interactive selection.
            buyout: Prepared channel buyout this round will use, when it was
                not attached to a session yet. Channel value lowers the
                *funding* the wallet must supply; it never lowers the PoDLE
                commitment requirement, which follows the CoinJoin amount.

        Returns:
            ``None`` when a CoinJoin can proceed, otherwise a human-readable
            reason describing why it cannot.
        """
        min_conf = self.config.taker_utxo_age

        source_mixdepth = (
            mixdepth if mixdepth is not None else (None if self.config.select_utxos else 0)
        )
        if (
            self.config.channel_ring.enabled
            and self.config.channel_ring.taker_joins
            and source_mixdepth is not None
            and source_mixdepth not in self.config.channel_ring.mixdepth_nodes
        ):
            return "Source mixdepth has no configured channel-ring node"

        if input_utxos is not None:
            if self.config.select_utxos:
                return "Cannot specify both --select-utxos and --input-utxo"
            resolved_mixdepth = mixdepth if mixdepth is not None else 0
            try:
                await self._resolve_explicit_input_utxos(
                    input_utxos,
                    resolved_mixdepth,
                    amount,
                    buyout,
                )
            except ValueError as exc:
                return str(exc)
            return None

        # Interactive selection follows different rules (the user may pick
        # unlocked fidelity bonds and is not bound to auto-selection), so only
        # require that *something* is selectable here.
        if self.config.select_utxos:
            mixdepths = (
                [mixdepth] if mixdepth is not None else list(range(self.wallet.mixdepth_count))
            )
            utxos = []
            for md in mixdepths:
                if (
                    self.config.channel_ring.enabled
                    and self.config.channel_ring.taker_joins
                    and md not in self.config.channel_ring.mixdepth_nodes
                ):
                    continue
                utxos.extend(await self.wallet.get_utxos(md))
            reserved = self.wallet.get_locked_input_outpoints()
            if not selectable_for_interactive(utxos, min_conf, excluded_outpoints=reserved):
                if mixdepth is not None:
                    return classify_utxos(
                        utxos, mixdepth, min_conf, reserved_outpoints=reserved
                    ).no_eligible_reason()
                return (
                    "No selectable UTXOs in any mixdepth (all UTXOs are "
                    "frozen, immature, locked fidelity bonds, or in use)"
                )
            return None

        if mixdepth is None:
            mixdepth = 0
        utxos = await self.wallet.get_utxos(mixdepth)

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
        if (
            not podle_threshold_met(
                breakdown.eligible, amount, min_conf, self.config.taker_utxo_amtpercent
            )
            and self.config.external_podle_mode != "only"
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
                self._wallet_funding_required(amount, buyout),
                min_conf,
                exclude=reserved,
            )
        except ValueError as exc:
            return _append_confirmation_hint(str(exc), min_conf)

        return None

    async def _prepare_requested_input_selection(
        self,
        amount: int,
        mixdepth: int | None,
        input_utxos: list[str] | None,
    ) -> tuple[list[UTXOInfo] | None, list[UTXOInfo] | None, int] | None:
        """Resolve explicit or interactive input requests and run preflight."""
        explicitly_selected: list[UTXOInfo] | None = None
        manually_selected: list[UTXOInfo] | None = None

        if input_utxos is not None:
            if self.config.select_utxos:
                reason = "Cannot specify both --select-utxos and --input-utxo"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None
            resolved_mixdepth = mixdepth if mixdepth is not None else 0
            try:
                explicitly_selected = await self._resolve_explicit_input_utxos(
                    input_utxos,
                    resolved_mixdepth,
                    amount,
                )
            except ValueError as exc:
                reason = str(exc)
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None
            self._session.strict_input_selection = True
            logger.info(
                f"Using {len(explicitly_selected)} explicit input UTXO(s) "
                f"from mixdepth {resolved_mixdepth}"
            )
        elif self.config.select_utxos:
            logger.info("Launching interactive UTXO selection...")
            manually_selected = await self._maybe_select_utxos_interactively(
                amount=amount,
                mixdepth=mixdepth,
            )
            if not manually_selected:
                return None
            resolved_mixdepth = manually_selected[0].mixdepth
            logger.info(f"Source mixdepth: {resolved_mixdepth} (from selection)")
        else:
            resolved_mixdepth = mixdepth if mixdepth is not None else 0

        if (
            self.config.channel_ring.enabled
            and self.config.channel_ring.taker_joins
            and resolved_mixdepth not in self.config.channel_ring.mixdepth_nodes
        ):
            self._session.last_failure_reason = (
                "Source mixdepth has no configured channel-ring node"
            )
            self.state = TakerState.FAILED
            return None

        if explicitly_selected is None:
            eligibility_reason = await self.check_utxo_eligibility(amount, resolved_mixdepth)
            if eligibility_reason is not None:
                logger.error(eligibility_reason)
                self._session.last_failure_reason = eligibility_reason
                self.state = TakerState.FAILED
                return None

        self.last_source_mixdepth = resolved_mixdepth
        return explicitly_selected, manually_selected, resolved_mixdepth

    def _select_coinjoin_utxos_with_podle(
        self,
        mixdepth: int,
        target_amount: int,
        private_key_getter: Callable[[str], bytes | None],
        excluded_outpoints: set[tuple[str, int]],
    ) -> list[UTXOInfo]:
        """Select funding inputs containing a usable local PoDLE UTXO when enabled."""
        if self.config.external_podle_mode == "only":
            return self.wallet.select_utxos(
                mixdepth,
                target_amount,
                self.config.taker_utxo_age,
                exclude=excluded_outpoints,
            )
        available = self.wallet.get_all_utxos(
            mixdepth,
            self.config.taker_utxo_age,
            exclude=excluded_outpoints,
        )
        fresh_utxos = self.podle_manager.get_fresh_commitment_utxos(
            wallet_utxos=available,
            cj_amount=self._session.cj_amount,
            private_key_getter=private_key_getter,
            min_confirmations=self.config.taker_utxo_age,
            min_percent=self.config.taker_utxo_amtpercent,
            max_retries=self.config.taker_utxo_retries,
        )
        if not fresh_utxos:
            raise ValueError(
                f"No fresh PoDLE commitments remain on eligible UTXOs in mixdepth {mixdepth}"
            )

        selected = self.wallet.select_utxos(
            mixdepth,
            target_amount,
            self.config.taker_utxo_age,
            exclude=excluded_outpoints,
        )
        fresh_outpoints = {(utxo.txid, utxo.vout) for utxo in fresh_utxos}
        if any((utxo.txid, utxo.vout) in fresh_outpoints for utxo in selected):
            return selected

        # Prefer the largest fresh UTXO so the replacement is least likely to
        # increase the input count. The wallet selector still enforces its own
        # mixdepth and merge policy around the mandatory input.
        selection_error: ValueError | None = None
        for podle_utxo in sorted(
            fresh_utxos,
            key=lambda utxo: (utxo.value, utxo.confirmations),
            reverse=True,
        ):
            try:
                return self.wallet.select_utxos(
                    mixdepth,
                    target_amount,
                    self.config.taker_utxo_age,
                    include_utxos=[podle_utxo],
                    exclude=excluded_outpoints,
                )
            except ValueError as exc:
                selection_error = exc

        if selection_error is not None:
            raise selection_error
        raise ValueError(f"Unable to select a PoDLE-capable UTXO in mixdepth {mixdepth}")

    async def _allocate_podle_commitment(
        self,
        wallet_utxos: list[UTXOInfo],
        get_private_key: Callable[[str], bytes | None],
    ) -> ExtendedPoDLECommitment | None:
        """Allocate exactly the configured PoDLE source without changing funding inputs."""
        if self.config.external_podle_mode == "only":
            preview = self._session.external_podle_preview
            if preview is not None:
                # The round already excluded this seller from maker selection,
                # so it must burn this exact record or none at all.
                self._session.external_podle_preview = None
                return self._claim_previewed_external(preview)
            return await self.podle_manager.consume_external(
                backend=self.backend,
                network=(self.config.bitcoin_network or self.config.network).value,
                cj_amount=self._session.cj_amount,
                min_confirmations=self.config.taker_utxo_age,
                min_percent=self.config.taker_utxo_amtpercent,
                max_retries=self.config.taker_utxo_retries,
                seller_policy=self._seller_separation,
            )
        return self.podle_manager.generate_fresh_commitment(
            wallet_utxos=wallet_utxos,
            cj_amount=self._session.cj_amount,
            private_key_getter=get_private_key,
            min_confirmations=self.config.taker_utxo_age,
            min_percent=self.config.taker_utxo_amtpercent,
            max_retries=self.config.taker_utxo_retries,
        )

    @property
    def _seller_separation(self) -> _RoundSellerSeparation:
        """Return the seller separation view of the current round.

        It owns no state of its own: the round's maker identities and seller
        bonds live in the session, so a fresh view is always consistent with
        the round that is running.
        """
        return _RoundSellerSeparation(self)

    async def _prepare_external_podle(self, cj_amount_bound: int) -> None:
        """Pick the external credential this round will use, before selecting makers.

        Nothing is consumed here: a cancelled or failed round leaves the
        credential usable. The preview exists so the seller's own fidelity bond
        can be hard-excluded from maker selection before any maker is contacted.

        ``cj_amount_bound`` is the CoinJoin amount for normal rounds. A sweep
        does not know its amount until makers are chosen, so it passes the
        total input value, an upper bound: a credential whose backing UTXO is
        only just large enough for the eventual CoinJoin amount can be rejected
        here even though it would have been eligible.

        Finding no usable credential is not decided here. The round continues
        and fails at commitment allocation, exactly as it did before previews
        existed; any credential allocated there is still filtered by the same
        seller separation policy.
        """
        if self.config.external_podle_mode != "only":
            return
        preview = await self.podle_manager.preview_external(
            backend=self.backend,
            network=(self.config.bitcoin_network or self.config.network).value,
            cj_amount=cj_amount_bound,
            min_confirmations=self.config.taker_utxo_age,
            min_percent=self.config.taker_utxo_amtpercent,
            max_retries=self.config.taker_utxo_retries,
            seller_policy=self._seller_separation,
        )
        if preview is None:
            logger.warning(
                "No usable external PoDLE credential is available yet for this CoinJoin "
                "(external_podle_mode=only)"
            )
            return
        self._session.external_podle_preview = preview
        # Exclude the seller before selection, not after: makers chosen now
        # would otherwise learn a commitment their own bond authorized.
        self._seller_separation.record_claim(preview.seller_bond)

    def _claim_previewed_external(
        self, preview: ExternalPoDLEPreview
    ) -> ExtendedPoDLECommitment | None:
        """Burn exactly the previewed credential, never a different seller's."""
        if not self._seller_separation.allows(preview.seller_bond):
            return None
        commitment = self.podle_manager.claim_previewed_external(
            preview, seller_policy=self._seller_separation
        )
        if commitment is None:
            logger.error(
                "The selected external PoDLE credential is no longer available; not "
                "substituting a different seller after makers were selected"
            )
        return commitment

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
        recovery_tasks = getattr(self, "_channel_ring_recovery_tasks", {})
        for task in recovery_tasks.values():
            task.cancel()

        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks.values(), return_exceptions=True)
            recovery_tasks.clear()

        self.podle_manager.close()
        await self.directory_client.close_all()
        if self._channel_ring_nodes is not None:
            # The foreground round is also a journal writer. Do not release
            # its process lease merely because background tasks have stopped.
            async with self._round_lock:
                coordinator = self._session.ring_coordinator
                if coordinator is not None and coordinator.acceptor_task is not None:
                    coordinator.acceptor_task.cancel()
                    await asyncio.gather(coordinator.acceptor_task, return_exceptions=True)
                await self._channel_ring_nodes.close()
                self._channel_ring_nodes = None
        if close_wallet:
            await self.wallet.close()
        logger.info("Taker stopped")

    async def _periodic_channel_ring_reconciliation(self) -> None:
        from taker.channel_ring import reconcile_taker_ring_records

        while self.running:
            assert self._channel_ring_store is not None
            assert self._channel_ring_nodes is not None
            await reconcile_taker_ring_records(
                self._channel_ring_store,
                self._channel_ring_nodes,
                self.backend,
                self._channel_ring_recovery_tasks,
                acceptor_timeout_seconds=self.config.channel_ring.phase_timeout_seconds,
                active_session_identities=self._active_ring_session_identities(),
            )
            if not self._renew_channel_ring_input_locks():
                logger.error(
                    "Stopped channel-ring reconciliation because an active input "
                    "reservation could not be renewed"
                )
                return
            if self._channel_ring_coordinator_store is not None:
                from taker.channel_ring_maker_only import reconcile_maker_only_ring_records

                await reconcile_maker_only_ring_records(
                    self._channel_ring_coordinator_store,
                    self.backend,
                    active_session_identities=self._active_ring_session_identities(),
                )
            await asyncio.sleep(self.config.channel_ring.phase_timeout_seconds)

    def _active_ring_session_identities(self) -> frozenset[str]:
        """Identify the ring round this process is still driving, if any.

        Reconciliation must never escalate a record that a live round still owns.
        """
        session = getattr(self, "_session", None)
        coordinator = None if session is None else session.ring_coordinator
        if coordinator is None:
            return frozenset()
        return frozenset({coordinator.session_identity})

    def _renew_channel_ring_input_locks(self) -> bool:
        if self._channel_ring_store is None:
            return True
        report = self._channel_ring_store.load_all()
        if report.corruptions:
            return False
        ttl = self._session.input_lock_ttl_sec()
        active_records = [
            (record.input_lock_owner, record.local_input_outpoints)
            for record in report.records
            if record.active and record.local_input_outpoints
        ]
        coordinator_store = getattr(self, "_channel_ring_coordinator_store", None)
        if coordinator_store is not None:
            active_records.extend(
                (record.input_lock_owner, record.input_outpoints)
                for record in coordinator_store.load_all()
                if record.active
            )
        for owner, inputs in active_records:
            if owner is None:
                return False
            outpoints = {(item.txid, item.vout) for item in inputs}
            # Renew live leases before restoring missing ones from the durable
            # record. Acquisition deliberately rejects even our own live lease.
            if self.wallet.renew_coinjoin_inputs(outpoints, ttl=ttl, owner=owner):
                continue
            if not self.wallet.reserve_coinjoin_inputs(outpoints, ttl=ttl, owner=owner):
                return False
        return True

    async def _update_offers_with_bond_values(self, offers: list[Offer]) -> int | None:
        """
        Verify fidelity bonds and calculate their values.

        Uses the backend's ``verify_bonds()`` method for efficient bulk verification
        that works correctly on all backends (Bitcoin Core, neutrino, mempool).

        For each offer with a fidelity bond proof, derives the P2WSH bond address
        from the UTXO public key and locktime, then delegates verification to the
        backend which can batch the lookups optimally.
        """
        for offer in offers:
            offer.fidelity_bond_value = 0
            offer.fidelity_bond_verified = None

        bonded_offers = [offer for offer in offers if offer.fidelity_bond_data]
        if not bonded_offers:
            return None

        try:
            current_block_height = await self.backend.get_block_height()
        except Exception as e:
            logger.warning("Cannot verify fidelity bond certificate expiry")
            logger.bind(sensitive=True).warning("Fidelity bond expiry detail: {}", e)
            return None
        if type(current_block_height) is not int or current_block_height < 0:
            logger.warning(
                f"Cannot verify fidelity bond certificate expiry: backend returned "
                f"invalid block height {current_block_height!r}"
            )
            return None

        # Deduplicate identical claims while verifying conflicting script claims
        # independently. A claim is the outpoint plus its proof-derived script.
        claim_to_request: dict[tuple[str, int, str], BondVerificationRequest] = {}
        claim_to_offers: dict[tuple[str, int, str], list[Offer]] = {}

        for offer in bonded_offers:
            bond_data = offer.fidelity_bond_data
            assert bond_data is not None

            txid = bond_data["utxo_txid"]
            vout = bond_data["utxo_vout"]
            cert_expiry_height = bond_data.get("cert_expiry")

            if not isinstance(cert_expiry_height, int):
                offer.fidelity_bond_verified = False
                logger.bind(sensitive=True).debug(
                    "Bond {}:{} missing certificate expiry, skipping", txid, vout
                )
                continue
            if current_block_height > cert_expiry_height:
                offer.fidelity_bond_verified = False
                logger.debug(
                    f"Bond {txid}:{vout} certificate expired at block "
                    f"{cert_expiry_height} (current block {current_block_height})"
                )
                continue

            locktime = bond_data["locktime"]
            utxo_pub = bond_data.get("utxo_pub")

            if not utxo_pub:
                offer.fidelity_bond_verified = False
                logger.bind(sensitive=True).debug(
                    "Bond {}:{} missing utxo_pub, skipping", txid, vout
                )
                continue

            try:
                utxo_pub_bytes = bytes.fromhex(utxo_pub) if isinstance(utxo_pub, str) else utxo_pub
                bond_addr = derive_bond_address(
                    utxo_pub_bytes,
                    locktime,
                    self.config.bitcoin_network or self.config.network,
                )
            except Exception as e:
                offer.fidelity_bond_verified = False
                logger.debug("Failed to derive bond address")
                logger.bind(sensitive=True).debug(
                    "Bond address derivation detail for {}:{}: {}", txid, vout, e
                )
                continue

            request = BondVerificationRequest(
                txid=txid,
                vout=vout,
                utxo_pub=utxo_pub_bytes,
                locktime=locktime,
                address=bond_addr.address,
                scriptpubkey=bond_addr.scriptpubkey.hex(),
            )
            claim_key = (txid, vout, request.scriptpubkey)
            if claim_key in claim_to_request:
                claim_to_offers[claim_key].append(offer)
                continue

            claim_to_request[claim_key] = request
            claim_to_offers[claim_key] = [offer]

        if not claim_to_request:
            return current_block_height

        logger.info(f"Verifying {len(claim_to_request)} fidelity bonds...")

        # Bulk verify via the backend (batched for efficiency)
        try:
            requests = list(claim_to_request.values())
            results = await self.backend.verify_bonds(requests)
        except Exception as e:
            logger.warning("Bond verification failed")
            logger.bind(sensitive=True).warning("Bond verification detail: {}", e)
            return None
        if len(results) != len(requests):
            logger.warning(
                f"Bond verification returned {len(results)} results for {len(requests)} requests"
            )
            return None

        current_time = int(time.time())
        claim_values: dict[tuple[str, int, str], int] = {}

        for request, result in zip(requests, results, strict=True):
            if (result.txid, result.vout) != (request.txid, request.vout):
                logger.warning(
                    f"Bond verification result mismatch: requested "
                    f"{request.txid}:{request.vout}, received {result.txid}:{result.vout}"
                )
                continue
            if not result.valid:
                claim_key = (request.txid, request.vout, request.scriptpubkey)
                for offer in claim_to_offers[claim_key]:
                    offer.fidelity_bond_verified = False
                logger.bind(sensitive=True).debug(
                    "Bond {}:{} invalid: {}", result.txid, result.vout, result.error
                )
                continue

            claim_key = (request.txid, request.vout, request.scriptpubkey)
            for offer in claim_to_offers[claim_key]:
                offer.fidelity_bond_verified = True

            bond_value = calculate_timelocked_fidelity_bond_value(
                utxo_value=result.value,
                confirmation_time=result.block_time,
                locktime=request.locktime,
                current_time=current_time,
            )

            if bond_value > 0:
                claim_values[claim_key] = bond_value

        # Update only offers whose certificate and proof data were eligible.
        updated_count = 0
        for claim_key, bond_value in claim_values.items():
            for offer in claim_to_offers[claim_key]:
                offer.fidelity_bond_value = bond_value
                updated_count += 1

        logger.info(f"Updated {updated_count} offers with verified fidelity bond values")
        return current_block_height

    def _drain_and_apply_market_faults(
        self, offers: list[Offer], current_block_height: int | None
    ) -> list[Offer]:
        """Ingest directory evidence and hard-remove only exact verified bond matches."""
        if not self.config.market_fault_exclusion:
            return offers
        for client in self.directory_client.clients.values():
            for proof in client.drain_market_faults():
                self.market_fault_cache.ingest(proof)
        if current_block_height is None:
            return offers
        excluded_nicks = self.market_fault_cache.excluded_nicks(
            offers,
            network=(self.config.bitcoin_network or self.config.network).value,
            height=current_block_height,
        )
        if not excluded_nicks:
            return offers
        logger.warning(
            "Hard-excluding {} maker nick(s) with verified market fault evidence",
            len(excluded_nicks),
        )
        return [offer for offer in offers if offer.counterparty not in excluded_nicks]

    def _log_initial_maker_fee_plan(self, fee_plan: dict[str, int]) -> None:
        """Explain the opt-in fee equalization policy before makers are contacted."""
        if not self.config.equalize_cj_fees or not fee_plan:
            return

        target_fee = max(fee_plan.values())
        bumped_count = sum(
            fee_plan[nick]
            > calculate_cj_fee(
                session.offer,
                self._session.cj_amount,
                self.config.round_up_cj_fees,
            )
            for nick, session in self._session.maker_sessions.items()
        )
        logger.warning(
            "Maker fee equalization is enabled; legacy makers that require exact fee "
            "payments may refuse to sign, causing this CoinJoin attempt to fail"
        )
        logger.info(
            f"Initial equalized maker fee: {target_fee:,} sats each; "
            f"{bumped_count}/{len(fee_plan)} maker payments increased"
        )

    def _ring_request_error(
        self, amount: int, maker_count: int, buyout: ChannelBuyout | None
    ) -> str | None:
        if not self.config.channel_ring.enabled:
            return None
        self._session.strict_maker_count = maker_count
        minimum = max(self.config.minimum_makers, self._ring_maker_floor())
        if buyout is not None:
            return RING_BUYOUT_CONFLICT
        if amount == 0:
            return "Channel-ring mode rejects sweep/no-change CoinJoins"
        if maker_count < minimum:
            return f"Channel-ring mode requires at least {minimum} selected makers"
        return None

    def _enforce_ring_request(
        self, amount: int, maker_count: int, buyout: ChannelBuyout | None = None
    ) -> None:
        error = self._ring_request_error(amount, maker_count, buyout)
        if error is None:
            return
        self._session.last_failure_reason = error
        raise ValueError(error)

    def _ring_maker_floor(self) -> int:
        """Keep at least three channel endpoints without guessing maker roles."""
        return max(
            self.config.channel_ring.minimum_makers,
            2 if self.config.channel_ring.taker_joins else 3,
        )

    def _select_channel_ring_makers(
        self,
        offers: list[Offer],
        maker_count: int,
        required_features: set[str] | None,
        *,
        ring_slots: int | None = None,
        hard_exclude_nicks: set[str] | None = None,
        exclude_nicks: set[str] | None = None,
        penalized_maker_keys: set[str] | None = None,
    ) -> tuple[dict[str, Offer], int]:
        """Reserve ring and ordinary slots using the normal orderbook chooser.

        Select as many eligible non-ignored ring makers as fit the requested
        total. Only relax soft exclusions to reach the ring minimum; do not
        prefer an ignored ring maker over a usable ordinary maker. Then fill
        remaining slots from the combined orderbook.
        """
        if self.config.channel_ring.enabled is True and (
            self._session.ring_maker_sessions or self._session.ordinary_maker_sessions
        ):
            raise ValueError(
                "Channel-ring maker roles are frozen; replacements are no longer allowed"
            )
        if self.config.channel_ring.enabled is not True:
            return self.orderbook_manager.select_makers(
                cj_amount=self._session.cj_amount,
                n=maker_count,
                required_features=required_features,
                hard_exclude_nicks=hard_exclude_nicks,
                exclude_nicks=exclude_nicks,
                penalized_maker_keys=penalized_maker_keys,
            )
        ring_slots = self._ring_maker_floor() if ring_slots is None else ring_slots
        remaining_slots = maker_count - ring_slots
        if remaining_slots < 0:
            raise ValueError("Channel-ring request has fewer makers than required ring slots")
        base_hard_exclude = set(hard_exclude_nicks or ())
        ring_pool = [
            offer
            for offer in offers
            if offer_supports_private_channel_ring(offer)
            and offer.ordertype is self.config.preferred_offer_type
        ]

        def select_from(
            pool: list[Offer], count: int, hard: set[str]
        ) -> tuple[dict[str, Offer], int]:
            original = self.orderbook_manager.offers
            self.orderbook_manager.offers = pool
            try:
                return self.orderbook_manager.select_makers(
                    cj_amount=self._session.cj_amount,
                    n=count,
                    required_features=required_features,
                    hard_exclude_nicks=hard,
                    exclude_nicks=exclude_nicks,
                    penalized_maker_keys=penalized_maker_keys,
                )
            finally:
                self.orderbook_manager.offers = original

        soft_exclude = self.orderbook_manager.ignored_makers | set(exclude_nicks or ())
        preferred_ring_pool = [
            offer for offer in ring_pool if offer.counterparty not in soft_exclude
        ]
        ring, ring_fee = select_from(preferred_ring_pool, maker_count, base_hard_exclude)
        if len(ring) < ring_slots:
            ring, ring_fee = select_from(ring_pool, ring_slots, base_hard_exclude)
        if len(ring) < ring_slots:
            raise ValueError("Not enough private_channel_ring makers for reserved ring slots")
        remaining_slots = maker_count - len(ring)
        network = (self.config.bitcoin_network or self.config.network).value
        ring_bonds = {
            bond
            for bond in (verified_bond_key(offer, network) for offer in ring.values())
            if bond is not None
        }
        remaining_pool = [
            offer
            for offer in offers
            if offer.ordertype is self.config.preferred_offer_type
            and offer.counterparty not in ring
            and verified_bond_key(offer, network) not in ring_bonds
        ]
        remaining, remaining_fee = select_from(
            remaining_pool, remaining_slots, base_hard_exclude | set(ring)
        )
        if len(remaining) != remaining_slots:
            raise ValueError("Not enough makers for the remaining channel-ring slots")
        return {**ring, **remaining}, ring_fee + remaining_fee

    def _ring_replacement_slots(self, needed: int) -> int:
        """Return how many pending replacement slots must remain ring-capable."""
        if self.config.channel_ring.enabled is not True:
            return 0
        active_ring = set(self._session.maker_sessions) & self._session.ring_candidate_nicks
        missing_ring = self._ring_maker_floor() - len(active_ring)
        return min(needed, max(0, missing_ring))

    def _drop_capability_flipped_ring_candidates(self) -> set[str]:
        """Discard stale ring candidates before roles are frozen."""
        if self.config.channel_ring.enabled is not True:
            return set()
        dropped = {
            nick
            for nick in self._session.ring_candidate_nicks & set(self._session.maker_sessions)
            if not offer_supports_private_channel_ring(self._session.maker_sessions[nick].offer)
        }
        for nick in dropped:
            self._session.remove_maker_session(nick)
        return dropped

    async def _prepare_channel_ring(self, destination: str, mixdepth: int) -> bool:
        if self._channel_ring_nodes is None or self._channel_ring_store is None:
            self._session.last_failure_reason = "Channel-ring backend was not safely initialized"
            logger.error(self._session.last_failure_reason)
            return False
        self.state = TakerState.RING_INVITING
        assert self._session.podle_commitment is not None
        session_identity = f"{self.nick}:{self._session.podle_commitment.to_commitment_str()}"
        if self.config.channel_ring.taker_joins:
            from taker.channel_ring import TakerRingCoordinator

            coordinator = TakerRingCoordinator(
                self._session,
                config=self.config.channel_ring,
                store=self._channel_ring_store,
                initialized_backend=self._channel_ring_nodes.for_mixdepth(mixdepth),
                chain_backend=self.backend,
                session_identity=session_identity,
            )
        else:
            from taker.channel_ring_maker_only import MakerOnlyRingCoordinator

            if self._channel_ring_coordinator_store is None:
                raise RuntimeError("Maker-only ring coordinator journal was not initialized")
            coordinator = MakerOnlyRingCoordinator(
                self._session,
                config=self.config.channel_ring,
                store=self._channel_ring_coordinator_store,
                chain_backend=self.backend,
                session_identity=session_identity,
            )
        self._session.ring_coordinator = coordinator
        if await coordinator.prepare(destination, mixdepth):
            return True
        self._session.last_failure_reason = "Strict channel-ring negotiation aborted"
        return False

    def _initial_maker_details(self, fee_plan: dict[str, int]) -> list[dict[str, Any]]:
        """Snapshot the offer and directory details shown at initial confirmation."""
        details: list[dict[str, Any]] = []
        for nick, session in self._session.maker_sessions.items():
            # Preserve the directory's original first-serving-peer ordering.
            # If none serve, the last observed location is shown as before.
            location = None
            for client in self.directory_client.clients.values():
                location = client._active_peers.get(nick)
                if not location or location == "NOT-SERVING-ONION":
                    continue
                break
            details.append(
                {
                    "nick": nick,
                    "fee": fee_plan[nick],
                    "advertised_fee": calculate_cj_fee(
                        session.offer, self._session.cj_amount, False
                    ),
                    "bond_value": session.offer.fidelity_bond_value,
                    "location": location,
                }
            )
        return details

    async def _build_transaction_phase(self, destination: str, mixdepth: int) -> bool:
        if self.config.channel_ring.enabled:
            return await self._prepare_channel_ring(destination, mixdepth)
        self.state = TakerState.BUILDING_TX
        logger.debug("Phase 3: Building transaction...")
        return await self._session._phase_build_tx(destination=destination, mixdepth=mixdepth)

    async def do_coinjoin(
        self,
        amount: int,
        destination: str,
        mixdepth: int | None = None,
        counterparty_count: int | None = None,
        exclude_nicks: set[str] | None = None,
        input_utxos: list[str] | None = None,
        penalized_maker_keys: set[str] | None = None,
        buyout: ChannelBuyout | None = None,
    ) -> str | None:
        """Run one CoinJoin with fresh, non-reusable per-round state."""
        if self._round_lock.locked():
            logger.error("A CoinJoin round is already active on this Taker instance")
            return None

        async with self._round_lock:
            session = CoinJoinSession()
            session.attach(self)
            if buyout is not None:
                if self.config.channel_ring.enabled:
                    raise ValueError(RING_BUYOUT_CONFLICT)
                if (
                    amount <= 0
                    or offer_output_script_type(self.config.preferred_offer_type) != "p2tr"
                ):
                    raise ValueError("A channel buyout requires a non-sweep Taproot CoinJoin")
                if (
                    buyout.terms.proposal.network
                    != (self.config.bitcoin_network or self.config.network).value
                ):
                    raise ValueError("Buyout and CoinJoin Bitcoin networks differ")
                session.buyout = buyout
            self._session = session
            self.state = TakerState.IDLE
            return await self._do_coinjoin(
                amount=amount,
                destination=destination,
                mixdepth=mixdepth,
                counterparty_count=counterparty_count,
                exclude_nicks=exclude_nicks,
                penalized_maker_keys=penalized_maker_keys,
                input_utxos=input_utxos,
            )

    async def _do_coinjoin(
        self,
        amount: int,
        destination: str,
        mixdepth: int | None = None,
        counterparty_count: int | None = None,
        exclude_nicks: set[str] | None = None,
        input_utxos: list[str] | None = None,
        penalized_maker_keys: set[str] | None = None,
    ) -> str | None:
        """
        Execute a single CoinJoin transaction.

        Args:
            amount: Amount in satoshis (0 for sweep)
            destination: Destination address ("INTERNAL" for next mixdepth)
            mixdepth: Source mixdepth. ``None`` means: derive it from the
                interactive UTXO selection when ``select_utxos`` is enabled
                (the first selected UTXO pins the mixdepth), otherwise fall
                back to mixdepth 0.
            counterparty_count: Number of makers (default from config)
            exclude_nicks: Additional maker nicks to exclude from selection
                (on top of ``orderbook_manager.ignored_makers`` and
                ``own_wallet_nicks``).
            input_utxos: Optional exact ``txid:vout`` input set. Explicit
                inputs are never expanded with other wallet UTXOs.
            penalized_maker_keys: Recent maker nick and bond identity keys to
                probabilistically penalize without excluding them. The tumbler
                uses this for one-phase counterparty diversity.

        Returns:
            Transaction ID if successful, None otherwise
        """
        try:
            self._begin_input_lock_round()
            # Reset per-call state so callers reading ``last_used_nicks`` after
            # a failure don't pick up nicks from a previous successful round.
            self._session.last_used_nicks = set()
            self._session.last_used_maker_keys = set()
            # When the caller does not pin a counterparty count, fall back to
            # the configured value (which may itself be ``None`` to request a
            # random draw from the upstream-aligned [8, 10] range).
            self._session.last_failure_reason = None

            # Re-read maker nick state on every coinjoin attempt.  The maker
            # may have been started *after* this Taker was constructed (common
            # in tumbler runs), so the nick read at __init__ time would be
            # stale.  Refreshing here ensures the hard exclusion is always
            # current regardless of startup order.
            current_maker_nick = read_nick_state(self.config.data_dir, self._maker_nick_component)
            if current_maker_nick:
                if current_maker_nick not in self.orderbook_manager.own_wallet_nicks:
                    logger.bind(sensitive=True).info(
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
            self._session.maker_target_count = n_makers
            self._enforce_ring_request(amount, n_makers, self._session.buyout)

            # Resolve explicit or interactive input requests before orderbook
            # and bond work, then fail fast if the requested source cannot fund
            # a CoinJoin.
            requested_inputs = await self._prepare_requested_input_selection(
                amount,
                mixdepth,
                input_utxos,
            )
            if requested_inputs is None:
                return None
            explicitly_selected_utxos, manually_selected_utxos, mixdepth = requested_inputs
            if self._session.buyout is not None:
                binding = self._session.buyout.buyer.runtime_binding
                binding_mixdepth = binding.get("mixdepth")
                if type(binding_mixdepth) is not int or binding_mixdepth != mixdepth:
                    self._session.last_failure_reason = (
                        "Buyout LND binding does not match the wallet source mixdepth"
                    )
                    logger.error(self._session.last_failure_reason)
                    return None
                fingerprint = binding.get("wallet_fingerprint")
                if (
                    not isinstance(fingerprint, str)
                    or not fingerprint
                    or fingerprint != self.wallet.wallet_fingerprint
                ):
                    self._session.last_failure_reason = (
                        "Buyout LND binding does not match the wallet identity"
                    )
                    logger.error(self._session.last_failure_reason)
                    return None
                # Input selection (including interactive selection) is complete.
                # Refuse a cross-mixdepth buyout before reserving its journal or
                # revealing a PoDLE commitment to the makers.
                self._session.buyout.begin_round()

            # Determine destination address
            if destination == "INTERNAL":
                dest_mixdepth = (mixdepth + 1) % self.wallet.mixdepth_count
                # Use internal chain (/1) for CoinJoin outputs, not external (/0)
                # This matches the reference implementation behavior where all JM-generated
                # addresses (CJ outputs and change) use the internal branch
                destination = self.wallet.get_new_internal_address(dest_mixdepth)
                logger.bind(sensitive=True).info("Using internal address: {}", destination)
            else:
                # Warn when the user-supplied destination does not match the
                # wallet's native script type (#113).
                warn_if_destination_script_mismatch(destination)

            # Resolve fee rate early (before any fee estimation calls)
            try:
                await self._session._resolve_fee_rate()
            except ValueError as e:
                logger.error("Unable to resolve CoinJoin fee rate")
                logger.bind(sensitive=True).error("CoinJoin fee rate error: {}", e)
                self._session.last_failure_reason = str(e)
                self.state = TakerState.FAILED
                return None

            # Track if this is a sweep (no change) transaction
            self._session.is_sweep = amount == 0

            # UTXO selection (interactive or automatic) is done before fetching
            # the orderbook to avoid wasting the user's time on a doomed round.
            # Now fetch orderbook after UTXO selection is done
            self.state = TakerState.FETCHING_ORDERBOOK
            logger.debug("Fetching orderbook...")
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
            if self.backend.requires_neutrino_metadata():
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
                logger.bind(sensitive=True).info(
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

            # Verify bonds once, then apply any independently matching fault
            # proof before this offer set can enter selection or replacements.
            current_block_height = await self._update_offers_with_bond_values(offers)
            offers = self._drain_and_apply_market_faults(offers, current_block_height)

            self.orderbook_manager.update_offers(offers)

            if len(offers) < n_makers:
                reason = f"Not enough offers: need {n_makers}, found {len(offers)}"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None

            if self.backend.requires_neutrino_metadata():
                logger.info(
                    "Neutrino backend: requiring neutrino_compat in offer filtering, "
                    "will also negotiate during handshake"
                )

            self.state = TakerState.SELECTING_MAKERS

            def get_private_key(addr: str) -> bytes | None:
                key = self.wallet.get_key_for_address(addr)
                if key is None:
                    return None
                return key.get_private_key_bytes()

            if self._session.is_sweep:
                # SWEEP MODE: Select ALL UTXOs and calculate exact cj_amount for zero change
                logger.info("Sweep mode: selecting UTXOs from mixdepth")

                # Use explicitly or manually selected UTXOs when available;
                # otherwise get all eligible UTXOs from the mixdepth.
                if explicitly_selected_utxos is not None:
                    self._session.preselected_utxos = explicitly_selected_utxos
                    logger.bind(sensitive=True).info(
                        f"Sweep using exactly {len(explicitly_selected_utxos)} explicit "
                        "input UTXO(s)"
                    )
                elif manually_selected_utxos:
                    self._session.preselected_utxos = manually_selected_utxos
                    logger.bind(sensitive=True).info(
                        f"Sweep using {len(manually_selected_utxos)} manually selected UTXOs "
                        f"(--select-utxos was used)"
                    )
                else:
                    # Get ALL UTXOs from the mixdepth (default sweep behavior)
                    locked_inputs = self.wallet.get_locked_input_outpoints()
                    self._session.preselected_utxos = self.wallet.get_all_utxos(
                        mixdepth,
                        self.config.taker_utxo_age,
                        exclude=locked_inputs,
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
                logger.bind(sensitive=True).info(
                    f"Sweep: {len(self._session.preselected_utxos)} UTXOs, "
                    f"total value: {total_input_value:,} sats"
                )

                if self.config.max_maker_utxos <= 0:
                    reason = (
                        "Sweep CoinJoins require max_maker_utxos to be greater than 0 so the "
                        "minimum miner fee rate can be guaranteed before maker inputs are known."
                    )
                    logger.error(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.FAILED
                    return None

                # Budget for every maker input the authenticated session will
                # accept. This keeps the fixed sweep amount relayable even when
                # all makers contribute the configured maximum input count.
                estimated_inputs, estimated_outputs = _estimate_initial_tx_shape(
                    len(self._session.preselected_utxos),
                    n_makers,
                    is_sweep=True,
                    max_maker_utxos=self.config.max_maker_utxos,
                )
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

                # The sweep amount is only known after selection, so the
                # credential is validated against the total input value.
                await self._prepare_external_podle(total_input_value)

                # Use sweep order selection - this calculates exact cj_amount for zero change
                selected_offers, self._session.cj_amount, total_fee = (
                    self.orderbook_manager.select_makers_for_sweep(
                        total_input_value=total_input_value,
                        my_txfee=estimated_tx_fee,
                        n=n_makers,
                        required_features=required_features,
                        exclude_nicks=exclude_nicks,
                        hard_exclude_nicks=self._seller_separation.excluded_nicks(),
                        penalized_maker_keys=penalized_maker_keys,
                    )
                )
                self._seller_separation.record_offered_makers(selected_offers.values())

                if len(selected_offers) < self.config.minimum_makers:
                    reason = f"Not enough makers for sweep: {len(selected_offers)}"
                    logger.error(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.FAILED
                    return None

                logger.bind(sensitive=True).info(
                    f"Sweep: cj_amount={self._session.cj_amount:,} sats calculated for zero change"
                )
            else:
                # NORMAL MODE: Select minimum UTXOs needed
                self._session.cj_amount = amount
                logger.bind(sensitive=True).info(
                    "Selecting {} makers for {:,} sats...", n_makers, self._session.cj_amount
                )

                await self._prepare_external_podle(self._session.cj_amount)

                selected_offers, total_fee = self._select_channel_ring_makers(
                    offers,
                    n_makers,
                    required_features,
                    exclude_nicks=exclude_nicks,
                    hard_exclude_nicks=self._seller_separation.excluded_nicks(),
                    penalized_maker_keys=penalized_maker_keys,
                )
                self._seller_separation.record_offered_makers(selected_offers.values())

                if len(selected_offers) < self.config.minimum_makers:
                    reason = f"Not enough makers selected: {len(selected_offers)}"
                    logger.error(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.FAILED
                    return None
                if self.config.channel_ring.enabled and len(selected_offers) != n_makers:
                    raise ValueError(
                        "Channel-ring selection did not return the exact requested maker set"
                    )

                # Pre-select UTXOs for CoinJoin, then generate PoDLE from one of them
                # This ensures the PoDLE UTXO is one we'll actually use in the transaction
                logger.info("Selecting UTXOs and generating PoDLE commitment...")

                # Use explicitly or manually selected UTXOs if available.
                if explicitly_selected_utxos is not None:
                    self._session.preselected_utxos = explicitly_selected_utxos
                    logger.bind(sensitive=True).info(
                        f"Using exactly {len(explicitly_selected_utxos)} explicit input UTXO(s) "
                        f"(total: {sum(u.value for u in explicitly_selected_utxos):,} sats)"
                    )
                elif manually_selected_utxos:
                    self._session.preselected_utxos = manually_selected_utxos
                    logger.bind(sensitive=True).info(
                        f"Using {len(manually_selected_utxos)} manually selected UTXOs "
                        f"(total: {sum(u.value for u in manually_selected_utxos):,} sats)"
                    )
                else:
                    # Estimate required amount (conservative estimate for UTXO pre-selection)
                    # We'll refine this in _phase_build_tx once we have exact maker UTXOs
                    estimated_inputs = 2 + len(selected_offers) * 2  # Rough estimate
                    estimated_inputs += self._session.buyout_input_count
                    estimated_outputs = 2 + len(selected_offers) * 2
                    estimated_tx_fee = self._session._estimate_tx_fee(
                        estimated_inputs, estimated_outputs
                    )
                    estimated_required = self._session.cj_amount + total_fee + estimated_tx_fee
                    estimated_required = self._session.wallet_funding_required(estimated_required)

                    # Pre-select UTXOs for the CoinJoin, skipping any inputs
                    # locked by another in-flight round (this or another process
                    # on the same wallet) so we don't build a conflicting tx.
                    locked_inputs = self.wallet.get_locked_input_outpoints()
                    try:
                        self._session.preselected_utxos = self._select_coinjoin_utxos_with_podle(
                            mixdepth,
                            estimated_required,
                            get_private_key,
                            locked_inputs,
                        )
                        preselected = self._session.preselected_utxos
                        logger.bind(sensitive=True).info(
                            f"Pre-selected {len(preselected)} UTXOs for CoinJoin "
                            f"(total: {sum(u.value for u in preselected):,} sats)"
                        )
                    except ValueError as e:
                        reason = _append_confirmation_hint(str(e), self.config.taker_utxo_age)
                        logger.error("Unable to pre-select CoinJoin inputs")
                        logger.bind(sensitive=True).error(
                            "CoinJoin input selection detail: {}", reason
                        )
                        self._session.last_failure_reason = reason
                        self.state = TakerState.FAILED
                        return None

            # "Block first, then continue": persist a lock on our chosen inputs
            # before negotiating with makers, so a concurrent round on the same
            # wallet cannot pick the same UTXO and produce a conflicting
            # transaction. The lock auto-expires (and is released on failure),
            # so a crash never blocks these funds permanently.
            to_reserve = {(u.txid, u.vout) for u in self._session.preselected_utxos}
            if not self.wallet.reserve_coinjoin_inputs(
                to_reserve,
                ttl=self._session.input_lock_ttl_sec(),
                owner=self._session.input_lock_owner,
            ):
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
            self._session.ring_candidate_nicks = {
                nick
                for nick, offer in selected_offers.items()
                if offer_supports_private_channel_ring(offer)
            }
            initial_fee_plan = self._session.maker_fee_plan()
            total_fee = sum(initial_fee_plan.values())

            logger.bind(sensitive=True).info(
                f"Selected {len(self._session.maker_sessions)} makers, "
                f"total fee: {total_fee:,} sats"
            )
            self._log_initial_maker_fee_plan(initial_fee_plan)

            # Log the same estimate used by the transaction calculations. Sweep
            # amounts commit to the deterministic budget before !fill, while
            # normal CoinJoins use the session's randomized fee rate.
            (
                estimated_inputs,
                estimated_outputs,
                estimated_tx_fee,
                estimated_fee_rate,
            ) = _initial_fee_confirmation_values(
                self._session,
                len(self._session.preselected_utxos),
                n_makers,
                self.config.max_maker_utxos,
            )
            logger.bind(sensitive=True).info(
                f"Estimated transaction (mining) fee: {estimated_tx_fee:,} sats "
                f"(~{estimated_fee_rate:.2f} sat/vB for ~{estimated_inputs} inputs, "
                f"{estimated_outputs} outputs)"
            )

            # Prompt for confirmation after maker selection
            if hasattr(self, "confirmation_callback") and self.confirmation_callback:
                try:
                    maker_details = self._initial_maker_details(initial_fee_plan)

                    confirmation_timeout = float(self.config.initial_confirmation_timeout_sec)
                    confirmed = await self._request_confirmation(
                        timeout=confirmation_timeout if confirmation_timeout > 0 else None,
                        maker_details=maker_details,
                        cj_amount=self._session.cj_amount,
                        total_fee=total_fee + estimated_tx_fee,
                        destination=destination,
                        mining_fee=estimated_tx_fee,
                        fee_rate=estimated_fee_rate,
                        stage="initial",
                    )
                    if not confirmed:
                        logger.info("CoinJoin cancelled by user")
                        self.state = TakerState.CANCELLED
                        return None
                except TimeoutError:
                    reason = (
                        "Initial CoinJoin confirmation expired after "
                        f"{self.config.initial_confirmation_timeout_sec} seconds; "
                        "start a fresh CoinJoin to fetch current offers."
                    )
                    logger.warning(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.CANCELLED
                    return None
                except Exception as e:
                    logger.error("CoinJoin confirmation failed")
                    logger.bind(sensitive=True).error("CoinJoin confirmation error detail: {}", e)
                    self.state = TakerState.FAILED
                    return None

            self._session.podle_commitment = await self._allocate_podle_commitment(
                self._session.preselected_utxos,
                get_private_key,
            )

            if not self._session.podle_commitment:
                reason = "Failed to generate PoDLE commitment"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None
            self._activate_coinjoin_log_context()

            max_replacement_attempts = self.config.max_maker_replacement_attempts
            if not await self._run_fill_with_replacements(
                destination=destination,
                selected_offers=selected_offers,
                required_features=required_features,
                mixdepth=mixdepth,
                get_private_key=get_private_key,
                max_replacement_attempts=max_replacement_attempts,
                penalized_maker_keys=penalized_maker_keys,
            ):
                return None

            if not await self._run_auth_with_replacements(
                required_features=required_features,
                get_private_key=get_private_key,
                max_replacement_attempts=max_replacement_attempts,
                penalized_maker_keys=penalized_maker_keys,
            ):
                return None

            # Phase 3: Build transaction
            if not await self._build_transaction_phase(destination, mixdepth):
                logger.error("Transaction build or channel-ring preparation failed")
                self.state = TakerState.FAILED
                return None

            # Phase 4: Collect signatures
            self.state = TakerState.COLLECTING_SIGNATURES
            logger.debug("Phase 4: Collecting signatures...")

            sig_success = await self._session._phase_collect_signatures()
            if not sig_success:
                if self.config.equalize_cj_fees and (
                    self._session.failed_signer_nicks or self._session.declined_signer_nicks
                ):
                    logger.warning(
                        "An equalized-fee CoinJoin was rejected or left unsigned; a legacy "
                        "maker that requires its exact advertised fee may be incompatible. "
                        "Start a new CoinJoin round to select replacements."
                    )
                    incompatible_nicks = (
                        self._session.failed_signer_nicks | self._session.declined_signer_nicks
                    )
                    logger.bind(sensitive=True).warning(
                        "Possible equalized-fee incompatible makers: {}",
                        ", ".join(sorted(incompatible_nicks)),
                    )
                for nick in self._session.failed_signer_nicks:
                    self.orderbook_manager.add_ignored_maker(nick)
                logger.error("Signature collection failed")
                self.state = TakerState.FAILED
                return None

            return await self._finalize_and_broadcast(destination)

        except Exception as e:
            logger.error("CoinJoin failed")
            logger.bind(sensitive=True).error("CoinJoin failure detail: {}", e)
            # Fire-and-forget notification for failed CoinJoin
            phase = self.state.value if hasattr(self, "state") else ""
            amount = self._session.cj_amount
            spawn_task(
                get_notifier().notify_coinjoin_failed(
                    str(e), phase, amount, self._current_coinjoin_id()
                )
            )
            self.state = TakerState.FAILED
            return None
        finally:
            # Before local taker signing, failure cannot produce a transaction
            # that spends our inputs, so locks can be released. At or after the
            # signing boundary, renew through the pending window because
            # cancellation, confirmation decline, or a failed broadcast can
            # leave a usable transaction.
            if self.state != TakerState.COMPLETE:
                self.release_input_locks()
            self._clear_coinjoin_log_context()

    async def _maybe_select_utxos_interactively(
        self, amount: int, mixdepth: int | None
    ) -> list[UTXOInfo] | None:
        """Run the interactive UTXO selector across the whole wallet.

        All mixdepths are displayed for a full-wallet overview. When
        ``mixdepth`` is ``None`` the user may select from any mixdepth (the
        TUI pins the source mixdepth to the first selected UTXO); when set,
        only that mixdepth is selectable and the rest is context.
        """
        if not self.config.select_utxos:
            logger.debug("Interactive UTXO selection not requested (--select-utxos not set)")
            return None

        from jmwallet.utxo_selector import select_utxos_interactive

        try:
            # Get ALL UTXOs (all mixdepths, including frozen/immature ones)
            # for display in the interactive selector. Ineligible UTXOs are
            # shown but rendered as unselectable ([-]) so the user sees the
            # full picture of their wallet.
            min_age = self.config.taker_utxo_age
            available_utxos: list[UTXOInfo] = []
            for md in range(self.wallet.mixdepth_count):
                available_utxos.extend(await self.wallet.get_utxos(md))
            if not available_utxos:
                reason = "No UTXOs in wallet"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None

            # Check that at least some UTXOs are selectable (confirmed
            # enough, not frozen/locked, and in the pinned mixdepth if any).
            candidates = (
                available_utxos
                if mixdepth is None
                else [u for u in available_utxos if u.mixdepth == mixdepth]
            )
            locked_inputs = self.wallet.get_locked_input_outpoints()
            if self.config.channel_ring.enabled and self.config.channel_ring.taker_joins:
                locked_inputs = locked_inputs | {
                    (utxo.txid, utxo.vout)
                    for utxo in available_utxos
                    if utxo.mixdepth not in self.config.channel_ring.mixdepth_nodes
                }
            if not selectable_for_interactive(
                candidates, min_age, excluded_outpoints=locked_inputs
            ):
                where = "wallet" if mixdepth is None else f"mixdepth {mixdepth}"
                reason = (
                    f"No eligible UTXOs in {where} "
                    f"(all {len(candidates)} UTXOs are frozen, immature, locked, or in use)"
                )
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return None

            # Populate only unlabeled non-bond UTXOs from wallet internals.
            # Preserve BIP-329 user labels and fidelity-bond labels.
            for utxo in available_utxos:
                if utxo.label is None and not utxo.is_fidelity_bond:
                    utxo.label = self.wallet.get_utxo_label_from_wallet(utxo.address)

            logger.bind(sensitive=True).info(
                f"Launching interactive UTXO selector ({len(available_utxos)} available, "
                f"target amount: {amount} sats, sweep: {amount == 0})..."
            )
            manually_selected_utxos = select_utxos_interactive(
                available_utxos,
                amount,
                allowed_mixdepth=mixdepth,
                min_confirmations=min_age,
                excluded_outpoints=locked_inputs,
            )

            if not manually_selected_utxos:
                logger.info("UTXO selection cancelled by user")
                self.state = TakerState.CANCELLED
                return None

            total_selected = sum(u.value for u in manually_selected_utxos)
            logger.bind(sensitive=True).info(
                f"Manually selected {len(manually_selected_utxos)} UTXOs "
                f"(total: {total_selected:,} sats)"
            )

            # Validate selected UTXOs have sufficient funds (for non-sweep)
            if amount > 0 and total_selected < self._session.wallet_funding_required(amount):
                logger.error("Selected UTXOs have insufficient funds")
                logger.bind(sensitive=True).error(
                    "Selected UTXO funding detail: have {:,} sats, need at least {:,} sats",
                    total_selected,
                    amount,
                )
                self.state = TakerState.FAILED
                return None
        except RuntimeError as e:
            logger.error("Interactive UTXO selection failed")
            logger.bind(sensitive=True).error("Interactive UTXO selection detail: {}", e)
            self.state = TakerState.FAILED
            return None

        return manually_selected_utxos

    async def _run_auth_with_replacements(
        self,
        required_features: set[str] | None,
        get_private_key: Any,
        max_replacement_attempts: int,
        penalized_maker_keys: set[str] | None = None,
    ) -> bool:
        self.state = TakerState.AUTHENTICATING
        logger.debug("Phase 2: Sending !auth and receiving !ioauth...")

        auth_replacement_attempt = 0
        # Nicks that failed or could not be verified during this auth stage are
        # hard-excluded from re-selection so no auth session or PoDLE proof is replayed.
        failed_nicks: set[str] = set()
        target_makers = max(self._session.maker_target_count, self.config.minimum_makers)
        while True:
            auth_result = await self._session._phase_auth()

            flipped = self._drop_capability_flipped_ring_candidates()
            if flipped:
                auth_result.failed_makers.extend(sorted(flipped))

            current_makers = len(self._session.maker_sessions)
            if auth_result.success and current_makers >= target_makers:
                return True

            for failed_nick in auth_result.failed_makers:
                self.orderbook_manager.add_ignored_maker(failed_nick)
                failed_nicks.add(failed_nick)
                logger.debug(f"Added {failed_nick} to ignored makers (failed auth)")
            for unavailable_nick in auth_result.unavailable_makers:
                failed_nicks.add(unavailable_nick)
                logger.debug(
                    f"Hard-excluded {unavailable_nick} for this round "
                    "(UTXO verification unavailable)"
                )

            # A successful floor-level auth still needs target restoration. A
            # failed auth can only be recovered when it identified makers to replace.
            if not auth_result.success and not auth_result.needs_replacement:
                reason = "Auth phase failed without replaceable makers"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return False

            added_replacements = False
            replacement_commitment_ready = not auth_result.podle_revealed
            while (
                current_makers < target_makers
                and auth_replacement_attempt < max_replacement_attempts
            ):
                auth_replacement_attempt += 1
                needed = target_makers - current_makers
                logger.info(
                    f"Attempting maker replacement in auth phase "
                    f"(attempt {auth_replacement_attempt}/{max_replacement_attempts}): "
                    f"need {needed} more makers"
                )

                current_session_nicks = set(self._session.maker_sessions.keys())
                replacement_offers, _ = self._select_channel_ring_makers(
                    self.orderbook_manager.offers,
                    needed,
                    required_features,
                    ring_slots=self._ring_replacement_slots(needed),
                    hard_exclude_nicks=(
                        current_session_nicks
                        | failed_nicks
                        | self._seller_separation.excluded_nicks()
                    ),
                    penalized_maker_keys=penalized_maker_keys,
                )

                if not replacement_offers:
                    break

                # These candidates are about to be contacted, so a commitment
                # rotated below must not come from any of their bonds.
                self._seller_separation.record_offered_makers(replacement_offers.values())

                if len(replacement_offers) < needed:
                    logger.info(
                        f"Auth replacement selection is partial: found {len(replacement_offers)}, "
                        f"need {needed}; retrying the remaining deficit after mini-fill"
                    )

                if not replacement_commitment_ready:
                    if not await self._rotate_commitment_for_auth_replacement(get_private_key):
                        break
                    replacement_commitment_ready = True

                before_mini_fill = current_makers
                if not await self._fill_replacement_makers(replacement_offers, failed_nicks):
                    reason = "Auth replacement mini-fill could not initialize"
                    logger.error(reason)
                    self._session.last_failure_reason = reason
                    self.state = TakerState.FAILED
                    return False
                current_makers = len(self._session.maker_sessions)
                added_replacements = added_replacements or current_makers > before_mini_fill

            # Replacement makers cannot be final survivors until this next auth
            # pass has verified their !ioauth responses.
            if added_replacements:
                continue

            if auth_result.success and current_makers >= self._session.required_maker_count:
                logger.warning(
                    f"Auth replacement attempts exhausted or no candidates remain; proceeding "
                    f"with {current_makers}/{target_makers} makers "
                    f"(minimum {self._session.required_maker_count})"
                )
                return True

            reason = (
                f"Auth phase failed: only {current_makers} authenticated makers remain, "
                f"below minimum {self._session.required_maker_count}"
            )
            logger.error(reason)
            self._session.last_failure_reason = reason
            self.state = TakerState.FAILED
            return False

    async def _rotate_commitment_for_auth_replacement(self, get_private_key: Any) -> bool:
        """Prepare a fresh PoDLE proof after an auth-stage commitment disclosure."""
        if any(
            not maker_session.responded_auth
            for maker_session in self._session.maker_sessions.values()
        ):
            logger.error("Cannot rotate PoDLE while an unauthenticated maker session remains")
            return False

        new_commitment = await self._allocate_podle_commitment(
            self._session.preselected_utxos,
            get_private_key,
        )
        if new_commitment is None:
            logger.warning("No fresh PoDLE commitment remains for auth-stage maker replacement")
            return False

        self._session.podle_commitment = new_commitment
        self._activate_coinjoin_log_context()
        logger.debug("Rotated PoDLE commitment for auth-stage maker replacement")
        return True

    async def _fill_replacement_makers(
        self, replacement_offers: dict[str, Any], failed_nicks: set[str]
    ) -> bool:
        """Run a mini fill phase for auth-stage replacement makers.

        Creates sessions, binds channels, sends !fill and processes the
        !pubkey responses via the shared helper (which also parses the
        features field, so replacement makers advertising neutrino_compat are
        not re-dropped in the next auth pass). Makers that do not produce a
        usable !pubkey are removed, ignored and added to ``failed_nicks`` so
        they are not re-selected within this round.

        Returns False only on unrecoverable state (missing PoDLE commitment or
        crypto session); "no maker responded" is left to the caller's
        replacement budget.
        """
        if not self._session.podle_commitment or not self._session.crypto_session:
            logger.error("Missing commitment or crypto session for replacement")
            return False

        for nick, offer in replacement_offers.items():
            self._session.maker_sessions[nick] = MakerSession(
                nick=nick, offer=offer, supports_neutrino_compat=False
            )
            if offer_supports_private_channel_ring(offer):
                self._session.ring_candidate_nicks.add(nick)
            logger.debug(f"Added replacement maker for auth: {nick}")
        logger.debug("Running fill phase for replacement makers...")
        new_maker_nicks = list(replacement_offers.keys())

        commitment_hex = self._session.podle_commitment.to_commitment_str()
        taker_pubkey = self._session.crypto_session.get_pubkey_hex()

        for nick in new_maker_nicks:
            binding = self.directory_client.bind_session(nick)
            session = self._session.maker_sessions[nick]
            if binding is None:
                logger.warning(f"No communication channel available for replacement maker {nick}")
                continue
            session.comm_channel = binding.channel_id
            if binding.is_direct:
                logger.debug(f"Will use DIRECT connection for replacement maker {nick}")
            else:
                logger.debug(f"Will use {binding.channel_id} for replacement maker {nick}")

        for nick in new_maker_nicks:
            session = self._session.maker_sessions[nick]
            fill_data = (
                f"{session.offer.oid} {self._session.cj_amount} {taker_pubkey} {commitment_hex}"
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

        for nick in new_maker_nicks:
            ready = False
            if nick in responses and not responses[nick].get("error"):
                try:
                    response_data = responses[nick]["data"].strip()
                    ready = self._session.process_pubkey_response(nick, response_data)
                    if ready:
                        logger.debug(f"Replacement maker {nick} ready")
                except Exception as e:
                    logger.warning("Failed to process maker response")
                    logger.bind(sensitive=True).warning(
                        "Failed to process {} response: {}", nick, e
                    )
            else:
                logger.warning(f"Replacement maker {nick} didn't respond to !fill")
            if not ready:
                self._session.remove_maker_session(nick)
                failed_nicks.add(nick)
                self.orderbook_manager.add_ignored_maker(nick)
        return True

    async def _run_fill_with_replacements(
        self,
        destination: str,
        selected_offers: dict[str, Any],
        required_features: set[str] | None,
        mixdepth: int,
        get_private_key: Any,
        max_replacement_attempts: int,
        penalized_maker_keys: set[str] | None = None,
    ) -> bool:
        self.state = TakerState.FILLING
        logger.debug("Phase 1: Sending !fill to makers...")
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

        spawn_task(
            get_notifier().notify_coinjoin_start(
                self._session.cj_amount,
                len(self._session.maker_sessions),
                destination,
                self._current_coinjoin_id(),
            )
        )

        max_podle_retries = self.config.taker_utxo_retries
        replacement_attempt = 0
        podle_retry = 0
        failed_nicks: set[str] = set()
        target_makers = max(self._session.maker_target_count, self.config.minimum_makers)
        while True:
            session_size_before_fill = len(self._session.maker_sessions)
            fill_result = await self._session._phase_fill()

            n_blacklisted = len(fill_result.blacklist_makers)
            majority_blacklist = (
                fill_result.blacklist_error
                and session_size_before_fill > 0
                and n_blacklisted * 2 >= session_size_before_fill
            )

            if majority_blacklist and self._session.podle_commitment is not None:
                commitment_hex = self._session.podle_commitment.commitment.commitment.hex()
                try:
                    from jmcore.commitment_blacklist import add_commitment

                    add_commitment(commitment_hex)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        f"Could not persist majority-reported blacklisted commitment: {exc}"
                    )

            if fill_result.blacklist_error and not majority_blacklist:
                logger.warning(
                    f"Minority blacklist rejection from {fill_result.blacklist_makers} "
                    f"({n_blacklisted}/{session_size_before_fill}). Ignoring those makers "
                    "and trying replacement with the same commitment."
                )
                for failed_nick in fill_result.failed_makers:
                    self.orderbook_manager.add_ignored_maker(failed_nick)
                    failed_nicks.add(failed_nick)
                    logger.debug(f"Added {failed_nick} to ignored makers (minority blacklist)")
            elif fill_result.blacklist_error:
                logger.warning(
                    f"Majority blacklist rejection ({n_blacklisted}/{session_size_before_fill}) "
                    f"from {fill_result.blacklist_makers}. Rotating commitment and retrying."
                )
                blacklist_nicks = set(fill_result.blacklist_makers)
                for failed_nick in set(fill_result.failed_makers) - blacklist_nicks:
                    self.orderbook_manager.add_ignored_maker(failed_nick)
                    failed_nicks.add(failed_nick)
                    logger.debug(f"Added {failed_nick} to ignored makers (failed fill)")
            elif fill_result.failed_makers:
                for failed_nick in fill_result.failed_makers:
                    self.orderbook_manager.add_ignored_maker(failed_nick)
                    failed_nicks.add(failed_nick)
                    logger.debug(f"Added {failed_nick} to ignored makers (failed fill)")

            if majority_blacklist:
                if podle_retry < max_podle_retries - 1:
                    logger.warning(
                        f"Commitment blacklisted, retrying with new NUMS index "
                        f"(attempt {podle_retry + 2}/{max_podle_retries})..."
                    )
                    podle_retry += 1
                    new_commitment = await self._allocate_podle_commitment(
                        self._session.preselected_utxos,
                        get_private_key,
                    )
                    if new_commitment is None and self.config.external_podle_mode != "only":
                        added = self._session._expand_preselected_utxos_same_mixdepth(mixdepth)
                        if added > 0:
                            logger.info(
                                f"Preselected UTXOs exhausted for PoDLE; added {added} "
                                f"additional UTXO(s) from mixdepth {mixdepth}, which will "
                                "also be spent in the CoinJoin."
                            )
                            new_commitment = await self._allocate_podle_commitment(
                                self._session.preselected_utxos,
                                get_private_key,
                            )
                    if new_commitment is None:
                        if self._session.strict_input_selection:
                            reason = (
                                "No more PoDLE commitments available from the explicit "
                                "input UTXOs; automatic input expansion is disabled"
                            )
                        else:
                            reason = (
                                "No more PoDLE commitments available: all indices exhausted "
                                f"across all eligible UTXOs in mixdepth {mixdepth}"
                            )
                        logger.error(reason)
                        self._session.last_failure_reason = reason
                        self.state = TakerState.FAILED
                        return False

                    self._session.podle_commitment = new_commitment
                    self._activate_coinjoin_log_context()
                    self._session.maker_sessions = {
                        nick: MakerSession(nick=nick, offer=offer, supports_neutrino_compat=False)
                        for nick, offer in selected_offers.items()
                        if nick not in self.orderbook_manager.ignored_makers
                        and nick not in failed_nicks
                    }
                    self._session.ring_candidate_nicks = {
                        nick
                        for nick, offer in selected_offers.items()
                        if nick in self._session.maker_sessions
                        and offer_supports_private_channel_ring(offer)
                    }
                    continue

                logger.error(
                    f"Fill phase failed after {max_podle_retries} PoDLE commitment attempts"
                )
                self.state = TakerState.FAILED
                return False

            current_makers = len(self._session.maker_sessions)
            if fill_result.success and current_makers >= target_makers:
                return True

            if not fill_result.success and not fill_result.needs_replacement:
                reason = "Fill phase failed without replaceable makers"
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return False

            needed = target_makers - current_makers
            if replacement_attempt >= max_replacement_attempts:
                if fill_result.success and current_makers >= self._session.required_maker_count:
                    logger.warning(
                        f"Fill replacement attempts exhausted; proceeding with {current_makers}/"
                        f"{target_makers} makers (minimum {self._session.required_maker_count})"
                    )
                    return True
                reason = (
                    f"Fill phase failed: only {current_makers} responding makers remain, "
                    f"below minimum {self._session.required_maker_count}"
                )
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return False

            replacement_attempt += 1
            logger.info(
                f"Attempting maker replacement (attempt {replacement_attempt}/"
                f"{max_replacement_attempts}): need {needed} more makers"
            )

            current_session_nicks = set(self._session.maker_sessions.keys())
            replacement_offers, _ = self._select_channel_ring_makers(
                self.orderbook_manager.offers,
                needed,
                required_features,
                ring_slots=self._ring_replacement_slots(needed),
                hard_exclude_nicks=(
                    current_session_nicks | failed_nicks | self._seller_separation.excluded_nicks()
                ),
                penalized_maker_keys=penalized_maker_keys,
            )
            self._seller_separation.record_offered_makers(replacement_offers.values())

            if not replacement_offers:
                if fill_result.success and current_makers >= self._session.required_maker_count:
                    logger.warning(
                        f"No fill replacements available; proceeding with {current_makers}/"
                        f"{target_makers} makers (minimum {self._session.required_maker_count})"
                    )
                    return True
                reason = (
                    f"Fill phase failed: no replacements available and only {current_makers} "
                    f"makers remain (minimum {self._session.required_maker_count})"
                )
                logger.error(reason)
                self._session.last_failure_reason = reason
                self.state = TakerState.FAILED
                return False

            if len(replacement_offers) < needed:
                logger.info(
                    f"Fill replacement selection is partial: found {len(replacement_offers)}, "
                    f"need {needed}; retrying the remaining deficit"
                )

            for nick, offer in replacement_offers.items():
                self._session.maker_sessions[nick] = MakerSession(
                    nick=nick, offer=offer, supports_neutrino_compat=False
                )
                if offer_supports_private_channel_ring(offer):
                    self._session.ring_candidate_nicks.add(nick)
                logger.info(f"Added replacement maker: {nick}")
            selected_offers.update(replacement_offers)

    async def _finalize_and_broadcast(self, destination: str) -> str | None:
        # Final confirmation before broadcast
        num_taker_inputs = len(self._session.selected_utxos) + self._session.buyout_input_count
        num_maker_inputs = sum(len(s.utxos) for s in self._session.maker_sessions.values())
        total_inputs = num_taker_inputs + num_maker_inputs

        tx = deserialize_transaction(self._session.final_tx)
        total_outputs = len(tx.outputs)
        total_output_value = sum(out.value for out in tx.outputs)

        taker_input_value = sum(utxo.value for utxo in self._session.selected_utxos)
        if self._session.buyout is not None:
            taker_input_value += self._session.buyout.total_value
        maker_input_value = sum(
            utxo["value"]
            for session in self._session.maker_sessions.values()
            for utxo in session.utxos
        )
        total_input_value = taker_input_value + maker_input_value
        actual_mining_fee = total_input_value - total_output_value

        final_fee_plan = self._session.maker_fee_plan()
        total_maker_fees = sum(final_fee_plan.values())
        total_cost = total_maker_fees + actual_mining_fee
        actual_vsize = calculate_tx_vsize(self._session.final_tx)
        actual_fee_rate = actual_mining_fee / actual_vsize if actual_vsize > 0 else 0.0

        minimum_fee_rate = self._session._minimum_fee_rate_sat_vb
        if minimum_fee_rate is None or not fee_rate_meets_minimum(
            actual_mining_fee, actual_vsize, minimum_fee_rate
        ):
            reason = (
                f"Final CoinJoin miner fee rate {actual_fee_rate:.2f} sat/vB is below required "
                f"{minimum_fee_rate:.2f} sat/vB"
                if minimum_fee_rate is not None
                else "Final CoinJoin miner fee rate could not be verified"
            )
            logger.error("Final CoinJoin miner fee rate is below the required minimum")
            logger.bind(sensitive=True).error("Final CoinJoin miner fee detail: {}", reason)
            self._session.last_failure_reason = reason
            self.state = TakerState.FAILED
            return None

        logger.info("Final transaction ready to broadcast")
        sensitive_logger = logger.bind(sensitive=True)
        sensitive_logger.info("=" * 70)
        sensitive_logger.info("FINAL TRANSACTION SUMMARY - Ready to broadcast")
        sensitive_logger.info("=" * 70)
        sensitive_logger.info(f"CoinJoin amount:      {self._session.cj_amount:,} sats")
        sensitive_logger.info(f"Makers participating: {len(self._session.maker_sessions)}")
        sensitive_logger.info(
            f"  Makers: {', '.join(nick[:10] + '...' for nick in self._session.maker_sessions)}"
        )
        sensitive_logger.info(
            f"Transaction inputs:   {total_inputs} ({num_taker_inputs} yours, "
            f"{num_maker_inputs} makers)"
        )
        sensitive_logger.info(f"Transaction outputs:  {total_outputs}")
        sensitive_logger.info(f"Maker fees:           {total_maker_fees:,} sats")
        sensitive_logger.info(
            f"Mining fee:           {actual_mining_fee:,} sats ({actual_fee_rate:.2f} sat/vB)"
        )
        sensitive_logger.info(f"Total cost:           {total_cost:,} sats")
        sensitive_logger.info(
            f"Transaction size:     {actual_vsize} vbytes ({len(self._session.final_tx)} bytes)"
        )
        sensitive_logger.info("-" * 70)
        sensitive_logger.debug("Transaction hex (for manual verification/broadcast):")
        sensitive_logger.debug(self._session.final_tx.hex())
        sensitive_logger.info("=" * 70)

        if hasattr(self, "confirmation_callback") and self.confirmation_callback:
            try:
                maker_details = []
                for nick, session in self._session.maker_sessions.items():
                    fee = final_fee_plan[nick]
                    advertised_fee = calculate_cj_fee(
                        session.offer,
                        self._session.cj_amount,
                        False,
                    )
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
                            "advertised_fee": advertised_fee,
                            "bond_value": bond_value,
                            "location": location,
                        }
                    )

                confirmed = await self._request_confirmation(
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
                logger.error("Final CoinJoin confirmation failed")
                logger.bind(sensitive=True).error("Final confirmation error detail: {}", e)
                self.state = TakerState.FAILED
                return None

        self.state = TakerState.BROADCASTING
        logger.debug("Phase 5: Broadcasting transaction...")

        self._session.txid = await self._session._phase_broadcast()
        if not self._session.txid:
            logger.error("Broadcast failed")
            self.state = TakerState.FAILED
            return None

        self._session.last_used_nicks = set(self._session.maker_sessions)
        self._session.last_used_maker_keys = {
            key
            for session in self._session.maker_sessions.values()
            for key in maker_selection_keys(session.offer)
        }

        self.state = TakerState.COMPLETE
        logger.info("CoinJoin complete, broadcast method: {}", self._session.broadcast_method)
        logger.bind(sensitive=True).info("CoinJoin complete: txid={}", self._session.txid)

        try:
            updated = update_taker_awaiting_transaction_broadcast(
                destination_address=self._session.cj_destination,
                change_address=self._session.wallet_change_address,
                txid=self._session.txid,
                mining_fee=actual_mining_fee,
                broadcast_method=self._session.broadcast_method,
                broadcast_policy=self._session.broadcast_policy,
                broadcast_fallback_reason=self._session.broadcast_fallback_reason,
                data_dir=self.config.data_dir,
                wallet_fingerprint=self.wallet.wallet_fingerprint,
            )
            if updated:
                logger.bind(sensitive=True).debug(
                    f"Updated history entry for CJ txid {self._session.txid[:16]}..., "
                    f"mining_fee={actual_mining_fee} sats"
                )
            else:
                logger.warning("CoinJoin history may be inconsistent")
                logger.bind(sensitive=True).warning(
                    "No matching awaiting history entry for destination {}",
                    self._session.cj_destination,
                )

            destination_vout = self._session._get_taker_cj_output_index()
            await self._update_pending_transaction_now(
                self._session.txid,
                self._session.cj_destination,
                destination_vout if destination_vout is not None else -1,
                len(self._session.maker_sessions),
            )
        except Exception as e:
            logger.warning("Failed to update CoinJoin history")
            logger.bind(sensitive=True).warning("CoinJoin history update detail: {}", e)

        total_fees = total_maker_fees + actual_mining_fee
        spawn_task(
            get_notifier().notify_coinjoin_complete(
                self._session.txid,
                self._session.cj_amount,
                len(self._session.maker_sessions),
                total_fees,
                self._current_coinjoin_id(),
                broadcast_method=self._session.broadcast_method,
                broadcast_fallback_reason=self._session.broadcast_fallback_reason,
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
