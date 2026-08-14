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
from dataclasses import dataclass, field
from typing import Any, ClassVar

from jmcore.bitcoin import (
    TxOutput,
    address_to_scriptpubkey,
    encode_varint,
    parse_transaction,
    serialize_output,
    serialize_transaction,
)
from loguru import logger

# BIP-174 per-input witness UTXO key type.
_PSBT_IN_WITNESS_UTXO = 0x01

# LND ships ECDSA TLS certificates; gRPC must be told to allow that cipher suite
# before any channel is created, or the TLS handshake fails. Set it at import time.
os.environ.setdefault("GRPC_SSL_CIPHER_SUITES", "HIGH+ECDSA")

_PSBT_MAGIC = b"\x70\x73\x62\x74\xff"


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


@dataclass
class LndBackend:
    """Minimal LND gRPC client for taproot channel-as-change via PSBT funding."""

    requires_pre_signing_funding_completion: ClassVar[bool] = True

    host: str  # 'host:port' of the LND gRPC listener
    tls_cert: bytes
    macaroon_hex: str
    network: str = "signet"
    _channel: Any = field(default=None, repr=False)
    _stub: Any = field(default=None, repr=False)
    _pending: dict[str, _PendingOpen] = field(default_factory=dict, repr=False)

    @classmethod
    def from_paths(
        cls, host: str, tls_cert_path: str, macaroon_path: str, network: str = "signet"
    ) -> LndBackend:
        with open(tls_cert_path, "rb") as f:
            cert = f.read()
        with open(macaroon_path, "rb") as f:
            mac = f.read().hex()
        return cls(host=host, tls_cert=cert, macaroon_hex=mac, network=network)

    def _ensure(self) -> None:
        if self._stub is not None:
            return
        import grpc

        from jmswap.lndrpc import lightning_pb2_grpc as lng

        ssl = grpc.ssl_channel_credentials(self.tls_cert)
        mac = self.macaroon_hex

        def _add_macaroon(_ctx: Any, cb: Any) -> None:
            cb((("macaroon", mac),), None)

        creds = grpc.composite_channel_credentials(
            ssl, grpc.metadata_call_credentials(_add_macaroon)
        )
        self._channel = grpc.aio.secure_channel(self.host, creds)
        self._stub = lng.LightningStub(self._channel)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

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
