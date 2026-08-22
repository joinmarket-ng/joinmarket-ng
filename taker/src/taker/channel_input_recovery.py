"""Bounded startup reconciliation for durable taker channel-input records."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from jmcore.bitcoin import get_txid
from jmcore.channel_input_config import ChannelInputConfig
from jmswap.channel_input import InitializedChannelInputBackend
from jmswap.lnd import EscrowOutpoint

from taker.channel_input_endpoints import LocalChannelInputEndpoint, RemoteChannelInputEndpoint
from taker.channel_input_negotiation import TakerChannelInputNegotiation
from taker.channel_input_parent import (
    ChannelInputEndpoint,
    ChannelInputParentChain,
    ChannelInputParentCoordinator,
    ChannelInputParentRecord,
    ChannelInputParentState,
    ChannelInputRestartAction,
)
from taker.channel_input_runtime import TakerChannelInputRegistry
from taker.channel_input_transport import TakerChannelInputTransport
from taker.multi_directory import ReplyRoute

if TYPE_CHECKING:
    from jmswap.lnd import ChannelInputForceCloseOutcome


class TakerChannelInputRecoveryError(Exception):
    """Startup reconciliation found durable channel-input state that cannot advance safely."""


class _DirectoryClient(Protocol):
    def recovery_bootstrap_routes(self, nick: str) -> tuple[ReplyRoute, ...]: ...


class _HeightSource(Protocol):
    async def get_block_height(self) -> int: ...


@dataclass(frozen=True)
class _RecoveryMaker:
    """Roster placeholder, never a live maker session or encryption source."""

    comm_channel: str = "recovery-only"
    crypto: None = None


@dataclass(frozen=True)
class _RecoverySession:
    """Minimal transport session which permits only authenticated recovery routes."""

    directory_client: _DirectoryClient
    maker_sessions: Mapping[str, _RecoveryMaker]


class _RecoveryRequiredEndpoint:
    """Stops a query-first decision only when it needs a peer endpoint."""

    async def channel_input_escrow_status(self, *_args: object, **_kwargs: object) -> Any:
        raise _PeerRecoveryRequiredError


class _PeerRecoveryRequiredError(Exception):
    """The exact parent was not observed and peer recovery is now required."""


_TERMINAL_STATES = frozenset(
    {
        ChannelInputParentState.CANCELED,
        ChannelInputParentState.OBSERVED,
        ChannelInputParentState.CONFIRMED,
    }
)


async def reconcile_taker_channel_input_records(
    config: ChannelInputConfig,
    registry: TakerChannelInputRegistry,
    initialized_backend: InitializedChannelInputBackend,
    chain_backend: ChannelInputParentChain,
    directory_client: _DirectoryClient,
    parent_record_directory: Path,
    *,
    parent_coordinator: ChannelInputParentCoordinator | None = None,
) -> None:
    """Reconcile all durable taker attempts once, before accepting new work.

    Parent records are queried before any recovery handshake.  Every peer operation
    uses a newly authenticated recovery transport, never a retained live session.
    """

    coordinator = parent_coordinator or ChannelInputParentCoordinator(parent_record_directory)
    records = coordinator.load_all()
    active = {
        (negotiation.invite.session_id, negotiation.invite.attempt): negotiation
        for negotiation in registry.active_negotiations
    }
    by_session: dict[str, tuple[TakerChannelInputNegotiation, ...]] = {}
    for negotiation in active.values():
        by_session.setdefault(negotiation.invite.session_id, ())
        by_session[negotiation.invite.session_id] += (negotiation,)

    parent_sessions = {record.plan.session_id for record in records}
    for record in records:
        if record.state in _TERMINAL_STATES:
            continue
        if record.force_closes:
            height = await _current_height(cast(_HeightSource, chain_backend))
            await _continue_force_close(
                coordinator, record, initialized_backend, chain_backend, height
            )
            continue
        if record.final_tx_hex is not None:
            height = await _current_height(cast(_HeightSource, chain_backend))
            action = await coordinator.decide_restart(
                record.plan.session_id, {}, chain_backend, current_height=height
            )
            if action is ChannelInputRestartAction.NONE:
                continue
            if action is ChannelInputRestartAction.PUBLISH_EXACT_PARENT:
                await coordinator.publish(record.plan.session_id, chain_backend)
                continue
            if action is ChannelInputRestartAction.FORCE_CLOSE:
                await _force_close_missing(coordinator, record, initialized_backend, height)
                continue
            raise TakerChannelInputRecoveryError(
                "finalized channel parent returned a peer-dependent restart action"
            )
        negotiation = _exact_negotiation(record, by_session)
        _validate_record_negotiation(record, negotiation)
        await _reconcile_parent_record(
            config,
            coordinator,
            record,
            negotiation,
            registry,
            initialized_backend,
            chain_backend,
            directory_client,
        )

    for negotiation in registry.active_negotiations:
        if negotiation.invite.session_id in parent_sessions:
            continue
        transport = _recovery_transport(
            config, registry, initialized_backend, directory_client, negotiation
        )
        await _establish_all_recovery_routes(transport, directory_client, negotiation)
        canceled = await transport.cancel_or_replay(
            negotiation.invite.session_id, negotiation.invite.attempt, "startup_recovery"
        )
        if not canceled:
            raise TakerChannelInputRecoveryError(
                "durable channel-input negotiation cancellation was incomplete"
            )


async def _reconcile_parent_record(
    config: ChannelInputConfig,
    coordinator: ChannelInputParentCoordinator,
    record: ChannelInputParentRecord,
    negotiation: TakerChannelInputNegotiation,
    registry: TakerChannelInputRegistry,
    initialized_backend: InitializedChannelInputBackend,
    chain_backend: ChannelInputParentChain,
    directory_client: _DirectoryClient,
) -> None:
    height = await _current_height(cast(_HeightSource, chain_backend))
    query_endpoints = {
        contribution.endpoint_id: cast(ChannelInputEndpoint, _RecoveryRequiredEndpoint())
        for channel in record.plan.channels
        for contribution in (channel.first, channel.second)
    }
    try:
        action = await coordinator.decide_restart(
            record.plan.session_id, query_endpoints, chain_backend, current_height=height
        )
    except _PeerRecoveryRequiredError:
        action = None

    if action is ChannelInputRestartAction.NONE:
        return
    if action is ChannelInputRestartAction.PUBLISH_EXACT_PARENT:
        await coordinator.publish(record.plan.session_id, chain_backend)
        return
    if action is ChannelInputRestartAction.FORCE_CLOSE:
        await _force_close_missing(coordinator, record, initialized_backend, height)
        return

    transport = _recovery_transport(
        config, registry, initialized_backend, directory_client, negotiation
    )
    await _establish_all_recovery_routes(transport, directory_client, negotiation)
    endpoints = _endpoint_map(record, negotiation, registry, initialized_backend, transport)
    if action is None:
        action = await coordinator.decide_restart(
            record.plan.session_id, endpoints, chain_backend, current_height=height
        )
    if action is ChannelInputRestartAction.NONE:
        return
    if action is ChannelInputRestartAction.CANCEL_PRE_SIGN:
        canceled = await coordinator.cancel(record.plan.session_id, endpoints)
        if canceled.state is not ChannelInputParentState.CANCELED:
            raise TakerChannelInputRecoveryError("channel-parent cancellation was incomplete")
        return
    if action is ChannelInputRestartAction.RESUME_EXACT_PARENT:
        resumed = await coordinator.resume(record.plan.session_id, endpoints)
        if resumed.final_tx_hex is None or resumed.final_txid is None:
            raise TakerChannelInputRecoveryError(
                "exact parent resume did not produce a final transaction"
            )
        await coordinator.publish(record.plan.session_id, chain_backend)
        return
    if action is ChannelInputRestartAction.PUBLISH_EXACT_PARENT:
        await coordinator.publish(record.plan.session_id, chain_backend)
        return
    raise TakerChannelInputRecoveryError("channel-parent restart action is unsupported")


def _recovery_transport(
    config: ChannelInputConfig,
    registry: TakerChannelInputRegistry,
    initialized_backend: InitializedChannelInputBackend,
    directory_client: _DirectoryClient,
    negotiation: TakerChannelInputNegotiation,
) -> TakerChannelInputTransport:
    session = _RecoverySession(
        directory_client=directory_client,
        maker_sessions={nick: _RecoveryMaker() for nick in negotiation.selected_nicks},
    )
    return TakerChannelInputTransport(
        session=cast(Any, session),
        registry=registry,
        initialized_backend=initialized_backend,
        config=config,
        settlement_script_allocator=lambda _nick: "5120" + "00" * 32,
        current_time=lambda: int(time.time()),
        current_height=0,
    )


async def _establish_all_recovery_routes(
    transport: TakerChannelInputTransport,
    directory_client: _DirectoryClient,
    negotiation: TakerChannelInputNegotiation,
) -> None:
    for nick in negotiation.selected_nicks:
        routes = directory_client.recovery_bootstrap_routes(nick)
        if not routes:
            raise TakerChannelInputRecoveryError("maker has no recovery bootstrap route")
        failure: Exception | None = None
        for route in routes:
            if route.nick != nick:
                raise TakerChannelInputRecoveryError(
                    "recovery bootstrap route belongs to another maker"
                )
            try:
                await transport.establish_recovery(
                    negotiation.invite.session_id, negotiation.invite.attempt, nick, route
                )
                break
            except Exception as exc:
                failure = exc
        else:
            raise TakerChannelInputRecoveryError(
                "could not establish authenticated maker recovery transport"
            ) from failure


def _exact_negotiation(
    record: ChannelInputParentRecord,
    by_session: Mapping[str, tuple[TakerChannelInputNegotiation, ...]],
) -> TakerChannelInputNegotiation:
    candidates = by_session.get(record.plan.session_id, ())
    if len(candidates) != 1:
        raise TakerChannelInputRecoveryError(
            "nonterminal channel-parent record has no exact durable negotiation attempt"
        )
    return candidates[0]


def _validate_record_negotiation(
    record: ChannelInputParentRecord, negotiation: TakerChannelInputNegotiation
) -> None:
    if (
        negotiation.invite.session_id != record.plan.session_id
        or negotiation.prepare != record.plan.authorization.prepare
        or negotiation.transcript is None
        or negotiation.transcript.unsigned_parent_tx != record.plan.unsigned_parent_hex
    ):
        raise TakerChannelInputRecoveryError(
            "channel-parent record does not match the durable negotiation attempt"
        )
    remote_hellos = dict(negotiation.remote_hellos)
    local_hellos = dict(negotiation.local_hellos)
    if set(remote_hellos) != set(negotiation.selected_nicks) or set(local_hellos) != set(
        negotiation.selected_nicks
    ):
        raise TakerChannelInputRecoveryError("durable negotiation has incomplete endpoint hellos")
    remote_by_key = {hello.participant_key: nick for nick, hello in remote_hellos.items()}
    if len(remote_by_key) != len(remote_hellos) or len(record.plan.channels) != len(remote_hellos):
        raise TakerChannelInputRecoveryError("channel-parent endpoint mapping is not one-to-one")
    endpoint_ids: set[str] = set()
    mapped_nicks: set[str] = set()
    descriptors = {
        descriptor.input_index: descriptor for descriptor in negotiation.transcript.channels
    }
    records_by_index = {channel.channel_input_index: channel for channel in record.channels}
    if record.channels and len(records_by_index) != len(record.plan.channels):
        raise TakerChannelInputRecoveryError(
            "channel-parent frozen channel records are inconsistent"
        )
    if record.state is not ChannelInputParentState.CREATED and len(records_by_index) != len(
        record.plan.channels
    ):
        raise TakerChannelInputRecoveryError("channel-parent is missing frozen channel records")
    frozen_inputs = dict(negotiation.frozen_inputs) if record.channels else {}
    for channel in record.plan.channels:
        nick = remote_by_key.get(channel.first.participant_key)
        descriptor = descriptors.get(channel.channel_input_index)
        if (
            nick is None
            or descriptor is None
            or channel.second.participant_key != negotiation.coordinator_key
            or local_hellos[nick].participant_key != negotiation.coordinator_key
            or descriptor.first_participant_key != channel.first.participant_key
            or descriptor.second_participant_key != channel.second.participant_key
            or descriptor.funding_outpoint != remote_hellos[nick].funding_outpoint
            or channel.first.endpoint_id in endpoint_ids
            or channel.second.endpoint_id in endpoint_ids
        ):
            raise TakerChannelInputRecoveryError(
                "channel-parent endpoint signer mapping is invalid"
            )
        endpoint_ids.update((channel.first.endpoint_id, channel.second.endpoint_id))
        mapped_nicks.add(nick)
        if record.channels:
            frozen = frozen_inputs.get(nick)
            channel_record = records_by_index.get(channel.channel_input_index)
            if (
                frozen is None
                or channel_record is None
                or channel_record.second_snapshot.funding_txid
                != frozen.snapshot.funding_outpoint.txid
                or channel_record.second_snapshot.funding_vout
                != frozen.snapshot.funding_outpoint.vout
            ):
                raise TakerChannelInputRecoveryError(
                    "channel-parent local frozen channel differs from durable negotiation"
                )
    if mapped_nicks != set(negotiation.selected_nicks):
        raise TakerChannelInputRecoveryError(
            "channel-parent does not cover the selected maker roster"
        )


def _endpoint_map(
    record: ChannelInputParentRecord,
    negotiation: TakerChannelInputNegotiation,
    registry: TakerChannelInputRegistry,
    initialized_backend: InitializedChannelInputBackend,
    transport: TakerChannelInputTransport,
) -> dict[str, ChannelInputEndpoint]:
    remote_by_key = {
        hello.participant_key: nick for nick, hello in dict(negotiation.remote_hellos).items()
    }
    endpoints: dict[str, ChannelInputEndpoint] = {}
    for channel in record.plan.channels:
        nick = remote_by_key.get(channel.first.participant_key)
        if nick is None:
            raise TakerChannelInputRecoveryError(
                "channel-parent endpoint signer mapping is invalid"
            )
        for endpoint_id, endpoint in (
            (
                channel.first.endpoint_id,
                RemoteChannelInputEndpoint(
                    registry=registry,
                    transport=transport,
                    initialized_backend=initialized_backend,
                    session_id=negotiation.invite.session_id,
                    attempt=negotiation.invite.attempt,
                    nick=nick,
                ),
            ),
            (
                channel.second.endpoint_id,
                LocalChannelInputEndpoint(
                    registry=registry,
                    transport=transport,
                    initialized_backend=initialized_backend,
                    session_id=negotiation.invite.session_id,
                    attempt=negotiation.invite.attempt,
                    nick=nick,
                ),
            ),
        ):
            if endpoint_id in endpoints:
                raise TakerChannelInputRecoveryError("channel-parent endpoint ID is duplicated")
            endpoints[endpoint_id] = endpoint
    return endpoints


async def _current_height(chain_backend: _HeightSource) -> int:
    height = await chain_backend.get_block_height()
    if isinstance(height, bool) or not isinstance(height, int) or not 0 <= height <= 0xFFFFFFFF:
        raise TakerChannelInputRecoveryError("chain backend returned an invalid current height")
    return height


async def _continue_force_close(
    coordinator: ChannelInputParentCoordinator,
    record: ChannelInputParentRecord,
    initialized_backend: InitializedChannelInputBackend,
    chain_backend: ChannelInputParentChain,
    current_height: int,
) -> None:
    observed = await coordinator.observe_restart(record.plan.session_id, chain_backend)
    if observed.state in {
        ChannelInputParentState.OBSERVED,
        ChannelInputParentState.CONFIRMED,
    }:
        return
    recovery_height = record.plan.authorization.prepare.approval_set.transcript.recovery_height
    if current_height < recovery_height:
        raise TakerChannelInputRecoveryError(
            "partial force-close record conflicts with exact recovery"
        )
    await _force_close_missing(coordinator, observed, initialized_backend, current_height)


async def _force_close_missing(
    coordinator: ChannelInputParentCoordinator,
    record: ChannelInputParentRecord,
    initialized_backend: InitializedChannelInputBackend,
    current_height: int,
) -> None:
    recovery_height = record.plan.authorization.prepare.approval_set.transcript.recovery_height
    if current_height < recovery_height:
        raise TakerChannelInputRecoveryError("force close conflicts with exact parent evidence")
    covered = {(item.funding_txid, item.funding_vout) for item in record.force_closes}
    parent_txid = get_txid(record.plan.unsigned_parent_hex)
    for channel in record.channels:
        snapshot = channel.second_snapshot
        point = (snapshot.funding_txid, snapshot.funding_vout)
        if point in covered:
            continue
        outcome: ChannelInputForceCloseOutcome = (
            await initialized_backend.backend.force_close_channel_input_recovery(
                record.plan.session_id,
                EscrowOutpoint(txid=snapshot.funding_txid, vout=snapshot.funding_vout),
                parent_txid,
                recovery_height,
                current_height,
            )
        )
        coordinator.record_force_close(record.plan.session_id, outcome)
