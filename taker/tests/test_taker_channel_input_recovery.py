"""Startup reconciliation tests for durable taker channel-input state."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import ANY, AsyncMock, Mock

import pytest
from jmcore.channel_input_config import ChannelInputConfig
from jmswap.channel_input import InitializedChannelInputBackend

from taker.channel_input_negotiation import TakerChannelInputNegotiation
from taker.channel_input_parent import (
    ChannelInputParentChain,
    ChannelInputParentCoordinator,
    ChannelInputParentRecord,
    ChannelInputParentState,
    ChannelInputRestartAction,
)
from taker.channel_input_recovery import (
    TakerChannelInputRecoveryError,
    _DirectoryClient,
    _establish_all_recovery_routes,
    _validate_record_negotiation,
    reconcile_taker_channel_input_records,
)
from taker.channel_input_runtime import TakerChannelInputRegistry
from taker.channel_input_transport import TakerChannelInputTransport
from taker.multi_directory import ReplyRoute


class FakeRegistry:
    def __init__(self, negotiations: tuple[TakerChannelInputNegotiation, ...]) -> None:
        self.active_negotiations = negotiations


class FakeChain:
    def __init__(self, events: list[str], height: int = 100) -> None:
        self.events = events
        self.height = height

    async def get_block_height(self) -> int:
        self.events.append("height")
        return self.height


class FakeCoordinator:
    def __init__(
        self, records: tuple[ChannelInputParentRecord, ...], action: ChannelInputRestartAction
    ) -> None:
        self.records = records
        self.action = action
        self.events: list[str] = []
        self.decide_restart = AsyncMock(side_effect=self._decide_restart)
        self.cancel = AsyncMock(side_effect=self._cancel)
        self.resume = AsyncMock(side_effect=self._resume)
        self.publish = AsyncMock(side_effect=self._publish)
        self.observe_restart = AsyncMock(side_effect=self._observe_restart)
        self.record_force_close = Mock(side_effect=self._record_force_close)

    def load_all(self) -> tuple[object, ...]:
        return self.records

    async def _decide_restart(self, *_args: object, **_kwargs: object) -> ChannelInputRestartAction:
        self.events.append("decide")
        return self.action

    async def _cancel(self, *_args: object) -> object:
        self.events.append("cancel")
        return SimpleNamespace(state=ChannelInputParentState.CANCELED)

    async def _resume(self, *_args: object) -> object:
        self.events.append("resume")
        return SimpleNamespace(final_tx_hex="00", final_txid="11" * 32)

    async def _publish(self, *_args: object) -> object:
        self.events.append("publish")
        return SimpleNamespace()

    async def _observe_restart(self, *_args: object) -> object:
        self.events.append("observe")
        return self.records[0]

    def _record_force_close(self, *_args: object) -> object:
        self.events.append("record-force-close")
        return SimpleNamespace()


def _negotiation(session_id: str = "session") -> TakerChannelInputNegotiation:
    return cast(
        TakerChannelInputNegotiation,
        SimpleNamespace(
            invite=SimpleNamespace(session_id=session_id, attempt=1),
            selected_nicks=("maker",),
        ),
    )


def _record(
    state: ChannelInputParentState = ChannelInputParentState.CHANNEL_SIGNING,
    *,
    force_closes: tuple[object, ...] = (),
) -> ChannelInputParentRecord:
    transcript = SimpleNamespace(recovery_height=90)
    prepare = SimpleNamespace(approval_set=SimpleNamespace(transcript=transcript))
    return cast(
        ChannelInputParentRecord,
        SimpleNamespace(
            state=state,
            force_closes=force_closes,
            final_tx_hex=None,
            final_txid=None,
            channels=(
                SimpleNamespace(
                    second_snapshot=SimpleNamespace(funding_txid="aa" * 32, funding_vout=0)
                ),
                SimpleNamespace(
                    second_snapshot=SimpleNamespace(funding_txid="bb" * 32, funding_vout=1)
                ),
            ),
            plan=SimpleNamespace(
                session_id="session",
                unsigned_parent_hex="00",
                authorization=SimpleNamespace(prepare=prepare),
                channels=(
                    SimpleNamespace(
                        first=SimpleNamespace(endpoint_id="remote"),
                        second=SimpleNamespace(endpoint_id="local"),
                    ),
                ),
            ),
        ),
    )


class FakeTransport:
    def __init__(self) -> None:
        self.establish_recovery = AsyncMock()
        self.cancel_or_replay = AsyncMock(return_value=True)


def _transport() -> FakeTransport:
    return FakeTransport()


async def _reconcile(
    config: object,
    registry: object,
    initialized_backend: object,
    chain_backend: object,
    directory_client: object,
    parent_record_directory: Path,
    *,
    parent_coordinator: object,
) -> None:
    await reconcile_taker_channel_input_records(
        cast(ChannelInputConfig, config),
        cast(TakerChannelInputRegistry, registry),
        cast(InitializedChannelInputBackend, initialized_backend),
        cast(ChannelInputParentChain, chain_backend),
        cast(_DirectoryClient, directory_client),
        parent_record_directory,
        parent_coordinator=cast(ChannelInputParentCoordinator, parent_coordinator),
    )


@pytest.mark.asyncio
async def test_publish_and_terminal_records_do_not_require_peer_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    chain = FakeChain(events)
    published = FakeCoordinator(
        (_record(ChannelInputParentState.FINAL_TX_PERSISTED),),
        ChannelInputRestartAction.PUBLISH_EXACT_PARENT,
    )
    terminal = FakeCoordinator(
        (_record(ChannelInputParentState.CONFIRMED),), ChannelInputRestartAction.NONE
    )
    directory = SimpleNamespace(recovery_bootstrap_routes=Mock(side_effect=AssertionError))
    initialized = SimpleNamespace(backend=SimpleNamespace())
    registry = FakeRegistry((_negotiation(),))
    monkeypatch.setattr(
        "taker.channel_input_recovery._validate_record_negotiation", lambda *_: None
    )

    await _reconcile(
        SimpleNamespace(),
        registry,
        initialized,
        chain,
        directory,
        Path("unused"),
        parent_coordinator=published,
    )
    await _reconcile(
        SimpleNamespace(),
        FakeRegistry(()),
        initialized,
        chain,
        directory,
        Path("unused"),
        parent_coordinator=terminal,
    )

    assert published.events == ["decide", "publish"]
    assert terminal.events == []
    assert directory.recovery_bootstrap_routes.call_count == 0


@pytest.mark.asyncio
async def test_no_parent_negotiation_is_canceled_over_fresh_recovery_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _transport()
    directory = SimpleNamespace(
        recovery_bootstrap_routes=Mock(return_value=(ReplyRoute("maker", "directory:one"),))
    )
    monkeypatch.setattr("taker.channel_input_recovery._recovery_transport", lambda *_: transport)

    await _reconcile(
        SimpleNamespace(),
        FakeRegistry((_negotiation(),)),
        SimpleNamespace(),
        FakeChain([]),
        directory,
        Path("unused"),
        parent_coordinator=FakeCoordinator((), ChannelInputRestartAction.NONE),
    )

    transport.establish_recovery.assert_awaited_once_with(
        "session", 1, "maker", ReplyRoute("maker", "directory:one")
    )
    transport.cancel_or_replay.assert_awaited_once_with("session", 1, "startup_recovery")


@pytest.mark.asyncio
async def test_recovery_route_retries_per_maker() -> None:
    negotiation = _negotiation()
    first = ReplyRoute("maker", "directory:first")
    second = ReplyRoute("maker", "directory:second")
    transport = SimpleNamespace(
        establish_recovery=AsyncMock(side_effect=[RuntimeError("down"), None])
    )
    directory = SimpleNamespace(recovery_bootstrap_routes=Mock(return_value=(first, second)))

    await _establish_all_recovery_routes(
        cast(TakerChannelInputTransport, transport), directory, negotiation
    )

    assert transport.establish_recovery.await_args_list == [
        (("session", 1, "maker", first), {}),
        (("session", 1, "maker", second), {}),
    ]


def test_endpoint_mapping_rejects_wrong_durable_signer() -> None:
    prepare = object()
    record = SimpleNamespace(
        state=ChannelInputParentState.CREATED,
        channels=(),
        plan=SimpleNamespace(
            session_id="session",
            unsigned_parent_hex="parent",
            authorization=SimpleNamespace(prepare=prepare),
            channels=(
                SimpleNamespace(
                    channel_input_index=0,
                    first=SimpleNamespace(endpoint_id="remote", participant_key="wrong"),
                    second=SimpleNamespace(endpoint_id="local", participant_key="coordinator"),
                ),
            ),
        ),
    )
    negotiation = SimpleNamespace(
        invite=SimpleNamespace(session_id="session"),
        prepare=prepare,
        coordinator_key="coordinator",
        selected_nicks=("maker",),
        transcript=SimpleNamespace(
            unsigned_parent_tx="parent",
            channels=(
                SimpleNamespace(
                    input_index=0,
                    first_participant_key="maker-key",
                    second_participant_key="coordinator",
                ),
            ),
        ),
        remote_hellos={"maker": SimpleNamespace(participant_key="maker-key")}.items(),
        local_hellos={"maker": SimpleNamespace(participant_key="coordinator")}.items(),
    )

    with pytest.raises(TakerChannelInputRecoveryError, match="signer mapping"):
        _validate_record_negotiation(
            cast(ChannelInputParentRecord, record), cast(TakerChannelInputNegotiation, negotiation)
        )


@pytest.mark.asyncio
async def test_cancel_pre_sign_establishes_recovery_before_parent_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _transport()
    coordinator = FakeCoordinator((_record(),), ChannelInputRestartAction.CANCEL_PRE_SIGN)
    directory = SimpleNamespace(
        recovery_bootstrap_routes=Mock(return_value=(ReplyRoute("maker", "directory:one"),))
    )
    monkeypatch.setattr(
        "taker.channel_input_recovery._validate_record_negotiation", lambda *_: None
    )
    monkeypatch.setattr("taker.channel_input_recovery._endpoint_map", lambda *_: {})
    monkeypatch.setattr("taker.channel_input_recovery._recovery_transport", lambda *_: transport)

    await _reconcile(
        SimpleNamespace(),
        FakeRegistry((_negotiation(),)),
        SimpleNamespace(),
        FakeChain([]),
        directory,
        Path("unused"),
        parent_coordinator=coordinator,
    )

    assert coordinator.events == ["decide", "cancel"]
    transport.establish_recovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_finalizes_then_publishes_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _transport()
    coordinator = FakeCoordinator((_record(),), ChannelInputRestartAction.RESUME_EXACT_PARENT)
    directory = SimpleNamespace(
        recovery_bootstrap_routes=Mock(return_value=(ReplyRoute("maker", "directory:one"),))
    )
    monkeypatch.setattr(
        "taker.channel_input_recovery._validate_record_negotiation", lambda *_: None
    )
    monkeypatch.setattr("taker.channel_input_recovery._endpoint_map", lambda *_: {})
    monkeypatch.setattr("taker.channel_input_recovery._recovery_transport", lambda *_: transport)

    await _reconcile(
        SimpleNamespace(),
        FakeRegistry((_negotiation(),)),
        SimpleNamespace(),
        FakeChain([]),
        directory,
        Path("unused"),
        parent_coordinator=coordinator,
    )

    assert coordinator.events == ["decide", "resume", "publish"]


@pytest.mark.asyncio
async def test_partial_force_close_replays_only_uncovered_local_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    covered = SimpleNamespace(funding_txid="aa" * 32, funding_vout=0)
    record = _record(force_closes=(covered,))
    coordinator = FakeCoordinator((record,), ChannelInputRestartAction.NONE)
    outcome = SimpleNamespace()
    backend = SimpleNamespace(force_close_channel_input_recovery=AsyncMock(return_value=outcome))
    monkeypatch.setattr(
        "taker.channel_input_recovery._validate_record_negotiation", lambda *_: None
    )
    monkeypatch.setattr("taker.channel_input_recovery.get_txid", lambda _: "parent")

    await _reconcile(
        SimpleNamespace(),
        FakeRegistry((_negotiation(),)),
        SimpleNamespace(backend=backend),
        FakeChain([], height=100),
        SimpleNamespace(recovery_bootstrap_routes=Mock(side_effect=AssertionError)),
        Path("unused"),
        parent_coordinator=coordinator,
    )

    backend.force_close_channel_input_recovery.assert_awaited_once_with(
        "session", ANY, "parent", 90, 100
    )
    assert coordinator.events == ["observe", "record-force-close"]
    coordinator.decide_restart.assert_not_awaited()
