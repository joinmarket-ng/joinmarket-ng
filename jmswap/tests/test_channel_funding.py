"""Tests for the externally-funded channel orchestration helpers."""

from __future__ import annotations

import pytest
from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address

from jmswap.channel_funding import (
    complete_external_channel_funding,
    request_external_channel_funding,
)
from jmswap.errors import LightningError

FUNDING_SPK = create_p2tr_scriptpubkey(b"\x05" * 32)


class _FakeLdk:
    def __init__(self, ready_after: int = 1, value: int = 1_000_000) -> None:
        self.opened: tuple[str, str, int] | None = None
        self.calls = 0
        self.ready_after = ready_after
        self.value = value
        self.completed: tuple[str, str, int] | None = None

    async def open_channel_external_funding(
        self, node_pubkey, address, channel_amount_sats, push_to_counterparty_msat=None
    ):
        self.opened = (node_pubkey, address, channel_amount_sats)
        return "ucid-1"

    async def get_funding_output(self, user_channel_id):
        self.calls += 1
        if self.calls >= self.ready_after:
            return True, FUNDING_SPK.hex(), self.value
        return False, "", 0

    async def provide_funding_outpoint(
        self, user_channel_id, funding_txid, funding_vout, raw_tx_hex=None, input_utxos=None
    ):
        self.completed = (user_channel_id, funding_txid, funding_vout)


async def test_request_returns_funding_address() -> None:
    ldk = _FakeLdk(ready_after=2, value=1_000_000)
    ucid, address = await request_external_channel_funding(
        ldk, "02" + "11" * 32, "peer:9735", 1_000_000, "regtest", poll_interval=0.001
    )
    assert ucid == "ucid-1"
    assert address == scriptpubkey_to_address(FUNDING_SPK, "regtest")
    assert ldk.opened == ("02" + "11" * 32, "peer:9735", 1_000_000)


async def test_request_rejects_value_mismatch() -> None:
    ldk = _FakeLdk(ready_after=1, value=999_999)
    with pytest.raises(LightningError, match="!= requested amount"):
        await request_external_channel_funding(
            ldk, "02" + "11" * 32, "peer:9735", 1_000_000, "regtest", poll_interval=0.001
        )


async def test_request_times_out() -> None:
    ldk = _FakeLdk(ready_after=10_000)
    with pytest.raises(LightningError, match="timed out"):
        await request_external_channel_funding(
            ldk,
            "02" + "11" * 32,
            "peer:9735",
            1_000_000,
            "regtest",
            poll_interval=0.001,
            timeout=0.02,
        )


async def test_complete_provides_outpoint() -> None:
    ldk = _FakeLdk()
    await complete_external_channel_funding(ldk, "ucid-1", "ab" * 32, 1)
    assert ldk.completed == ("ucid-1", "ab" * 32, 1)
