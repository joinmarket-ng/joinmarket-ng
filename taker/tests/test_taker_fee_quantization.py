"""Unit tests for the taker-side FeeQuantizer policy."""

from __future__ import annotations

from decimal import Decimal

from taker.fee_quantization import FeeQuantizer


class TestFromLimits:
    def test_disabled_is_inactive(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.001", enabled=False)
        assert q.enabled is False
        assert q.active is False
        assert q.rel_quantum is None
        assert q.abs_quantum is None

    def test_default_limits_resolve_to_grid_points(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.001", enabled=True)
        assert q.rel_quantum == Decimal("0.001")
        assert q.abs_quantum == 500
        assert q.active is True

    def test_limits_floor_to_grid(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=750, rel_fee="0.0015", enabled=True)
        assert q.rel_quantum == Decimal("0.001")
        assert q.abs_quantum == 500

    def test_enabled_subgrid_abs_floors_to_free_band(self) -> None:
        # An absolute limit below the lowest paid quantum (100) floors to the
        # free band (0), and a relative limit below the grid yields no rel
        # quantum. The quantizer stays active and homogenizes every maker to a
        # zero absolute fee, so only free makers remain eligible.
        q = FeeQuantizer.from_limits(abs_fee=50, rel_fee="0.00001", enabled=True)
        assert q.enabled is True
        assert q.active is True
        assert q.rel_quantum is None
        assert q.abs_quantum == 0
        assert q.slot_fee(1_000_000) == 0


class TestSlotFee:
    def test_inactive_returns_none(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.001", enabled=False)
        assert q.slot_fee(1_000_000) is None

    def test_rel_dominates_for_large_amounts(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.001", enabled=True)
        # 0.1% of 10_000_000 = 10_000 > 500 abs
        assert q.slot_fee(10_000_000) == 10_000

    def test_abs_dominates_for_small_amounts(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.001", enabled=True)
        # 0.1% of 100_000 = 100 < 500 abs
        assert q.slot_fee(100_000) == 500

    def test_rel_dominates_over_free_abs_band(self) -> None:
        # abs limit below the lowest paid quantum floors to the free band (0),
        # so the relative quantum dominates the slot fee.
        q = FeeQuantizer.from_limits(abs_fee=50, rel_fee="0.001", enabled=True)
        assert q.abs_quantum == 0
        assert q.slot_fee(1_000_000) == 1000

    def test_only_abs_quantum(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.00001", enabled=True)
        assert q.rel_quantum is None
        assert q.slot_fee(1_000_000) == 500


class TestPaidFee:
    def test_inactive_returns_exact(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.001", enabled=False)
        assert q.paid_fee(123, 1_000_000) == 123

    def test_raises_cheap_maker_to_slot(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.001", enabled=True)
        # Maker advertised 200 sats, slot for 1M is 1000 -> pay 1000
        assert q.paid_fee(200, 1_000_000) == 1000

    def test_never_underpays_advertised(self) -> None:
        q = FeeQuantizer.from_limits(abs_fee=500, rel_fee="0.001", enabled=True)
        # An offer somehow above the slot still gets paid its exact fee
        assert q.paid_fee(5000, 1_000_000) == 5000
