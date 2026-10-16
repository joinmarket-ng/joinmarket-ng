"""One uniform warning for opt-in experimental features."""

from __future__ import annotations

from collections.abc import Iterable

from loguru import logger

TAPROOT_PIT = "Taproot (tr0) CoinJoin pit"
CHANNEL_RING = "co-funded Lightning channel rings"
CHANNEL_BUYOUT = "private channel buyouts"
CREDENTIAL_MARKET = "PoDLE and fidelity bond credential market"

GUIDE = "docs/experimental-ring-market.md"


def experimental_warnings(features: Iterable[str], network: str) -> list[str]:
    """Return the warning lines for the enabled experimental features, if any."""
    enabled = sorted(set(features))
    if not enabled:
        return []
    lines = [
        f"EXPERIMENTAL features enabled: {', '.join(enabled)}. They are not audited, "
        f"their protocols and on-disk formats may change incompatibly, and bugs can "
        f"lose funds. See {GUIDE}."
    ]
    if network == "mainnet":
        lines.append(
            "EXPERIMENTAL features are running on mainnet. Use a dedicated wallet, "
            "dedicated Lightning nodes, and only amounts you can afford to lose."
        )
    return lines


def warn_experimental(features: Iterable[str], network: str) -> None:
    """Log a warning naming each enabled experimental feature.

    Does nothing when no feature is enabled, so callers can pass the result of
    their own feature checks unconditionally.
    """
    for line in experimental_warnings(features, network):
        logger.warning(line)
