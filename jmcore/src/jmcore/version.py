"""
Centralized version management for JoinMarket NG.

This is the single source of truth for the project version.
All components inherit their version from here.
"""

from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass
from typing import Any

# The project version - update this when releasing
# Format: MAJOR.MINOR.PATCH (Semantic Versioning)
__version__ = "0.27.0"

# Alias for convenience
VERSION = __version__

logger = logging.getLogger(__name__)

GITHUB_RELEASES_URL = "https://api.github.com/repos/joinmarket-ng/joinmarket-ng/releases/latest"


def get_version() -> str:
    """Return the current version string."""
    return __version__


def _get_build_info_module() -> object | None:
    """Return ``jmcore._build_info`` respecting explicit test overrides.

    Tests patch ``sys.modules["jmcore._build_info"]`` to either inject a fake
    stamped module or explicitly hide the module with ``None``. Using
    ``importlib.import_module`` directly would bypass that intent in an editable
    install because the on-disk ``_build_info.py`` still exists inside the
    worktree. Check ``sys.modules`` first so tests can deterministically control
    whether stamped build metadata is visible.
    """
    module_name = "jmcore._build_info"
    if module_name in sys.modules:
        return sys.modules[module_name]

    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def get_commit_hash() -> str | None:
    """Return the short git commit hash, or None if unavailable.

    Resolution order:

    1. ``jmcore._build_info.COMMIT`` -- written at wheel build time by
       ``setup.py``. This is the only source that survives non-editable
       installs (``pip install git+...``, Docker, release wheels) where
       the package directory has no ``.git``.
    2. Live ``git rev-parse --short HEAD`` from the package directory.
       Works for editable installs (``pip install -e``) where the source
       tree retains its working ``.git``.
    3. ``None`` when neither source produced a hash.
    """
    build_info = _get_build_info_module()
    if build_info is not None:
        commit = getattr(build_info, "COMMIT", "") or ""
        commit = commit.strip()
        if commit:
            return commit

    import subprocess
    from pathlib import Path

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).parent,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def get_build_ref() -> str | None:
    """Return the branch or tag the package was built from, if known.

    Populated by ``setup.py`` from the ``JOINMARKET_BUILD_REF`` environment
    variable (set by ``install.sh``) or, as a fallback, the local ``git``
    branch/tag when building from a working tree. Non-editable installs
    that lack both leave this as ``None``; callers should treat that as
    "unknown" rather than "stable".
    """
    build_info = _get_build_info_module()
    if build_info is not None:
        ref = getattr(build_info, "REF", "") or ""
        ref = ref.strip()
        if ref:
            return ref
    return None


def get_version_tuple() -> tuple[int, int, int]:
    """Return the version as a tuple of (major, minor, patch)."""
    parts = __version__.split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def get_version_info() -> dict[str, str | int]:
    """Return version information as a dictionary."""
    major, minor, patch = get_version_tuple()
    return {
        "version": __version__,
        "major": major,
        "minor": minor,
        "patch": patch,
    }


def _parse_version_tag(tag: str) -> tuple[int, int, int]:
    """Parse a version tag like 'v0.15.0' or '0.15.0' into a tuple.

    Raises ValueError if the tag cannot be parsed.
    """
    tag = tag.strip().lstrip("v")
    parts = tag.split(".")
    if len(parts) != 3:
        msg = f"Invalid version tag format: {tag!r}"
        raise ValueError(msg)
    return (int(parts[0]), int(parts[1]), int(parts[2]))


@dataclass(frozen=True)
class UpdateCheckResult:
    """Result of a GitHub update check."""

    latest_version: str
    is_newer: bool


async def check_for_updates_from_github(
    socks_proxy: str | None = None,
    timeout: float = 30.0,
) -> UpdateCheckResult | None:
    """Check GitHub for the latest release and compare with the local version.

    This function makes an HTTP request to the GitHub API. When socks_proxy is
    provided, the request is routed through the given SOCKS5 proxy (e.g. Tor).

    **Privacy note**: This contacts GitHub and reveals your IP (or Tor exit node).
    Only call this when the user has explicitly opted in via ``check_for_updates``.

    Args:
        socks_proxy: Optional SOCKS5 proxy URL (e.g. "socks5h://127.0.0.1:9050").
        timeout: HTTP request timeout in seconds.

    Returns:
        UpdateCheckResult with the latest version and whether it is newer,
        or None if the check failed for any reason.
    """
    import httpx

    client_kwargs: dict[str, Any] = {}
    if socks_proxy:
        try:
            from httpx_socks import AsyncProxyTransport

            from jmcore.tor_isolation import normalize_proxy_url

            # python-socks does not support the socks5h:// scheme directly.
            # normalize_proxy_url converts socks5h:// -> socks5:// + rdns=True
            # so that .onion addresses are resolved by Tor.
            normalized = normalize_proxy_url(socks_proxy)

            transport = AsyncProxyTransport.from_url(normalized.url, rdns=normalized.rdns)
            client_kwargs["transport"] = transport
            logger.debug(
                "Update check configured with SOCKS proxy: %s (rdns=%s)",
                socks_proxy,
                normalized.rdns,
            )
        except ImportError:
            logger.warning("httpx-socks not available, update check without proxy")
        except Exception:
            logger.warning("Failed to configure SOCKS proxy for update check", exc_info=True)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            **client_kwargs,
        ) as client:
            response = await client.get(
                GITHUB_RELEASES_URL,
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()

        data = response.json()
        tag_name: str = data["tag_name"]
        latest = _parse_version_tag(tag_name)
        current = get_version_tuple()
        latest_str = f"{latest[0]}.{latest[1]}.{latest[2]}"

        logger.debug("Update check: current=%s, latest=%s", __version__, latest_str)
        return UpdateCheckResult(latest_version=latest_str, is_newer=latest > current)

    except Exception:
        logger.warning("Failed to check for updates from GitHub", exc_info=True)
        return None
