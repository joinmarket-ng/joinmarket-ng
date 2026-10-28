"""Unit coverage for the history benchmark's RPC and funding setup."""

from __future__ import annotations

import json
from collections import deque
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.e2e import test_descriptor_wallet_history_perf as history_perf

RPC_CONFIG = {
    "rpc_url": "http://bitcoin.invalid:18443",
    "rpc_user": "user",
    "rpc_password": "pass",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("next_field", ["next_index", "next"])
async def test_address_pool_derives_unique_ranges_from_next_external_index(
    monkeypatch: pytest.MonkeyPatch, next_field: str
) -> None:
    calls: list[tuple[str, list[Any] | None, str | None]] = []
    descriptor = "wpkh(test-external/*)#test"

    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        calls.append((method, params, wallet))
        if method == "keypoolrefill":
            return None
        if method == "listdescriptors":
            return {
                "descriptors": [
                    {"active": True, "internal": True, "desc": "wpkh(test-internal/*)"},
                    {
                        "active": False,
                        "internal": False,
                        "desc": "wpkh(test-inactive/*)",
                    },
                    {"active": True, "internal": False, "desc": "pkh(test-legacy/*)"},
                    {
                        "active": True,
                        "internal": False,
                        "desc": descriptor,
                        next_field: 37,
                    },
                ]
            }
        if method == "deriveaddresses":
            assert params is not None
            assert params[0] == descriptor
            first, last = params[1]
            return [f"test-address-{index}" for index in range(first, last + 1)]
        raise AssertionError(f"Unexpected RPC: {method}")

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    async with httpx.AsyncClient() as client:
        pool = await history_perf._derive_address_pool(client, "test-wallet", 1010)

    issued = [pool.popleft() for _ in range(1010)]
    assert issued == [f"test-address-{index}" for index in range(37, 1047)]
    assert not pool
    assert all(
        wallet == "test-wallet"
        for method, _, wallet in calls
        if method != "deriveaddresses"
    )
    assert all(
        wallet is None for method, _, wallet in calls if method == "deriveaddresses"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("descriptors", "error"),
    [
        ([], "one active external wpkh descriptor"),
        (
            [{"active": True, "internal": True, "desc": "wpkh(internal/*)"}],
            "one active external wpkh descriptor",
        ),
        (
            [
                {
                    "active": True,
                    "internal": False,
                    "desc": "wpkh(a/*)",
                    "next_index": 0,
                },
                {
                    "active": True,
                    "internal": False,
                    "desc": "wpkh(b/*)",
                    "next_index": 0,
                },
            ],
            "one active external wpkh descriptor",
        ),
        (
            [
                {
                    "active": True,
                    "internal": False,
                    "desc": "wpkh(a/*)",
                    "next_index": True,
                }
            ],
            "invalid next index",
        ),
    ],
)
async def test_address_pool_rejects_ambiguous_or_invalid_descriptor(
    monkeypatch: pytest.MonkeyPatch, descriptors: list[dict[str, Any]], error: str
) -> None:
    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        if method == "keypoolrefill":
            return None
        assert method == "listdescriptors"
        return {"descriptors": descriptors}

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match=error):
            await history_perf._derive_address_pool(client, "test-wallet", 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "batch", [["same", "same"], ["short"], ["valid", None], "not-a-list"]
)
async def test_address_pool_rejects_duplicate_or_incomplete_results(
    monkeypatch: pytest.MonkeyPatch, batch: Any
) -> None:
    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        if method == "keypoolrefill":
            return None
        if method == "listdescriptors":
            return {
                "descriptors": [
                    {
                        "active": True,
                        "internal": False,
                        "desc": "wpkh(a/*)",
                        "next_index": 0,
                    }
                ]
            }
        assert method == "deriveaddresses"
        return batch

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    async with httpx.AsyncClient() as client:
        with pytest.raises(
            RuntimeError, match="duplicate addresses|invalid address batch"
        ):
            await history_perf._derive_address_pool(client, "test-wallet", 2)


@pytest.mark.asyncio
async def test_receive_batches_use_distinct_pool_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batches: list[dict[str, float]] = []

    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        assert wallet == "miner"
        if method == "sendmany":
            assert params is not None
            batches.append(params[1])
        else:
            assert method == "generatetoaddress"
        return []

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    addresses = [f"receive-{index}" for index in range(600)]
    pool = deque(addresses)
    async with httpx.AsyncClient() as client:
        await history_perf._send_many_receives(
            client, 600, 500, "mining-address", "miner", pool
        )

    assert [len(batch) for batch in batches] == [500, 100]
    assert [address for batch in batches for address in batch] == addresses
    assert all(value == 0.0001 for batch in batches for value in batch.values())
    assert not pool


@pytest.mark.asyncio
async def test_workload_phases_share_pools_without_reusing_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destinations: list[str] = []
    coinjoins: list[tuple[list[dict[str, Any]], dict[str, float]]] = []
    broadcasts = 0

    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        nonlocal broadcasts
        if method == "sendmany":
            assert params is not None
            destinations.extend(params[1])
        elif method == "listunspent":
            assert params is not None
            if params[-1].get("minimumAmount") == 0.01:
                return []  # Require the miner-input preparation phase.
            count = 2 if wallet == "test" else 8
            return [
                {"txid": f"{wallet}-tx-{index}", "vout": 0, "amount": 0.01}
                for index in range(count)
            ]
        elif method == "createrawtransaction":
            assert params is not None
            inputs, outputs = params
            coinjoins.append((inputs, outputs))
            destinations.extend(outputs)
            return "raw"
        elif method == "signrawtransactionwithwallet":
            return {"hex": "signed", "complete": True}
        elif method == "sendrawtransaction":
            broadcasts += 1
        else:
            assert method == "generatetoaddress"
        return []

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    test_pool = deque(f"test-address-{index}" for index in range(22))
    miner_pool = deque(f"miner-address-{index}" for index in range(14))
    async with httpx.AsyncClient() as client:
        await history_perf._send_many_receives(
            client, 4, 2, "mining-address", "miner", test_pool
        )
        await history_perf._ensure_miner_utxos(
            client, "mining-address", "miner", 8, miner_pool
        )
        await history_perf._make_coinjoin_like(
            client, 2, 5, "mining-address", "miner", "test", test_pool, miner_pool
        )

    assert broadcasts == 2
    assert len(destinations) == len(set(destinations)) == 36
    assert not test_pool and not miner_pool
    for inputs, outputs in coinjoins:
        assert len(inputs) == 5
        assert sum(value == 0.005 for value in outputs.values()) == 5
        assert sum(address.startswith("test-address-") for address in outputs) == 3
        assert sum(address.startswith("miner-address-") for address in outputs) == 3


@pytest.mark.asyncio
async def test_rpc_reuses_client_with_root_and_wallet_routing() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        if payload["method"] == "broken":
            return httpx.Response(
                500, json={"error": {"code": -1, "message": "RPC failed"}}
            )
        return httpx.Response(200, json={"result": payload["params"], "error": None})

    async with httpx.AsyncClient(
        base_url=RPC_CONFIG["rpc_url"] + "/",
        auth=(RPC_CONFIG["rpc_user"], RPC_CONFIG["rpc_password"]),
        transport=httpx.MockTransport(respond),
    ) as client:
        assert await history_perf._rpc(client, "root_call") == []
        wallet_result = await history_perf._rpc(
            client, "wallet_call", [1], wallet="miner"
        )
        assert wallet_result == [1]
        with pytest.raises(RuntimeError, match="broken RPC error"):
            await history_perf._rpc(client, "broken", wallet="miner")
        assert await history_perf._rpc(client, "after_error", [2]) == [2]
        assert not client.is_closed

    assert client.is_closed
    assert [request.url.path for request in requests] == [
        "/",
        "/wallet/miner",
        "/wallet/miner",
        "/",
    ]
    assert all(
        request.headers["authorization"] == "Basic dXNlcjpwYXNz" for request in requests
    )


@pytest.mark.asyncio
async def test_funder_path_returns_only_after_funds_are_spendable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mined: list[int] = []
    balance = 0.0

    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        nonlocal balance
        if method == "getnewaddress":
            return "miner-address"
        if method == "sendtoaddress":
            assert wallet == "test-funder"
            return "txid"
        if method == "generatetoaddress":
            assert params is not None
            mined.append(params[0])
            balance = 9.0
            return ["block"]
        if method == "getbalance":
            return balance
        raise AssertionError(f"Unexpected RPC: {method}")

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    async with httpx.AsyncClient() as client:
        address = await history_perf._setup_funded_miner(client, "miner")
        assert address == "miner-address"
    assert balance >= 8.0
    assert mined == [1]


@pytest.mark.asyncio
async def test_standalone_funding_stops_when_coinbases_mature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mined: list[int] = []

    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        if method == "getnewaddress":
            return "miner-address"
        if method == "sendtoaddress":
            raise RuntimeError("test-funder is unavailable")
        if method == "getwalletinfo":
            return {"walletname": "miner"}  # Modern Core omits balance here.
        if method == "getbalance":
            return 50.0 if sum(mined) >= 200 else 0.0
        if method == "getblockchaininfo":
            return {"blocks": sum(mined)}
        if method == "getblockstats":
            return {"subsidy": 5_000_000_000}
        if method == "generatetoaddress":
            assert params is not None
            mined.append(params[0])
            return ["block"]
        raise AssertionError(f"Unexpected RPC: {method}")

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    async with httpx.AsyncClient() as client:
        address = await history_perf._setup_funded_miner(client, "miner")
        assert address == "miner-address"
    assert mined == [100, 100]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("balance", "subsidy", "error"),
    [
        (None, 5_000_000_000, "invalid spendable balance"),
        (float("nan"), 5_000_000_000, "invalid spendable balance"),
        (0.0, None, "invalid block subsidy"),
        (0.0, 0, "subsidy is exhausted"),
    ],
)
async def test_unknown_or_exhausted_funding_never_starts_mining(
    monkeypatch: pytest.MonkeyPatch, balance: Any, subsidy: Any, error: str
) -> None:
    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        if method == "sendtoaddress":
            raise RuntimeError("test-funder is unavailable")
        if method == "generatetoaddress":
            pytest.fail("Unknown or exhausted funding must not trigger mining")
        return {
            "getnewaddress": "miner-address",
            "getbalance": balance,
            "getblockchaininfo": {"blocks": 5_000},
            "getblockstats": {"subsidy": subsidy},
        }[method]

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match=error):
            await history_perf._setup_funded_miner(client, "miner")


@pytest.mark.asyncio
@pytest.mark.parametrize("funded_after_last_round", [False, True])
async def test_funding_checks_balance_after_final_allowed_mining_round(
    monkeypatch: pytest.MonkeyPatch,
    funded_after_last_round: bool,
) -> None:
    mined: list[int] = []
    balance_checks = 0

    async def rpc(
        client: httpx.AsyncClient,
        method: str,
        params: list[Any] | None = None,
        wallet: str | None = None,
    ) -> Any:
        nonlocal balance_checks
        if method == "sendtoaddress":
            raise RuntimeError("test-funder is unavailable")
        if method == "getbalance":
            balance_checks += 1
            return 9.0 if funded_after_last_round and len(mined) == 50 else 0.0
        if method == "generatetoaddress":
            assert params is not None
            mined.append(params[0])
            return ["block"]
        return {
            "getnewaddress": "miner-address",
            "getblockchaininfo": {"blocks": sum(mined)},
            "getblockstats": {"subsidy": 5_000_000_000},
        }[method]

    monkeypatch.setattr(history_perf, "_rpc", rpc)
    async with httpx.AsyncClient() as client:
        if funded_after_last_round:
            address = await history_perf._setup_funded_miner(client, "miner")
            assert address == "miner-address"
        else:
            with pytest.raises(RuntimeError, match="after 50 mining rounds"):
                await history_perf._setup_funded_miner(client, "miner")
    assert mined == [100] * 50
    assert balance_checks == 51


@pytest.mark.asyncio
async def test_setup_failure_unloads_wallets_before_client_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unloaded: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["method"] == "unloadwallet"
        unloaded.append(payload["params"][0])
        return httpx.Response(200, json={"result": None, "error": None})

    monkeypatch.setattr(history_perf, "_ensure_wallet", AsyncMock())
    monkeypatch.setattr(
        history_perf,
        "_setup_funded_miner",
        AsyncMock(side_effect=RuntimeError("funding failed")),
    )
    async with httpx.AsyncClient(
        base_url=RPC_CONFIG["rpc_url"] + "/", transport=httpx.MockTransport(respond)
    ) as client:
        with pytest.raises(RuntimeError, match="funding failed"):
            await history_perf.test_get_addresses_with_history_scales_on_large_wallet(
                RPC_CONFIG, client
            )
        assert not client.is_closed

    assert client.is_closed
    assert len(unloaded) == 2
    assert unloaded[0].startswith("jmng_perf_test_")
    assert unloaded[1].startswith("jmng_perf_miner_")
