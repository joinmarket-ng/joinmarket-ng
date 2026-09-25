"""Deadline recovery for explicitly authorized, runtime-bound sessions.

Unsigned locks can be released at the agreed TTL. Signed funding requires an
affirmative force-close authorization and must never be unlocked as if no
signature existed. An uncertain force-close RPC is left for explicit retry.
"""

from __future__ import annotations

from typing import Any, cast

from jmswap.bitcoin_escrow import EscrowOutpoint
from jmswap.buyout_chain import BuyoutChain
from jmswap.buyout_messages import BuyoutParent, BuyoutPropose
from jmswap.buyout_signing import _save, require_runtime_binding, session_terms
from jmswap.buyout_store import BuyoutStore, StoredSession
from jmswap.buyout_terms import BuyoutTerms, ProtocolError, validate_parent
from jmswap.lnd_escrow import LndEscrowClient
from jmswap.lnd_peer import InvoiceState, LndPeerClient


def _proposal(record: StoredSession) -> BuyoutPropose:
    proposal = BuyoutPropose.model_validate(record.data["proposal"])
    points = tuple(f"{point.txid}:{point.vout}" for point in proposal.channel_points)
    if proposal.epoch_id != record.session_id or points != record.channel_points:
        raise ProtocolError("recovery proposal does not match the journal reservation")
    return proposal


class BuyoutRecovery:
    def __init__(
        self,
        store: BuyoutStore,
        escrow: LndEscrowClient,
        peer: LndPeerClient,
        chain: BuyoutChain,
        *,
        runtime_binding: dict[str, str | int],
    ) -> None:
        if not runtime_binding:
            raise ValueError("deadline recovery requires an explicit runtime binding")
        self.store, self.escrow, self.peer, self.chain = store, escrow, peer, chain
        self.binding = dict(runtime_binding)

    async def poll(self, session_id: str) -> bool:
        """Return true when deadline recovery owns this polling iteration."""
        with self.store.exclusive_operation():
            record = self.store.get(session_id)
            require_runtime_binding(record, self.binding)
            if record.state == "CANCELED" or record.data.get("recovery_authorized") is not True:
                return False
            height = await self.chain.height()
            proposal = _proposal(record)
            if (
                record.parent_signing_started
                and record.data.get("force_close_authorized") is not True
            ):
                return False
            if record.data.get("force_close_requested") is True and record.parent_signing_started:
                return await self._force_close(
                    record, session_terms(record), height, retry_uncertain=False
                )
            freeze_height = record.data.get("freeze_height")
            if type(freeze_height) is not int or freeze_height < 0:
                return False
            if not record.parent_signing_started:
                if height >= freeze_height + proposal.freeze_ttl_blocks:
                    await self._cancel_unsigned(record, proposal)
                    return True
                return False
            terms = session_terms(record)
            if height < freeze_height + terms.acceptance.max_freeze_blocks:
                return False
            return await self._force_close(record, terms, height, retry_uncertain=False)

    async def force_close(self, session_id: str) -> None:
        """Explicit operator action, including retry of an uncertain close RPC."""
        with self.store.exclusive_operation():
            record = self.store.get(session_id)
            require_runtime_binding(record, self.binding)
            _proposal(record)
            if not record.parent_signing_started:
                raise ProtocolError("unsigned sessions must be canceled instead of force-closed")
            if not await self._force_close(
                record,
                session_terms(record),
                await self.chain.height(),
                retry_uncertain=True,
            ):
                raise ProtocolError(
                    "force close is unsafe for this session's chain or payment state"
                )

    async def _cancel_unsigned(self, record: StoredSession, proposal: BuyoutPropose) -> None:
        # The journal marker and native backend must both permit cancellation.
        # Exact-session cancellation receipts make lost replies safe to retry.
        _save(self.store, record.session_id, "DEADLINE_CANCELING", deadline_cancel_started=True)
        for point in proposal.channel_points:
            await self.escrow.cancel(point, bytes.fromhex(record.session_id))
        _save(self.store, record.session_id, "CANCELED")

    async def _payment_allows_close(self, record: StoredSession, terms: BuyoutTerms) -> bool:
        if record.data.get("settlement_preimage"):
            return False
        if record.data.get("payment_started") and record.data.get("payment_failed") is not True:
            return False
        if record.data.get("invoice_started"):
            invoice = await self.peer.invoice_status(bytes.fromhex(terms.acceptance.payment_hash))
            # OPEN may still receive payment, ACCEPTED may settle, and unknown is
            # not evidence of nonpayment. Only terminal cancellation permits it.
            return invoice.state is InvoiceState.CANCELED
        return True

    async def _force_close(
        self,
        record: StoredSession,
        terms: BuyoutTerms,
        height: int,
        *,
        retry_uncertain: bool,
    ) -> bool:
        if not await self._payment_allows_close(record, terms):
            return False
        message = BuyoutParent.model_validate(record.data["parent"])
        parent = validate_parent(
            terms,
            bytes.fromhex(message.unsigned_parent_tx),
            message.prevouts,
            message.channel_input_indices,
            message.escrow_output_index,
            height,
        )
        before = await self.chain.tip()
        observed = await self.chain.transaction(parent.txid)
        if observed is not None and observed.confirmations > 0:
            return False
        unspent = []
        for channel in terms.channels:
            point = channel.point
            funding = await self.chain.transaction(point.txid)
            if funding is None or funding.height is None or funding.confirmations <= 0:
                return False
            outpoint = EscrowOutpoint(
                point.txid, point.vout, channel.capacity_sat, channel.funding_script
            )
            if await self.chain.unspent(outpoint, include_mempool=False):
                unspent.append(point)
        if before != await self.chain.tip():
            return False
        if not unspent:
            # The funding transactions are confirmed but their outputs are gone,
            # while the immutable parent is absent from the confirmed chain.
            # Continue monitoring this record so a reorg can revive its parent.
            _save(self.store, record.session_id, "CONFLICTED")
            return True
        attempts = list(cast(dict[str, Any], record.data).get("force_close_attempts", []))
        for point in unspent:
            key = f"{point.txid}:{point.vout}"
            prior = next((item for item in reversed(attempts) if item["point"] == key), None)
            if prior is not None and prior["status"] == "accepted":
                continue
            if prior is not None and not retry_uncertain:
                _save(self.store, record.session_id, "FORCE_CLOSE_RECOVERY_REQUIRED")
                return True
            if len(attempts) >= 64:
                raise ProtocolError("force-close attempt limit reached; recovery required")
            attempts.append({"point": key, "height": height, "status": "started"})
            _save(
                self.store,
                record.session_id,
                "FORCE_CLOSE_REQUESTED",
                force_close_attempts=attempts,
                force_close_requested=True,
                recovery_authorized=True,
                force_close_authorized=True,
            )
            await self.peer.close_channel(point, force=True)
            attempts[-1] = {**attempts[-1], "status": "accepted"}
            _save(
                self.store, record.session_id, "FORCE_CLOSE_PENDING", force_close_attempts=attempts
            )
        return True
