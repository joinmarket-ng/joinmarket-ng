"""Regression coverage for regtest wallet funding helpers."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, call

import pytest

from tests.e2e import rpc_utils

TARGET_ADDRESS = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"


@pytest.mark.asyncio
async def test_funded_source_sends_without_coinbase_maturity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def core_rpc(
        method: str, params: list[Any] | None = None, wallet: str | None = None
    ) -> Any:
        # Modern Core returns wallet metadata without a balance field.
        if method == "getwalletinfo":
            return {"walletname": wallet, "descriptors": True}
        if method == "getbalance":
            return 6_000.0
        if method == "getnewaddress":
            return TARGET_ADDRESS
        if method == "getblockchaininfo":
            return {"blocks": 583}
        if method == "getblockstats":
            return {"subsidy": 625_000_000}
        return None

    rpc = AsyncMock(side_effect=core_rpc)
    monkeypatch.setattr(rpc_utils, "rpc_call", rpc)

    assert await rpc_utils.ensure_wallet_funded(TARGET_ADDRESS, 1.0, confirmations=2)
    rpc.assert_has_awaits(
        [
            call("getbalance", wallet=rpc_utils.TEST_FUNDER_WALLET),
            call(
                "sendtoaddress",
                [TARGET_ADDRESS, 1.1],
                wallet=rpc_utils.TEST_FUNDER_WALLET,
            ),
            call("getnewaddress", ["", "bech32"], wallet=rpc_utils.TEST_FUNDER_WALLET),
            call("generatetoaddress", [2, TARGET_ADDRESS]),
        ]
    )
    assert rpc.await_count == 4


@pytest.mark.asyncio
async def test_core_wallet_with_sufficient_balance_does_not_mine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rpc = AsyncMock(
        side_effect=lambda method, **kwargs: 2.0 if method == "getbalance" else {}
    )
    monkeypatch.setattr(rpc_utils, "rpc_call", rpc)

    assert await rpc_utils.fund_core_wallet("recipient", TARGET_ADDRESS)
    rpc.assert_awaited_once_with("getbalance", wallet="recipient")


@pytest.mark.asyncio
async def test_core_wallet_funds_only_deficit(monkeypatch: pytest.MonkeyPatch) -> None:
    rpc = AsyncMock(return_value=0.25)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(rpc_utils, "rpc_call", rpc)
    monkeypatch.setattr(rpc_utils, "send_from_test_funder", send)

    assert await rpc_utils.fund_core_wallet("recipient", TARGET_ADDRESS)
    send.assert_awaited_once_with(TARGET_ADDRESS, 0.85, 1)
    rpc.assert_awaited_once_with("getbalance", wallet="recipient")


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_unfunded_source_retains_coinbase_fallback(
    monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    rpc = AsyncMock(
        side_effect=[
            rpc_utils.BitcoinRPCError("Wallet not loaded") if unavailable else 0.0,
            {"blocks": 583},
            {"subsidy": 625_000_000},
            None,
        ]
    )
    monkeypatch.setattr(rpc_utils, "rpc_call", rpc)

    assert await rpc_utils.ensure_wallet_funded(TARGET_ADDRESS, 1.0, confirmations=2)
    rpc.assert_any_await("generatetoaddress", [103, TARGET_ADDRESS])


@pytest.mark.asyncio
@pytest.mark.parametrize("final_balance, funded", [(1.5, True), (0.5, False)])
async def test_core_wallet_checks_balance_after_last_mining_round(
    monkeypatch: pytest.MonkeyPatch, final_balance: float, funded: bool
) -> None:
    rpc = AsyncMock(
        side_effect=[
            0.0,
            0.0,
            {"blocks": 583},
            {"subsidy": 625_000_000},
            None,
            final_balance,
        ]
    )
    monkeypatch.setattr(rpc_utils, "rpc_call", rpc)
    monkeypatch.setattr(
        rpc_utils, "send_from_test_funder", AsyncMock(return_value=False)
    )

    assert (
        await rpc_utils.fund_core_wallet("recipient", TARGET_ADDRESS, max_rounds=1)
        is funded
    )
    rpc.assert_any_await("generatetoaddress", [101, TARGET_ADDRESS], wallet="recipient")
    assert rpc.await_args == call("getbalance", wallet="recipient")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_live_funder_uses_only_requested_confirmation_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-funded Core wallet must not trigger 100-block maturity mining."""
    address = await rpc_utils.rpc_call(
        "getnewaddress", wallet=rpc_utils.TEST_FUNDER_WALLET
    )
    rpc = AsyncMock(wraps=rpc_utils.rpc_call)
    monkeypatch.setattr(rpc_utils, "rpc_call", rpc)

    assert await rpc_utils.ensure_wallet_funded(
        address, amount_btc=0.01, confirmations=2
    )

    # The suite also runs a background miner, so inspect our real RPC requests
    # rather than asserting an exact change in the shared chain height.
    mining_calls = [c for c in rpc.await_args_list if c.args[0] == "generatetoaddress"]
    assert len(mining_calls) == 1
    assert mining_calls[0].args[1][0] == 2
    received = await rpc_utils.rpc_call(
        "getreceivedbyaddress", [address, 2], wallet=rpc_utils.TEST_FUNDER_WALLET
    )
    assert received == pytest.approx(0.11)
