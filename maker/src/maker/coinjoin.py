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
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from jmcore.bitcoin import address_to_scriptpubkey_for_network, estimate_vsize, parse_transaction
from jmcore.channel_input import (
    ChannelInputValidationError,
    validate_settlement_outputs_against_parent,
)
from jmcore.encryption import CryptoSession
from jmcore.fee_policy import (
    estimate_p2wpkh_vsize,
    fee_rate_meets_minimum,
    format_low_fee_error,
)
from jmcore.models import NetworkType, Offer, calculate_cj_fee, offer_output_script_type
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

from maker.channel_input import MakerChannelInputNegotiation, MakerChannelInputStage
from maker.mixdepth_selection import MixdepthSelectionPolicy, mixdepth_attempt_order
from maker.offer_math import required_maker_input
from maker.tx_verification import find_output_index, verify_unsigned_transaction

MINER_FEE_PREVOUT_LOOKUP_TIMEOUT_SEC = 10.0
MINER_FEE_PREVOUT_LOOKUP_BATCH_SIZE = 10


def _classify_scriptpubkey(scriptpubkey: str | bytes) -> str | None:
    """Return an ``estimate_vsize`` script type for a standard scriptPubKey."""
    if isinstance(scriptpubkey, str):
        try:
            script = bytes.fromhex(scriptpubkey)
        except ValueError:
            return None
    elif isinstance(scriptpubkey, bytes):
        script = scriptpubkey
    else:
        return None

    if len(script) == 22 and script.startswith(b"\x00\x14"):
        return "p2wpkh"
    if len(script) == 34 and script.startswith(b"\x00\x20"):
        return "p2wsh"
    if len(script) == 34 and script.startswith(b"\x51\x20"):
        return "p2tr"
    if len(script) == 25 and script.startswith(b"\x76\xa9\x14") and script.endswith(b"\x88\xac"):
        return "p2pkh"
    if len(script) == 23 and script.startswith(b"\xa9\x14") and script.endswith(b"\x87"):
        return "p2sh"
    return None


def _has_scriptpubkey_metadata(scriptpubkey: object) -> bool:
    """Return whether a prevout has usable scriptPubKey metadata."""
    return isinstance(scriptpubkey, (str, bytes)) and bool(scriptpubkey)


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
        # Rigid pit (JMP-0005): the equal-output, change and input script types
        # are all fixed by the offer family (sw0 -> p2wpkh, tr0 -> p2tr). There
        # is no per-transaction or taker-chosen output type. A single-type
        # wallet only derives/spends one family, so the offer family must match
        # the wallet type or the maker cannot serve a uniform pit.
        self.pit_script_type = offer_output_script_type(offer.ordertype)
        wallet_type = getattr(wallet, "address_type", None)
        if wallet_type in ("p2wpkh", "p2tr") and self.pit_script_type != wallet_type:
            raise ValueError(
                f"Offer {offer.ordertype.value!r} implies a {self.pit_script_type!r} pit but "
                f"the wallet is {wallet_type!r}; a rigid JMP-0005 pit requires them to "
                f"match (advertise a {wallet_type!r} offer family)."
            )
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
        self.signing_boundary_crossed = False
        self.comm_channel = ""  # Track communication channel ("direct" or "dir:<node_id>")

        # Feature detection for extended UTXO format (neutrino_compat)
        # Initially, we use extended format if our own backend requires it (neutrino)
        # This will be updated to True if taker sends extended format during !auth
        self.peer_neutrino_compat = backend.requires_neutrino_metadata()

        # E2E encryption session with taker
        self.crypto = CryptoSession()

    def is_timed_out(self) -> bool:
        """Check if the session has exceeded the timeout."""
        return time.monotonic() >= self.deadline

    @property
    def input_lock_owner(self) -> str:
        if not self.commitment:
            # Internal selection callers may exercise this helper before !fill.
            # Production selection occurs after the commitment is bound.
            return f"maker:{self.taker_nick}:uninitialized"
        return f"maker:{self.taker_nick}:{self.commitment.hex()}"

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
        self,
        tx_hex: str,
        active_check: Callable[[], bool] | None = None,
        *,
        channel_input: MakerChannelInputNegotiation | None = None,
        channel_input_binding: str | None = None,
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

            channel_fee_credit_sat = self._authorize_channel_input_fee_credit(
                tx_hex,
                channel_input=channel_input,
                channel_input_binding=channel_input_binding,
            )

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
                channel_fee_credit_sat=channel_fee_credit_sat,
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

    def _authorize_channel_input_fee_credit(
        self,
        tx_hex: str,
        *,
        channel_input: MakerChannelInputNegotiation | None,
        channel_input_binding: str | None,
    ) -> int:
        """Authorize the only ordinary-fee credit backed by a frozen channel transcript."""

        if channel_input is None and channel_input_binding is None:
            return 0
        if channel_input is None or channel_input_binding is None:
            raise ValueError("channel-input fee credit requires negotiation and binding")
        try:
            negotiation = MakerChannelInputNegotiation.from_snapshot(channel_input.snapshot())
        except (ChannelInputValidationError, TypeError, ValueError) as exc:
            raise ValueError("channel-input negotiation is not durably authorized") from exc
        if negotiation.ordinary_session_binding != channel_input_binding:
            raise ValueError("channel-input binding differs from ordinary session")
        if negotiation.coordinator_actor != self.taker_nick:
            raise ValueError("channel-input coordinator differs from ordinary taker")
        if negotiation.stage not in {
            MakerChannelInputStage.APPROVAL_SET_ACKED,
            MakerChannelInputStage.PREPARED,
            MakerChannelInputStage.NONCES_READY,
            MakerChannelInputStage.SIGNING,
            MakerChannelInputStage.FINALIZING,
            MakerChannelInputStage.FINALIZED,
        }:
            raise ValueError("channel-input negotiation has not acknowledged approvals")
        if (
            negotiation.proposal is None
            or negotiation.approval_set is None
            or negotiation.local_ack is None
        ):
            raise ValueError("channel-input negotiation lacks approved transcript evidence")

        transcript = negotiation.proposal.transcript
        if tx_hex != transcript.unsigned_parent_tx:
            raise ValueError("ordinary transaction differs from channel-input transcript")
        # Defense in depth: accept_proposal already bound these terms to the
        # parent bytes, but this is the last gate before an irreversible
        # signature, and the approved settlement is what backs the fee credit.
        try:
            validate_settlement_outputs_against_parent(
                transcript, negotiation.proposal.recipient_terms
            )
        except ChannelInputValidationError as exc:
            raise ValueError(f"channel settlement does not match the parent transaction: {exc}")
        transaction = parse_transaction(tx_hex)
        channel_input_indices = {descriptor.input_index for descriptor in transcript.channels}
        input_indices = {
            (tx_input.txid, tx_input.vout): index
            for index, tx_input in enumerate(transaction.inputs)
        }
        if len(input_indices) != len(transaction.inputs):
            raise ValueError("ordinary transaction has duplicate inputs")
        for outpoint, utxo in self.our_utxos.items():
            input_index = input_indices.get(outpoint)
            if input_index is None:
                raise ValueError("ordinary transaction omits a maker input")
            if input_index in channel_input_indices:
                raise ValueError("maker wallet input occupies a channel-input index")
            prevout = transcript.input_prevouts[input_index]
            try:
                wallet_script = bytes.fromhex(utxo.scriptpubkey)
            except (TypeError, ValueError) as exc:
                raise ValueError("maker wallet input has an invalid prevout script") from exc
            if (
                prevout.outpoint.key() != outpoint
                or prevout.value_sat != utxo.value
                or bytes.fromhex(prevout.script_pubkey) != wallet_script
            ):
                raise ValueError("channel transcript prevout differs from maker wallet input")

        try:
            ordinary_scripts = {
                address_to_scriptpubkey_for_network(self.cj_address, self.wallet.network),
                address_to_scriptpubkey_for_network(self.change_address, self.wallet.network),
            }
        except ValueError as exc:
            raise ValueError("maker ordinary output address is invalid") from exc
        ordinary_output_indices = {
            index
            for index, output in enumerate(transaction.outputs)
            if output.script in ordinary_scripts
        }
        channel_settlement_indices: set[int] = set()
        channel_net_claim_sat = 0
        for terms in negotiation.proposal.recipient_terms:
            channel_net_claim_sat += terms.claim_adjustment_sat - terms.miner_fee_share_sat
            for output in terms.settlement_outputs:
                settlement_script = bytes.fromhex(output.script_pubkey)
                if (
                    output.output_index in ordinary_output_indices
                    or settlement_script in ordinary_scripts
                ):
                    raise ValueError("channel settlement overlaps an ordinary CoinJoin output")
                if output.output_index in channel_settlement_indices:
                    raise ValueError("channel settlement output is counted more than once")
                channel_settlement_indices.add(output.output_index)

        advertised_fee_sat = calculate_cj_fee(self.offer.ordertype, self.offer.cjfee, self.amount)
        if channel_net_claim_sat < advertised_fee_sat:
            raise ValueError("channel settlement does not authorize the advertised CoinJoin fee")
        expected_change_sat = (
            sum(utxo.value for utxo in self.our_utxos.values()) - self.amount - self.offer.txfee
        )
        change_script = address_to_scriptpubkey_for_network(
            self.change_address, self.wallet.network
        )
        change_outputs = [
            output for output in transaction.outputs if output.script == change_script
        ]
        if len(change_outputs) != 1 or change_outputs[0].value != expected_change_sat:
            raise ValueError(
                "channel-input ordinary change must exactly exclude the advertised fee"
            )
        return advertised_fee_sat

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
        foreign_prevouts = {
            outpoint: utxo
            for outpoint, (utxo, _) in zip(foreign_inputs, lookup_results, strict=True)
        }
        input_scripts: list[str | bytes] = []
        missing_local_script = False
        for tx_input in tx.inputs:
            outpoint = (tx_input.txid, tx_input.vout)
            local_utxo = self.our_utxos.get(outpoint)
            if local_utxo is not None:
                scriptpubkey = local_utxo.scriptpubkey
                if not _has_scriptpubkey_metadata(scriptpubkey):
                    missing_local_script = True
                    continue
            else:
                foreign_utxo = foreign_prevouts[outpoint]
                if foreign_utxo is None or not _has_scriptpubkey_metadata(
                    foreign_utxo.scriptpubkey
                ):
                    return (
                        "Could not resolve all foreign prevout scripts for miner-fee verification"
                    )
                scriptpubkey = foreign_utxo.scriptpubkey
            input_scripts.append(scriptpubkey)

        input_types = [_classify_scriptpubkey(script) for script in input_scripts]
        output_types = [_classify_scriptpubkey(output.script) for output in tx.outputs]
        if any(script_type is None for script_type in output_types):
            return "Could not classify all transaction output scripts for miner-fee verification"

        if self.pit_script_type == "p2tr":
            if missing_local_script:
                return "Could not resolve all local prevout scripts for tr0 miner-fee verification"
            if any(script_type != "p2tr" for script_type in input_types):
                return "tr0 CoinJoin contains a non-P2TR input"
            if any(script_type != "p2tr" for script_type in output_types):
                return "tr0 CoinJoin contains a non-P2TR output"
            vsize = estimate_vsize(["p2tr"] * len(tx.inputs), ["p2tr"] * len(tx.outputs))
        elif missing_local_script:
            # Legacy sw0 wallet callers may omit local scriptPubKey metadata.
            vsize = estimate_p2wpkh_vsize(len(tx.inputs), len(tx.outputs))
        else:
            if any(script_type not in {"p2wpkh", "p2wsh", "p2tr"} for script_type in input_types):
                return "Cannot estimate miner fee for an unsupported input script type"
            vsize = estimate_vsize(
                [script_type for script_type in input_types if script_type is not None],
                [script_type for script_type in output_types if script_type is not None],
            )
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

            # Inputs disclosed to another in-flight session are not available
            # liquidity. Apply the same exclusion to both the balance gate and
            # the selector so the chosen mixdepth is actually fillable.
            exclude = set(exclude_utxos or set())
            exclude |= self.wallet.get_locked_input_outpoints()
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

            cj_output_mixdepth = (max_mixdepth + 1) % self.wallet.mixdepth_count
            cj_address = self.wallet.get_new_internal_address(cj_output_mixdepth)
            change_address = self.wallet.get_new_internal_address(max_mixdepth)

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

            foreign_utxo = await self.backend.get_utxo(txid_hex, tx_input.vout)
            if foreign_utxo is None or not foreign_utxo.scriptpubkey:
                raise TransactionSigningError(
                    f"Cannot resolve prevout for {txid_hex}:{tx_input.vout} "
                    "(required for taproot sighash)"
                )
            values.append(foreign_utxo.value)
            scripts.append(bytes.fromhex(foreign_utxo.scriptpubkey))
        return values, scripts

    async def _sign_transaction(
        self, tx_hex: str, active_check: Callable[[], bool] | None = None
    ) -> list[str]:
        """Sign our inputs in the transaction.

        Returns list of base64-encoded signatures in JM format.
        Each signature is ``base64(sig_len || sig || pub_len || pub)``. Taproot
        signatures use a 64-byte BIP340 signature and 32-byte x-only output key.
        """
        import base64

        try:
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

            return signatures

        except TransactionSigningError as e:
            logger.error("Signing error")
            logger.bind(sensitive=True).error(f"Signing error: {e}")
            return []
        except Exception as e:
            logger.error("Failed to sign transaction")
            logger.bind(sensitive=True).error(f"Failed to sign transaction: {e}")
            return []
