"""The taker names each enabled experimental feature before going online."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from jmcore import experimental
from loguru import logger

from taker.taker import Taker


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m.record["message"])), level="WARNING")
    yield messages
    logger.remove(sink)


def _taker(**overrides: Any) -> Any:
    config = SimpleNamespace(
        address_type="p2wpkh",
        channel_ring=SimpleNamespace(enabled=False),
        external_podle_mode="disabled",
        market_fault_exclusion=False,
        bitcoin_network=None,
        network="signet",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return SimpleNamespace(config=config)


def test_default_taker_logs_no_experimental_warning(warnings: list[str]) -> None:
    Taker.warn_experimental_features(_taker())
    assert warnings == []


def test_each_opt_in_is_named(warnings: list[str]) -> None:
    Taker.warn_experimental_features(
        _taker(
            address_type="p2tr",
            channel_ring=SimpleNamespace(enabled=True),
            external_podle_mode="only",
            network="mainnet",
        )
    )
    assert len(warnings) == 2
    for feature in (
        experimental.TAPROOT_PIT,
        experimental.CHANNEL_RING,
        experimental.CREDENTIAL_MARKET,
    ):
        assert feature in warnings[0]
    assert "mainnet" in warnings[1]


def test_fault_exclusion_alone_logs_experimental_warning(warnings: list[str]) -> None:
    Taker.warn_experimental_features(_taker(market_fault_exclusion=True))
    assert len(warnings) == 1
    assert experimental.CREDENTIAL_MARKET in warnings[0]
