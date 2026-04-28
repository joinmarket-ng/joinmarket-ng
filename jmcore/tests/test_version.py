"""Tests for the version module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmcore.version import (
    UpdateCheckResult,
    _parse_version_tag,
    check_for_updates_from_github,
    get_build_ref,
    get_commit_hash,
    get_version,
)


def _hide_build_info() -> dict[str, object]:
    """Make ``import jmcore._build_info`` raise ImportError.

    Used by tests that exercise the live-git fallback path; without this,
    a wheel built with ``setup.py`` (or a previous test run) may have left
    ``_build_info.py`` on disk and short-circuit the chain.
    """
    return {"jmcore._build_info": None}


class TestGetCommitHash:
    """Tests for get_commit_hash."""

    def test_returns_short_hash_in_git_repo(self) -> None:
        """In this repo, get_commit_hash should return a short hex string."""
        result = get_commit_hash()
        assert result is not None
        assert len(result) >= 7
        assert all(c in "0123456789abcdef" for c in result)

    def test_returns_none_when_git_missing(self) -> None:
        """When git is not found and no _build_info, return None."""
        with (
            patch.dict("sys.modules", _hide_build_info()),
            patch("subprocess.run", side_effect=FileNotFoundError),
        ):
            assert get_commit_hash() is None

    def test_returns_none_on_failure(self) -> None:
        """When git command fails and no _build_info, return None."""
        mock_result = MagicMock()
        mock_result.returncode = 128
        with (
            patch.dict("sys.modules", _hide_build_info()),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert get_commit_hash() is None

    def test_prefers_build_info_over_git(self) -> None:
        """A stamped _build_info.COMMIT must take precedence over git."""
        fake_module = MagicMock()
        fake_module.COMMIT = "deadbee"
        fake_module.REF = "main"
        with patch.dict("sys.modules", {"jmcore._build_info": fake_module}):
            assert get_commit_hash() == "deadbee"
            assert get_build_ref() == "main"

    def test_falls_back_to_git_when_build_info_empty(self) -> None:
        """An empty _build_info.COMMIT must NOT shadow the live git lookup."""
        fake_module = MagicMock()
        fake_module.COMMIT = ""
        fake_module.REF = ""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234\n"
        with (
            patch.dict("sys.modules", {"jmcore._build_info": fake_module}),
            patch("subprocess.run", return_value=mock_result),
        ):
            assert get_commit_hash() == "abc1234"

    def test_get_build_ref_returns_none_when_unstamped(self) -> None:
        with patch.dict("sys.modules", _hide_build_info()):
            assert get_build_ref() is None


class TestParseVersionTag:
    """Tests for _parse_version_tag helper."""

    def test_parse_with_v_prefix(self) -> None:
        assert _parse_version_tag("v1.2.3") == (1, 2, 3)

    def test_parse_without_prefix(self) -> None:
        assert _parse_version_tag("1.2.3") == (1, 2, 3)

    def test_parse_with_whitespace(self) -> None:
        assert _parse_version_tag("  v0.15.0  ") == (0, 15, 0)

    def test_parse_invalid_format_two_parts(self) -> None:
        with pytest.raises(ValueError, match="Invalid version tag format"):
            _parse_version_tag("1.2")

    def test_parse_invalid_format_four_parts(self) -> None:
        with pytest.raises(ValueError, match="Invalid version tag format"):
            _parse_version_tag("1.2.3.4")

    def test_parse_invalid_format_non_numeric(self) -> None:
        with pytest.raises(ValueError):
            _parse_version_tag("v1.2.beta")

    def test_parse_zero_version(self) -> None:
        assert _parse_version_tag("v0.0.0") == (0, 0, 0)

    def test_parse_large_numbers(self) -> None:
        assert _parse_version_tag("v100.200.300") == (100, 200, 300)


class TestCheckForUpdatesFromGitHub:
    """Tests for check_for_updates_from_github."""

    @pytest.mark.asyncio
    async def test_newer_version_available(self) -> None:
        """Test detection of a newer version."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": "v99.0.0"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_for_updates_from_github()

        assert result is not None
        assert result.latest_version == "99.0.0"
        assert result.is_newer is True

    @pytest.mark.asyncio
    async def test_current_version_is_latest(self) -> None:
        """Test when the current version matches the latest."""
        current = get_version()
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": f"v{current}"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_for_updates_from_github()

        assert result is not None
        assert result.latest_version == current
        assert result.is_newer is False

    @pytest.mark.asyncio
    async def test_older_version_on_github(self) -> None:
        """Test when GitHub has an older version (e.g., running pre-release)."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": "v0.0.1"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_for_updates_from_github()

        assert result is not None
        assert result.is_newer is False

    @pytest.mark.asyncio
    async def test_network_error_returns_none(self) -> None:
        """Test that network errors return None instead of raising."""
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_for_updates_from_github()

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self) -> None:
        """Test that malformed JSON returns None."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"no_tag_name": "bad"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_for_updates_from_github()

        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_version_tag_returns_none(self) -> None:
        """Test that an unparseable tag_name returns None."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": "release-candidate-1"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_for_updates_from_github()

        assert result is None

    @pytest.mark.asyncio
    async def test_with_socks_proxy(self) -> None:
        """Test that SOCKS proxy is configured when provided.

        ``socks5h://`` URLs are normalized to ``socks5://`` + ``rdns=True``
        because python-socks does not recognise the ``h`` suffix.
        """
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": "v99.0.0"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_transport = MagicMock()

        with (
            patch("httpx.AsyncClient", return_value=mock_client) as mock_cls,
            patch(
                "httpx_socks.AsyncProxyTransport.from_url",
                return_value=mock_transport,
            ) as mock_from_url,
        ):
            result = await check_for_updates_from_github(
                socks_proxy="socks5h://127.0.0.1:9050",
            )

        assert result is not None
        # socks5h:// is normalized to socks5:// with rdns=True
        mock_from_url.assert_called_once_with("socks5://127.0.0.1:9050", rdns=True)
        # Verify transport was passed to AsyncClient
        call_kwargs = mock_cls.call_args[1]
        assert call_kwargs["transport"] is mock_transport

    @pytest.mark.asyncio
    async def test_socks_import_error_falls_back(self) -> None:
        """Test that missing httpx-socks falls back to no proxy."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"tag_name": "v99.0.0"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("httpx.AsyncClient", return_value=mock_client) as mock_cls,
            patch.dict("sys.modules", {"httpx_socks": None}),
        ):
            result = await check_for_updates_from_github(
                socks_proxy="socks5h://127.0.0.1:9050",
            )

        # Should still work, just without proxy
        assert result is not None
        assert result.is_newer is True
        call_kwargs = mock_cls.call_args[1]
        assert "transport" not in call_kwargs

    @pytest.mark.asyncio
    async def test_http_status_error_returns_none(self) -> None:
        """Test that HTTP errors (404, 500, etc.) return None."""
        import httpx

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=MagicMock(),
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_for_updates_from_github()

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        """Test that timeout returns None."""
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ReadTimeout("Timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await check_for_updates_from_github(timeout=5.0)

        assert result is None


class TestUpdateCheckResult:
    """Tests for UpdateCheckResult dataclass."""

    def test_frozen(self) -> None:
        """Test that UpdateCheckResult is immutable."""
        result = UpdateCheckResult(latest_version="1.0.0", is_newer=True)
        with pytest.raises(AttributeError):
            result.latest_version = "2.0.0"  # type: ignore[misc]

    def test_fields(self) -> None:
        result = UpdateCheckResult(latest_version="1.2.3", is_newer=False)
        assert result.latest_version == "1.2.3"
        assert result.is_newer is False
