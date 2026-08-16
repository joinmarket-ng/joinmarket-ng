"""LND backend for channel-as-change: open a real simple-taproot channel whose
funding output is a CoinJoin output.

Unlike the forked LDK Server (whose channel funding output is a P2WSH 2-of-2),
LND's ``SIMPLE_TAPROOT`` channels use a MuSig2 2-of-2 **P2TR** funding output, so a
maker's CoinJoin change can fund a real Lightning channel that looks like any other
Taproot spend on chain. The funding output is placed in the CoinJoin via LND's PSBT
funding shim (``OpenChannel`` with a ``PsbtShim`` + ``no_publish``), then the channel
point is pinned with ``FundingStateStep`` (verify, then finalize).

This implements the :class:`jmswap.channel_funding.ExternalFundingLightning` protocol
(plus ``node_id`` / ``connect_peer``) so it slots in wherever the LDK backend is used.
Standalone LND cannot ``push_amt`` on a taproot channel (the overlay needs a special
build), so a pooled channel is rebalanced with a normal post-open payment instead;
``open_channel_external_funding`` therefore ignores a requested push and the caller
arranges the rebalance separately.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, ClassVar

from jmcore.bitcoin import (
    TxOutput,
    address_to_scriptpubkey,
    encode_varint,
    get_txid,
    parse_transaction,
    serialize_output,
    serialize_transaction,
)
from loguru import logger

from jmswap.channel_escrow import ChannelSnapshot, EscrowOutpoint
from jmswap.channel_funding import ChannelInputEscrowCapability
from jmswap.peer_transport import (
    ESCROW_PEER_CUSTOM_MESSAGE_TYPE,
    MAX_LND_CUSTOM_MESSAGE_PAYLOAD,
    RawReceivedCustomMessage,
)

# BIP-174 per-input witness UTXO key type.
_PSBT_IN_WITNESS_UTXO = 0x01

# LND ships ECDSA TLS certificates; gRPC must be told to allow that cipher suite
# before any channel is created, or the TLS handshake fails. Set it at import time.
os.environ.setdefault("GRPC_SSL_CIPHER_SUITES", "HIGH+ECDSA")

_PSBT_MAGIC = b"\x70\x73\x62\x74\xff"

# BOLT custom message types occupy the upper half of the uint16 type space. The wire
# message includes the two-byte type, leaving 65,533 bytes for the payload.
LND_CUSTOM_MESSAGE_TYPE_MIN = 32_768
LND_CUSTOM_MESSAGE_TYPE_MAX = 65_535
LND_CUSTOM_MESSAGE_MAX_DATA_SIZE = MAX_LND_CUSTOM_MESSAGE_PAYLOAD
JOINMARKET_ESCROW_MESSAGE_TYPE = ESCROW_PEER_CUSTOM_MESSAGE_TYPE


def _validate_compressed_node_id_bytes(peer_node_id: bytes) -> bytes:
    if not isinstance(peer_node_id, bytes):
        raise ValueError("peer node ID must be bytes")
    if len(peer_node_id) != 33 or peer_node_id[0] not in (0x02, 0x03):
        raise ValueError("peer node ID must be a 33-byte compressed public key")
    return peer_node_id


def _validate_compressed_node_id(peer_node_id: str) -> bytes:
    if not isinstance(peer_node_id, str) or len(peer_node_id) != 66:
        raise ValueError("peer node ID must be a 33-byte compressed public key hex string")
    try:
        peer = bytes.fromhex(peer_node_id)
    except ValueError as exc:
        raise ValueError("peer node ID must be a 33-byte compressed public key hex string") from exc
    return _validate_compressed_node_id_bytes(peer)


def _validate_custom_message_type(message_type: int) -> int:
    if isinstance(message_type, bool) or not isinstance(message_type, int):
        raise ValueError("custom message type must be an integer")
    if not LND_CUSTOM_MESSAGE_TYPE_MIN <= message_type <= LND_CUSTOM_MESSAGE_TYPE_MAX:
        raise ValueError(
            f"custom message type must be in {LND_CUSTOM_MESSAGE_TYPE_MIN}.."
            f"{LND_CUSTOM_MESSAGE_TYPE_MAX}"
        )
    return message_type


def _validate_custom_message_data(data: bytes) -> bytes:
    if not isinstance(data, bytes):
        raise ValueError("custom message data must be bytes")
    if not data:
        raise ValueError("custom message data cannot be empty")
    if len(data) > LND_CUSTOM_MESSAGE_MAX_DATA_SIZE:
        raise ValueError(
            f"custom message data must be at most {LND_CUSTOM_MESSAGE_MAX_DATA_SIZE} bytes"
        )
    return data


def _psbt_kv(key_type: int, value: bytes) -> bytes:
    key = bytes([key_type])
    return encode_varint(len(key)) + key + encode_varint(len(value)) + value


def unsigned_psbt_from_tx(raw_tx_hex: str, input_utxos: list[tuple[int, bytes]]) -> bytes:
    """Build a BIP-174 PSBT wrapping the unsigned form of ``raw_tx_hex``.

    LND's ``PsbtVerify`` derives the channel point from the unsigned transaction and
    checks the input sum, so each input carries a ``WITNESS_UTXO``. ``input_utxos`` is
    the per-input ``(value_sats, scriptpubkey)`` of each spent output, in input order.
    Witnesses are stripped so the txid matches the eventual segwit broadcast.
    """
    tx = parse_transaction(raw_tx_hex)
    if len(input_utxos) != len(tx.inputs):
        raise ValueError(
            f"input_utxos has {len(input_utxos)} entries, tx has {len(tx.inputs)} inputs"
        )
    for inp in tx.inputs:
        inp.scriptsig = b""  # the BIP-174 unsigned tx has empty scriptSigs
    unsigned = serialize_transaction(tx.version, tx.inputs, tx.outputs, tx.locktime, witnesses=None)
    out = _PSBT_MAGIC + _psbt_kv(0x00, unsigned) + b"\x00"  # global unsigned tx, end map
    for value, spk in input_utxos:
        witness_utxo = serialize_output(TxOutput(value=value, script=spk))
        out += _psbt_kv(_PSBT_IN_WITNESS_UTXO, witness_utxo) + b"\x00"  # input map + end
    out += b"\x00" * len(tx.outputs)  # empty per-output maps
    return out


@dataclass
class _PendingOpen:
    pending_chan_id: bytes
    stream: Any
    funding_address: str = ""
    funding_amount: int = 0
    verified: bool = False


class LndEscrowStage(IntEnum):
    """Durable stages reported by the patched LND escrow subserver."""

    LOCKED = 0
    PARENT_PREPARED = 1
    PARENT_SIGNED = 2
    FINALIZED = 3


@dataclass(frozen=True)
class LndEscrowSigningAttempt:
    """One in-memory MuSig2 attempt for an immutable escrow parent."""

    attempt_id: bytes
    public_nonce: bytes


@dataclass(frozen=True)
class LndEscrowStatus:
    """Durable status for a channel-input signing session."""

    stage: LndEscrowStage
    parent_txid: str | None
    finalized: bool


@dataclass(frozen=True)
class LndFinalizedEscrowParent:
    """The exact fully signed parent persisted and published by patched LND."""

    tx_hex: str
    txid: str


@dataclass
class LndBackend:
    """Minimal LND gRPC client for taproot channel-as-change via PSBT funding."""

    requires_pre_signing_funding_completion: ClassVar[bool] = True
    channel_input_escrow_capabilities: ClassVar[frozenset[ChannelInputEscrowCapability]] = (
        frozenset()
    )

    host: str  # 'host:port' of the LND gRPC listener
    tls_cert: bytes
    macaroon_hex: str
    network: str = "signet"
    escrow_macaroon_hex: str | None = None
    _channel: Any = field(default=None, repr=False)
    _escrow_channel: Any = field(default=None, repr=False)
    _stub: Any = field(default=None, repr=False)
    _escrow_stub: Any = field(default=None, repr=False)
    _pending: dict[str, _PendingOpen] = field(default_factory=dict, repr=False)

    @classmethod
    def from_paths(
        cls,
        host: str,
        tls_cert_path: str,
        macaroon_path: str,
        network: str = "signet",
        escrow_macaroon_path: str | None = None,
    ) -> LndBackend:
        with open(tls_cert_path, "rb") as f:
            cert = f.read()
        with open(macaroon_path, "rb") as f:
            mac = f.read().hex()
        escrow_mac = None
        if escrow_macaroon_path is not None:
            with open(escrow_macaroon_path, "rb") as f:
                escrow_mac = f.read().hex()
        return cls(
            host=host,
            tls_cert=cert,
            macaroon_hex=mac,
            network=network,
            escrow_macaroon_hex=escrow_mac,
        )

    def _ensure(self) -> None:
        if self._stub is not None:
            return
        import grpc

        from jmswap.lndrpc import channelescrow_pb2_grpc as escrow_grpc
        from jmswap.lndrpc import lightning_pb2_grpc as lng

        ssl = grpc.ssl_channel_credentials(self.tls_cert)

        def authenticated_channel(macaroon_hex: str) -> Any:
            def _add_macaroon(_ctx: Any, cb: Any) -> None:
                cb((("macaroon", macaroon_hex),), None)

            creds = grpc.composite_channel_credentials(
                ssl, grpc.metadata_call_credentials(_add_macaroon)
            )
            return grpc.aio.secure_channel(self.host, creds)

        self._channel = authenticated_channel(self.macaroon_hex)
        self._stub = lng.LightningStub(self._channel)
        if self.escrow_macaroon_hex is None:
            self._escrow_channel = self._channel
        else:
            self._escrow_channel = authenticated_channel(self.escrow_macaroon_hex)
        self._escrow_stub = escrow_grpc.ChannelEscrowStub(self._escrow_channel)

    async def close(self) -> None:
        if self._escrow_channel is not None and self._escrow_channel is not self._channel:
            await self._escrow_channel.close()
        if self._channel is not None:
            await self._channel.close()
        self._channel = None
        self._escrow_channel = None
        self._stub = None
        self._escrow_stub = None

    async def node_id(self) -> str:
        self._ensure()
        from jmswap.lndrpc import lightning_pb2 as ln

        info = await self._stub.GetInfo(ln.GetInfoRequest())
        return str(info.identity_pubkey)

    async def connect_peer(self, node_pubkey: str, host: str) -> None:
        """Connect to a peer (idempotent: an existing connection is not an error)."""
        self._ensure()
        from jmswap.lndrpc import lightning_pb2 as ln

        req = ln.ConnectPeerRequest(
            addr=ln.LightningAddress(pubkey=node_pubkey, host=host), perm=True
        )
        try:
            await self._stub.ConnectPeer(req)
        except Exception as e:  # noqa: BLE001 - already-connected is benign
            if "already connected" not in str(e).lower():
                raise

    async def send_custom_message(
        self,
        peer_node_id: str,
        data: bytes,
        message_type: int = JOINMARKET_ESCROW_MESSAGE_TYPE,
    ) -> None:
        """Queue one JoinMarket custom message for an authenticated LND peer."""
        peer = _validate_compressed_node_id(peer_node_id)
        payload = _validate_custom_message_data(data)
        custom_type = _validate_custom_message_type(message_type)
        self._ensure()
        from jmswap.lndrpc import lightning_pb2 as ln

        response = await self._stub.SendCustomMessage(
            ln.SendCustomMessageRequest(peer=peer, type=custom_type, data=payload)
        )
        status = str(response.status).strip()
        if status != "message sent successfully":
            raise RuntimeError(f"LND returned an unexpected custom message status: {status}")

    async def subscribe_custom_messages(
        self, message_type: int = JOINMARKET_ESCROW_MESSAGE_TYPE
    ) -> AsyncIterator[RawReceivedCustomMessage]:
        """Yield validated inbound messages of one LND custom type."""
        custom_type = _validate_custom_message_type(message_type)
        self._ensure()
        from jmswap.lndrpc import lightning_pb2 as ln

        stream = self._stub.SubscribeCustomMessages(ln.SubscribeCustomMessagesRequest())
        async for message in stream:
            if message.type != custom_type:
                continue
            peer = _validate_compressed_node_id_bytes(message.peer)
            data = _validate_custom_message_data(message.data)
            yield RawReceivedCustomMessage(peer_node_id=peer.hex(), data=data)

    @staticmethod
    def _escrow_session_id(session_id: str) -> bytes:
        """Map the application session identifier to LND's fixed 32-byte key."""
        if not session_id:
            raise ValueError("escrow session ID cannot be empty")
        import hashlib

        return hashlib.sha256(session_id.encode()).digest()

    @staticmethod
    def _parse_channel_point(channel_point: str) -> EscrowOutpoint:
        try:
            txid, output_index = channel_point.rsplit(":", 1)
            return EscrowOutpoint(txid=txid, vout=int(output_index))
        except (ValueError, TypeError) as exc:
            raise ValueError("LND channel point must use txid:vout format") from exc

    @staticmethod
    def _rpc_channel_point(outpoint: EscrowOutpoint) -> Any:
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        return escrow.ChannelPoint(
            funding_txid=bytes.fromhex(outpoint.txid)[::-1],
            output_index=outpoint.vout,
        )

    @staticmethod
    def _rpc_txid(raw_hash: bytes) -> str:
        if len(raw_hash) != 32:
            raise RuntimeError("patched LND returned an invalid transaction ID")
        return raw_hash[::-1].hex()

    async def _resolve_channel_point(self, channel_id: str) -> EscrowOutpoint:
        if ":" in channel_id:
            return self._parse_channel_point(channel_id)

        self._ensure()
        from jmswap.lndrpc import lightning_pb2 as ln

        response = await self._stub.ListChannels(ln.ListChannelsRequest())
        for channel in response.channels:
            if str(channel.chan_id) == channel_id:
                return self._parse_channel_point(str(channel.channel_point))
        raise ValueError(f"LND channel not found: {channel_id}")

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> ChannelSnapshot:
        """Quiesce and durably lock one existing SIMPLE_TAPROOT channel."""
        self._ensure()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        point = await self._resolve_channel_point(channel_id)
        response = await self._escrow_stub.FreezeChannel(
            escrow.FreezeChannelRequest(
                channel_point=self._rpc_channel_point(point),
                session_id=self._escrow_session_id(session_id),
            )
        )
        if response.funding_outpoint is None:
            raise RuntimeError("patched LND omitted the frozen funding outpoint")
        frozen_point = EscrowOutpoint(
            txid=self._rpc_txid(response.funding_outpoint.funding_txid),
            vout=response.funding_outpoint.output_index,
        )
        if frozen_point != point:
            raise RuntimeError("patched LND froze a different channel outpoint")
        return ChannelSnapshot(
            session_id=session_id,
            channel_id=channel_id,
            funding_outpoint=frozen_point,
            capacity_sats=response.capacity_sat,
            maker_claim_sats=response.local_claim_sat,
            peer_claim_sats=response.remote_claim_sat,
            quiescent=True,
            active_htlcs=0,
        )

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: list[tuple[int, bytes]],
        channel_input_index: int,
    ) -> str:
        """Durably bind LND to the exact parent and complete BIP341 prevouts."""
        self._ensure()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.PrepareParent(
            escrow.PrepareParentRequest(
                channel_point=self._rpc_channel_point(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
                raw_parent_tx=bytes.fromhex(parent_tx_hex),
                prev_outs=[
                    escrow.PrevOut(value_sat=value, pk_script=script)
                    for value, script in input_utxos
                ],
                channel_input_index=channel_input_index,
            )
        )
        response_txid = self._rpc_txid(response.txid)
        if response_txid != get_txid(parent_tx_hex):
            raise RuntimeError("patched LND authorized an unexpected parent transaction ID")
        return response_txid

    async def begin_channel_input_signing(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        *,
        restart_attempt: bool = False,
    ) -> LndEscrowSigningAttempt:
        """Create or retrieve a MuSig2 nonce for the prepared exact parent."""
        self._ensure()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.BeginSigning(
            escrow.BeginSigningRequest(
                channel_point=self._rpc_channel_point(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
                restart_attempt=restart_attempt,
            )
        )
        if len(response.attempt_id) != 32 or len(response.local_public_nonce) != 66:
            raise RuntimeError("patched LND returned invalid MuSig2 attempt data")
        return LndEscrowSigningAttempt(
            attempt_id=bytes(response.attempt_id),
            public_nonce=bytes(response.local_public_nonce),
        )

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes:
        """Persist and return this endpoint's partial signature."""
        if len(attempt.attempt_id) != 32 or len(attempt.public_nonce) != 66:
            raise ValueError("invalid local MuSig2 signing attempt")
        if len(remote_public_nonce) != 66:
            raise ValueError("remote MuSig2 public nonce must be exactly 66 bytes")
        self._ensure()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.SignParent(
            escrow.SignParentRequest(
                channel_point=self._rpc_channel_point(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
                attempt_id=attempt.attempt_id,
                remote_public_nonce=remote_public_nonce,
            )
        )
        partial = bytes(response.local_partial_signature)
        if len(partial) != 32:
            raise RuntimeError("patched LND returned an invalid MuSig2 partial signature")
        return partial

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        """Combine, verify, persist, and publish the exact parent transaction."""
        if len(attempt.attempt_id) != 32 or len(attempt.public_nonce) != 66:
            raise ValueError("invalid local MuSig2 signing attempt")
        if len(remote_partial_signature) != 32:
            raise ValueError("remote MuSig2 partial signature must be exactly 32 bytes")
        self._ensure()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.FinalizeParent(
            escrow.FinalizeParentRequest(
                channel_point=self._rpc_channel_point(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
                attempt_id=attempt.attempt_id,
                remote_partial_signature=remote_partial_signature,
            )
        )
        tx_hex = bytes(response.raw_tx).hex()
        txid = self._rpc_txid(response.txid)
        if get_txid(tx_hex) != txid:
            raise RuntimeError("patched LND returned inconsistent finalized parent bytes")
        return LndFinalizedEscrowParent(tx_hex=tx_hex, txid=txid)

    async def channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus:
        """Return the durable LND stage for a known local escrow session."""
        self._ensure()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.Status(
            escrow.StatusRequest(
                channel_point=self._rpc_channel_point(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
            )
        )
        stage = LndEscrowStage(response.stage)
        parent_txid = self._rpc_txid(response.parent_txid) if response.parent_txid else None
        expects_parent = stage >= LndEscrowStage.PARENT_PREPARED
        if (parent_txid is not None) != expects_parent:
            raise RuntimeError("patched LND returned inconsistent escrow parent status")
        if bool(response.finalized) != (stage is LndEscrowStage.FINALIZED):
            raise RuntimeError("patched LND returned inconsistent finalized status")
        return LndEscrowStatus(
            stage=stage,
            parent_txid=parent_txid,
            finalized=response.finalized,
        )

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool:
        """Cancel before signature release and report whether the live link resumed."""
        self._ensure()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.Cancel(
            escrow.CancelRequest(
                channel_point=self._rpc_channel_point(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
            )
        )
        return bool(response.link_resumed)

    async def open_channel_external_funding(
        self,
        node_pubkey: str,
        address: str,
        channel_amount_sats: int,
        push_to_counterparty_msat: int | None = None,
    ) -> str:
        """Start a SIMPLE_TAPROOT PSBT-funded channel open; return a user_channel_id.

        ``address`` is the peer's ``host:port`` (the channel counterparty). The returned
        id keys the in-flight open for :meth:`get_funding_output` /
        :meth:`provide_funding_outpoint`. ``push_to_counterparty_msat`` allocates the
        peer's contribution as its initial remote balance.
        """
        self._ensure()
        from jmswap.lndrpc import lightning_pb2 as ln

        push_msat = push_to_counterparty_msat or 0
        if push_msat < 0 or push_msat % 1000:
            raise ValueError("LND channel push must be a non-negative whole-satoshi amount")
        push_sat = push_msat // 1000
        if push_sat >= channel_amount_sats:
            raise ValueError("LND channel push must be less than the channel capacity")
        await self.connect_peer(node_pubkey, address)
        pending = secrets.token_bytes(32)
        req = ln.OpenChannelRequest(
            node_pubkey=bytes.fromhex(node_pubkey),
            local_funding_amount=channel_amount_sats,
            push_sat=push_sat,
            commitment_type=ln.CommitmentType.SIMPLE_TAPROOT,
            private=True,  # LND requires taproot channels to be unannounced
            funding_shim=ln.FundingShim(
                psbt_shim=ln.PsbtShim(pending_chan_id=pending, no_publish=True)
            ),
        )
        stream = self._stub.OpenChannel(req)
        update = await stream.read()
        if update.WhichOneof("update") != "psbt_fund":
            raise RuntimeError(f"expected psbt_fund update, got {update.WhichOneof('update')}")
        ucid = pending.hex()
        self._pending[ucid] = _PendingOpen(
            pending_chan_id=pending,
            stream=stream,
            funding_address=update.psbt_fund.funding_address,
            funding_amount=update.psbt_fund.funding_amount,
        )
        logger.info(
            f"LND taproot channel open started: {channel_amount_sats} sats to "
            f"{update.psbt_fund.funding_address} (ucid={ucid[:12]})"
        )
        return ucid

    async def get_funding_output(self, user_channel_id: str) -> tuple[bool, str, int]:
        """Return ``(ready, funding_scriptpubkey_hex, value)`` for the in-flight open."""
        p = self._pending.get(user_channel_id)
        if p is None or not p.funding_address:
            return (False, "", 0)
        spk = address_to_scriptpubkey(p.funding_address)
        return (True, spk.hex(), p.funding_amount)

    async def provide_funding_outpoint(
        self,
        user_channel_id: str,
        funding_txid: str,
        funding_vout: int,
        raw_tx_hex: str | None = None,
        input_utxos: list[tuple[int, bytes]] | None = None,
    ) -> None:
        """Pin the channel point: PsbtVerify (unsigned tx) then PsbtFinalize (final tx).

        ``raw_tx_hex`` is the assembled CoinJoin (the unsigned form is enough; witnesses
        are stripped so the txid matches) and ``input_utxos`` the per-input
        ``(value, scriptpubkey)`` of every spent output (LND checks the input sum). With
        ``skip_finalize`` LND derives the channel outpoint from the verified PSBT and
        waits for it to confirm on chain; the taker broadcasts the CoinJoin, so no
        signed transaction is handed back to LND.
        """
        if raw_tx_hex is None or input_utxos is None:
            raise ValueError(
                "LND PSBT funding requires the assembled tx (raw_tx_hex) and input_utxos"
            )
        self._ensure()
        from jmswap.lndrpc import lightning_pb2 as ln

        p = self._pending[user_channel_id]
        psbt = unsigned_psbt_from_tx(raw_tx_hex, input_utxos)
        await self._stub.FundingStateStep(
            ln.FundingTransitionMsg(
                psbt_verify=ln.FundingPsbtVerify(
                    funded_psbt=psbt, pending_chan_id=p.pending_chan_id, skip_finalize=True
                )
            )
        )
        p.verified = True
        logger.info(
            f"LND channel point pinned at {funding_txid}:{funding_vout} "
            f"(ucid={user_channel_id[:12]}); awaiting on-chain confirmation"
        )

    async def cancel_external_channel_funding(self, user_channel_id: str) -> None:
        """Cancel an in-flight PSBT funding shim after the CoinJoin is rejected."""
        pending = self._pending.get(user_channel_id)
        if pending is None:
            return
        self._ensure()
        from jmswap.lndrpc import lightning_pb2 as ln

        await self._stub.FundingStateStep(
            ln.FundingTransitionMsg(
                shim_cancel=ln.FundingShimCancel(pending_chan_id=pending.pending_chan_id)
            )
        )
        del self._pending[user_channel_id]
        logger.info(f"Cancelled LND channel funding shim (ucid={user_channel_id[:12]})")
