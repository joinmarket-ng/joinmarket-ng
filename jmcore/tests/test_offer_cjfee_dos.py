"""Relative cjfee normalization must reject pathological peer input.

A public offer's cjfee is attacker-controlled. ``format(Decimal(x), "f")`` on a
tiny string with a huge exponent expands to gigabytes, so a single offer could
OOM every peer parsing the orderbook.
"""

from __future__ import annotations

import time

import pytest

from jmcore.directory_client import normalize_relative_cjfee


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.0001", "0.0001"),
        ("1E-9", "0.000000001"),
        ("0", "0"),
        ("0.00015", "0.00015"),
    ],
)
def test_legitimate_fees_normalized(raw, expected):
    assert normalize_relative_cjfee(raw) == expected


@pytest.mark.parametrize(
    "hostile",
    ["1E-9999999999", "1E999999999", "1E-100000", "NaN", "Inf", "-0.5", "9" * 64],
)
def test_pathological_fees_rejected_fast(hostile):
    start = time.monotonic()
    with pytest.raises(ValueError):
        normalize_relative_cjfee(hostile)
    # Rejected by inspecting the Decimal, never by expanding it.
    assert time.monotonic() - start < 1.0
