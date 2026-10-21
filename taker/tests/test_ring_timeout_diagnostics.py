"""Timeout progress survives cancellation without exposing private participant identities."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jmcore.cofunded_ring import (
    RingFailurePayload,
    RingOpenDiagnosticsPayload,
    RingOpenPayload,
    RingPreparedPayload,
    encode_ring_message,
    ring_hash,
    sign_payload,
)
from test_channel_ring_coordinator import _harness, _ring_key, _secret
from test_channel_ring_maker_only import _coordinator

from taker.channel_ring import RingResponseTimeoutError, TakerRingCoordinator
from taker.channel_ring_maker_only import MakerOnlyRingCoordinator
from taker.ring_diagnostics import RingResponseProgress


async def _planned(tmp_path: Path, maker_only: bool) -> Any:
    coordinator = _coordinator(tmp_path) if maker_only else _harness(tmp_path)
    coordinator._preconditions()
    if maker_only:
        fee, residuals = coordinator._finalize_fees_and_inputs(0)
        await coordinator._invite(fee, residuals, 0)
    else:
        fee, residuals = coordinator._finalize_fees_and_inputs(0)
        await coordinator._invite(fee, residuals)
    await coordinator._plan()
    return coordinator


def _progress(coordinator: Any, maker_only: bool) -> RingResponseProgress:
    record = coordinator._record()
    plans = record.plans if maker_only else record.coordinator_plans
    return RingResponseProgress(
        "ring_prepared",
        record.round_nonce,
        record.revision,
        {
            nick: (key, plans[key], SimpleNamespace(decrypt=_decrypt))
            for key, nick in coordinator._peer_by_key.items()
        },
    )


def _decrypt(value: str) -> str:
    return base64.urlsafe_b64decode(value).decode()


def _wire(payload: Any) -> str:
    return base64.urlsafe_b64encode(encode_ring_message(payload).encode()).decode()


def _failure(progress: RingResponseProgress, nick: str) -> RingFailurePayload:
    key, plan, _ = progress.participants[nick]
    assert plan is not None and key is not None
    payload = RingFailurePayload(
        round_nonce=progress.round_nonce,
        revision=progress.revision,
        signer_key=key,
        plan_hash=ring_hash("plan", plan).hex(),
        pending_channel_id=plan.outgoing_edge.pending_channel_id,
        leg="outgoing",
        reason="timeout",
    )
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("maker_only", [False, True])
@pytest.mark.parametrize("outer_timeout", [False, True])
async def test_real_receive_preserves_phase_partial_counts_and_reported_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maker_only: bool,
    outer_timeout: bool,
) -> None:
    coordinator = await _planned(tmp_path, maker_only)
    progress = _progress(coordinator, maker_only)
    nicks = list(progress.participants)
    reporter = nicks[1]
    key, _plan, _ = progress.participants[reporter]
    assert key is not None
    signed = sign_payload(_failure(progress, reporter), coordinator.peer_secrets[reporter])
    for maker in coordinator.session.ring_maker_sessions.values():
        maker.crypto = SimpleNamespace(decrypt=_decrypt)

    async def wait(**kwargs: Any) -> Any:
        assert not kwargs["response_filter"](reporter, _wire(signed))
        kwargs["response_progress"][nicks[0]] = {"data": ["one-partial-response"]}
        if outer_timeout:
            await asyncio.sleep(60)
        return kwargs["response_progress"]

    monkeypatch.setattr(
        coordinator.session.directory_client, "wait_for_responses", wait, raising=False
    )
    base = MakerOnlyRingCoordinator if maker_only else TakerRingCoordinator
    with pytest.raises(RingResponseTimeoutError) as failure:
        await base._receive(coordinator, RingPreparedPayload, expected_counts=2, timeout=0.02)
    message = str(failure.value)
    assert "phase opening" in message and "missing ring_prepared" in message
    assert "(1/2)" in message and "(0/2)" in message
    assert "participant-reported failure on edge" in message
    assert "outgoing, timeout" in message and "root cause unknown" in message
    for nick, (participant_key, plan, _) in progress.participants.items():
        assert nick not in message and participant_key not in message
        assert plan is not None
        assert plan.successor.node_id not in message
        assert plan.successor.onion_endpoint not in message
        assert plan.outgoing_edge.pending_channel_id not in message
    assert not coordinator.sign_authorized
    await coordinator.cancel("coordinator_failure")
    assert not coordinator.sign_authorized


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["signature", "revision", "round", "edge", "plan", "signer"])
async def test_unbound_failure_reports_are_not_trusted(
    tmp_path: Path,
    changed: str,
) -> None:
    coordinator = await _planned(tmp_path, True)
    progress = _progress(coordinator, True)
    nick = next(iter(progress.participants))
    failure = _failure(progress, nick)
    updates: dict[str, Any] = {
        "signature": {"sig": "00" * 64},
        "revision": {"revision": failure.revision + 1},
        "round": {"round_nonce": "ef" * 32},
        "edge": {"pending_channel_id": "ef" * 32},
        "plan": {"plan_hash": "ef" * 32},
        "signer": {},
    }[changed]
    signed = sign_payload(failure.model_copy(update=updates), coordinator.peer_secrets[nick])
    if changed == "signature":
        signed = signed.model_copy(update={"sig": "00" * 64})
    if changed == "signer":
        signed = sign_payload(failure.model_copy(update={"signer_key": _ring_key(9)}), _secret(9))
    assert progress.include_response(nick, _wire(signed))
    assert progress.reports == {}


@pytest.mark.asyncio
async def test_a_late_opening_report_never_counts_as_cancellation_evidence(tmp_path: Path) -> None:
    coordinator = await _planned(tmp_path, True)
    progress = _progress(coordinator, True)
    nick = next(iter(progress.participants))
    signed = sign_payload(_failure(progress, nick), coordinator.peer_secrets[nick])
    progress.expected_response = "ring_cancel"
    assert not progress.include_response(nick, _wire(signed))
    assert progress.responses == {} and progress.reports == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("maker_only", [False, True])
async def test_diagnostic_open_requires_explicit_maker_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maker_only: bool,
) -> None:
    coordinator = await _planned(tmp_path, maker_only)
    nicks = list(coordinator.session.ring_maker_sessions)
    coordinator.session.ring_maker_sessions[nicks[0]].supports_ring_diagnostics = True
    sent: dict[str, Any] = {}
    original_exchange = coordinator._exchange
    original_send = coordinator._send

    async def exchange(payloads: Any, expected: Any, **kwargs: Any) -> Any:
        if expected is RingPreparedPayload:
            sent.update(payloads)
        return await original_exchange(payloads, expected, **kwargs)

    async def send(nick: str, payload: Any) -> None:
        if isinstance(payload, (RingOpenPayload, RingOpenDiagnosticsPayload)):
            sent[nick] = payload
        await original_send(nick, payload)

    monkeypatch.setattr(coordinator, "_exchange", exchange)
    monkeypatch.setattr(coordinator, "_send", send)
    if maker_only:
        await coordinator._open()
    else:
        await coordinator._open(coordinator._record().plan)
    assert set(sent) == set(nicks)
    assert isinstance(sent[nicks[0]], RingOpenDiagnosticsPayload)
    for nick in nicks[1:]:
        assert isinstance(sent[nick], RingOpenPayload)
    assert not coordinator.sign_authorized
