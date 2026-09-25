"""Tests for the uniform experimental feature warning."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from loguru import logger

from jmcore import experimental
from jmcore.experimental import experimental_warnings, warn_experimental


@pytest.fixture
def warnings() -> Iterator[list[str]]:
    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m.record["message"])), level="WARNING")
    yield messages
    logger.remove(sink)


def test_no_features_logs_nothing(warnings: list[str]) -> None:
    warn_experimental([], "mainnet")
    assert warnings == []


def test_names_each_feature_once(warnings: list[str]) -> None:
    warn_experimental(
        [experimental.CHANNEL_RING, experimental.TAPROOT_PIT, experimental.CHANNEL_RING],
        "signet",
    )
    assert len(warnings) == 1
    assert warnings[0].count(experimental.CHANNEL_RING) == 1
    assert experimental.TAPROOT_PIT in warnings[0]
    assert experimental.GUIDE in warnings[0]


def test_mainnet_adds_a_second_warning(warnings: list[str]) -> None:
    warn_experimental([experimental.CREDENTIAL_MARKET], "mainnet")
    assert len(warnings) == 2
    assert "mainnet" in warnings[1]


def test_warning_lines_match_the_logged_warning(warnings: list[str]) -> None:
    lines = experimental_warnings([experimental.CHANNEL_BUYOUT], "mainnet")
    warn_experimental([experimental.CHANNEL_BUYOUT], "mainnet")
    assert lines == warnings
