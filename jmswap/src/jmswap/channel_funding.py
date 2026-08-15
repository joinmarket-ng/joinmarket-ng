"""
Externally-funded Lightning channel opens (channel-as-change endgame).

These helpers drive the forked LDK Server's external-funding RPCs so a channel's
funding output can come from a JoinMarket CoinJoin instead of the node's own
on-chain wallet:

1. :func:`request_external_channel_funding` opens the channel and returns the
   funding *address* (and the channel's ``user_channel_id``); the caller places
   an output paying that address (worth the channel amount) in its CoinJoin and
   broadcasts it;
2. :func:`complete_external_channel_funding` supplies the resulting outpoint,
   driving the channel to ready.

The ``ldk`` argument is any object exposing the external-funding methods of
:class:`jmswap.ldk.LdkServerBackend`.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from enum import StrEnum
from typing import Protocol

from jmcore.bitcoin import scriptpubkey_to_address

from jmswap.errors import LightningError


class ChannelInputEscrowCapability(StrEnum):
    """Backend operations required by confirmation-gated channel-input escrow."""

    SPEND_EXISTING_CHANNEL = "spend_existing_channel"
    PRESIGN_TIMEOUT_RECOVERY = "presign_timeout_recovery"
    MONITOR_PARENT_CONFIRMATIONS = "monitor_parent_confirmations"
    MONITOR_CONFLICTS_AND_REORGS = "monitor_conflicts_and_reorgs"
    FORCE_SINGLE_PART_PAYMENT = "force_single_part_payment"
    EXCLUDE_CONSUMED_CHANNEL = "exclude_consumed_channel"
    PERSIST_RECOVERY_STATE = "persist_recovery_state"
    REBROADCAST_RECOVERY = "rebroadcast_recovery"


REQUIRED_CHANNEL_INPUT_ESCROW_CAPABILITIES = frozenset(ChannelInputEscrowCapability)


def missing_channel_input_escrow_capabilities(
    backend: object,
) -> frozenset[ChannelInputEscrowCapability]:
    """Return escrow operations that ``backend`` does not explicitly provide."""
    declared: frozenset[ChannelInputEscrowCapability] = frozenset(
        getattr(backend, "channel_input_escrow_capabilities", frozenset())
    )
    return REQUIRED_CHANNEL_INPUT_ESCROW_CAPABILITIES.difference(declared)


def require_channel_input_escrow_capabilities(backend: object) -> None:
    """Fail closed unless a backend declares the complete escrow lifecycle."""
    missing = missing_channel_input_escrow_capabilities(backend)
    if missing:
        names = ", ".join(sorted(capability.value for capability in missing))
        raise LightningError(f"backend lacks channel-input escrow capabilities: {names}")


class ExternalFundingLightning(Protocol):
    requires_pre_signing_funding_completion: bool
    channel_input_escrow_capabilities: frozenset[ChannelInputEscrowCapability]

    async def open_channel_external_funding(
        self,
        node_pubkey: str,
        address: str,
        channel_amount_sats: int,
        push_to_counterparty_msat: int | None = None,
    ) -> str: ...

    async def get_funding_output(self, user_channel_id: str) -> tuple[bool, str, int]: ...

    async def provide_funding_outpoint(
        self,
        user_channel_id: str,
        funding_txid: str,
        funding_vout: int,
        raw_tx_hex: str | None = None,
        input_utxos: list[tuple[int, bytes]] | None = None,
    ) -> None: ...

    async def cancel_external_channel_funding(self, user_channel_id: str) -> None: ...


async def request_external_channel_funding(
    ldk: ExternalFundingLightning,
    peer_node_id: str,
    peer_address: str,
    amount_sats: int,
    network: str = "mainnet",
    *,
    push_to_counterparty_msat: int | None = None,
    poll_interval: float = 1.0,
    timeout: float = 60.0,
) -> tuple[str, str]:
    """Open an externally-funded channel and return ``(user_channel_id, address)``.

    Place an output paying ``address`` (worth exactly ``amount_sats``) in the
    CoinJoin, broadcast it, then call :func:`complete_external_channel_funding`.
    """
    if push_to_counterparty_msat is not None and (
        push_to_counterparty_msat < 0
        or push_to_counterparty_msat % 1000
        or push_to_counterparty_msat >= amount_sats * 1000
    ):
        raise LightningError(
            "channel push must be a non-negative whole-satoshi amount below capacity"
        )
    user_channel_id = await ldk.open_channel_external_funding(
        peer_node_id,
        peer_address,
        amount_sats,
        push_to_counterparty_msat=push_to_counterparty_msat,
    )
    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            ready, script_hex, value = await ldk.get_funding_output(user_channel_id)
            if ready:
                if value != amount_sats:
                    raise LightningError(
                        f"channel funding value {value} != requested amount {amount_sats}"
                    )
                address = scriptpubkey_to_address(bytes.fromhex(script_hex), network)
                return user_channel_id, address
            await asyncio.sleep(poll_interval)
        raise LightningError("timed out waiting for the channel funding output")
    except (Exception, asyncio.CancelledError):
        with suppress(Exception):
            await ldk.cancel_external_channel_funding(user_channel_id)
        raise


async def complete_external_channel_funding(
    ldk: ExternalFundingLightning,
    user_channel_id: str,
    funding_txid: str,
    funding_vout: int,
    raw_tx_hex: str | None = None,
    input_utxos: list[tuple[int, bytes]] | None = None,
) -> None:
    """Supply the CoinJoin funding outpoint, driving the channel to ready.

    ``raw_tx_hex`` (the assembled CoinJoin) and ``input_utxos`` (per-input
    ``(value, scriptpubkey)``) are required by the LND backend's PSBT verify/finalize
    flow; the LDK backend takes the outpoint after broadcast and ignores them.
    """
    await ldk.provide_funding_outpoint(
        user_channel_id, funding_txid, funding_vout, raw_tx_hex, input_utxos
    )
