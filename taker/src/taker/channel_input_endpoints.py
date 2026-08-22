"""Local LND and remote maker adapters for durable channel-input parents."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from jmcore.bitcoin import get_txid, parse_transaction
from jmcore.channel_input import (
    ChannelInputFinalizedPayload,
    ChannelInputPartialPayload,
    ChannelInputPreparedPayload,
    ChannelInputStatusPayload,
    Outpoint,
    encode_channel_input_message,
)
from jmswap.channel_input import InitializedChannelInputBackend
from jmswap.lnd import (
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
    WitnessUtxo,
)

from taker.channel_input_negotiation import (
    TakerChannelInputNegotiation,
    TakerChannelInputParentChannelState,
    TakerChannelInputStage,
)
from taker.channel_input_runtime import TakerChannelInputRegistry
from taker.channel_input_transport import TakerChannelInputTransport


class TakerChannelInputEndpointError(Exception):
    """A local or remote channel-input endpoint could not safely advance."""


class _ChannelInputEndpointBase:
    def __init__(
        self,
        *,
        registry: TakerChannelInputRegistry,
        transport: TakerChannelInputTransport,
        initialized_backend: InitializedChannelInputBackend,
        session_id: str,
        attempt: int,
        nick: str,
    ) -> None:
        self._registry = registry
        self._transport = transport
        self._initialized_backend = initialized_backend
        self._session_id = session_id
        self._attempt = attempt
        self._nick = nick

    def _negotiation(self) -> TakerChannelInputNegotiation:
        negotiation = self._registry.get(self._session_id, self._attempt)
        if negotiation is None:
            raise TakerChannelInputEndpointError("unknown channel-input endpoint attempt")
        if self._nick not in negotiation.selected_nicks:
            raise TakerChannelInputEndpointError("endpoint maker is not selected")
        if (
            negotiation.stage is not TakerChannelInputStage.PREPARE_READY
            or negotiation.prepare is None
            or negotiation.transcript is None
            or negotiation.prepare != negotiation._exact_prepare()
        ):
            raise TakerChannelInputEndpointError("endpoint requires durable prepare authorization")
        return negotiation

    def _state(self) -> TakerChannelInputParentChannelState:
        negotiation = self._negotiation()
        state = next(
            (item for item in negotiation.parent_channel_states if item.nick == self._nick), None
        )
        if state is None:
            raise TakerChannelInputEndpointError("endpoint has no durable post-prepare state")
        return state

    def _frozen(self) -> FrozenChannelSnapshot:
        negotiation = self._negotiation()
        frozen = dict(negotiation.frozen_inputs).get(self._nick)
        if frozen is None:
            raise TakerChannelInputEndpointError("endpoint has no durable freeze evidence")
        return frozen.snapshot

    def _plan_channel_id(self) -> str:
        frozen = self._frozen()
        return f"channel-input:{frozen.funding_outpoint.txid}:{frozen.funding_outpoint.vout}"

    @staticmethod
    def _outpoint_matches(actual: EscrowOutpoint, expected: Outpoint) -> bool:
        return actual.txid == expected.txid and actual.vout == expected.vout

    def _validate_session_and_outpoint(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> TakerChannelInputParentChannelState:
        if session_id != self._session_id:
            raise TakerChannelInputEndpointError("endpoint session differs from durable session")
        state = self._state()
        if not self._outpoint_matches(funding_outpoint, state.funding_outpoint):
            raise TakerChannelInputEndpointError(
                "endpoint funding outpoint differs from durable channel"
            )
        return state

    def _validate_prepare_call(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo],
        channel_input_index: int,
    ) -> TakerChannelInputNegotiation:
        if session_id != self._session_id:
            raise TakerChannelInputEndpointError("endpoint session differs from durable session")
        negotiation = self._negotiation()
        transcript = negotiation.transcript
        assert transcript is not None
        expected_prevouts = tuple(
            WitnessUtxo(value_sat=item.value_sat, script_pubkey=bytes.fromhex(item.script_pubkey))
            for item in transcript.input_prevouts
        )
        descriptor = next(
            (
                item
                for item in transcript.channels
                if item.funding_outpoint
                == dict(negotiation.remote_hellos)[self._nick].funding_outpoint
            ),
            None,
        )
        if (
            descriptor is None
            or not self._outpoint_matches(funding_outpoint, descriptor.funding_outpoint)
            or parent_tx_hex != transcript.unsigned_parent_tx
            or tuple(input_utxos) != expected_prevouts
            or channel_input_index != descriptor.input_index
        ):
            raise TakerChannelInputEndpointError("prepare call differs from durable transcript")
        return negotiation

    def _validate_attempt(
        self, session_id: str, funding_outpoint: EscrowOutpoint, attempt: LndEscrowSigningAttempt
    ) -> TakerChannelInputParentChannelState:
        state = self._validate_session_and_outpoint(session_id, funding_outpoint)
        if len(attempt.attempt_id) != 32 or len(attempt.public_nonce) != 66:
            raise TakerChannelInputEndpointError("endpoint signing attempt has invalid dimensions")
        return state

    def _descriptor_index(self) -> int:
        negotiation = self._negotiation()
        assert negotiation.transcript is not None
        outpoint = dict(negotiation.remote_hellos)[self._nick].funding_outpoint
        descriptor = next(
            (item for item in negotiation.transcript.channels if item.funding_outpoint == outpoint),
            None,
        )
        if descriptor is None:
            raise TakerChannelInputEndpointError("durable transcript lacks endpoint channel")
        return descriptor.input_index

    def _synthetic_remote_attempt(
        self, prepared: ChannelInputPreparedPayload
    ) -> LndEscrowSigningAttempt:
        return LndEscrowSigningAttempt(
            attempt_id=hashlib.sha256(
                encode_channel_input_message(prepared).encode("ascii")
            ).digest(),
            public_nonce=bytes.fromhex(prepared.public_nonce),
        )

    @staticmethod
    def _finalized_parent(
        payload: ChannelInputFinalizedPayload, index: int
    ) -> LndFinalizedEscrowParent:
        transaction = parse_transaction(payload.channel_signed_parent_tx)
        witnesses = [witness for witness in transaction.witnesses if witness]
        if len(witnesses) != 1 or len(witnesses[0]) != 1:
            raise TakerChannelInputEndpointError("maker final parent has invalid channel witness")
        return LndFinalizedEscrowParent(
            tx_hex=payload.channel_signed_parent_tx,
            txid=payload.txid,
            channel_input_index=index,
            channel_input_witness=(witnesses[0][0],),
        )


class LocalChannelInputEndpoint(_ChannelInputEndpointBase):
    """Adapter for the taker's local ChannelEscrowRPC endpoint."""

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        frozen = self._frozen()
        if session_id != self._session_id or channel_id != self._plan_channel_id():
            raise TakerChannelInputEndpointError("freeze call differs from durable local evidence")
        return frozen

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo],
        channel_input_index: int,
    ) -> str:
        self._validate_prepare_call(
            session_id, funding_outpoint, parent_tx_hex, input_utxos, channel_input_index
        )
        try:
            return await self._initialized_backend.backend.prepare_channel_input_parent(
                session_id, funding_outpoint, parent_tx_hex, input_utxos, channel_input_index
            )
        except Exception as exc:
            raise TakerChannelInputEndpointError("local LND prepare failed") from exc

    async def begin_channel_input_signing(
        self, session_id: str, funding_outpoint: EscrowOutpoint, *, restart_attempt: bool = False
    ) -> LndEscrowSigningAttempt:
        state = self._validate_session_and_outpoint(session_id, funding_outpoint)
        if state.local_prepared is not None and not restart_attempt:
            assert state.local_attempt_id is not None
            return LndEscrowSigningAttempt(
                attempt_id=bytes.fromhex(state.local_attempt_id),
                public_nonce=bytes.fromhex(state.local_prepared.public_nonce),
            )
        try:
            if restart_attempt:
                await self._registry.plan_local_prepared_rotation(
                    self._session_id, self._attempt, self._nick
                )
            signing_attempt = await self._initialized_backend.backend.begin_channel_input_signing(
                session_id, funding_outpoint, restart_attempt=restart_attempt
            )
            await self._registry.record_local_prepared(
                self._session_id, self._attempt, self._nick, signing_attempt
            )
            return signing_attempt
        except TakerChannelInputEndpointError:
            raise
        except Exception as exc:
            raise TakerChannelInputEndpointError("local LND signing attempt failed") from exc

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes:
        state = self._validate_attempt(session_id, funding_outpoint, attempt)
        if (
            state.local_attempt_id != attempt.attempt_id.hex()
            or state.local_prepared is None
            or state.local_prepared.public_nonce != attempt.public_nonce.hex()
            or state.remote_prepared is None
            or state.remote_prepared.public_nonce != remote_public_nonce.hex()
        ):
            raise TakerChannelInputEndpointError(
                "local sign call differs from durable nonce evidence"
            )
        try:
            request = await self._registry.build_sign_request(
                self._session_id, self._attempt, self._nick
            )
            state = self._state()
            if (
                state.sign_request != request
                or request.local_public_nonce != remote_public_nonce.hex()
                or state.local_prepared is None
                or request.peer_prepared != state.local_prepared
            ):
                raise TakerChannelInputEndpointError(
                    "local sign authorization differs from durable nonce evidence"
                )
            partial = await self._initialized_backend.backend.sign_channel_input_parent(
                session_id, funding_outpoint, attempt, remote_public_nonce
            )
            await self._registry.record_local_partial(
                self._session_id, self._attempt, self._nick, partial
            )
            return partial
        except Exception as exc:
            raise TakerChannelInputEndpointError("local LND sign failed") from exc

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        state = self._validate_attempt(session_id, funding_outpoint, attempt)
        if (
            state.local_attempt_id != attempt.attempt_id.hex()
            or state.local_prepared is None
            or state.local_prepared.public_nonce != attempt.public_nonce.hex()
            or state.remote_partial is None
            or state.remote_partial.partial_signature != remote_partial_signature.hex()
        ):
            raise TakerChannelInputEndpointError(
                "local finalize call differs from durable evidence"
            )
        try:
            request = await self._registry.build_finalize_request(
                self._session_id, self._attempt, self._nick
            )
            state = self._state()
            if (
                state.finalize_request != request
                or state.local_partial is None
                or request.peer_partial != state.local_partial
            ):
                raise TakerChannelInputEndpointError(
                    "local finalize authorization differs from durable partial evidence"
                )
            return await self._initialized_backend.backend.finalize_channel_input_parent(
                session_id, funding_outpoint, attempt, remote_partial_signature
            )
        except Exception as exc:
            raise TakerChannelInputEndpointError("local LND finalize failed") from exc

    async def recover_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        state = self._validate_session_and_outpoint(session_id, funding_outpoint)
        if (
            state.finalize_request is None
            or state.remote_partial is None
            or state.remote_partial.partial_signature != remote_partial_signature.hex()
            or state.local_partial is None
            or state.finalize_request.peer_partial != state.local_partial
        ):
            raise TakerChannelInputEndpointError(
                "local recovery differs from durable finalization evidence"
            )
        try:
            return await self._initialized_backend.backend.recover_channel_input_parent(
                session_id, funding_outpoint, remote_partial_signature
            )
        except Exception as exc:
            raise TakerChannelInputEndpointError("local LND recovery failed") from exc

    async def channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus:
        self._validate_session_and_outpoint(session_id, funding_outpoint)
        try:
            status = await self._initialized_backend.backend.find_channel_input_escrow_status(
                session_id, funding_outpoint
            )
            if status is None:
                raise TakerChannelInputEndpointError("local LND has no escrow status")
            state = self._state()
            if status.durable_local_partial_signature is not None and state.local_partial is None:
                await self._registry.record_local_partial(
                    self._session_id,
                    self._attempt,
                    self._nick,
                    status.durable_local_partial_signature,
                )
            return status
        except TakerChannelInputEndpointError:
            raise
        except Exception as exc:
            raise TakerChannelInputEndpointError("local LND status failed") from exc

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool:
        self._validate_session_and_outpoint(session_id, funding_outpoint)
        try:
            return await self._transport.cancel_or_replay(self._session_id, self._attempt)
        except Exception as exc:
            raise TakerChannelInputEndpointError("channel-input cancellation failed") from exc


class RemoteChannelInputEndpoint(_ChannelInputEndpointBase):
    """Adapter for one selected maker's encrypted post-prepare endpoint."""

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        frozen = self._frozen()
        if session_id != self._session_id or channel_id != self._plan_channel_id():
            raise TakerChannelInputEndpointError("freeze call differs from durable remote evidence")
        local_hello = dict(self._negotiation().local_hellos).get(self._nick)
        if local_hello is None:
            raise TakerChannelInputEndpointError("remote endpoint lacks durable local hello")
        return FrozenChannelSnapshot(
            session_id=frozen.session_id,
            channel_id=frozen.channel_id,
            funding_outpoint=frozen.funding_outpoint,
            funding_scriptpubkey=frozen.funding_scriptpubkey,
            peer_node_pubkey=bytes.fromhex(local_hello.node_key),
            local_funding_pubkey=frozen.remote_funding_pubkey,
            remote_funding_pubkey=frozen.local_funding_pubkey,
            capacity_sat=frozen.capacity_sat,
            local_claim_sat=frozen.remote_claim_sat,
            remote_claim_sat=frozen.local_claim_sat,
            quiescent=frozen.quiescent,
            active_htlcs=frozen.active_htlcs,
        )

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo],
        channel_input_index: int,
    ) -> str:
        negotiation = self._validate_prepare_call(
            session_id, funding_outpoint, parent_tx_hex, input_utxos, channel_input_index
        )
        assert negotiation.prepare is not None
        try:
            response = await self._transport.exchange_one(
                self._session_id,
                self._attempt,
                self._nick,
                negotiation.prepare,
                ChannelInputPreparedPayload,
            )
            await self._registry.accept_remote_prepared(
                self._session_id, self._attempt, self._nick, response
            )
            return get_txid(parent_tx_hex)
        except Exception as exc:
            raise TakerChannelInputEndpointError("remote maker prepare failed") from exc

    async def begin_channel_input_signing(
        self, session_id: str, funding_outpoint: EscrowOutpoint, *, restart_attempt: bool = False
    ) -> LndEscrowSigningAttempt:
        if restart_attempt:
            raise TakerChannelInputEndpointError("remote signing attempts cannot be rotated")
        state = self._validate_session_and_outpoint(session_id, funding_outpoint)
        if state.remote_prepared is None:
            raise TakerChannelInputEndpointError("remote endpoint lacks prepared nonce evidence")
        return self._synthetic_remote_attempt(state.remote_prepared)

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes:
        state = self._validate_attempt(session_id, funding_outpoint, attempt)
        if (
            state.remote_prepared is None
            or attempt != self._synthetic_remote_attempt(state.remote_prepared)
            or state.local_prepared is None
            or remote_public_nonce.hex() != state.local_prepared.public_nonce
        ):
            raise TakerChannelInputEndpointError(
                "remote sign call differs from durable nonce evidence"
            )
        try:
            request = await self._registry.build_sign_request(
                self._session_id, self._attempt, self._nick
            )
            response = await self._transport.exchange_one(
                self._session_id, self._attempt, self._nick, request, ChannelInputPartialPayload
            )
            await self._registry.accept_remote_partial(
                self._session_id, self._attempt, self._nick, response
            )
            return bytes.fromhex(response.partial_signature)
        except Exception as exc:
            raise TakerChannelInputEndpointError("remote maker sign failed") from exc

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        state = self._validate_attempt(session_id, funding_outpoint, attempt)
        if (
            state.remote_prepared is None
            or attempt != self._synthetic_remote_attempt(state.remote_prepared)
            or state.local_partial is None
            or state.local_partial.partial_signature != remote_partial_signature.hex()
        ):
            raise TakerChannelInputEndpointError(
                "remote finalize call differs from durable evidence"
            )
        try:
            request = await self._registry.build_finalize_request(
                self._session_id, self._attempt, self._nick
            )
            response = await self._transport.exchange_one(
                self._session_id, self._attempt, self._nick, request, ChannelInputFinalizedPayload
            )
            await self._registry.accept_remote_finalized(
                self._session_id, self._attempt, self._nick, response
            )
            return self._finalized_parent(response, self._descriptor_index())
        except Exception as exc:
            raise TakerChannelInputEndpointError("remote maker finalize failed") from exc

    async def recover_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        state = self._validate_session_and_outpoint(session_id, funding_outpoint)
        if state.remote_finalized is not None:
            return self._finalized_parent(state.remote_finalized, self._descriptor_index())
        if (
            state.local_partial is None
            or state.local_partial.partial_signature != remote_partial_signature.hex()
        ):
            raise TakerChannelInputEndpointError(
                "remote recovery differs from durable partial evidence"
            )
        attempt = await self.begin_channel_input_signing(session_id, funding_outpoint)
        return await self.finalize_channel_input_parent(
            session_id, funding_outpoint, attempt, remote_partial_signature
        )

    async def channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus:
        self._validate_session_and_outpoint(session_id, funding_outpoint)
        try:
            request = await self._registry.build_status_request(
                self._session_id, self._attempt, self._nick
            )
            response = await self._transport.exchange_one(
                self._session_id, self._attempt, self._nick, request, ChannelInputStatusPayload
            )
            await self._registry.accept_remote_status(
                self._session_id, self._attempt, self._nick, response
            )
            state = self._state()
            negotiation = self._negotiation()
            assert negotiation.transcript is not None
            stage = {
                "parent_prepared": LndEscrowStage.PARENT_PREPARED,
                "parent_signed": LndEscrowStage.PARENT_SIGNED,
                "finalized": LndEscrowStage.FINALIZED,
            }[response.escrow_stage]
            finalized = (
                self._finalized_parent(state.remote_finalized, self._descriptor_index())
                if state.remote_finalized is not None
                else None
            )
            active_attempt = (
                self._synthetic_remote_attempt(state.remote_prepared)
                if response.attempt_active and state.remote_prepared is not None
                else None
            )
            return LndEscrowStatus(
                stage=stage,
                parent_txid=get_txid(negotiation.transcript.unsigned_parent_tx),
                finalized=stage is LndEscrowStage.FINALIZED,
                durable_local_public_nonce=(
                    bytes.fromhex(state.remote_prepared.public_nonce)
                    if stage >= LndEscrowStage.PARENT_SIGNED and state.remote_prepared is not None
                    else None
                ),
                durable_remote_public_nonce=(
                    bytes.fromhex(state.local_prepared.public_nonce)
                    if stage >= LndEscrowStage.PARENT_SIGNED and state.local_prepared is not None
                    else None
                ),
                durable_local_partial_signature=(
                    bytes.fromhex(state.remote_partial.partial_signature)
                    if stage >= LndEscrowStage.PARENT_SIGNED and state.remote_partial is not None
                    else None
                ),
                finalized_parent=finalized,
                active_attempt=active_attempt,
            )
        except Exception as exc:
            raise TakerChannelInputEndpointError("remote maker status failed") from exc

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool:
        self._validate_session_and_outpoint(session_id, funding_outpoint)
        try:
            return await self._transport.cancel_or_replay(self._session_id, self._attempt)
        except Exception as exc:
            raise TakerChannelInputEndpointError("channel-input cancellation failed") from exc
