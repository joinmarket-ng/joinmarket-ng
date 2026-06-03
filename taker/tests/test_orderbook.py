"""
Unit tests for orderbook management and order selection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jmcore.models import Offer, OfferType

from taker.config import MaxCjFee
from taker.orderbook import (
    OrderbookManager,
    calculate_cj_fee,
    cheapest_order_choose,
    choose_orders,
    choose_sweep_orders,
    dedupe_offers_by_bond,
    dedupe_offers_by_maker,
    fidelity_bond_weighted_choose,
    filter_offers,
    is_fee_within_limits,
    random_order_choose,
    weighted_order_choose,
)


@pytest.fixture
def sample_offers() -> list[Offer]:
    """Sample offers for testing."""
    return [
        Offer(
            counterparty="maker1",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee="0.001",
            fidelity_bond_value=100_000,
        ),
        Offer(
            counterparty="maker2",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=500_000,
            txfee=500,
            cjfee="0.0005",
            fidelity_bond_value=50_000,
        ),
        Offer(
            counterparty="maker3",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=10_000,
            maxsize=2_000_000,
            txfee=1500,
            cjfee=5000,  # Absolute fee
            fidelity_bond_value=200_000,
        ),
        Offer(
            counterparty="maker4",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=2000,
            cjfee="0.002",
            fidelity_bond_value=0,
        ),
    ]


@pytest.fixture
def max_cj_fee() -> MaxCjFee:
    """Default fee limits - generous enough to allow maker3's absolute fee at 50k."""
    return MaxCjFee(abs_fee=50_000, rel_fee="0.1")


class TestCalculateCjFee:
    """Tests for calculate_cj_fee."""

    def test_relative_fee(self) -> None:
        """Test relative fee calculation."""
        offer = Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee="0.001",
        )
        # 0.1% of 100,000 = 100
        assert calculate_cj_fee(offer, 100_000) == 100

    def test_absolute_fee(self) -> None:
        """Test absolute fee calculation."""
        offer = Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee=5000,
        )
        # Fixed 5000 sats regardless of amount
        assert calculate_cj_fee(offer, 100_000) == 5000
        assert calculate_cj_fee(offer, 1_000_000) == 5000


class TestIsFeeWithinLimits:
    """Tests for is_fee_within_limits."""

    def test_within_limits(self, max_cj_fee: MaxCjFee) -> None:
        """Test relative fee within limits."""
        offer = Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee="0.001",  # 0.1% - checked against rel_fee limit
        )
        # 0.001 <= 0.1 (rel_fee), so it passes
        assert is_fee_within_limits(offer, 100_000, max_cj_fee) is True

    def test_exceeds_absolute_limit(self) -> None:
        """Test absolute fee exceeds absolute limit."""
        max_fee = MaxCjFee(abs_fee=1000, rel_fee="0.01")
        offer = Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee=5000,  # 5000 > 1000 abs_fee limit
        )
        assert is_fee_within_limits(offer, 100_000, max_fee) is False

    def test_exceeds_relative_limit(self) -> None:
        """Test relative fee exceeds relative limit."""
        max_fee = MaxCjFee(abs_fee=50_000, rel_fee="0.0005")  # 0.05%
        offer = Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee="0.001",  # 0.001 > 0.0005 rel_fee limit
        )
        assert is_fee_within_limits(offer, 100_000, max_fee) is False

    def test_absolute_within_abs_limit_even_if_high_for_amount(self) -> None:
        """Test that absolute offers are only checked against abs limit, not amount."""
        max_fee = MaxCjFee(abs_fee=10_000, rel_fee="0.001")  # 0.1%
        offer = Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee=5000,  # 5000 <= 10000 abs_fee, so it passes
        )
        # Even though 5000/100000 = 5% which exceeds the 0.1% rel_fee limit,
        # absolute offers are only checked against abs_fee
        assert is_fee_within_limits(offer, 100_000, max_fee) is True

    def test_relative_within_rel_limit_even_if_high_absolute(self) -> None:
        """Test that relative offers are only checked against rel limit, not absolute."""
        max_fee = MaxCjFee(abs_fee=100, rel_fee="0.01")  # 1%
        offer = Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=10_000_000,
            txfee=1000,
            cjfee="0.005",  # 0.5% - within 1% rel_fee limit
        )
        # At 10M sats, this would be 50,000 sats which exceeds abs_fee=100
        # But relative offers are only checked against rel_fee
        assert is_fee_within_limits(offer, 10_000_000, max_fee) is True


class TestFilterOffers:
    """Tests for filter_offers."""

    def test_filters_by_amount_range(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """Test filtering by amount range."""
        # maker4 requires minsize=100_000
        filtered = filter_offers(sample_offers, 50_000, max_cj_fee)
        assert len(filtered) == 3
        assert all(o.counterparty != "maker4" for o in filtered)

    def test_filters_ignored_makers(self, sample_offers: list[Offer], max_cj_fee: MaxCjFee) -> None:
        """Test filtering ignored makers."""
        filtered = filter_offers(
            sample_offers, 100_000, max_cj_fee, ignored_makers={"maker1", "maker2"}
        )
        assert len(filtered) == 2
        assert all(o.counterparty not in ("maker1", "maker2") for o in filtered)

    def test_filters_by_offer_type(self, sample_offers: list[Offer], max_cj_fee: MaxCjFee) -> None:
        """Test filtering by offer type."""
        filtered = filter_offers(
            sample_offers, 100_000, max_cj_fee, allowed_types={OfferType.SW0_ABSOLUTE}
        )
        assert len(filtered) == 1
        assert filtered[0].counterparty == "maker3"


class TestDedupeOffersByMaker:
    """Tests for dedupe_offers_by_maker."""

    def test_keeps_cheapest(self) -> None:
        """Test keeping only cheapest offer per maker."""
        offers = [
            Offer(
                counterparty="maker1",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.002",  # More expensive
            ),
            Offer(
                counterparty="maker1",
                oid=1,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",  # Cheaper
            ),
        ]
        deduped = dedupe_offers_by_maker(offers)
        assert len(deduped) == 1
        assert deduped[0].cjfee == "0.001"


class TestDedupeOffersByBond:
    """Tests for dedupe_offers_by_bond (sybil protection)."""

    def test_different_makers_same_bond_keeps_cheapest(self) -> None:
        """Two makers sharing same bond UTXO - keep only the cheapest."""
        bond_data = {
            "utxo_txid": "a" * 64,
            "utxo_vout": 0,
            "locktime": 500000,
            "utxo_pub": "pubkey",
            "cert_expiry": 1700000000,
        }
        offers = [
            Offer(
                counterparty="maker1",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.002",  # More expensive
                fidelity_bond_data=bond_data,
            ),
            Offer(
                counterparty="maker2",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",  # Cheaper
                fidelity_bond_data=bond_data,
            ),
        ]
        deduped = dedupe_offers_by_bond(offers, cj_amount=100_000)
        assert len(deduped) == 1
        assert deduped[0].counterparty == "maker2"  # Cheaper one kept

    def test_different_bonds_preserved(self) -> None:
        """Makers with different bonds are all preserved."""
        offers = [
            Offer(
                counterparty="maker1",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                fidelity_bond_data={
                    "utxo_txid": "a" * 64,
                    "utxo_vout": 0,
                    "locktime": 500000,
                    "utxo_pub": "pubkey1",
                    "cert_expiry": 1700000000,
                },
            ),
            Offer(
                counterparty="maker2",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                fidelity_bond_data={
                    "utxo_txid": "b" * 64,  # Different bond
                    "utxo_vout": 0,
                    "locktime": 500000,
                    "utxo_pub": "pubkey2",
                    "cert_expiry": 1700000000,
                },
            ),
        ]
        deduped = dedupe_offers_by_bond(offers, cj_amount=100_000)
        assert len(deduped) == 2

    def test_unbonded_offers_passed_through(self) -> None:
        """Offers without bonds pass through unchanged."""
        offers = [
            Offer(
                counterparty="maker1",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                # No fidelity_bond_data
            ),
            Offer(
                counterparty="maker2",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.002",
                # No fidelity_bond_data
            ),
        ]
        deduped = dedupe_offers_by_bond(offers, cj_amount=100_000)
        assert len(deduped) == 2

    def test_mixed_bonded_unbonded(self) -> None:
        """Mix of bonded and unbonded offers."""
        bond_data = {
            "utxo_txid": "a" * 64,
            "utxo_vout": 0,
            "locktime": 500000,
            "utxo_pub": "pubkey",
            "cert_expiry": 1700000000,
        }
        offers = [
            # Two makers sharing bond
            Offer(
                counterparty="bonded1",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.002",
                fidelity_bond_data=bond_data,
            ),
            Offer(
                counterparty="bonded2",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",  # Cheaper - this one should be kept
                fidelity_bond_data=bond_data,
            ),
            # Unbonded maker
            Offer(
                counterparty="unbonded",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.003",
            ),
        ]
        deduped = dedupe_offers_by_bond(offers, cj_amount=100_000)
        assert len(deduped) == 2
        nicks = {o.counterparty for o in deduped}
        assert "bonded2" in nicks  # Cheaper bonded
        assert "unbonded" in nicks  # Unbonded passes through

    def test_fee_comparison_uses_actual_cj_amount(self) -> None:
        """Fee comparison should use the actual cj_amount, not a reference amount."""
        bond_data = {
            "utxo_txid": "a" * 64,
            "utxo_vout": 0,
            "locktime": 500000,
            "utxo_pub": "pubkey",
            "cert_expiry": 1700000000,
        }
        offers = [
            Offer(
                counterparty="maker_abs",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee=5000,  # 5000 sats fixed
                fidelity_bond_data=bond_data,
            ),
            Offer(
                counterparty="maker_rel",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.01",  # 1%
                fidelity_bond_data=bond_data,
            ),
        ]

        # At 100k sats: abs=5000, rel=1000 -> rel wins
        deduped_small = dedupe_offers_by_bond(offers, cj_amount=100_000)
        assert len(deduped_small) == 1
        assert deduped_small[0].counterparty == "maker_rel"

        # At 1M sats: abs=5000, rel=10000 -> abs wins
        deduped_large = dedupe_offers_by_bond(offers, cj_amount=1_000_000)
        assert len(deduped_large) == 1
        assert deduped_large[0].counterparty == "maker_abs"


class TestOrderChoosers:
    """Tests for order selection algorithms."""

    def test_random_order_choose(self, sample_offers: list[Offer]) -> None:
        """Test random selection."""
        selected = random_order_choose(sample_offers, 2)
        assert len(selected) == 2
        assert all(o in sample_offers for o in selected)

    def test_random_order_choose_more_than_available(self, sample_offers: list[Offer]) -> None:
        """Test random selection when requesting more than available."""
        selected = random_order_choose(sample_offers, 10)
        assert len(selected) == len(sample_offers)

    def test_cheapest_order_choose(self, sample_offers: list[Offer]) -> None:
        """Test cheapest selection."""
        selected = cheapest_order_choose(sample_offers, 2, cj_amount=100_000)
        assert len(selected) == 2
        # maker2 (0.0005) and maker3 (5000 absolute = 5% at 100k) should be cheapest
        # Actually maker2 = 50 sats, maker3 = 5000 sats, maker1 = 100 sats
        nicks = {o.counterparty for o in selected}
        assert "maker2" in nicks  # Cheapest at 50 sats

    def test_weighted_order_choose(self, sample_offers: list[Offer]) -> None:
        """Test weighted selection."""
        selected = weighted_order_choose(sample_offers, 2)
        assert len(selected) == 2
        assert all(o in sample_offers for o in selected)

    def test_fidelity_bond_weighted_choose(self, sample_offers: list[Offer]) -> None:
        """Test fidelity bond weighted selection."""
        selected = fidelity_bond_weighted_choose(sample_offers, 2)
        assert len(selected) == 2
        # maker3 has highest bond value (200,000), should be frequently selected


class TestChooseOrders:
    """Tests for choose_orders."""

    def test_choose_orders(self, sample_offers: list[Offer], max_cj_fee: MaxCjFee) -> None:
        """Test full order selection flow."""
        orders, total_fee = choose_orders(
            offers=sample_offers,
            cj_amount=100_000,
            n=2,
            max_cj_fee=max_cj_fee,
        )
        assert len(orders) == 2
        assert total_fee > 0

    def test_choose_orders_not_enough_makers(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """Test when not enough makers available."""
        orders, total_fee = choose_orders(
            offers=sample_offers[:1],  # Only 1 offer
            cj_amount=100_000,
            n=3,
            max_cj_fee=max_cj_fee,
        )
        assert len(orders) == 1


class TestChooseSweepOrders:
    """Tests for choose_sweep_orders."""

    def test_choose_sweep_orders(self, max_cj_fee: MaxCjFee) -> None:
        """Test sweep order selection and amount calculation."""
        from taker.orderbook import choose_sweep_orders

        # Create specific offers for this test
        offers = [
            Offer(
                counterparty="maker1",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=200_000_000,  # Large enough
                txfee=1000,
                cjfee="0.001",  # 0.1%
                fidelity_bond_value=100_000,
            ),
            Offer(
                counterparty="maker2",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=200_000_000,  # Large enough
                txfee=500,
                cjfee="0.0005",  # 0.05%
                fidelity_bond_value=50_000,
            ),
        ]

        # Total input 1 BTC, txfee 10k sats
        # Makers: maker1 (0.1%), maker2 (0.05%)
        # Approx fees: 0.15% of ~1 BTC ~ 150k sats
        # expected cj_amount around 100M - 10k - 150k = 99.84M
        orders, cj_amount, total_fee = choose_sweep_orders(
            offers=offers,
            total_input_value=100_000_000,
            my_txfee=10_000,
            n=2,
            max_cj_fee=max_cj_fee,
        )
        assert len(orders) == 2
        assert cj_amount > 0
        assert total_fee > 0
        # Should be exactly equal or off by very small amount due to integer rounding
        # With integer arithmetic, we might leave 1-2 sats behind (miner donation)
        diff = 100_000_000 - (cj_amount + total_fee + 10_000)
        assert diff >= 0
        assert diff < 5

        # Verify cj_amount is calculated correctly with integer arithmetic
        # sum_rel_fees = 0.001 + 0.0005 = 0.0015
        # available = 100_000_000 - 10_000 = 99_990_000
        # expected = 99_990_000 / 1.0015 = 99,840,239 (rounded down)
        # Using integer arithmetic:
        # num=99990000, den=10000, sum_num=15
        # (99990000 * 10000) // (10000 + 15) = 999900000000 // 10015 = 99840239
        assert cj_amount == 99_840_239


class TestOrderbookManager:
    """Tests for OrderbookManager."""

    def test_update_offers(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """Test updating orderbook."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)
        assert len(manager.offers) == len(sample_offers)

    def test_add_ignored_maker(self, max_cj_fee: MaxCjFee, tmp_path: Path) -> None:
        """Test adding ignored maker."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.add_ignored_maker("bad_maker")
        assert "bad_maker" in manager.ignored_makers

        # Verify persistence
        ignored_path = tmp_path / "ignored_makers.txt"
        assert ignored_path.exists()
        with open(ignored_path, encoding="utf-8") as f:
            makers = {line.strip() for line in f}
        assert "bad_maker" in makers

    def test_ignored_makers_persistence(self, max_cj_fee: MaxCjFee, tmp_path: Path) -> None:
        """Test that ignored makers persist across manager instances."""
        # First manager adds ignored makers
        manager1 = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager1.add_ignored_maker("maker1")
        manager1.add_ignored_maker("maker2")
        assert len(manager1.ignored_makers) == 2

        # Second manager should load the persisted ignored makers
        manager2 = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        assert len(manager2.ignored_makers) == 2
        assert "maker1" in manager2.ignored_makers
        assert "maker2" in manager2.ignored_makers

    def test_clear_ignored_makers(self, max_cj_fee: MaxCjFee, tmp_path: Path) -> None:
        """Test clearing ignored makers."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.add_ignored_maker("maker1")
        manager.add_ignored_maker("maker2")
        assert len(manager.ignored_makers) == 2

        ignored_path = tmp_path / "ignored_makers.txt"
        assert ignored_path.exists()

        manager.clear_ignored_makers()
        assert len(manager.ignored_makers) == 0
        assert not ignored_path.exists()

    def test_add_honest_maker(self, max_cj_fee: MaxCjFee, tmp_path: Path) -> None:
        """Test adding honest maker."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.add_honest_maker("good_maker")
        assert "good_maker" in manager.honest_makers

    def test_select_makers(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """Test maker selection."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)

        orders, fee = manager.select_makers(cj_amount=100_000, n=2)
        assert len(orders) == 2
        assert fee > 0

    def test_select_makers_honest_only(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """Test honest-only maker selection."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)
        manager.add_honest_maker("maker1")

        orders, fee = manager.select_makers(cj_amount=100_000, n=2, honest_only=True)
        # Only maker1 is honest
        assert len(orders) <= 1

    def test_select_makers_exclude_nicks(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """Test maker selection with explicit nick exclusion.

        This tests the exclude_nicks parameter used during maker replacement
        to avoid re-selecting makers that are already in the current session.
        """
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)

        # First, select some makers without exclusion
        orders1, _ = manager.select_makers(cj_amount=100_000, n=2)
        assert len(orders1) == 2

        # Get the nicks of selected makers
        selected_nicks = set(orders1.keys())

        # Now select again, excluding the previously selected makers
        orders2, _ = manager.select_makers(
            cj_amount=100_000,
            n=2,
            exclude_nicks=selected_nicks,
        )

        # The newly selected makers should not overlap with the first selection
        new_nicks = set(orders2.keys())
        assert len(new_nicks & selected_nicks) == 0, "Should not re-select excluded makers"

    def test_select_makers_exclude_nicks_combined_with_ignored(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """Test that exclude_nicks works together with ignored_makers."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)

        # Ignore maker1
        manager.add_ignored_maker("maker1")

        # Exclude maker2 via parameter
        exclude = {"maker2"}

        # Try to select makers (should not get maker1 or maker2)
        orders, _ = manager.select_makers(
            cj_amount=100_000,
            n=2,
            exclude_nicks=exclude,
        )

        # Verify neither excluded maker is in the result
        assert "maker1" not in orders
        assert "maker2" not in orders

    def test_select_makers_excludes_own_wallet_nicks(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """Test that own_wallet_nicks are automatically excluded from selection."""
        # Initialize with own wallet nicks (simulating same wallet maker nick)
        own_wallet_nicks = {"maker1"}
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path, own_wallet_nicks=own_wallet_nicks)
        manager.update_offers(sample_offers)

        # Try to select makers (should not get maker1)
        orders, _ = manager.select_makers(cj_amount=100_000, n=3)

        # Verify own wallet nick is excluded
        assert "maker1" not in orders

    def test_select_makers_own_wallet_nicks_combined_with_excluded(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """Test own_wallet_nicks combined with exclude_nicks and ignored_makers.

        With soft fallback: ``own_wallet_nicks`` is hard (always excluded),
        but ``ignored_makers`` and ``exclude_nicks`` are soft and may be
        relaxed when too few makers remain. Here we ask for n=1 from a pool
        of 4 with maker1 hard-excluded and maker2/maker3 soft-excluded; the
        strict pass returns maker4 only and that's enough to satisfy n=1
        without falling back.
        """
        # Initialize with own wallet nick
        own_wallet_nicks = {"maker1"}
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path, own_wallet_nicks=own_wallet_nicks)
        manager.update_offers(sample_offers)

        # Ignore maker2
        manager.add_ignored_maker("maker2")

        # Exclude maker3 via parameter
        exclude = {"maker3"}

        # Select one maker. Strict pass yields maker4 -> no soft fallback.
        orders, _ = manager.select_makers(cj_amount=100_000, n=1, exclude_nicks=exclude)

        # All three are excluded in this case (no fallback needed).
        assert "maker1" not in orders  # own wallet nick (hard)
        assert "maker2" not in orders  # ignored (soft, but enough makers without it)
        assert "maker3" not in orders  # excluded via parameter (soft, ditto)
        assert "maker4" in orders

    def test_soft_fallback_relaxes_ignored_when_pool_too_small(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """``ignored_makers`` is best-effort: relaxed when not enough makers remain.

        Bug: a CoinJoin must not fail just because every remaining maker is on
        the persisted ignore list. Falling back to a previously-ignored maker
        is strictly preferable to failing the whole tumble phase.
        """
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)

        # Persistently ignore three of the four makers, leaving only maker1.
        manager.add_ignored_maker("maker2")
        manager.add_ignored_maker("maker3")
        manager.add_ignored_maker("maker4")

        # Ask for two: strict pass would only return maker1, so the soft
        # fallback must kick in and relax the ignored set.
        orders, _ = manager.select_makers(cj_amount=100_000, n=2)

        assert len(orders) == 2, "Soft fallback should refill from ignored makers"
        assert "maker1" in orders

    def test_soft_fallback_relaxes_exclude_nicks(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """The tumbler's cross-phase ``exclude_nicks`` is also best-effort.

        Repeating a maker across tumble phases hurts privacy a little, but
        failing the phase outright is worse: the user has to manually resume
        and the in-flight CoinJoin amount is locked in maker funds.
        """
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)

        # Soft-exclude three of four makers via the per-call parameter.
        orders, _ = manager.select_makers(
            cj_amount=100_000,
            n=3,
            exclude_nicks={"maker2", "maker3", "maker4"},
        )

        assert len(orders) == 3, "Soft exclude_nicks must be relaxed when pool is too small"

    def test_hard_exclude_nicks_never_relaxed(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """``hard_exclude_nicks`` are strict even when the pool is too small.

        Used by the taker for makers that *just* failed in the current
        attempt (re-asking them would fail again) and for own-wallet nicks.
        Returning fewer makers is the correct outcome -- the caller decides
        whether to fail or proceed below ``minimum_makers``.
        """
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)

        # Hard-exclude three of four; even when n=3 we must not return them.
        orders, _ = manager.select_makers(
            cj_amount=100_000,
            n=3,
            hard_exclude_nicks={"maker2", "maker3", "maker4"},
        )

        assert "maker2" not in orders
        assert "maker3" not in orders
        assert "maker4" not in orders
        assert len(orders) <= 1

    def test_own_wallet_nicks_never_relaxed_via_soft_fallback(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """Own-wallet nicks must never be re-introduced by the soft fallback.

        Selecting one's own maker as counterparty would link wallet identities,
        defeating the privacy of the CoinJoin. This invariant is non-negotiable
        regardless of how starved the pool gets.
        """
        own_wallet_nicks = {"maker1"}
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path, own_wallet_nicks=own_wallet_nicks)
        manager.update_offers(sample_offers)

        # Ignore (soft) every other maker. Even with the soft fallback, the
        # own-wallet nick must stay excluded.
        for nick in ("maker2", "maker3", "maker4"):
            manager.add_ignored_maker(nick)

        orders, _ = manager.select_makers(cj_amount=100_000, n=2)

        assert "maker1" not in orders, "Own wallet nick must never be selected"

    def test_sweep_soft_fallback_relaxes_ignored(
        self, sample_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """``select_makers_for_sweep`` honors the same soft-exclusion fallback."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(sample_offers)

        manager.add_ignored_maker("maker2")
        manager.add_ignored_maker("maker3")
        manager.add_ignored_maker("maker4")

        orders, _cj_amount, _fee = manager.select_makers_for_sweep(
            total_input_value=1_000_000,
            my_txfee=2000,
            n=2,
        )

        assert len(orders) == 2, "Sweep soft fallback should refill from ignored makers"


class TestMixedBondedBondlessSelection:
    """Tests for the per-slot probabilistic bonded/bondless selection."""

    def test_always_fills_n_slots(self) -> None:
        """Regardless of coin flips, we always fill exactly n slots."""
        offers = [
            Offer(
                counterparty=f"Maker{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=1000 if i < 5 else 0,  # First 5 bonded
            )
            for i in range(10)
        ]

        for _ in range(20):
            selected = fidelity_bond_weighted_choose(
                offers=offers, n=3, bondless_makers_allowance=0.2, bondless_require_zero_fee=False
            )
            assert len(selected) == 3

    def test_fills_all_slots(self) -> None:
        """Ensure we always fill all n slots when enough offers exist."""
        offers = [
            Offer(
                counterparty=f"BondedMaker{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=100000,
            )
            for i in range(2)
        ] + [
            Offer(
                counterparty=f"BondlessMaker{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=0,
            )
            for i in range(8)
        ]

        # Should always get exactly 5 makers
        for _ in range(10):
            selected = fidelity_bond_weighted_choose(
                offers=offers,
                n=5,
                bondless_makers_allowance=0.2,
                bondless_require_zero_fee=False,
            )
            assert len(selected) == 5

    def test_bonded_makers_prioritized(self) -> None:
        """High-bond makers should be heavily favored in bonded slots."""
        high_bond = Offer(
            counterparty="HighBond",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=1000,
            maxsize=1000000,
            txfee=0,
            cjfee=0,
            fidelity_bond_value=1_000_000_000,  # 1B sats
        )

        low_bonds = [
            Offer(
                counterparty=f"LowBond{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=1000,  # 1k sats
            )
            for i in range(9)
        ]

        offers = [high_bond] + low_bonds

        # Run 100 times, high bond should be selected almost always
        # Each slot has 80% chance of being bonded, and HighBond dominates
        high_bond_count = 0
        for _ in range(100):
            selected = fidelity_bond_weighted_choose(
                offers=offers, n=3, bondless_makers_allowance=0.2, bondless_require_zero_fee=False
            )
            if high_bond in selected:
                high_bond_count += 1

        # Should be selected in >90% of runs
        assert high_bond_count > 90

    def test_bondless_zero_fee_filter(self) -> None:
        """Non-zero-fee bondless offers are pre-filtered out entirely."""
        bonded = [
            Offer(
                counterparty=f"Bonded{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=100000,
            )
            for i in range(5)
        ]

        # Zero fee bondless -- should survive pre-filter
        zero_fee = [
            Offer(
                counterparty=f"ZeroFee{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=0,
            )
            for i in range(3)
        ]

        # Non-zero fee bondless -- should be pre-filtered out
        nonzero_fee = [
            Offer(
                counterparty=f"NonZeroFee{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=100,
                fidelity_bond_value=0,
            )
            for i in range(3)
        ]

        offers = bonded + zero_fee + nonzero_fee

        # NonZeroFee makers should never appear (pre-filtered)
        for _ in range(20):
            selected = fidelity_bond_weighted_choose(
                offers=offers, n=3, bondless_makers_allowance=0.5, bondless_require_zero_fee=True
            )
            assert len(selected) == 3
            selected_nicks = {o.counterparty for o in selected}
            nonzero_nicks = {o.counterparty for o in nonzero_fee}
            assert len(selected_nicks & nonzero_nicks) == 0

    def test_insufficient_bonded_fills_from_all(self) -> None:
        """If not enough bonded offers, fill remainder from all remaining."""
        # Only 1 bonded maker
        bonded = Offer(
            counterparty="OnlyBonded",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=1000,
            maxsize=1000000,
            txfee=0,
            cjfee=0,
            fidelity_bond_value=100000,
        )

        # 5 bondless makers
        bondless = [
            Offer(
                counterparty=f"Bondless{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=0,
            )
            for i in range(5)
        ]

        offers = [bonded] + bondless

        # With low bondless allowance, the bonded maker should be selected
        # most of the time (80% of slots try bonded first)
        bonded_count = 0
        for _ in range(100):
            selected = fidelity_bond_weighted_choose(
                offers=offers, n=4, bondless_makers_allowance=0.2, bondless_require_zero_fee=False
            )
            assert len(selected) == 4
            if bonded in selected:
                bonded_count += 1

        # Bonded maker should be selected in most runs
        assert bonded_count > 70

    def test_per_slot_coin_flip_varies_bondless_count(self) -> None:
        """The number of bondless picks should vary across runs (not deterministic)."""
        bonded = [
            Offer(
                counterparty=f"Bonded{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=100000,
            )
            for i in range(20)
        ]

        bondless = [
            Offer(
                counterparty=f"Bondless{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=0,
            )
            for i in range(20)
        ]

        offers = bonded + bondless

        bondless_counts: set[int] = set()
        for _ in range(100):
            selected = fidelity_bond_weighted_choose(
                offers=offers,
                n=10,
                bondless_makers_allowance=0.3,
                bondless_require_zero_fee=False,
            )
            assert len(selected) == 10
            num_bondless = sum(1 for o in selected if o.fidelity_bond_value == 0)
            bondless_counts.add(num_bondless)

        # With per-slot coin flip (p=0.3), we should see varying counts
        assert len(bondless_counts) >= 3

    def test_zero_allowance_selects_only_bonded(self) -> None:
        """With allowance=0, all slots should use bonded weighted selection."""
        bonded = [
            Offer(
                counterparty=f"Bonded{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=100000,
            )
            for i in range(10)
        ]

        bondless = [
            Offer(
                counterparty=f"Bondless{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=0,
            )
            for i in range(10)
        ]

        offers = bonded + bondless

        for _ in range(20):
            selected = fidelity_bond_weighted_choose(
                offers=offers, n=5, bondless_makers_allowance=0.0, bondless_require_zero_fee=False
            )
            assert len(selected) == 5
            # All should be bonded
            assert all(o.fidelity_bond_value > 0 for o in selected)

    def test_bondless_slot_picks_uniformly_from_all(self) -> None:
        """Bondless (uniform) slots pick from ALL offers, not just bondless."""
        # 50 bonded + 1 bondless. With high allowance, the bondless maker
        # should appear rarely because they compete with 50 others uniformly.
        bonded = [
            Offer(
                counterparty=f"Bonded{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,
                fidelity_bond_value=100000,
            )
            for i in range(50)
        ]

        bondless_maker = Offer(
            counterparty="RareBondless",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=1000,
            maxsize=1000000,
            txfee=0,
            cjfee=0,
            fidelity_bond_value=0,
        )

        offers = bonded + [bondless_maker]

        # Run many times: bondless maker should appear infrequently
        appearances = 0
        runs = 500
        for _ in range(runs):
            selected = fidelity_bond_weighted_choose(
                offers=offers,
                n=10,
                bondless_makers_allowance=0.2,
                bondless_require_zero_fee=False,
            )
            if bondless_maker in selected:
                appearances += 1

        # With 51 offers, P(per uniform slot) = 1/51 ≈ 0.02
        # Expected uniform slots per run = 10 * 0.2 = 2
        # P(picked in run) ≈ 1 - (1 - 1/51)^2 ≈ 0.039
        # So appearances should be roughly 2-6% of runs
        # Allow generous bounds for statistical test
        assert appearances < runs * 0.15, (
            f"Bondless maker appeared {appearances}/{runs} times "
            f"({appearances / runs:.1%}), expected <15%"
        )


class TestFilterOffersByNickVersion:
    """Tests for filtering offers by nick version (reserved for future reference compat).

    NOTE: Nick version filtering is NOT used for neutrino detection - that uses
    handshake features instead. These tests ensure the filter logic works correctly
    for potential future reference implementation compatibility.
    """

    @pytest.fixture
    def mixed_version_offers(self) -> list[Offer]:
        """Offers from makers with different nicks (all J5 in our implementation)."""
        return [
            Offer(
                counterparty="J5oldmaker123OOO",  # maker 1
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
            ),
            Offer(
                counterparty="J5newmaker456OOO",  # maker 2
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
            ),
            Offer(
                counterparty="J5another789OOO",  # maker 3
                oid=1,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=500_000,
                txfee=500,
                cjfee="0.0005",
            ),
        ]

    def test_filter_no_version_requirement(
        self, mixed_version_offers: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """Without version requirement, all offers pass."""
        eligible = filter_offers(
            offers=mixed_version_offers,
            cj_amount=100_000,
            max_cj_fee=max_cj_fee,
            min_nick_version=None,
        )
        assert len(eligible) == 3

    def test_filter_min_version(
        self, mixed_version_offers: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """Test min_nick_version filtering (for potential future reference compat)."""
        # In our implementation all makers use v5, but filter logic remains for future compat
        eligible = filter_offers(
            offers=mixed_version_offers,
            cj_amount=100_000,
            max_cj_fee=max_cj_fee,
            min_nick_version=6,  # Would filter for hypothetical future nick versions
        )
        # All our test makers are J5, so none pass
        assert len(eligible) == 0

    def test_choose_orders_with_version_filter(
        self, mixed_version_offers: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """choose_orders respects min_nick_version (for reference compat)."""
        orders, fee = choose_orders(
            offers=mixed_version_offers,
            cj_amount=100_000,
            n=2,
            max_cj_fee=max_cj_fee,
            min_nick_version=5,  # Our makers are J5
        )
        assert len(orders) == 2
        for nick in orders.keys():
            assert nick.startswith("J5")

    def test_orderbook_manager_with_version_filter(
        self, mixed_version_offers: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """OrderbookManager.select_makers respects min_nick_version."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(mixed_version_offers)

        orders, fee = manager.select_makers(cj_amount=100_000, n=2, min_nick_version=5)
        assert len(orders) == 2
        for nick in orders.keys():
            assert nick.startswith("J5")

    def test_not_enough_makers_with_min_version(
        self, mixed_version_offers: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """When not enough makers meet version requirement, return what's available."""
        orders, fee = choose_orders(
            offers=mixed_version_offers,
            cj_amount=100_000,
            n=5,  # Request more than total available
            max_cj_fee=max_cj_fee,
            min_nick_version=5,
        )
        # Only 3 J5 makers available
        assert len(orders) == 3


class TestRequiredFeaturesFiltering:
    """Tests for required_features filtering in offer selection."""

    @pytest.fixture
    def max_cj_fee(self) -> MaxCjFee:
        return MaxCjFee(abs_fee=50_000, rel_fee="0.1")

    @pytest.fixture
    def offers_with_features(self) -> list[Offer]:
        """Offers with varying neutrino_compat feature status."""
        return [
            # Maker with neutrino_compat confirmed via peerlist_features
            Offer(
                counterparty="J5compatible1OOO",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                features={"neutrino_compat": True},
            ),
            # Maker confirmed as NOT supporting neutrino_compat
            Offer(
                counterparty="J5incompatible1O",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                features={"neutrino_compat": False},
            ),
            # Maker with unknown feature status (no peerlist_features directory)
            Offer(
                counterparty="J5unknown1OOOOOO",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                features={},
            ),
            # Another compatible maker
            Offer(
                counterparty="J5compatible2OOO",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=500,
                cjfee="0.0005",
                features={"neutrino_compat": True},
            ),
            # Maker with neutrino_compat via deprecated !neutrino flag (no features)
            Offer(
                counterparty="J5legacyneutrinoO",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                neutrino_compat=True,
                features={},
            ),
        ]

    def test_no_required_features_passes_all(
        self, offers_with_features: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """Without required_features, all offers pass (feature filtering disabled)."""
        eligible = filter_offers(
            offers=offers_with_features,
            cj_amount=100_000,
            max_cj_fee=max_cj_fee,
            required_features=None,
        )
        assert len(eligible) == 5

    def test_required_features_filters_known_incompatible(
        self, offers_with_features: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """Offers from makers known to lack required features are filtered out."""
        eligible = filter_offers(
            offers=offers_with_features,
            cj_amount=100_000,
            max_cj_fee=max_cj_fee,
            required_features={"neutrino_compat"},
        )
        # Should include: compatible1, unknown1, compatible2, legacyneutrino
        # Should exclude: incompatible1 (features dict says neutrino_compat=False)
        assert len(eligible) == 4
        nicks = {o.counterparty for o in eligible}
        assert "J5compatible1OOO" in nicks
        assert "J5compatible2OOO" in nicks
        assert "J5unknown1OOOOOO" in nicks  # Unknown status passes through
        assert "J5legacyneutrinoO" in nicks  # Empty features = unknown, passes
        assert "J5incompatible1O" not in nicks  # Known incompatible

    def test_unknown_features_pass_through(self, max_cj_fee: MaxCjFee) -> None:
        """Offers with empty features dict (unknown) are NOT filtered out."""
        offers = [
            Offer(
                counterparty="J5unknown1OOOOOO",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                features={},  # Unknown -- no directory supports peerlist_features
            ),
        ]
        eligible = filter_offers(
            offers=offers,
            cj_amount=100_000,
            max_cj_fee=max_cj_fee,
            required_features={"neutrino_compat"},
        )
        assert len(eligible) == 1

    def test_empty_required_features_passes_all(
        self, offers_with_features: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """Empty required_features set doesn't filter anything."""
        eligible = filter_offers(
            offers=offers_with_features,
            cj_amount=100_000,
            max_cj_fee=max_cj_fee,
            required_features=set(),
        )
        assert len(eligible) == 5

    def test_choose_orders_with_required_features(
        self, offers_with_features: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """choose_orders passes required_features to filter_offers."""
        orders, fee = choose_orders(
            offers=offers_with_features,
            cj_amount=100_000,
            n=2,
            max_cj_fee=max_cj_fee,
            required_features={"neutrino_compat"},
        )
        assert len(orders) == 2
        # None of the selected should be the known-incompatible maker
        assert "J5incompatible1O" not in orders

    def test_orderbook_manager_with_required_features(
        self, offers_with_features: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """OrderbookManager.select_makers respects required_features."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(offers_with_features)

        orders, fee = manager.select_makers(
            cj_amount=100_000, n=2, required_features={"neutrino_compat"}
        )
        assert len(orders) == 2
        assert "J5incompatible1O" not in orders

    def test_all_known_incompatible_returns_zero(self, max_cj_fee: MaxCjFee) -> None:
        """When all offers are known-incompatible, zero are returned."""
        offers = [
            Offer(
                counterparty=f"J5incompat{i}OOOO",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                features={"neutrino_compat": False},
            )
            for i in range(5)
        ]
        eligible = filter_offers(
            offers=offers,
            cj_amount=100_000,
            max_cj_fee=max_cj_fee,
            required_features={"neutrino_compat"},
        )
        assert len(eligible) == 0

    def test_feature_with_true_in_dict_passes(self, max_cj_fee: MaxCjFee) -> None:
        """Offer with the required feature set to True passes."""
        offers = [
            Offer(
                counterparty="J5compat1OOOOOOO",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=1_000_000,
                txfee=1000,
                cjfee="0.001",
                features={"neutrino_compat": True, "peerlist_features": True},
            ),
        ]
        eligible = filter_offers(
            offers=offers,
            cj_amount=100_000,
            max_cj_fee=max_cj_fee,
            required_features={"neutrino_compat"},
        )
        assert len(eligible) == 1

    def test_choose_sweep_orders_with_required_features(
        self, offers_with_features: list[Offer], max_cj_fee: MaxCjFee
    ) -> None:
        """choose_sweep_orders passes required_features to filter_offers."""
        orders, cj_amount, fee = choose_sweep_orders(
            offers=offers_with_features,
            total_input_value=500_000,
            my_txfee=1000,
            n=2,
            max_cj_fee=max_cj_fee,
            required_features={"neutrino_compat"},
        )
        assert len(orders) == 2
        # Known-incompatible maker should not be selected
        assert "J5incompatible1O" not in orders

    def test_orderbook_manager_sweep_with_required_features(
        self, offers_with_features: list[Offer], max_cj_fee: MaxCjFee, tmp_path: Path
    ) -> None:
        """OrderbookManager.select_makers_for_sweep respects required_features."""
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
        manager.update_offers(offers_with_features)

        orders, cj_amount, fee = manager.select_makers_for_sweep(
            total_input_value=500_000,
            my_txfee=1000,
            n=2,
            required_features={"neutrino_compat"},
        )
        assert len(orders) == 2
        assert "J5incompatible1O" not in orders


class TestFeeQuantization:
    """Tests for issue #508 fee homogenization in maker selection."""

    @pytest.fixture
    def quant_offers(self) -> list[Offer]:
        # A spread of cheap relative offers, all below the 0.001 quantum.
        # Marked NG (non-empty features) so they are homogenized by the quantizer.
        return [
            Offer(
                counterparty=f"maker{i}",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=200_000_000,
                txfee=0,
                cjfee=fee,
                fidelity_bond_value=100_000,
                features={"neutrino_compat": True},
            )
            for i, fee in enumerate(("0.00002", "0.0001", "0.0002", "0.0005"))
        ]

    def test_filter_offers_excludes_above_quantum(self, quant_offers: list[Offer]) -> None:
        from taker.fee_quantization import FeeQuantizer

        # Quantum slot for cj=1M with rel limit 0.0002 -> 0.0002 grid -> 200 sats.
        quantizer = FeeQuantizer.from_limits(abs_fee=0, rel_fee="0.0002", enabled=True)
        max_fee = MaxCjFee(abs_fee=0, rel_fee="0.0002")
        eligible = filter_offers(
            offers=quant_offers,
            cj_amount=1_000_000,
            max_cj_fee=max_fee,
            fee_quantizer=quantizer,
        )
        # 0.0005 offer (500 sats > 200 slot) is excluded; the rest pass.
        names = {o.counterparty for o in eligible}
        assert "maker3" not in names
        assert {"maker0", "maker1", "maker2"} <= names

    def test_choose_orders_homogenizes_total_fee(self, quant_offers: list[Offer]) -> None:
        from taker.fee_quantization import FeeQuantizer

        quantizer = FeeQuantizer.from_limits(abs_fee=0, rel_fee="0.001", enabled=True)
        max_fee = MaxCjFee(abs_fee=0, rel_fee="0.001")
        cj_amount = 1_000_000
        orders, total_fee = choose_orders(
            offers=quant_offers,
            cj_amount=cj_amount,
            n=4,
            max_cj_fee=max_fee,
            fee_quantizer=quantizer,
        )
        # Slot fee = 0.001 * 1M = 1000 sats per maker, regardless of advertised.
        slot = quantizer.slot_fee(cj_amount)
        assert slot == 1000
        assert total_fee == slot * len(orders)

    def test_choose_orders_unquantized_uses_advertised(self, quant_offers: list[Offer]) -> None:
        max_fee = MaxCjFee(abs_fee=0, rel_fee="0.001")
        cj_amount = 1_000_000
        orders, total_fee = choose_orders(
            offers=quant_offers,
            cj_amount=cj_amount,
            n=4,
            max_cj_fee=max_fee,
            fee_quantizer=None,
        )
        expected = sum(calculate_cj_fee(o, cj_amount) for o in orders.values())
        assert total_fee == expected

    def test_sweep_quantized_pays_equal_fee_and_zeroes_change(
        self, quant_offers: list[Offer]
    ) -> None:
        from taker.fee_quantization import FeeQuantizer

        quantizer = FeeQuantizer.from_limits(abs_fee=0, rel_fee="0.001", enabled=True)
        max_fee = MaxCjFee(abs_fee=0, rel_fee="0.001")
        total_input = 100_000_000
        my_txfee = 10_000
        orders, cj_amount, total_fee = choose_sweep_orders(
            offers=quant_offers,
            total_input_value=total_input,
            my_txfee=my_txfee,
            n=4,
            max_cj_fee=max_fee,
            fee_quantizer=quantizer,
        )
        assert len(orders) == 4
        slot = quantizer.slot_fee(cj_amount)
        assert total_fee == slot * len(orders)
        # Change should be (near) zero: residual under 1 sat per maker from rounding.
        residual = total_input - my_txfee - cj_amount - total_fee
        assert abs(residual) <= len(orders)

    @pytest.fixture
    def legacy_offers(self) -> list[Offer]:
        # Same spread but with NO features: legacy (clientserver) makers that
        # reject overpayment and must be paid their exact advertised fee.
        return [
            Offer(
                counterparty=f"legacy{i}",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=200_000_000,
                txfee=0,
                cjfee=fee,
                fidelity_bond_value=100_000,
            )
            for i, fee in enumerate(("0.00002", "0.0001", "0.0002", "0.0005"))
        ]

    def test_maker_paid_fee_legacy_uses_exact(self, legacy_offers: list[Offer]) -> None:
        from taker.fee_quantization import FeeQuantizer
        from taker.orderbook import maker_paid_fee, offer_is_ng

        quantizer = FeeQuantizer.from_limits(abs_fee=0, rel_fee="0.001", enabled=True)
        cj_amount = 1_000_000
        for offer in legacy_offers:
            assert not offer_is_ng(offer)
            # Legacy makers are never overpaid: paid fee equals their exact fee.
            assert maker_paid_fee(offer, cj_amount, quantizer) == calculate_cj_fee(offer, cj_amount)

    def test_choose_orders_legacy_not_homogenized(self, legacy_offers: list[Offer]) -> None:
        from taker.fee_quantization import FeeQuantizer

        quantizer = FeeQuantizer.from_limits(abs_fee=0, rel_fee="0.001", enabled=True)
        max_fee = MaxCjFee(abs_fee=0, rel_fee="0.001")
        cj_amount = 1_000_000
        orders, total_fee = choose_orders(
            offers=legacy_offers,
            cj_amount=cj_amount,
            n=4,
            max_cj_fee=max_fee,
            fee_quantizer=quantizer,
        )
        # Legacy makers are paid exactly their advertised fee, not the slot.
        expected = sum(calculate_cj_fee(o, cj_amount) for o in orders.values())
        assert total_fee == expected

    def test_choose_orders_mixed_homogenizes_only_ng(
        self, quant_offers: list[Offer], legacy_offers: list[Offer]
    ) -> None:
        from taker.fee_quantization import FeeQuantizer

        quantizer = FeeQuantizer.from_limits(abs_fee=0, rel_fee="0.001", enabled=True)
        max_fee = MaxCjFee(abs_fee=0, rel_fee="0.001")
        cj_amount = 1_000_000
        orders, total_fee = choose_orders(
            offers=quant_offers + legacy_offers,
            cj_amount=cj_amount,
            n=8,
            max_cj_fee=max_fee,
            fee_quantizer=quantizer,
        )
        slot = quantizer.slot_fee(cj_amount)
        expected = sum(
            slot if o.features else calculate_cj_fee(o, cj_amount) for o in orders.values()
        )
        assert total_fee == expected

    def test_sweep_mixed_zeroes_change(
        self, quant_offers: list[Offer], legacy_offers: list[Offer]
    ) -> None:
        from taker.fee_quantization import FeeQuantizer
        from taker.orderbook import maker_paid_fee

        quantizer = FeeQuantizer.from_limits(abs_fee=0, rel_fee="0.001", enabled=True)
        max_fee = MaxCjFee(abs_fee=0, rel_fee="0.001")
        total_input = 100_000_000
        my_txfee = 10_000
        orders, cj_amount, total_fee = choose_sweep_orders(
            offers=quant_offers + legacy_offers,
            total_input_value=total_input,
            my_txfee=my_txfee,
            n=8,
            max_cj_fee=max_fee,
            fee_quantizer=quantizer,
        )
        # Per-maker fee vector: NG paid slot, legacy paid exact.
        expected_fee = sum(maker_paid_fee(o, cj_amount, quantizer) for o in orders.values())
        assert total_fee == expected_fee
        # No change in a sweep: residual is a few sats from integer rounding.
        residual = total_input - my_txfee - cj_amount - total_fee
        assert 0 <= residual <= len(orders)
