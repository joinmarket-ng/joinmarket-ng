from __future__ import annotations

import json

import httpx
import pytest

from jmswap.buyout_chain import BuyoutChain, ChainError, CoreRpcError


async def test_ready_checks_do_not_create_indexes_or_wallets() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        calls.append(method)
        result = (
            {"chain": "regtest", "initialblockdownload": False}
            if method == "getblockchaininfo"
            else {"txindex": {"synced": True}}
        )
        return httpx.Response(200, json={"result": result})

    async with BuyoutChain(
        "http://localhost", "user", "secret", transport=httpx.MockTransport(respond)
    ):
        pass
    assert calls == ["getblockchaininfo", "getindexinfo"]


@pytest.mark.parametrize("index", [{}, {"txindex": {"synced": False}}])
async def test_missing_or_unsynchronized_index_is_rejected(index: dict[str, object]) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        result = (
            index
            if method == "getindexinfo"
            else {"chain": "regtest", "initialblockdownload": False}
        )
        return httpx.Response(200, json={"result": result})

    chain = BuyoutChain(
        "http://localhost", "user", "secret", transport=httpx.MockTransport(respond)
    )
    with pytest.raises(ChainError, match="txindex"):
        await chain.__aenter__()
    assert chain._client.is_closed


@pytest.mark.parametrize("code,missing", [(-5, True), (-28, False), (-1, False)])
async def test_only_not_found_is_an_absent_transaction(code: int, missing: bool) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": code, "message": "sensitive detail"}})

    chain = BuyoutChain(
        "http://localhost", "user", "secret", transport=httpx.MockTransport(respond)
    )
    try:
        if missing:
            assert await chain.transaction("11" * 32) is None
        else:
            with pytest.raises(CoreRpcError) as failure:
                await chain.transaction("11" * 32)
            assert "sensitive detail" not in str(failure.value)
    finally:
        await chain.__aexit__()


async def test_fee_estimate_uses_floor_and_incremental_relay_policy() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        method = json.loads(request.content)["method"]
        result = (
            {"feerate": 0.00001}
            if method == "estimatesmartfee"
            else {"mempoolminfee": 0.000025, "incrementalrelayfee": 0.00001}
        )
        return httpx.Response(200, json={"result": result})

    chain = BuyoutChain(
        "http://localhost", "user", "secret", transport=httpx.MockTransport(respond)
    )
    try:
        assert await chain.fee_rates() == (3, 1)
    finally:
        await chain.__aexit__()
