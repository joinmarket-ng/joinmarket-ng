"""
Tests for the typed-event notification registry and :meth:`Notifier.emit`.

These tests exercise the seam introduced by the
``NotificationEvent`` / ``EVENT_TEMPLATES`` refactor: every public
``notify_*`` shim funnels through :meth:`Notifier.emit`, which looks up
the event in the registry and delegates to :meth:`Notifier._send`.

The byte-for-byte title/body contract preserved by the refactor is
already exercised by ``test_notifications.py``; this module focuses on
properties of the registry itself.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from jmcore.notifications import (
    EVENT_TEMPLATES,
    NotificationConfig,
    NotificationEvent,
    NotificationPriority,
    Notifier,
)


@pytest.fixture(autouse=True)
def isolate_proxy_env() -> Generator[None, None, None]:
    """Restore proxy env vars after each test (see test_notifications.py)."""
    with patch.dict(os.environ, {}, clear=False):
        yield


class TestRegistryCompleteness:
    """The registry must cover every declared event."""

    def test_every_event_has_a_template(self) -> None:
        for event in NotificationEvent:
            assert event in EVENT_TEMPLATES, f"missing template for {event}"

    def test_gating_attr_exists_on_config(self) -> None:
        """Every gating_attr must name a real bool field on NotificationConfig."""
        config = NotificationConfig()
        for event, template in EVENT_TEMPLATES.items():
            if template.gating_attr is None:
                continue
            assert hasattr(config, template.gating_attr), (
                f"{event} references missing config attr {template.gating_attr!r}"
            )
            assert isinstance(getattr(config, template.gating_attr), bool)


class TestEmitGating:
    """``emit`` must short-circuit when the gating flag is off."""

    @pytest.mark.asyncio
    async def test_emit_gated_off_returns_false_without_sending(self) -> None:
        config = NotificationConfig(enabled=True, urls=["test://"], notify_fill=False)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        result = await notifier.emit(
            NotificationEvent.FILL_REQUEST,
            taker_nick="alice",
            cj_amount=10_000,
            offer_id=0,
        )

        assert result is False
        notifier._send.assert_not_called()

    @pytest.mark.asyncio
    async def test_emit_gated_on_dispatches_to_send(self) -> None:
        config = NotificationConfig(enabled=True, urls=["test://"], notify_fill=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        result = await notifier.emit(
            NotificationEvent.FILL_REQUEST,
            taker_nick="alice",
            cj_amount=10_000,
            offer_id=7,
        )

        assert result is True
        notifier._send.assert_called_once()
        call = notifier._send.call_args
        assert call.kwargs["title"] == "Fill Request Received"
        assert "alice" in call.kwargs["body"]
        assert "Offer ID: 7" in call.kwargs["body"]
        assert call.kwargs["priority"] == NotificationPriority.INFO

    @pytest.mark.asyncio
    async def test_emit_ungated_event_always_dispatches(self) -> None:
        """Events with gating_attr=None (e.g. ORDERBOOK_STATUS) skip the gate."""
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        result = await notifier.emit(
            NotificationEvent.ORDERBOOK_STATUS,
            connected_directories=2,
            total_directories=3,
            total_offers=42,
            total_makers=10,
        )

        assert result is True
        notifier._send.assert_called_once()


class TestEmitPriorityBuilder:
    """Priority builders must be able to vary by payload."""

    @pytest.mark.asyncio
    async def test_directory_disconnect_priority_warning_when_some_connected(self) -> None:
        config = NotificationConfig(enabled=True, urls=["test://"], notify_disconnect=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await notifier.emit(
            NotificationEvent.DIRECTORY_DISCONNECT,
            server="ex.onion",
            connected_count=1,
            total_count=3,
        )

        assert notifier._send.call_args.kwargs["priority"] == NotificationPriority.WARNING

    @pytest.mark.asyncio
    async def test_directory_disconnect_priority_failure_when_none_connected(self) -> None:
        config = NotificationConfig(enabled=True, urls=["test://"], notify_disconnect=True)
        notifier = Notifier(config)
        notifier._send = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await notifier.emit(
            NotificationEvent.DIRECTORY_DISCONNECT,
            server="ex.onion",
            connected_count=0,
            total_count=3,
        )

        assert notifier._send.call_args.kwargs["priority"] == NotificationPriority.FAILURE


class TestShimsDelegateToEmit:
    """Each public notify_* shim must funnel through emit()."""

    @pytest.mark.asyncio
    async def test_notify_fill_request_uses_emit(self) -> None:
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)
        notifier.emit = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await notifier.notify_fill_request("alice", 12345, 3)

        notifier.emit.assert_awaited_once_with(
            NotificationEvent.FILL_REQUEST,
            taker_nick="alice",
            cj_amount=12345,
            offer_id=3,
        )

    @pytest.mark.asyncio
    async def test_notify_all_directories_disconnected_uses_emit(self) -> None:
        config = NotificationConfig(enabled=True, urls=["test://"])
        notifier = Notifier(config)
        notifier.emit = AsyncMock(return_value=True)  # type: ignore[method-assign]

        await notifier.notify_all_directories_disconnected()

        notifier.emit.assert_awaited_once_with(
            NotificationEvent.ALL_DIRECTORIES_DISCONNECTED,
        )
