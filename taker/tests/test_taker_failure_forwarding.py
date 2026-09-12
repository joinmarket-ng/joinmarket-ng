"""Tests for Taker forwarding of per-round session diagnostics.

The tumbler runner reads the taker's failure reason and successful maker identities
to explain why a round did not broadcast and to discourage maker reuse. These
live on the per-round ``CoinJoinSession``; the ``Taker`` must expose them as
read-only properties so external consumers see the real values instead of
``None`` (which previously broke the tumbler's confirmation-aware retry hint).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config

from taker.taker import Taker


def _taker() -> Taker:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    config = make_taker_config(counterparty_count=2, minimum_makers=2)
    return Taker(wallet, backend, config)


@pytest.mark.asyncio
async def test_last_failure_reason_property_forwards() -> None:
    taker = _taker()
    assert taker.last_failure_reason is None
    taker._session.last_failure_reason = "boom"
    assert taker.last_failure_reason == "boom"


@pytest.mark.asyncio
async def test_last_used_nicks_property_forwards() -> None:
    taker = _taker()
    assert taker.last_used_nicks == set()
    taker._session.last_used_nicks = {"J5Maker"}
    assert taker.last_used_nicks == {"J5Maker"}


@pytest.mark.asyncio
async def test_last_used_maker_keys_property_forwards() -> None:
    taker = _taker()
    assert taker.last_used_maker_keys == set()
    taker._session.last_used_maker_keys = {"nick:J5Maker", f"bond:{'a' * 64}:0"}
    assert taker.last_used_maker_keys == {"nick:J5Maker", f"bond:{'a' * 64}:0"}


@pytest.mark.asyncio
async def test_txid_property_forwards() -> None:
    taker = _taker()
    assert taker.txid == ""
    taker._session.txid = "f" * 64
    assert taker.txid == "f" * 64
