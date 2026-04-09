"""
Tests for jm-wallet debug-info command.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import patch

import httpx
from jmcore.cli_common import ResolvedBackendSettings
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.cli.debug_info import (
    _detect_deployment,
    _extract_peer_count,
    _extract_version_from_payload,
    _extract_version_from_text,
    _extract_watched_address_count,
    _format_bytes_precise,
    _get_neutrino_info,
    _get_package_versions,
    _get_system_info,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestFormatBytes:
    def test_bytes(self) -> None:
        assert _format_bytes_precise(512) == "512.0 B"

    def test_kilobytes(self) -> None:
        assert _format_bytes_precise(1024) == "1.0 KB"

    def test_megabytes(self) -> None:
        assert _format_bytes_precise(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self) -> None:
        result = _format_bytes_precise(8 * 1024**3)
        assert result == "8.0 GB"

    def test_terabytes(self) -> None:
        result = _format_bytes_precise(2 * 1024**4)
        assert result == "2.0 TB"


class TestGetSystemInfo:
    def test_returns_required_keys(self) -> None:
        info = _get_system_info()
        assert "platform" in info
        assert "architecture" in info
        assert "python" in info

    def test_python_version_format(self) -> None:
        info = _get_system_info()
        # Should be just the version number, not the full sys.version string
        assert " " not in info["python"]
        parts = info["python"].split(".")
        assert len(parts) >= 2

    def test_disk_info_present(self) -> None:
        info = _get_system_info()
        # Disk info should be present on any standard system
        assert "disk_total" in info
        assert "disk_free" in info


class TestGetPackageVersions:
    def test_returns_dict(self) -> None:
        versions = _get_package_versions()
        assert isinstance(versions, dict)

    def test_installed_packages_have_versions(self) -> None:
        versions = _get_package_versions()
        # At minimum jmcore should be installed in the test environment
        for ver in versions.values():
            assert ver  # Non-empty string


class TestNeutrinoExtractors:
    def test_extract_version_from_payload_prefers_known_keys(self) -> None:
        payload = {"server_version": "v0.10.0", "version": "v0.9.0"}
        assert _extract_version_from_payload(payload) == "v0.9.0"

    def test_extract_version_from_text_neutrinod_format(self) -> None:
        assert _extract_version_from_text("neutrinod v0.10.0\n") == "v0.10.0"

    def test_extract_watched_address_count_from_dict_and_list(self) -> None:
        assert _extract_watched_address_count({"watched_addresses": 12}) == 12
        assert _extract_watched_address_count(["a", "b", "c"]) == 3

    def test_extract_watched_address_count_allows_generic_count(self) -> None:
        assert _extract_watched_address_count({"count": 8}, allow_generic_count=False) is None
        assert _extract_watched_address_count({"count": 8}, allow_generic_count=True) == 8

    def test_extract_peer_count(self) -> None:
        assert _extract_peer_count({"count": 6}) == 6
        assert _extract_peer_count({"peers": [{}, {}]}) == 2


class _FakeResponse:
    def __init__(
        self,
        url: str,
        *,
        status_code: int = 200,
        json_data: object | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"{self.status_code} response for {self.url}", request=request, response=response
            )

    def json(self) -> object:
        if self._json_data is None:
            raise ValueError("No JSON payload")
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, responses: dict[str, _FakeResponse | Exception]) -> None:
        self._responses = responses

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        tb: object,
    ) -> bool:
        return False

    async def get(self, url: str) -> _FakeResponse:
        response = self._responses.get(url)
        if isinstance(response, Exception):
            raise response
        if response is None:
            return _FakeResponse(url, status_code=404)
        return response


class TestGetNeutrinoInfo:
    def test_get_neutrino_info_uses_version_endpoint_and_watch_count(self) -> None:
        base = "http://127.0.0.1:8334"
        responses: dict[str, _FakeResponse | Exception] = {
            f"{base}/v1/status": _FakeResponse(
                f"{base}/v1/status",
                json_data={
                    "block_height": 850000,
                    "filter_height": 849999,
                    "synced": True,
                    "peers": 7,
                },
            ),
            f"{base}/v1/version": _FakeResponse(
                f"{base}/v1/version",
                status_code=200,
                text="neutrinod v0.10.0\n",
            ),
            f"{base}/v1/peers": _FakeResponse(
                f"{base}/v1/peers",
                json_data={"peers": [], "count": 6},
            ),
            f"{base}/v1/rescan/status": _FakeResponse(
                f"{base}/v1/rescan/status",
                json_data={
                    "in_progress": False,
                    "last_start_height": 800000,
                    "last_scanned_tip": 850000,
                    "watched_addresses": 42,
                },
            ),
        }

        with patch(
            "httpx.AsyncClient",
            return_value=_FakeAsyncClient(responses),
        ):
            info = asyncio.run(_get_neutrino_info(base))

        assert info["status"] == "reachable"
        assert info["server_version"] == "v0.10.0"
        assert info["version_source"] == "/v1/version"
        assert info["watched_addresses"] == "42"
        assert info["peers_connected"] == "6"

    def test_get_neutrino_info_handles_missing_version_and_watch_count(self) -> None:
        base = "http://127.0.0.1:8334"
        responses: dict[str, _FakeResponse | Exception] = {
            f"{base}/v1/status": _FakeResponse(
                f"{base}/v1/status",
                json_data={"block_height": 850000, "filter_height": 850000, "synced": True},
            ),
            f"{base}/v1/version": _FakeResponse(f"{base}/v1/version", status_code=404),
            f"{base}/version": _FakeResponse(f"{base}/version", status_code=404),
            f"{base}/v1/peers": _FakeResponse(
                f"{base}/v1/peers",
                json_data={"count": 5},
            ),
            f"{base}/v1/rescan/status": _FakeResponse(
                f"{base}/v1/rescan/status",
                json_data={"in_progress": False},
            ),
        }

        with patch(
            "httpx.AsyncClient",
            return_value=_FakeAsyncClient(responses),
        ):
            info = asyncio.run(_get_neutrino_info(base))

        assert info["server_version"] == "unknown"
        assert info["watched_addresses"].startswith("unknown")
        assert info["peers_connected"] == "5"


class TestDetectDeployment:
    def test_native_default(self) -> None:
        # In test environment (not Docker/Flatpak/Snap), should return native
        with (
            patch("os.path.exists", return_value=False),
            patch.dict("os.environ", {}, clear=True),
        ):
            assert _detect_deployment() == "native"

    def test_docker_dockerenv(self) -> None:
        def fake_exists(path: str) -> bool:
            return path == "/.dockerenv"

        with patch("jmwallet.cli.debug_info.os.path.exists", side_effect=fake_exists):
            assert _detect_deployment() == "docker"

    def test_flatpak(self) -> None:
        with (
            patch("jmwallet.cli.debug_info.os.path.exists", return_value=False),
            patch.dict("os.environ", {"FLATPAK_ID": "org.example.App"}),
        ):
            assert _detect_deployment() == "flatpak"

    def test_snap(self) -> None:
        with (
            patch("jmwallet.cli.debug_info.os.path.exists", return_value=False),
            patch.dict("os.environ", {"SNAP": "/snap/app/1"}, clear=True),
        ):
            assert _detect_deployment() == "snap"


# ---------------------------------------------------------------------------
# CLI integration tests (Typer runner)
# ---------------------------------------------------------------------------


class TestDebugInfoCommand:
    """Test the debug-info CLI command via CliRunner."""

    def test_basic_output(self) -> None:
        """Command runs and outputs expected sections."""
        result = runner.invoke(app, ["debug-info", "--backend", "scantxoutset"])
        assert result.exit_code == 0, f"Failed: {result.stdout}"
        assert "JoinMarket NG" in result.stdout
        assert "System" in result.stdout
        assert "Backend" in result.stdout
        assert "version:" in result.stdout

    def test_shows_backend_type(self) -> None:
        result = runner.invoke(
            app, ["debug-info", "--backend", "scantxoutset", "--network", "signet"]
        )
        assert result.exit_code == 0
        assert "scantxoutset" in result.stdout
        assert "signet" in result.stdout

    def test_neutrino_backend_unreachable(self) -> None:
        """Neutrino backend gracefully handles unreachable server."""
        result = runner.invoke(
            app,
            [
                "debug-info",
                "--backend",
                "neutrino",
                "--neutrino-url",
                "http://127.0.0.1:19999",
            ],
        )
        assert result.exit_code == 0
        assert "Neutrino Server" in result.stdout
        # Should report unreachable, not crash
        assert "unreachable" in result.stdout or "error" in result.stdout

    def test_neutrino_backend_with_mock_server(self) -> None:
        """Neutrino backend shows server info when reachable."""
        with patch("jmwallet.cli.debug_info._get_neutrino_info") as mock_probe:
            mock_probe.return_value = {
                "status": "reachable",
                "server_version": "v0.10.0",
                "block_height": "850000",
                "filter_height": "849999",
                "synced": "True",
                "peers_connected": "6",
                "rescan_status": "available",
                "rescan_in_progress": "False",
                "persistent_state": "yes (v0.9.0+)",
                "last_start_height": "800000",
                "last_scanned_tip": "850000",
                "watched_addresses": "42",
            }

            result = runner.invoke(
                app,
                [
                    "debug-info",
                    "--backend",
                    "neutrino",
                    "--neutrino-url",
                    "http://127.0.0.1:8334",
                ],
            )
            assert result.exit_code == 0, f"Failed: {result.stdout}"
            assert "Neutrino Server" in result.stdout
            assert "850000" in result.stdout
            assert "v0.10.0" in result.stdout
            assert "watched_addresses" in result.stdout

    def test_no_wallet_data_leaked(self) -> None:
        """Ensure no wallet-sensitive data appears in output."""
        result = runner.invoke(app, ["debug-info", "--backend", "scantxoutset"])
        assert result.exit_code == 0
        output = result.stdout.lower()
        # Should not contain sensitive wallet material terms.
        for term in ("mnemonic", "seed phrase", "xpriv", "xpub", "zpub", "private key"):
            assert term not in output, f"Sensitive term '{term}' found in debug-info output"

        # Should not leak address-like payloads.
        address_patterns = [
            r"\bbc1[ac-hj-np-z02-9]{20,}\b",
            r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b",
            r"\b(tb1|bcrt1)[ac-hj-np-z02-9]{20,}\b",
        ]
        for pattern in address_patterns:
            assert re.search(pattern, result.stdout) is None

    def test_deployment_shown(self) -> None:
        result = runner.invoke(app, ["debug-info", "--backend", "scantxoutset"])
        assert result.exit_code == 0
        assert "deployment:" in result.stdout

    def test_neutrino_tls_auth_status_disabled(self) -> None:
        """Backend section should show TLS/auth disabled when not configured."""
        no_tls_backend = ResolvedBackendSettings(
            network="signet",
            bitcoin_network="signet",
            backend_type="neutrino",
            rpc_url="",
            rpc_user="",
            rpc_password="",
            neutrino_url="http://127.0.0.1:8334",
            neutrino_add_peers=[],
            data_dir=Path("/tmp/jm-test"),
            neutrino_tls_cert=None,
            neutrino_auth_token=None,
        )
        with (
            patch("jmwallet.cli.debug_info._get_neutrino_info") as mock_probe,
            patch(
                "jmwallet.cli.debug_info.resolve_backend_settings",
                return_value=no_tls_backend,
            ),
        ):
            mock_probe.return_value = {"status": "reachable", "block_height": "100"}
            result = runner.invoke(
                app,
                [
                    "debug-info",
                    "--backend",
                    "neutrino",
                    "--neutrino-url",
                    "http://127.0.0.1:8334",
                ],
            )
            assert result.exit_code == 0, f"Failed: {result.stdout}"
            assert "tls:     disabled" in result.stdout
            assert "auth:    disabled" in result.stdout

    def test_neutrino_tls_auth_status_enabled(self) -> None:
        """Backend section should show TLS/auth enabled when configured."""
        tls_backend = ResolvedBackendSettings(
            network="signet",
            bitcoin_network="signet",
            backend_type="neutrino",
            rpc_url="",
            rpc_user="",
            rpc_password="",
            neutrino_url="https://127.0.0.1:8334",
            neutrino_add_peers=[],
            data_dir=Path("/tmp/jm-test"),
            neutrino_tls_cert="/path/to/tls.cert",
            neutrino_auth_token="deadbeef",
        )
        with (
            patch("jmwallet.cli.debug_info._get_neutrino_info") as mock_probe,
            patch(
                "jmwallet.cli.debug_info.resolve_backend_settings",
                return_value=tls_backend,
            ),
        ):
            mock_probe.return_value = {"status": "reachable", "block_height": "100"}
            result = runner.invoke(
                app,
                [
                    "debug-info",
                    "--backend",
                    "neutrino",
                    "--neutrino-url",
                    "https://127.0.0.1:8334",
                ],
            )
            assert result.exit_code == 0, f"Failed: {result.stdout}"
            assert "tls:     enabled" in result.stdout
            assert "auth:    enabled" in result.stdout
