"""
Tests for the standard transaction confirmation display (issue #107).

Verifies that the SEND confirmation summary follows the workflow ordering:
Source Mixdepth, Destination, Amount, Change, Miner Fee Rate, Miner Fee.
"""

from __future__ import annotations

import pytest

from jmcore.confirmation import _display_standard_send_confirmation


@pytest.fixture
def send_capture(capsys: pytest.CaptureFixture[str]) -> pytest.CaptureFixture[str]:
    """Render a representative SEND confirmation matching jm-wallet send."""
    _display_standard_send_confirmation(
        operation="send",
        amount=79_456,
        destination="bc1qexampledestination",
        fee=None,
        mining_fee=333,
        additional_info={
            "Source Mixdepth": 2,
            "Change": "19,422 sats (0.00019422 BTC)",
            "Miner Fee Rate": "1.20 sat/vB",
        },
    )
    return capsys


def test_header_uses_mixed_case(send_capture: pytest.CaptureFixture[str]) -> None:
    """Header reads 'Expected SEND Transaction', not legacy all-caps."""
    out = send_capture.readouterr().out
    assert "Expected SEND Transaction" in out
    assert "TRANSACTION CONFIRMATION" not in out


def test_field_order_follows_workflow(send_capture: pytest.CaptureFixture[str]) -> None:
    """Fields appear in the order proposed in issue #107."""
    out = send_capture.readouterr().out
    expected_order = [
        "Source Mixdepth:",
        "Destination:",
        "Amount:",
        "Change:",
        "Miner Fee Rate:",
        "Miner Fee:",
    ]
    positions = [out.index(label) for label in expected_order]
    assert positions == sorted(positions), f"unexpected order in output:\n{out}"


def test_fee_label_renamed_to_miner_fee(send_capture: pytest.CaptureFixture[str]) -> None:
    """The plain 'Fee:' label is replaced with 'Miner Fee:' for SEND."""
    out = send_capture.readouterr().out
    # 'Miner Fee:' must appear; bare 'Fee:' (not preceded by 'Miner ') must not.
    assert "Miner Fee:" in out
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Fee:"):
            raise AssertionError(f"unexpected bare 'Fee:' label: {line!r}")


def test_sweep_renders_human_readable(capsys: pytest.CaptureFixture[str]) -> None:
    """A zero amount is rendered as a SWEEP rather than '0 sats'."""
    _display_standard_send_confirmation(
        operation="send",
        amount=0,
        destination="bc1qexampledestination",
        fee=200,
        mining_fee=None,
        additional_info={"Source Mixdepth": 0},
    )
    out = capsys.readouterr().out
    assert "SWEEP" in out


def test_internal_destination_label(capsys: pytest.CaptureFixture[str]) -> None:
    """INTERNAL destination is rendered with the next-mixdepth hint."""
    _display_standard_send_confirmation(
        operation="send",
        amount=10_000,
        destination="INTERNAL",
        fee=100,
        mining_fee=None,
        additional_info={"Source Mixdepth": 1},
    )
    out = capsys.readouterr().out
    assert "INTERNAL (next mixdepth)" in out


def test_unknown_additional_info_keys_still_render(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Forward-compatibility: unknown additional_info keys are not dropped."""
    _display_standard_send_confirmation(
        operation="send",
        amount=1_000,
        destination="bc1qexample",
        fee=50,
        mining_fee=None,
        additional_info={"Custom Note": "hello world"},
    )
    out = capsys.readouterr().out
    assert "Custom Note:" in out
    assert "hello world" in out
