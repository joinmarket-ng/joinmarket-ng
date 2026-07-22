"""External HTTP fee-rate estimation for backends without a full node.

The neutrino backend cannot estimate fees from its own view of the network
(no mempool, no ``estimatesmartfee``). Like other light clients (LND's
``fee.url``, Wasabi's mempool.space provider, Electrum servers), we fetch
estimates from a configurable HTTP endpoint, routed through Tor.

Supported response formats (auto-detected):

- mempool.space recommended fees:
  ``{"fastestFee": 3, "halfHourFee": 2, "hourFee": 1, "economyFee": 1, ...}``
- Esplora ``/fee-estimates``:
  ``{"1": 87.8, "2": 55.1, ..., "144": 1.0}`` (sat/vB keyed by target)
- LND ``fee.url`` style:
  ``{"fee_by_block_target": {"2": 12500, "6": 5000}}`` (sat/kvB)

All results are normalized to a ``{block_target: sat_per_vb}`` mapping.

Privacy: a fee query reveals only "someone behind this connection wants fee
estimates" (no addresses), but at send time it correlates with an imminent
broadcast, so callers must pass a Tor SOCKS proxy for non-local URLs. The
default (third-party) sources are only auto-enabled when a proxy is available.

Safety: estimates are advisory input from a third party. Entries above
:data:`ABSURD_FEE_RATE_SAT_VB` are discarded at parse time, and every
consumer additionally enforces the operator's hard cap
(``wallet.max_fee_rate_sat_vb``, default 1000 sat/vB) on the resolved rate,
so a compromised or broken provider cannot make transactions spend more than
the configured maximum.
"""

from __future__ import annotations

import ipaddress
import math
import time
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from loguru import logger

# Default sources per network, tried in order. Onion services first (no exit
# node, end-to-end Tor), then their clearnet endpoints over Tor. Two independent
# providers keep a single operator outage from disabling estimation.
_MEMPOOL_ONION = "http://mempoolhqx4isw62xs7abwphsq7ldayuidyx2v2oethdhhj6mlo2r6ad.onion"
_ESPLORA_ONION = "http://explorerzydxu5ecjrkwceayqybizmpjjznk5izmitf2modhcusuqlid.onion"

DEFAULT_FEE_SOURCE_URLS: dict[str, tuple[str, ...]] = {
    "mainnet": (
        f"{_MEMPOOL_ONION}/api/v1/fees/recommended",
        f"{_ESPLORA_ONION}/api/fee-estimates",
        "https://mempool.space/api/v1/fees/recommended",
        "https://blockstream.info/api/fee-estimates",
    ),
    "testnet": (
        f"{_MEMPOOL_ONION}/testnet/api/v1/fees/recommended",
        f"{_ESPLORA_ONION}/testnet/api/fee-estimates",
        "https://mempool.space/testnet/api/v1/fees/recommended",
        "https://blockstream.info/testnet/api/fee-estimates",
    ),
    "testnet4": (
        f"{_MEMPOOL_ONION}/testnet4/api/v1/fees/recommended",
        "https://mempool.space/testnet4/api/v1/fees/recommended",
    ),
    "signet": (
        f"{_MEMPOOL_ONION}/signet/api/v1/fees/recommended",
        "https://mempool.space/signet/api/v1/fees/recommended",
    ),
}

# Parse-time absurdity bound (defense in depth, well above any historically
# observed mainnet fee rate). The real spending limit is the per-wallet
# ``max_fee_rate_sat_vb`` cap enforced by every consumer on the final rate.
ABSURD_FEE_RATE_SAT_VB = 5_000.0

# Process-wide circuit breaker. Tumbler phases create fresh backends, so health
# memory must live here to avoid paying the same dead-onion timeout per phase.
# Failed sources move behind healthy sources for this interval, then regain
# their configured priority (and onion preference) automatically.
SOURCE_RETRY_SECONDS = 600.0
_SOURCE_RETRY_AFTER: dict[str, float] = {}

# Sentinels that explicitly disable external fee estimation.
_DISABLED_VALUES = {"", "off", "none", "disabled"}

# Mapping of mempool.space recommended-fee fields to block targets.
_MEMPOOL_SPACE_TARGETS: dict[str, int] = {
    "fastestFee": 1,
    "halfHourFee": 3,
    "hourFee": 6,
    "economyFee": 144,
}


class FeeSourceError(Exception):
    """Raised when external fee estimates cannot be fetched or parsed."""


class FeeEstimateResult(NamedTuple):
    """Normalized estimates and the source that successfully returned them."""

    estimates: dict[int, float]
    source_url: str


def is_fee_source_disabled(url: str | None) -> bool:
    """Return True when *url* is an explicit "disable" sentinel (not None)."""
    return url is not None and url.strip().lower() in _DISABLED_VALUES


def default_fee_source_urls(network: str) -> list[str]:
    """Return the default fee source URLs for *network* (empty for regtest)."""
    return list(DEFAULT_FEE_SOURCE_URLS.get(network, ()))


def build_fee_source_proxy(
    socks_host: str | None,
    socks_port: int | None,
    stream_isolation: bool = False,
) -> str | None:
    """Build the SOCKS proxy URL used for fee source requests.

    With ``stream_isolation`` the shared MEMPOOL isolation category is used so
    fee queries ride a Tor circuit separate from directory/peer traffic.
    """
    if not socks_host or not socks_port:
        return None
    if stream_isolation:
        from jmcore.tor_isolation import IsolationCategory, build_isolated_proxy_url

        return build_isolated_proxy_url(socks_host, socks_port, IsolationCategory.MEMPOOL)
    return f"socks5h://{socks_host}:{socks_port}"


def parse_fee_estimates(data: Any) -> dict[int, float]:
    """Normalize a fee source response to ``{block_target: sat_per_vb}``.

    Raises :class:`FeeSourceError` when the payload matches no known format
    or contains no usable estimate.
    """
    if not isinstance(data, dict):
        raise FeeSourceError(f"Unrecognized fee source response type: {type(data).__name__}")

    estimates: dict[int, float] = {}

    if isinstance(data.get("fee_by_block_target"), dict):
        # LND fee.url format: sat/kvB keyed by target.
        for key, value in data["fee_by_block_target"].items():
            target, rate = _parse_entry(key, value, max_rate=ABSURD_FEE_RATE_SAT_VB * 1000.0)
            if target is not None and rate is not None:
                estimates[target] = rate / 1000.0
    elif "fastestFee" in data:
        # mempool.space /api/v1/fees/recommended: sat/vB.
        for field, target in _MEMPOOL_SPACE_TARGETS.items():
            value = data.get(field)
            if (
                isinstance(value, int | float)
                and math.isfinite(value)
                and 0 < value <= ABSURD_FEE_RATE_SAT_VB
            ):
                estimates[target] = float(value)
    else:
        # Esplora /fee-estimates: sat/vB keyed by target.
        for key, value in data.items():
            target, rate = _parse_entry(key, value)
            if target is not None and rate is not None:
                estimates[target] = rate

    if not estimates:
        raise FeeSourceError("Fee source response contained no usable estimates")
    return estimates


def _parse_entry(
    key: Any, value: Any, max_rate: float = ABSURD_FEE_RATE_SAT_VB
) -> tuple[int | None, float | None]:
    """Parse one ``target -> rate`` entry; returns (None, None) when invalid.

    ``max_rate`` is the parse-time absurdity bound in the entry's unit
    (sat/vB, or sat/kvB for the LND format).
    """
    try:
        target = int(str(key))
        rate = float(value)
    except (TypeError, ValueError):
        return None, None
    if target <= 0 or not math.isfinite(rate) or rate <= 0 or rate > max_rate:
        logger.warning("Discarding out-of-range fee estimate entry: {} -> {}", key, value)
        return None, None
    return target, rate


def pick_fee_rate(estimates: dict[int, float], target_blocks: int) -> float:
    """Select the sat/vB rate for confirming within *target_blocks*.

    Picks the largest available target that does not exceed the requested one
    (a faster-or-equal estimate is always sufficient); when the request is
    faster than anything available, the fastest available estimate is used.
    """
    if not estimates:
        raise FeeSourceError("No fee estimates available")
    eligible = [t for t in estimates if t <= target_blocks]
    chosen = max(eligible) if eligible else min(estimates)
    return estimates[chosen]


async def fetch_fee_estimates_with_fallback(
    urls: list[str],
    socks_proxy: str | None = None,
    timeout: float = 20.0,
    transport: Any | None = None,
) -> FeeEstimateResult:
    """Fetch estimates from the first working source in *urls*.

    Sources are tried in order (defaults put onion services first, then
    clearnet mirrors); per-source failures are logged and the next source is
    tried. Raises :class:`FeeSourceError` when every source fails.
    """
    if not urls:
        raise FeeSourceError("No fee source URLs configured")
    now = time.monotonic()
    healthy = [url for url in urls if _SOURCE_RETRY_AFTER.get(url, 0.0) <= now]
    cooling_down = [url for url in urls if _SOURCE_RETRY_AFTER.get(url, 0.0) > now]

    last_error: FeeSourceError | None = None
    for url in [*healthy, *cooling_down]:
        try:
            estimates = await fetch_fee_estimates(
                url, socks_proxy=socks_proxy, timeout=timeout, transport=transport
            )
            _SOURCE_RETRY_AFTER.pop(url, None)
            return FeeEstimateResult(estimates=estimates, source_url=url)
        except FeeSourceError as exc:
            logger.warning("Fee source {} failed: {}", url, exc)
            _SOURCE_RETRY_AFTER[url] = time.monotonic() + SOURCE_RETRY_SECONDS
            last_error = exc
    raise FeeSourceError(f"All {len(urls)} fee sources failed (last error: {last_error})")


async def fetch_fee_estimates(
    url: str,
    socks_proxy: str | None = None,
    timeout: float = 20.0,
    transport: Any | None = None,
) -> dict[int, float]:
    """Fetch and parse fee estimates from *url*.

    ``socks_proxy`` routes the request through Tor (``socks5h://host:port``,
    optionally with stream-isolation credentials). ``transport`` allows tests
    to inject an ``httpx`` mock transport.
    """
    import httpx

    client_kwargs: dict[str, Any] = {"timeout": timeout, "trust_env": False}
    if transport is not None:
        client_kwargs["transport"] = transport
    elif socks_proxy and not _is_loopback_url(url):
        try:
            from httpx_socks import AsyncProxyTransport

            from jmcore.tor_isolation import normalize_proxy_url

            normalized = normalize_proxy_url(socks_proxy)
            client_kwargs["transport"] = AsyncProxyTransport.from_url(
                normalized.url, rdns=normalized.rdns
            )
        except ImportError as exc:
            raise FeeSourceError(
                "Tor-routed fee estimation requires httpx-socks; install httpx-socks and retry"
            ) from exc

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        raise FeeSourceError(f"Fee source request failed: {exc}") from exc
    except ValueError as exc:
        raise FeeSourceError(f"Fee source returned invalid JSON: {exc}") from exc
    except Exception as exc:
        # httpx-socks/python-socks proxy failures (for example ProxyTimeoutError
        # while connecting to an unreachable onion service) do not derive from
        # httpx.HTTPError. They must also become FeeSourceError so the caller's
        # fallback chain can try the next source instead of aborting the send.
        raise FeeSourceError(f"Fee source connection failed: {exc}") from exc

    estimates = parse_fee_estimates(data)
    logger.debug("Fetched fee estimates from {}: {}", url, estimates)
    return estimates


def _is_loopback_url(url: str) -> bool:
    """Return whether *url* names localhost or a loopback IP address."""
    hostname = urlsplit(url).hostname
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
