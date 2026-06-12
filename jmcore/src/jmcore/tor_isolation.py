"""
Tor stream isolation via SOCKS5 authentication.

Tor's ``IsolateSOCKSAuth`` flag (enabled by default on every ``SocksPort``)
ensures that connections carrying different SOCKS5 username/password pairs are
routed through independent circuits.  This module provides helpers that tag
each connection category with a unique credential pair so that, for example,
directory-server traffic and notification traffic never share a circuit.

No Tor configuration changes are required -- ``IsolateSOCKSAuth`` is already
the default.

References
----------
- Tor proposal 171: https://spec.torproject.org/proposals/171-separate-streams.html
- Tails stream isolation design: https://tails.net/contribute/design/stream_isolation/
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote


class IsolationCategory(StrEnum):
    """Well-known connection categories for stream isolation.

    Each value becomes the SOCKS5 *username*; the password is a per-process
    random token so that different process instances also get distinct circuits.
    """

    DIRECTORY = "jm-directory"
    PEER = "jm-peer"
    MEMPOOL = "jm-mempool"
    NOTIFICATION = "jm-notification"
    UPDATE_CHECK = "jm-update-check"
    HEALTH_CHECK = "jm-health-check"
    SWAP = "jm-swap"


# Per-process random token (hex-encoded 16 bytes = 32 hex chars).
# Generated once at import time so every connection in the same process that
# shares a category also shares the same circuit (intentional).
_PROCESS_TOKEN: str = os.urandom(16).hex()


@dataclass(frozen=True)
class IsolationCredentials:
    """SOCKS5 username/password pair for stream isolation."""

    username: str
    password: str


def get_isolation_credentials(category: IsolationCategory) -> IsolationCredentials:
    """Return SOCKS5 credentials that isolate *category* onto its own circuit.

    The username is the category tag; the password is a per-process random
    token.  Two connections with the same ``(username, password)`` will share
    a circuit (Tor groups them), while connections with different usernames
    will be isolated.
    """
    return IsolationCredentials(username=category.value, password=_PROCESS_TOKEN)


def build_isolated_proxy_url(
    host: str,
    port: int,
    category: IsolationCategory,
    *,
    resolve_dns_remotely: bool = True,
) -> str:
    """Build a SOCKS5 proxy URL with stream-isolation credentials embedded.

    Args:
        host: SOCKS proxy host (e.g. ``"127.0.0.1"``).
        port: SOCKS proxy port (e.g. ``9050``).
        category: The isolation category for this connection.
        resolve_dns_remotely: If True use ``socks5h://`` (DNS resolved by
            proxy, required for ``.onion`` addresses).  If False use
            ``socks5://``.

    Returns:
        URL like ``socks5h://jm-directory:a1b2c3...@127.0.0.1:9050``.
    """
    creds = get_isolation_credentials(category)
    scheme = "socks5h" if resolve_dns_remotely else "socks5"
    # Percent-encode username/password in case they contain special chars
    user = quote(creds.username, safe="")
    pwd = quote(creds.password, safe="")
    return f"{scheme}://{user}:{pwd}@{host}:{port}"


@dataclass(frozen=True)
class NormalizedProxyURL:
    """A SOCKS5 proxy URL normalized for python-socks / httpx-socks.

    The ``socks5h://`` scheme (remote DNS via proxy) is the standard way to
    request that the SOCKS proxy resolves hostnames -- required for ``.onion``
    addresses.  However, ``python-socks`` (used by ``httpx-socks``) does not
    recognise the ``h`` suffix and raises ``ValueError``.

    This dataclass carries a ``socks5://`` URL together with an ``rdns`` flag
    that should be forwarded to ``AsyncProxyTransport.from_url(..., rdns=rdns)``.
    """

    url: str
    """Proxy URL with ``socks5://`` scheme (never ``socks5h://``)."""

    rdns: bool
    """Whether DNS should be resolved remotely by the proxy."""


def normalize_proxy_url(proxy_url: str) -> NormalizedProxyURL:
    """Normalize a SOCKS5(h) proxy URL for use with ``httpx-socks``.

    Converts ``socks5h://`` to ``socks5://`` with ``rdns=True`` so that
    ``AsyncProxyTransport.from_url(result.url, rdns=result.rdns)`` works
    correctly even for ``.onion`` addresses.

    Plain ``socks5://`` URLs are returned unchanged with ``rdns=False``.
    """
    if proxy_url.startswith("socks5h://"):
        return NormalizedProxyURL(
            url=proxy_url.replace("socks5h://", "socks5://", 1),
            rdns=True,
        )
    return NormalizedProxyURL(url=proxy_url, rdns=False)
