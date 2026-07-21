"""Tests for jmcore.fee_source (external HTTP fee estimation)."""

from __future__ import annotations

import json

import httpx
import pytest

from jmcore.fee_source import (
    ABSURD_FEE_RATE_SAT_VB,
    FeeSourceError,
    _is_loopback_url,
    build_fee_source_proxy,
    default_fee_source_urls,
    fetch_fee_estimates,
    fetch_fee_estimates_with_fallback,
    is_fee_source_disabled,
    parse_fee_estimates,
    pick_fee_rate,
)


class TestParseFeeEstimates:
    def test_mempool_space_recommended_format(self) -> None:
        data = {
            "fastestFee": 10,
            "halfHourFee": 5,
            "hourFee": 3,
            "economyFee": 1,
            "minimumFee": 1,
        }
        estimates = parse_fee_estimates(data)
        assert estimates == {1: 10.0, 3: 5.0, 6: 3.0, 144: 1.0}

    def test_esplora_fee_estimates_format(self) -> None:
        data = {"1": 87.882, "2": 55.5, "6": 10.0, "144": 1.027}
        estimates = parse_fee_estimates(data)
        assert estimates == {1: 87.882, 2: 55.5, 6: 10.0, 144: 1.027}

    def test_lnd_fee_url_format_converts_kvb_to_vb(self) -> None:
        data = {"fee_by_block_target": {"2": 12500, "6": 5000}, "min_relay_feerate": 1000}
        estimates = parse_fee_estimates(data)
        assert estimates == {2: 12.5, 6: 5.0}

    def test_invalid_entries_are_skipped(self) -> None:
        data = {"1": 10.0, "abc": 5.0, "6": -1, "0": 2.0}
        estimates = parse_fee_estimates(data)
        assert estimates == {1: 10.0}

    def test_absurd_rates_are_discarded(self) -> None:
        estimates = parse_fee_estimates({"1": ABSURD_FEE_RATE_SAT_VB + 1, "6": 10.0})
        assert estimates == {6: 10.0}

        lnd_estimates = parse_fee_estimates(
            {
                "fee_by_block_target": {
                    "1": (ABSURD_FEE_RATE_SAT_VB + 1) * 1000,
                    "6": 10_000,
                }
            }
        )
        assert lnd_estimates == {6: 10.0}

    def test_empty_or_unknown_payload_raises(self) -> None:
        with pytest.raises(FeeSourceError):
            parse_fee_estimates({})
        with pytest.raises(FeeSourceError):
            parse_fee_estimates([1, 2, 3])
        with pytest.raises(FeeSourceError):
            parse_fee_estimates({"abc": "def"})


class TestPickFeeRate:
    def test_picks_largest_target_not_exceeding_request(self) -> None:
        estimates = {1: 10.0, 3: 5.0, 6: 3.0, 144: 1.0}
        assert pick_fee_rate(estimates, 6) == 3.0
        assert pick_fee_rate(estimates, 5) == 5.0
        assert pick_fee_rate(estimates, 1000) == 1.0

    def test_falls_back_to_fastest_when_request_is_faster(self) -> None:
        estimates = {3: 5.0, 6: 3.0}
        assert pick_fee_rate(estimates, 1) == 5.0

    def test_empty_estimates_raise(self) -> None:
        with pytest.raises(FeeSourceError):
            pick_fee_rate({}, 3)


class TestDefaultsAndSentinels:
    def test_default_urls_per_network(self) -> None:
        mainnet = default_fee_source_urls("mainnet")
        assert mainnet[0].startswith("http://mempoolhqx4isw62")
        assert "https://mempool.space/api/v1/fees/recommended" in mainnet
        assert "https://blockstream.info/api/fee-estimates" in mainnet

        signet = default_fee_source_urls("signet")
        assert signet[0].endswith(".onion/signet/api/v1/fees/recommended")
        assert signet[1] == "https://mempool.space/signet/api/v1/fees/recommended"
        assert default_fee_source_urls("regtest") == []

    def test_disable_sentinels(self) -> None:
        assert is_fee_source_disabled("off") is True
        assert is_fee_source_disabled("NONE") is True
        assert is_fee_source_disabled("") is True
        assert is_fee_source_disabled(" disabled ") is True
        assert is_fee_source_disabled(None) is False
        assert is_fee_source_disabled("https://example.com/fees") is False

    def test_loopback_url_detection(self) -> None:
        assert _is_loopback_url("http://localhost:3006/api/fee-estimates") is True
        assert _is_loopback_url("http://127.0.0.1:3006/api/fee-estimates") is True
        assert _is_loopback_url("http://[::1]:3006/api/fee-estimates") is True
        assert _is_loopback_url("https://mempool.space/api/v1/fees/recommended") is False


class TestBuildFeeSourceProxy:
    def test_plain_socks_proxy(self) -> None:
        assert build_fee_source_proxy("127.0.0.1", 9050) == "socks5h://127.0.0.1:9050"

    def test_missing_host_or_port_returns_none(self) -> None:
        assert build_fee_source_proxy(None, 9050) is None
        assert build_fee_source_proxy("127.0.0.1", None) is None
        assert build_fee_source_proxy("", 9050) is None

    def test_stream_isolation_uses_mempool_category(self) -> None:
        proxy = build_fee_source_proxy("127.0.0.1", 9050, stream_isolation=True)
        assert proxy is not None
        assert proxy.startswith("socks5h://jm-mempool:")
        assert proxy.endswith("@127.0.0.1:9050")


class TestFetchFeeEstimates:
    @pytest.mark.asyncio
    async def test_fallback_uses_next_working_source(self) -> None:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            if request.url.host == "unavailable.example.com":
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "fastestFee": 8,
                    "halfHourFee": 4,
                    "hourFee": 2,
                    "economyFee": 1,
                },
            )

        estimates = await fetch_fee_estimates_with_fallback(
            ["https://unavailable.example.com/fees", "https://working.example.com/fees"],
            transport=httpx.MockTransport(handler),
        )
        assert requested == [
            "https://unavailable.example.com/fees",
            "https://working.example.com/fees",
        ]
        assert estimates.source_url == "https://working.example.com/fees"
        assert estimates.estimates[3] == 4.0

        # The process-wide circuit breaker protects fresh backends (for
        # example, later tumbler phases) from retrying the dead source during
        # its cooldown.
        requested.clear()
        again = await fetch_fee_estimates_with_fallback(
            ["https://unavailable.example.com/fees", "https://working.example.com/fees"],
            transport=httpx.MockTransport(handler),
        )
        assert requested == ["https://working.example.com/fees"]
        assert again.source_url == "https://working.example.com/fees"

    @pytest.mark.asyncio
    async def test_fallback_raises_after_all_sources_fail(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with pytest.raises(FeeSourceError, match="All 2 fee sources failed"):
            await fetch_fee_estimates_with_fallback(
                ["https://one.example.com/fees", "https://two.example.com/fees"],
                transport=httpx.MockTransport(handler),
            )

    @pytest.mark.asyncio
    async def test_fetch_and_parse(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/fees/recommended")
            return httpx.Response(
                200, json={"fastestFee": 8, "halfHourFee": 4, "hourFee": 2, "economyFee": 1}
            )

        estimates = await fetch_fee_estimates(
            "https://example.com/api/v1/fees/recommended",
            transport=httpx.MockTransport(handler),
        )
        assert estimates == {1: 8.0, 3: 4.0, 6: 2.0, 144: 1.0}

    @pytest.mark.asyncio
    async def test_http_error_raises_fee_source_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        with pytest.raises(FeeSourceError, match="request failed"):
            await fetch_fee_estimates(
                "https://example.com/fees", transport=httpx.MockTransport(handler)
            )

    @pytest.mark.asyncio
    async def test_invalid_json_raises_fee_source_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json")

        with pytest.raises(FeeSourceError, match="invalid JSON"):
            await fetch_fee_estimates(
                "https://example.com/fees", transport=httpx.MockTransport(handler)
            )

    @pytest.mark.asyncio
    async def test_esplora_response_end_to_end(self) -> None:
        payload = json.dumps({"1": 20.0, "3": 10.0, "25": 2.0})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=payload, headers={"content-type": "application/json"})

        estimates = await fetch_fee_estimates(
            "https://example.com/fee-estimates", transport=httpx.MockTransport(handler)
        )
        assert estimates == {1: 20.0, 3: 10.0, 25: 2.0}
