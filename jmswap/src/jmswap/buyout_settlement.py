"""Durable invoice, payment, cooperative sweep, and unilateral escrow recovery.

Polling never treats missing metadata as permission to pay. Only sessions with
an affirmative settlement authorization participate. Every external side effect
has a durable intent first; a lost payment reply is tracked, never paid again.
The presigned split and payment preimage survive chain reorganizations.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, cast

from bitcointx.core.key import CKey
from jmcore.bitcoin import get_txid, parse_transaction_bytes, serialize_transaction
from jmcore.musig2 import nonce_agg, sign_partial
from jmcore.taproot import TaprootTx, TxIn, TxOut, taproot_sighash

from jmswap.bitcoin_escrow import (
    UnsignedKeyPathSpend,
    build_claim,
    build_cooperative_sweep,
    escrow_nonce,
    escrow_session,
    finalize_key_path_spend,
    verify_signed_split,
)
from jmswap.buyout_chain import BuyoutChain, ChainTransaction
from jmswap.buyout_messages import (
    BuyoutInvoice,
    BuyoutMessage,
    BuyoutParent,
    BuyoutStatus,
    BuyoutSweep,
    BuyoutSweepPartial,
    accept_hash,
    encode_buyout_payload,
    object_hash,
)
from jmswap.buyout_signing import Request, _save, require_runtime_binding, session_terms
from jmswap.buyout_store import BuyoutStore, StoredSession
from jmswap.buyout_terms import (
    SPLIT_SAFETY_MARGIN_BLOCKS,
    BuyoutTerms,
    ProtocolError,
    ValidatedParent,
    validate_parent,
)
from jmswap.lnd_peer import InvoiceState, LndPeerClient, PaymentResult, PaymentStatus


@dataclass(frozen=True)
class SettlementPolicy:
    payment_fee_limit_sat: int = 1_000
    payment_timeout_seconds: int = 60
    invoice_expiry_seconds: int = 3_600
    max_chain_fee_sat: int = 10_000
    bump_after_blocks: int = 1

    def __post_init__(self) -> None:
        values = (
            self.payment_timeout_seconds,
            self.invoice_expiry_seconds,
            self.max_chain_fee_sat,
            self.bump_after_blocks,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("settlement bounds must be positive integers")
        if type(self.payment_fee_limit_sat) is not int or self.payment_fee_limit_sat < 0:
            raise ValueError("payment fee limit must be a non-negative integer")


def _parent(record: StoredSession, height: int) -> tuple[BuyoutTerms, ValidatedParent]:
    terms = session_terms(record)
    message = BuyoutParent.model_validate(record.data["parent"])
    parent = validate_parent(
        terms,
        bytes.fromhex(message.unsigned_parent_tx),
        message.prevouts,
        message.channel_input_indices,
        message.escrow_output_index,
        height,
    )
    return terms, parent


def _status(record: StoredSession, terms: BuyoutTerms, parent: ValidatedParent) -> BuyoutStatus:
    return BuyoutStatus(
        v=1,
        type="buyout_status",
        epoch_id=record.session_id,
        attempt=terms.proposal.attempt,
        accept_hash=accept_hash(terms.acceptance),
        stage="parent_signed",
        parent_hash=parent.parent_hash,
        txid=parent.txid,
    )


def _unsigned(raw: bytes) -> bytes:
    parsed = parse_transaction_bytes(raw)
    return serialize_transaction(parsed.version, parsed.inputs, parsed.outputs, parsed.locktime)


class BuyoutSettlement:
    def __init__(
        self,
        store: BuyoutStore,
        peer: LndPeerClient,
        chain: BuyoutChain,
        policy: SettlementPolicy | None = None,
        request: Request | None = None,
        *,
        runtime_binding: dict[str, str | int] | None = None,
    ) -> None:
        self.runtime_binding = dict(runtime_binding or {})
        self.store, self.peer, self.chain, self.policy, self.request = (
            store,
            peer,
            chain,
            policy or SettlementPolicy(),
            request,
        )

    async def _observe(self, parent: ValidatedParent) -> ChainTransaction | None:
        observed = await self.chain.transaction(parent.txid)
        if observed is not None and _unsigned(observed.raw) != parent.raw:
            raise ProtocolError("observed parent differs from the authorized transaction")
        return observed

    async def handle(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        with self.store.exclusive_operation():
            record = self.store.get(message.epoch_id)
            require_runtime_binding(record, self.runtime_binding)
            terms, parent = _parent(record, await self.chain.height())
            if (
                record.peer_pubkey != peer
                or record.role != "counterparty"
                or not record.parent_signing_started
                or message.attempt != terms.proposal.attempt
                or getattr(message, "accept_hash", None) != accept_hash(terms.acceptance)
            ):
                raise ProtocolError("settlement request is not bound to the authenticated session")
            if record.data.get("settlement_authorized") is not True:
                raise ProtocolError("settlement was not authorized for this session")
            if isinstance(message, BuyoutStatus):
                if message.txid != parent.txid or message.parent_hash != parent.parent_hash:
                    raise ProtocolError("settlement status names another parent")
                return await self._invoice(record, terms, parent)
            if isinstance(message, BuyoutSweep):
                return await self._sweep_partial(record, terms, parent, message)
            raise ProtocolError("message is not a settlement request")

    async def _invoice(
        self, record: StoredSession, terms: BuyoutTerms, parent: ValidatedParent
    ) -> BuyoutMessage:
        observed = await self._observe(parent)
        if (
            observed is None
            or observed.confirmations < terms.acceptance.settlement_depth
            or observed.height is None
            or await self.chain.height() + terms.proposal.cltv_limit
            > observed.height + terms.proposal.csv_delay - SPLIT_SAFETY_MARGIN_BLOCKS
            or not await self.chain.unspent(parent.escrow_outpoint)
        ):
            return _status(record, terms, parent)
        invoice = record.data.get("invoice")
        if invoice is None:
            if record.data.get("invoice_started") is True:
                invoice = await self.peer.invoice_request(
                    bytes.fromhex(terms.acceptance.payment_hash)
                )
                if invoice is None:
                    return _status(record, terms, parent)
            else:
                _save(self.store, record.session_id, "INVOICE_CREATING", invoice_started=True)
                # LND hints only active private channels whose peer is in the
                # public graph, so peers with only private channels stay hidden.
                # A direct peer of the buyer needs no hint.
                invoice = await self.peer.create_invoice(
                    bytes.fromhex(cast(str, record.data["preimage"])),
                    terms.claim_sat,
                    self.policy.invoice_expiry_seconds,
                    include_private_routes=True,
                )
            _save(self.store, record.session_id, "INVOICE_READY", invoice=invoice)
        return BuyoutInvoice(
            v=1,
            type="buyout_invoice",
            epoch_id=record.session_id,
            attempt=terms.proposal.attempt,
            accept_hash=accept_hash(terms.acceptance),
            txid=parent.txid,
            bolt11=cast(str, invoice),
        )

    async def _sweep_partial(
        self,
        record: StoredSession,
        terms: BuyoutTerms,
        parent: ValidatedParent,
        message: BuyoutSweep,
    ) -> BuyoutSweepPartial:
        invoice = await self.peer.invoice_status(bytes.fromhex(terms.acceptance.payment_hash))
        if invoice.state is not InvoiceState.SETTLED or invoice.amount_paid_sat < terms.claim_sat:
            raise ProtocolError("escrow invoice has not been paid in full")
        if await self._observe(parent) is None:
            raise ProtocolError("settled parent is not in the current chain view")
        digest = object_hash("sweep", message)
        saved = cast(dict[str, Any], record.data).get("sweep_replies", {}).get(digest)
        if saved is not None:
            return BuyoutSweepPartial.model_validate(saved)
        parsed = parse_transaction_bytes(bytes.fromhex(message.unsigned_sweep_tx))
        point = parent.escrow_outpoint
        if (
            parsed.version != 2
            or parsed.locktime != 0
            or len(parsed.inputs) != 1
            or parsed.inputs[0].txid != point.txid
            or parsed.inputs[0].vout != point.vout
            or parsed.inputs[0].sequence != 0xFFFFFFFD
            or parsed.inputs[0].scriptsig
            or any(parsed.witnesses)
            or not parsed.outputs
            or any(
                len(output.script) != 34 or output.script[:2] != b"\x51\x20" or output.value <= 0
                for output in parsed.outputs
            )
            or sum(output.value for output in parsed.outputs) >= point.value
        ):
            raise ProtocolError(
                "cooperative sweep is not an RBF spend to buyer-chosen Taproot outputs"
            )
        if _unsigned(bytes.fromhex(message.unsigned_sweep_tx)).hex() != message.unsigned_sweep_tx:
            raise ProtocolError("cooperative sweep must be unsigned and canonical")
        tx = TaprootTx(
            inputs=[TxIn(parsed.inputs[0].txid, point.vout, parsed.inputs[0].sequence)],
            outputs=[TxOut(output.value, output.script) for output in parsed.outputs],
        )
        sighash = taproot_sighash(tx, 0, [point.value], [point.scriptpubkey])
        secret = bytes.fromhex(cast(str, record.data["escrow_secret"]))
        nonce, public = escrow_nonce(bytes(CKey(secret).pub), privkey=secret, sighash=sighash)
        context = escrow_session(
            terms.escrow, nonce_agg([bytes.fromhex(message.sweep_nonce_B), public]), sighash
        )
        partial = sign_partial(nonce, secret, context)
        response = BuyoutSweepPartial(
            v=1,
            type="buyout_sweep_partial",
            epoch_id=record.session_id,
            attempt=message.attempt,
            accept_hash=message.accept_hash,
            sweep_hash=digest,
            sweep_nonce_C=public.hex(),
            sweep_partial_C=partial.hex(),
        )
        replies = dict(cast(dict[str, Any], record.data).get("sweep_replies", {}))
        if len(replies) >= 64:
            raise ProtocolError("cooperative sweep attempt limit reached")
        replies[digest] = response.model_dump()
        _save(self.store, record.session_id, "SETTLED", sweep_replies=replies)
        return response

    async def poll(self, session_id: str) -> str:
        """Advance one explicitly authorized session; never start a second payment."""
        with self.store.exclusive_operation():
            record = self.store.get(session_id)
            require_runtime_binding(record, self.runtime_binding)
            if (
                record.data.get("settlement_authorized") is not True
                or not record.parent_signing_started
                or not record.data.get("split_tx")
            ):
                return record.state
            height, _ = await self.chain.tip()
            terms, parent = _parent(record, height)
            observed = await self._observe(parent)
            if observed is None:
                if record.role == "buyer" and record.data.get("payment_started"):
                    raw_parent = record.data.get("finalized_parent")
                    if not isinstance(raw_parent, str):
                        _save(self.store, session_id, "PARENT_RECOVERY_REQUIRED")
                        return "PARENT_RECOVERY_REQUIRED"
                    raw = bytes.fromhex(raw_parent)
                    if _unsigned(raw) != parent.raw:
                        raise ProtocolError(
                            "recovery parent differs from the authorized transaction"
                        )
                    _save(self.store, session_id, "PARENT_REBROADCAST")
                    await self.chain.broadcast(raw)
                    return "PARENT_REBROADCAST"
                _save(self.store, session_id, "PARENT_MISSING")
                return "PARENT_MISSING"
            if await self._completed(record, terms, parent, observed):
                return self.store.get(session_id).state
            # An earlier poll may have recorded PARENT_MISSING before broadcast.
            # A verified parent is affirmative evidence, even if the invoice or
            # payment depth has not yet been reached. This is a last-observed
            # status, never authorization to pay. Do not downgrade a session
            # that has progressed to invoice, payment, spend, or recovery.
            record = self.store.get(session_id)
            if record.state in {"PARENT_SIGNED", "PARENT_MISSING"} and not any(
                record.data.get(key)
                for key in (
                    "invoice_started",
                    "invoice",
                    "payment_started",
                    "settlement_preimage",
                    "spends",
                    "force_close_requested",
                )
            ):
                _save(self.store, session_id, "PARENT_OBSERVED")
                record = self.store.get(session_id)
            if record.role == "counterparty":
                await self._counterparty_poll(record, terms, parent, observed, height)
            else:
                await self._buyer_poll(record, terms, parent, observed, height)
            return self.store.get(session_id).state

    async def _completed(
        self,
        record: StoredSession,
        terms: BuyoutTerms,
        parent: ValidatedParent,
        observed: ChainTransaction,
    ) -> bool:
        for spend in cast(dict[str, Any], record.data).get("spends", []):
            transaction = await self.chain.transaction(get_txid(spend["raw"]))
            if (
                transaction is not None
                and transaction.confirmations >= terms.proposal.buyer_settlement_depth
            ):
                _save(self.store, record.session_id, "COMPLETED", completed_txid=transaction.txid)
                return True
        # With the exact parent confirmed, absence from the *confirmed* UTXO set
        # proves the escrow was spent on chain, even when the peer used its own
        # fee or destination. Wait a full finality interval from that observation.
        if observed.height is not None and not await self.chain.unspent(
            parent.escrow_outpoint, include_mempool=False
        ):
            tip = await self.chain.tip()
            evidence = cast(dict[str, Any], record.data).get("spent_observation")
            if (
                evidence is None
                or tip[0] < evidence["height"]
                or await self.chain.block_hash(evidence["height"]) != evidence["hash"]
            ):
                evidence = {"height": tip[0], "hash": tip[1]}
            if tip != await self.chain.tip():
                return True
            complete = tip[0] - evidence["height"] + 1 >= terms.proposal.buyer_settlement_depth
            _save(
                self.store,
                record.session_id,
                "COMPLETED" if complete else "SPEND_CONFIRMED",
                spent_observation=evidence,
            )
            return True
        if record.data.get("spent_observation") is not None:
            _save(self.store, record.session_id, "PARENT_CONFIRMED", spent_observation=None)
        return False

    async def _counterparty_poll(
        self,
        record: StoredSession,
        terms: BuyoutTerms,
        parent: ValidatedParent,
        observed: ChainTransaction,
        height: int,
    ) -> None:
        if (
            not record.data.get("invoice")
            and observed.confirmations >= terms.acceptance.settlement_depth
        ):
            response = await self._invoice(record, terms, parent)
            if isinstance(response, BuyoutInvoice):
                await self.peer.send(
                    bytes.fromhex(record.peer_pubkey), encode_buyout_payload(response)
                )
            record = self.store.get(record.session_id)
        if observed.height is None or height + 1 < observed.height + terms.proposal.csv_delay:
            return
        if record.data.get("invoice_started"):
            invoice = await self.peer.invoice_status(bytes.fromhex(terms.acceptance.payment_hash))
            if invoice.state is InvoiceState.SETTLED:
                return
        if not await self.chain.unspent(parent.escrow_outpoint):
            return
        await self._split(record, terms, parent, height)

    async def _split(
        self,
        record: StoredSession,
        terms: BuyoutTerms,
        parent: ValidatedParent,
        height: int,
    ) -> None:
        raw = bytes.fromhex(cast(str, record.data["split_tx"]))
        verify_signed_split(
            terms.escrow,
            parent.escrow_outpoint,
            bytes.fromhex(terms.proposal.split_script_B),
            bytes.fromhex(terms.acceptance.split_script_C),
            terms.counterparty_split_sat,
            terms.proposal.split_fee,
            terms.proposal.csv_delay,
            raw,
        )
        await self._broadcast(record, raw, terms.proposal.split_fee, "split", height)

    async def _buyer_poll(
        self,
        record: StoredSession,
        terms: BuyoutTerms,
        parent: ValidatedParent,
        observed: ChainTransaction,
        height: int,
    ) -> None:
        if record.data.get("payment_started") and not record.data.get("settlement_preimage"):
            await self._track_payment(record, terms, height)
            record = self.store.get(record.session_id)
        if record.data.get("settlement_preimage"):
            await self._spend_paid(record, terms, parent, observed, height)
            return
        if (
            record.data.get("recovery_authorized") is True
            and (
                not record.data.get("payment_started") or record.data.get("payment_failed") is True
            )
            and observed.height is not None
            and height + 1 >= observed.height + terms.proposal.csv_delay
            and await self.chain.unspent(parent.escrow_outpoint)
        ):
            await self._split(record, terms, parent, height)
            return
        if record.data.get("payment_started"):
            return
        if self.request is None or observed.height is None:
            return
        if observed.confirmations < terms.proposal.buyer_settlement_depth:
            return
        if (
            height + terms.proposal.cltv_limit
            > observed.height + terms.proposal.csv_delay - SPLIT_SAFETY_MARGIN_BLOCKS
        ):
            return
        if not await self.chain.unspent(parent.escrow_outpoint):
            return
        response = await self.request(record.peer_pubkey, _status(record, terms, parent))
        if not isinstance(response, BuyoutInvoice):
            return
        if (
            response.epoch_id != record.session_id
            or response.attempt != terms.proposal.attempt
            or response.accept_hash != accept_hash(terms.acceptance)
            or response.txid != parent.txid
        ):
            raise ProtocolError("invoice is for a different buyout")
        invoice = await self.peer.inspect_invoice(response.bolt11)
        node = await self.peer.node_info()
        if not node.synced_to_chain or node.network != terms.proposal.network:
            raise ProtocolError("payment node is not synchronized to the agreed network")
        now = time.time()
        if (
            invoice.payment_hash.hex() != terms.acceptance.payment_hash
            or invoice.destination.hex() != record.peer_pubkey
            or invoice.amount_sat != terms.claim_sat
            or invoice.created_at > now + 60
            or invoice.min_final_cltv > terms.proposal.cltv_limit
            or invoice.created_at + invoice.expiry_seconds
            <= now + self.policy.payment_timeout_seconds
        ):
            raise ProtocolError("invoice does not meet the agreed settlement bounds")
        # Recheck the active chain after peer and invoice RPCs, immediately before
        # recording payment intent. No node API can eliminate a later deep reorg.
        before = await self.chain.tip()
        current = await self._observe(parent)
        unspent = await self.chain.unspent(parent.escrow_outpoint)
        if (
            current is None
            or current.height is None
            or not unspent
            or current.confirmations < terms.proposal.buyer_settlement_depth
            or before != await self.chain.tip()
            or before[0] + terms.proposal.cltv_limit
            > current.height + terms.proposal.csv_delay - SPLIT_SAFETY_MARGIN_BLOCKS
        ):
            return
        _save(
            self.store,
            record.session_id,
            "PAYMENT_STARTED",
            payment_started=True,
            invoice=response.bolt11,
            payment_parent_block=current.block_hash,
            finalized_parent=current.raw.hex(),
        )
        result = await self.peer.pay(
            response.bolt11,
            fee_limit_sat=self.policy.payment_fee_limit_sat,
            cltv_limit=terms.proposal.cltv_limit,
            timeout_seconds=self.policy.payment_timeout_seconds,
        )
        self._payment_result(record.session_id, terms, result, before[0])

    def _payment_result(
        self, session_id: str, terms: BuyoutTerms, result: PaymentResult, height: int
    ) -> None:
        if result.payment_hash.hex() != terms.acceptance.payment_hash:
            raise ProtocolError("payment result is for a different payment hash")
        if result.status is PaymentStatus.SUCCEEDED:
            if (
                result.preimage is None
                or hashlib.sha256(result.preimage).hexdigest() != terms.acceptance.payment_hash
            ):
                raise ProtocolError("settled payment has no valid preimage")
            _save(
                self.store,
                session_id,
                "SETTLED",
                settlement_preimage=result.preimage.hex(),
                settled_height=height,
                payment_fee_sat=result.fee_sat,
            )
        elif result.status is PaymentStatus.FAILED:
            _save(self.store, session_id, "PAYMENT_FAILED", payment_failed=True)

    async def _track_payment(self, record: StoredSession, terms: BuyoutTerms, height: int) -> None:
        result = await self.peer.track_payment(bytes.fromhex(terms.acceptance.payment_hash))
        if result is not None:
            self._payment_result(record.session_id, terms, result, height)

    async def _broadcast(
        self, record: StoredSession, raw: bytes, fee: int, kind: str, height: int
    ) -> None:
        spends = list(
            cast(dict[str, Any], self.store.get(record.session_id).data).get("spends", [])
        )
        if not any(item["raw"] == raw.hex() for item in spends):
            if len(spends) >= 64:
                raise ProtocolError("escrow spend attempt limit reached")
            spends.append({"raw": raw.hex(), "fee": fee, "kind": kind, "height": height})
        _save(self.store, record.session_id, "SPEND_BROADCASTING", spends=spends)
        await self.chain.broadcast(raw)
        _save(self.store, record.session_id, "SPEND_BROADCAST")

    async def _cooperative(
        self, record: StoredSession, terms: BuyoutTerms, spend: UnsignedKeyPathSpend
    ) -> bytes | None:
        if self.request is None or record.data.get("cooperative_started"):
            return None
        secret = bytes.fromhex(cast(str, record.data["escrow_secret"]))
        nonce, public = escrow_nonce(bytes(CKey(secret).pub), privkey=secret, sighash=spend.sighash)
        message = BuyoutSweep(
            v=1,
            type="buyout_sweep",
            epoch_id=record.session_id,
            attempt=terms.proposal.attempt,
            accept_hash=accept_hash(terms.acceptance),
            unsigned_sweep_tx=spend.tx._serialize_no_witness().hex(),
            sweep_nonce_B=public.hex(),
        )
        _save(self.store, record.session_id, "SWEEP_REQUESTED", cooperative_started=True)
        try:
            response = await self.request(record.peer_pubkey, message)
            if (
                not isinstance(response, BuyoutSweepPartial)
                or response.epoch_id != record.session_id
                or response.attempt != terms.proposal.attempt
                or response.accept_hash != message.accept_hash
                or response.sweep_hash != object_hash("sweep", message)
            ):
                raise ProtocolError("cooperative sweep response does not match")
            pub_c = bytes.fromhex(response.sweep_nonce_C)
            context = escrow_session(terms.escrow, nonce_agg([public, pub_c]), spend.sighash)
            partial = sign_partial(nonce, secret, context)
            return finalize_key_path_spend(
                terms.escrow, spend, public, pub_c, partial, bytes.fromhex(response.sweep_partial_C)
            )
        except Exception:
            # The preimage path remains available. Never recreate a lost nonce.
            return None

    async def _spend_paid(
        self,
        record: StoredSession,
        terms: BuyoutTerms,
        parent: ValidatedParent,
        observed: ChainTransaction,
        height: int,
    ) -> None:
        data = cast(dict[str, Any], record.data)
        spends = data.get("spends", [])
        rate, incremental = await self.chain.fee_rates()
        deadline = (
            observed.height + terms.proposal.csv_delay - SPLIT_SAFETY_MARGIN_BLOCKS
            if observed.height is not None
            else height
        )
        latest = spends[-1] if spends else None
        if latest is not None:
            transaction = await self.chain.transaction(get_txid(latest["raw"]))
            if transaction is not None and transaction.confirmations > 0:
                return
            if height < latest["height"] + self.policy.bump_after_blocks:
                if transaction is None:
                    # Replay an affirmative intent until replacement is due. Once
                    # due, a rejected replay must not prevent a higher-fee claim.
                    await self.chain.broadcast(bytes.fromhex(latest["raw"]))
                return
            if transaction is not None and latest["kind"] == "cooperative" and height < deadline:
                return
        elif not await self.chain.unspent(parent.escrow_outpoint):
            return
        destination = bytes.fromhex(terms.proposal.split_script_B)
        if latest is None and not data.get("cooperative_started") and height < deadline:
            fee = rate * 111
            if fee > self.policy.max_chain_fee_sat:
                raise ProtocolError("cooperative sweep exceeds the operator fee budget")
            spend = build_cooperative_sweep(terms.escrow, parent.escrow_outpoint, destination, fee)
            cooperative = await self._cooperative(record, terms, spend)
            if cooperative is not None:
                await self._broadcast(record, cooperative, fee, "cooperative", height)
                return
            record = self.store.get(record.session_id)
        fee = rate * 146
        if latest is not None:
            fee = max(fee, latest["fee"] + incremental * 146)
        if fee > self.policy.max_chain_fee_sat:
            raise ProtocolError("claim sweep exceeds the operator fee budget")
        claim = build_claim(
            terms.escrow,
            parent.escrow_outpoint,
            bytes.fromhex(cast(str, record.data["settlement_preimage"])),
            bytes.fromhex(cast(str, record.data["claim_secret"])),
            destination,
            fee,
        )
        await self._broadcast(record, claim, fee, "claim", height)
