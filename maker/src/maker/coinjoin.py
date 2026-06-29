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

import time
from enum import StrEnum
from typing import Any

from jmcore.encryption import CryptoSession
from jmcore.models import NetworkType, Offer
from jmcore.podle import parse_podle_revelation, verify_podle
from jmcore.protocol import (
    UTXOMetadata,
    format_utxo_list,
)
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import (
    TransactionSigningError,
    deserialize_transaction,
)
from loguru import logger

from maker.tx_verification import verify_unsigned_transaction


class CoinJoinState(StrEnum):
    """CoinJoin session states"""

    IDLE = "idle"
    FILL_RECEIVED = "fill_received"
    PUBKEY_SENT = "pubkey_sent"
    AUTH_RECEIVED = "auth_received"
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
        taker_utxo_retries: int = 10,
        taker_utxo_age: int = 5,
        taker_utxo_amtpercent: int = 20,
        session_timeout_sec: int = 300,
        merge_algorithm: str = "default",
        restrict_md0: bool = True,
        channel_change: bool = False,
        ldk: object | None = None,
        channel_peer: str | None = None,
        combine_capable: bool = False,
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
        # Experimental: emit change as a Taproot channel-funding output. When an
        # LDK Server backend and an LN peer are configured, channelize the change
        # into a real externally-funded Lightning channel.
        self.channel_change = channel_change
        self.ldk = ldk
        self.channel_peer = channel_peer
        self._channel_user_id: str | None = None
        self._channel_funding_spk: bytes | None = None
        # Maker-to-maker channel-as-change (combine): the maker contributes a fresh
        # funding pubkey in !ioauth; the taker pairs two makers into one shared 2-of-2
        # output pooling their change. Verified on !tx against the relayed partner key.
        self.combine_capable = combine_capable
        self._cc_priv: bytes | None = None
        self._cc_pubkey: bytes | None = None
        self._combine_shared_spk: bytes | None = None
        self._combine_pooled_value: int = 0

        self.state = CoinJoinState.IDLE
        self.amount = 0
        self.our_utxos: dict[tuple[str, int], UTXOInfo] = {}
        self.cj_address = ""
        self.change_address = ""
        self.mixdepth = 0
        self.commitment = b""
        self.taker_nacl_pk = ""  # Taker's NaCl pubkey (hex) for btc_sig
        self.created_at = time.time()
        self.session_timeout_sec = session_timeout_sec
        self.comm_channel = ""  # Track communication channel ("direct" or "dir:<node_id>")

        # Feature detection for extended UTXO format (neutrino_compat)
        # Initially, we use extended format if our own backend requires it (neutrino)
        # This will be updated to True if taker sends extended format during !auth
        self.peer_neutrino_compat = backend.requires_neutrino_metadata()

        # E2E encryption session with taker
        self.crypto = CryptoSession()

    def is_timed_out(self) -> bool:
        """Check if the session has exceeded the timeout."""
        return time.time() - self.created_at > self.session_timeout_sec

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
            logger.debug(
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

            logger.info(
                f"Received !fill from {self.taker_nick}: "
                f"amount={amount}, commitment={commitment[:16]}..., taker_pk={taker_pk[:16]}..."
            )

            # Set up E2E encryption with taker's NaCl pubkey
            try:
                self.crypto.setup_encryption(taker_pk)
                logger.debug(f"Set up encryption box with taker {self.taker_nick}")
            except Exception as e:
                logger.error(f"Failed to set up encryption with taker: {e}")
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
            logger.error(f"Failed to handle !fill: {e}")
            self.state = CoinJoinState.FAILED
            return False, {"error": str(e)}

    async def handle_auth(
        self,
        commitment: str,
        revelation: dict[str, Any],
        kphex: str,
        exclude_utxos: set[tuple[str, int]] | None = None,
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

            if self.state != CoinJoinState.PUBKEY_SENT:
                return False, {"error": "Session not in correct state for !auth"}

            commitment_bytes = bytes.fromhex(commitment)
            if commitment_bytes != self.commitment:
                logger.debug(
                    f"Commitment mismatch: received={commitment[:16]}..., "
                    f"expected={self.commitment.hex()[:16]}..."
                )
                return False, {"error": "Commitment mismatch"}

            parsed_rev = parse_podle_revelation(revelation)
            if not parsed_rev:
                logger.debug(f"Failed to parse PoDLE revelation: {revelation}")
                return False, {"error": "Invalid PoDLE revelation format"}

            # Log PoDLE verification inputs at TRACE level
            logger.trace(
                f"PoDLE verification inputs: P={parsed_rev['P'].hex()[:32]}..., "
                f"P2={parsed_rev['P2'].hex()[:32]}..., sig={parsed_rev['sig'].hex()[:32]}..., "
                f"e={parsed_rev['e'].hex()[:16]}..., commitment={commitment[:16]}..."
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
                logger.warning(
                    f"PoDLE verification failed for {self.taker_nick}: {error} "
                    f"(commitment={commitment[:16]}..., utxo={utxo_str})"
                )
                return False, {"error": f"PoDLE verification failed: {error}"}

            logger.info("PoDLE proof verified ✓")
            logger.debug(
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
                    logger.warning(
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
                if not result.valid:
                    return False, {"error": f"Taker's UTXO verification failed: {result.error}"}

                taker_utxo_value = result.value
                taker_utxo_confirmations = result.confirmations
                logger.debug(f"Neutrino-verified taker's UTXO: {utxo_txid}:{utxo_vout}")
            else:
                # Full node: direct UTXO lookup
                taker_utxo = await self.backend.get_utxo(utxo_txid, utxo_vout)

                if not taker_utxo:
                    return False, {"error": "Taker's UTXO not found on blockchain"}

                taker_utxo_value = taker_utxo.value
                taker_utxo_confirmations = taker_utxo.confirmations

            if taker_utxo_confirmations < self.taker_utxo_age:
                logger.debug(
                    f"Taker UTXO too young: {utxo_txid}:{utxo_vout} has "
                    f"{taker_utxo_confirmations} confirmations, need {self.taker_utxo_age}"
                )
                return False, {
                    "error": f"Taker's UTXO too young: "
                    f"{taker_utxo_confirmations} < {self.taker_utxo_age}"
                }

            required_amount = int(self.amount * self.taker_utxo_amtpercent / 100)
            if taker_utxo_value < required_amount:
                logger.debug(
                    f"Taker UTXO too small: {utxo_txid}:{utxo_vout} has "
                    f"{taker_utxo_value} sats, need {required_amount} sats "
                    f"({self.taker_utxo_amtpercent}% of {self.amount})"
                )
                return False, {
                    "error": f"Taker's UTXO too small: {taker_utxo_value} < {required_amount}"
                }

            logger.info("Taker's UTXO validated ✓")
            logger.debug(
                f"Taker UTXO details: {utxo_txid}:{utxo_vout}, "
                f"value={taker_utxo_value} sats, confirmations={taker_utxo_confirmations}"
            )

            utxos_dict, cj_addr, change_addr, mixdepth = await self._select_our_utxos(
                exclude_utxos=exclude_utxos
            )

            if not utxos_dict:
                return False, {"error": "Failed to select UTXOs"}

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

            # Maker-to-maker channel-as-change: advertise a fresh funding pubkey so
            # the taker can pair us with a fellow maker into a shared 2-of-2 output.
            if self.combine_capable:
                from maker.channel_change import derive_combine_funding_key

                index = self.wallet.get_next_address_index(self.mixdepth, 1)
                priv, pub, _path = derive_combine_funding_key(self.wallet, index)
                self._cc_priv, self._cc_pubkey = priv, pub
                response["cc_pubkey"] = pub.hex()
                logger.info(
                    "offering maker-to-maker channel-as-change (cc_pubkey {})", pub.hex()[:16]
                )

            self.state = CoinJoinState.IOAUTH_SENT
            logger.info(f"Sent !ioauth with {len(utxos_dict)} UTXOs")

            return True, response

        except Exception as e:
            logger.error(f"Failed to handle !auth: {e}")
            self.state = CoinJoinState.FAILED
            return False, {"error": str(e)}

    def _verify_combine(self, combine: dict[str, Any] | None) -> tuple[str | None, int | None]:
        """Recompute and authorize a maker-to-maker channel-as-change shared output.

        Given the taker-relayed ``partner_pubkey``/``partner_change``, the maker
        independently recomputes the shared 2-of-2 address from its own funding key
        (so the taker cannot pin it to an output it did not agree to) and the pooled
        value. Returns ``(shared_address, pooled_value)`` for the verifier, or
        ``(None, None)`` when no combine applies. Raises on any mismatch.
        """
        if not combine:
            return None, None
        if self._cc_pubkey is None:
            raise ValueError("received combine info but this maker offered no funding key")

        from jmcore.bitcoin import address_to_scriptpubkey
        from jmcore.models import calculate_cj_fee
        from jmswap.channel import combined_funding_address

        partner_pub = bytes.fromhex(combine["partner_pubkey"])
        recomputed = combined_funding_address([self._cc_pubkey, partner_pub], self.wallet.network)
        if recomputed != combine["shared_address"]:
            raise ValueError(
                f"shared channel address mismatch: recomputed {recomputed} "
                f"!= taker {combine['shared_address']}"
            )
        my_total_in = sum(u.value for u in self.our_utxos.values())
        real_cjfee = calculate_cj_fee(self.offer.ordertype, self.offer.cjfee, self.amount)
        own_change = my_total_in - self.amount - self.offer.txfee + real_cjfee
        pooled = own_change + int(combine["partner_change"])
        self._combine_shared_spk = address_to_scriptpubkey(recomputed)
        self._combine_pooled_value = pooled
        logger.info(
            "maker-to-maker channel-as-change: shared output {} pooling {} sats",
            recomputed,
            pooled,
        )
        return recomputed, pooled

    async def handle_tx(
        self, tx_hex: str, combine: dict[str, Any] | None = None
    ) -> tuple[bool, dict[str, Any]]:
        """
        Handle !tx message from taker.

        CRITICAL SECURITY: Verifies unsigned transaction before signing!

        Args:
            tx_hex: Unsigned transaction hex
            combine: Optional maker-to-maker channel-as-change info from the taker
                (``partner_pubkey``, ``partner_change``, ``shared_address``). When
                present, the maker verifies its change went into the shared 2-of-2
                output instead of a plain change output.

        Returns:
            (success, response_data with signatures or error)
        """
        try:
            if self.is_timed_out():
                self.state = CoinJoinState.FAILED
                return False, {"error": f"Session timed out after {self.session_timeout_sec}s"}

            if self.state != CoinJoinState.IOAUTH_SENT:
                return False, {"error": "Session not in correct state for !tx"}

            logger.info(f"Received !tx from {self.taker_nick}, verifying...")
            logger.debug(f"Transaction hex to verify and sign: {tx_hex}")

            # Convert network string to NetworkType enum
            network = NetworkType(self.wallet.network)

            combine_address, combine_value = self._verify_combine(combine)

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
                combine_address=combine_address,
                combine_value=combine_value,
            )

            if not is_valid:
                logger.error(f"Transaction verification FAILED: {error}")
                self.state = CoinJoinState.FAILED
                return False, {"error": f"Transaction verification failed: {error}"}

            logger.info("Transaction verification PASSED ✓")

            signatures = await self._sign_transaction(tx_hex)  # type: ignore[arg-type]

            if not signatures:
                return False, {"error": "Failed to sign transaction"}

            # Channel-as-change: now that we have co-signed the final transaction,
            # supply the channel funding outpoint so the channel can complete once
            # the taker broadcasts the round.
            await self._complete_channel_change(tx_hex)

            # Compute txid from the unsigned transaction for history tracking
            # The txid is computed from the non-witness data so we can calculate it now
            from jmcore.bitcoin import get_txid

            txid = get_txid(tx_hex)

            response = {"signatures": signatures, "txid": txid}

            self.state = CoinJoinState.SIG_SENT
            logger.info(f"Sent !sig with {len(signatures)} signatures (txid: {txid[:16]}...)")

            return True, response

        except Exception as e:
            logger.error(f"Failed to handle !tx: {e}")
            self.state = CoinJoinState.FAILED
            return False, {"error": str(e)}

    async def _select_our_utxos(
        self,
        exclude_utxos: set[tuple[str, int]] | None = None,
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
        try:
            from jmcore.models import OfferType

            real_cjfee = 0
            if self.offer.ordertype in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE):
                real_cjfee = int(self.offer.cjfee)
            else:
                from jmcore.bitcoin import calculate_relative_fee

                real_cjfee = calculate_relative_fee(self.amount, str(self.offer.cjfee))

            total_amount = self.amount + self.offer.txfee
            required_amount = total_amount + 10000 - real_cjfee

            balances = {}
            for md in range(self.wallet.mixdepth_count):
                # Use balance for offers (excludes fidelity bonds)
                balance = await self.wallet.get_balance_for_offers(
                    md,
                    min_confirmations=self.min_confirmations,
                    restrict_md0=self.restrict_md0,
                )
                balances[md] = balance

            eligible_mixdepths = {md: bal for md, bal in balances.items() if bal >= required_amount}

            if not eligible_mixdepths:
                logger.error(f"No mixdepth with sufficient balance: need {required_amount}")
                return {}, "", "", -1

            max_mixdepth = max(eligible_mixdepths, key=lambda md: eligible_mixdepths[md])

            # Exclude inputs already committed to another in-flight CoinJoin
            # (this process or another, maker or taker): re-read the persisted
            # locks right before choosing so we never sign the same UTXO into
            # two concurrent transactions (which would double-spend each other).
            exclude = set(exclude_utxos or set())
            exclude |= self.wallet.get_locked_input_outpoints()

            # Use merge algorithm for UTXO selection
            # Makers can consolidate UTXOs "for free" since takers pay all fees
            selected = self.wallet.select_utxos_with_merge(
                max_mixdepth,
                required_amount,
                self.min_confirmations,
                merge_algorithm=self.merge_algorithm,
                restrict_md0=self.restrict_md0,
                exclude=exclude,
            )

            utxos_dict = {(utxo.txid, utxo.vout): utxo for utxo in selected}

            # "Block first, then continue": persist a lock on the chosen inputs
            # before revealing them to the taker. Acquisition is atomic across
            # processes; on conflict (a racing round grabbed one first) decline
            # this session rather than risk a conflicting transaction. The lock
            # auto-expires, so a crashed round never blocks these funds forever.
            if not self.wallet.reserve_coinjoin_inputs(
                set(utxos_dict.keys()), ttl=self.session_timeout_sec
            ):
                logger.warning(
                    "Selected UTXOs were locked by a concurrent session; "
                    "declining this CoinJoin to avoid a conflicting transaction"
                )
                return {}, "", "", -1

            cj_output_mixdepth = (max_mixdepth + 1) % self.wallet.mixdepth_count
            cj_index = self.wallet.get_next_address_index(cj_output_mixdepth, 1)
            cj_address = self.wallet.get_change_address(cj_output_mixdepth, cj_index)

            # The maker's change is deterministic and must match the taker's
            # computation exactly (taker/tx_builder.py): inputs - cj_amount -
            # txfee + cjfee. This is the value channelled when channel-as-change
            # with a real Lightning channel is enabled.
            change_amount = (
                sum(u.value for u in selected) - self.amount - self.offer.txfee + real_cjfee
            )
            change_address = await self._build_change_address(max_mixdepth, change_amount)

            # Reserve addresses immediately after selection to prevent reuse
            # in concurrent CoinJoin sessions. Once shared with a taker, addresses
            # must never be reused even if the CoinJoin fails.
            self.wallet.reserve_addresses({cj_address, change_address})

            logger.info(
                f"Selected {len(selected)} UTXOs from mixdepth {max_mixdepth} "
                f"(merge_algorithm={self.merge_algorithm}), "
                f"total value: {sum(u.value for u in selected)} sats"
            )
            for utxo in selected:
                logger.debug(
                    f"  UTXO {utxo.txid}:{utxo.vout} value={utxo.value} sats address={utxo.address}"
                )

            return utxos_dict, cj_address, change_address, max_mixdepth

        except Exception as e:
            logger.error(f"Failed to select UTXOs: {e}")
            return {}, "", "", -1

    async def _build_change_address(self, max_mixdepth: int, change_amount: int) -> str:
        """Return the change address, optionally a Taproot channel-funding output.

        With ``channel_change`` enabled the change is emitted as a Taproot
        channel-funding output (no chaining successor). When a Lightning backend
        (``self.ldk``) and a channel peer are configured, it opens a *real*
        externally-funded Lightning channel for the exact change amount and uses
        the channel's funding address (taker-driven: the maker channelizes only
        its own change). Otherwise it falls back to a recoverable self-2-of-2
        funding output, then a plain change address.
        """
        if self.channel_change and change_amount > 0:
            address = await self._open_channel_change(change_amount)
            if address is not None:
                return address
            # fall back to a self-2-of-2 funding output (tracked + sweepable)
            data_dir = getattr(self.wallet, "data_dir", None)
            if data_dir:
                try:
                    from pathlib import Path

                    from maker.channel_change import (
                        ChannelChangeStore,
                        emit_channel_change_address,
                    )

                    store = ChannelChangeStore(Path(data_dir) / "channel_changes")
                    address = emit_channel_change_address(self.wallet, store)
                    logger.warning(
                        "emitting CoinJoin change as a self-funded Taproot output {} "
                        "(no Lightning peer configured)",
                        address,
                    )
                    return address
                except Exception as exc:  # noqa: BLE001 - never block a CoinJoin
                    logger.error("channel-change emission failed, using plain change: {}", exc)
        change_index = self.wallet.get_next_address_index(max_mixdepth, 1)
        return self.wallet.get_change_address(max_mixdepth, change_index)

    async def _open_channel_change(self, change_amount: int) -> str | None:
        """Open a real externally-funded Lightning channel for the change amount.

        Returns the channel funding address (to use as the CoinJoin change), or
        ``None`` if no Lightning backend/peer is configured or the open fails.
        The funding outpoint is supplied later from :meth:`handle_tx`.
        """
        ldk = getattr(self, "ldk", None)
        peer = getattr(self, "channel_peer", None)
        if ldk is None or not peer or "@" not in peer:
            return None
        try:
            from jmcore.bitcoin import address_to_scriptpubkey
            from jmswap.channel_funding import request_external_channel_funding

            peer_node_id, peer_address = peer.split("@", 1)
            user_channel_id, funding_address = await request_external_channel_funding(
                ldk,
                peer_node_id,
                peer_address,
                change_amount,
                self.wallet.network,
                timeout=float(getattr(self, "channel_open_timeout", 45.0)),
            )
            self._channel_user_id = user_channel_id
            self._channel_funding_spk = address_to_scriptpubkey(funding_address)
            logger.info(
                "channel-as-change: opened channel {} funding {} sats at {}",
                user_channel_id,
                change_amount,
                funding_address,
            )
            return funding_address
        except Exception as exc:  # noqa: BLE001 - never block a CoinJoin
            logger.error("channel-as-change open failed, falling back: {}", exc)
            return None

    async def _complete_channel_change(self, tx_hex: str) -> None:
        """Supply the CoinJoin funding outpoint to LDK after co-signing the tx."""
        user_id = getattr(self, "_channel_user_id", None)
        spk = getattr(self, "_channel_funding_spk", None)
        ldk = getattr(self, "ldk", None)
        if not user_id or spk is None or ldk is None:
            return
        try:
            from jmcore.bitcoin import get_txid, parse_transaction
            from jmswap.channel_funding import complete_external_channel_funding

            tx = parse_transaction(tx_hex)
            vout = next(i for i, o in enumerate(tx.outputs) if o.script == spk)
            txid = get_txid(tx_hex)
            await complete_external_channel_funding(ldk, user_id, txid, vout)
            logger.info("channel-as-change: provided funding outpoint {}:{}", txid, vout)
        except Exception as exc:  # noqa: BLE001 - the maker still co-signs the round
            logger.error("channel-as-change outpoint provision failed: {}", exc)

    async def _sign_transaction(self, tx_hex: str) -> list[str]:
        """Sign our inputs in the transaction.

        Returns list of base64-encoded signatures in JM format.
        Each signature is: base64(varint(sig_len) + sig + varint(pub_len) + pub)
        This matches the CScript serialization format.
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

            for (txid, vout), utxo_info in self.our_utxos.items():
                # Find the input index in the transaction
                utxo_key = (txid, vout)
                if utxo_key not in input_index_map:
                    logger.error(f"Our UTXO {txid}:{vout} not found in transaction inputs")
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
                signed = self.wallet.sign_input(tx, input_index, utxo_info)
                signature = signed.signature
                pubkey_bytes = signed.pubkey

                logger.debug(
                    f"Signing UTXO {txid}:{vout} at input_index={input_index}, "
                    f"value={utxo_info.value}, address={utxo_info.address}, "
                    f"pubkey={pubkey_bytes.hex()[:16]}..."
                )

                # Format as CScript: varint(sig_len) + sig + varint(pub_len) + pub
                # For lengths < 0x4c (76), varint is just the length byte
                sig_len = len(signature)
                pub_len = len(pubkey_bytes)

                # Build the sigmsg in JM format
                sigmsg = bytes([sig_len]) + signature + bytes([pub_len]) + pubkey_bytes

                # Base64 encode for transmission
                sig_b64 = base64.b64encode(sigmsg).decode("ascii")
                signatures.append(sig_b64)

                logger.debug(f"Signed input {input_index} for UTXO {txid}:{vout}")

            return signatures

        except TransactionSigningError as e:
            logger.error(f"Signing error: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to sign transaction: {e}")
            return []
