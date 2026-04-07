"""
Privacy-friendly diagnostic information for support and troubleshooting.

Outputs system, backend, and package version details without exposing
any wallet keys, addresses, balances, or transaction data.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
from collections.abc import Mapping
from typing import Annotated, Any

import typer
from jmcore.cli_common import resolve_backend_settings, setup_cli
from jmcore.version import get_version
from loguru import logger

from jmwallet.cli import app

_NEUTRINO_VERSION_FIELDS = (
    "version",
    "neutrino_version",
    "server_version",
    "build_version",
    "release",
    "tag",
)

_NEUTRINO_VERSION_HEADERS = (
    "X-Neutrino-Version",
    "X-Server-Version",
    "X-Version",
)

_NEUTRINO_WATCH_COUNT_FIELDS = (
    "watched_addresses",
    "watched_address_count",
    "watch_count",
)


def _as_text(value: Any) -> str | None:
    """Return non-empty text for simple scalar values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _extract_version_from_payload(payload: Any) -> str | None:
    """Best-effort extraction of server version from a JSON payload."""
    if isinstance(payload, dict):
        for key in _NEUTRINO_VERSION_FIELDS:
            version = _as_text(payload.get(key))
            if version:
                return version
    return _as_text(payload)


def _extract_version_from_headers(headers: Mapping[str, str]) -> str | None:
    """Best-effort extraction of server version from HTTP headers."""
    for header in _NEUTRINO_VERSION_HEADERS:
        version = _as_text(headers.get(header))
        if version:
            return version
    return None


def _extract_version_from_text(payload: str) -> str | None:
    """Extract a version string from plain-text endpoint responses."""
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("neutrinod "):
            candidate = _as_text(line.split(" ", 1)[1])
            if candidate:
                return candidate
        if line.lower().startswith("version:"):
            candidate = _as_text(line.split(":", 1)[1])
            if candidate:
                return candidate
        return line
    return None


def _extract_watched_address_count(
    payload: Any, *, allow_generic_count: bool = False
) -> int | None:
    """Extract watched-address count from endpoint payloads."""
    if isinstance(payload, list):
        return len(payload)

    if not isinstance(payload, dict):
        return None

    keys = list(_NEUTRINO_WATCH_COUNT_FIELDS)
    if allow_generic_count:
        keys.append("count")

    for key in keys:
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value

    for key in ("addresses", "watched"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)

    return None


def _extract_peer_count(payload: Any) -> int | None:
    """Extract connected-peer count from /v1/peers style payloads."""
    if isinstance(payload, dict):
        value = payload.get("count")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        peers = payload.get("peers")
        if isinstance(peers, list):
            return len(peers)
    return None


def _format_bytes_precise(n: int) -> str:
    """Format a byte count with one decimal place."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _get_system_info() -> dict[str, str]:
    """Collect privacy-safe system information."""
    info: dict[str, str] = {}
    info["platform"] = platform.platform()
    info["architecture"] = platform.machine()
    info["python"] = sys.version.split()[0]

    # Memory (psutil optional)
    try:
        import psutil  # type: ignore[import-untyped]

        mem = psutil.virtual_memory()
        info["ram_total"] = _format_bytes_precise(mem.total)
        info["ram_available"] = _format_bytes_precise(mem.available)
    except ImportError:
        # Fall back to /proc/meminfo on Linux
        try:
            with open("/proc/meminfo") as f:
                meminfo: dict[str, int] = {}
                for line in f:
                    parts = line.split()
                    if parts[0] in ("MemTotal:", "MemAvailable:"):
                        # Values are in kB
                        meminfo[parts[0].rstrip(":")] = int(parts[1]) * 1024
                if "MemTotal" in meminfo:
                    info["ram_total"] = _format_bytes_precise(meminfo["MemTotal"])
                if "MemAvailable" in meminfo:
                    info["ram_available"] = _format_bytes_precise(meminfo["MemAvailable"])
        except OSError:
            pass

    # Disk
    try:
        usage = shutil.disk_usage("/")
        info["disk_total"] = _format_bytes_precise(usage.total)
        info["disk_free"] = _format_bytes_precise(usage.free)
    except OSError:
        pass

    return info


def _get_package_versions() -> dict[str, str]:
    """Collect installed package versions for JoinMarket NG components."""
    from importlib.metadata import PackageNotFoundError, version

    packages = [
        "joinmarket-ng-core",
        "joinmarket-ng-wallet",
        "joinmarket-ng-maker",
        "joinmarket-ng-taker",
        "joinmarket-ng-directory-server",
        "joinmarket-ng-orderbook-watcher",
    ]
    versions: dict[str, str] = {}
    for pkg in packages:
        try:
            versions[pkg] = version(pkg)
        except PackageNotFoundError:
            pass
    return versions


async def _get_neutrino_info(
    neutrino_url: str,
    *,
    tls_cert_path: str | None = None,
    auth_token: str | None = None,
) -> dict[str, str]:
    """Probe neutrino-api for sync status and capabilities (no wallet data)."""
    import ssl
    from pathlib import Path

    import httpx

    info: dict[str, str] = {}
    info["url"] = neutrino_url

    # Build httpx client kwargs with optional TLS pinning and auth header.
    client_kwargs: dict[str, Any] = {"timeout": 10.0}
    if tls_cert_path:
        cert = Path(tls_cert_path)
        if cert.is_file():
            ctx = ssl.create_default_context(cafile=str(cert))
            client_kwargs["verify"] = ctx
    if auth_token:
        client_kwargs["headers"] = {"Authorization": f"Bearer {auth_token}"}

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            # /v1/status -- always available
            status_resp = await client.get(f"{neutrino_url}/v1/status")
            status_resp.raise_for_status()
            status = status_resp.json()
            info["status"] = "reachable"
            info["block_height"] = str(status.get("block_height", "?"))
            info["filter_height"] = str(status.get("filter_height", "?"))
            info["synced"] = str(status.get("synced", "?"))

            # Version detection from payload and headers if available.
            status_version = _extract_version_from_payload(status)
            if status_version:
                info["server_version"] = status_version
                info["version_source"] = "/v1/status payload"

            header_version = _extract_version_from_headers(status_resp.headers)
            if header_version:
                info["server_version"] = header_version
                info["version_source"] = "/v1/status header"

            status_watch_count = _extract_watched_address_count(status)
            if status_watch_count is not None:
                info["watched_addresses"] = str(status_watch_count)

            peer_count_from_status = status.get("peers")
            if isinstance(peer_count_from_status, int) and not isinstance(
                peer_count_from_status, bool
            ):
                info["peers_connected"] = str(peer_count_from_status)

            # Optional explicit version endpoints (if server exposes them).
            for endpoint in ("v1/version", "version"):
                try:
                    version_resp = await client.get(f"{neutrino_url}/{endpoint}")
                    version_resp.raise_for_status()

                    endpoint_version: str | None = None
                    try:
                        endpoint_version = _extract_version_from_payload(version_resp.json())
                    except ValueError:
                        endpoint_version = None

                    if endpoint_version is None:
                        endpoint_version = _extract_version_from_headers(version_resp.headers)

                    if endpoint_version is None:
                        endpoint_version = _extract_version_from_text(version_resp.text)

                    if endpoint_version:
                        info["server_version"] = endpoint_version
                        info["version_source"] = f"/{endpoint}"
                        break
                except httpx.HTTPStatusError as exc:
                    # Endpoint does not exist on current server (expected for old versions).
                    if exc.response.status_code in (404, 405):
                        continue
                    info["version_probe"] = f"error ({exc.response.status_code})"
                    break
                except Exception:
                    continue

            # /v1/peers -- useful diagnostics (connected peer count).
            try:
                peers_resp = await client.get(f"{neutrino_url}/v1/peers")
                peers_resp.raise_for_status()
                peers_payload = peers_resp.json()
                peer_count = _extract_peer_count(peers_payload)
                if peer_count is not None:
                    info["peers_connected"] = str(peer_count)

                peers_watch_count = _extract_watched_address_count(peers_payload)
                if peers_watch_count is not None:
                    info["watched_addresses"] = str(peers_watch_count)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in (404, 405):
                    info["peers_status"] = f"error ({exc.response.status_code})"
            except Exception as exc:
                info["peers_status"] = f"error ({exc})"

            # /v1/rescan/status -- v0.7.0+
            try:
                resp2 = await client.get(f"{neutrino_url}/v1/rescan/status")
                resp2.raise_for_status()
                rescan = resp2.json()
                info["rescan_status"] = "available"

                in_progress = rescan.get("in_progress")
                if isinstance(in_progress, bool):
                    info["rescan_in_progress"] = str(in_progress)

                if "last_start_height" in rescan and "last_scanned_tip" in rescan:
                    info["persistent_state"] = "yes (v0.9.0+)"
                    info["last_start_height"] = str(rescan.get("last_start_height", "?"))
                    info["last_scanned_tip"] = str(rescan.get("last_scanned_tip", "?"))
                else:
                    info["persistent_state"] = "no (pre-v0.9.0)"

                rescan_watch_count = _extract_watched_address_count(rescan)
                if rescan_watch_count is not None:
                    info["watched_addresses"] = str(rescan_watch_count)

                rescan_version = _extract_version_from_payload(rescan)
                if rescan_version and "server_version" not in info:
                    info["server_version"] = rescan_version
                    info["version_source"] = "/v1/rescan/status payload"
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    info["rescan_status"] = "unavailable (pre-v0.7.0)"
                else:
                    info["rescan_status"] = f"error ({exc.response.status_code})"
            except Exception as exc:
                info["rescan_status"] = f"error ({exc})"

            if "server_version" not in info:
                info["server_version"] = "unknown"

            if "watched_addresses" not in info:
                info["watched_addresses"] = "unknown (API does not expose watch count)"

    except httpx.ConnectError:
        info["status"] = "unreachable"
    except Exception as exc:
        info["status"] = f"error ({exc})"

    return info


def _detect_deployment() -> str:
    """Best-effort detection of deployment method."""
    # Docker
    if os.path.exists("/.dockerenv"):
        return "docker"
    try:
        with open("/proc/1/cgroup") as f:
            if "docker" in f.read():
                return "docker"
    except OSError:
        pass

    # Flatpak
    if os.environ.get("FLATPAK_ID") or os.path.exists("/app/.flatpak-info"):
        return "flatpak"

    # Snap
    if os.environ.get("SNAP"):
        return "snap"

    return "native"


@app.command("debug-info")
def debug_info(
    network: Annotated[
        str | None,
        typer.Option("--network", "-n", help="Bitcoin network"),
    ] = None,
    backend_type: Annotated[
        str | None,
        typer.Option(
            "--backend", "-b", help="Backend: scantxoutset | descriptor_wallet | neutrino"
        ),
    ] = None,
    neutrino_url: Annotated[
        str | None,
        typer.Option("--neutrino-url", envvar="NEUTRINO_URL"),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Print privacy-friendly diagnostic information for troubleshooting.

    Outputs system details, package versions, and backend status.
    No wallet keys, addresses, balances, or transaction data is included.
    """
    settings = setup_cli(log_level)

    backend = resolve_backend_settings(
        settings,
        network=network,
        backend_type=backend_type,
        neutrino_url=neutrino_url,
    )

    sections: list[str] = []

    # -- JoinMarket NG -------------------------------------------------
    lines = [
        "JoinMarket NG",
        f"  version:    {get_version()}",
        f"  deployment: {_detect_deployment()}",
    ]
    pkg_versions = _get_package_versions()
    if pkg_versions:
        lines.append("  packages:")
        for pkg, ver in pkg_versions.items():
            lines.append(f"    {pkg}: {ver}")
    sections.append("\n".join(lines))

    # -- System --------------------------------------------------------
    sys_info = _get_system_info()
    lines = ["System"]
    for key, value in sys_info.items():
        lines.append(f"  {key}: {value}")
    sections.append("\n".join(lines))

    # -- Backend -------------------------------------------------------
    lines = [
        "Backend",
        f"  type:    {backend.backend_type}",
        f"  network: {backend.network}",
    ]
    if backend.backend_type == "neutrino":
        lines.append(f"  url:     {backend.neutrino_url}")
        lines.append(f"  tls:     {'enabled' if backend.neutrino_tls_cert else 'disabled'}")
        lines.append(f"  auth:    {'enabled' if backend.neutrino_auth_token else 'disabled'}")
    else:
        lines.append(f"  rpc_url: {backend.rpc_url}")
    sections.append("\n".join(lines))

    # -- Neutrino details (async probe) --------------------------------
    if backend.backend_type == "neutrino":
        try:
            neutrino_info = asyncio.run(
                _get_neutrino_info(
                    backend.neutrino_url,
                    tls_cert_path=backend.neutrino_tls_cert,
                    auth_token=backend.neutrino_auth_token,
                )
            )
            lines = ["Neutrino Server"]
            for key, value in neutrino_info.items():
                if key == "url":
                    continue  # Already shown in Backend section
                lines.append(f"  {key}: {value}")
            sections.append("\n".join(lines))
        except Exception as exc:
            logger.debug(f"Neutrino probe failed: {exc}")
            sections.append("Neutrino Server\n  status: probe failed")

    typer.echo("\n\n".join(sections))
