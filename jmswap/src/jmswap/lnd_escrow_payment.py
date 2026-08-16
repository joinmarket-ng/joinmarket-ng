"""Restart-safe LND adapter for a single channel-input escrow payment."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import stat
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jmswap.channel_escrow import (
    EscrowRouteEvidence,
    PaymentObservationRequest,
    SinglePartPaymentRequest,
    SinglePartPaymentResult,
)
from jmswap.errors import ChannelInputEscrowError, LightningError
from jmswap.lightning import PaymentStatus
from jmswap.lnd import LndBackend

_JOURNAL_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


@runtime_checkable
class BlockHeightSource(Protocol):
    """The narrow chain dependency needed to time an escrow payment."""

    async def block_height(self) -> int:
        """Return the current best-chain height."""


class _PaymentJournal(BaseModel):
    """Private durable intent and latest observation for one LND payment."""

    model_config = _JOURNAL_CONFIG

    session_id: str = Field(min_length=1)
    request: SinglePartPaymentRequest
    allowed_primary_channel_ids: tuple[int, ...] = Field(min_length=1)
    allowed_route_channel_ids: tuple[str, ...] = Field(min_length=1)
    excluded_route_channel_ids: tuple[str, ...] = Field(min_length=1)
    result: SinglePartPaymentResult | None = None

    @field_validator("allowed_primary_channel_ids")
    @classmethod
    def validate_primary_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(channel_id <= 0 for channel_id in value) or len(set(value)) != len(value):
            raise ValueError("allowed primary channel IDs must be unique and positive")
        return value

    @field_validator("allowed_route_channel_ids", "excluded_route_channel_ids")
    @classmethod
    def validate_route_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not channel_id for channel_id in value) or len(set(value)) != len(value):
            raise ValueError("route channel IDs must be unique and non-empty")
        return value


class _PaymentNotFoundError(Exception):
    """LND has no payment matching a durable local intent."""


class LndEscrowPaymentAdapter:
    """Submit and observe one escrow payment with a durable exact-retry journal."""

    def __init__(
        self,
        lnd: LndBackend,
        height_source: BlockHeightSource,
        journal_dir: str | Path,
        *,
        fee_limit_msat: int,
        timeout_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(lnd, LndBackend):
            raise TypeError("lnd must be an LndBackend")
        if not isinstance(height_source, BlockHeightSource):
            raise TypeError("height_source must provide async block_height")
        if (
            isinstance(fee_limit_msat, bool)
            or not isinstance(fee_limit_msat, int)
            or fee_limit_msat <= 0
        ):
            raise ValueError("fee_limit_msat must be positive")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")

        self._lnd = lnd
        self._height_source = height_source
        self._fee_limit_msat = fee_limit_msat
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._journal_dir = Path(journal_dir)
        self._journal_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self._journal_dir.is_dir():
            raise ValueError("journal_dir must be a directory")
        if stat.S_IMODE(self._journal_dir.stat().st_mode) & 0o077:
            raise ValueError("journal_dir must not be accessible by group or others")

    async def send_single_part_payment(
        self, request: SinglePartPaymentRequest
    ) -> SinglePartPaymentResult:
        """Durably bind and submit one payment, or recover its existing LND state."""
        session_id = request.prepared.terms.snapshot.session_id
        lock_descriptor = await self._acquire_journal_lock(session_id)
        try:
            journal = self._load_journal(session_id)
            is_new_journal = journal is None
            if journal is None:
                try:
                    journal = await self._new_journal(request)
                except ChannelInputEscrowError:
                    return await self._pre_submission_failure(request)
            elif journal.request != request:
                raise ChannelInputEscrowError("conflicting payment request for escrow session")

            if journal.result is not None and journal.result.status is not PaymentStatus.PENDING:
                return journal.result

            if is_new_journal:
                return await self._send_payment(journal)

            try:
                return await self._track_payment(journal)
            except _PaymentNotFoundError:
                return await self._send_payment(journal)
        finally:
            self._release_journal_lock(lock_descriptor)

    async def observe_escrow_payment(
        self, request: PaymentObservationRequest
    ) -> SinglePartPaymentResult:
        """Track and atomically persist the first usable current LND payment state."""
        lock_descriptor = await self._acquire_journal_lock(request.session_id)
        try:
            journal = self._load_journal(request.session_id)
            if journal is None:
                raise ChannelInputEscrowError("escrow payment journal was not found")
            expected_payment_id = journal.request.prepared.terms.payment_hash
            if request.payment_id != expected_payment_id:
                raise ChannelInputEscrowError(
                    "payment ID does not match the escrow payment journal"
                )
            if journal.result is not None and journal.result.status is not PaymentStatus.PENDING:
                return journal.result
            try:
                return await self._track_payment(journal)
            except _PaymentNotFoundError as exc:
                raise LightningError("LND payment was not found") from exc
        finally:
            self._release_journal_lock(lock_descriptor)

    async def _new_journal(self, request: SinglePartPaymentRequest) -> _PaymentJournal:
        lightning, _ = self._lnd.payment_stubs()
        try:
            response = await lightning.ListChannels(self._list_channels_request())
        except Exception as exc:  # noqa: BLE001 - gRPC errors have many concrete classes
            raise LightningError("LND channel listing failed") from exc

        excluded_primary_ids, excluded_route_ids = self._resolve_exclusions(
            request, response.channels
        )
        allowed_primary_ids: list[int] = []
        allowed_route_ids: set[str] = set()
        for channel in response.channels:
            primary_id = int(channel.chan_id)
            if not bool(channel.active) or primary_id <= 0 or primary_id in excluded_primary_ids:
                continue
            allowed_primary_ids.append(primary_id)
            allowed_route_ids.update(self._channel_route_ids(channel))
        if not allowed_primary_ids:
            raise ChannelInputEscrowError(
                "no eligible first-hop channels remain for escrow payment"
            )

        journal = _PaymentJournal(
            session_id=request.prepared.terms.snapshot.session_id,
            request=request,
            allowed_primary_channel_ids=tuple(sorted(allowed_primary_ids)),
            allowed_route_channel_ids=tuple(sorted(allowed_route_ids)),
            excluded_route_channel_ids=tuple(sorted(excluded_route_ids)),
        )
        self._write_journal(journal)
        return journal

    @staticmethod
    def _list_channels_request() -> Any:
        from jmswap.lndrpc import lightning_pb2 as ln

        return ln.ListChannelsRequest()

    def _resolve_exclusions(
        self, request: SinglePartPaymentRequest, channels: Any
    ) -> tuple[set[int], set[str]]:
        requested_exclusions = set(request.excluded_channel_ids)
        consumed_channel = request.prepared.terms.snapshot.channel_id
        matched_identifiers: set[str] = set()
        excluded_primary_ids: set[int] = set()
        excluded_route_ids: set[str] = set()

        for channel in channels:
            identifiers = self._channel_identifiers(channel)
            matched = identifiers & requested_exclusions
            if not matched:
                continue
            matched_identifiers.update(matched)
            primary_id = int(channel.chan_id)
            if primary_id > 0:
                excluded_primary_ids.add(primary_id)
            excluded_route_ids.update(self._channel_route_ids(channel))

        if consumed_channel not in matched_identifiers:
            raise ChannelInputEscrowError("the consumed escrow channel is not listed by LND")
        if requested_exclusions != matched_identifiers:
            raise ChannelInputEscrowError("an excluded escrow channel is not listed by LND")
        return excluded_primary_ids, excluded_route_ids

    @staticmethod
    def _channel_identifiers(channel: Any) -> set[str]:
        identifiers = {str(channel.chan_id), str(channel.channel_point)}
        identifiers.update(str(alias) for alias in channel.alias_scids)
        return identifiers

    @staticmethod
    def _channel_route_ids(channel: Any) -> set[str]:
        route_ids = {str(channel.chan_id)}
        route_ids.update(str(alias) for alias in channel.alias_scids)
        return {channel_id for channel_id in route_ids if channel_id != "0"}

    async def _send_payment(self, journal: _PaymentJournal) -> SinglePartPaymentResult:
        if int(self._clock()) >= journal.request.prepared.terms.invoice_expiry_unix:
            return await self._persist_pre_submission_failure(journal)
        if await self._height_source.block_height() != journal.request.requested_at_height:
            return await self._persist_pre_submission_failure(journal)
        _, router = self._lnd.payment_stubs()
        request = journal.request
        try:
            cltv_limit = self._cltv_limit(request)
        except ChannelInputEscrowError:
            return await self._persist_pre_submission_failure(journal)
        from jmswap.lndrpc import router_pb2

        rpc_request = router_pb2.SendPaymentRequest(
            payment_request=request.prepared.terms.invoice,
            fee_limit_msat=self._fee_limit_msat,
            timeout_seconds=self._timeout_seconds,
            max_parts=1,
            no_inflight_updates=False,
            outgoing_chan_ids=journal.allowed_primary_channel_ids,
            allow_self_payment=False,
            amp=False,
            cancelable=False,
            cltv_limit=cltv_limit,
        )
        try:
            stream = router.SendPaymentV2(rpc_request)
            return await self._first_usable_result(stream, journal)
        except Exception as exc:  # noqa: BLE001 - gRPC errors have many concrete classes
            if self._rpc_code_name(exc) == "ALREADY_EXISTS":
                try:
                    return await self._track_payment(journal)
                except _PaymentNotFoundError as track_exc:
                    raise LightningError(
                        "LND payment was not found after a duplicate submission"
                    ) from track_exc
            if isinstance(exc, (ChannelInputEscrowError, LightningError)):
                raise
            raise LightningError("LND payment submission failed") from exc

    async def _track_payment(self, journal: _PaymentJournal) -> SinglePartPaymentResult:
        _, router = self._lnd.payment_stubs()
        from jmswap.lndrpc import router_pb2

        try:
            stream = router.TrackPaymentV2(
                router_pb2.TrackPaymentRequest(
                    payment_hash=bytes.fromhex(journal.request.prepared.terms.payment_hash),
                    no_inflight_updates=False,
                )
            )
            return await self._first_usable_result(stream, journal)
        except Exception as exc:  # noqa: BLE001 - gRPC errors have many concrete classes
            if self._rpc_code_name(exc) == "NOT_FOUND":
                raise _PaymentNotFoundError from exc
            if isinstance(exc, (ChannelInputEscrowError, LightningError, _PaymentNotFoundError)):
                raise
            raise LightningError("LND payment tracking failed") from exc

    async def _first_usable_result(
        self, stream: Any, journal: _PaymentJournal
    ) -> SinglePartPaymentResult:
        async for payment in stream:
            result = await self._map_payment(payment, journal)
            if result is None:
                continue
            if journal.result is not None:
                self._validate_monotone_result(journal.result, result)
            updated = journal.model_copy(update={"result": result})
            self._write_journal(updated)
            return result
        raise LightningError("LND payment stream ended without a usable payment update")

    async def _map_payment(
        self, payment: Any, journal: _PaymentJournal
    ) -> SinglePartPaymentResult | None:
        terms = journal.request.prepared.terms
        expected_hash = terms.payment_hash
        if str(payment.payment_hash) != expected_hash:
            raise ChannelInputEscrowError("LND payment hash does not match escrow terms")
        if payment.payment_request and str(payment.payment_request) != terms.invoice:
            raise ChannelInputEscrowError("LND payment request does not match escrow terms")
        self._validate_payment_value(payment, terms.invoice_amount_msat)

        from jmswap.lndrpc import lightning_pb2 as ln

        if payment.status == ln.Payment.INITIATED:
            return None
        observed_height = await self._height_source.block_height()
        if payment.status == ln.Payment.IN_FLIGHT:
            attempts = [
                attempt for attempt in payment.htlcs if attempt.status == ln.HTLCAttempt.IN_FLIGHT
            ]
            if len(attempts) != 1:
                raise ChannelInputEscrowError(
                    "in-flight escrow payment requires one in-flight attempt"
                )
            route = self._route_evidence(attempts[0], journal)
            return self._result(PaymentStatus.PENDING, route, journal, observed_height)
        if payment.status == ln.Payment.SUCCEEDED:
            attempts = [
                attempt for attempt in payment.htlcs if attempt.status == ln.HTLCAttempt.SUCCEEDED
            ]
            if len(attempts) != 1:
                raise ChannelInputEscrowError(
                    "succeeded escrow payment requires one succeeded attempt"
                )
            preimage = bytes(attempts[0].preimage)
            if len(preimage) != 32 or hashlib.sha256(preimage).hexdigest() != expected_hash:
                raise ChannelInputEscrowError("LND payment preimage does not match escrow hash")
            route = self._route_evidence(attempts[0], journal)
            return self._result(
                PaymentStatus.SUCCEEDED,
                route,
                journal,
                observed_height,
                preimage=preimage.hex(),
                settled_height=observed_height,
            )
        if payment.status == ln.Payment.FAILED:
            failed_attempts = [
                attempt
                for attempt in payment.htlcs
                if attempt.status == ln.HTLCAttempt.FAILED and attempt.HasField("route")
            ]
            failed_route: EscrowRouteEvidence | None = None
            if failed_attempts:
                failed_route = self._route_evidence(
                    max(enumerate(failed_attempts), key=lambda item: (item[1].attempt_id, item[0]))[
                        1
                    ],
                    journal,
                )
            return self._result(PaymentStatus.FAILED, failed_route, journal, observed_height)
        raise ChannelInputEscrowError("LND returned an unknown escrow payment status")

    async def _pre_submission_failure(
        self, request: SinglePartPaymentRequest
    ) -> SinglePartPaymentResult:
        observed_height = max(
            request.requested_at_height,
            await self._height_source.block_height(),
        )
        result = SinglePartPaymentResult(
            payment_id=request.prepared.terms.payment_hash,
            status=PaymentStatus.FAILED,
            attempt_height=request.requested_at_height,
            observed_height=observed_height,
        )
        result.validate_against(request.prepared.terms)
        return result

    async def _persist_pre_submission_failure(
        self, journal: _PaymentJournal
    ) -> SinglePartPaymentResult:
        result = await self._pre_submission_failure(journal.request)
        self._write_journal(journal.model_copy(update={"result": result}))
        return result

    @staticmethod
    def _validate_payment_value(payment: Any, expected_msat: int) -> None:
        populated_values = (
            (int(payment.value_msat), expected_msat),
            (int(payment.value_sat), expected_msat // 1000),
            (int(payment.value), expected_msat // 1000),
        )
        if any(value and value != expected for value, expected in populated_values):
            raise ChannelInputEscrowError("LND payment value does not match escrow invoice")

    def _route_evidence(self, attempt: Any, journal: _PaymentJournal) -> EscrowRouteEvidence:
        if not attempt.HasField("route"):
            raise ChannelInputEscrowError("LND payment attempt lacks route evidence")
        route = attempt.route
        route_ids = tuple(str(hop.chan_id) for hop in route.hops if hop.chan_id)
        if not route_ids or len(route_ids) != len(route.hops) or route.total_time_lock <= 0:
            raise ChannelInputEscrowError("LND returned an empty or malformed escrow route")
        if route.hops[-1].expiry <= 0:
            raise ChannelInputEscrowError("LND returned an invalid final route expiry")
        allowed_first_hops = set(journal.allowed_route_channel_ids)
        excluded = set(journal.excluded_route_channel_ids)
        if route_ids[0] not in allowed_first_hops or set(route_ids) & excluded:
            raise ChannelInputEscrowError("LND route uses an excluded or unknown channel")
        evidence = EscrowRouteEvidence(
            part_count=1,
            route_channel_ids=route_ids,
            final_expiry_height=int(route.hops[-1].expiry),
            outgoing_expiry_height=int(route.total_time_lock),
        )
        evidence.validate_against(journal.request.prepared.terms)
        return evidence

    @staticmethod
    def _result(
        status: PaymentStatus,
        route: EscrowRouteEvidence | None,
        journal: _PaymentJournal,
        observed_height: int,
        *,
        preimage: str | None = None,
        settled_height: int | None = None,
    ) -> SinglePartPaymentResult:
        result = SinglePartPaymentResult(
            payment_id=journal.request.prepared.terms.payment_hash,
            status=status,
            preimage=preimage,
            attempt_height=journal.request.requested_at_height,
            observed_height=observed_height,
            settled_height=settled_height,
            route=route,
        )
        result.validate_against(journal.request.prepared.terms)
        return result

    @staticmethod
    def _cltv_limit(request: SinglePartPaymentRequest) -> int:
        terms = request.prepared.terms
        strictest_deadline = min(
            terms.timeout_height - terms.safety_delta_blocks,
            terms.success_broadcast_deadline - terms.preimage_budget_blocks,
        )
        cltv_limit = strictest_deadline - request.requested_at_height
        if cltv_limit <= 0 or cltv_limit < terms.invoice_min_final_cltv_delta:
            raise ChannelInputEscrowError("escrow CLTV budget cannot cover the invoice final delta")
        return cltv_limit

    def _journal_path(self, session_id: str) -> Path:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return self._journal_dir / f"{digest}.json"

    async def _acquire_journal_lock(self, session_id: str) -> int:
        lock_path = self._journal_path(session_id).with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.fchmod(descriptor, 0o600)
        try:
            import asyncio

            await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @staticmethod
    def _release_journal_lock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def _load_journal(self, session_id: str) -> _PaymentJournal | None:
        path = self._journal_path(session_id)
        if not path.exists():
            return None
        try:
            journal = _PaymentJournal.model_validate_json(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - corrupted journals must fail closed
            raise ChannelInputEscrowError("escrow payment journal is invalid") from exc
        if journal.session_id != session_id:
            raise ChannelInputEscrowError("escrow payment journal session does not match")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ChannelInputEscrowError("escrow payment journal has unsafe permissions")
        return journal

    def _write_journal(self, journal: _PaymentJournal) -> None:
        path = self._journal_path(journal.session_id)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=self._journal_dir, prefix=f".{path.stem}.", suffix=".tmp"
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as file_handle:
                file_handle.write(journal.model_dump_json().encode("ascii"))
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(temporary_path, path)
            directory_fd = os.open(self._journal_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_path)
            raise

    @staticmethod
    def _validate_monotone_result(
        previous: SinglePartPaymentResult, current: SinglePartPaymentResult
    ) -> None:
        if (
            current.payment_id != previous.payment_id
            or current.attempt_height != previous.attempt_height
        ):
            raise ChannelInputEscrowError("LND payment observation changed the payment identity")
        if current.observed_height < previous.observed_height:
            raise ChannelInputEscrowError("LND payment observation height moved backwards")
        if previous.status is not PaymentStatus.PENDING and current != previous:
            raise ChannelInputEscrowError("terminal escrow payment result cannot change")

    @staticmethod
    def _rpc_code_name(exc: Exception) -> str | None:
        code = getattr(exc, "code", None)
        if not callable(code):
            return None
        try:
            value = code()
        except Exception:  # noqa: BLE001 - error inspection must not mask the RPC failure
            return None
        return getattr(value, "name", None)
