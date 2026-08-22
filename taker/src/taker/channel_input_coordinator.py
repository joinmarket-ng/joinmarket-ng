"""One live taker-owned channel-input v1 round through parent publication."""

from __future__ import annotations

import math
import secrets
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from jmcore.bitcoin import address_to_scriptpubkey
from jmcore.channel_input import ChannelInputKeyPair, ChannelInputPreparePayload
from jmcore.channel_input_config import ChannelInputConfig
from jmswap.channel_input import InitializedChannelInputBackend

from taker.channel_input_endpoints import LocalChannelInputEndpoint, RemoteChannelInputEndpoint
from taker.channel_input_negotiation import TakerChannelInputNegotiation, TakerChannelInputStage
from taker.channel_input_parent import (
    ChannelInputParentChain,
    ChannelInputParentCoordinator,
    ChannelInputParentPlan,
    ChannelInputParentRecord,
    ChannelInputParentState,
)
from taker.channel_input_planner import (
    ChannelInputParentDraft,
    authorize_channel_input_parent,
    derive_channel_input_parent_draft,
    plan_channel_input_contributions,
)
from taker.channel_input_runtime import TakerChannelInputRegistry
from taker.channel_input_transport import TakerChannelInputTransport
from taker.orderbook import calculate_cj_fee
from taker.tx_builder import ChannelTransactionContribution

if TYPE_CHECKING:
    from taker.coinjoin_session import CoinJoinSession


class TakerChannelInputCoordinatorError(Exception):
    """A live channel-input v1 round cannot safely advance."""


_RECOVERY_HEIGHT_WINDOW_BLOCKS = 4


class TakerChannelInputCoordinator:
    """Own one exact parent from authenticated hellos through publication."""

    def __init__(
        self,
        session: CoinJoinSession,
        config: ChannelInputConfig,
        registry: TakerChannelInputRegistry,
        initialized_backend: InitializedChannelInputBackend,
        chain_backend: ChannelInputParentChain,
        parent_record_directory: Path,
        *,
        clock: Callable[[], float] = time.time,
        current_height: int | Callable[[], int] | None = None,
        session_key: bytes | None = None,
        session_id: str | None = None,
        parent_coordinator: ChannelInputParentCoordinator | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self.registry = registry
        self.initialized_backend = initialized_backend
        self.chain_backend = chain_backend
        self.parent_coordinator = parent_coordinator or ChannelInputParentCoordinator(
            parent_record_directory
        )
        self._clock = clock
        self._current_height = current_height
        self._session_key = session_key
        self._configured_session_id = session_id
        self._session_id: str | None = None
        self._attempt: int | None = None
        self._transport: TakerChannelInputTransport | None = None
        self._negotiation: TakerChannelInputNegotiation | None = None
        self._contributions: tuple[ChannelTransactionContribution, ...] = ()
        self._draft: ChannelInputParentDraft | None = None
        self._prepare: ChannelInputPreparePayload | None = None
        self._plan: ChannelInputParentPlan | None = None
        self._final_record: ChannelInputParentRecord | None = None
        self._build_started = False
        self._abort_result: bool | None = None
        self._settlement_mixdepth: int | None = None
        self._settlement_addresses: dict[str, str] = {}
        self._proposal_height: int | None = None

    @property
    def session_id(self) -> str:
        if self._session_id is None:
            raise TakerChannelInputCoordinatorError("channel-input round has not been prepared")
        return self._session_id

    @property
    def attempt(self) -> int:
        if self._attempt is None:
            raise TakerChannelInputCoordinatorError("channel-input round has not been prepared")
        return self._attempt

    @property
    def transport(self) -> TakerChannelInputTransport:
        if self._transport is None:
            raise TakerChannelInputCoordinatorError("channel-input transport has not been created")
        return self._transport

    @property
    def contributions(self) -> tuple[ChannelTransactionContribution, ...]:
        return self._contributions

    async def prepare(self, destination: str, mixdepth: int) -> ChannelInputPreparePayload:
        """Persist an invitation, complete pre-prepare exchange, and retain its exact draft."""

        if self._prepare is not None:
            return self._prepare
        if self._build_started:
            raise TakerChannelInputCoordinatorError(
                "channel-input parent build was already attempted"
            )
        current_time = self._unix_time()
        secret = self._new_secret()
        session_id = self._new_session_id()
        attempt = 1
        self._settlement_mixdepth = self._destination_mixdepth(mixdepth)
        negotiation = await self.registry.create(
            selected_nicks=tuple(self.session.maker_sessions),
            secret_key=secret,
            session_id=session_id,
            attempt=attempt,
            expiry=current_time + math.ceil(6 * self.config.phase_timeout_seconds),
            min_recovery_delta=self.config.recovery_delay_blocks,
            max_recovery_delta=(self.config.recovery_delay_blocks + _RECOVERY_HEIGHT_WINDOW_BLOCKS),
        )
        self._session_id = session_id
        self._attempt = attempt
        self._negotiation = negotiation
        self._transport = TakerChannelInputTransport(
            session=self.session,
            registry=self.registry,
            initialized_backend=self.initialized_backend,
            config=self.config,
            settlement_script_allocator=self._allocate_settlement_script,
            current_time=self._unix_time,
            current_height=self._current_proposal_height,
        )
        await self.transport.exchange_hellos(session_id, attempt)
        reloaded = self.registry.get(session_id, attempt)
        if reloaded is None or reloaded.stage is not TakerChannelInputStage.HELLOS_READY:
            raise TakerChannelInputCoordinatorError(
                "channel-input hello exchange was not durably completed"
            )
        negotiation = reloaded
        self._negotiation = reloaded
        maker_fees = {
            nick: calculate_cj_fee(self.session.maker_sessions[nick].offer, self.session.cj_amount)
            for nick in negotiation.selected_nicks
        }
        self._contributions = plan_channel_input_contributions(negotiation, maker_fees)
        self._build_started = True
        built = await self.session._phase_build_tx(
            destination, mixdepth, channel_contributions=self._contributions
        )
        if not built:
            raise TakerChannelInputCoordinatorError("could not build the channel-input parent")
        current_height = await self._height()
        self._proposal_height = current_height
        self._draft = derive_channel_input_parent_draft(
            negotiation,
            self.session.unsigned_tx,
            self.session.tx_metadata,
            self._contributions,
            current_height,
            self.config.max_parent_fee_sat,
        )
        prepared = await self.transport.build_prepare(
            session_id, attempt, self._draft.transcript, self._draft.endpoint_terms
        )
        self._prepare = prepared.prepare
        return self._prepare

    async def finalize_parent(self) -> str:
        """Durably execute the one exact parent after ordinary witnesses are complete."""

        if self._final_record is not None:
            assert self._final_record.final_txid is not None
            return self._final_record.final_txid
        if self._draft is None or self._prepare is None or self._negotiation is None:
            raise TakerChannelInputCoordinatorError("channel-input prepare is not complete")
        if not self.session.ordinary_witnesses:
            raise TakerChannelInputCoordinatorError("ordinary parent witnesses are incomplete")
        if self._plan is None:
            self._plan = authorize_channel_input_parent(
                self._draft, self._prepare, self.session.ordinary_witnesses
            )
        endpoints = self._endpoint_map()
        self.session.signing_boundary_crossed = True
        record = await self.parent_coordinator.execute(self._plan, endpoints)
        if record.final_tx_hex is None or record.final_txid is None:
            raise TakerChannelInputCoordinatorError("channel-input parent was not finalized")
        self._final_record = record
        self.session.final_tx = bytes.fromhex(record.final_tx_hex)
        self.session.txid = record.final_txid
        return record.final_txid

    async def publish(self) -> str:
        """Query and publish the exact durable parent through its coordinator."""

        if self._final_record is None or self._final_record.final_txid is None:
            raise TakerChannelInputCoordinatorError("channel-input parent has not been finalized")
        inputs_valid, validation_error = await self.session._revalidate_inputs_before_broadcast()
        if not inputs_valid:
            raise TakerChannelInputCoordinatorError(
                f"refusing channel-input parent publication: {validation_error}"
            )
        if not self.session.renew_input_locks("broadcast channel-input parent"):
            raise TakerChannelInputCoordinatorError(
                "refusing channel-input parent publication after input lock renewal failure"
            )
        record = await self.parent_coordinator.publish(self.session_id, self.chain_backend)
        if record.final_txid != self._final_record.final_txid:
            raise TakerChannelInputCoordinatorError("published channel-input parent ID differs")
        return record.final_txid

    async def abort(self, reason_code: str) -> bool:
        """Cancel only a pre-partial round and return its durable cancellation result."""

        if self._abort_result is not None:
            if self._abort_result:
                self.session.signing_boundary_crossed = False
            return self._abort_result
        if self._session_id is None or self._attempt is None or self._transport is None:
            self._abort_result = False
            return False
        record = self.parent_coordinator.load(self._session_id)
        if record is not None and record.state is ChannelInputParentState.CANCELED:
            self._abort_result = True
            self.session.signing_boundary_crossed = False
            return True
        if self._has_partial_or_final_evidence(record):
            self._abort_result = False
            return False
        if record is not None:
            canceled = await self.parent_coordinator.cancel(self._session_id, self._endpoint_map())
            self._abort_result = canceled.state.value == "canceled"
        else:
            self._abort_result = await self._transport.cancel_or_replay(
                self._session_id, self._attempt, reason_code
            )
        if self._abort_result:
            self.session.signing_boundary_crossed = False
        return self._abort_result

    def _endpoint_map(self) -> Mapping[str, RemoteChannelInputEndpoint | LocalChannelInputEndpoint]:
        if self._negotiation is None:
            raise TakerChannelInputCoordinatorError("channel-input negotiation is not available")
        endpoints: dict[str, RemoteChannelInputEndpoint | LocalChannelInputEndpoint] = {}
        for nick, contribution in zip(
            self._negotiation.selected_nicks, self._contributions, strict=True
        ):
            for endpoint_id, endpoint in (
                (
                    contribution.first_endpoint.endpoint_id,
                    RemoteChannelInputEndpoint(
                        registry=self.registry,
                        transport=self.transport,
                        initialized_backend=self.initialized_backend,
                        session_id=self.session_id,
                        attempt=self.attempt,
                        nick=nick,
                    ),
                ),
                (
                    contribution.second_endpoint.endpoint_id,
                    LocalChannelInputEndpoint(
                        registry=self.registry,
                        transport=self.transport,
                        initialized_backend=self.initialized_backend,
                        session_id=self.session_id,
                        attempt=self.attempt,
                        nick=nick,
                    ),
                ),
            ):
                if endpoint_id in endpoints:
                    raise TakerChannelInputCoordinatorError(
                        "channel-input endpoint ID is duplicated"
                    )
                endpoints[endpoint_id] = endpoint
        return endpoints

    def _allocate_settlement_script(self, nick: str) -> str:
        if self._settlement_mixdepth is None:
            raise TakerChannelInputCoordinatorError(
                "settlement allocation has no destination mixdepth"
            )
        existing = self._settlement_addresses.get(nick)
        if existing is None:
            address = self.session.wallet.get_new_persisted_internal_address(
                self._settlement_mixdepth, f"channel-input:{self.session_id}:{nick}"
            )
            if address in self._settlement_addresses.values():
                raise TakerChannelInputCoordinatorError(
                    "wallet reused a channel-input settlement address"
                )
            self._settlement_addresses[nick] = address
        else:
            address = existing
        try:
            script = address_to_scriptpubkey(address)
        except (TypeError, ValueError) as exc:
            raise TakerChannelInputCoordinatorError(
                "wallet returned an invalid settlement address"
            ) from exc
        if len(script) != 34 or script[:2] != b"\x51\x20":
            raise TakerChannelInputCoordinatorError("wallet settlement address is not P2TR")
        return script.hex()

    def _destination_mixdepth(self, mixdepth: int) -> int:
        count = self.session.wallet.mixdepth_count
        if isinstance(mixdepth, bool) or not isinstance(mixdepth, int) or not 0 <= mixdepth < count:
            raise TakerChannelInputCoordinatorError("source mixdepth is invalid")
        return (mixdepth + 1) % count

    def _new_secret(self) -> bytes:
        secret = self._session_key or ChannelInputKeyPair.generate().secret_key
        try:
            ChannelInputKeyPair.from_secret(secret)
        except ValueError as exc:
            raise TakerChannelInputCoordinatorError("channel-input secret key is invalid") from exc
        return secret

    def _new_session_id(self) -> str:
        session_id = self._configured_session_id or secrets.token_hex(32)
        if len(session_id) != 64 or session_id.lower() != session_id:
            raise TakerChannelInputCoordinatorError("channel-input session ID is invalid")
        try:
            bytes.fromhex(session_id)
        except ValueError as exc:
            raise TakerChannelInputCoordinatorError("channel-input session ID is invalid") from exc
        return session_id

    def _unix_time(self) -> int:
        current_time = self._clock()
        if isinstance(current_time, bool) or not isinstance(current_time, (int, float)):
            raise TakerChannelInputCoordinatorError("clock returned an invalid UNIX time")
        return int(current_time)

    def _current_proposal_height(self) -> int:
        if self._proposal_height is None:
            raise TakerChannelInputCoordinatorError(
                "channel-input proposal height has not been captured"
            )
        return self._proposal_height

    async def _height(self) -> int:
        source = self._current_height
        height = source() if callable(source) else source
        if height is None:
            height = await self.session.backend.get_block_height()
        if isinstance(height, bool) or not isinstance(height, int) or not 0 <= height <= 0xFFFFFFFF:
            raise TakerChannelInputCoordinatorError("chain returned an invalid current height")
        return height

    def _has_partial_or_final_evidence(self, record: ChannelInputParentRecord | None) -> bool:
        negotiation = self.registry.get(self._session_id or "", self._attempt or 0)
        if negotiation is not None and any(
            state.local_partial is not None
            or state.remote_partial is not None
            or state.remote_finalized is not None
            for state in negotiation.parent_channel_states
        ):
            return True
        return record is not None and (
            record.final_tx_hex is not None
            or record.final_txid is not None
            or any(
                channel.witness is not None
                or any(
                    evidence is not None and evidence.partial_signature is not None
                    for evidence in (channel.first_evidence, channel.second_evidence)
                )
                for channel in record.channels
            )
        )
