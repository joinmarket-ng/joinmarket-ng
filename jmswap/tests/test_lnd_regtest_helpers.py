"""Exercise funding retry decisions without a Docker stack."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import test_lnd_buyout_regtest as regtest

NODE_ID = "02" + "11" * 32
RPC_PREFIX = "compose exec failed: [lncli] rpc error: code = Unknown desc = "


@pytest.mark.parametrize(
    "detail", ["peer disconnected", f"peer {NODE_ID} disconnected", f"peer {NODE_ID} is not online"]
)
def test_pre_broadcast_peer_error_reconnects_then_retries(
    detail: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = MagicMock(node_ids={"bob": NODE_ID})
    opened = {"funding_txid": "22" * 32, "output_index": 0}
    calls = 0

    def lncli(node: str, command: str, *args: str) -> Any:
        nonlocal calls
        assert node == "alice"
        if command == "listpeers":
            return {"peers": [{"pub_key": NODE_ID}]} if calls == 0 else {"peers": []}
        if command == "connect":
            assert args == (f"{NODE_ID}@bob:9735",)
            return {}
        assert command == "openchannel"
        assert args == ("--node_key", NODE_ID, "--local_amt", "100000")
        calls += 1
        if calls == 1:
            raise regtest.RegtestError(RPC_PREFIX + detail)
        return opened

    stack.lncli.side_effect = lncli
    monkeypatch.setattr(regtest.time, "sleep", MagicMock())
    assert (
        regtest._open_channel_rpc(
            cast(regtest.BuyoutRegtest, stack), "alice", "bob", "--local_amt", "100000"
        )
        == opened
    )
    assert calls == 2
    stack.lncli.assert_any_call("alice", "connect", f"{NODE_ID}@bob:9735")


@pytest.mark.parametrize(
    "detail",
    [
        "stream disconnected after sending update",
        "transport is not connected",
        "context deadline exceeded",
        "insufficient funds",
        "peer disconnected; funding state uncertain",
    ],
)
def test_ambiguous_or_other_funding_error_is_never_retried(detail: str) -> None:
    stack = MagicMock(node_ids={"bob": NODE_ID})
    error = regtest.RegtestError(RPC_PREFIX + detail)
    stack.lncli.side_effect = [{"peers": [{"pub_key": NODE_ID}]}, error]
    with pytest.raises(regtest.RegtestError) as raised:
        regtest._open_channel_rpc(cast(regtest.BuyoutRegtest, stack), "alice", "bob")
    assert raised.value is error
    assert stack.lncli.call_count == 2  # listpeers, then exactly one openchannel


def test_pre_broadcast_disconnect_retry_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    stack = MagicMock(node_ids={"bob": NODE_ID})
    error = regtest.RegtestError(RPC_PREFIX + "peer disconnected")
    outcomes: list[Any] = [{"peers": []}, {}, error] * 3
    stack.lncli.side_effect = outcomes
    sleep = MagicMock()
    monkeypatch.setattr(regtest.time, "sleep", sleep)
    with pytest.raises(regtest.RegtestError) as raised:
        regtest._open_channel_rpc(cast(regtest.BuyoutRegtest, stack), "alice", "bob")
    assert raised.value is error
    assert stack.lncli.call_count == 9  # Three checks, connects, and opens.
    assert sleep.call_count == 2
