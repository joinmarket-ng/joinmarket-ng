"""Focused live-round ownership tests for the taker channel-input coordinator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.channel_input import ChannelInputPreparePayload
from jmcore.channel_input_config import ChannelInputConfig
from jmswap.channel_input import InitializedChannelInputBackend

from taker.channel_input_coordinator import (
    TakerChannelInputCoordinator,
    TakerChannelInputCoordinatorError,
)
from taker.channel_input_negotiation import TakerChannelInputNegotiation, TakerChannelInputStage
from taker.channel_input_parent import (
    ChannelInputParentChain,
    ChannelInputParentCoordinator,
    ChannelInputParentRecord,
    ChannelInputParentState,
)
from taker.channel_input_planner import ChannelInputParentDraft
from taker.channel_input_runtime import TakerChannelInputRegistry
from taker.channel_input_transport import TakerChannelInputTransport
from taker.coinjoin_session import CoinJoinSession
from taker.tx_builder import ChannelTransactionContribution


def _config() -> ChannelInputConfig:
    return ChannelInputConfig(phase_timeout_seconds=10.0, recovery_delay_blocks=6)


def _contribution(nick: str) -> SimpleNamespace:
    return SimpleNamespace(
        first_endpoint=SimpleNamespace(
            endpoint_id=f"remote:{nick}", output=SimpleNamespace(value=0)
        ),
        second_endpoint=SimpleNamespace(
            endpoint_id=f"local:{nick}", output=SimpleNamespace(value=0)
        ),
    )


def _session() -> SimpleNamespace:
    addresses = (
        scriptpubkey_to_address(b"\x51\x20" + b"\x01" * 32, "regtest"),
        scriptpubkey_to_address(b"\x51\x20" + b"\x02" * 32, "regtest"),
    )
    wallet = MagicMock(mixdepth_count=4)
    wallet.get_new_persisted_internal_address.side_effect = addresses
    session = SimpleNamespace(
        maker_sessions={
            "alice": SimpleNamespace(offer=SimpleNamespace()),
            "bob": SimpleNamespace(offer=SimpleNamespace()),
        },
        cj_amount=100_000,
        wallet=wallet,
        config=SimpleNamespace(network=SimpleNamespace(value="regtest")),
        backend=SimpleNamespace(get_block_height=AsyncMock(return_value=100)),
        ordinary_witnesses=(SimpleNamespace(input_index=0, witness=("00",)),),
        signing_boundary_crossed=False,
        unsigned_tx=b"",
        tx_metadata={},
        final_tx=b"",
        txid="",
    )
    session._phase_build_tx = AsyncMock(return_value=True)
    session.maker_fee_plan = MagicMock(return_value={"alice": 17, "bob": 17})
    session._revalidate_inputs_before_broadcast = AsyncMock(return_value=(True, ""))
    session.renew_input_locks = MagicMock(return_value=True)
    return session


@pytest.mark.parametrize("reverse_selection", [False, True])
def test_prepare_persists_uses_paid_fee_plan_and_builds_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path, reverse_selection: bool
) -> None:
    events: list[str] = []
    session = _session()
    if reverse_selection:
        session.maker_sessions = dict(reversed(session.maker_sessions.items()))
    paid_maker_fees = {"alice": 1_001, "bob": 1_001}
    session.maker_fee_plan.return_value = paid_maker_fees
    negotiation = SimpleNamespace(
        selected_nicks=("alice", "bob"), stage=TakerChannelInputStage.HELLOS_READY
    )
    draft = SimpleNamespace(transcript="transcript", endpoint_terms=("terms",))
    prepare = SimpleNamespace()

    def create(**_kwargs: object) -> SimpleNamespace:
        assert _kwargs["selected_nicks"] == ("alice", "bob")
        events.append("create")
        return negotiation

    registry = SimpleNamespace(
        create=AsyncMock(side_effect=create),
        get=MagicMock(return_value=negotiation),
    )

    class Transport:
        def __init__(self, **kwargs: object) -> None:
            events.append("transport")
            self.allocate = cast(Callable[[str], str], kwargs["settlement_script_allocator"])

        async def exchange_hellos(self, _session_id: str, _attempt: int) -> None:
            events.append("hellos")
            assert self.allocate("alice").startswith("5120")
            assert self.allocate("bob").startswith("5120")

        async def build_prepare(self, *_args: object) -> SimpleNamespace:
            events.append("prepare")
            return SimpleNamespace(prepare=prepare)

    fees: list[tuple[TakerChannelInputNegotiation, Mapping[str, int]]] = []

    def plan(
        actual: TakerChannelInputNegotiation, mapping: Mapping[str, int]
    ) -> tuple[ChannelTransactionContribution, ...]:
        fees.append((actual, mapping))
        return cast(
            tuple[ChannelTransactionContribution, ...],
            tuple(
                SimpleNamespace(
                    first_endpoint=SimpleNamespace(
                        endpoint_id=f"remote:{nick}", output=SimpleNamespace(value=50_000 + fee)
                    ),
                    second_endpoint=SimpleNamespace(
                        endpoint_id=f"local:{nick}", output=SimpleNamespace(value=50_000 - fee)
                    ),
                )
                for nick, fee in mapping.items()
            ),
        )

    monkeypatch.setattr("taker.channel_input_coordinator.TakerChannelInputTransport", Transport)
    monkeypatch.setattr(
        "taker.channel_input_coordinator.plan_channel_input_contributions",
        plan,
    )
    monkeypatch.setattr(
        "taker.channel_input_coordinator.derive_channel_input_parent_draft", lambda *a: draft
    )
    coordinator = TakerChannelInputCoordinator(
        cast(CoinJoinSession, session),
        _config(),
        cast(TakerChannelInputRegistry, registry),
        cast(InitializedChannelInputBackend, MagicMock()),
        cast(ChannelInputParentChain, MagicMock()),
        tmp_path / "parents",
        clock=lambda: 1_000,
        current_height=100,
        session_key=b"\x01" * 32,
        session_id="aa" * 32,
    )

    assert asyncio.run(coordinator.prepare("bcrt1pdestination", 2)) is prepare
    assert asyncio.run(coordinator.prepare("other", 0)) is prepare
    assert events == ["create", "transport", "hellos", "prepare"]
    assert registry.create.await_count == 1
    assert registry.create.await_args.kwargs["expiry"] == 1_060
    assert registry.create.await_args.kwargs["min_recovery_delta"] == 6
    assert registry.create.await_args.kwargs["max_recovery_delta"] == 10
    assert fees == [(cast(TakerChannelInputNegotiation, negotiation), paid_maker_fees)]
    session.maker_fee_plan.assert_called_once_with()
    first_settlement_values = [
        contribution.first_endpoint.output.value for contribution in coordinator.contributions
    ]
    assert first_settlement_values == [51_001, 51_001]
    session._phase_build_tx.assert_awaited_once_with(
        "bcrt1pdestination", 2, channel_contributions=coordinator.contributions
    )
    assert session.wallet.get_new_persisted_internal_address.call_args_list[0].args == (
        3,
        f"channel-input:{'aa' * 32}:alice",
    )
    assert session.wallet.get_new_persisted_internal_address.call_args_list[1].args == (
        3,
        f"channel-input:{'aa' * 32}:bob",
    )


def test_finalize_and_publish_use_exact_parent_coordinator(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    session = _session()
    transport = MagicMock()
    contributions = (_contribution("alice"), _contribution("bob"))
    record = SimpleNamespace(final_tx_hex="aabb", final_txid="11" * 32)
    signing_boundaries: list[bool] = []

    async def execute(*_args: object) -> SimpleNamespace:
        signing_boundaries.append(session.signing_boundary_crossed)
        return record

    parent = SimpleNamespace(
        execute=AsyncMock(side_effect=execute), publish=AsyncMock(return_value=record)
    )
    coordinator = TakerChannelInputCoordinator(
        cast(CoinJoinSession, session),
        _config(),
        cast(TakerChannelInputRegistry, MagicMock()),
        cast(InitializedChannelInputBackend, MagicMock()),
        cast(ChannelInputParentChain, MagicMock()),
        tmp_path / "parents",
        parent_coordinator=cast(ChannelInputParentCoordinator, parent),
    )
    coordinator._session_id = "aa" * 32
    coordinator._attempt = 1
    coordinator._transport = cast(TakerChannelInputTransport, transport)
    coordinator._negotiation = cast(
        TakerChannelInputNegotiation, SimpleNamespace(selected_nicks=("alice", "bob"))
    )
    coordinator._contributions = cast(tuple[ChannelTransactionContribution, ...], contributions)
    coordinator._draft = cast(ChannelInputParentDraft, SimpleNamespace())
    coordinator._prepare = cast(ChannelInputPreparePayload, SimpleNamespace())
    plan = SimpleNamespace()
    monkeypatch.setattr(
        "taker.channel_input_coordinator.authorize_channel_input_parent", lambda *a: plan
    )
    remote = MagicMock(side_effect=lambda **kwargs: ("remote", kwargs["nick"]))
    local = MagicMock(side_effect=lambda **kwargs: ("local", kwargs["nick"]))
    monkeypatch.setattr("taker.channel_input_coordinator.RemoteChannelInputEndpoint", remote)
    monkeypatch.setattr("taker.channel_input_coordinator.LocalChannelInputEndpoint", local)

    assert asyncio.run(coordinator.finalize_parent()) == "11" * 32
    assert session.signing_boundary_crossed
    assert signing_boundaries == [True]
    endpoints = parent.execute.await_args.args[1]
    assert endpoints == {
        "remote:alice": ("remote", "alice"),
        "local:alice": ("local", "alice"),
        "remote:bob": ("remote", "bob"),
        "local:bob": ("local", "bob"),
    }
    assert session.final_tx == bytes.fromhex("aabb")
    assert session.txid == "11" * 32
    assert asyncio.run(coordinator.publish()) == "11" * 32
    session._revalidate_inputs_before_broadcast.assert_awaited_once_with()
    session.renew_input_locks.assert_called_once_with("broadcast channel-input parent")
    parent.publish.assert_awaited_once_with("aa" * 32, coordinator.chain_backend)


@pytest.mark.parametrize(
    ("validation", "renewed", "error"),
    [
        ((False, "input unavailable"), True, "input unavailable"),
        ((True, ""), False, "lock renewal failure"),
    ],
)
def test_publish_rejects_invalid_inputs_or_lost_locks_before_query(
    tmp_path, validation: tuple[bool, str], renewed: bool, error: str
) -> None:
    session = _session()
    session._revalidate_inputs_before_broadcast.return_value = validation
    session.renew_input_locks.return_value = renewed
    record = SimpleNamespace(final_tx_hex="aabb", final_txid="11" * 32)
    parent = SimpleNamespace(publish=AsyncMock(return_value=record))
    coordinator = TakerChannelInputCoordinator(
        cast(CoinJoinSession, session),
        _config(),
        cast(TakerChannelInputRegistry, MagicMock()),
        cast(InitializedChannelInputBackend, MagicMock()),
        cast(ChannelInputParentChain, MagicMock()),
        tmp_path / "parents",
        parent_coordinator=cast(ChannelInputParentCoordinator, parent),
    )
    coordinator._session_id = "aa" * 32
    coordinator._final_record = cast(ChannelInputParentRecord, record)

    with pytest.raises(TakerChannelInputCoordinatorError, match=error):
        asyncio.run(coordinator.publish())

    parent.publish.assert_not_awaited()


def test_abort_delegates_pre_partial_and_fails_closed_after_partial(tmp_path) -> None:
    session = _session()
    session.signing_boundary_crossed = True
    transport = SimpleNamespace(cancel_or_replay=AsyncMock(return_value=True))
    parent = SimpleNamespace(load=MagicMock(return_value=None))
    registry = SimpleNamespace(
        get=MagicMock(return_value=SimpleNamespace(parent_channel_states=()))
    )
    coordinator = TakerChannelInputCoordinator(
        cast(CoinJoinSession, session),
        _config(),
        cast(TakerChannelInputRegistry, registry),
        cast(InitializedChannelInputBackend, MagicMock()),
        cast(ChannelInputParentChain, MagicMock()),
        tmp_path / "parents",
        parent_coordinator=cast(ChannelInputParentCoordinator, parent),
    )
    coordinator._session_id = "aa" * 32
    coordinator._attempt = 1
    coordinator._transport = cast(TakerChannelInputTransport, transport)

    assert asyncio.run(coordinator.abort("failure"))
    assert asyncio.run(coordinator.abort("other"))
    assert not session.signing_boundary_crossed
    transport.cancel_or_replay.assert_awaited_once_with("aa" * 32, 1, "failure")

    partial_registry = SimpleNamespace(
        get=MagicMock(
            return_value=SimpleNamespace(
                parent_channel_states=(
                    SimpleNamespace(
                        local_partial=object(), remote_partial=None, remote_finalized=None
                    ),
                )
            )
        )
    )
    blocked = TakerChannelInputCoordinator(
        cast(CoinJoinSession, session),
        _config(),
        cast(TakerChannelInputRegistry, partial_registry),
        cast(InitializedChannelInputBackend, MagicMock()),
        cast(ChannelInputParentChain, MagicMock()),
        tmp_path / "other-parents",
        parent_coordinator=cast(ChannelInputParentCoordinator, parent),
    )
    blocked._session_id = "bb" * 32
    blocked._attempt = 1
    blocked._transport = cast(TakerChannelInputTransport, transport)
    session.signing_boundary_crossed = True
    assert not asyncio.run(blocked.abort("failure"))
    assert session.signing_boundary_crossed


def test_abort_uses_parent_cancellation_when_record_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    session = _session()
    record = SimpleNamespace(
        state=ChannelInputParentState.PARENT_PREPARED,
        final_tx_hex=None,
        final_txid=None,
        channels=(),
    )
    parent = SimpleNamespace(
        load=MagicMock(return_value=record),
        cancel=AsyncMock(return_value=SimpleNamespace(state=SimpleNamespace(value="canceled"))),
    )
    registry = SimpleNamespace(
        get=MagicMock(return_value=SimpleNamespace(parent_channel_states=()))
    )
    coordinator = TakerChannelInputCoordinator(
        cast(CoinJoinSession, session),
        _config(),
        cast(TakerChannelInputRegistry, registry),
        cast(InitializedChannelInputBackend, MagicMock()),
        cast(ChannelInputParentChain, MagicMock()),
        tmp_path / "parents",
        parent_coordinator=cast(ChannelInputParentCoordinator, parent),
    )
    coordinator._session_id = "aa" * 32
    coordinator._attempt = 1
    coordinator._transport = cast(TakerChannelInputTransport, MagicMock())
    coordinator._negotiation = cast(
        TakerChannelInputNegotiation, SimpleNamespace(selected_nicks=())
    )
    coordinator._contributions = ()
    session.signing_boundary_crossed = True

    assert asyncio.run(coordinator.abort("failure"))
    assert not session.signing_boundary_crossed
    parent.cancel.assert_awaited_once()


def test_prepare_rejects_failed_build_without_rebuilding(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    session = _session()
    session._phase_build_tx.return_value = False
    negotiation = SimpleNamespace(
        selected_nicks=("alice",), stage=TakerChannelInputStage.HELLOS_READY
    )
    registry = SimpleNamespace(
        create=AsyncMock(return_value=negotiation), get=MagicMock(return_value=negotiation)
    )

    class Transport:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def exchange_hellos(self, *_args: object) -> None:
            return None

    monkeypatch.setattr("taker.channel_input_coordinator.TakerChannelInputTransport", Transport)
    monkeypatch.setattr(
        "taker.channel_input_coordinator.plan_channel_input_contributions",
        lambda *_args: (_contribution("alice"),),
    )
    coordinator = TakerChannelInputCoordinator(
        cast(CoinJoinSession, session),
        _config(),
        cast(TakerChannelInputRegistry, registry),
        cast(InitializedChannelInputBackend, MagicMock()),
        cast(ChannelInputParentChain, MagicMock()),
        tmp_path / "parents",
        current_height=1,
    )
    with pytest.raises(TakerChannelInputCoordinatorError, match="could not build"):
        asyncio.run(coordinator.prepare("destination", 0))
    with pytest.raises(TakerChannelInputCoordinatorError, match="already attempted"):
        asyncio.run(coordinator.prepare("destination", 0))
