"""
CoinJoin session state and protocol phases.

A ``CoinJoinSession`` owns the ephemeral state of a single CoinJoin attempt
(cj_amount, selected makers, PoDLE commitment, crypto session, unsigned and
final transaction bytes, txid, fee rates, and book-keeping fields) together
with the protocol phase methods that drive it (``_phase_fill``,
``_phase_auth``, ``_phase_build_tx``, ``_phase_collect_signatures``,
``_phase_broadcast`` and their supporting helpers).

The owning ``Taker`` provides persistent infrastructure (wallet, backend,
config, directory client) which the session reads via ``attach``. Splitting
the per-call protocol state and behavior out of ``Taker`` makes the boundary
between long-lived infrastructure and per-call orchestration explicit and
keeps ``Taker`` focused on lifecycle (start/stop/sync_wallet/run_schedule).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jmcore.bitcoin import get_txid, pubkey_to_p2wpkh_script
from jmcore.constants import BITCOIN_DUST_THRESHOLD, DUST_THRESHOLD
from jmcore.encryption import CryptoSession
from jmcore.fee_policy import (
    MinimumFeeRateExceedsCapError,
    estimate_p2wpkh_vsize,
    fee_rate_meets_minimum,
    parse_low_fee_error,
    resolve_min_fee_rate,
)
from jmcore.protocol import FEATURE_NEUTRINO_COMPAT, MakerError, UTXOMetadata, parse_utxo_list
from jmcore.randomness import secure_random
from jmwallet.history import (
    HistoryWriteError,
    append_history_entry,
    create_taker_history_entry,
    mark_pending_transaction_failed,
)
from jmwallet.wallet.signing import (
    TransactionSigningError,
    create_p2wpkh_script_code,
    deserialize_transaction,
    verify_p2wpkh_signature,
)
from jmwallet.wallet.spend import enforce_fee_rate_cap
from loguru import logger

from taker.config import BroadcastPolicy
from taker.models import MakerSession, PhaseResult
from taker.orderbook import calculate_cj_fee, calculate_cj_fee_plan
from taker.podle import ExtendedPoDLECommitment, get_eligible_podle_utxos
from taker.tx_builder import CoinJoinTxBuilder, build_coinjoin_tx, compute_tx_locktime

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.models import UTXOInfo
    from jmwallet.wallet.service import WalletService

    from taker.config import TakerConfig
    from taker.multi_directory import MultiDirectoryClient
    from taker.taker import Taker


@dataclass(frozen=True)
class _MakerUTXOVerificationOutcome:
    """Fail-closed result that separates bad maker data from backend outages."""

    error: str | None = None
    unavailable: bool = False


class CoinJoinSession:
    """Per-call CoinJoin state and protocol phases.

    ``attach(taker)`` wires the persistent dependencies (wallet, backend,
    config, directory client) from the owning ``Taker``. ``reset()`` clears
    transient state at the start of each ``do_coinjoin`` invocation so that
    consumers reading successful maker identities or ``last_failure_reason``
    after a previous round see only the current round's values.
    """

    # Persistent dependencies are resolved lazily from the owning Taker via
    # ``attach``. We don't snapshot them on attach because some tests assign
    # ``taker.wallet`` / ``taker.backend`` after constructing the session
    # (e.g. via ``Taker.__new__`` bypass), so reading them on-demand keeps
    # those patterns working.
    _taker: Taker

    @property
    def wallet(self) -> WalletService:
        return self._taker.wallet

    @property
    def backend(self) -> BlockchainBackend:
        return self._taker.backend

    @property
    def config(self) -> TakerConfig:
        return self._taker.config

    @property
    def directory_client(self) -> MultiDirectoryClient:
        return self._taker.directory_client

    def __init__(self) -> None:
        # Amount-related state. ``is_sweep`` mirrors ``cj_amount == 0`` at the
        # moment ``do_coinjoin`` is invoked; both are kept because sweep math
        # later mutates ``cj_amount`` to the calculated zero-change value.
        self.cj_amount: int = 0
        self.is_sweep: bool = False

        # Maker-session bookkeeping. ``maker_sessions`` is keyed by nick.
        self.maker_sessions: dict[str, MakerSession] = {}
        # Requested maker count for this round. Replacement runners use this
        # target before falling back to the configured minimum floor.
        self.maker_target_count: int = 0

        # PoDLE commitment used for this CoinJoin. Rotated on majority-blacklist.
        self.podle_commitment: ExtendedPoDLECommitment | None = None

        # Transaction bytes at successive phases:
        # ``unsigned_tx`` is the constructed-but-unsigned PSBT-equivalent;
        # ``final_tx`` is fully signed and ready to broadcast.
        self.unsigned_tx: bytes = b""
        self.tx_metadata: dict[str, Any] = {}
        self.final_tx: bytes = b""
        self.txid: str = ""
        self.broadcast_policy: str = ""
        self.broadcast_method: str = ""
        self.broadcast_fallback_reason: str = ""

        # UTXO selection: ``preselected_utxos`` are committed to before the
        # CoinJoin; ``selected_utxos`` is the final taker input list used for
        # signing (typically equal to preselected_utxos but kept separate to
        # express the build-time vs sign-time distinction explicitly).
        self.preselected_utxos: list[UTXOInfo] = []
        self.selected_utxos: list[UTXOInfo] = []
        # Explicit coin control is strict: maker-fee changes and PoDLE retries
        # may not pull additional inputs from the wallet.
        self.strict_input_selection: bool = False

        # ``(txid, vout)`` inputs we hold a persisted CoinJoin lock on for this
        # round, so a concurrent round (this or another process) won't reuse
        # them and build a conflicting transaction. Released on pre-sign
        # failure; left to auto-expire after local signing (the inputs may be
        # spent or the transaction may still be broadcast).
        self.reserved_inputs: set[tuple[str, int]] = set()
        self.input_lock_owner = secrets.token_hex(32)
        # Once local taker signing starts, a usable transaction may exist even
        # if this process is cancelled before broadcast. From that point onward
        # input locks are retained and renewed instead of released.
        self.signing_boundary_crossed = False

        # Counterparty identities in the successfully broadcast transaction.
        # The tumbler reads these after success to penalize immediate reuse.
        self.last_used_nicks: set[str] = set()
        self.last_used_maker_keys: set[str] = set()

        # Human-readable failure reason exposed for tumbler diagnostics.
        self.last_failure_reason: str | None = None
        # Makers that failed to provide valid signatures after receiving !tx.
        # The owner adds these nicks to the persistent ignored-maker set after
        # the round fails; replacing them here would invalidate the transaction.
        self.failed_signer_nicks: set[str] = set()
        # Makers that answered !tx with an explicit protocol error, such as a
        # minimum miner fee policy rejection. Declining is honest behaviour, so
        # these nicks are kept out of the persistent ignored-maker set.
        self.declined_signer_nicks: set[str] = set()

        # Addresses recorded for broadcast verification and history reconciliation.
        self.cj_destination: str = ""
        self.taker_change_address: str = ""

        # Sweep-only: the tx-fee budget reserved at order-selection time. At
        # build time we re-use this exact number to keep the actual fee in line
        # with what was budgeted (avoids residual fee issues).
        self._sweep_tx_fee_budget: int = 0

        # E2E encryption session used for maker communication.
        self.crypto_session: CryptoSession | None = None

        # Fee-rate state. ``_fee_rate`` is the base rate from backend estimation
        # or manual config; ``_randomized_fee_rate`` applies the tx_fee_factor
        # jitter and is the value used for all subsequent fee calculations.
        self._fee_rate: float | None = None
        self._randomized_fee_rate: float | None = None
        self._minimum_fee_rate_sat_vb: float | None = None

    def attach(self, taker: Taker) -> None:
        """Wire the owning ``Taker`` so the session can read persistent deps.

        Called once during ``Taker.__init__``. The session reads
        ``wallet`` / ``backend`` / ``config`` / ``directory_client`` from the
        Taker lazily so test sites that assign those attributes after
        constructing the session (via ``Taker.__new__`` bypass) keep working.
        """
        self._taker = taker

    def maker_fee_plan(self) -> dict[str, int]:
        """Return the paid fee plan for the current participating makers."""
        return calculate_cj_fee_plan(
            (session.offer for session in self.maker_sessions.values()),
            self.cj_amount,
            round_up_cj_fees=self.config.round_up_cj_fees,
            equalize_cj_fees=self.config.equalize_cj_fees,
        )

    def reset(self) -> None:
        """Reset transient session state to a fresh state."""
        if self.reserved_inputs:
            raise RuntimeError("Cannot reset a CoinJoin session while it owns input leases")
        self.cj_amount = 0
        self.is_sweep = False
        self.maker_sessions = {}
        self.maker_target_count = 0
        self.podle_commitment = None
        self.unsigned_tx = b""
        self.tx_metadata = {}
        self.final_tx = b""
        self.txid = ""
        self.broadcast_policy = ""
        self.broadcast_method = ""
        self.broadcast_fallback_reason = ""
        self.preselected_utxos = []
        self.selected_utxos = []
        self.strict_input_selection = False
        self.reserved_inputs = set()
        self.input_lock_owner = secrets.token_hex(32)
        self.signing_boundary_crossed = False
        self.last_used_nicks = set()
        self.last_used_maker_keys = set()
        self.last_failure_reason = None
        self.failed_signer_nicks = set()
        self.declined_signer_nicks = set()
        self.cj_destination = ""
        self.taker_change_address = ""
        self._sweep_tx_fee_budget = 0
        self.crypto_session = None
        self._fee_rate = None
        self._randomized_fee_rate = None
        self._minimum_fee_rate_sat_vb = None

    def input_lock_ttl_sec(self) -> float:
        """Cover the remaining protocol plus the pending-broadcast window."""
        protocol_window = (
            float(self.config.order_wait_time)
            + float(self.config.initial_confirmation_timeout_sec)
            + float(self.config.maker_timeout_sec)
            * (self.config.taker_utxo_retries + 2 * self.config.max_maker_replacement_attempts + 3)
            + float(self.config.broadcast_timeout_sec) * 21
        )
        pending_window = float(self.config.pending_tx_abandon_hours) * 3600
        return protocol_window + pending_window

    def renew_input_locks(self, operation: str) -> bool:
        """Atomically verify ownership and extend this round's input leases."""
        if not self.reserved_inputs:
            logger.error(f"Cannot {operation}: this round has no reserved taker inputs")
            return False
        try:
            renewed = self.wallet.renew_coinjoin_inputs(
                self.reserved_inputs,
                owner=self.input_lock_owner,
                ttl=self.input_lock_ttl_sec(),
            )
        except Exception as exc:
            logger.error(f"Cannot {operation}: failed to renew taker input locks")
            logger.bind(sensitive=True).error("Input lock renewal detail: {}", exc)
            return False
        if not renewed:
            logger.error(f"Cannot {operation}: taker input lock ownership was lost")
            return False
        return True

    def retain_input_locks(self) -> None:
        """Best-effort renewal after signatures may exist or broadcast is uncertain."""
        if not self.reserved_inputs:
            return
        if not self.renew_input_locks("retain signed-round input locks"):
            logger.error(
                "Signed-round input locks could not be renewed; the owner-qualified "
                "leases were left untouched"
            )

    def _expand_preselected_utxos_same_mixdepth(self, mixdepth: int) -> int:
        """Add another eligible UTXO from the same mixdepth to ``preselected_utxos``.

        Called when all PoDLE indices on the currently preselected UTXOs are
        exhausted (either used or blacklisted). The newly added UTXO will also
        be spent in the CoinJoin, so we never cross mixdepth boundaries.

        Returns the number of UTXOs actually added (0 if none available).
        """
        if self.strict_input_selection:
            logger.info("Strict input selection prevents adding another PoDLE UTXO")
            return 0

        try:
            all_utxos = self.wallet.get_all_utxos(mixdepth, self.config.taker_utxo_age)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Could not list UTXOs while selecting a PoDLE commitment")
            logger.bind(sensitive=True).warning("PoDLE UTXO lookup detail: {}", exc)
            return 0

        already = {(u.txid, u.vout) for u in self.preselected_utxos}
        # Only consider candidates that meet the PoDLE value threshold; otherwise
        # they'd just inflate inputs without enabling a fresh commitment.
        eligible = get_eligible_podle_utxos(
            all_utxos,
            self.cj_amount,
            min_confirmations=self.config.taker_utxo_age,
            min_percent=self.config.taker_utxo_amtpercent,
        )
        candidates = [u for u in eligible if (u.txid, u.vout) not in already]

        if not candidates:
            return 0

        # Sorted by (confirmations, value) DESC from get_eligible_podle_utxos;
        # add just one UTXO at a time to minimise bloating the transaction.
        for new_utxo in candidates:
            outpoint = (new_utxo.txid, new_utxo.vout)
            if not self.wallet.reserve_coinjoin_inputs(
                {outpoint},
                ttl=self.input_lock_ttl_sec(),
                owner=self.input_lock_owner,
            ):
                continue
            self.reserved_inputs.add(outpoint)
            self.preselected_utxos.append(new_utxo)
            logger.info(
                f"Expanded preselected UTXOs with {new_utxo.txid}:{new_utxo.vout} "
                f"(value={new_utxo.value}, confs={new_utxo.confirmations}) from mixdepth "
                f"{mixdepth} to enable a fresh PoDLE commitment."
            )
            return 1
        return 0

    def _drop_neutrino_incompatible_sessions(self, nicks: list[str] | None = None) -> list[str]:
        """Drop sessions for makers whose handshake explicitly lacks neutrino_compat.

        Called just after opportunistic direct-peer handshakes complete, to
        avoid wasting a !fill + !pubkey round trip on a maker we already know
        is incompatible. Peers whose feature status is unknown (no direct
        handshake, or legacy peer that sent an empty features field) are kept
        and revalidated during _phase_auth.

        Returns the list of dropped nicks (empty if none).
        """
        dropped: list[str] = []
        candidates = list(self.maker_sessions.keys()) if nicks is None else list(nicks)
        for nick in candidates:
            peer = self.directory_client.get_connected_peer(nick)
            if peer is None:
                continue
            support = peer.supports_feature(FEATURE_NEUTRINO_COMPAT)
            if support is False:
                dropped.append(nick)
        for nick in dropped:
            logger.warning(
                f"Dropping maker {nick} before !fill: peer handshake reports "
                f"no neutrino_compat support (taker requires it)."
            )
            del self.maker_sessions[nick]
        return dropped

    def process_pubkey_response(self, nick: str, response_data: str) -> bool:
        """Parse a maker's !pubkey payload and set up its encryption session.

        Payload format: ``<nacl_pubkey_hex> [features=<comma-separated>]
        <signing_pk> <sig>``. Records the maker's NaCl pubkey, parses the
        optional features field (e.g. ``features=neutrino_compat``) into
        ``supports_neutrino_compat``, and creates the per-maker crypto session
        reusing the taker keypair from ``self.crypto_session`` (the pubkey the
        maker saw in !fill). Used for both the initial fill phase and the
        mini-fill run for replacement makers, so feature detection stays
        consistent across the two paths.

        Returns True on success; False on an empty payload or missing taker
        crypto session (the caller drops the maker session). Exceptions from
        an invalid pubkey propagate and are handled the same way by callers.
        """
        if self.crypto_session is None:
            logger.error(f"No taker crypto session while processing !pubkey from {nick}")
            return False

        parts = response_data.split()
        if not parts:
            logger.warning(f"Empty !pubkey response from {nick}")
            return False

        session = self.maker_sessions[nick]
        nacl_pubkey = parts[0]
        session.pubkey = nacl_pubkey
        session.responded_fill = True

        # Parse optional features (e.g., "features=neutrino_compat")
        for part in parts[1:]:
            if part.startswith("features="):
                features_str = part[len("features=") :]
                features = set(features_str.split(",")) if features_str else set()
                if FEATURE_NEUTRINO_COMPAT in features:
                    session.supports_neutrino_compat = True
                    logger.debug(f"Maker {nick} supports neutrino_compat")
                break

        # Set up encryption session with this maker using their NaCl pubkey.
        # IMPORTANT: Reuse the same keypair from self.crypto_session that was
        # sent in !fill, just set up a new box with the maker's pubkey.
        crypto = CryptoSession.__new__(CryptoSession)
        crypto.keypair = self.crypto_session.keypair  # Reuse taker keypair!
        crypto.box = None
        crypto.counterparty_pubkey = ""
        crypto.setup_encryption(nacl_pubkey)
        session.crypto = crypto
        logger.debug(f"Processed !pubkey from {nick}: {nacl_pubkey[:16]}..., encryption set up")
        return True

    async def _phase_fill(self) -> PhaseResult:
        """Send !fill to all selected makers and wait for !pubkey responses.

        Returns:
            PhaseResult with success status, failed makers list, and blacklist flag.
        """
        if not self.podle_commitment:
            return PhaseResult(success=False)

        # A repeat !fill reuses the commitment, which the maker refuses as already
        # reserved without replying, so only fill makers that have not responded.
        pending_nicks = [
            nick for nick, session in self.maker_sessions.items() if not session.responded_fill
        ]
        if not pending_nicks:
            return PhaseResult(success=len(self.maker_sessions) >= self.config.minimum_makers)

        # Established makers hold the pubkey from their !fill; only a fresh round rotates it.
        if len(pending_nicks) == len(self.maker_sessions) or self.crypto_session is None:
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

        # Start direct connection attempts for the makers being filled
        if self.directory_client.prefer_direct_connections:
            for nick in pending_nicks:
                maker_location = self.directory_client.get_peer_location(nick)
                if maker_location:
                    self.directory_client.try_direct_connect(nick)

        # Wait up to 5 seconds for direct connections to establish
        # This timeout balances privacy (prefer direct) vs latency (don't wait too long)
        if self.directory_client.prefer_direct_connections:
            pending_tasks = []
            for nick in pending_nicks:
                task = self.directory_client.get_pending_connect_task(nick)
                if task is not None and not task.done():
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

        # Pre-fill compatibility filter: once direct connections have handshaked,
        # we know each peer's advertised features. If the taker requires
        # neutrino_compat and a peer explicitly does NOT advertise it, drop the
        # session now rather than wasting a !fill + !pubkey round trip (and a
        # PoDLE retry if the maker happens to also blacklist our commitment).
        #
        # Peers whose feature support is still unknown (no direct handshake,
        # or legacy peer with no features field) are kept; the existing check
        # in _phase_auth will catch them later.
        if self.backend.requires_neutrino_metadata():
            incompatible = self._drop_neutrino_incompatible_sessions(pending_nicks)
            if incompatible and len(self.maker_sessions) < self.config.minimum_makers:
                logger.error(
                    f"After filtering {len(incompatible)} neutrino-incompatible maker(s), "
                    f"only {len(self.maker_sessions)} remain (need "
                    f"{self.config.minimum_makers})."
                )
                return PhaseResult(
                    success=False,
                    failed_makers=incompatible,
                )
            pending_nicks = [nick for nick in pending_nicks if nick in self.maker_sessions]

        # Determine and record communication channel for each maker by
        # delegating to the directory layer. The DTO encapsulates the
        # "prefer direct, otherwise pick the most relevant directory"
        # algorithm that used to be open-coded here and in two other sites.
        for nick in pending_nicks:
            session = self.maker_sessions[nick]
            binding = self.directory_client.bind_session(nick)
            if binding is None:
                # No directories connected -- shouldn't happen at this stage.
                raise RuntimeError(f"No communication channel available for {nick}")
            session.comm_channel = binding.channel_id
            if binding.is_direct:
                logger.debug(f"Will use DIRECT connection for {nick}")
            else:
                logger.debug(
                    f"Will use {binding.channel_id} for {nick} "
                    f"(onion: {binding.peer_location or 'unknown'})"
                )

        # Format: fill <oid> <amount> <taker_pubkey> <commitment>
        for nick in pending_nicks:
            session = self.maker_sessions[nick]
            fill_data = f"{session.offer.oid} {self.cj_amount} {taker_pubkey} {commitment_hex}"
            channel = await self.directory_client.send_privmsg(
                nick, "fill", fill_data, log_routing=True, force_channel=session.comm_channel
            )
            # Verify the channel used matches what we recorded
            assert channel == session.comm_channel, f"Channel mismatch for {nick}"

        timeout = self.config.maker_timeout_sec
        expected_nicks = list(pending_nicks)

        responses = await self.directory_client.wait_for_responses(
            expected_nicks=expected_nicks,
            expected_command="!pubkey",
            timeout=timeout,
        )

        # Track failed makers and blacklist errors
        failed_makers: list[str] = []
        blacklist_makers: list[str] = []
        # Subset of failed_makers that did not respond at all -- they may have
        # silently dropped our !fill because they consider our commitment
        # blacklisted (the reference maker implementation never replies in
        # that case). When *any* maker explicitly returns a blacklist error,
        # we promote these silent timeouts to "presumed blacklist" so the
        # majority/minority threshold in do_coinjoin reflects reality.
        silent_makers: list[str] = []
        blacklist_error = False

        # Process responses
        # Maker sends: "<nacl_pubkey> [features=...] <signing_pubkey> <signature>"
        # Directory client strips command, we get the data part
        # Note: responses may include error responses with {"error": True, "data": "reason"}
        for nick in pending_nicks:
            if nick in responses:
                # Check if this is an error response
                if responses[nick].get("error"):
                    error_msg = responses[nick].get("data", "Unknown error")
                    logger.error("Maker rejected !fill")
                    logger.bind(sensitive=True).error(
                        "Maker {} rejected !fill: {}", nick, error_msg
                    )
                    # Check if this is a blacklist error
                    if "blacklist" in error_msg.lower():
                        blacklist_error = True
                        blacklist_makers.append(nick)
                        logger.bind(sensitive=True).warning(
                            f"Commitment was blacklisted by {nick} - may need retry with new index"
                        )
                    failed_makers.append(nick)
                    del self.maker_sessions[nick]
                    continue

                try:
                    response_data = responses[nick]["data"].strip()
                    if not self.process_pubkey_response(nick, response_data):
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                except Exception as e:
                    logger.warning("Invalid !pubkey response from maker")
                    logger.bind(sensitive=True).warning(
                        "Invalid !pubkey response from {}: {}", nick, e
                    )
                    failed_makers.append(nick)
                    del self.maker_sessions[nick]
            else:
                logger.warning(f"No !pubkey response from {nick}")
                failed_makers.append(nick)
                silent_makers.append(nick)
                del self.maker_sessions[nick]

        # If at least one maker explicitly rejected the commitment as
        # blacklisted, treat the silent makers (timeouts) as also-blacklisted.
        # Reference-implementation makers do not send any reply when they see
        # a blacklisted commitment, so without this promotion the
        # majority/minority split in do_coinjoin under-counts the rejection
        # and we'd keep retrying with the same dead commitment instead of
        # rotating it.
        if blacklist_error and silent_makers:
            logger.warning(
                f"Promoting {len(silent_makers)} silent maker(s) "
                f"({silent_makers}) to presumed-blacklist after explicit "
                f"blacklist rejection from {blacklist_makers}: reference "
                "makers stay silent on blacklisted commitments."
            )
            for nick in silent_makers:
                if nick not in blacklist_makers:
                    blacklist_makers.append(nick)

        # Opportunistic early drop for neutrino takers: a maker whose !pubkey
        # did not advertise neutrino_compat would be dropped in _phase_auth
        # anyway (we cannot verify its UTXOs without extended metadata), so
        # drop it now, before wasting an !auth round trip, and let the fill
        # replacement machinery find a substitute. The auth-phase check stays
        # as a safety net for replacement paths.
        if self.backend.requires_neutrino_metadata():
            for nick in pending_nicks:
                if nick not in self.maker_sessions:
                    continue
                session = self.maker_sessions[nick]
                if session.responded_fill and not session.supports_neutrino_compat:
                    logger.warning(
                        f"Dropping maker {nick} after !pubkey: no neutrino_compat in "
                        f"advertised features (taker requires extended UTXO metadata)."
                    )
                    failed_makers.append(nick)
                    del self.maker_sessions[nick]

        if len(self.maker_sessions) < self.config.minimum_makers:
            logger.error(f"Not enough makers responded: {len(self.maker_sessions)}")
            return PhaseResult(
                success=False,
                failed_makers=failed_makers,
                blacklist_error=blacklist_error,
                blacklist_makers=blacklist_makers,
            )

        return PhaseResult(
            success=True,
            failed_makers=failed_makers,
            blacklist_error=blacklist_error,
            blacklist_makers=blacklist_makers,
        )

    async def _phase_auth(self) -> PhaseResult:
        """Send !auth with PoDLE proof and wait for !ioauth responses.

        Returns:
            PhaseResult with success status and failed makers list.
        """
        if not self.podle_commitment:
            return PhaseResult(success=False)

        # A repeat !auth reaches a maker session that has left PUBKEY_SENT and is
        # rejected, so only authenticate makers that have not responded.
        pending_nicks = [
            nick for nick, session in self.maker_sessions.items() if not session.responded_auth
        ]
        if not pending_nicks:
            return PhaseResult(success=len(self.maker_sessions) >= self.config.minimum_makers)

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

        # Remove incompatible makers before sending any !auth. Otherwise, if
        # filtering drops the session below the maker floor, a replacement pass
        # would resend !auth to compatible makers whose responses were not read.
        incompatible_makers: list[str] = []
        if taker_requires_extended:
            for nick in list(pending_nicks):
                session = self.maker_sessions[nick]
                if session.supports_neutrino_compat:
                    continue

                logger.error(
                    f"Incompatible maker {nick}: taker uses Neutrino backend but maker "
                    f"doesn't support neutrino_compat. Taker cannot verify maker's UTXOs "
                    f"without extended metadata (scriptpubkey + blockheight)."
                )
                incompatible_makers.append(nick)
                del self.maker_sessions[nick]

            pending_nicks = [nick for nick in pending_nicks if nick in self.maker_sessions]

            # Report incompatible makers as failed so the replacement loop can
            # ignore them and pick substitutes before any PoDLE proof is revealed.
            if len(self.maker_sessions) < self.config.minimum_makers:
                logger.error(
                    f"Not enough compatible makers: {len(self.maker_sessions)} "
                    f"< {self.config.minimum_makers}. Neutrino takers require makers that "
                    f"provide extended UTXO metadata (neutrino_compat)."
                )
                return PhaseResult(success=False, failed_makers=incompatible_makers)

        podle_revealed = False
        for nick in pending_nicks:
            session = self.maker_sessions[nick]
            if session.crypto is None:
                logger.error(f"No encryption session for {nick}")
                continue

            maker_requires_extended = session.supports_neutrino_compat

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

            # Opportunistically upgrade to a direct connection if one has
            # finished handshaking since !fill (mirrors the reference taker).
            session.comm_channel = self.directory_client.upgrade_channel_prefer_direct(
                nick, session.comm_channel
            )

            # Encrypt and send on the (possibly upgraded) session channel.
            encrypted_revelation = session.crypto.encrypt(revelation_str)
            await self.directory_client.send_privmsg(
                nick,
                "auth",
                encrypted_revelation,
                log_routing=True,
                force_channel=session.comm_channel,
            )
            podle_revealed = True

        timeout = self.config.maker_timeout_sec
        expected_nicks = list(pending_nicks)

        responses = await self.directory_client.wait_for_responses(
            expected_nicks=expected_nicks,
            expected_command="!ioauth",
            timeout=timeout,
        )

        # Track failed makers for potential replacement, seeded with the makers
        # already dropped for incompatibility so they are ignored going forward.
        failed_makers: list[str] = list(incompatible_makers)
        unavailable_makers: list[str] = []

        # Process responses
        # Maker sends !ioauth as ENCRYPTED space-separated:
        # <utxo_list> <auth_pub> <cj_addr> <change_addr> <btc_sig>
        # where utxo_list can be:
        # - Legacy format: txid:vout,txid:vout,...
        # - Extended format (neutrino_compat): txid:vout:scriptpubkey:blockheight,...
        # Response format from directory: "<encrypted_data> <signing_pubkey> <signature>"
        for nick in expected_nicks:
            if nick in responses:
                # Explicit protocol error from the maker (e.g. "Failed to
                # select UTXOs"). Error payloads are plaintext, so handle them
                # before attempting decryption.
                if responses[nick].get("error"):
                    error_msg = responses[nick].get("data", "Unknown error")
                    safe_error = (
                        MakerError.VERIFICATION_UNAVAILABLE
                        if error_msg == MakerError.VERIFICATION_UNAVAILABLE
                        else MakerError.AUTHENTICATION_FAILED
                    )
                    logger.error(f"Maker {nick} rejected !auth: {safe_error.value}")
                    logger.bind(sensitive=True).error(
                        "Maker {} rejected !auth: {}", nick, error_msg
                    )
                    # A remote maker controls this error token, so it cannot be
                    # trusted to bypass the persistent failed-maker policy.
                    failed_makers.append(nick)
                    del self.maker_sessions[nick]
                    continue

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
                    if len(ioauth_parts) < 5:
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

                    # The maker must prove control of its auth key by signing its
                    # NaCl pubkey with it. An unauthenticated session lets a
                    # malicious directory substitute the maker's encryption key and
                    # MITM the channel, so a failing btc_sig is fatal.
                    btc_sig = ioauth_parts[4]
                    from jmcore.crypto import ecdsa_verify

                    if not ecdsa_verify(session.pubkey, btc_sig, bytes.fromhex(auth_pub)):
                        logger.warning(f"btc_sig verification failed from {nick}, dropping")
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    # Parse utxo_list using protocol helper
                    # (handles both legacy and extended format)
                    # Then verify each UTXO using the appropriate backend method
                    session.utxos = []
                    utxo_metadata_list = parse_utxo_list(utxo_list_str)

                    # We pay the mining fee for every input in the CoinJoin, so a
                    # maker that declares an unbounded number of inputs bills its
                    # own UTXO consolidation to us. Cap the count before touching
                    # the backend (verifying hundreds of outpoints is itself
                    # expensive) and drop the maker so it can be replaced.
                    max_maker_utxos = self.config.max_maker_utxos
                    if max_maker_utxos and len(utxo_metadata_list) > max_maker_utxos:
                        logger.warning(
                            f"Dropping maker {nick}: declared {len(utxo_metadata_list)} inputs, "
                            f"more than max_maker_utxos={max_maker_utxos}. The taker pays the "
                            "mining fee for every input, so this would inflate our fee."
                        )
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    # Track if maker sent extended format
                    has_extended = any(u.has_neutrino_metadata() for u in utxo_metadata_list)
                    if has_extended:
                        session.supports_neutrino_compat = True
                        logger.debug(f"Maker {nick} sent extended UTXO format (neutrino_compat)")

                    verification = await self._verify_maker_utxos(nick, session, utxo_metadata_list)
                    if verification.error is not None:
                        logger.warning(f"Dropping maker {nick}: {verification.error}")
                        if verification.unavailable:
                            unavailable_makers.append(nick)
                        else:
                            failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    # Every outpoint in the transaction must be unique across ALL
                    # participants. A maker learns our PoDLE input from !auth and
                    # sees nothing of other makers, but colluding makers (or one
                    # maker with several nicks) could still declare the same
                    # outpoint; any duplicate makes the tx consensus-invalid and
                    # burns the round after PoDLE commitments were revealed.
                    maker_outpoints = {(u["txid"], u["vout"]) for u in session.utxos}
                    taken_outpoints = {(u.txid, u.vout) for u in self.preselected_utxos}
                    for other_nick, other_session in self.maker_sessions.items():
                        if other_nick == nick:
                            continue
                        taken_outpoints.update((u["txid"], u["vout"]) for u in other_session.utxos)
                    overlapping = maker_outpoints & taken_outpoints
                    if overlapping:
                        sample = next(iter(overlapping))
                        logger.bind(sensitive=True).warning(
                            f"Dropping maker {nick}: input {sample[0]}:{sample[1]} is "
                            "already used by another participant in this round"
                        )
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    # Reference-taker parity: the maker must fund its CoinJoin
                    # output and still leave a non-dust change output. Otherwise
                    # the tx build fails (negative change) or the maker's change
                    # output is omitted, in which case the maker refuses to sign;
                    # either way the whole round dies after PoDLE commitments were
                    # burned. Drop such makers here so they can be replaced.
                    maker_total_input = sum(u["value"] for u in session.utxos)
                    maker_cjfee = self.maker_fee_plan()[nick]
                    maker_change = (
                        maker_total_input - self.cj_amount - session.offer.txfee + maker_cjfee
                    )
                    if maker_change < DUST_THRESHOLD:
                        logger.bind(sensitive=True).warning(
                            f"Dropping maker {nick}: inputs total {maker_total_input} sats "
                            f"leaves change of {maker_change} sats (cj_amount={self.cj_amount}, "
                            f"txfee={session.offer.txfee}, cjfee={maker_cjfee}), below "
                            f"the maker change threshold ({DUST_THRESHOLD})"
                        )
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    # Tie the authenticated session to on-chain ownership: the auth
                    # pubkey must own one of the maker's declared UTXOs. Compare
                    # case-insensitively: for neutrino peers the scriptPubKey is
                    # peer-supplied hex whose case is not normalized (matching the
                    # case-insensitive signing-phase check via bytes.fromhex).
                    auth_spk = pubkey_to_p2wpkh_script(bytes.fromhex(auth_pub)).hex()
                    if not any(
                        u.get("scriptpubkey", "").lower() == auth_spk for u in session.utxos
                    ):
                        logger.warning("Maker authentication key matches no declared UTXO")
                        logger.bind(sensitive=True).warning(
                            "Authentication key for {} matches no declared UTXO", nick
                        )
                        failed_makers.append(nick)
                        del self.maker_sessions[nick]
                        continue

                    session.cj_address = cj_addr
                    session.change_address = change_addr
                    session.auth_pubkey = auth_pub  # Store for later verification
                    session.responded_auth = True
                    logger.bind(sensitive=True).debug(
                        f"Processed !ioauth from {nick}: {len(session.utxos)} UTXOs, "
                        f"cj_addr={cj_addr[:16]}..."
                    )
                except Exception as e:
                    logger.warning("Invalid !ioauth response from maker")
                    logger.bind(sensitive=True).warning(
                        "Invalid !ioauth response from {}: {}", nick, e
                    )
                    failed_makers.append(nick)
                    del self.maker_sessions[nick]
            else:
                logger.warning(f"No !ioauth response from {nick}")
                failed_makers.append(nick)
                del self.maker_sessions[nick]

        if len(self.maker_sessions) < self.config.minimum_makers:
            logger.error(f"Not enough makers sent UTXOs: {len(self.maker_sessions)}")
            return PhaseResult(
                success=False,
                failed_makers=failed_makers,
                unavailable_makers=unavailable_makers,
                podle_revealed=podle_revealed,
            )

        return PhaseResult(
            success=True,
            failed_makers=failed_makers,
            unavailable_makers=unavailable_makers,
            podle_revealed=podle_revealed,
        )

    async def _verify_maker_utxos(
        self,
        nick: str,
        session: MakerSession,
        utxo_metadata_list: list[UTXOMetadata],
    ) -> _MakerUTXOVerificationOutcome:
        """Verify a maker's declared UTXOs on-chain, populating ``session.utxos``.

        Every outpoint must be unique within the maker's own list (duplicates
        would put a duplicate input in the transaction, which is
        consensus-invalid), exist in the UTXO set, and be confirmed (matching
        the reference taker). A spent or missing output makes the final
        transaction consensus-invalid, and an unconfirmed one makes it
        unconfirmable until the parent confirms. Crediting a historical or zero
        value instead would let a single bad maker abort the whole round at
        tx-build time.

        Returns a fail-closed outcome. Backend unavailability is distinct from
        conclusive invalid maker data so callers do not blacklist honest makers.
        """
        declared_outpoints: set[tuple[str, int]] = set()
        for utxo_meta in utxo_metadata_list:
            txid = utxo_meta.txid
            vout = utxo_meta.vout
            scriptpubkey = ""

            # An outpoint listed twice would put a duplicate input in
            # the transaction, which is consensus-invalid.
            if (txid, vout) in declared_outpoints:
                return _MakerUTXOVerificationOutcome(f"declared duplicate input {txid}:{vout}")
            declared_outpoints.add((txid, vout))

            try:
                if self.backend.requires_neutrino_metadata() and utxo_meta.has_neutrino_metadata():
                    # Use Neutrino-compatible verification with metadata
                    result = await self.backend.verify_utxo_with_metadata(
                        txid=txid,
                        vout=vout,
                        scriptpubkey=utxo_meta.scriptpubkey,  # type: ignore
                        blockheight=utxo_meta.blockheight,  # type: ignore
                    )
                    if not result.valid:
                        return _MakerUTXOVerificationOutcome(
                            f"Neutrino UTXO verification failed for {txid}:{vout}: {result.error}",
                            unavailable=result.unavailable,
                        )
                    if result.confirmations <= 0:
                        return _MakerUTXOVerificationOutcome(f"UTXO {txid}:{vout} is unconfirmed")
                    value = result.value
                    address = ""  # Not available from verification
                    scriptpubkey = utxo_meta.scriptpubkey or ""
                    logger.bind(sensitive=True).debug(
                        "Neutrino-verified UTXO {}:{} = {} sats", txid, vout, value
                    )
                else:
                    # Full node: direct UTXO lookup.
                    utxo_info = await self.backend.get_utxo(txid, vout)
                    if utxo_info is None:
                        return _MakerUTXOVerificationOutcome(
                            f"UTXO {txid}:{vout} is spent or does not exist"
                        )
                    if utxo_info.confirmations <= 0:
                        return _MakerUTXOVerificationOutcome(f"UTXO {txid}:{vout} is unconfirmed")
                    value = utxo_info.value
                    address = utxo_info.address
                    scriptpubkey = utxo_info.scriptpubkey or ""
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return _MakerUTXOVerificationOutcome(
                    f"error verifying UTXO {txid}:{vout}: {e}", unavailable=True
                )

            session.utxos.append(
                {
                    "txid": txid,
                    "vout": vout,
                    "value": value,
                    "address": address,
                    "scriptpubkey": scriptpubkey,
                    "blockheight": utxo_meta.blockheight,
                }
            )
            logger.bind(sensitive=True).debug(
                "Added UTXO from {}: {}:{} = {} sats", nick, txid, vout, value
            )

        return _MakerUTXOVerificationOutcome()

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
            maker_fee_plan = self.maker_fee_plan()
            total_maker_fee = sum(maker_fee_plan.values())
            for nick, session in self.maker_sessions.items():
                maker_total_input = sum(utxo["value"] for utxo in session.utxos)
                maker_change = (
                    maker_total_input - self.cj_amount - session.offer.txfee + maker_fee_plan[nick]
                )
                if maker_change < DUST_THRESHOLD:
                    self.last_failure_reason = (
                        f"Final fee plan leaves maker {nick} with {maker_change} sats of change, "
                        f"below the maker change threshold ({DUST_THRESHOLD}); retry with "
                        "different makers."
                    )
                    logger.warning("Final maker fee plan would create dust change")
                    logger.bind(sensitive=True).warning(
                        "Final maker fee plan detail: {}", self.last_failure_reason
                    )
                    return False
            if self.config.equalize_cj_fees and maker_fee_plan:
                target_fee = max(maker_fee_plan.values())
                base_fees = {
                    nick: calculate_cj_fee(
                        session.offer,
                        self.cj_amount,
                        self.config.round_up_cj_fees,
                    )
                    for nick, session in self.maker_sessions.items()
                }
                bumped_count = sum(
                    maker_fee_plan[nick] > base_fee for nick, base_fee in base_fees.items()
                )
                logger.info(
                    f"Equalizing CoinJoin fees at {target_fee:,} sats per maker; "
                    f"{bumped_count}/{len(maker_fee_plan)} maker payments increased"
                )
                for nick, paid_fee in maker_fee_plan.items():
                    logger.bind(sensitive=True).debug(
                        "Maker {} fee: advertised/rounded {} sats, paid {} sats",
                        nick,
                        base_fees[nick],
                        paid_fee,
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
                logger.bind(sensitive=True).info(
                    f"Sweep mode: using all {len(selected_utxos)} UTXOs, "
                    f"total {preselected_total:,} sats"
                )

                # For sweeps, we MUST use the tx_fee_budget that was calculated at order
                # selection time. The equation that determined cj_amount was:
                #   total_input = cj_amount + maker_fees + tx_fee_budget
                #
                # Using any other value for tx_fee would create a residual even
                # when the maker set and its fees are unchanged:
                #   residual = total_input - cj_amount - maker_fees - tx_fee
                #            = tx_fee_budget - tx_fee
                #
                # If tx_fee < budget: positive residual goes to miners (overpaying!)
                # If tx_fee > budget: negative residual fails the CJ (underfunded)
                #
                # By using the budget as tx_fee, we ensure:
                #   - The taker pays exactly what was stated at the start
                #   - The fee rate may differ based on actual tx size
                #   - The taker's total outflow remains the amount approved
                #
                # If the maker set changes after !fill, an unclaimed maker fee
                # also becomes residual. That value goes to miners because a
                # sweep has no taker change output, without increasing the
                # taker's approved total outflow.
                #
                # Calculate actual vsize for fee rate logging
                actual_tx_vsize = estimate_p2wpkh_vsize(num_inputs, num_outputs)

                # Use the budget as the tx_fee
                tx_fee = self._sweep_tx_fee_budget

                # Calculate residual (should be minimal - just from integer division)
                residual = preselected_total - self.cj_amount - total_maker_fee - tx_fee
                if residual < 0:
                    logger.error("Sweep calculation failed due to a negative residual")
                    if self.config.equalize_cj_fees:
                        self.last_failure_reason = (
                            "Sweep maker fee equalization exceeds the amount reserved during "
                            "selection, likely because a replacement maker raised the uniform "
                            "fee target. Start a new CoinJoin round to select a compatible set."
                        )
                    else:
                        self.last_failure_reason = (
                            "Sweep maker fees exceed the amount reserved during selection; "
                            "retry the CoinJoin with different makers."
                        )
                    logger.bind(sensitive=True).error(
                        "Sweep funding detail: negative residual of {} sats; total_input={}, "
                        "cj_amount={}, maker_fees={}, tx_fee_budget={}. {}",
                        residual,
                        preselected_total,
                        self.cj_amount,
                        total_maker_fee,
                        tx_fee,
                        self.last_failure_reason,
                    )
                    return False

                # The transaction's mining fee also includes the contribution
                # deducted from each maker's change and any taker residual that
                # has no output. Use the complete fee when checking relayability.
                maker_txfee = sum(session.offer.txfee for session in self.maker_sessions.values())
                actual_mining_fee = tx_fee + maker_txfee + residual
                actual_fee_rate = actual_mining_fee / actual_tx_vsize if actual_tx_vsize > 0 else 0

                logger.bind(sensitive=True).info(
                    f"Sweep: cj_amount={self.cj_amount:,} (from !fill), "
                    f"maker_fees={total_maker_fee:,}, "
                    f"tx_fee={tx_fee:,} (budget), "
                    f"maker_txfee={maker_txfee:,}, "
                    f"residual={residual} sats, "
                    f"actual_mining_fee={actual_mining_fee:,}, "
                    f"actual_vsize={actual_tx_vsize}, "
                    f"effective_rate={actual_fee_rate:.2f} sat/vB"
                )

                # Small positive residual is expected from integer division in
                # calculate_sweep_amount. A larger residual can occur when maker
                # fees decrease after the sweep amount is fixed. Both go to miners.
                if residual > 100:
                    logger.warning("Sweep residual value is being redirected to miners")
                    logger.bind(sensitive=True).warning(
                        "Sweep residual detail: {} sats redirected to miners", residual
                    )

                # The residual becomes additional miner fee (no taker change in sweep).
                # Check the finalized transaction shape against the deterministic
                # base-rate budget used to calculate the amount sent in !fill.
                if self.config.max_sweep_fee_change is not None:
                    if tx_fee <= 0:
                        self.last_failure_reason = (
                            "Sweep fee budget must be positive after order selection; "
                            "retry the CoinJoin."
                        )
                        logger.error("Sweep fee budget is invalid")
                        logger.bind(sensitive=True).error(
                            "Sweep fee budget detail: {}", self.last_failure_reason
                        )
                        return False

                    tolerance = self.config.max_sweep_fee_change
                    actual_base_fee = self._estimate_tx_fee(
                        num_inputs, num_outputs, use_base_rate=True
                    )
                    fee_ratio = actual_base_fee / tx_fee
                    if fee_ratio > 1 + tolerance:
                        self.last_failure_reason = (
                            "Sweep transaction fee estimate exceeds the selected fee budget: "
                            f"estimated {actual_base_fee} sats for {num_inputs} inputs and "
                            f"{num_outputs} outputs, budget {tx_fee} sats, "
                            f"tolerance {tolerance:.2f}. "
                            "Retry the CoinJoin with different makers."
                        )
                        logger.error("Sweep fee estimate exceeds the selected budget")
                        logger.bind(sensitive=True).error(
                            "Sweep fee estimate detail: {}", self.last_failure_reason
                        )
                        return False

                # Defensively verify the finalized shape against the relay floor.
                # Sweep budgeting should cover every input allowed by policy, but
                # fail before signing if that invariant is ever broken.
                fee_rate_floor = self._minimum_fee_rate_sat_vb or self.config.min_fee_rate_sat_vb
                if not fee_rate_meets_minimum(actual_mining_fee, actual_tx_vsize, fee_rate_floor):
                    logger.error("Sweep fee rate is below the required minimum")
                    logger.bind(sensitive=True).error(
                        f"Sweep failed: effective fee rate {actual_fee_rate:.2f} sat/vB "
                        f"is below the required minimum of {fee_rate_floor:.2f} sat/vB "
                        f"({actual_mining_fee:,} sats over ~{actual_tx_vsize} vB). The selected "
                        "fee budget did not cover the finalized transaction shape."
                    )
                    return False

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
                    logger.bind(sensitive=True).info(
                        f"Using pre-selected UTXOs: {len(selected_utxos)} UTXOs, "
                        f"total {preselected_total:,} sats (need {required:,})"
                    )
                else:
                    # Need additional UTXOs beyond pre-selection
                    # This can happen if actual fees were higher than estimated
                    if self.strict_input_selection:
                        self.last_failure_reason = (
                            "Explicit input UTXOs are insufficient after negotiated fees: "
                            f"have {preselected_total:,} sats, need {required:,} sats. "
                            "Add another --input-utxo or reduce the CoinJoin amount."
                        )
                        logger.error("Explicit input UTXOs are insufficient after negotiated fees")
                        logger.bind(sensitive=True).error(
                            "Explicit input UTXO funding detail: {}", self.last_failure_reason
                        )
                        return False
                    logger.warning(
                        "Pre-selected UTXOs are insufficient, selecting additional inputs"
                    )
                    logger.bind(sensitive=True).warning(
                        "Pre-selected UTXO funding detail: have {:,}, need {:,}",
                        preselected_total,
                        required,
                    )
                    # Skip inputs locked by another in-flight round; our own
                    # already-reserved preselected UTXOs are force-included.
                    locked_inputs = self.wallet.get_locked_input_outpoints()
                    selected_utxos = self.wallet.select_utxos(
                        mixdepth,
                        required,
                        self.config.taker_utxo_age,
                        include_utxos=self.preselected_utxos,  # Include pre-selected (PoDLE UTXO)
                        exclude=locked_inputs,
                    )

            if not selected_utxos:
                logger.error("Failed to select enough UTXOs")
                return False

            # Lock any inputs added beyond the already-reserved preselection so a
            # concurrent round can't grab them; on conflict, fail this round.
            extra_inputs = {(u.txid, u.vout) for u in selected_utxos} - self.reserved_inputs
            if extra_inputs and not self.wallet.reserve_coinjoin_inputs(
                extra_inputs,
                ttl=self.input_lock_ttl_sec(),
                owner=self.input_lock_owner,
            ):
                logger.error(
                    "Additional UTXOs are locked by another in-flight CoinJoin; "
                    "aborting to avoid a conflicting transaction"
                )
                return False
            self.reserved_inputs |= extra_inputs

            # Store selected UTXOs for signing later
            self.selected_utxos = selected_utxos

            taker_total = sum(u.value for u in selected_utxos)

            # Calculate expected change to determine if we need a change address
            # Change = total_input - cj_amount - maker_fees - tx_fee
            expected_change = taker_total - self.cj_amount - total_maker_fee - tx_fee

            # Only generate change address if we'll actually have a change output
            # This avoids recording unused addresses in history
            if expected_change > BITCOIN_DUST_THRESHOLD:
                taker_change_address = self.wallet.get_new_internal_address(mixdepth)
                self.taker_change_address = taker_change_address
                logger.bind(sensitive=True).debug(
                    "Generated change address (expected: {} sats)", expected_change
                )
            else:
                # No change output needed (sweep or change is dust)
                taker_change_address = ""  # Will be ignored by tx builder
                self.taker_change_address = ""
                if expected_change > 0:
                    logger.bind(sensitive=True).debug(
                        f"No change address needed: change {expected_change} sats "
                        f"is at or below the taker change threshold "
                        f"({BITCOIN_DUST_THRESHOLD})"
                    )
                else:
                    logger.debug("No change address needed: sweep mode (exact spend)")

            # Build maker data
            maker_data = {}
            for nick, session in self.maker_sessions.items():
                cjfee = maker_fee_plan[nick]
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
            current_height = await self.backend.get_block_height()
            locktime = compute_tx_locktime(current_height)
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
                locktime=locktime,
            )

            logger.bind(sensitive=True).debug("Built unsigned tx: {} bytes", len(self.unsigned_tx))
            logger.bind(sensitive=True).debug(
                "Unsigned transaction hex: {}", self.unsigned_tx.hex()
            )

            # Log final transaction details
            logger.bind(sensitive=True).debug(
                f"Final CoinJoin transaction details: "
                f"{num_inputs} inputs ({num_taker_inputs} taker, {num_maker_inputs} maker), "
                f"{num_outputs} outputs"
            )
            logger.bind(sensitive=True).debug(
                f"Transaction amounts: cj_amount={self.cj_amount:,} sats, "
                f"total_maker_fees={total_maker_fee:,} sats, "
                f"mining_fee={tx_fee:,} sats "
                f"({self._fee_rate:.2f} sat/vB)"
            )
            logger.bind(sensitive=True).debug(
                f"Participating makers: {', '.join(self.maker_sessions.keys())}"
            )

            if self._minimum_fee_rate_sat_vb is not None:
                fee_error = self._verify_unsigned_minimum_miner_fee()
                if fee_error is not None:
                    self.last_failure_reason = fee_error
                    logger.error("Unsigned transaction does not meet the minimum miner fee")
                    logger.bind(sensitive=True).error("Minimum miner fee detail: {}", fee_error)
                    return False

            return True

        except Exception as e:
            logger.error("Failed to build transaction")
            logger.bind(sensitive=True).error("Transaction build error detail: {}", e)
            return False

    def _estimate_tx_fee(
        self, num_inputs: int, num_outputs: int, *, use_base_rate: bool = False
    ) -> int:
        """Estimate transaction fee.

        Uses the fee rate from _resolve_fee_rate() which must be called before
        this method. By default, uses the session's randomized fee rate for
        privacy. For sweep budget calculations, use_base_rate=True to get
        a deterministic estimate.

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

        # P2WPKH: ~68 vbytes per input, 31 vbytes per output, ~11 overhead
        vsize = estimate_p2wpkh_vsize(num_inputs, num_outputs)

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

        try:
            minimum_fee_rate = await resolve_min_fee_rate(
                self.backend,
                static_floor=self.config.min_fee_rate_sat_vb,
                block_target=self.config.min_fee_block_target,
                max_fee_rate=self.config.max_fee_rate_sat_vb,
            )
        except MinimumFeeRateExceedsCapError as exc:
            enforce_fee_rate_cap(
                exc.fee_rate,
                self.config.max_fee_rate_sat_vb,
                source=exc.source,
            )
            raise
        self._minimum_fee_rate_sat_vb = minimum_fee_rate
        logger.bind(sensitive=True).info(
            f"Resolved minimum CoinJoin miner fee rate: {self._minimum_fee_rate_sat_vb:.2f} sat/vB"
        )

        # 1. Manual fee rate takes priority
        if self.config.fee_rate is not None:
            self._fee_rate = self.config.fee_rate
            if self._fee_rate < self._minimum_fee_rate_sat_vb:
                raise ValueError(
                    f"Manual fee rate {self._fee_rate:.2f} sat/vB is below the required "
                    f"minimum {self._minimum_fee_rate_sat_vb:.2f} sat/vB"
                )
            enforce_fee_rate_cap(self._fee_rate, self.config.max_fee_rate_sat_vb, source="manual")
            logger.bind(sensitive=True).info("Using manual fee rate: {:.2f} sat/vB", self._fee_rate)
            self._apply_fee_randomization()
            return self._fee_rate

        # 2. Block target specified - check backend capability
        if self.config.fee_block_target is not None:
            if not self.backend.can_estimate_fee():
                raise ValueError(
                    "Cannot use --block-target with neutrino backend without an "
                    "external fee source. Use --fee-rate to specify a manual rate, "
                    "or configure bitcoin.fee_estimate_url (external estimates are "
                    "enabled by default when a Tor proxy is available)."
                )
            self._fee_rate = await self.backend.estimate_fee(self.config.fee_block_target)
            if self._fee_rate < self._minimum_fee_rate_sat_vb:
                logger.bind(sensitive=True).info(
                    f"Estimated fee {self._fee_rate:.2f} sat/vB is below required minimum "
                    f"{self._minimum_fee_rate_sat_vb:.2f} sat/vB, using required minimum"
                )
                self._fee_rate = self._minimum_fee_rate_sat_vb
            enforce_fee_rate_cap(
                self._fee_rate, self.config.max_fee_rate_sat_vb, source="backend estimate"
            )
            logger.bind(sensitive=True).info(
                f"Fee estimation for {self.config.fee_block_target} blocks: "
                f"{self._fee_rate:.2f} sat/vB"
            )
            self._apply_fee_randomization()
            return self._fee_rate

        # 3. Default: 3-block estimation if backend supports it
        if self.backend.can_estimate_fee():
            default_target = 3
            self._fee_rate = await self.backend.estimate_fee(default_target)
            if self._fee_rate < self._minimum_fee_rate_sat_vb:
                logger.bind(sensitive=True).info(
                    f"Estimated fee {self._fee_rate:.2f} sat/vB is below required minimum "
                    f"{self._minimum_fee_rate_sat_vb:.2f} sat/vB, using required minimum"
                )
                self._fee_rate = self._minimum_fee_rate_sat_vb
            enforce_fee_rate_cap(
                self._fee_rate, self.config.max_fee_rate_sat_vb, source="backend estimate"
            )
            logger.bind(sensitive=True).info(
                f"Fee estimation for {default_target} blocks (default): {self._fee_rate:.2f} sat/vB"
            )
            self._apply_fee_randomization()
            return self._fee_rate

        # 4. Neutrino backend without manual fee - fall back to 1.0 sat/vB
        fallback_rate = self._minimum_fee_rate_sat_vb
        logger.bind(sensitive=True).warning(
            f"Fee estimation is not available with the neutrino backend and no --fee-rate "
            f"was specified. Falling back to {fallback_rate} sat/vB."
        )
        self._fee_rate = fallback_rate
        enforce_fee_rate_cap(self._fee_rate, self.config.max_fee_rate_sat_vb, source="fallback")
        self._apply_fee_randomization()
        return self._fee_rate

    def _verify_unsigned_minimum_miner_fee(self) -> str | None:
        """Validate known prevout values before !tx exposes the signing request."""
        if self._minimum_fee_rate_sat_vb is None:
            return "Minimum CoinJoin miner fee rate was not resolved"
        try:
            tx = deserialize_transaction(self.unsigned_tx)
        except TransactionSigningError as exc:
            return f"Cannot verify unsigned CoinJoin miner fee: {exc}"
        total_input = sum(utxo.value for utxo in self.selected_utxos) + sum(
            utxo["value"] for session in self.maker_sessions.values() for utxo in session.utxos
        )
        fee = total_input - sum(output.value for output in tx.outputs)
        vsize = estimate_p2wpkh_vsize(len(tx.inputs), len(tx.outputs))
        if fee < 0:
            return "CoinJoin has a negative miner fee"
        if not fee_rate_meets_minimum(fee, vsize, self._minimum_fee_rate_sat_vb):
            return (
                f"CoinJoin miner fee rate {fee / vsize:.2f} sat/vB is below required "
                f"{self._minimum_fee_rate_sat_vb:.2f} sat/vB before requesting signatures"
            )
        return None

    def _apply_fee_randomization(self) -> None:
        """Apply tx_fee_factor randomization to get the session's fee rate.

        This is called once per CoinJoin session to determine the randomized
        fee rate used for all fee calculations. The randomization provides
        privacy by varying the fee rate within the configured range.

        The randomized rate is stored in self._randomized_fee_rate and used
        by _estimate_tx_fee() for all calculations.
        """
        if self._fee_rate is None:
            return

        base_rate = self._fee_rate

        if self.config.tx_fee_factor > 0:
            # Randomize between base and base * (1 + factor)
            upper_rate = min(
                base_rate * (1 + self.config.tx_fee_factor),
                self.config.max_fee_rate_sat_vb,
            )
            self._randomized_fee_rate = secure_random.uniform(
                base_rate,
                upper_rate,
            )
            enforce_fee_rate_cap(
                self._randomized_fee_rate,
                self.config.max_fee_rate_sat_vb,
                source="randomized",
            )
            logger.bind(sensitive=True).info(
                f"Randomized fee rate: {self._randomized_fee_rate:.2f} sat/vB "
                f"(base={base_rate:.2f}, factor={self.config.tx_fee_factor})"
            )
        else:
            self._randomized_fee_rate = base_rate
            logger.bind(sensitive=True).info(
                "Fee rate randomization disabled (factor=0); using {:.2f} sat/vB", base_rate
            )

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

    def _record_signer_decline(self, nick: str, error_msg: object) -> tuple[float, float] | None:
        """Record a maker's explicit signing refusal without exposing peer text."""
        low_fee_decline = parse_low_fee_error(error_msg)
        if low_fee_decline is not None:
            proposed_fee_rate, minimum_fee_rate = low_fee_decline
            logger.info(
                "Maker {} declined to sign: proposed miner fee rate {:.4f} sat/vB, "
                "required minimum {:.4f} sat/vB",
                nick,
                proposed_fee_rate,
                minimum_fee_rate,
            )
        else:
            logger.warning(f"Maker {nick} declined to sign")
        logger.bind(sensitive=True).warning("Maker {} declined to sign: {}", nick, error_msg)
        self.declined_signer_nicks.add(nick)
        del self.maker_sessions[nick]
        return low_fee_decline

    def _finalize_declined_history(self, low_fee_decline: tuple[float, float] | None) -> str:
        """Finalize the pre-sign history row for a definitive maker refusal."""
        failure_reason = "Maker declined signing"
        if low_fee_decline is not None:
            proposed_fee_rate, minimum_fee_rate = low_fee_decline
            failure_reason = (
                "Maker declined signing: proposed miner fee rate "
                f"{proposed_fee_rate:.4f} sat/vB, required minimum "
                f"{minimum_fee_rate:.4f} sat/vB"
            )

        if self.signing_boundary_crossed:
            return failure_reason

        try:
            finalized = mark_pending_transaction_failed(
                destination_address=self.cj_destination,
                failure_reason=failure_reason,
                data_dir=self.config.data_dir,
                txid="",
                wallet_fingerprint=self.wallet.wallet_fingerprint,
            )
            if not finalized:
                logger.warning(
                    "Could not find pending CoinJoin history entry to finalize after maker decline"
                )
        except HistoryWriteError as exc:
            logger.warning("Could not finalize declined CoinJoin history entry")
            logger.bind(sensitive=True).warning(
                "Declined CoinJoin history finalization detail: {}", exc
            )
        return failure_reason

    async def _phase_collect_signatures(self) -> bool:
        """Send !tx and collect !sig responses from makers.

        The reference maker sends signatures in TRANSACTION INPUT ORDER, not in the
        order UTXOs were originally provided. We must match signatures to transaction
        inputs by verifying which UTXO each signature is valid for, not by index.
        """
        if self._minimum_fee_rate_sat_vb is not None:
            fee_error = self._verify_unsigned_minimum_miner_fee()
            if fee_error is not None:
                self.last_failure_reason = fee_error
                logger.error("Unsigned transaction does not meet the minimum miner fee")
                logger.bind(sensitive=True).error("Minimum miner fee detail: {}", fee_error)
                return False

        # Encode transaction as base64 (expected by maker after decryption)
        import base64

        tx_b64 = base64.b64encode(self.unsigned_tx).decode("ascii")

        # Record history BEFORE sending !tx to makers.
        # This ensures addresses are persisted before they're revealed in the transaction.
        # If we crash after sending !tx but before broadcast, the addresses won't be reused.
        try:
            total_maker_fees = sum(self.maker_fee_plan().values())
            maker_nicks = list(self.maker_sessions.keys())
            destination_vout = self._get_taker_cj_output_index()

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
                broadcast_method="",
                broadcast_policy=self.config.tx_broadcast.value,
                network=(self.config.bitcoin_network or self.config.network).value,
                failure_reason="Awaiting transaction",
                wallet_fingerprint=self.wallet.wallet_fingerprint,
                source_addresses=[utxo.address for utxo in self.selected_utxos],
                destination_vout=destination_vout if destination_vout is not None else -1,
            )
            append_history_entry(history_entry, data_dir=self.config.data_dir)

            logger.bind(sensitive=True).debug(
                f"Recorded pre-broadcast history entry for CJ to {self.cj_destination[:20]}..."
                + (" (no change)" if not self.taker_change_address else "")
            )
        except HistoryWriteError as e:
            logger.error("Aborting CoinJoin to prevent address reuse")
            logger.bind(sensitive=True).error("History write error detail: {}", e)
            return False

        # Send ENCRYPTED !tx to each maker
        for nick, session in self.maker_sessions.items():
            if session.crypto is None:
                logger.error(f"No encryption session for {nick}")
                continue

            # Opportunistically upgrade to a direct connection if one became
            # available since the previous phase (mirrors the reference taker).
            session.comm_channel = self.directory_client.upgrade_channel_prefer_direct(
                nick, session.comm_channel
            )

            encrypted_tx = session.crypto.encrypt(tx_b64)
            # Verify ownership immediately before every delivery. Maker-only
            # signatures cannot spend taker inputs, so they do not cross the
            # local signing boundary.
            if not self.renew_input_locks(f"send !tx to maker {nick}"):
                return False
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
            logger.error("Failed to deserialize transaction")
            logger.bind(sensitive=True).error("Transaction deserialization detail: {}", e)
            return False

        # Build a map of input_index -> (txid_hex, vout)
        input_map: dict[int, tuple[str, int]] = {}
        for idx, tx_input in enumerate(tx.inputs):
            txid_hex = tx_input.txid_le[::-1].hex()
            input_map[idx] = (txid_hex, tx_input.vout)

        # Process responses
        low_fee_decline: tuple[float, float] | None = None
        for nick in list(self.maker_sessions.keys()):
            if nick in responses:
                if responses[nick].get("error"):
                    parsed_low_fee_error = self._record_signer_decline(
                        nick, responses[nick].get("data", "")
                    )
                    if low_fee_decline is None:
                        low_fee_decline = parsed_low_fee_error
                    continue

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

                    for sig_idx, response_data in enumerate(response_data_list):
                        parts = response_data.strip().split()
                        if not parts:
                            continue

                        encrypted_data = parts[0]
                        decrypted_sig = session.crypto.decrypt(encrypted_data)

                        # Parse signature (same as before)
                        padding_needed = (4 - len(decrypted_sig) % 4) % 4
                        padded_sig = decrypted_sig + "=" * padding_needed
                        sig_bytes = base64.b64decode(padded_sig)
                        sig_len = sig_bytes[0]
                        signature = sig_bytes[1 : 1 + sig_len]
                        pub_len = sig_bytes[1 + sig_len]
                        pubkey = sig_bytes[2 + sig_len : 2 + sig_len + pub_len]

                        # Try to verify against each of maker's inputs
                        matched_input_idx = None

                        for idx in maker_input_indices:
                            if idx in matched_indices:
                                continue

                            txid, vout = input_map[idx]
                            utxo = maker_utxo_map[(txid, vout)]
                            value = utxo["value"]

                            # Bind the maker-supplied pubkey to this UTXO's own
                            # scriptPubKey. Without this a signature by any key
                            # verifies for a UTXO the maker does not control,
                            # yielding a consensus-invalid coinjoin.
                            utxo_spk = utxo.get("scriptpubkey", "")
                            if not utxo_spk or bytes.fromhex(utxo_spk) != pubkey_to_p2wpkh_script(
                                pubkey
                            ):
                                continue

                            # Create scriptCode for verification
                            script_code = create_p2wpkh_script_code(pubkey)

                            if verify_p2wpkh_signature(
                                tx, idx, script_code, value, signature, pubkey
                            ):
                                matched_input_idx = idx
                                break

                        if matched_input_idx is not None:
                            matched_indices.add(matched_input_idx)
                            txid, vout = input_map[matched_input_idx]
                            witness = [signature.hex(), pubkey.hex()]

                            sig_infos.append({"txid": txid, "vout": vout, "witness": witness})
                            logger.bind(sensitive=True).debug(
                                f"Verified signature from {nick} matches input {matched_input_idx} "
                                f"({txid[:16]}...:{vout})"
                            )
                        else:
                            logger.warning(
                                f"Signature #{sig_idx + 1} from {nick} "
                                "did not verify against any input"
                            )
                            logger.bind(sensitive=True).debug(
                                f"  Unverified sig pubkey={pubkey.hex()[:32]}..., "
                                f"tried inputs={maker_input_indices}, "
                                f"already matched={sorted(matched_indices)}"
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
                    logger.warning("Invalid !sig response from maker")
                    logger.bind(sensitive=True).warning(
                        "Invalid !sig response from {}: {}", nick, e
                    )
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
            self.failed_signer_nicks.update(missing_makers - self.declined_signer_nicks)
            missing_signature_reason = (
                f"Missing or invalid signatures from maker(s): {', '.join(sorted(missing_makers))}"
            )
            if self.declined_signer_nicks:
                decline_failure_reason = self._finalize_declined_history(low_fee_decline)
                self.last_failure_reason = f"{decline_failure_reason}; {missing_signature_reason}"
            else:
                self.last_failure_reason = missing_signature_reason
            logger.error("Missing signatures from required makers")
            logger.bind(sensitive=True).error(
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

        logger.bind(sensitive=True).info("Signed tx: {} bytes", len(self.final_tx))
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

            # Verify ownership before starting local signing. A renewal failure
            # still leaves the round safely releasable.
            if not self.renew_input_locks("sign taker inputs"):
                return []

            # Sign each of our UTXOs
            for utxo in self.selected_utxos:
                # Find the input index in the transaction
                utxo_key = (utxo.txid, utxo.vout)
                if utxo_key not in input_index_map:
                    logger.error("Selected UTXO was not found in transaction inputs")
                    logger.bind(sensitive=True).error(
                        "Missing selected UTXO: {}:{}", utxo.txid, utxo.vout
                    )
                    continue

                input_index = input_index_map[utxo_key]

                # Safety check: Fidelity bond (P2WSH) UTXOs should never be in CoinJoins
                if utxo.is_p2wsh:
                    raise TransactionSigningError(
                        f"Cannot sign P2WSH UTXO {utxo.txid}:{utxo.vout} in CoinJoin - "
                        f"fidelity bond UTXOs cannot be used in CoinJoins"
                    )

                # Delegate key access and signing to the wallet so private keys
                # never leave the wallet (issue #518).
                # This is the local signing boundary. Maker signatures alone
                # cannot spend taker inputs.
                self.signing_boundary_crossed = True
                signed = self.wallet.sign_input(tx, input_index, utxo)

                signatures_info.append(
                    {
                        "txid": utxo.txid,
                        "vout": utxo.vout,
                        "signature": signed.signature.hex(),
                        "pubkey": signed.pubkey.hex(),
                        "witness": [item.hex() for item in signed.witness],
                    }
                )

                logger.bind(sensitive=True).debug(
                    "Signed input {} for UTXO {}:{}", input_index, utxo.txid, utxo.vout
                )

            logger.info(f"Signed {len(signatures_info)} taker inputs")
            return signatures_info

        except TransactionSigningError as e:
            logger.error("Transaction signing error")
            logger.bind(sensitive=True).error("Transaction signing error detail: {}", e)
            return []
        except Exception as e:
            logger.error("Failed to sign transaction")
            logger.bind(sensitive=True).error("Transaction signing error detail: {}", e)
            return []

    def _log_manual_csv_entry(
        self, total_maker_fees: int, mining_fee: int, destination: str
    ) -> None:
        """
        Log a CSV entry that can be manually added for tracking unbroadcast transactions.

        When users decline to broadcast or want to broadcast manually, this logs
        the CSV entry they can add to history.csv for tracking.
        """
        try:
            txid = get_txid(self.final_tx.hex())
            maker_nicks = list(self.maker_sessions.keys())
            destination_vout = self._get_taker_cj_output_index()

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
                broadcast_method="",
                broadcast_policy=self.config.tx_broadcast.value,
                network=(self.config.bitcoin_network or self.config.network).value,
                failure_reason="User declined broadcast (manual broadcast pending)",
                wallet_fingerprint=self.wallet.wallet_fingerprint,
                source_addresses=[utxo.address for utxo in self.selected_utxos],
                destination_vout=destination_vout if destination_vout is not None else -1,
            )

            # Format as CSV line for manual addition
            from dataclasses import fields

            fieldnames = [f.name for f in fields(history_entry)]
            values = [str(getattr(history_entry, f)) for f in fieldnames]

            logger.info("Manual broadcast history entry generated")
            sensitive_logger = logger.bind(sensitive=True)
            sensitive_logger.info("-" * 70)
            sensitive_logger.info("MANUAL CSV ENTRY - Add to history.csv if broadcasting manually:")
            sensitive_logger.info(f"txid: {txid}")
            sensitive_logger.info(f"CSV line: {','.join(values)}")
            sensitive_logger.info("-" * 70)
        except Exception as e:
            logger.warning("Failed to generate manual CSV entry")
            logger.bind(sensitive=True).warning("Manual CSV generation detail: {}", e)

    async def _revalidate_inputs_before_broadcast(self) -> tuple[bool, str]:
        """Recheck every known input against the current chain and mempool view."""
        inputs: dict[tuple[str, int], tuple[str, int | None, bool]] = {
            (utxo.txid, utxo.vout): (utxo.scriptpubkey, utxo.height, True)
            for utxo in self.selected_utxos
        }
        for session in self.maker_sessions.values():
            for utxo in session.utxos:
                inputs.setdefault(
                    (utxo["txid"], utxo["vout"]),
                    (utxo.get("scriptpubkey", ""), utxo.get("blockheight"), False),
                )

        for (txid, vout), (scriptpubkey, blockheight, wallet_owned) in inputs.items():
            try:
                if self.backend.requires_neutrino_metadata():
                    if not scriptpubkey or blockheight is None:
                        return False, f"missing verification metadata for {txid}:{vout}"
                    verify = (
                        self.backend.verify_wallet_utxo_with_metadata
                        if wallet_owned
                        else self.backend.verify_utxo_with_metadata
                    )
                    result = await verify(
                        txid=txid,
                        vout=vout,
                        scriptpubkey=scriptpubkey,
                        blockheight=blockheight,
                    )
                    if not result.valid or result.confirmations <= 0:
                        return False, f"input {txid}:{vout} is no longer available"
                else:
                    backend_utxo = await self.backend.get_utxo(txid, vout)
                    if backend_utxo is None or backend_utxo.confirmations <= 0:
                        return False, f"input {txid}:{vout} is no longer available"
            except Exception as e:
                return False, f"failed to revalidate input {txid}:{vout}: {e}"

        return True, ""

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
        - A watched-mempool tracker can verify unconfirmed transactions by txid
        - When the backend has no mempool access, all non-SELF policies fall back
          to broadcasting to ALL available makers simultaneously (like MULTIPLE_PEERS
          with peer_count = all makers). Verification is skipped; the
          pending-transaction monitor confirms the txid via block scanning.
          This maximises the probability that the tx reaches the network and avoids
          the privacy-leaking self-broadcast fallback (issue #482).

        Returns:
            Transaction ID if successful, empty string otherwise
        """
        import base64

        inputs_valid, validation_error = await self._revalidate_inputs_before_broadcast()
        if not inputs_valid:
            logger.error("Refusing to broadcast after input revalidation")
            logger.bind(sensitive=True).error("Input revalidation detail: {}", validation_error)
            return ""
        if not self.renew_input_locks("broadcast transaction"):
            return ""

        policy = self.config.tx_broadcast
        self.broadcast_policy = policy.value
        self.broadcast_method = ""
        self.broadcast_fallback_reason = ""
        has_mempool = self.backend.has_mempool_access()
        logger.debug(f"Broadcasting with policy: {policy.value}, mempool_access: {has_mempool}")

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

        # Without mempool access we cannot verify that any individual maker
        # broadcast the transaction. Sending to a single random maker and
        # "trusting" it is risky – if that maker is offline the tx is lost
        # and we would fall back to self-broadcast (privacy leak). Instead,
        # send to ALL makers simultaneously. All of them already know the
        # transaction so this reveals nothing new, and it maximises the
        # probability that at least one relays it to the Bitcoin network.
        # The pending-transaction monitor will confirm via block scanning.
        if not has_mempool and maker_nicks:
            logger.info(
                f"Backend has no mempool access – broadcasting !push to all "
                f"{len(maker_nicks)} maker(s) for reliability (issue #482)"
            )
            success_count = await self._broadcast_to_all_makers(maker_nicks, tx_b64)
            if success_count > 0:
                self.broadcast_method = f"makers-unverified:{success_count}"
                logger.info(
                    "!push delivery accepted; transaction will be confirmed via block monitoring"
                )
                logger.bind(sensitive=True).info(
                    "!push delivered to {}/{} maker(s); transaction {} will be confirmed via block "
                    "monitoring",
                    success_count,
                    len(maker_nicks),
                    expected_txid,
                )
                return expected_txid
            # Every send_privmsg raised – fall through to policy-specific handling
            # (NOT_SELF will return ""; others may self-broadcast).
            logger.warning("All !push sends failed (no mempool access path)")
            if policy == BroadcastPolicy.NOT_SELF:
                logger.error("NOT_SELF policy: all maker !push attempts failed")
                logger.bind(sensitive=True).error(
                    "Transaction hex for manual broadcast: {}", self.final_tx.hex()
                )
                return ""
            return await self._broadcast_self(fallback_reason="peer_delivery_failed")

        elif policy == BroadcastPolicy.RANDOM_PEER:
            # Try makers in random order, fall back to self as last resort
            if not maker_nicks:
                return await self._broadcast_self(fallback_reason="no_makers_available")

            secure_random.shuffle(maker_nicks)

            for candidate in maker_nicks:
                txid = await self._broadcast_via_maker(candidate, tx_b64)
                if txid:
                    return txid

            # Last resort: self-broadcast
            return await self._broadcast_self(fallback_reason="maker_broadcast_unverified")

        elif policy == BroadcastPolicy.MULTIPLE_PEERS:
            # Broadcast to N random makers simultaneously, fall back to self
            if not maker_nicks:
                return await self._broadcast_self(fallback_reason="no_makers_available")

            # Select N random makers (or all if less than N)
            peer_count = min(self.config.broadcast_peer_count, len(maker_nicks))
            selected_peers = secure_random.sample(maker_nicks, peer_count)

            success_count = await self._broadcast_to_all_makers(selected_peers, tx_b64)

            if success_count > 0:
                self.broadcast_method = f"makers-unverified:{success_count}"
                if has_mempool:
                    logger.info(
                        f"Broadcast sent to {success_count}/{peer_count} makers "
                        "(MULTIPLE_PEERS policy)."
                    )
                else:
                    logger.info(
                        "Broadcast sent to makers and will be confirmed via block monitoring"
                    )
                    logger.bind(sensitive=True).info(
                        "Broadcast sent to {}/{} makers. "
                        "Transaction {} will be confirmed via block monitoring",
                        success_count,
                        peer_count,
                        expected_txid,
                    )
                return expected_txid

            # All peers failed, fall back to self
            return await self._broadcast_self(fallback_reason="peer_delivery_failed")

        elif policy == BroadcastPolicy.NOT_SELF:
            # Only makers can broadcast - no self fallback
            if not maker_nicks:
                logger.error("NOT_SELF policy but no makers available")
                return ""

            # Try makers in random order with verification
            secure_random.shuffle(maker_nicks)

            for maker_nick in maker_nicks:
                txid = await self._broadcast_via_maker(maker_nick, tx_b64)
                if txid:
                    return txid

            # No fallback for NOT_SELF - log the transaction for manual broadcast
            logger.error("All maker broadcast attempts failed")
            logger.bind(sensitive=True).error(
                "Transaction hex for manual broadcast: {}", self.final_tx.hex()
            )
            return ""

        else:
            # Unknown policy, fallback to self
            return await self._broadcast_self(fallback_reason="unknown_policy")

    async def _broadcast_to_all_makers(self, maker_nicks: list[str], tx_b64: str) -> int:
        """
        Send !push to all makers simultaneously for redundant broadcast.

        Used in two situations:
          - ``MULTIPLE_PEERS`` policy with a configured peer count (the
            normal multi-peer broadcast).
          - Backends without mempool access (``has_mempool_access() ==
            False``): we cannot verify that any single maker actually
            broadcast the tx, so we fan out to every available maker
            and rely on block-based confirmation. With the m0wer
            neutrino-api fork's watched mempool tracker enabled this
            path is no longer the default for neutrino.

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
                if not self.renew_input_locks(f"broadcast through maker {nick}"):
                    return False
                # Get the comm_channel from maker_sessions if available
                session = self.maker_sessions.get(nick)
                force_channel = session.comm_channel if session else None
                await self.directory_client.send_privmsg(
                    nick, "push", tx_b64, log_routing=True, force_channel=force_channel
                )
                return True
            except Exception as e:
                logger.warning("Failed to send !push to maker")
                logger.bind(sensitive=True).warning("!push delivery detail for {}: {}", nick, e)
                return False

        # Send to all makers concurrently
        results = await asyncio.gather(*[send_push(nick) for nick in maker_nicks])

        success_count = sum(1 for r in results if r)
        logger.info(f"!push sent to {success_count}/{len(maker_nicks)} makers")

        return success_count

    async def _broadcast_self(self, *, fallback_reason: str = "") -> str:
        """
        Broadcast transaction via our own backend.

        Handles the case where a maker may have already broadcast the transaction,
        which would cause our broadcast to fail with "inputs already spent" or
        "already in mempool". In these cases, we verify the transaction exists
        and treat it as success.
        """
        if not self.renew_input_locks("broadcast through the local backend"):
            return ""
        builder = CoinJoinTxBuilder(self.config.bitcoin_network or self.config.network)
        expected_txid = builder.get_txid(self.final_tx)
        if fallback_reason:
            self.broadcast_fallback_reason = fallback_reason
            logger.warning(
                "PRIVACY WARNING: {} policy is using local self-broadcast because {}. "
                "The local backend can associate this CoinJoin with this client.",
                self.broadcast_policy or self.config.tx_broadcast.value,
                fallback_reason,
            )
            logger.bind(sensitive=True).warning(
                "Self-broadcast fallback transaction: {}", expected_txid
            )
        try:
            txid = await self.backend.broadcast_transaction(self.final_tx.hex())
            if not isinstance(txid, str) or txid.lower() != expected_txid:
                logger.error("Local backend returned an unexpected transaction ID")
                logger.bind(sensitive=True).error(
                    "Local backend returned txid {!r}; expected {}", txid, expected_txid
                )
                return ""
            self.broadcast_method = "self-fallback" if fallback_reason else "self"
            logger.info("Broadcast via self successful")
            logger.bind(sensitive=True).info("Broadcast via self successful: {}", txid)
            return expected_txid
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
                    "Self-broadcast rejected, checking whether a maker broadcast the transaction"
                )
                logger.bind(sensitive=True).info("Self-broadcast rejection detail: {}", e)

                # Calculate expected txid and verify the CoinJoin output exists
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
                    self.broadcast_method = "self-fallback" if fallback_reason else "self"
                    logger.info("Transaction was already broadcast by a maker")
                    logger.bind(sensitive=True).info(
                        "Transaction already broadcast by maker: {}", expected_txid
                    )
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
                    self.broadcast_method = "self-fallback" if fallback_reason else "self"
                    logger.info("Transaction confirmed after propagation delay")
                    logger.bind(sensitive=True).info(
                        "Transaction confirmed after propagation delay: {}", expected_txid
                    )
                    return expected_txid

                logger.warning("Self-broadcast failed and transaction was not found")
                logger.bind(sensitive=True).warning("Self-broadcast failure detail: {}", e)
                return ""

            logger.warning("Self-broadcast failed")
            logger.bind(sensitive=True).warning("Self-broadcast failure detail: {}", e)
            return ""

    async def _broadcast_via_maker(self, maker_nick: str, tx_b64: str) -> str:
        """
        Request a maker to broadcast the transaction.

        Sends !push command and waits briefly for the transaction to appear.
        We don't expect a response from the maker - they broadcast unquestioningly.

        Verification first checks exact-txid visibility through get_transaction().
        For Neutrino this uses the watched-mempool tracker. Output verification is
        retained as a fallback in case the transaction confirms and leaves the tracker
        before it is observed here.

        Args:
            maker_nick: The maker's nick to send the push request to
            tx_b64: Base64-encoded signed transaction

        Returns:
            Transaction ID if broadcast detected, empty string otherwise
        """
        try:
            start_time = time.monotonic()
            deadline = start_time + self.config.broadcast_timeout_sec
            logger.info(f"Requesting broadcast via maker: {maker_nick}")

            if not self.renew_input_locks(f"broadcast through maker {maker_nick}"):
                return ""

            # Send !push to the maker (unencrypted, like reference implementation)
            # Use the same comm_channel as the rest of the session
            session = self.maker_sessions.get(maker_nick)
            force_channel = session.comm_channel if session else None
            remaining = deadline - time.monotonic()
            async with asyncio.timeout(remaining):
                await self.directory_client.send_privmsg(
                    maker_nick, "push", tx_b64, log_routing=True, force_channel=force_channel
                )

            # Calculate the expected txid
            builder = CoinJoinTxBuilder(self.config.bitcoin_network or self.config.network)
            expected_txid = builder.get_txid(self.final_tx)

            # Get current block height for Neutrino optimization
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    current_height = await self.backend.get_block_height()
            except Exception as e:
                logger.debug("Could not get block height, proceeding without a hint")
                logger.bind(sensitive=True).debug("Block height lookup detail: {}", e)
                current_height = None

            # Get taker's CJ output index for verification
            taker_cj_vout = self._get_taker_cj_output_index()
            if taker_cj_vout is None:
                logger.warning(
                    "Could not find taker CJ output index; chain-output fallback is unavailable"
                )

            # Also get change output for additional verification
            taker_change_vout = self._get_taker_change_output_index()

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                try:
                    async with asyncio.timeout(remaining):
                        try:
                            tx = await self.backend.get_transaction(expected_txid)
                        except Exception as e:
                            logger.debug(
                                "Exact transaction lookup failed, trying output verification"
                            )
                            logger.bind(sensitive=True).debug(
                                "Exact transaction lookup failed for {}: {}", expected_txid, e
                            )
                            tx = None
                        if tx is not None and tx.txid == expected_txid:
                            self.broadcast_method = f"maker:{maker_nick}"
                            total_time = time.monotonic() - start_time
                            logger.info("Transaction broadcast via maker detected in mempool")
                            logger.bind(sensitive=True).info(
                                "Transaction broadcast via {} detected in mempool: {} "
                                "(total: {:.2f}s)",
                                maker_nick,
                                expected_txid,
                                total_time,
                            )
                            return expected_txid

                        # Neutrino removes transactions from its watched-mempool endpoint
                        # after confirmation. Preserve address-based chain verification for
                        # that race and for backends without arbitrary txid lookup.
                        cj_verified = False
                        if taker_cj_vout is not None and self.cj_destination:
                            cj_verified = await self.backend.verify_tx_output(
                                txid=expected_txid,
                                vout=taker_cj_vout,
                                address=self.cj_destination,
                                start_height=current_height,
                            )
                        change_verified = True
                        if taker_change_vout is not None and self.taker_change_address:
                            change_verified = await self.backend.verify_tx_output(
                                txid=expected_txid,
                                vout=taker_change_vout,
                                address=self.taker_change_address,
                                start_height=current_height,
                            )

                        if cj_verified and change_verified:
                            self.broadcast_method = f"maker:{maker_nick}"
                            total_time = time.monotonic() - start_time
                            logger.info("Transaction broadcast via maker confirmed on chain")
                            logger.bind(sensitive=True).info(
                                "Transaction broadcast via {} confirmed on chain: {} "
                                "(total: {:.2f}s)",
                                maker_nick,
                                expected_txid,
                                total_time,
                            )
                            return expected_txid
                except TimeoutError:
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(2.0, remaining))

            # Could not verify broadcast
            total_time = time.monotonic() - start_time
            logger.debug("Could not confirm broadcast via maker within the timeout")
            logger.bind(sensitive=True).debug(
                "Could not confirm broadcast via {}: transaction {} was not visible within {:.2f}s",
                maker_nick,
                expected_txid,
                total_time,
            )
            return ""

        except Exception as e:
            logger.warning("Broadcast via maker failed")
            logger.bind(sensitive=True).warning(
                "Broadcast via maker {} failure detail: {}", maker_nick, e
            )
            return ""
