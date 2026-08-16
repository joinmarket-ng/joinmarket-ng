"""Bitcoin Core chain and recovery operations for channel-input escrow.

This is intentionally a narrow adapter.  It owns no Lightning channel state,
does not prepare or sign timeout transactions, and does not declare the full
``ChannelInputEscrowBackend`` capability set.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiohttp
from jmcore.bitcoin import calculate_tx_vsize, get_txid
from pydantic import BaseModel, ConfigDict, Field

from jmswap.bitcoin_escrow import (
    ChannelInputEscrowTree,
    build_success_spend,
    parse_escrow_transaction,
    verify_success_spend,
)
from jmswap.channel_escrow import (
    ChainObservationRequest,
    EscrowChainObservation,
    EscrowLifecycleState,
    EscrowOutpoint,
    EscrowParentBroadcast,
    EscrowSpendResult,
    EscrowTimeoutBroadcast,
    ParentBroadcastRequest,
    RecoveryBroadcastResult,
    RecoveryRequest,
    SuccessSpendRequest,
)
from jmswap.errors import ChainError, ChannelInputEscrowError
from jmswap.spend import SwapOutpoint

_MODEL_CONFIG = ConfigDict(
    extra="forbid", frozen=True, strict=True, ser_json_bytes="hex", val_json_bytes="hex"
)
_KNOWN_ALREADY_SUBMITTED_CODES = frozenset({-27})
_SAT_PER_BTC = Decimal(100_000_000)


@runtime_checkable
class BitcoinCoreRpc(Protocol):
    """The narrow JSON-RPC dependency used by this adapter."""

    async def call(self, method: str, params: list[Any]) -> Any:
        """Call one Bitcoin Core RPC method."""


class BitcoinCoreRpcError(ChainError):
    """A sanitized Bitcoin Core RPC failure retaining only its numeric code."""

    def __init__(self, code: int | None = None) -> None:
        self.code = code
        message = (
            "Bitcoin Core RPC request failed"
            if code is None
            else f"Bitcoin Core RPC error ({code})"
        )
        super().__init__(message)


class AiohttpBitcoinCoreRpc:
    """Lazy-session JSON-RPC client using HTTP basic authentication."""

    def __init__(
        self, url: str, username: str, password: str, *, timeout_seconds: int = 30
    ) -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("url must be an HTTP(S) URL")
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError("Bitcoin Core RPC credentials must be strings")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._url = url
        self._auth = aiohttp.BasicAuth(username, password)
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._request_id = 0

    async def call(self, method: str, params: list[Any]) -> Any:
        if not isinstance(method, str) or not method or not isinstance(params, list):
            raise ValueError("Bitcoin Core RPC method and parameters are invalid")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(auth=self._auth, timeout=self._timeout)
        self._request_id += 1
        try:
            async with self._session.post(
                self._url,
                json={"jsonrpc": "1.0", "id": self._request_id, "method": method, "params": params},
            ) as response:
                payload = await response.json(content_type=None)
        except BitcoinCoreRpcError:
            raise
        except (aiohttp.ClientError, ValueError, TypeError) as exc:
            raise BitcoinCoreRpcError() from exc
        if not isinstance(payload, dict):
            raise BitcoinCoreRpcError()
        error = payload.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            raise BitcoinCoreRpcError(code if isinstance(code, int) else None)
        if response.status != 200:
            raise BitcoinCoreRpcError()
        if "result" not in payload:
            raise BitcoinCoreRpcError()
        return payload["result"]

    async def close(self) -> None:
        """Close the lazily-created HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None


# A less surprising spelling for callers that do not care about aiohttp's casing.
BitcoinCoreJsonRpc = AiohttpBitcoinCoreRpc


@dataclass(frozen=True)
class SuccessSpendMaterial:
    """Maker-local key and destination material for one success sweep."""

    tree: ChannelInputEscrowTree
    maker_claim_private_key: bytes
    maker_output_script: bytes
    maker_output_is_controlled: bool

    def __post_init__(self) -> None:
        if not isinstance(self.tree, ChannelInputEscrowTree):
            raise TypeError("tree must be a ChannelInputEscrowTree")
        if (
            not isinstance(self.maker_claim_private_key, bytes)
            or len(self.maker_claim_private_key) != 32
        ):
            raise ValueError("maker claim private key must be exactly 32 bytes")
        if not isinstance(self.maker_output_script, bytes) or not self.maker_output_script:
            raise ValueError("maker output script must be non-empty")
        if self.maker_output_is_controlled is not True:
            raise ValueError("maker output script must be attested maker-controlled")


@runtime_checkable
class SuccessSpendMaterialResolver(Protocol):
    """Resolve private maker sweep material without putting keys in the journal."""

    async def resolve_success_material(self, session_id: str) -> SuccessSpendMaterial:
        """Return the material for the specified escrow session."""


class _SuccessPlan(BaseModel):
    model_config = _MODEL_CONFIG

    request: SuccessSpendRequest
    result: EscrowSpendResult
    complete: bool = False


class _TimeoutPlan(BaseModel):
    model_config = _MODEL_CONFIG

    request: RecoveryRequest
    result: EscrowTimeoutBroadcast
    complete: bool = False


class _FeeBumpPlan(BaseModel):
    model_config = _MODEL_CONFIG

    request: RecoveryRequest
    result: EscrowSpendResult
    complete: bool = False


class _RecoveryPlan(BaseModel):
    model_config = _MODEL_CONFIG

    request: RecoveryRequest
    result: RecoveryBroadcastResult
    transaction_hexes: tuple[str, ...] = Field(min_length=1)
    complete: bool = False


class _EscrowJournal(BaseModel):
    """Private journal.  It deliberately has no key-bearing fields."""

    model_config = _MODEL_CONFIG

    session_id: str = Field(min_length=1)
    frozen_outpoint: EscrowOutpoint
    monitor_start_height: int = Field(ge=0)
    parent_request: ParentBroadcastRequest | None = None
    parent_result: EscrowParentBroadcast | None = None
    parent_complete: bool = False
    last_observation: EscrowChainObservation | None = None
    success_plan: _SuccessPlan | None = None
    fee_bump_plans: tuple[_FeeBumpPlan, ...] = ()
    timeout_plan: _TimeoutPlan | None = None
    recovery_plans: tuple[_RecoveryPlan, ...] = ()


class BitcoinCoreEscrowChainAdapter:
    """Durable Bitcoin Core adapter for the escrow chain/recovery subset only."""

    def __init__(
        self,
        rpc: BitcoinCoreRpc,
        material_resolver: SuccessSpendMaterialResolver,
        journal_dir: str | Path,
        *,
        fee_target_blocks: int,
        minimum_feerate_sat_vb: int | float,
    ) -> None:
        if not isinstance(rpc, BitcoinCoreRpc):
            raise TypeError("rpc must provide async call")
        if not isinstance(material_resolver, SuccessSpendMaterialResolver):
            raise TypeError("material_resolver must resolve success material")
        if (
            isinstance(fee_target_blocks, bool)
            or not isinstance(fee_target_blocks, int)
            or fee_target_blocks <= 0
        ):
            raise ValueError("fee_target_blocks must be positive")
        if (
            isinstance(minimum_feerate_sat_vb, bool)
            or not isinstance(minimum_feerate_sat_vb, (int, float))
            or not math.isfinite(float(minimum_feerate_sat_vb))
            or minimum_feerate_sat_vb <= 0
        ):
            raise ValueError("minimum_feerate_sat_vb must be positive")
        self._rpc = rpc
        self._resolver = material_resolver
        self._fee_target_blocks = fee_target_blocks
        self._minimum_feerate_milli_sat_vb = self._ceil_decimal(
            Decimal(str(minimum_feerate_sat_vb)) * Decimal(1000)
        )
        self._journal_dir = Path(journal_dir)
        self._journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._journal_dir.is_dir():
            raise ValueError("journal_dir must be a directory")
        if stat.S_IMODE(self._journal_dir.stat().st_mode) & 0o077:
            raise ValueError("journal_dir must not be accessible by group or others")

    async def register_frozen_channel(self, session_id: str, outpoint: EscrowOutpoint) -> None:
        """Durably begin monitoring before a parent can be released."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be non-empty")
        if not isinstance(outpoint, EscrowOutpoint):
            raise TypeError("outpoint must be an EscrowOutpoint")
        descriptor = await self._acquire_lock(session_id)
        try:
            journal = self._load_journal(session_id)
            if journal is not None:
                if journal.frozen_outpoint != outpoint:
                    raise ChannelInputEscrowError("conflicting frozen channel registration")
                return
            height = await self._height()
            self._write_journal(
                _EscrowJournal(
                    session_id=session_id,
                    frozen_outpoint=outpoint,
                    monitor_start_height=height,
                )
            )
        finally:
            self._release_lock(descriptor)

    async def broadcast_parent(self, request: ParentBroadcastRequest) -> EscrowParentBroadcast:
        """Broadcast exactly the finalized parent stored before the RPC attempt."""
        session_id = request.prepared.terms.snapshot.session_id
        descriptor = await self._acquire_lock(session_id)
        try:
            journal = self._require_journal(session_id)
            self._require_registered_parent(journal, request)
            actual_txid = self._transaction_id(
                request.finalized_parent.signed_parent_tx_hex, "parent"
            )
            if actual_txid != request.prepared.parent_evidence.parent_txid:
                raise ChannelInputEscrowError(
                    "finalized parent transaction ID does not match prepared parent"
                )
            if journal.parent_request is not None and journal.parent_request != request:
                raise ChannelInputEscrowError("conflicting parent broadcast request")
            if journal.parent_result is None:
                new_result = EscrowParentBroadcast(
                    session_id=session_id,
                    parent_txid=actual_txid,
                    broadcast_height=await self._height(),
                )
                journal = journal.model_copy(
                    update={
                        "parent_request": request,
                        "parent_result": new_result,
                        "parent_complete": False,
                    }
                )
                self._write_journal(journal)
            result = journal.parent_result
            if result is None:
                raise ChannelInputEscrowError("parent broadcast journal is incomplete")
            if not journal.parent_complete:
                await self._broadcast_exact(
                    request.finalized_parent.signed_parent_tx_hex,
                    result.parent_txid,
                    test_accept=False,
                )
                journal = journal.model_copy(update={"parent_complete": True})
                self._write_journal(journal)
            return result
        finally:
            self._release_lock(descriptor)

    async def observe_escrow_chain(
        self, request: ChainObservationRequest
    ) -> EscrowChainObservation:
        """Observe active confirmation, descendants, and confirmed channel conflicts."""
        descriptor = await self._acquire_lock(request.session_id)
        try:
            journal = self._require_journal(request.session_id)
            if journal.parent_request is None or journal.parent_result is None:
                raise ChannelInputEscrowError("parent broadcast journal was not found")
            if request.parent_txid != journal.parent_result.parent_txid:
                raise ChannelInputEscrowError("chain observation parent does not match journal")
            height = await self._height()
            parent_confirmations = await self._active_confirmations(
                request.parent_txid, journal.parent_request.finalized_parent.signed_parent_tx_hex
            )
            active = parent_confirmations > 0
            conflict: tuple[str, int] | None = None
            if not active:
                conflict = await self._find_confirmed_conflict(journal, height)
            success_txid: str | None = None
            success_confirmations: int | None = None
            timeout_txid: str | None = None
            timeout_confirmations: int | None = None
            if active:
                success_txid, success_confirmations = await self._success_observation(journal)
                timeout_txid, timeout_confirmations = await self._timeout_observation(journal)
            observation = EscrowChainObservation(
                session_id=request.session_id,
                parent_txid=request.parent_txid,
                parent_confirmations=parent_confirmations if active else 0,
                in_best_chain=active,
                conflict_txid=conflict[0] if conflict is not None else None,
                conflict_confirmations=conflict[1] if conflict is not None else None,
                conflicting_channel_outpoint=journal.frozen_outpoint
                if conflict is not None
                else None,
                conflict_spends_channel=True if conflict is not None else None,
                success_txid=success_txid,
                success_confirmations=success_confirmations,
                timeout_txid=timeout_txid,
                timeout_confirmations=timeout_confirmations,
                observed_height=height,
            )
            self._write_journal(journal.model_copy(update={"last_observation": observation}))
            return observation
        finally:
            self._release_lock(descriptor)

    async def build_broadcast_success_spend(
        self, request: SuccessSpendRequest
    ) -> EscrowSpendResult:
        """Build, preflight, and broadcast one exact maker-controlled success spend."""
        session_id = request.prepared.terms.snapshot.session_id
        descriptor = await self._acquire_lock(session_id)
        try:
            journal = self._require_journal(session_id)
            self._require_prepared_parent(journal, request.prepared.parent_evidence.parent_txid)
            if journal.success_plan is not None and journal.success_plan.request != request:
                raise ChannelInputEscrowError("conflicting success spend request")
            if journal.success_plan is None:
                material, outpoint, preimage = await self._resolve_success_material(request)
                feerate = await self._target_feerate_milli()
                result = self._build_success_result(
                    request,
                    material,
                    outpoint,
                    preimage,
                    feerate,
                    await self._height(),
                    None,
                    False,
                )
                journal = journal.model_copy(
                    update={"success_plan": _SuccessPlan(request=request, result=result)}
                )
                self._write_journal(journal)
            plan = journal.success_plan
            if plan is None:
                raise ChannelInputEscrowError("success spend journal is incomplete")
            if not plan.complete:
                await self._broadcast_exact(
                    plan.result.tx_hex,
                    plan.result.txid,
                    test_accept=True,
                )
                journal = journal.model_copy(
                    update={"success_plan": plan.model_copy(update={"complete": True})}
                )
                self._write_journal(journal)
            return plan.result
        finally:
            self._release_lock(descriptor)

    async def broadcast_timeout_recovery(self, request: RecoveryRequest) -> EscrowTimeoutBroadcast:
        """Broadcast the exact prepared timeout transaction after its strict maturity."""
        record = request.record
        session_id = record.session_id
        descriptor = await self._acquire_lock(session_id)
        try:
            journal = self._require_journal(session_id)
            prepared = self._require_record_parent(journal, request)
            if journal.timeout_plan is not None and journal.timeout_plan.request != request:
                raise ChannelInputEscrowError("conflicting timeout recovery request")
            if journal.timeout_plan is None:
                height = await self._height()
                if height <= prepared.terms.timeout_height:
                    raise ChannelInputEscrowError("timeout recovery is not yet mature")
                timeout = prepared.timeout_evidence
                if self._timeout_transaction_id(timeout.timeout_tx_hex) != timeout.timeout_txid:
                    raise ChannelInputEscrowError(
                        "timeout transaction ID does not match prepared timeout"
                    )
                result = EscrowTimeoutBroadcast(
                    session_id=session_id,
                    parent_txid=prepared.parent_evidence.parent_txid,
                    timeout_txid=timeout.timeout_txid,
                    broadcast_height=height,
                )
                journal = journal.model_copy(
                    update={"timeout_plan": _TimeoutPlan(request=request, result=result)}
                )
                self._write_journal(journal)
            plan = journal.timeout_plan
            if plan is None:
                raise ChannelInputEscrowError("timeout recovery journal is incomplete")
            if not plan.complete:
                await self._broadcast_exact(
                    record.prepared.timeout_evidence.timeout_tx_hex if record.prepared else "",
                    plan.result.timeout_txid,
                    test_accept=False,
                )
                journal = journal.model_copy(
                    update={"timeout_plan": plan.model_copy(update={"complete": True})}
                )
                self._write_journal(journal)
            return plan.result
        finally:
            self._release_lock(descriptor)

    async def fee_bump_success_spend(self, request: RecoveryRequest) -> EscrowSpendResult:
        """Build and broadcast one exact RBF replacement without wallet inputs."""
        record = request.record
        session_id = record.session_id
        descriptor = await self._acquire_lock(session_id)
        try:
            journal = self._require_journal(session_id)
            prepared = self._require_record_parent(journal, request)
            prior = record.success_spend
            if prior is None:
                raise ChannelInputEscrowError("fee bump requires a pending success spend")
            if journal.success_plan is None:
                raise ChannelInputEscrowError("initial success spend journal was not found")
            matching_index = next(
                (
                    index
                    for index, existing in enumerate(journal.fee_bump_plans)
                    if existing.request == request
                ),
                None,
            )
            if matching_index is None:
                durable_prior = (
                    journal.fee_bump_plans[-1].result
                    if journal.fee_bump_plans
                    else journal.success_plan.result
                )
                if durable_prior != prior:
                    raise ChannelInputEscrowError(
                        "fee bump success spend does not match the latest durable replacement"
                    )
                payment = record.payment_result
                if payment is None or payment.preimage is None or payment.settled_height is None:
                    raise ChannelInputEscrowError("fee bump requires settled payment evidence")
                material = await self._material_for(prepared.terms.snapshot.session_id, prepared)
                outpoint = self._prepared_outpoint(prepared)
                incremental = await self._incremental_relay_feerate_milli()
                target = await self._target_feerate_milli()
                result = self._build_replacement_result(
                    prepared,
                    payment.preimage,
                    payment.settled_height,
                    material,
                    outpoint,
                    prior,
                    target,
                    incremental,
                    await self._height(),
                )
                journal = journal.model_copy(
                    update={
                        "fee_bump_plans": (
                            *journal.fee_bump_plans,
                            _FeeBumpPlan(request=request, result=result),
                        )
                    }
                )
                self._write_journal(journal)
                matching_index = len(journal.fee_bump_plans) - 1
            plan = journal.fee_bump_plans[matching_index]
            if not plan.complete:
                await self._broadcast_exact(
                    plan.result.tx_hex,
                    plan.result.txid,
                    test_accept=True,
                )
                plans = list(journal.fee_bump_plans)
                plans[matching_index] = plan.model_copy(update={"complete": True})
                journal = journal.model_copy(update={"fee_bump_plans": tuple(plans)})
                self._write_journal(journal)
            return plan.result
        finally:
            self._release_lock(descriptor)

    async def rebroadcast_recovery_artifacts(
        self, request: RecoveryRequest
    ) -> RecoveryBroadcastResult:
        """Rebroadcast the exact parent and required descendant in parent-first order."""
        record = request.record
        session_id = record.session_id
        descriptor = await self._acquire_lock(session_id)
        try:
            journal = self._require_journal(session_id)
            prepared = self._require_record_parent(journal, request)
            matching_index = next(
                (
                    index
                    for index, existing in enumerate(journal.recovery_plans)
                    if existing.request == request
                ),
                None,
            )
            if matching_index is None:
                txids = [prepared.parent_evidence.parent_txid]
                tx_hexes = (
                    [journal.parent_request.finalized_parent.signed_parent_tx_hex]
                    if journal.parent_request
                    else []
                )
                if record.state_before_reorg is EscrowLifecycleState.SUCCESS_SPEND_PENDING:
                    latest = record.success_spend
                    if latest is None:
                        raise ChannelInputEscrowError(
                            "success recovery requires a durable success spend"
                        )
                    txids.append(latest.txid)
                    tx_hexes.append(latest.tx_hex)
                elif record.state_before_reorg is EscrowLifecycleState.TIMEOUT_PENDING:
                    if record.timeout_broadcast is None:
                        raise ChannelInputEscrowError(
                            "timeout recovery requires durable timeout evidence"
                        )
                    txids.append(record.timeout_broadcast.timeout_txid)
                    tx_hexes.append(prepared.timeout_evidence.timeout_tx_hex)
                result = RecoveryBroadcastResult(
                    session_id=session_id,
                    parent_txid=prepared.parent_evidence.parent_txid,
                    broadcast_txids=tuple(txids),
                    broadcast_height=await self._height(),
                )
                journal = journal.model_copy(
                    update={
                        "recovery_plans": (
                            *journal.recovery_plans,
                            _RecoveryPlan(
                                request=request,
                                result=result,
                                transaction_hexes=tuple(tx_hexes),
                            ),
                        )
                    }
                )
                self._write_journal(journal)
                matching_index = len(journal.recovery_plans) - 1
            plan = journal.recovery_plans[matching_index]
            if not plan.complete:
                for tx_hex, txid in zip(
                    plan.transaction_hexes,
                    plan.result.broadcast_txids,
                    strict=True,
                ):
                    await self._broadcast_exact(tx_hex, txid, test_accept=False)
                plans = list(journal.recovery_plans)
                plans[matching_index] = plan.model_copy(update={"complete": True})
                journal = journal.model_copy(update={"recovery_plans": tuple(plans)})
                self._write_journal(journal)
            return plan.result
        finally:
            self._release_lock(descriptor)

    def _require_registered_parent(
        self, journal: _EscrowJournal, request: ParentBroadcastRequest
    ) -> None:
        if journal.frozen_outpoint != request.prepared.terms.snapshot.funding_outpoint:
            raise ChannelInputEscrowError(
                "prepared parent does not spend the registered frozen channel"
            )

    def _require_prepared_parent(self, journal: _EscrowJournal, parent_txid: str) -> None:
        if journal.parent_request is None or journal.parent_result is None:
            raise ChannelInputEscrowError("parent broadcast journal was not found")
        if journal.parent_result.parent_txid != parent_txid:
            raise ChannelInputEscrowError("prepared parent does not match durable parent journal")

    def _require_record_parent(self, journal: _EscrowJournal, request: RecoveryRequest) -> Any:
        prepared = request.record.prepared
        if prepared is None:
            raise ChannelInputEscrowError("recovery request requires prepared escrow evidence")
        self._require_prepared_parent(journal, prepared.parent_evidence.parent_txid)
        if journal.parent_request is None or journal.parent_request.prepared != prepared:
            raise ChannelInputEscrowError(
                "recovery prepared escrow does not match durable parent journal"
            )
        return prepared

    async def _resolve_success_material(
        self, request: SuccessSpendRequest
    ) -> tuple[SuccessSpendMaterial, SwapOutpoint, bytes]:
        material = await self._material_for(
            request.prepared.terms.snapshot.session_id, request.prepared
        )
        preimage = bytes.fromhex(request.payment_result.preimage or "")
        if len(preimage) != 32:
            raise ChannelInputEscrowError("success spend requires a 32-byte payment preimage")
        return material, self._prepared_outpoint(request.prepared), preimage

    async def _material_for(self, session_id: str, prepared: Any) -> SuccessSpendMaterial:
        try:
            material = await self._resolver.resolve_success_material(session_id)
        except (ChannelInputEscrowError, ChainError):
            raise
        except Exception as exc:  # noqa: BLE001 - resolver errors must not leak secrets
            raise ChannelInputEscrowError("could not resolve maker success material") from exc
        if not isinstance(material, SuccessSpendMaterial):
            raise ChannelInputEscrowError("success material resolver returned an invalid result")
        if material.tree.payment_hash.hex() != prepared.terms.payment_hash:
            raise ChannelInputEscrowError(
                "success material payment hash does not match prepared escrow"
            )
        script = bytes.fromhex(prepared.parent_evidence.escrow_scriptpubkey)
        if material.tree.funding_output_script() != script:
            raise ChannelInputEscrowError(
                "success material funding script does not match prepared escrow"
            )
        return material

    @staticmethod
    def _prepared_outpoint(prepared: Any) -> SwapOutpoint:
        evidence = prepared.parent_evidence
        return SwapOutpoint(
            txid=evidence.escrow_outpoint.txid,
            vout=evidence.escrow_outpoint.vout,
            value=evidence.escrow_value_sats,
            scriptpubkey=bytes.fromhex(evidence.escrow_scriptpubkey),
        )

    def _build_success_result(
        self,
        request: SuccessSpendRequest,
        material: SuccessSpendMaterial,
        outpoint: SwapOutpoint,
        preimage: bytes,
        feerate_milli: int,
        height: int,
        replaces_txid: str | None,
        replacement_policy_verified: bool,
    ) -> EscrowSpendResult:
        # The first signed serialization gives the exact witness-aware vsize.
        unsigned_fee_tx = build_success_spend(
            material.tree,
            outpoint,
            preimage,
            material.maker_claim_private_key,
            material.maker_output_script,
            0,
        )
        vsize = calculate_tx_vsize(unsigned_fee_tx.serialize())
        fee = self._fee_for_rate(feerate_milli, vsize)
        tx = build_success_spend(
            material.tree,
            outpoint,
            preimage,
            material.maker_claim_private_key,
            material.maker_output_script,
            fee,
        )
        raw = tx.serialize()
        vsize = calculate_tx_vsize(raw)
        fee = self._fee_for_rate(feerate_milli, vsize)
        # Rebuild after the exact vsize calculation.  This is deterministic.
        tx = build_success_spend(
            material.tree,
            outpoint,
            preimage,
            material.maker_claim_private_key,
            material.maker_output_script,
            fee,
        )
        if not verify_success_spend(material.tree, outpoint, material.maker_output_script, fee, tx):
            raise ChannelInputEscrowError("success spend verification failed")
        raw = tx.serialize()
        tx_hex = raw.hex()
        txid = self._transaction_id(tx_hex, "success")
        result = EscrowSpendResult(
            session_id=request.prepared.terms.snapshot.session_id,
            parent_txid=request.prepared.parent_evidence.parent_txid,
            spent_escrow_outpoint=request.prepared.parent_evidence.escrow_outpoint,
            txid=txid,
            tx_hex=tx_hex,
            escrow_input_value_sats=outpoint.value,
            additional_input_value_sats=0,
            maker_controlled_output_sats=outpoint.value - fee,
            fee_sats=fee,
            fee_rate_milli_sat_vb=self._fee_rate_milli(fee, calculate_tx_vsize(raw)),
            fulfillment_height=request.payment_result.settled_height or 0,
            broadcast_height=height,
            rbf_enabled=True,
            preimage_verified=True,
            success_path_verified=True,
            transaction_verified=True,
            all_outputs_maker_controlled=True,
            replacement_policy_verified=replacement_policy_verified,
            replaces_txid=replaces_txid,
        )
        result.validate_against(request.prepared)
        return result

    def _build_replacement_result(
        self,
        prepared: Any,
        preimage_hex: str,
        settled_height: int,
        material: SuccessSpendMaterial,
        outpoint: SwapOutpoint,
        prior: EscrowSpendResult,
        target_milli: int,
        incremental_milli: int,
        height: int,
    ) -> EscrowSpendResult:
        preimage = bytes.fromhex(preimage_hex)
        fee = prior.fee_sats + 1
        for _ in range(4):
            probe = build_success_spend(
                material.tree,
                outpoint,
                preimage,
                material.maker_claim_private_key,
                material.maker_output_script,
                fee,
            )
            vsize = calculate_tx_vsize(probe.serialize())
            required = max(
                self._fee_for_rate(target_milli, vsize),
                prior.fee_sats + self._fee_for_rate(incremental_milli, vsize),
                (prior.fee_rate_milli_sat_vb * vsize) // 1000 + 1,
                prior.fee_sats + 1,
            )
            if required == fee:
                break
            fee = required
        tx = build_success_spend(
            material.tree,
            outpoint,
            preimage,
            material.maker_claim_private_key,
            material.maker_output_script,
            fee,
        )
        raw = tx.serialize()
        vsize = calculate_tx_vsize(raw)
        if fee <= prior.fee_sats or self._fee_rate_milli(fee, vsize) <= prior.fee_rate_milli_sat_vb:
            raise ChannelInputEscrowError("replacement does not increase fee and feerate")
        if not verify_success_spend(material.tree, outpoint, material.maker_output_script, fee, tx):
            raise ChannelInputEscrowError("replacement success spend verification failed")
        result = EscrowSpendResult(
            session_id=prepared.terms.snapshot.session_id,
            parent_txid=prepared.parent_evidence.parent_txid,
            spent_escrow_outpoint=prepared.parent_evidence.escrow_outpoint,
            txid=self._transaction_id(raw.hex(), "success replacement"),
            tx_hex=raw.hex(),
            escrow_input_value_sats=outpoint.value,
            additional_input_value_sats=0,
            maker_controlled_output_sats=outpoint.value - fee,
            fee_sats=fee,
            fee_rate_milli_sat_vb=self._fee_rate_milli(fee, vsize),
            fulfillment_height=settled_height,
            broadcast_height=height,
            rbf_enabled=True,
            preimage_verified=True,
            success_path_verified=True,
            transaction_verified=True,
            all_outputs_maker_controlled=True,
            replacement_policy_verified=True,
            replaces_txid=prior.txid,
        )
        result.validate_against(prepared)
        return result

    async def _target_feerate_milli(self) -> int:
        candidates = [self._minimum_feerate_milli_sat_vb]
        for method, params, field in (
            ("estimatesmartfee", [self._fee_target_blocks], "feerate"),
            ("getmempoolinfo", [], "mempoolminfee"),
        ):
            try:
                response = await self._rpc.call(method, params)
            except BitcoinCoreRpcError:
                continue
            if isinstance(response, dict) and response.get(field) is not None:
                try:
                    candidates.append(self._btc_kvb_to_milli(response[field]))
                except (ArithmeticError, TypeError, ValueError):
                    continue
        return max(candidates)

    async def _incremental_relay_feerate_milli(self) -> int:
        try:
            response = await self._rpc.call("getmempoolinfo", [])
        except BitcoinCoreRpcError as exc:
            raise ChainError("could not obtain incremental relay fee") from exc
        if not isinstance(response, dict) or "incrementalrelayfee" not in response:
            raise ChainError("Bitcoin Core did not return incremental relay fee")
        try:
            return self._btc_kvb_to_milli(response["incrementalrelayfee"])
        except (ArithmeticError, TypeError, ValueError) as exc:
            raise ChainError("Bitcoin Core returned an invalid incremental relay fee") from exc

    async def _broadcast_exact(self, tx_hex: str, txid: str, *, test_accept: bool) -> None:
        if await self._exact_transaction_visible(txid, tx_hex):
            return
        if test_accept:
            try:
                accepted = await self._rpc.call("testmempoolaccept", [[tx_hex]])
            except BitcoinCoreRpcError as exc:
                raise ChainError("Bitcoin Core mempool policy check failed") from exc
            if (
                not isinstance(accepted, list)
                or len(accepted) != 1
                or not isinstance(accepted[0], dict)
                or accepted[0].get("allowed") is not True
            ):
                raise ChainError("Bitcoin Core rejected escrow transaction by mempool policy")
        try:
            returned = await self._rpc.call("sendrawtransaction", [tx_hex])
        except BitcoinCoreRpcError as exc:
            if exc.code in _KNOWN_ALREADY_SUBMITTED_CODES and await self._exact_transaction_visible(
                txid, tx_hex
            ):
                return
            raise ChainError("Bitcoin Core rejected escrow transaction broadcast") from exc
        if not isinstance(returned, str) or returned.lower() != txid:
            raise ChainError("Bitcoin Core returned an unexpected transaction ID")
        if not await self._exact_transaction_visible(txid, tx_hex):
            raise ChainError("Bitcoin Core did not make the exact transaction visible")

    async def _exact_transaction_visible(self, txid: str, expected_hex: str) -> bool:
        try:
            transaction = await self._rpc.call("getrawtransaction", [txid, True])
        except BitcoinCoreRpcError:
            return False
        if not isinstance(transaction, dict) or not isinstance(transaction.get("hex"), str):
            raise ChainError("Bitcoin Core returned malformed transaction visibility data")
        actual_hex = transaction["hex"].lower()
        if (
            self._transaction_id(actual_hex, "visible") != txid
            or actual_hex != expected_hex.lower()
        ):
            raise ChainError(
                "Bitcoin Core visible transaction does not match durable transaction bytes"
            )
        blockhash = transaction.get("blockhash")
        if blockhash is None:
            return True
        if not isinstance(blockhash, str):
            raise ChainError("Bitcoin Core returned malformed transaction block data")
        try:
            header = await self._rpc.call("getblockheader", [blockhash, True])
        except BitcoinCoreRpcError:
            return False
        confirmations = header.get("confirmations") if isinstance(header, dict) else None
        return isinstance(confirmations, int) and confirmations > 0

    async def _active_confirmations(self, txid: str, expected_hex: str) -> int:
        try:
            transaction = await self._rpc.call("getrawtransaction", [txid, True])
        except BitcoinCoreRpcError:
            return 0
        if not isinstance(transaction, dict) or not isinstance(transaction.get("hex"), str):
            raise ChainError("Bitcoin Core returned malformed transaction data")
        actual_hex = transaction["hex"].lower()
        if (
            self._transaction_id(actual_hex, "observed") != txid
            or actual_hex != expected_hex.lower()
        ):
            raise ChainError("Bitcoin Core transaction bytes do not match durable evidence")
        blockhash = transaction.get("blockhash")
        if not isinstance(blockhash, str):
            return 0
        try:
            header = await self._rpc.call("getblockheader", [blockhash, True])
        except BitcoinCoreRpcError:
            return 0
        confirmations = header.get("confirmations") if isinstance(header, dict) else None
        return confirmations if isinstance(confirmations, int) and confirmations > 0 else 0

    async def _find_confirmed_conflict(
        self, journal: _EscrowJournal, tip_height: int
    ) -> tuple[str, int] | None:
        for height in range(journal.monitor_start_height, tip_height + 1):
            try:
                blockhash = await self._rpc.call("getblockhash", [height])
                block = await self._rpc.call("getblock", [blockhash, 2])
            except BitcoinCoreRpcError as exc:
                raise ChainError("Bitcoin Core block scan failed") from exc
            if (
                not isinstance(blockhash, str)
                or not isinstance(block, dict)
                or not isinstance(block.get("tx"), list)
            ):
                raise ChainError("Bitcoin Core returned malformed block scan data")
            for transaction in block["tx"]:
                if not isinstance(transaction, dict):
                    raise ChainError("Bitcoin Core returned malformed block transaction")
                txid = transaction.get("txid")
                if (
                    not isinstance(txid, str)
                    or journal.parent_result is None
                    or txid.lower() == journal.parent_result.parent_txid
                ):
                    continue
                vins = transaction.get("vin")
                if not isinstance(vins, list):
                    continue
                if any(
                    isinstance(vin, dict)
                    and vin.get("txid") == journal.frozen_outpoint.txid
                    and vin.get("vout") == journal.frozen_outpoint.vout
                    for vin in vins
                ):
                    return txid.lower(), tip_height - height + 1
        return None

    async def _success_observation(self, journal: _EscrowJournal) -> tuple[str | None, int | None]:
        plans = (*reversed(journal.fee_bump_plans), journal.success_plan)
        latest: EscrowSpendResult | None = None
        for plan in plans:
            if plan is None:
                continue
            if latest is None:
                latest = plan.result
            confirmations = await self._active_confirmations(plan.result.txid, plan.result.tx_hex)
            if confirmations:
                return plan.result.txid, confirmations
        return (latest.txid, 0) if latest is not None else (None, None)

    async def _timeout_observation(self, journal: _EscrowJournal) -> tuple[str | None, int | None]:
        if journal.timeout_plan is None or journal.parent_request is None:
            return None, None
        tx_hex = journal.parent_request.prepared.timeout_evidence.timeout_tx_hex
        confirmations = await self._active_confirmations(
            journal.timeout_plan.result.timeout_txid, tx_hex
        )
        return journal.timeout_plan.result.timeout_txid, confirmations

    async def _height(self) -> int:
        try:
            height = await self._rpc.call("getblockcount", [])
        except BitcoinCoreRpcError as exc:
            raise ChainError("Bitcoin Core height lookup failed") from exc
        if isinstance(height, bool) or not isinstance(height, int) or height < 0:
            raise ChainError("Bitcoin Core returned an invalid block height")
        return height

    @staticmethod
    def _transaction_id(tx_hex: str, name: str) -> str:
        try:
            return get_txid(tx_hex)
        except (TypeError, ValueError) as exc:
            raise ChannelInputEscrowError(f"{name} transaction encoding is invalid") from exc

    @staticmethod
    def _timeout_transaction_id(tx_hex: str) -> str:
        try:
            parse_escrow_transaction(tx_hex)
        except ChannelInputEscrowError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize parser implementation failures
            raise ChannelInputEscrowError("timeout transaction encoding is invalid") from exc
        return BitcoinCoreEscrowChainAdapter._transaction_id(tx_hex, "timeout")

    @staticmethod
    def _ceil_decimal(value: Decimal) -> int:
        return int(value.to_integral_value(rounding=ROUND_CEILING))

    @classmethod
    def _btc_kvb_to_milli(cls, value: Any) -> int:
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite() or decimal_value < 0:
            raise ValueError("fee rate is invalid")
        return cls._ceil_decimal(decimal_value * _SAT_PER_BTC)

    @staticmethod
    def _fee_for_rate(feerate_milli: int, vsize: int) -> int:
        return (feerate_milli * vsize + 999) // 1000

    @staticmethod
    def _fee_rate_milli(fee_sats: int, vsize: int) -> int:
        return (fee_sats * 1000 + vsize - 1) // vsize

    def _journal_path(self, session_id: str) -> Path:
        return self._journal_dir / f"{hashlib.sha256(session_id.encode('utf-8')).hexdigest()}.json"

    async def _acquire_lock(self, session_id: str) -> int:
        lock_path = self._journal_path(session_id).with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _release_lock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def _load_journal(self, session_id: str) -> _EscrowJournal | None:
        path = self._journal_path(session_id)
        if not path.exists():
            return None
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ChannelInputEscrowError("escrow chain journal has unsafe permissions")
        try:
            journal = _EscrowJournal.model_validate_json(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - corrupt private journals fail closed
            raise ChannelInputEscrowError("escrow chain journal is invalid") from exc
        if journal.session_id != session_id:
            raise ChannelInputEscrowError("escrow chain journal session does not match")
        return journal

    def _require_journal(self, session_id: str) -> _EscrowJournal:
        journal = self._load_journal(session_id)
        if journal is None:
            raise ChannelInputEscrowError("frozen channel registration was not found")
        return journal

    def _write_journal(self, journal: _EscrowJournal) -> None:
        path = self._journal_path(journal.session_id)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=self._journal_dir, prefix=f".{path.stem}.", suffix=".tmp"
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(journal.model_dump_json().encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            directory_descriptor = os.open(self._journal_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)
            raise
