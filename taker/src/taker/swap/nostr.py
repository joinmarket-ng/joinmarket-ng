"""
Nostr relay client for swap provider discovery and encrypted RPC.

Implements the Electrum-compatible protocol for discovering swap providers
via NIP-38 replaceable events and communicating via NIP-04 encrypted DMs.

Discovery protocol:
1. Connect to relay(s) via WebSocket
2. Subscribe to kind 30315 events with d-tag "electrum-swapserver-5"
3. Parse provider offers from event content (JSON)
4. Rank providers by PoW bits and fee

RPC protocol (kind 25582):
1. Generate ephemeral keypair for privacy
2. Subscribe to DMs from provider to our ephemeral pubkey
3. Encrypt request as NIP-04 DM (kind 25582)
4. Send signed event to relay
5. Wait for response matching ``reply_to`` field in decrypted content
6. Decrypt and return the JSON response
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlparse

import aiohttp
from jmcore.tor_isolation import (
    IsolationCategory,
    build_isolated_proxy_url,
    normalize_proxy_url,
)
from loguru import logger

from taker.swap.models import (
    DEFAULT_SWAP_RELAYS,
    MIN_POW_BITS,
    NOSTR_D_TAG,
    NOSTR_EVENT_KIND_DM,
    NOSTR_EVENT_KIND_OFFER,
    SwapProvider,
)
from taker.swap.nip04 import (
    create_nip04_dm_event,
    nip04_decrypt,
    privkey_to_xonly_pubkey,
)


def _compute_pow_bits(pubkey_hex: str, nonce_hex: str) -> int:
    """Compute proof-of-work bits for a provider offer.

    PoW is computed as the number of leading zero bits of SHA256(pubkey || nonce).

    Args:
        pubkey_hex: Provider's Nostr pubkey (hex).
        nonce_hex: PoW nonce from the offer (hex).

    Returns:
        Number of leading zero bits.
    """
    nonce_clean = nonce_hex.removeprefix("0x")
    # Pad to even length (bytes.fromhex requires pairs of hex digits)
    if len(nonce_clean) % 2:
        nonce_clean = "0" + nonce_clean
    data = bytes.fromhex(pubkey_hex) + bytes.fromhex(nonce_clean)
    digest = hashlib.sha256(data).digest()

    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
        else:
            # Count leading zeros in this byte
            bits += (byte ^ 0xFF).bit_length() - (8 - byte.bit_length())
            # Actually, just count leading zero bits
            mask = 0x80
            while mask and not (byte & mask):
                bits += 1
                mask >>= 1
            break
    return bits


def _parse_offer_content(content: str, pubkey: str, offer_id: str) -> SwapProvider | None:
    """Parse a swap provider offer from Nostr event content.

    Args:
        content: JSON content of the Nostr event.
        pubkey: Provider's Nostr pubkey (hex).

    Returns:
        SwapProvider if valid, None otherwise.
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        logger.debug(f"Invalid JSON in swap offer from {pubkey[:16]}...")
        return None

    try:
        relays = [r.strip() for r in data.get("relays", "").split(",") if r.strip()]
        pow_nonce = data.get("pow_nonce", "")
        pow_bits = _compute_pow_bits(pubkey, pow_nonce) if pow_nonce else 0

        return SwapProvider(
            offer_id=offer_id,
            pubkey=pubkey,
            percentage_fee=float(data.get("percentage_fee", 0.5)),
            mining_fee=int(data.get("mining_fee", 1500)),
            min_amount=int(data.get("min_amount", 20_000)),
            max_reverse_amount=int(data.get("max_reverse_amount", 5_000_000)),
            relays=relays,
            pow_bits=pow_bits,
        )
    except (ValueError, KeyError, TypeError) as e:
        logger.debug(f"Failed to parse swap offer from {pubkey[:16]}...: {e}")
        return None


def _event_matches_network(tags: list[Any], network: str) -> bool:
    """Return True when the Nostr event is valid for the requested network.

    Network is encoded in ``r`` tags as ``net:<network>`` (e.g. ``net:signet``).

    Compatibility rule:
    - If no network tags are present, treat as mainnet-only legacy behavior.
    - Non-mainnet networks (signet/testnet/regtest) require an explicit match.
    """
    tag_networks: set[str] = set()
    for tag in tags:
        if not isinstance(tag, list) or len(tag) < 2:
            continue
        key = tag[0]
        value = tag[1]
        if key != "r" or not isinstance(value, str):
            continue
        if value.startswith("net:"):
            tag_networks.add(value[4:])

    if not tag_networks:
        return network == "mainnet"
    return network in tag_networks


def _proxy_connector_from_isolated_url(proxy_url: str) -> aiohttp.TCPConnector:
    """Build an aiohttp_socks connector from an isolated proxy URL.

    ``aiohttp_socks`` does not accept the ``socks5h://`` scheme directly, so
    we normalize to ``socks5://`` and pass ``rdns=True`` when needed.
    """
    from aiohttp_socks import ProxyConnector

    normalized = normalize_proxy_url(proxy_url)
    return ProxyConnector.from_url(normalized.url, rdns=normalized.rdns)


def _should_use_proxy_for_url(url: str) -> bool:
    """Return whether a URL should go through Tor SOCKS proxy.

    Local relay/service URLs used in tests (localhost/127.0.0.1/::1) must bypass
    Tor; routing them through SOCKS typically fails and breaks local e2e flows.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return host not in {"localhost", "127.0.0.1", "::1"}


class NostrSwapDiscovery:
    """Discovers swap providers via Nostr relays.

    Connects to configured relays, subscribes to provider offer events,
    and returns a ranked list of available providers.
    """

    def __init__(
        self,
        relays: list[str] | None = None,
        network: str = "mainnet",
        min_pow_bits: int = MIN_POW_BITS,
        socks_host: str | None = None,
        socks_port: int = 9050,
        connection_timeout: float = 30.0,
    ) -> None:
        """Initialize the discovery client.

        Args:
            relays: Nostr relay WebSocket URLs. Defaults to DEFAULT_SWAP_RELAYS.
            network: Bitcoin network name for filtering offers.
            min_pow_bits: Minimum PoW bits to accept an offer.
            socks_host: SOCKS5 proxy host for Tor.
            socks_port: SOCKS5 proxy port.
            connection_timeout: WebSocket connection timeout in seconds.
        """
        self.relays = relays or list(DEFAULT_SWAP_RELAYS)
        self.network = network
        self.min_pow_bits = min_pow_bits
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.connection_timeout = connection_timeout

    async def discover_providers(
        self,
        timeout: float = 15.0,
        max_providers: int = 10,
    ) -> list[SwapProvider]:
        """Query Nostr relays for swap provider offers.

        Connects to each relay, subscribes to offer events, collects
        responses, then ranks by PoW and returns the best providers.

        Args:
            timeout: Maximum time to wait for relay responses.
            max_providers: Maximum number of providers to return.

        Returns:
            List of SwapProvider sorted by PoW bits (descending), then fee (ascending).
        """
        providers: dict[str, SwapProvider] = {}  # pubkey -> provider (dedup)

        now = int(time.time())
        # Primary filter: match by d-tag only. We enforce network tags locally
        # to avoid accidentally selecting a provider for a different network.
        # Untagged legacy offers are treated as mainnet-only.
        subscription_filter = {
            "kinds": [NOSTR_EVENT_KIND_OFFER],
            "limit": max_providers,
            "#d": [NOSTR_D_TAG],
            "since": now - 3600,
            "until": now + 3600,
        }

        for relay_url in self.relays:
            try:
                relay_providers = await self._query_relay(relay_url, subscription_filter, timeout)
                for provider in relay_providers:
                    # Dedup by pubkey, keep highest PoW
                    existing = providers.get(provider.pubkey)
                    if existing is None or provider.pow_bits > existing.pow_bits:
                        providers[provider.pubkey] = provider
            except Exception as e:
                logger.debug(f"Failed to query relay {relay_url}: {e}")
                continue

        # Filter by minimum PoW
        valid = [p for p in providers.values() if p.pow_bits >= self.min_pow_bits]

        # Sort: highest PoW first, then lowest total fee
        valid.sort(key=lambda p: (-p.pow_bits, p.percentage_fee, p.mining_fee))

        return valid[:max_providers]

    async def _query_relay(
        self,
        relay_url: str,
        subscription_filter: dict[str, Any],
        timeout: float,
    ) -> list[SwapProvider]:
        """Query a single Nostr relay for swap offers.

        Args:
            relay_url: WebSocket URL of the relay.
            subscription_filter: Nostr subscription filter.
            timeout: Maximum wait time.

        Returns:
            List of SwapProvider from this relay.
        """
        providers: list[SwapProvider] = []
        sub_id = secrets.token_hex(16)

        proxy = None
        if self.socks_host and _should_use_proxy_for_url(relay_url):
            proxy = build_isolated_proxy_url(
                self.socks_host,
                self.socks_port,
                IsolationCategory.SWAP,
            )

        connector = None
        if proxy:
            try:
                connector = _proxy_connector_from_isolated_url(proxy)
            except ImportError:
                logger.warning("aiohttp_socks not installed, connecting without Tor")

        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.ws_connect(relay_url) as ws:
                    # Send subscription request
                    req = json.dumps(["REQ", sub_id, subscription_filter])
                    await ws.send_str(req)

                    # Collect events until EOSE or timeout
                    end_time = time.monotonic() + timeout
                    while time.monotonic() < end_time:
                        remaining = end_time - time.monotonic()
                        if remaining <= 0:
                            break

                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=min(remaining, 5.0))
                        except TimeoutError:
                            break

                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except json.JSONDecodeError:
                                continue

                            if isinstance(data, list):
                                if data[0] == "EVENT" and len(data) >= 3:
                                    event = data[2]
                                    if not _event_matches_network(
                                        event.get("tags", []), self.network
                                    ):
                                        continue
                                    provider = _parse_offer_content(
                                        event.get("content", ""),
                                        event.get("pubkey", ""),
                                        event.get("id", ""),
                                    )
                                    if provider:
                                        providers.append(provider)

                                elif data[0] == "EOSE":
                                    # End of stored events, we have all offers
                                    break

                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break

                    # Close subscription
                    close_req = json.dumps(["CLOSE", sub_id])
                    await ws.send_str(close_req)

            except Exception as e:
                logger.debug(f"WebSocket error with {relay_url}: {e}")

        return providers


class NostrSwapRPC:
    """Sends encrypted RPC requests to a swap provider via Nostr DMs.

    Uses NIP-04 encryption with a fresh ephemeral keypair per session for
    privacy.  The provider is identified by their Nostr public key (x-only
    hex).

    Flow for each ``call()``:

    1. Connect to the first reachable relay.
    2. Subscribe to kind 25582 events tagged with our ephemeral pubkey
       (``#p`` filter with ``limit: 0`` for new events only).
    3. Wait for the EOSE marker so we know the subscription is live.
    4. Build a kind 25582 event: NIP-04 encrypt the JSON-RPC payload
       using our ephemeral privkey -> provider pubkey ECDH, sign the
       event with a BIP-340 Schnorr signature, then send it.
    5. Read incoming events, decrypt each one, and check whether the
       decrypted JSON contains ``"reply_to": "<our_request_event_id>"``.
    6. Return the matched response (minus the ``reply_to`` bookkeeping
       field) or raise after ``timeout`` seconds.
    """

    def __init__(
        self,
        provider_pubkey: str,
        relays: list[str],
        socks_host: str | None = None,
        socks_port: int = 9050,
        connection_timeout: float = 30.0,
    ) -> None:
        """Initialize the RPC client.

        Args:
            provider_pubkey: Provider's x-only Nostr public key (64-char hex).
            relays: Nostr relay WebSocket URLs where the provider listens.
            socks_host: SOCKS5 proxy host for Tor.
            socks_port: SOCKS5 proxy port.
            connection_timeout: WebSocket connection timeout in seconds.
        """
        self.provider_pubkey = provider_pubkey
        self.relays = relays
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.connection_timeout = connection_timeout

        # Fresh ephemeral keypair for this RPC session (never reused).
        self._privkey = secrets.token_bytes(32)
        self._pubkey = privkey_to_xonly_pubkey(self._privkey)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send an encrypted RPC request and wait for the response.

        Tries each relay in order until one succeeds.  A single relay
        connection is used for both sending the request and receiving
        the response.

        Args:
            method: RPC method name (e.g. ``"createswap"``).
            params: Method parameters (merged into the request JSON
                alongside ``"method"``).
            timeout: Maximum seconds to wait for a response.

        Returns:
            Parsed JSON response from the provider.

        Raises:
            TimeoutError: No response within *timeout*.
            ConnectionError: Unable to connect to any relay.
            ValueError: Provider returned an error payload.
        """
        last_error: Exception | None = None

        for relay_url in self.relays:
            try:
                return await self._call_via_relay(relay_url, method, params, timeout)
            except Exception as e:
                logger.debug(f"Nostr RPC failed via {relay_url}: {e}")
                last_error = e
                continue

        raise ConnectionError(
            f"Unable to reach swap provider via any Nostr relay "
            f"({len(self.relays)} tried). Last error: {last_error}"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_connector(self, relay_url: str) -> aiohttp.TCPConnector | None:
        """Build an aiohttp connector, optionally with Tor SOCKS5 proxy."""
        if not self.socks_host or not _should_use_proxy_for_url(relay_url):
            return None
        try:
            proxy_url = build_isolated_proxy_url(
                self.socks_host,
                self.socks_port,
                IsolationCategory.SWAP,
            )
            return _proxy_connector_from_isolated_url(proxy_url)
        except ImportError:
            logger.warning("aiohttp_socks not installed, connecting without Tor")
            return None

    async def _call_via_relay(
        self,
        relay_url: str,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """Execute a single RPC call through one relay.

        1. Connect WebSocket.
        2. Subscribe for responses (kind 25582, #p = our pubkey, limit 0).
        3. Wait for EOSE.
        4. Send NIP-04 encrypted request.
        5. Wait for matching response (``reply_to``).
        """
        connector = self._build_connector(relay_url)
        sub_id = secrets.token_hex(16)

        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect(relay_url) as ws:
                # --- Step 2: subscribe for responses ---
                sub_filter = {
                    "kinds": [NOSTR_EVENT_KIND_DM],
                    "limit": 0,  # only new events
                    "#p": [self._pubkey],
                }
                await ws.send_str(json.dumps(["REQ", sub_id, sub_filter]))

                # --- Step 3: wait for EOSE ---
                await self._wait_for_eose(ws, sub_id, timeout=min(timeout, 10.0))

                # --- Step 4: build + send request event ---
                request_payload = {**params, "method": method}
                request_json = json.dumps(request_payload, separators=(",", ":"))

                event = create_nip04_dm_event(
                    content=request_json,
                    our_privkey=self._privkey,
                    recipient_pubkey_hex=self.provider_pubkey,
                    kind=NOSTR_EVENT_KIND_DM,
                )
                request_event_id = event["id"]

                await ws.send_str(json.dumps(["EVENT", event]))
                logger.debug(
                    f"Sent Nostr DM RPC: method={method}, event_id={request_event_id[:16]}..."
                )

                # --- Step 5: wait for matching response ---
                try:
                    response_data = await self._wait_for_response(ws, request_event_id, timeout)
                finally:
                    # Clean up subscription
                    try:
                        await ws.send_str(json.dumps(["CLOSE", sub_id]))
                    except Exception:
                        pass

        # Check for error in response
        if "error" in response_data:
            raise ValueError(f"Swap provider error: {response_data['error']}")

        return response_data

    async def _wait_for_eose(
        self, ws: aiohttp.ClientWebSocketResponse, sub_id: str, timeout: float
    ) -> None:
        """Wait for the EOSE (End Of Stored Events) marker.

        This confirms our subscription is active and the relay has
        finished sending any stored events (there should be none since
        we use ``limit: 0``).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=min(remaining, 5.0))
            except TimeoutError:
                # No EOSE yet -- relay may be slow.  Log but continue.
                logger.debug("Timeout waiting for EOSE, proceeding anyway")
                return

            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, list) and data[0] == "EOSE" and data[1] == sub_id:
                    return
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise ConnectionError(f"WebSocket closed while waiting for EOSE: {msg}")

        logger.debug("Timed out waiting for EOSE, proceeding anyway")

    async def _wait_for_response(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        request_event_id: str,
        timeout: float,
    ) -> dict[str, Any]:
        """Wait for a response DM matching our request event ID.

        The provider's response is a kind 25582 event with NIP-04
        encrypted JSON containing ``"reply_to": "<request_event_id>"``.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=min(remaining, 5.0))
            except TimeoutError:
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                response = self._try_parse_response(msg.data, request_event_id)
                if response is not None:
                    return response
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise ConnectionError(f"WebSocket closed while waiting for swap response: {msg}")

        raise TimeoutError(
            f"No response from swap provider within {timeout}s "
            f"(request_event_id={request_event_id[:16]}...)"
        )

    def _try_parse_response(self, raw_msg: str, request_event_id: str) -> dict[str, Any] | None:
        """Try to parse a WebSocket message as a matching response.

        Returns the decrypted response dict if it matches, or None.
        """
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            return None

        if not isinstance(data, list) or data[0] != "EVENT" or len(data) < 3:
            return None

        event = data[2]
        if not isinstance(event, dict):
            return None

        # Must be from the provider
        if event.get("pubkey") != self.provider_pubkey:
            return None

        # Must be kind 25582
        if event.get("kind") != NOSTR_EVENT_KIND_DM:
            return None

        # Decrypt the content
        encrypted_content = event.get("content", "")
        if not encrypted_content:
            return None

        try:
            plaintext = nip04_decrypt(encrypted_content, self._privkey, self.provider_pubkey)
        except Exception as e:
            logger.debug(f"Failed to decrypt DM from provider: {e}")
            return None

        try:
            response_data = json.loads(plaintext)
        except json.JSONDecodeError:
            logger.debug(f"Decrypted DM is not valid JSON: {plaintext[:100]}")
            return None

        # Check reply_to correlation
        reply_to = response_data.pop("reply_to", None)
        if reply_to != request_event_id:
            logger.debug(
                f"DM reply_to mismatch: got {reply_to}, expected {request_event_id[:16]}..."
            )
            return None

        logger.debug(f"Received matching swap response for event {request_event_id[:16]}...")
        return response_data


class HTTPSwapTransport:
    """Direct HTTP transport for swap provider communication.

    This is the simpler alternative to Nostr DMs, used when the
    provider exposes an HTTP API (Boltz-compatible or Electrum-compatible).
    Also used for the mock swap provider in regtest.
    """

    def __init__(
        self,
        base_url: str,
        socks_host: str | None = None,
        socks_port: int = 9050,
        connection_timeout: float = 30.0,
    ) -> None:
        """Initialize HTTP transport.

        Args:
            base_url: Base URL of the swap provider API.
            socks_host: SOCKS5 proxy host for Tor.
            socks_port: SOCKS5 proxy port.
            connection_timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.connection_timeout = connection_timeout

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Send an HTTP request to the swap provider.

        For Electrum-compatible servers, the endpoint is POST /<method>.
        For Boltz v2, it's POST /v2/swap/reverse.

        Args:
            method: RPC method name.
            params: Method parameters (sent as JSON body).
            timeout: Request timeout.

        Returns:
            Parsed JSON response.

        Raises:
            ConnectionError: If HTTP request fails.
            ValueError: If provider returns an error.
        """
        connector = None
        url = f"{self.base_url}/{method}"
        if self.socks_host and _should_use_proxy_for_url(url):
            try:
                proxy_url = build_isolated_proxy_url(
                    self.socks_host,
                    self.socks_port,
                    IsolationCategory.SWAP,
                )
                connector = _proxy_connector_from_isolated_url(proxy_url)
            except ImportError:
                logger.warning("aiohttp_socks not installed, connecting without Tor")

        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.post(
                    url,
                    json=params,
                    timeout=timeout,
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise ValueError(
                            f"Swap provider returned HTTP {response.status}: {error_text}"
                        )
                    return await response.json()
            except aiohttp.ClientError as e:
                raise ConnectionError(f"Failed to connect to swap provider at {url}: {e}") from e

    async def get_pairs(self, timeout: float = 15.0) -> dict[str, Any]:
        """Get provider's supported pairs and terms.

        Fetches the /getpairs endpoint for fee and limit information.

        Args:
            timeout: Request timeout.

        Returns:
            Parsed pairs response with fees and limits.
        """
        connector = None
        url = f"{self.base_url}/getpairs"
        if self.socks_host and _should_use_proxy_for_url(url):
            try:
                proxy_url = build_isolated_proxy_url(
                    self.socks_host,
                    self.socks_port,
                    IsolationCategory.SWAP,
                )
                connector = _proxy_connector_from_isolated_url(proxy_url)
            except ImportError:
                pass

        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.get(
                    url,
                    timeout=timeout,
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise ValueError(
                            f"Swap provider returned HTTP {response.status}: {error_text}"
                        )
                    return await response.json()
            except aiohttp.ClientError as e:
                raise ConnectionError(
                    f"Failed to get pairs from swap provider at {url}: {e}"
                ) from e

    @classmethod
    def provider_from_pairs(cls, pairs_data: dict[str, Any], base_url: str) -> SwapProvider:
        """Create a SwapProvider from /getpairs response.

        Args:
            pairs_data: Response from /getpairs.
            base_url: Provider's HTTP URL.

        Returns:
            SwapProvider with extracted terms.
        """
        # Electrum format: top-level fields
        # Boltz format: nested under pairs.BTC/BTC
        if "pairs" in pairs_data:
            pair_data = pairs_data["pairs"].get("BTC/BTC", {})
            fees = pair_data.get("fees", {})
            limits = pair_data.get("limits", {})
            return SwapProvider(
                offer_id="http-provider",
                pubkey="http-provider",
                percentage_fee=float(fees.get("percentage", 0.5)),
                mining_fee=int(
                    fees.get("minerFees", {}).get("baseAsset", {}).get("mining_fee", 1500)
                ),
                min_amount=int(limits.get("minimal", 20_000)),
                max_reverse_amount=int(limits.get("max_reverse_amount", 5_000_000)),
                http_url=base_url,
            )
        else:
            # Direct Electrum format
            return SwapProvider(
                offer_id="http-provider",
                pubkey="http-provider",
                percentage_fee=float(pairs_data.get("percentage_fee", 0.5)),
                mining_fee=int(pairs_data.get("mining_fee", 1500)),
                min_amount=int(pairs_data.get("min_amount", 20_000)),
                max_reverse_amount=int(pairs_data.get("max_reverse_amount", 5_000_000)),
                http_url=base_url,
            )
