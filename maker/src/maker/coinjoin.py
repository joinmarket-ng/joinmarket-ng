"""
CoinJoin protocol handler for makers.

Manages the maker side of the CoinJoin protocol:
1. !fill - Taker requests to fill order
2. !pubkey - Maker sends commitment pubkey
3. !auth - Taker sends PoDLE proof (VERIFY!)
4. !ioauth - Maker sends selected UTXOs
5. !tx - Taker sends unsigned transaction (VERIFY!)
6. !sig - Maker sends signatures
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from jmcore.bitcoin import parse_transaction
from jmcore.encryption import CryptoSession
from jmcore.fee_policy import (
    estimate_p2wpkh_vsize,
    fee_rate_meets_minimum,
    format_low_fee_error,
)
from jmcore.models import NetworkType, Offer, offer_output_script_type
from jmcore.podle import parse_podle_revelation, verify_podle, verify_podle_binding
from jmcore.protocol import (
    UTXOMetadata,
    format_utxo_list,
)
from jmwallet.backends.base import UTXO, BlockchainBackend
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import (
    TransactionSigningError,
    deserialize_transaction,
)
from loguru import logger

from maker.mixdepth_selection import MixdepthSelectionPolicy, mixdepth_attempt_order
from maker.offer_math import required_maker_input
from maker.tx_verification import find_output_index, verify_unsigned_transaction

if TYPE_CHECKING:
    # A maker without prepared channel buyouts never imports jmswap at runtime.
    from jmswap.coinjoin_funding import ChannelBuyout

MINER_FEE_PREVOUT_LOOKUP_TIMEOUT_SEC = 10.0
MINER_FEE_PREVOUT_LOOKUP_BATCH_SIZE = 10


def bound_buyout_mixdepth(
    buyout: ChannelBuyout,
    wallet: WalletService,
    backend: BlockchainBackend,
    pit_script_type: str,
) -> int:
    """Validate the wallet binding before advertising or accepting buyout rounds."""
    if pit_script_type != "p2tr" or getattr(wallet, "address_type", None) != "p2tr":
        raise ValueError("A channel buyout requires a Taproot wallet and a Taproot pit")
    if not backend.can_lookup_arbitrary_utxos():
        raise ValueError("A channel buyout requires a backend that can look up arbitrary UTXOs")
    binding = dict(getattr(buyout.buyer, "runtime_binding", None) or {})
    if buyout.terms.proposal.network != wallet.network or binding.get("network") != wallet.network:
        raise ValueError("Buyout and wallet Bitcoin networks differ")
    fingerprint = binding.get("wallet_fingerprint")
    if not isinstance(fingerprint, str) or fingerprint != wallet.wallet_fingerprint:
        raise ValueError("Buyout is not bound to this wallet")
    mixdepth = binding.get("mixdepth")
    if type(mixdepth) is not int:
        raise ValueError("Buyout is not bound to a wallet mixdepth")
    if not 0 <= mixdepth < wallet.mixdepth_count:
        raise ValueError(f"Buyout mixdepth {mixdepth} is outside this wallet")
    return mixdepth


class CoinJoinState(StrEnum):
    """CoinJoin session states"""

    IDLE = "idle"
    FILL_RECEIVED = "fill_received"
    PUBKEY_SENT = "pubkey_sent"
    AUTH_RECEIVED = "auth_received"
    IOAUTH_SEND_STARTED = "ioauth_send_started"
    IOAUTH_SENT = "ioauth_sent"
    TX_RECEIVED = "tx_received"
    SIG_SENT = "sig_sent"
    COMPLETE = "complete"
    FAILED = "failed"


class CoinJoinSession:
    """
    Manages a single CoinJoin session with a taker.
    """

    def __init__(
        self,
        taker_nick: str,
        offer: Offer,
        wallet: WalletService,
        backend: BlockchainBackend,
        min_confirmations: int = 1,
        taker_utxo_retries: int = 3,
        taker_utxo_age: int = 5,
        taker_utxo_amtpercent: int = 20,
        session_timeout_sec: int = 300,
        pre_sign_timeout_sec: int = 180,
        input_lock_ttl_sec: float = 3600,
        merge_algorithm: str = "default",
        restrict_md0: bool = True,
        minimum_fee_rate_sat_vb: float | None = None,
        mixdepth_selection_policy: MixdepthSelectionPolicy = MixdepthSelectionPolicy.BALANCED,
        buyout: ChannelBuyout | None = None,
    ):
        self.taker_nick = taker_nick
        self.offer = offer
        self.wallet = wallet
        self.backend = backend
        self.min_confirmations = min_confirmations
        self.taker_utxo_retries = taker_utxo_retries
        self.taker_utxo_age = taker_utxo_age
        self.taker_utxo_amtpercent = taker_utxo_amtpercent
        self.merge_algorithm = merge_algorithm  # UTXO selection strategy
        self.restrict_md0 = restrict_md0  # Mixdepth 0 UTXO merge restriction
        self.minimum_fee_rate_sat_vb = minimum_fee_rate_sat_vb
        self.mixdepth_selection_policy = mixdepth_selection_policy

        self.state = CoinJoinState.IDLE
        self.amount = 0
        self.our_utxos: dict[tuple[str, int], UTXOInfo] = {}
        self.cj_address = ""
        self.change_address = ""
        self.mixdepth = 0
        # Rigid pit (JMP-0010): the equal-output, change and input script types
        # are all fixed by the offer family (sw0 -> p2wpkh, tr0 -> p2tr). There
        # is no per-transaction or taker-chosen output type. A single-type
        # wallet only derives/spends one family, so the offer family must match
        # the wallet type or the maker cannot serve a uniform pit.
        self.pit_script_type = offer_output_script_type(offer.ordertype)
        wallet_type = getattr(wallet, "address_type", None)
        if wallet_type in ("p2wpkh", "p2tr") and self.pit_script_type != wallet_type:
            raise ValueError(
                f"Offer {offer.ordertype.value!r} implies a {self.pit_script_type!r} pit but "
                f"the wallet is {wallet_type!r}; a rigid JMP-0010 pit requires them to "
                f"match (advertise a {wallet_type!r} offer family)."
            )
        # Optional prepared channel buyout. Channel funding outputs fund this
        # round without ever becoming wallet UTXOs, so they are tracked apart
        # from ``our_utxos`` and are never signed by the wallet.
        self.buyout = buyout
        self.buyout_mixdepth = (
            -1
            if buyout is None
            else bound_buyout_mixdepth(buyout, wallet, backend, self.pit_script_type)
        )
        self.channel_prevouts: dict[tuple[str, int], tuple[int, bytes]] = {}
        self.channel_heights: dict[tuple[str, int], int | None] = {}
        self._parent_prevouts: dict[tuple[str, int], tuple[int, bytes]] = {}
        self.commitment = b""
        self.commitment_authenticated = False
        self.taker_nacl_pk = ""  # Taker's NaCl pubkey (hex) for btc_sig
        self.created_at = time.monotonic()
        self.session_timeout_sec = session_timeout_sec
        self.pre_sign_timeout_sec = pre_sign_timeout_sec
        self.deadline = self.created_at + session_timeout_sec
        # A pre-sign reservation only needs to survive until this session's
        # deadline. It is renewed for the longer pending-broadcast window
        # immediately before a signature can be produced.
        self.pending_broadcast_ttl_sec = float(input_lock_ttl_sec)
        self.input_lock_owner = secrets.token_hex(32)
        self.signing_boundary_crossed = False
        self.comm_channel = ""  # Track communication channel ("direct" or "dir:<node_id>")

        # Feature detection for extended UTXO format (neutrino_compat)
        # Initially, we use extended format if our own backend requires it (neutrino)
        # This will be updated to True if taker sends extended format during !auth
        self.peer_neutrino_compat = backend.requires_neutrino_metadata()

        # E2E encryption session with taker
        self.crypto = CryptoSession()

    @property
    def wallet_change_address(self) -> str:
        """Escrow change belongs in the buyout journal, not wallet address history."""
        return "" if self.buyout is not None else self.change_address

    def is_timed_out(self) -> bool:
        """Check if the session has exceeded the timeout."""
        return time.monotonic() >= self.deadline

    def _get_channel_type(self, source: str) -> str:
        """Extract channel type from source string.

        The JoinMarket protocol allows messages to arrive via different directory servers
        (takers broadcast to all directories), so we only track "direct" vs "directory"
        to prevent mixing those two channel types.

        Args:
            source: Message source ("direct" or "dir:<node_id>")

        Returns:
            "direct" or "directory"
        """
        if source == "direct":
            return "direct"
        if source.startswith("dir:"):
            return "directory"
        # Unknown source type, treat as its own type for safety
        return source

    def validate_channel(self, source: str) -> bool:
        """
        Record the channel a message arrived on (always accepts the message).

        We track "direct" vs "directory" only for diagnostics. Switching between
        them mid-session is legitimate: the reference implementation routes each
        privmsg opportunistically (``jmdaemon/onionmc.py::_privmsg``). A taker
        typically sends ``!fill`` via a directory while a direct connection is
        still being established, then sends ``!auth``/``!tx`` over the direct
        connection once it handshakes. This is normal, not an attack.

        Mixing channel types is harmless here because:
        - Anti-replay protection signs every privmsg with a fixed
          ``hostid="onion-network"`` (the reference implementation treats all
          onion channels as one host), so signatures are not bound to a single
          transport and cannot be replayed across an attacker-chosen channel.
        - The maker fans its own responses out over all directories regardless
          of ``comm_channel``, so the recorded channel never gates routing.

        Messages from different directory servers (dir:serverA vs dir:serverB)
        are likewise expected because takers broadcast to ALL directory servers.

        Args:
            source: Message source ("direct" or "dir:<node_id>")

        Returns:
            Always True. The return type is preserved for backward
            compatibility with callers that branch on the result.
        """
        source_type = self._get_channel_type(source)

        if not self.comm_channel:
            # First message - record the channel type.
            self.comm_channel = source_type
            logger.debug(f"Session with {self.taker_nick} established on channel: {source_type}")
            return True

        if self.comm_channel != source_type:
            # Legitimate opportunistic channel switch (e.g. directory -> direct).
            # Log at debug level and follow the taker to its new channel.
            logger.bind(sensitive=True).debug(
                f"Channel switch for {self.taker_nick}: "
                f"session started on '{self.comm_channel}', "
                f"now receiving on '{source_type}' (accepted)"
            )
            self.comm_channel = source_type

        return True

    async def handle_fill(
        self, amount: int, commitment: str, taker_pk: str
    ) -> tuple[bool, dict[str, Any]]:
        """
        Handle !fill message from taker.

        Args:
            amount: CoinJoin amount requested
            commitment: PoDLE commitment (will be verified later in !auth)
            taker_pk: Taker's NaCl public key for E2E encryption

        Returns:
            (success, response_data)
        """
        try:
            if self.is_timed_out():
                self.state = CoinJoinState.FAILED
                return False, {"error": f"Session timed out after {self.session_timeout_sec}s"}

            if self.state != CoinJoinState.IDLE:
                return False, {"error": "Session not in IDLE state"}

            if amount < self.offer.minsize:
                return False, {"error": f"Amount too small: {amount} < {self.offer.minsize}"}

            if amount > self.offer.maxsize:
                return False, {"error": f"Amount too large: {amount} > {self.offer.maxsize}"}

            self.amount = amount
            self.commitment = bytes.fromhex(commitment)
            self.taker_nacl_pk = taker_pk  # Store for btc_sig in handle_auth
            self.state = CoinJoinState.FILL_RECEIVED

            logger.bind(sensitive=True).debug(
                f"Received !fill from {self.taker_nick}: "
                f"amount={amount}, commitment={commitment[:16]}..., taker_pk={taker_pk[:16]}..."
            )

            # Set up E2E encryption with taker's NaCl pubkey
            try:
                self.crypto.setup_encryption(taker_pk)
                logger.debug(f"Set up encryption box with taker {self.taker_nick}")
            except Exception as e:
                logger.error("Failed to set up encryption with taker")
                logger.bind(sensitive=True).error(f"Failed to set up encryption with taker: {e}")
                return False, {"error": f"Invalid taker pubkey: {e}"}

            # Return our NaCl pubkey and features for E2E encryption setup
            # Format for !pubkey: <nacl_pubkey_hex> [features=<comma-separated>]
            # Features are optional - legacy peers won't send them
            nacl_pubkey = self.crypto.get_pubkey_hex()

            self.state = CoinJoinState.PUBKEY_SENT

            # Include features in the response
            # neutrino_compat: We support extended UTXO format (txid:vout:scriptpubkey:blockheight)
            # All modern makers can accept extended format (extra fields are simply ignored)
            features: list[str] = ["neutrino_compat"]

            return True, {"nacl_pubkey": nacl_pubkey, "features": features}

        except Exception as e:
            logger.error("Failed to handle !fill")
            logger.bind(sensitive=True).error(f"Failed to handle !fill: {e}")
            self.state = CoinJoinState.FAILED
            return False, {"error": str(e)}

    async def handle_auth(
        self,
        commitment: str,
        revelation: dict[str, Any],
        kphex: str,
        exclude_utxos: set[tuple[str, int]] | None = None,
        active_check: Callable[[], bool] | None = None,
        podle_admission: Callable[[tuple[str, int]], bool] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Handle !auth message from taker.

        CRITICAL SECURITY: Verifies PoDLE proof and taker's UTXO.

        Args:
            commitment: PoDLE commitment (should match from !fill)
            revelation: PoDLE revelation data
            kphex: Encryption key (hex)
            exclude_utxos: ``(txid, vout)`` outpoints already committed to other
                in-flight sessions; never selected as our inputs (see
                :meth:`_select_our_utxos`).

        Returns:
            (success, response_data with UTXOs or error)
        """
        try:
            if self.is_timed_out():
                self.state = CoinJoinState.FAILED
                return False, {"error": f"Session timed out after {self.session_timeout_sec}s"}
            if active_check is not None and not active_check():
                return False, {"error": "Session expired during authentication"}

            if self.state != CoinJoinState.PUBKEY_SENT:
                return False, {"error": "Session not in correct state for !auth"}

            commitment_bytes = bytes.fromhex(commitment)
            if commitment_bytes != self.commitment:
                logger.bind(sensitive=True).debug(
                    f"Commitment mismatch: received={commitment[:16]}..., "
                    f"expected={self.commitment.hex()[:16]}..."
                )
                return False, {"error": "Commitment mismatch"}

            parsed_rev = parse_podle_revelation(revelation)
            if not parsed_rev:
                logger.bind(sensitive=True).debug(f"Failed to parse PoDLE revelation: {revelation}")
                return False, {"error": "Invalid PoDLE revelation format"}

            logger.bind(sensitive=True).debug(
                f"PoDLE verification inputs: P={parsed_rev['P'].hex()}, "
                f"P2={parsed_rev['P2'].hex()}, sig={parsed_rev['sig'].hex()}, "
                f"e={parsed_rev['e'].hex()}, commitment={commitment}"
            )

            is_valid, error = verify_podle(
                parsed_rev["P"],
                parsed_rev["P2"],
                parsed_rev["sig"],
                parsed_rev["e"],
                commitment_bytes,
                index_range=range(self.taker_utxo_retries),
            )

            if not is_valid:
                utxo_str = f"{parsed_rev['txid'][:16]}...:{parsed_rev['vout']}"
                logger.warning("PoDLE verification failed")
                logger.bind(sensitive=True).warning(
                    f"PoDLE verification failed for {self.taker_nick}: {error} "
                    f"(commitment={commitment[:16]}..., utxo={utxo_str})"
                )
                return False, {
                    "error": f"PoDLE verification failed: {error}",
                    "error_code": "podle_proof_invalid",
                    "error_reason": "PoDLE proof verification failed",
                }

            logger.debug("PoDLE proof verified ✓")
            logger.bind(sensitive=True).debug(
                f"PoDLE details: taker={self.taker_nick}, "
                f"utxo={parsed_rev['txid']}:{parsed_rev['vout']}, "
                f"commitment={commitment}"
            )

            utxo_txid = parsed_rev["txid"]
            utxo_vout = parsed_rev["vout"]

            # Check for extended UTXO metadata (neutrino_compat feature)
            # The revelation may include scriptpubkey and blockheight
            taker_scriptpubkey = parsed_rev.get("scriptpubkey")
            taker_blockheight = parsed_rev.get("blockheight")

            # Track if taker sent extended format - we'll respond in kind
            taker_sent_extended = taker_scriptpubkey is not None and taker_blockheight is not None
            if taker_sent_extended:
                logger.debug("Taker sent extended UTXO format (neutrino_compat)")
                # Update our peer detection - taker supports neutrino_compat
                self.peer_neutrino_compat = True

            # Verify the taker's UTXO exists on the blockchain
            # Use Neutrino-compatible verification if backend requires it and metadata available
            if self.backend.requires_neutrino_metadata():
                if not taker_scriptpubkey or taker_blockheight is None:
                    # Neutrino backend cannot verify UTXOs without extended metadata.
                    # This happens when a legacy taker (e.g. reference implementation)
                    # picks this maker -- they don't send scriptpubkey/blockheight.
                    logger.warning("Neutrino backend cannot verify the taker UTXO")
                    logger.bind(sensitive=True).warning(
                        f"Neutrino backend cannot verify taker UTXO "
                        f"{utxo_txid[:16]}...:{utxo_vout} - "
                        f"taker did not send extended metadata (neutrino_compat). "
                        f"Taker should select a full-node maker instead."
                    )
                    return False, {
                        "error": "Neutrino backend requires extended UTXO metadata "
                        "(neutrino_compat) for verification",
                        "error_code": "neutrino_incompatible",
                    }

                # Neutrino backend: use metadata-based verification
                result = await self.backend.verify_utxo_with_metadata(
                    txid=utxo_txid,
                    vout=utxo_vout,
                    scriptpubkey=taker_scriptpubkey,
                    blockheight=taker_blockheight,
                )
                if active_check is not None and not active_check():
                    return False, {"error": "Session expired during UTXO verification"}
                if not result.valid:
                    return False, {
                        "error": f"Taker's UTXO verification failed: {result.error}",
                        "error_code": (
                            "utxo_verification_unavailable"
                            if result.unavailable
                            else "podle_utxo_invalid"
                        ),
                        "error_reason": "PoDLE UTXO verification failed",
                    }

                taker_utxo_value = result.value
                taker_utxo_confirmations = result.confirmations
                # verify_utxo_with_metadata confirmed this scriptpubkey matches
                # the on-chain output, so it is authoritative for binding.
                verified_scriptpubkey: str | None = taker_scriptpubkey
                logger.debug(
                    "PoDLE authorization UTXO verified via Neutrino "
                    f"(scriptpubkey_len={len(taker_scriptpubkey) // 2} bytes)"
                )
                logger.bind(sensitive=True).debug(
                    f"Neutrino-verified taker's UTXO: {utxo_txid}:{utxo_vout}"
                )
            else:
                # Full node: direct UTXO lookup
                taker_utxo = await self.backend.get_utxo(utxo_txid, utxo_vout)
                if active_check is not None and not active_check():
                    return False, {"error": "Session expired during UTXO verification"}

                if not taker_utxo:
                    return False, {
                        "error": "Taker's UTXO not found on blockchain",
                        "error_code": "podle_utxo_invalid",
                        "error_reason": "PoDLE UTXO verification failed",
                    }

                taker_utxo_value = taker_utxo.value
                taker_utxo_confirmations = taker_utxo.confirmations
                verified_scriptpubkey = taker_utxo.scriptpubkey
                logger.debug(
                    "PoDLE authorization UTXO verified via Bitcoin Core "
                    f"(scriptpubkey_len={len(verified_scriptpubkey) // 2} bytes)"
                )

            # Bind the PoDLE public key P to the UTXO's scriptPubKey. Without
            # this a taker could present a valid PoDLE for a key it owns while
            # referencing a stranger's UTXO. The scriptpubkey used here is the
            # authoritative on-chain value (full node lookup, or neutrino
            # metadata already confirmed against the chain).
            if verified_scriptpubkey:
                bound, bind_err = verify_podle_binding(parsed_rev["P"], verified_scriptpubkey)
                if not bound:
                    unsupported_script = bind_err.startswith("Unsupported ")
                    error_code = (
                        "podle_binding_unsupported_script"
                        if unsupported_script
                        else "podle_binding_mismatch"
                    )
                    logger.warning(f"PoDLE ownership binding failed: {bind_err}")
                    logger.bind(sensitive=True).warning(
                        f"PoDLE binding failed for {self.taker_nick}: {bind_err} "
                        f"(utxo={utxo_txid}:{utxo_vout}, "
                        f"scriptpubkey={verified_scriptpubkey}, P={parsed_rev['P'].hex()})"
                    )
                    return False, {
                        "error": f"PoDLE binding failed: {bind_err}",
                        "error_code": error_code,
                        "error_reason": "PoDLE ownership binding failed",
                    }
                logger.debug("PoDLE bound to UTXO scriptpubkey ✓")
            else:
                logger.warning("Could not verify PoDLE binding to UTXO")
                logger.bind(sensitive=True).warning(
                    f"No scriptpubkey available to bind PoDLE for "
                    f"{utxo_txid[:16]}...:{utxo_vout}; rejecting"
                )
                return False, {
                    "error": "Could not verify PoDLE binding to UTXO",
                    "error_code": "podle_binding_unavailable",
                    "error_reason": "PoDLE ownership binding failed",
                }

            if taker_utxo_confirmations < self.taker_utxo_age:
                logger.bind(sensitive=True).debug(
                    f"Taker UTXO too young: {utxo_txid}:{utxo_vout} has "
                    f"{taker_utxo_confirmations} confirmations, need {self.taker_utxo_age}"
                )
                return False, {
                    "error": f"Taker's UTXO too young: "
                    f"{taker_utxo_confirmations} < {self.taker_utxo_age}"
                }

            required_amount = int(self.amount * self.taker_utxo_amtpercent / 100)
            if taker_utxo_value < required_amount:
                logger.bind(sensitive=True).debug(
                    f"Taker UTXO too small: {utxo_txid}:{utxo_vout} has "
                    f"{taker_utxo_value} sats, need {required_amount} sats "
                    f"({self.taker_utxo_amtpercent}% of {self.amount})"
                )
                return False, {
                    "error": f"Taker's UTXO too small: {taker_utxo_value} < {required_amount}"
                }

            logger.debug("Taker's UTXO validated ✓")
            logger.bind(sensitive=True).debug(
                f"Taker UTXO details: {utxo_txid}:{utxo_vout}, "
                f"value={taker_utxo_value} sats, confirmations={taker_utxo_confirmations}"
            )
            self.commitment_authenticated = True

            if podle_admission is not None and not podle_admission((utxo_txid, utxo_vout)):
                logger.warning("Rejecting concurrent PoDLE authorization UTXO")
                logger.bind(sensitive=True).warning(
                    f"Rejecting concurrent PoDLE outpoint from {self.taker_nick}: "
                    f"{utxo_txid[:16]}...:{utxo_vout}"
                )
                return False, {
                    "error": "Maker is already processing this authorization UTXO",
                    "error_code": "authorization UTXO already active",
                }

            if self.buyout is not None:
                channel_error = await self._verify_channel_inputs(active_check)
                if channel_error is not None:
                    logger.warning("Rejecting CoinJoin: channel input verification failed")
                    logger.bind(sensitive=True).warning(
                        f"Channel input verification failed for {self.taker_nick}: {channel_error}"
                    )
                    return False, {"error": channel_error}

            utxos_dict, cj_addr, change_addr, mixdepth = await self._select_our_utxos(
                exclude_utxos=exclude_utxos,
                active_check=active_check,
            )

            if active_check is not None and not active_check():
                return False, {"error": "Session expired during maker input selection"}

            if not utxos_dict:
                return False, {
                    "error": "Failed to select UTXOs",
                    "error_code": "UTXO selection failed",
                }

            self.our_utxos = utxos_dict
            self.cj_address = cj_addr
            self.change_address = change_addr
            self.mixdepth = mixdepth

            # Format UTXOs: extended format (neutrino_compat) includes scriptpubkey:blockheight
            # Legacy format is just txid:vout
            utxo_metadata_list = [
                UTXOMetadata(
                    txid=txid,
                    vout=vout,
                    scriptpubkey=utxo_info.scriptpubkey,
                    blockheight=utxo_info.height,
                )
                for (txid, vout), utxo_info in utxos_dict.items()
            ]

            # Channel funding outputs are disclosed after the wallet inputs.
            # Their metadata is chain-derived (see _verify_channel_inputs), not
            # taken from the buyout terms.
            if self.buyout is not None:
                utxo_metadata_list.extend(
                    UTXOMetadata(
                        txid=item["txid"],
                        vout=item["vout"],
                        scriptpubkey=self.channel_prevouts[(item["txid"], item["vout"])][1].hex(),
                        blockheight=self.channel_heights[(item["txid"], item["vout"])],
                    )
                    for item in self.buyout.inputs
                )

            # Use extended format if peer supports neutrino_compat
            utxo_list_str = format_utxo_list(utxo_metadata_list, extended=self.peer_neutrino_compat)
            if self.peer_neutrino_compat:
                logger.debug("Using extended UTXO format for neutrino_compat peer")
            else:
                logger.debug("Using legacy UTXO format for legacy peer")

            # Get EC key for our first UTXO to sign taker's encryption key
            # This proves we own the UTXO we're contributing
            first_utxo_key, first_utxo_info = next(iter(utxos_dict.items()))
            auth_address = first_utxo_info.address
            auth_hd_key = self.wallet.get_key_for_address(auth_address)

            if auth_hd_key is None:
                return False, {"error": f"Could not get key for address {auth_address}"}

            # Get our EC pubkey (compressed)
            auth_pub_bytes = auth_hd_key.get_public_key_bytes()

            # Sign OUR OWN NaCl pubkey (hex string) with our EC key
            # This proves to the taker that we own the UTXO and links it to our encryption identity
            from jmcore.crypto import ecdsa_sign

            our_nacl_pk_hex = self.crypto.get_pubkey_hex()
            btc_sig = ecdsa_sign(our_nacl_pk_hex, auth_hd_key.get_private_key_bytes())

            response = {
                "utxo_list": utxo_list_str,
                "auth_pub": auth_pub_bytes.hex(),
                "cj_addr": cj_addr,
                "change_addr": change_addr,
                "btc_sig": btc_sig,
            }

            # Authentication is complete and our inputs are reserved, but the
            # outer session has not attempted to reveal them via !ioauth yet.
            self.state = CoinJoinState.AUTH_RECEIVED
            logger.debug(f"Prepared !ioauth with {len(utxos_dict)} UTXOs")

            return True, response

        except Exception as e:
            logger.error("Failed to handle !auth")
            logger.bind(sensitive=True).error(f"Failed to handle !auth: {e}")
            self.state = CoinJoinState.FAILED
            return False, {"error": str(e)}

    async def handle_tx(
        self, tx_hex: str, active_check: Callable[[], bool] | None = None
    ) -> tuple[bool, dict[str, Any]]:
        """
        Handle !tx message from taker.

        CRITICAL SECURITY: Verifies unsigned transaction before signing!

        Args:
            tx_hex: Unsigned transaction hex

        Returns:
            (success, response_data with signatures or error)
        """
        try:
            if self.is_timed_out():
                self.state = CoinJoinState.FAILED
                return False, {"error": f"Session timed out after {self.session_timeout_sec}s"}
            if active_check is not None and not active_check():
                return False, {"error": "Session expired before transaction verification"}

            if self.state != CoinJoinState.IOAUTH_SENT:
                return False, {"error": "Session not in correct state for !tx"}

            logger.debug(f"Received !tx from {self.taker_nick}, verifying...")
            logger.bind(sensitive=True).debug(f"Transaction hex to verify and sign: {tx_hex}")

            # Convert network string to NetworkType enum
            network = NetworkType(self.wallet.network)

            # Reference/JAM sw0 takers set nLockTime to the current block
            # height for anti-fee-sniping by default; the locktime check needs
            # our own view of the chain tip to validate that (a `None` height
            # fails closed, rejecting any height-based locktime).
            current_block_height: int | None = None
            try:
                current_block_height = await self.backend.get_block_height()
            except Exception as exc:  # noqa: BLE001 - fail closed, not fatal to the round
                logger.warning(f"Could not fetch chain tip for locktime check: {exc}")

            is_valid, error = verify_unsigned_transaction(
                tx_hex=tx_hex,
                our_utxos=self.our_utxos,
                cj_address=self.cj_address,
                change_address=self.change_address,
                amount=self.amount,
                cjfee=self.offer.cjfee,
                txfee=self.offer.txfee,
                offer_type=self.offer.ordertype,
                network=network,
                current_block_height=current_block_height,
                external_prevouts=self.channel_prevouts or None,
            )

            if not is_valid:
                logger.error("Transaction verification failed")
                logger.bind(sensitive=True).error(f"Transaction verification FAILED: {error}")
                self.state = CoinJoinState.FAILED
                return False, {"error": f"Transaction verification failed: {error}"}

            if (
                self.backend.can_lookup_arbitrary_utxos()
                and self.minimum_fee_rate_sat_vb is not None
            ):
                fee_policy_error = await self._verify_minimum_miner_fee(tx_hex, active_check)
                if fee_policy_error is not None:
                    logger.warning("Rejecting CoinJoin: miner-fee verification failed")
                    logger.bind(sensitive=True).warning(
                        f"Rejecting CoinJoin from {self.taker_nick}: {fee_policy_error}"
                    )
                    self.state = CoinJoinState.FAILED
                    return False, {"error": fee_policy_error}

            if self.buyout is not None:
                buyout_error = await self._validate_buyout_parent(
                    tx_hex, current_block_height, active_check
                )
                if buyout_error is not None:
                    logger.warning("Rejecting CoinJoin: buyout parent validation failed")
                    logger.bind(sensitive=True).warning(
                        f"Buyout parent validation failed for {self.taker_nick}: {buyout_error}"
                    )
                    self.state = CoinJoinState.FAILED
                    return False, {"error": buyout_error}

            logger.debug("Transaction verification PASSED ✓")
            self.state = CoinJoinState.TX_RECEIVED

            if self.is_timed_out():
                self.state = CoinJoinState.FAILED
                return False, {"error": f"Session timed out after {self.session_timeout_sec}s"}

            if active_check is not None and not active_check():
                return False, {"error": "Session expired before signing"}

            if not self.wallet.renew_coinjoin_inputs(
                set(self.our_utxos),
                owner=self.input_lock_owner,
                ttl=self.pending_broadcast_ttl_sec,
            ):
                self.state = CoinJoinState.FAILED
                return False, {"error": "Maker input lock ownership was lost before signing"}

            # Signing may produce a usable signature before returning or
            # raising. Cross this boundary first so no later failure can make
            # the committed inputs available to a conflicting transaction.
            self.signing_boundary_crossed = True
            self.state = CoinJoinState.SIG_SENT
            if active_check is None:
                signatures = await self._sign_transaction(tx_hex)
            else:
                signatures = await self._sign_transaction(tx_hex, active_check=active_check)

            if active_check is not None and not active_check():
                return False, {"error": "Session expired during signing"}

            if not signatures:
                return False, {"error": "Failed to sign transaction"}

            # Compute txid from the unsigned transaction for history tracking
            # The txid is computed from the non-witness data so we can calculate it now
            from jmcore.bitcoin import get_txid

            txid = get_txid(tx_hex)

            destination_vout = find_output_index(tx_hex, self.cj_address, network)
            response = {
                "signatures": signatures,
                "txid": txid,
                "destination_vout": destination_vout,
            }

            logger.bind(sensitive=True).info(
                f"Sent !sig with {len(signatures)} signatures (txid: {txid[:16]}...)"
            )

            return True, response

        except Exception as e:
            logger.error("Failed to handle !tx")
            logger.bind(sensitive=True).error(f"Failed to handle !tx: {e}")
            if self.state != CoinJoinState.SIG_SENT:
                self.state = CoinJoinState.FAILED
            return False, {"error": str(e)}

    async def _resolve_parent_prevouts(
        self, tx_hex: str, active_check: Callable[[], bool] | None
    ) -> tuple[dict[tuple[str, int], tuple[int, bytes]] | None, str | None]:
        """Resolve a verified value and script for every input of the parent.

        A buyout binds the whole transaction, so unknown inputs are looked up
        against our own backend (a buyout always requires a backend that can);
        an input we cannot verify fails the round instead of being assumed.
        """
        prevouts: dict[tuple[str, int], tuple[int, bytes]] = {
            outpoint: (utxo.value, bytes.fromhex(utxo.scriptpubkey))
            for outpoint, utxo in self.our_utxos.items()
        }
        prevouts.update(self.channel_prevouts)
        parsed = parse_transaction(tx_hex)
        unknown = list(
            dict.fromkeys(
                (tx_input.txid, tx_input.vout)
                for tx_input in parsed.inputs
                if (tx_input.txid, tx_input.vout) not in prevouts
            )
        )
        try:
            async with asyncio.timeout(MINER_FEE_PREVOUT_LOOKUP_TIMEOUT_SEC):
                for offset in range(0, len(unknown), MINER_FEE_PREVOUT_LOOKUP_BATCH_SIZE):
                    batch = unknown[offset : offset + MINER_FEE_PREVOUT_LOOKUP_BATCH_SIZE]
                    found = await asyncio.gather(
                        *(self.backend.get_utxo(txid, vout) for txid, vout in batch)
                    )
                    for outpoint, utxo in zip(batch, found, strict=True):
                        if utxo is None or not utxo.scriptpubkey:
                            return None, "Could not verify every prevout of the buyout parent"
                        prevouts[outpoint] = (utxo.value, bytes.fromhex(utxo.scriptpubkey))
        except Exception as exc:  # noqa: BLE001 - a lookup failure fails the round
            logger.bind(sensitive=True).warning(f"Buyout parent prevout lookup failed: {exc}")
            return None, "Could not verify every prevout of the buyout parent"
        if self.is_timed_out() or (active_check is not None and not active_check()):
            return None, "Session expired during buyout parent verification"
        return prevouts, None

    async def _validate_buyout_parent(
        self,
        tx_hex: str,
        current_block_height: int | None,
        active_check: Callable[[], bool] | None,
    ) -> str | None:
        """Bind the complete CoinJoin to the buyout terms before any signing."""
        if self.buyout is None:
            return None
        if current_block_height is None:
            return "Chain tip is unknown; refusing to validate the buyout parent"
        prevouts, error = await self._resolve_parent_prevouts(tx_hex, active_check)
        if prevouts is None:
            return error
        try:
            self.buyout.validate(bytes.fromhex(tx_hex), prevouts, current_block_height)
        except Exception as exc:  # noqa: BLE001 - any rejection fails the round
            logger.bind(sensitive=True).warning(f"Buyout rejected the parent: {exc}")
            return f"Buyout parent validation failed: {exc}"
        self._parent_prevouts = prevouts
        return None

    async def _verify_minimum_miner_fee(
        self, tx_hex: str, active_check: Callable[[], bool] | None
    ) -> str | None:
        """Verify complete input values before the irreversible signing boundary."""
        if self.is_timed_out() or (active_check is not None and not active_check()):
            return "Session expired during miner-fee verification"

        tx = parse_transaction(tx_hex)
        foreign_inputs = list(
            dict.fromkeys(
                (tx_input.txid, tx_input.vout)
                for tx_input in tx.inputs
                if (tx_input.txid, tx_input.vout) not in self.our_utxos
            )
        )

        # The wire transaction has no prevout values, so light clients cannot
        # independently verify a taker-reported fee for foreign inputs.
        async def lookup_foreign_utxo(txid: str, vout: int) -> tuple[UTXO | None, Exception | None]:
            try:
                return await self.backend.get_utxo(txid, vout), None
            except Exception as exc:
                return None, exc

        try:
            async with asyncio.timeout(MINER_FEE_PREVOUT_LOOKUP_TIMEOUT_SEC):
                lookup_results: list[tuple[UTXO | None, Exception | None]] = []
                for offset in range(0, len(foreign_inputs), MINER_FEE_PREVOUT_LOOKUP_BATCH_SIZE):
                    batch = foreign_inputs[offset : offset + MINER_FEE_PREVOUT_LOOKUP_BATCH_SIZE]
                    lookup_results.extend(
                        await asyncio.gather(
                            *(lookup_foreign_utxo(txid, vout) for txid, vout in batch)
                        )
                    )
        except TimeoutError:
            logger.warning(
                "Skipping minimum miner-fee verification because foreign prevout lookup timed out"
            )
            return None
        if self.is_timed_out() or (active_check is not None and not active_check()):
            return "Session expired during miner-fee verification"

        missing_outpoints = [
            outpoint
            for outpoint, (utxo, error) in zip(foreign_inputs, lookup_results, strict=True)
            if utxo is None and error is None
        ]
        if missing_outpoints:
            logger.bind(sensitive=True).warning(
                "Foreign prevout lookup reported spent or absent input(s): {}",
                ", ".join(f"{txid}:{vout}" for txid, vout in missing_outpoints),
            )
            return "Could not look up all foreign prevouts for miner-fee verification"

        lookup_failures = [
            (outpoint, error)
            for outpoint, (_, error) in zip(foreign_inputs, lookup_results, strict=True)
            if error is not None
        ]
        if lookup_failures:
            logger.warning(
                "Skipping minimum miner-fee verification because foreign prevout lookup failed"
            )
            logger.bind(sensitive=True).warning(
                "Foreign prevout lookup failure(s): {}",
                "; ".join(f"{txid}:{vout}: {error}" for (txid, vout), error in lookup_failures),
            )
            return None

        total_input = sum(utxo.value for utxo in self.our_utxos.values()) + sum(
            utxo.value for utxo, _ in lookup_results if utxo is not None
        )
        total_output = sum(output.value for output in tx.outputs)
        fee = total_input - total_output
        vsize = estimate_p2wpkh_vsize(len(tx.inputs), len(tx.outputs))
        if fee < 0:
            return "CoinJoin has a negative miner fee"
        minimum_fee_rate = self.minimum_fee_rate_sat_vb
        if minimum_fee_rate is None:
            return "Minimum CoinJoin miner fee rate was not resolved"
        if not fee_rate_meets_minimum(fee, vsize, minimum_fee_rate):
            actual_rate = fee / vsize
            logger.info(
                "Rejecting CoinJoin before signing: proposed miner fee rate {:.4f} sat/vB, "
                "required minimum {:.4f} sat/vB",
                actual_rate,
                minimum_fee_rate,
            )
            return format_low_fee_error(actual_rate, minimum_fee_rate)
        return None

    async def _verify_channel_inputs(
        self, active_check: Callable[[], bool] | None = None
    ) -> str | None:
        """Confirm each channel funding output on chain before disclosing it.

        Channel funding is not wallet state, so the value, script, confirmation
        depth and block height we advertise must come from our own backend and
        agree with the buyout terms. A height is never invented: without one we
        cannot serve a peer that expects extended (neutrino) metadata.

        Returns:
            An error message, or ``None`` when every channel input is verified.
        """
        if self.buyout is None:
            return None
        prevouts: dict[tuple[str, int], tuple[int, bytes]] = {}
        heights: dict[tuple[str, int], int | None] = {}
        for item in self.buyout.inputs:
            outpoint = (item["txid"], item["vout"])
            if outpoint in prevouts:
                return "Buyout repeats a channel input"
            utxo = await self.backend.get_utxo(*outpoint)
            if active_check is not None and not active_check():
                return "Session expired during channel input verification"
            if utxo is None or not utxo.scriptpubkey:
                return "Channel funding output not found on the blockchain"
            if (
                utxo.value != item["value"]
                or utxo.scriptpubkey.lower() != str(item["scriptpubkey"]).lower()
            ):
                return "Channel funding output does not match the buyout terms"
            if utxo.confirmations < self.min_confirmations:
                return (
                    f"Channel funding output too young: "
                    f"{utxo.confirmations} < {self.min_confirmations}"
                )
            if self.peer_neutrino_compat and utxo.height is None:
                return "Channel funding output has no confirmed block height"
            prevouts[outpoint] = (utxo.value, bytes.fromhex(utxo.scriptpubkey))
            heights[outpoint] = utxo.height
        self.channel_prevouts = prevouts
        self.channel_heights = heights
        return None

    async def _select_our_utxos(
        self,
        exclude_utxos: set[tuple[str, int]] | None = None,
        active_check: Callable[[], bool] | None = None,
    ) -> tuple[dict[tuple[str, int], UTXOInfo], str, str, int]:
        """
        Select our UTXOs for the CoinJoin.

        Uses the configured merge_algorithm to determine UTXO selection:
        - default: Minimum UTXOs needed
        - gradual: +1 additional UTXO
        - greedy: ALL UTXOs from the mixdepth
        - random: +0 to +2 additional UTXOs

        Args:
            exclude_utxos: ``(txid, vout)`` outpoints that must not be selected
                because another concurrent session has already committed them.
                Without this, two overlapping sessions could pick the same UTXO
                and produce conflicting transactions; the one broadcast second is
                rejected (e.g. "insufficient fee, rejecting replacement").

        Returns:
            (utxos_dict, cj_address, change_address, mixdepth)
        """
        reserved_outpoints: set[tuple[str, int]] = set()
        try:
            required_amount = required_maker_input(self.offer, self.amount)
            if self.buyout is not None:
                # Channel value funds most of the round, but the escrow change
                # must keep its reserve, and an ordinary wallet input is still
                # required for the !ioauth ownership proof.
                required_amount = self.buyout.wallet_funding_required(required_amount)

            # Inputs disclosed to another in-flight session are not available
            # liquidity. Apply the same exclusion to both the balance gate and
            # the selector so the chosen mixdepth is actually fillable.
            exclude = set(exclude_utxos or set())
            exclude |= self.wallet.get_locked_input_outpoints()
            exclude |= set(self.channel_prevouts)
            md0_mergeable_outpoints = (
                await self.wallet.get_maker_rotation_lineage_outpoints()
                if self.restrict_md0
                else None
            )

            balances = {}
            for md in range(self.wallet.mixdepth_count):
                # Use balance for offers (excludes fidelity bonds)
                balance = await self.wallet.get_balance_for_offers(
                    md,
                    min_confirmations=self.min_confirmations,
                    restrict_md0=self.restrict_md0,
                    md0_mergeable_outpoints=md0_mergeable_outpoints,
                    exclude=exclude,
                )
                if active_check is not None and not active_check():
                    return {}, "", "", -1
                balances[md] = balance

            eligible_mixdepths = {md: bal for md, bal in balances.items() if bal >= required_amount}
            if self.buyout is not None:
                # The buyout is bound to one mixdepth; spending another wallet
                # mixdepth would link funds the binding never authorized.
                eligible_mixdepths = {
                    md: bal for md, bal in eligible_mixdepths.items() if md == self.buyout_mixdepth
                }

            if not eligible_mixdepths:
                logger.error("No mixdepth with sufficient balance")
                logger.bind(sensitive=True).error(
                    f"No mixdepth with sufficient balance: need {required_amount}"
                )
                return {}, "", "", -1

            selected: list[UTXOInfo] = []
            utxos_dict: dict[tuple[str, int], UTXOInfo] = {}
            max_mixdepth = -1

            # Selection can still lose a race to another process after the
            # balance snapshot; atomic reservation closes that race and lets us
            # try another independent mixdepth instead of double-signing an input.
            for candidate_mixdepth in mixdepth_attempt_order(
                eligible_mixdepths,
                self.wallet.mixdepth_count,
                self.mixdepth_selection_policy,
            ):
                try:
                    candidate = self.wallet.select_utxos_with_merge(
                        candidate_mixdepth,
                        required_amount,
                        self.min_confirmations,
                        merge_algorithm=self.merge_algorithm,
                        restrict_md0=self.restrict_md0,
                        md0_mergeable_outpoints=md0_mergeable_outpoints,
                        exclude=exclude,
                    )
                except ValueError as e:
                    logger.bind(sensitive=True).debug(
                        f"Mixdepth {candidate_mixdepth} became unavailable during selection: {e}"
                    )
                    continue

                candidate_dict = {(utxo.txid, utxo.vout): utxo for utxo in candidate}
                if not candidate_dict:
                    continue

                if active_check is not None and not active_check():
                    return {}, "", "", -1
                remaining_session = self.deadline - time.monotonic()
                if remaining_session <= 0:
                    return {}, "", "", -1
                if not self.wallet.reserve_coinjoin_inputs(
                    set(candidate_dict),
                    ttl=min(float(self.pre_sign_timeout_sec), remaining_session),
                    owner=self.input_lock_owner,
                ):
                    logger.warning(
                        f"Inputs from mixdepth {candidate_mixdepth} were locked by a "
                        "concurrent session; trying another mixdepth"
                    )
                    exclude |= self.wallet.get_locked_input_outpoints()
                    continue

                reserved_outpoints = set(candidate_dict)
                selected = candidate
                utxos_dict = candidate_dict
                max_mixdepth = candidate_mixdepth
                break

            if max_mixdepth < 0:
                logger.error("No mixdepth remained selectable after input reservations")
                logger.bind(sensitive=True).error(
                    f"No mixdepth remained selectable after input reservations: "
                    f"need {required_amount}"
                )
                return {}, "", "", -1

            if self.buyout is not None:
                # Consume the single-use durable reservation now: the wallet
                # inputs are committed and nothing has been disclosed yet. A
                # later failure never gives this reservation back.
                self.buyout.begin_round()

            cj_output_mixdepth = (max_mixdepth + 1) % self.wallet.mixdepth_count
            cj_address = self.wallet.get_new_internal_address(cj_output_mixdepth)
            change_address = (
                self.buyout.change_address
                if self.buyout is not None
                else self.wallet.get_new_internal_address(max_mixdepth)
            )

            logger.info("Selected maker inputs for CoinJoin")
            logger.bind(sensitive=True).info(
                f"Selected {len(selected)} UTXOs from mixdepth {max_mixdepth} "
                f"(merge_algorithm={self.merge_algorithm}), "
                f"total value: {sum(u.value for u in selected)} sats"
            )
            for utxo in selected:
                logger.bind(sensitive=True).debug(
                    f"  UTXO {utxo.txid}:{utxo.vout} value={utxo.value} sats address={utxo.address}"
                )

            return utxos_dict, cj_address, change_address, max_mixdepth

        except Exception as e:
            logger.error("Failed to select UTXOs")
            logger.bind(sensitive=True).error(f"Failed to select UTXOs: {e}")
            if reserved_outpoints:
                self.wallet.release_coinjoin_inputs(reserved_outpoints, owner=self.input_lock_owner)
            return {}, "", "", -1

    async def _assemble_prevouts(self, tx: Any) -> tuple[list[int], list[bytes]]:
        """Resolve every input's value and script for BIP341 signing."""
        values: list[int] = []
        scripts: list[bytes] = []
        for tx_input in tx.inputs:
            txid_hex = tx_input.txid_le[::-1].hex()
            key = (txid_hex, tx_input.vout)
            if key in self.our_utxos:
                utxo = self.our_utxos[key]
                values.append(utxo.value)
                scripts.append(bytes.fromhex(utxo.scriptpubkey))
                continue

            verified = self._parent_prevouts.get(key)
            if verified is not None:
                values.append(verified[0])
                scripts.append(verified[1])
                continue

            utxo = await self.backend.get_utxo(txid_hex, tx_input.vout)
            if utxo is None or not utxo.scriptpubkey:
                raise TransactionSigningError(
                    f"Cannot resolve prevout for {txid_hex}:{tx_input.vout} "
                    "(required for taproot sighash)"
                )
            values.append(utxo.value)
            scripts.append(bytes.fromhex(utxo.scriptpubkey))
        return values, scripts

    async def _sign_channel_inputs(
        self, tx_hex: str, active_check: Callable[[], bool] | None
    ) -> list[str]:
        """Collect channel signatures from the buyout runtime, never the wallet.

        The wire envelope is the ordinary one: a 64-byte BIP340 signature and
        the 32-byte x-only output key of the funding output being spent.
        """
        import base64

        if self.buyout is None:
            return []
        if not self._parent_prevouts:
            raise TransactionSigningError("Buyout parent was not verified before signing")
        if self.is_timed_out() or (active_check is not None and not active_check()):
            raise TransactionSigningError("Session expired before channel signing")
        signed = await self.buyout.sign(bytes.fromhex(tx_hex), self._parent_prevouts)
        if self.is_timed_out() or (active_check is not None and not active_check()):
            raise TransactionSigningError("Session expired during channel signing")

        produced = {(item["txid"], item["vout"]) for item in signed}
        if produced != set(self.channel_prevouts):
            raise TransactionSigningError("Buyout signed an unexpected set of inputs")

        encoded: list[str] = []
        for item in signed:
            outpoint = (item["txid"], item["vout"])
            if outpoint in self.our_utxos:
                raise TransactionSigningError("A channel input is also a wallet UTXO")
            signature = bytes.fromhex(item["signature"])
            script = self.channel_prevouts[outpoint][1]
            if len(signature) != 64 or len(script) != 34 or script[:2] != b"\x51\x20":
                raise TransactionSigningError("Channel input is not a Taproot key-spend signature")
            output_key = script[2:]
            sigmsg = bytes([len(signature)]) + signature + bytes([len(output_key)]) + output_key
            encoded.append(base64.b64encode(sigmsg).decode("ascii"))
        return encoded

    async def _sign_transaction(
        self, tx_hex: str, active_check: Callable[[], bool] | None = None
    ) -> list[str]:
        """Sign our inputs in the transaction.

        Returns list of base64-encoded signatures in JM format.
        Each signature is ``base64(sig_len || sig || pub_len || pub)``. Taproot
        signatures use a 64-byte BIP340 signature and 32-byte x-only output key.
        Channel inputs of a prepared buyout are signed first and by their own
        runtime; if they fail, no wallet input is signed and nothing usable is
        returned.
        """
        import base64

        try:
            channel_signatures = await self._sign_channel_inputs(tx_hex, active_check)
            tx_bytes = bytes.fromhex(tx_hex)
            tx = deserialize_transaction(tx_bytes)

            signatures: list[str] = []

            # Build a map of (txid, vout) -> input index for the transaction
            # Note: txid in tx.inputs is little-endian bytes, need to convert
            input_index_map: dict[tuple[str, int], int] = {}
            for idx, tx_input in enumerate(tx.inputs):
                # Convert little-endian txid bytes to big-endian hex string (RPC format)
                txid_hex = tx_input.txid_le[::-1].hex()
                input_index_map[(txid_hex, tx_input.vout)] = idx

            need_prevouts = any(utxo.is_p2tr for utxo in self.our_utxos.values())
            prevout_values: list[int] = []
            prevout_scripts: list[bytes] = []
            if need_prevouts:
                prevout_values, prevout_scripts = await self._assemble_prevouts(tx)

            for (txid, vout), utxo_info in self.our_utxos.items():
                if active_check is not None and not active_check():
                    logger.warning("Session expired before all maker inputs could be signed")
                    return []
                # Find the input index in the transaction
                utxo_key = (txid, vout)
                if utxo_key not in input_index_map:
                    logger.error("A maker UTXO was not found in transaction inputs")
                    logger.bind(sensitive=True).error(
                        f"Our UTXO {txid}:{vout} not found in transaction inputs"
                    )
                    continue

                input_index = input_index_map[utxo_key]

                # Safety check: Fidelity bond (P2WSH) UTXOs should never be in CoinJoins
                if utxo_info.is_p2wsh:
                    raise TransactionSigningError(
                        f"Cannot sign P2WSH UTXO {txid}:{vout} in CoinJoin - "
                        f"fidelity bond UTXOs cannot be used in CoinJoins"
                    )

                # Delegate key access and signing to the wallet so private keys
                # never leave the wallet (issue #518).
                signed = self.wallet.sign_input(
                    tx,
                    input_index,
                    utxo_info,
                    prevout_values=prevout_values if need_prevouts else None,
                    prevout_scripts=prevout_scripts if need_prevouts else None,
                )
                signature = signed.signature
                pubkey_bytes = signed.pubkey

                logger.bind(sensitive=True).debug(
                    f"Signing UTXO {txid}:{vout} at input_index={input_index}, "
                    f"value={utxo_info.value}, address={utxo_info.address}, "
                    f"pubkey={pubkey_bytes.hex()[:16]}..."
                )

                sigmsg = (
                    bytes([len(signature)]) + signature + bytes([len(pubkey_bytes)]) + pubkey_bytes
                )

                # Base64 encode for transmission
                sig_b64 = base64.b64encode(sigmsg).decode("ascii")
                signatures.append(sig_b64)

                logger.bind(sensitive=True).debug(
                    f"Signed input {input_index} for UTXO {txid}:{vout}"
                )

            # Channel signatures follow the wallet ones, matching the order the
            # inputs were disclosed in !ioauth.
            return signatures + channel_signatures

        except TransactionSigningError as e:
            logger.error("Signing error")
            logger.bind(sensitive=True).error(f"Signing error: {e}")
            return []
        except Exception as e:
            logger.error("Failed to sign transaction")
            logger.bind(sensitive=True).error(f"Failed to sign transaction: {e}")
            return []
