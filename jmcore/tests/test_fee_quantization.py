"""Unit tests for the maker fee quantization grid and rounding primitives."""

from __future__ import annotations

from decimal import Decimal

import pytest

from jmcore.fee_quantization import (
    QUANT_ABS,
    QUANT_REL,
    quantize_abs_down,
    quantize_abs_up,
    quantize_rel_down,
    quantize_rel_up,
    rel_quantum_to_sats,
)


class TestGridShape:
    def test_rel_grid_is_sorted_and_unique(self) -> None:
        values = list(QUANT_REL)
        assert values == sorted(values)
        assert len(set(values)) == len(values)

    def test_abs_grid_is_sorted_and_unique(self) -> None:
        values = list(QUANT_ABS)
        assert values == sorted(values)
        assert len(set(values)) == len(values)

    def test_smallest_rel_quantum(self) -> None:
        assert QUANT_REL[0] == Decimal("0.00002")

    def test_smallest_abs_quantum(self) -> None:
        assert QUANT_ABS[0] == 100


class TestQuantizeRelDown:
    @pytest.mark.parametrize(
        "rel_fee, expected",
        [
            ("0.001", Decimal("0.001")),  # exact grid point
            ("0.0015", Decimal("0.001")),  # between points -> floor
            ("0.1", Decimal("0.1")),  # largest grid point
            ("0.5", Decimal("0.1")),  # above grid -> clamps to largest
            ("0.00002", Decimal("0.00002")),  # smallest grid point
        ],
    )
    def test_floor_to_grid(self, rel_fee: str, expected: Decimal) -> None:
        assert quantize_rel_down(rel_fee) == expected

    def test_below_grid_returns_none(self) -> None:
        assert quantize_rel_down("0.00001") is None

    def test_accepts_float_and_decimal(self) -> None:
        assert quantize_rel_down(0.001) == Decimal("0.001")
        assert quantize_rel_down(Decimal("0.001")) == Decimal("0.001")


class TestQuantizeAbsDown:
    @pytest.mark.parametrize(
        "abs_fee, expected",
        [
            (500, 500),
            (750, 500),
            (10000, 10000),
            (50000, 10000),
            (100, 100),
        ],
    )
    def test_floor_to_grid(self, abs_fee: int, expected: int) -> None:
        assert quantize_abs_down(abs_fee) == expected

    def test_below_grid_returns_none(self) -> None:
        assert quantize_abs_down(99) is None


class TestQuantizeUp:
    def test_rel_up_rounds_up(self) -> None:
        assert quantize_rel_up("0.0015") == Decimal("0.002")
        assert quantize_rel_up("0.001") == Decimal("0.001")

    def test_rel_up_above_grid_returns_none(self) -> None:
        assert quantize_rel_up("0.5") is None

    def test_abs_up_rounds_up(self) -> None:
        assert quantize_abs_up(750) == 1000
        assert quantize_abs_up(500) == 500

    def test_abs_up_above_grid_returns_none(self) -> None:
        assert quantize_abs_up(50000) is None


class TestRelQuantumToSats:
    def test_basic_conversion(self) -> None:
        # 0.1% of 1_000_000 = 1000 sats
        assert rel_quantum_to_sats(Decimal("0.001"), 1_000_000) == 1000

    def test_banker_rounding_half_to_even(self) -> None:
        # 0.00002 * 25000 = 0.5 -> rounds to 0 (even)
        assert rel_quantum_to_sats(Decimal("0.00002"), 25000) == 0
        # 0.00002 * 75000 = 1.5 -> rounds to 2 (even)
        assert rel_quantum_to_sats(Decimal("0.00002"), 75000) == 2

    def test_zero_amount(self) -> None:
        assert rel_quantum_to_sats(Decimal("0.001"), 0) == 0
