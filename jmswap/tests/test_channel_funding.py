"""Tests for the externally-funded channel orchestration helpers."""

from __future__ import annotations

import pytest
from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address

from jmswap.channel_funding import (
    ChannelInputEscrowCapability,
    complete_external_channel_funding,
    missing_channel_input_escrow_capabilities,
    request_external_channel_funding,
    require_channel_input_escrow_capabilities,
)
from jmswap.errors import LightningError

FUNDING_SPK = create_p2tr_scriptpubkey(b"\x05" * 32)


class _FakeLdk:
    def __init__(self, ready_after: int = 1, value: int = 1_000_000) -> None:
        self.opened: tuple[str, str, int, int | None] | None = None
        self.opened_user_channel_id: str | None = None
        self.calls = 0
        self.ready_after = ready_after
        self.value = value
        self.completed: tuple[str, str, int] | None = None
        self.cancelled: list[str] = []

    async def open_channel_external_funding(
        self,
        node_pubkey,
        address,
        channel_amount_sats,
        push_to_counterparty_msat=None,
        user_channel_id=None,
    ):
        self.opened = (
            node_pubkey,
            address,
            channel_amount_sats,
            push_to_counterparty_msat,
        )
        self.opened_user_channel_id = user_channel_id
        return user_channel_id or "ucid-1"

    async def get_funding_output(self, user_channel_id):
        self.calls += 1
        if self.calls >= self.ready_after:
            return True, FUNDING_SPK.hex(), self.value
        return False, "", 0

    async def provide_funding_outpoint(
        self, user_channel_id, funding_txid, funding_vout, raw_tx_hex=None, input_utxos=None
    ):
        self.completed = (user_channel_id, funding_txid, funding_vout)

    async def cancel_external_channel_funding(self, user_channel_id):
        self.cancelled.append(user_channel_id)


async def test_request_returns_funding_address() -> None:
    ldk = _FakeLdk(ready_after=2, value=1_000_000)
    ucid, address = await request_external_channel_funding(
        ldk,
        "02" + "11" * 32,
        "peer:9735",
        1_000_000,
        "regtest",
        push_to_counterparty_msat=250_000_000,
        poll_interval=0.001,
    )
    assert ucid == "ucid-1"
    assert address == scriptpubkey_to_address(FUNDING_SPK, "regtest")
    assert ldk.opened == ("02" + "11" * 32, "peer:9735", 1_000_000, 250_000_000)


async def test_request_uses_caller_selected_pending_channel_id() -> None:
    ldk = _FakeLdk()

    ucid, _address = await request_external_channel_funding(
        ldk,
        "02" + "11" * 32,
        "peer:9735",
        1_000_000,
        "regtest",
        poll_interval=0.001,
        user_channel_id="reserved-channel",
    )

    assert ucid == "reserved-channel"
    assert ldk.opened_user_channel_id == "reserved-channel"


async def test_request_rejects_value_mismatch() -> None:
    ldk = _FakeLdk(ready_after=1, value=999_999)
    with pytest.raises(LightningError, match="!= requested amount"):
        await request_external_channel_funding(
            ldk, "02" + "11" * 32, "peer:9735", 1_000_000, "regtest", poll_interval=0.001
        )
    assert ldk.cancelled == ["ucid-1"]


async def test_request_rejects_invalid_remote_push() -> None:
    ldk = _FakeLdk()
    with pytest.raises(LightningError, match="push"):
        await request_external_channel_funding(
            ldk,
            "02" + "11" * 32,
            "peer:9735",
            1_000_000,
            "regtest",
            push_to_counterparty_msat=1,
        )
    assert ldk.opened is None


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
    assert ldk.cancelled == ["ucid-1"]


async def test_complete_provides_outpoint() -> None:
    ldk = _FakeLdk()
    await complete_external_channel_funding(ldk, "ucid-1", "ab" * 32, 1)
    assert ldk.completed == ("ucid-1", "ab" * 32, 1)


def test_channel_input_escrow_requires_complete_backend_lifecycle() -> None:
    class _Backend:
        channel_input_escrow_capabilities = frozenset(
            {ChannelInputEscrowCapability.SPEND_EXISTING_CHANNEL}
        )

    missing = missing_channel_input_escrow_capabilities(_Backend())

    assert ChannelInputEscrowCapability.SPEND_EXISTING_CHANNEL not in missing
    assert ChannelInputEscrowCapability.PRESIGN_TIMEOUT_RECOVERY in missing
    with pytest.raises(LightningError, match="presign_timeout_recovery"):
        require_channel_input_escrow_capabilities(_Backend())


def test_channel_input_escrow_accepts_complete_backend_lifecycle() -> None:
    class _Backend:
        channel_input_escrow_capabilities = frozenset(ChannelInputEscrowCapability)

    assert missing_channel_input_escrow_capabilities(_Backend()) == frozenset()
    require_channel_input_escrow_capabilities(_Backend())


def test_current_lightning_backends_declare_escrow_unsupported() -> None:
    from jmswap.ldk import LdkServerBackend
    from jmswap.lnd import LndBackend

    assert LdkServerBackend.channel_input_escrow_capabilities == frozenset()
    assert LndBackend.channel_input_escrow_capabilities == frozenset()
