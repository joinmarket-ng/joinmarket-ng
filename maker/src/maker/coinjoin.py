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

from jmcore.bitcoin import encode_varint, is_p2tr_address
from jmcore.encryption import CryptoSession
from jmcore.models import NetworkType, Offer
from jmcore.protocol import (
    UTXOMetadata,
    format_utxo_list,
)
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import (
    TransactionSigningError,
    create_p2wpkh_script_code,
    deserialize_transaction,
    sign_p2tr_input,
    sign_p2wpkh_input,
)
from loguru import logger

from maker.podle import parse_podle_revelation, verify_podle
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
        requested_address_type: str | None = None,
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
        # Address type requested by taker (None = use wallet default)
        self.requested_address_type = requested_address_type

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
        Validate that message comes from the same channel TYPE as the session.

        We only check that "direct" vs "directory" are not mixed. Messages arriving
        from different directory servers (dir:serverA vs dir:serverB) are expected
        because takers broadcast to ALL directory servers.

        Mixing channel types (e.g., !fill via directory, !auth via direct) could indicate:
        - Session confusion attack
        - Accidental misconfiguration
        - Network issues causing routing inconsistency

        Args:
            source: Message source ("direct" or "dir:<node_id>")

        Returns:
            True if channel is valid, False if it violates consistency
        """
        source_type = self._get_channel_type(source)

        if not self.comm_channel:
            # First message - record the channel type
            self.comm_channel = source_type
            logger.debug(f"Session with {self.taker_nick} established on channel: {source_type}")
            return True

        if self.comm_channel != source_type:
            logger.warning(
                f"Channel consistency violation for {self.taker_nick}: "
                f"session started on '{self.comm_channel}', "
                f"received message on '{source_type}'"
            )
            return False

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
        self, commitment: str, revelation: dict[str, Any], kphex: str
    ) -> tuple[bool, dict[str, Any]]:
        """
        Handle !auth message from taker.

        CRITICAL SECURITY: Verifies PoDLE proof and taker's UTXO.

        Args:
            commitment: PoDLE commitment (should match from !fill)
            revelation: PoDLE revelation data
            kphex: Encryption key (hex)

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
            if (
                self.backend.requires_neutrino_metadata()
                and taker_scriptpubkey
                and taker_blockheight is not None
            ):
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

            utxos_dict, cj_addr, change_addr, mixdepth = await self._select_our_utxos()

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

            self.state = CoinJoinState.IOAUTH_SENT
            logger.info(f"Sent !ioauth with {len(utxos_dict)} UTXOs")

            return True, response

        except Exception as e:
            logger.error(f"Failed to handle !auth: {e}")
            self.state = CoinJoinState.FAILED
            return False, {"error": str(e)}

    async def handle_tx(self, tx_hex: str) -> tuple[bool, dict[str, Any]]:
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

            if self.state != CoinJoinState.IOAUTH_SENT:
                return False, {"error": "Session not in correct state for !tx"}

            logger.info(f"Received !tx from {self.taker_nick}, verifying...")

            # Convert network string to NetworkType enum
            network = NetworkType(self.wallet.network)

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
            )

            if not is_valid:
                logger.error(f"Transaction verification FAILED: {error}")
                self.state = CoinJoinState.FAILED
                return False, {"error": f"Transaction verification failed: {error}"}

            logger.info("Transaction verification PASSED ✓")

            signatures = await self._sign_transaction(tx_hex)  # type: ignore[arg-type]

            if not signatures:
                return False, {"error": "Failed to sign transaction"}

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
    ) -> tuple[dict[tuple[str, int], UTXOInfo], str, str, int]:
        """
        Select our UTXOs for the CoinJoin.

        Uses the configured merge_algorithm to determine UTXO selection:
        - default: Minimum UTXOs needed
        - gradual: +1 additional UTXO
        - greedy: ALL UTXOs from the mixdepth
        - random: +0 to +2 additional UTXOs

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
                    md, min_confirmations=self.min_confirmations
                )
                balances[md] = balance

            eligible_mixdepths = {md: bal for md, bal in balances.items() if bal >= required_amount}

            if not eligible_mixdepths:
                logger.error(f"No mixdepth with sufficient balance: need {required_amount}")
                return {}, "", "", -1

            max_mixdepth = max(eligible_mixdepths, key=lambda md: eligible_mixdepths[md])

            # Use merge algorithm for UTXO selection
            # Makers can consolidate UTXOs "for free" since takers pay all fees
            selected = self.wallet.select_utxos_with_merge(
                max_mixdepth,
                required_amount,
                self.min_confirmations,
                merge_algorithm=self.merge_algorithm,
            )

            utxos_dict = {(utxo.txid, utxo.vout): utxo for utxo in selected}

            cj_output_mixdepth = (max_mixdepth + 1) % self.wallet.mixdepth_count
            cj_index = self.wallet.get_next_address_index(cj_output_mixdepth, 1)
            change_index = self.wallet.get_next_address_index(max_mixdepth, 1)

            # Determine which address type to use for our CoinJoin outputs.
            #
            # Default to p2wpkh (NOT the wallet's own type) when the taker
            # doesn't negotiate address_type.  A P2TR maker that blindly
            # emits P2TR outputs for a legacy taker would produce the *only*
            # non-P2WPKH equal-amount output in the transaction — a
            # fingerprint that destroys CoinJoin privacy.
            #
            # When the taker explicitly requests P2TR we honour it, but only
            # if our wallet actually uses P2TR descriptors.  Generating P2TR
            # addresses from a P2WPKH wallet is unsafe: those outputs fall
            # outside the wallet's wpkh() descriptors and become
            # unrecoverable after a restart / rescan.
            addr_type = self.requested_address_type or "p2wpkh"

            if addr_type == "p2tr" and self.wallet.address_type != "p2tr":
                raise ValueError(
                    f"Taker requested address_type='p2tr' but this maker "
                    f"wallet uses '{self.wallet.address_type}'. Rejecting "
                    f"CoinJoin to avoid unrecoverable outputs."
                )

            if addr_type == self.wallet.address_type:
                # Common path: requested type matches wallet type (or both
                # default to p2wpkh).  Use the wallet's own address generation.
                cj_address = self.wallet.get_change_address(cj_output_mixdepth, cj_index)
                change_address = self.wallet.get_change_address(max_mixdepth, change_index)
            else:
                # Cross-type path: P2TR wallet serving a legacy taker that
                # needs P2WPKH outputs.  Derive the same BIP86 keys but
                # encode them as P2WPKH.  The wallet imports wpkh()
                # descriptors over BIP86 paths at startup so these addresses
                # are recoverable on rescan.
                cj_key = self.wallet.master_key.derive(
                    f"{self.wallet.root_path}/{cj_output_mixdepth}'/1/{cj_index}"
                )
                cj_address = cj_key.get_address(self.wallet.network)
                self.wallet.address_cache[cj_address] = (cj_output_mixdepth, 1, cj_index)

                change_key = self.wallet.master_key.derive(
                    f"{self.wallet.root_path}/{max_mixdepth}'/1/{change_index}"
                )
                change_address = change_key.get_address(self.wallet.network)
                self.wallet.address_cache[change_address] = (max_mixdepth, 1, change_index)
                logger.info(
                    f"P2TR wallet generating P2WPKH addresses for legacy taker: "
                    f"cj={cj_address[:16]}..., change={change_address[:16]}..."
                )

            # Reserve addresses immediately after selection to prevent reuse
            # in concurrent CoinJoin sessions. Once shared with a taker, addresses
            # must never be reused even if the CoinJoin fails.
            self.wallet.reserve_addresses({cj_address, change_address})

            logger.info(
                f"Selected {len(selected)} UTXOs from mixdepth {max_mixdepth} "
                f"(merge_algorithm={self.merge_algorithm}), "
                f"total value: {sum(u.value for u in selected)} sats"
            )

            return utxos_dict, cj_address, change_address, max_mixdepth

        except Exception as e:
            logger.error(f"Failed to select UTXOs: {e}")
            return {}, "", "", -1

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
            # For Taproot (BIP341), we need ALL prevouts to compute the sighash
            # unless SIGHASH_ANYONECANPAY is used.
            all_prevouts_values = []
            all_prevouts_scripts = []
            has_p2tr = any(is_p2tr_address(utxo.address) for utxo in self.our_utxos.values())

            if has_p2tr:
                logger.debug("Taproot inputs detected, fetching all prevouts for sighash")
                for inp in tx.inputs:
                    txid = inp.txid_le[::-1].hex()
                    utxo = await self.backend.get_utxo(txid, inp.vout)
                    if not utxo:
                        raise TransactionSigningError(
                            f"Could not fetch prevout for {txid}:{inp.vout}"
                        )
                    all_prevouts_values.append(utxo.value)
                    # UTXOInfo.scriptpubkey is a hex string; sighash needs bytes
                    spk = utxo.scriptpubkey
                    all_prevouts_scripts.append(bytes.fromhex(spk) if isinstance(spk, str) else spk)

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

                key = self.wallet.get_key_for_address(utxo_info.address)
                if not key:
                    raise TransactionSigningError(f"Missing key for address {utxo_info.address}")

                priv_key = key.private_key

                # Check if it's P2TR
                if is_p2tr_address(utxo_info.address):
                    logger.debug(f"Signing P2TR input {input_index}")

                    # Use centralized taproot_tweak_privkey which correctly
                    # handles the BIP341 even-Y negation rule.
                    import coincurve
                    from jmcore.bitcoin import taproot_tweak_privkey

                    tweaked_priv_bytes = taproot_tweak_privkey(key.get_private_key_bytes())
                    tweaked_priv = coincurve.PrivateKey(tweaked_priv_bytes)

                    signature = sign_p2tr_input(
                        tx=tx,
                        input_index=input_index,
                        prevouts_values=all_prevouts_values,
                        prevouts_scripts=all_prevouts_scripts,
                        private_key=tweaked_priv,
                        sighash_type=0x00,  # SIGHASH_DEFAULT
                    )

                    # For Taproot, the "signature" in JM format is just the sig (64 bytes)
                    # or 65 bytes if sighash != default.
                    # JM format: varint(sig_len) + sig
                    # No pubkey needed for Taproot key-path witness
                    sigmsg = encode_varint(len(signature)) + signature
                else:
                    # P2WPKH logic
                    pubkey_bytes = key.get_public_key_bytes(compressed=True)

                    logger.debug(
                        f"Signing UTXO {txid}:{vout} at input_index={input_index}, "
                        f"value={utxo_info.value}, address={utxo_info.address}, "
                        f"pubkey={pubkey_bytes.hex()[:16]}..."
                    )

                    script_code = create_p2wpkh_script_code(pubkey_bytes)
                    signature = sign_p2wpkh_input(
                        tx=tx,
                        input_index=input_index,
                        script_code=script_code,
                        value=utxo_info.value,
                        private_key=priv_key,
                    )

                    # Format as CScript: varint(sig_len) + sig + varint(pub_len) + pub
                    sig_len = len(signature)
                    pub_len = len(pubkey_bytes)
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
