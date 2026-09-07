"""Transport-security diagnostics for direct backend connections."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from loguru import logger

_UNENCRYPTED_REMOTE_HTTP_WARNING = (
    "Configured backend endpoint uses unencrypted HTTP. "
    "Credentials and wallet traffic may be exposed."
)


def _is_loopback_host(hostname: str | None) -> bool:
    """Return whether a parsed hostname is explicitly local without DNS lookup."""
    if hostname is None:
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized).is_loopback
    except ValueError:
        return False


def warn_if_unencrypted_remote_http(url: str) -> None:
    """Warn when a direct backend URL uses HTTP outside an explicit loopback host."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        if url.lower().startswith("http:"):
            logger.warning(_UNENCRYPTED_REMOTE_HTTP_WARNING)
        return

    if parsed.scheme.lower() == "http" and not _is_loopback_host(parsed.hostname):
        logger.warning(_UNENCRYPTED_REMOTE_HTTP_WARNING)
