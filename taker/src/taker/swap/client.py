"""
High-level swap client for acquiring a swap input UTXO.

Orchestrates the full reverse submarine swap flow:
1. Discover/select a swap provider
2. Generate preimage and claim keypair
3. Create reverse swap (get LN invoice + lockup details)
4. Verify the provider's redeem script
5. Pay the LN invoice (automatically via LND if configured)
6. Watch the blockchain for the provider's lockup transaction
7. Return a SwapInput ready for CoinJoin inclusion

The swap input's claim witness is added during CoinJoin signing,
not here. This module only acquires the UTXO.

Lockup detection is *trustless*: instead of asking the provider for
swap status (which could lie), we poll our own Bitcoin node's UTXO set
and mempool for a payment to the lockup address.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from typing import TYPE_CHECKING, Any

from loguru import logger

from taker.swap.models import (
    ReverseSwapResponse,
    SwapInput,
    SwapProvider,
    SwapState,
)
from taker.swap.nostr import HTTPSwapTransport, NostrSwapDiscovery, NostrSwapRPC
from taker.swap.script import SwapScript

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend


class SwapClient:
    """Acquires a swap-derived UTXO for CoinJoin input injection.

    Usage:
        client = SwapClient(config, backend=my_backend)
        swap_input = await client.acquire_swap_input(
            desired_amount_sats=50000,
            network="regtest",
            current_block_height=200,
        )
        # swap_input.to_utxo_dict() can be added to taker_utxos
        # swap_input witness is added during CoinJoin signing
    """

    def __init__(
        self,
        # Provider selection
        preferred_provider_pubkey: str | None = None,
        nostr_relays: list[str] | None = None,
        # Network
        network: str = "mainnet",
        # Tor
        socks_host: str | None = None,
        socks_port: int = 9050,
        # Limits
        min_pow_bits: int = 0,
        max_swap_fee_pct: float = 1.0,
        # LND connection for automatic invoice payment (optional)
        lnd_rest_url: str | None = None,
        lnd_cert_path: str | None = None,
        lnd_macaroon_path: str | None = None,
        # Blockchain backend for trustless lockup detection
        backend: BlockchainBackend | None = None,
    ) -> None:
        """Initialize the swap client.

        Args:
            preferred_provider_pubkey: Preferred provider pubkey from Nostr discovery.
            nostr_relays: Nostr relay URLs for provider discovery.
            network: Bitcoin network name.
            socks_host: SOCKS5 proxy host for Tor.
            socks_port: SOCKS5 proxy port.
            min_pow_bits: Minimum PoW bits for Nostr-discovered providers.
            max_swap_fee_pct: Maximum acceptable swap fee percentage.
            lnd_rest_url: LND REST API URL for paying invoices.
            lnd_cert_path: Path to LND TLS certificate.
            lnd_macaroon_path: Path to LND admin macaroon.
            backend: Blockchain backend for watching the lockup address.
                When provided, lockup detection is fully trustless — the client
                watches its own node instead of asking the swap provider.
        """
        self.preferred_provider_pubkey = preferred_provider_pubkey
        self.nostr_relays = nostr_relays
        self.network = network
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.min_pow_bits = min_pow_bits
        self.max_swap_fee_pct = max_swap_fee_pct

        # LND connection (optional)
        self.lnd_rest_url = lnd_rest_url
        self.lnd_cert_path = lnd_cert_path
        self.lnd_macaroon_path = lnd_macaroon_path

        # Blockchain backend for trustless lockup detection
        self.backend = backend

        self.state = SwapState.IDLE
        self._preimage: bytes | None = None
        self._preimage_hash: bytes | None = None
        self._claim_privkey: bytes | None = None
        self._claim_pubkey: bytes | None = None
        self._provider: SwapProvider | None = None
        self._swap_response: ReverseSwapResponse | None = None
        self._swap_script: SwapScript | None = None
        self._main_payment_task: asyncio.Task[None] | None = None

    @property
    def lnd_configured(self) -> bool:
        """Check if LND connection is configured for automatic invoice payment."""
        return bool(self.lnd_rest_url and self.lnd_cert_path and self.lnd_macaroon_path)

    @property
    def provider(self) -> SwapProvider | None:
        """Return the previously discovered/configured swap provider, if any."""
        return self._provider

    async def discover_provider(self, target_amount_sats: int | None = None) -> SwapProvider:
        """Discover and cache a swap provider (Nostr or HTTP).

        Call this early in the CoinJoin flow so the provider's ``min_amount``
        is available for feasibility checks and the confirmation prompt.

        Returns:
            The best available SwapProvider.

        Raises:
            ConnectionError: If no provider is reachable.
        """
        self.state = SwapState.DISCOVERING
        provider = await self._get_provider(target_amount_sats=target_amount_sats)
        self._provider = provider
        self.state = SwapState.IDLE
        return provider

    async def acquire_swap_input(
        self,
        desired_amount_sats: int,
        current_block_height: int,
        wait_for_lockup: bool = True,
        lockup_timeout: float = 120.0,
    ) -> SwapInput:
        """Acquire a swap UTXO for CoinJoin input injection.

        This is the main entry point. It performs:
        1. Provider discovery (if no direct URL configured)
        2. Preimage and keypair generation
        3. Reverse swap creation
        4. Script verification
        5. Pay the LN invoice (if LND is configured)
        6. Wait for lockup transaction

        Args:
            desired_amount_sats: Desired on-chain amount in sats.
            current_block_height: Current blockchain height for timeout validation.
            wait_for_lockup: If True, wait for the lockup tx (requires invoice payment).
            lockup_timeout: Maximum seconds to wait for lockup.

        Returns:
            SwapInput ready for CoinJoin inclusion.

        Raises:
            ValueError: If parameters are invalid or provider verification fails.
            ConnectionError: If unable to reach the swap provider.
            TimeoutError: If lockup transaction not seen within timeout.
        """
        # Step 1: Get a provider (reuse pre-discovered provider if available)
        if self._provider is not None:
            provider = self._provider
            logger.debug(
                f"Using pre-discovered provider: pubkey={provider.pubkey[:16]}..., "
                f"min_amount={provider.min_amount:,}"
            )
        else:
            self.state = SwapState.DISCOVERING
            provider = await self._get_provider(
                max_attempts=12,
                retry_interval=10.0,
                target_amount_sats=desired_amount_sats,
            )
            self._provider = provider

        # Clamp up to provider minimum: the extra amount becomes additional fake fee,
        # which only makes the taker look like they earned more — still valid camouflage.
        if desired_amount_sats < provider.min_amount:
            logger.debug(
                f"Swap amount {desired_amount_sats:,} sats is below provider minimum "
                f"({provider.min_amount:,} sats) — padding up to minimum"
            )
            desired_amount_sats = provider.min_amount

        if desired_amount_sats > provider.max_reverse_amount:
            raise ValueError(
                f"Desired amount {desired_amount_sats} sats exceeds provider's "
                f"maximum ({provider.max_reverse_amount} sats)"
            )

        # Check fee is acceptable
        if provider.percentage_fee > self.max_swap_fee_pct:
            raise ValueError(
                f"Provider fee {provider.percentage_fee}% exceeds maximum "
                f"({self.max_swap_fee_pct}%)"
            )

        # Step 2: Generate cryptographic material
        self._generate_swap_secrets()

        # Step 3: Create reverse swap
        self.state = SwapState.REQUESTING
        invoice_amount = provider.calculate_invoice_amount(desired_amount_sats)
        swap_response = await self._create_reverse_swap(provider, invoice_amount)
        self._swap_response = swap_response

        # Step 4: Verify the redeem script
        self._verify_swap_response(swap_response, current_block_height)

        logger.info(
            f"Reverse swap created: id={swap_response.id}, "
            f"invoice_amount={invoice_amount}, "
            f"onchain_amount={swap_response.onchain_amount}, "
            f"timeout={swap_response.timeout_block_height}"
        )

        # Step 5: Pay invoices (if LND is configured)
        #
        # The provider returns two invoices for a reverse swap:
        #   1. ``minerFeeInvoice`` (prepay) — a normal invoice for mining fees
        #   2. ``invoice`` (main) — a **hold invoice** that settles only after
        #      the provider sees the preimage on-chain (from our CoinJoin claim)
        #
        # These invoices are **bundled** by the swap server: neither is settled
        # until *both* HTLCs have arrived.  We must therefore send both
        # payments concurrently.  The prepay settles immediately once the
        # bundle is complete, but the main hold invoice remains in-flight
        # until the CoinJoin (which spends the lockup UTXO) is broadcast and
        # reveals the preimage.  We fire the main payment as a background task
        # so we can proceed to wait for the lockup without blocking.
        if self.lnd_configured:
            # Start the main hold invoice payment as a background task.
            # It will complete only after the CoinJoin is broadcast.
            self._main_payment_task = asyncio.create_task(
                self._pay_invoice(swap_response.invoice),
            )
            if swap_response.miner_fee_invoice:
                logger.info("Paying prepay miner-fee invoice and main invoice concurrently...")
                # Await prepay — it settles once both HTLCs arrive at the provider.
                prepay_task = asyncio.create_task(
                    self._pay_invoice(swap_response.miner_fee_invoice),
                )
                # Wait for prepay to settle (timeout matches the payment timeout).
                # The main payment task continues running in the background.
                await prepay_task
                logger.info("Prepay miner-fee invoice settled.")
            else:
                logger.info("No prepay invoice; main hold invoice payment started in background.")
        else:
            logger.info(f"Pay this invoice to receive the swap UTXO: {swap_response.invoice}")

        # Step 6: Wait for lockup (provider broadcasts after payment)
        if wait_for_lockup:
            self.state = SwapState.WAITING_LOCKUP
            swap_input = await self._wait_for_lockup(swap_response, lockup_timeout)
        else:
            # Return a SwapInput with the expected details (lockup not yet seen)
            swap_input = SwapInput(
                txid="",  # Will be filled when lockup is detected
                vout=0,
                value=swap_response.onchain_amount,
                witness_script=self._swap_script.witness_script() if self._swap_script else b"",
                preimage=self._preimage or b"",
                claim_privkey=self._claim_privkey or b"",
                lockup_address=swap_response.lockup_address,
                timeout_block_height=swap_response.timeout_block_height,
                swap_id=swap_response.id,
                redeem_script_hex=swap_response.redeem_script,
            )

        self.state = SwapState.READY
        return swap_input

    @property
    def invoice(self) -> str | None:
        """Get the LN invoice to pay for the current swap."""
        return self._swap_response.invoice if self._swap_response else None

    @property
    def swap_id(self) -> str | None:
        """Get the current swap ID."""
        return self._swap_response.id if self._swap_response else None

    def _generate_swap_secrets(self) -> None:
        """Generate preimage and claim keypair for the swap.

        The preimage is 32 random bytes. The claim key is a secp256k1 private key.
        """
        self._preimage = secrets.token_bytes(32)
        self._preimage_hash = hashlib.sha256(self._preimage).digest()

        # Generate a secp256k1 keypair for the claim path
        # We use the coincurve library if available, otherwise fall back
        self._claim_privkey = secrets.token_bytes(32)
        self._claim_pubkey = self._derive_pubkey(self._claim_privkey)

    @staticmethod
    def _derive_pubkey(privkey: bytes) -> bytes:
        """Derive compressed public key from private key.

        Args:
            privkey: 32-byte private key.

        Returns:
            33-byte compressed public key.
        """
        try:
            from coincurve import PrivateKey

            pk = PrivateKey(privkey)
            return pk.public_key.format(compressed=True)
        except ImportError:
            pass

        try:
            # Fallback: use jmcore.crypto if it exposes derive_public_key.
            import jmcore.crypto as jmcrypto

            derive_public_key = getattr(jmcrypto, "derive_public_key", None)
            if callable(derive_public_key):
                return derive_public_key(privkey)
        except ImportError:
            pass

        # Last resort: use python-ecdsa
        try:
            from ecdsa import SECP256k1, SigningKey

            sk = SigningKey.from_string(privkey, curve=SECP256k1)
            vk = sk.get_verifying_key()
            # Compress the public key
            x = vk.pubkey.point.x()
            y = vk.pubkey.point.y()
            prefix = b"\x02" if y % 2 == 0 else b"\x03"
            return prefix + x.to_bytes(32, "big")
        except ImportError:
            raise ImportError("No secp256k1 library available. Install 'coincurve' or 'ecdsa'.")

    async def _get_provider(
        self,
        max_attempts: int = 1,
        retry_interval: float = 10.0,
        target_amount_sats: int | None = None,
    ) -> SwapProvider:
        """Get a swap provider, either from URL or via Nostr discovery.

        For Nostr discovery, the provider may not have published its offer
        yet (e.g. the swap server is still calculating liquidity after a
        channel opens).  Callers that can tolerate a wait should pass
        ``max_attempts > 1`` to retry with back-off.

        Args:
            max_attempts: Maximum discovery attempts for Nostr providers.
                Defaults to 1 (single attempt, fast failure).  The
                ``acquire_swap_input`` path passes a higher value.
            retry_interval: Seconds between Nostr discovery attempts.
            target_amount_sats: Optional expected swap amount for compatibility
                filtering and fee ranking logs.

        Returns:
            Selected SwapProvider.

        Raises:
            ConnectionError: If no provider available after all attempts.
        """
        import asyncio

        # Nostr discovery with retry — the provider's kind 30315 offer may
        # not appear on the relay immediately (e.g. the swap server needs
        # time to detect channel liquidity and enter its publishing loop).
        discovery = NostrSwapDiscovery(
            relays=self.nostr_relays,
            network=self.network,
            min_pow_bits=self.min_pow_bits,
            socks_host=self.socks_host,
            socks_port=self.socks_port,
        )

        for attempt in range(1, max_attempts + 1):
            providers = await discovery.discover_providers()
            if providers:
                compatible = providers
                if target_amount_sats is not None:
                    compatible = [
                        p
                        for p in providers
                        if p.min_amount <= target_amount_sats <= p.max_reverse_amount
                    ]

                amount_for_ranking = (
                    target_amount_sats if target_amount_sats is not None else 100_000
                )
                ranked = sorted(
                    compatible,
                    key=lambda p: (p.calculate_fee(amount_for_ranking), -p.pow_bits),
                )

                top = ranked[:5]
                summary = ", ".join(
                    f"{p.pubkey} fee@{amount_for_ranking:,}={p.calculate_fee(amount_for_ranking):,}"
                    for p in top
                )
                if top:
                    logger.info(f"Top swap offers (best fee first): {summary}")
                elif target_amount_sats is not None:
                    logger.warning(
                        "No swap offers compatible with expected amount "
                        f"{target_amount_sats:,} sats; falling back to all discovered offers"
                    )
                    ranked = sorted(
                        providers,
                        key=lambda p: (p.calculate_fee(amount_for_ranking), -p.pow_bits),
                    )

                if self.preferred_provider_pubkey:
                    preferred = self.preferred_provider_pubkey.lower()
                    exact = [p for p in ranked if p.pubkey.lower() == preferred]
                    if exact:
                        selected = exact[0]
                        logger.info(f"Selected preferred swap provider {selected.pubkey}")
                        if attempt > 1:
                            logger.info(
                                f"Swap provider discovered on attempt {attempt}/{max_attempts}"
                            )
                        return selected

                    prefix_matches = [p for p in ranked if p.pubkey.lower().startswith(preferred)]
                    if len(prefix_matches) == 1:
                        selected = prefix_matches[0]
                        logger.info(
                            "Selected preferred swap provider by pubkey prefix "
                            f"{preferred}: {selected.pubkey}"
                        )
                        if attempt > 1:
                            logger.info(
                                f"Swap provider discovered on attempt {attempt}/{max_attempts}"
                            )
                        return selected

                    if len(prefix_matches) > 1:
                        matches = ", ".join(p.pubkey for p in prefix_matches[:5])
                        logger.warning(
                            f"Preferred swap provider prefix is ambiguous, matches: {matches}"
                        )
                    else:
                        logger.warning(
                            "Preferred swap provider pubkey not found in current Nostr offers: "
                            f"{self.preferred_provider_pubkey}"
                        )

                if attempt > 1:
                    logger.info(f"Swap provider discovered on attempt {attempt}/{max_attempts}")
                return ranked[0]

            if attempt < max_attempts:
                logger.debug(
                    f"No swap providers found (attempt {attempt}/{max_attempts}), "
                    f"retrying in {retry_interval}s..."
                )
                await asyncio.sleep(retry_interval)

        raise ConnectionError(
            f"No swap providers found via Nostr after {max_attempts} attempts "
            f"(~{max_attempts * retry_interval:.0f}s)."
        )

    async def _create_reverse_swap(
        self,
        provider: SwapProvider,
        invoice_amount: int,
    ) -> ReverseSwapResponse:
        """Send a createswap request to the provider.

        Uses HTTP transport when the provider has an ``http_url``, otherwise
        falls back to Nostr DM RPC (kind 25582, NIP-04 encrypted).

        Args:
            provider: Selected swap provider.
            invoice_amount: LN invoice amount in sats.

        Returns:
            ReverseSwapResponse with lockup details.
        """
        assert self._preimage_hash is not None
        assert self._claim_pubkey is not None

        request_data = {
            "method": "createswap",
            "type": "reversesubmarine",
            "pairId": "BTC/BTC",
            "invoiceAmount": invoice_amount,
            "preimageHash": self._preimage_hash.hex(),
            "claimPublicKey": self._claim_pubkey.hex(),
        }

        if provider.http_url:
            transport = HTTPSwapTransport(
                provider.http_url,
                socks_host=self.socks_host,
                socks_port=self.socks_port,
            )
            response_data = await transport.call("createswap", request_data)
        elif provider.relays or self.nostr_relays:
            # Prefer the client's own relay list over the provider's advertised
            # relays.  We already discovered this provider on self.nostr_relays,
            # so we know those relays are reachable from the client.  The
            # provider's advertised relays may use internal/Docker hostnames
            # that are unreachable from the client's network.
            rpc_relays = self.nostr_relays or provider.relays
            rpc = NostrSwapRPC(
                provider_pubkey=provider.pubkey,
                relays=rpc_relays,
                socks_host=self.socks_host,
                socks_port=self.socks_port,
            )
            response_data = await rpc.call("createswap", request_data)
        else:
            raise ConnectionError(
                "Provider has neither an HTTP URL nor Nostr relays configured. "
                "Cannot send createswap request."
            )

        return ReverseSwapResponse(
            id=response_data["id"],
            invoice=response_data["invoice"],
            miner_fee_invoice=response_data.get("minerFeeInvoice"),
            lockup_address=response_data["lockupAddress"],
            redeem_script=response_data["redeemScript"],
            timeout_block_height=response_data["timeoutBlockHeight"],
            onchain_amount=response_data["onchainAmount"],
        )

    def _verify_swap_response(
        self,
        response: ReverseSwapResponse,
        current_block_height: int,
    ) -> None:
        """Verify the provider's swap response is honest.

        Checks:
        1. Redeem script matches expected parameters
        2. Lockup address matches redeem script
        3. Timeout is within acceptable bounds

        Args:
            response: Provider's response.
            current_block_height: Current blockchain height.

        Raises:
            ValueError: If verification fails.
        """
        assert self._preimage_hash is not None
        assert self._claim_pubkey is not None

        # Parse and verify the redeem script
        parsed_script = SwapScript.from_redeem_script(response.redeem_script)
        parsed_script.verify_against_provider(
            expected_preimage_hash=self._preimage_hash,
            expected_claim_pubkey=self._claim_pubkey,
            timeout_blockheight=response.timeout_block_height,
            current_block_height=current_block_height,
        )

        # Verify lockup address matches the script
        expected_address = parsed_script.p2wsh_address(self.network)
        if expected_address != response.lockup_address:
            raise ValueError(
                f"Lockup address mismatch: provider says {response.lockup_address}, "
                f"script derives to {expected_address}"
            )

        # Store the verified script
        self._swap_script = SwapScript(
            preimage_hash=self._preimage_hash,
            claim_pubkey=self._claim_pubkey,
            refund_pubkey=parsed_script.refund_pubkey,
            timeout_blockheight=response.timeout_block_height,
        )

        logger.info(
            f"Swap script verified: lockup_address={response.lockup_address}, "
            f"timeout={response.timeout_block_height} "
            f"(delta={response.timeout_block_height - current_block_height} blocks)"
        )

    async def _pay_invoice(self, payment_request: str) -> None:
        """Pay a BOLT11 invoice via the taker's LND node.

        Args:
            payment_request: BOLT11 invoice string.

        Raises:
            ValueError: If payment fails.
            ImportError: If LND client dependencies are not available.
        """
        from taker.swap.ln_client import LndConnection, LndRestClient

        assert self.lnd_rest_url is not None
        assert self.lnd_cert_path is not None
        assert self.lnd_macaroon_path is not None

        conn = LndConnection(
            rest_url=self.lnd_rest_url,
            cert_path=self.lnd_cert_path,
            macaroon_path=self.lnd_macaroon_path,
        )
        lnd_client = LndRestClient(conn)

        try:
            logger.info("Paying swap invoice via LND...")
            result = await lnd_client.pay_invoice(
                payment_request=payment_request,
                timeout_seconds=60,
                fee_limit_sat=1000,
            )
            logger.info(f"Invoice paid: preimage={result.get('payment_preimage_hex', '?')[:16]}...")
        finally:
            await lnd_client.close()

    async def _wait_for_lockup(
        self,
        response: ReverseSwapResponse,
        timeout: float,
    ) -> SwapInput:
        """Wait for the provider's lockup transaction to appear on-chain.

        Uses the taker's own blockchain backend to trustlessly detect the
        lockup — no provider status endpoint is queried.  This is the
        correct Bitcoin approach: *don't trust, verify*.

        Detection strategy:
        1. Poll ``backend.get_utxos([lockup_address])`` which uses
           ``scantxoutset`` (sees *confirmed* UTXOs).
        2. In parallel, call ``backend.get_address_balance()`` as a cheap
           fast-path.

        The UTXO set scan may not see unconfirmed transactions on all
        backends.  For ``BitcoinCoreBackend``, ``scantxoutset`` only
        returns confirmed UTXOs, but the lockup typically confirms within
        a few minutes in regtest (instant mining) and within ~10 min on
        mainnet.

        Args:
            response: Swap response with lockup details.
            timeout: Maximum seconds to wait.

        Returns:
            SwapInput with the lockup UTXO details.

        Raises:
            TimeoutError: If lockup not seen within timeout.
            RuntimeError: If no blockchain backend is configured.
        """
        import asyncio
        import time

        assert self._preimage is not None
        assert self._claim_privkey is not None
        assert self._swap_script is not None

        if self.backend is None:
            raise RuntimeError(
                "No blockchain backend configured for trustless lockup detection. "
                "Pass a BlockchainBackend instance when constructing SwapClient."
            )

        lockup_address = response.lockup_address
        expected_spk = self._swap_script.p2wsh_scriptpubkey().hex()
        start_time = time.monotonic()
        poll_interval = 2.0  # seconds between polls

        logger.info(
            f"Watching lockup address {lockup_address} for incoming UTXO (timeout={timeout}s)..."
        )

        while time.monotonic() - start_time < timeout:
            try:
                utxos = await self.backend.get_utxos([lockup_address])

                for utxo in utxos:
                    # Verify the scriptPubKey matches what we derived
                    if utxo.scriptpubkey.lower() == expected_spk.lower():
                        logger.info(
                            f"Lockup UTXO detected: {utxo.txid}:{utxo.vout} "
                            f"({utxo.value:,} sats, "
                            f"confirmations={utxo.confirmations})"
                        )
                        return SwapInput(
                            txid=utxo.txid,
                            vout=utxo.vout,
                            value=utxo.value,
                            witness_script=self._swap_script.witness_script(),
                            preimage=self._preimage,
                            claim_privkey=self._claim_privkey,
                            lockup_address=lockup_address,
                            timeout_block_height=response.timeout_block_height,
                            swap_id=response.id,
                            redeem_script_hex=response.redeem_script,
                        )

            except Exception as e:
                logger.debug(f"Lockup poll error (will retry): {e}")

            await asyncio.sleep(poll_interval)

        raise TimeoutError(
            f"Lockup transaction not seen after {timeout}s for address {lockup_address}. "
            f"Ensure the LN invoice has been paid and the provider has broadcast the lockup."
        )

    def _find_swap_output(
        self,
        tx_hex: str,
        lockup_address: str,
    ) -> tuple[int, int]:
        """Find the swap output in a transaction.

        Args:
            tx_hex: Raw transaction hex.
            lockup_address: Expected lockup address.

        Returns:
            (vout, value) tuple.

        Raises:
            ValueError: If no matching output found.
        """
        from jmcore.bitcoin import parse_transaction

        tx = parse_transaction(tx_hex)
        expected_scriptpubkey = self._swap_script.p2wsh_scriptpubkey() if self._swap_script else b""

        for i, output in enumerate(tx.outputs):
            if output.script == expected_scriptpubkey:
                return i, output.value

        raise ValueError(f"No output matching lockup address {lockup_address} found in transaction")

    def get_claim_witness_data(self, swap_input: SwapInput) -> dict[str, Any]:
        """Get the data needed to construct the claim witness during CoinJoin signing.

        The claim witness for a P2WSH spend is:
            <signature> <preimage> <witness_script>

        The signature must be created over the specific CoinJoin transaction
        being signed, so we return the components needed for signing.

        Args:
            swap_input: The SwapInput to claim.

        Returns:
            Dict with witness construction data:
            - witness_script: bytes
            - preimage: bytes
            - claim_privkey: bytes
            - scriptpubkey: bytes (for BIP-143 signing)
        """
        return {
            "witness_script": swap_input.witness_script,
            "preimage": swap_input.preimage,
            "claim_privkey": swap_input.claim_privkey,
            "scriptpubkey": swap_input.scriptpubkey,
        }
