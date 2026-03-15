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
    dedupe_offers_by_bond,
    dedupe_offers_by_maker,
    equalize_maker_fees,
    fidelity_bond_weighted_choose,
    filter_offers,
    is_fee_within_limits,
    random_order_choose,
    sample_fake_fee_from_orderbook,
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
        """Test own_wallet_nicks combined with exclude_nicks and ignored_makers."""
        # Initialize with own wallet nick
        own_wallet_nicks = {"maker1"}
        manager = OrderbookManager(max_cj_fee, data_dir=tmp_path, own_wallet_nicks=own_wallet_nicks)
        manager.update_offers(sample_offers)

        # Ignore maker2
        manager.add_ignored_maker("maker2")

        # Exclude maker3 via parameter
        exclude = {"maker3"}

        # Select makers
        orders, _ = manager.select_makers(cj_amount=100_000, n=2, exclude_nicks=exclude)

        # Verify all three are excluded
        assert "maker1" not in orders  # own wallet nick
        assert "maker2" not in orders  # ignored
        assert "maker3" not in orders  # excluded via parameter


class TestMixedBondedBondlessSelection:
    """Tests for the mixed bonded/bondless selection strategy."""

    def test_deterministic_split(self) -> None:
        """Test that the split between bonded and bondless is deterministic."""
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

        # With 3 makers and 0.125 allowance: bonded = floor(3 * 0.875) = 2
        selected = fidelity_bond_weighted_choose(
            offers=offers, n=3, bondless_makers_allowance=0.125, bondless_require_zero_fee=False
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
        # With allowance=0.2, bonded slots = floor(3 * 0.8) = 2
        # HighBond should win at least one of these slots nearly every time
        high_bond_count = 0
        for _ in range(100):
            selected = fidelity_bond_weighted_choose(
                offers=offers, n=3, bondless_makers_allowance=0.2, bondless_require_zero_fee=False
            )
            if high_bond in selected:
                high_bond_count += 1

        # Should be selected in >90% of runs
        assert high_bond_count > 90

    def test_bondless_fills_remaining_with_zero_fee(self) -> None:
        """Bondless slots should fill from zero-fee offers when required."""
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
            for i in range(2)
        ]

        # Zero fee bondless
        zero_fee = [
            Offer(
                counterparty=f"ZeroFee{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=0,  # Zero fee
                fidelity_bond_value=0,
            )
            for i in range(3)
        ]

        # Non-zero fee bondless (should be excluded from bondless slots)
        nonzero_fee = [
            Offer(
                counterparty=f"NonZeroFee{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=1000,
                maxsize=1000000,
                txfee=0,
                cjfee=100,  # Non-zero fee
                fidelity_bond_value=0,
            )
            for i in range(3)
        ]

        offers = bonded + zero_fee + nonzero_fee

        # With n=3, allowance=0.4: bonded=floor(3*0.6)=1, bondless=2
        # Should pick 1 bonded + 2 from zero_fee (not nonzero_fee)
        for _ in range(10):
            selected = fidelity_bond_weighted_choose(
                offers=offers, n=3, bondless_makers_allowance=0.4, bondless_require_zero_fee=True
            )
            assert len(selected) == 3

            # Check that nonzero_fee makers are not in bondless slots
            # (Note: they could be in bonded slots since they have bond=0,
            # but bonded prioritizes bond>0)
            selected_nicks = {o.counterparty for o in selected}
            nonzero_nicks = {o.counterparty for o in nonzero_fee}

            # Since bonded slots pick from bond>0 only, and bondless require zero fee,
            # nonzero_fee makers should not appear
            assert len(selected_nicks & nonzero_nicks) == 0

    def test_insufficient_bonded_fills_from_bondless(self) -> None:
        """If not enough bonded offers, fill remainder from bondless pool."""
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

        # With n=4, allowance=0.25: bonded=floor(4*0.75)=3, bondless=1
        # But we only have 1 bonded offer, so remaining 3 slots filled from bondless
        selected = fidelity_bond_weighted_choose(
            offers=offers, n=4, bondless_makers_allowance=0.25, bondless_require_zero_fee=False
        )

        assert len(selected) == 4
        # OnlyBonded should always be selected (in bonded phase)
        assert bonded in selected


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


class TestSampleFakeFeeFromOrderbook:
    """Tests for sample_fake_fee_from_orderbook()."""

    @pytest.fixture
    def orderbook_offers(self) -> list[Offer]:
        """Create a realistic orderbook with varied fees."""
        return [
            Offer(
                counterparty="maker_a",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=1000,
            ),
            Offer(
                counterparty="maker_b",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=1200,
            ),
            Offer(
                counterparty="maker_c",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=800,
            ),
            Offer(
                counterparty="maker_d",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=1100,
            ),
            Offer(
                counterparty="maker_e",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=950,
            ),
            # Selected maker (should be excluded from sampling)
            Offer(
                counterparty="selected_maker",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=900,
            ),
        ]

    def test_samples_from_orderbook_fees(self, orderbook_offers: list[Offer]) -> None:
        """Fake fee should be drawn from non-selected orderbook fees."""
        cj_amount = 100_000
        selected = {"selected_maker"}
        # Non-selected fees: 1000, 1200, 800, 1100, 950
        # Mean = 1010, stdev ~ 138.6
        # Range: [871, 1148]
        # Eligible: 1000, 800 is excluded, 1200 is excluded, 1100, 950
        # Actually: lower=max(1, 1010-138)=872, upper=1010+138=1148
        # 800 < 872 -> excluded. 1200 > 1148 -> excluded.
        # Filtered: [1000, 1100, 950]
        results = set()
        for _ in range(200):
            fee = sample_fake_fee_from_orderbook(
                offers=orderbook_offers,
                cj_amount=cj_amount,
                selected_nicks=selected,
                max_cj_fee=MaxCjFee(abs_fee=50_000, rel_fee="0.05"),
            )
            results.add(fee)
            assert fee > 0

        # Should only produce values from the filtered set
        assert results.issubset({950, 1000, 1100})

    def test_excludes_selected_makers(self, orderbook_offers: list[Offer]) -> None:
        """Fees from selected makers must not appear in the sample pool."""
        cj_amount = 100_000
        # Select all but one maker
        selected = {"maker_a", "maker_b", "maker_c", "maker_d", "maker_e"}
        # Only selected_maker (fee=900) remains, but need >= 3 -> fallback
        # fallback_max = int(0.05 * 100_000) = 5000 -> range [1, 5000]
        fee = sample_fake_fee_from_orderbook(
            offers=orderbook_offers,
            cj_amount=cj_amount,
            selected_nicks=selected,
            max_cj_fee=MaxCjFee(abs_fee=50_000, rel_fee="0.05"),
        )
        assert 1 <= fee <= 5000

    def test_fallback_when_insufficient_offers(self) -> None:
        """Falls back to derived range when fewer than 3 non-selected offers."""
        offers = [
            Offer(
                counterparty="maker_a",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=1000,
            ),
        ]
        # fallback_max = int(0.003 * 100_000) = 300 -> range [1, 300]
        fee = sample_fake_fee_from_orderbook(
            offers=offers,
            cj_amount=100_000,
            selected_nicks=set(),
            max_cj_fee=MaxCjFee(abs_fee=50_000, rel_fee="0.003"),
        )
        assert 1 <= fee <= 300

    def test_empty_orderbook_uses_fallback(self) -> None:
        """Empty orderbook should use fallback range."""
        # fallback_max = int(0.002 * 100_000) = 200 -> range [1, 200]
        fee = sample_fake_fee_from_orderbook(
            offers=[],
            cj_amount=100_000,
            selected_nicks=set(),
            max_cj_fee=MaxCjFee(abs_fee=50_000, rel_fee="0.002"),
        )
        assert 1 <= fee <= 200

    def test_offers_outside_amount_range_excluded(self) -> None:
        """Offers that don't cover cj_amount should be excluded."""
        offers = [
            Offer(
                counterparty=f"maker_{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=50_000,  # max < cj_amount
                txfee=500,
                cjfee=1000,
            )
            for i in range(5)
        ]
        # All offers excluded (maxsize < cj_amount) -> fallback
        # fallback_max = int(0.006 * 100_000) = 600 -> range [1, 600]
        fee = sample_fake_fee_from_orderbook(
            offers=offers,
            cj_amount=100_000,
            selected_nicks=set(),
            max_cj_fee=MaxCjFee(abs_fee=50_000, rel_fee="0.006"),
        )
        assert 1 <= fee <= 600

    def test_all_identical_fees(self) -> None:
        """When all fees are identical, return that fee."""
        offers = [
            Offer(
                counterparty=f"maker_{i}",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=1500,
            )
            for i in range(5)
        ]
        fee = sample_fake_fee_from_orderbook(
            offers=offers,
            cj_amount=100_000,
            selected_nicks=set(),
            max_cj_fee=MaxCjFee(abs_fee=50_000, rel_fee="0.05"),
        )
        assert fee == 1500

    def test_relative_fee_offers(self) -> None:
        """Works with relative fee offers."""
        offers = [
            Offer(
                counterparty=f"maker_{i}",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee="0.001",  # 0.1% -> 100 sats on 100k
            )
            for i in range(5)
        ]
        fee = sample_fake_fee_from_orderbook(
            offers=offers,
            cj_amount=100_000,
            selected_nicks=set(),
            max_cj_fee=MaxCjFee(abs_fee=50_000, rel_fee="0.05"),
        )
        assert fee == 100  # 0.1% of 100,000

    def test_mixed_offer_types(self) -> None:
        """Handles a mix of absolute and relative offers."""
        offers = [
            Offer(
                counterparty="abs_1",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=500,
            ),
            Offer(
                counterparty="abs_2",
                oid=0,
                ordertype=OfferType.SW0_ABSOLUTE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee=600,
            ),
            Offer(
                counterparty="rel_1",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee="0.005",  # 0.5% of 100k = 500
            ),
            Offer(
                counterparty="rel_2",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=5_000_000,
                txfee=500,
                cjfee="0.0055",  # 0.55% of 100k = 550
            ),
        ]
        # Fees: 500, 600, 500, 550 -> mean=537.5, stdev~41
        # range: [496, 578] -> filtered: [500, 500, 550]
        results = set()
        for _ in range(200):
            fee = sample_fake_fee_from_orderbook(
                offers=offers,
                cj_amount=100_000,
                selected_nicks=set(),
                max_cj_fee=MaxCjFee(abs_fee=50_000, rel_fee="0.01"),
            )
            results.add(fee)
        assert results.issubset({500, 550})


class TestEqualizeMakerFees:
    """Tests for equalize_maker_fees()."""

    def test_empty_dict(self) -> None:
        """Empty maker_fees returns empty dict."""
        result = equalize_maker_fees({}, 1000)
        assert result == {}

    def test_zero_leftover(self) -> None:
        """Zero leftover returns fees unchanged."""
        fees = {"alice": 100, "bob": 200, "carol": 300}
        result = equalize_maker_fees(fees, 0)
        assert result == fees

    def test_negative_leftover(self) -> None:
        """Negative leftover returns fees unchanged."""
        fees = {"alice": 100, "bob": 200}
        result = equalize_maker_fees(fees, -50)
        assert result == fees

    def test_single_maker(self) -> None:
        """Single maker gets all leftover."""
        result = equalize_maker_fees({"alice": 100}, 500)
        assert result == {"alice": 600}

    def test_all_equal_fees(self) -> None:
        """All fees equal -> leftover distributed evenly (floor)."""
        fees = {"alice": 100, "bob": 100, "carol": 100}
        result = equalize_maker_fees(fees, 30)
        # 30 / 3 = 10 each, no remainder
        assert result == {"alice": 110, "bob": 110, "carol": 110}

    def test_all_equal_fees_with_remainder(self) -> None:
        """Indivisible remainder distributed 1 sat each to first makers."""
        fees = {"alice": 100, "bob": 100, "carol": 100}
        result = equalize_maker_fees(fees, 31)
        # 31 / 3 = 10 each, 1 sat remainder -> first maker gets +1
        assert result == {"alice": 111, "bob": 110, "carol": 110}
        total_added = sum(result[n] - fees[n] for n in fees)
        assert total_added == 31  # ALL leftover distributed

    def test_two_makers_level_up(self) -> None:
        """Two makers: raise lower to upper, distribute rest."""
        fees = {"alice": 100, "bob": 200}
        # Budget = 500. Raise alice (100 -> 200) costs 100.
        # Remaining: 400. Both at 200. 400 / 2 = 200 each.
        # Final: alice=400, bob=400
        result = equalize_maker_fees(fees, 500)
        assert result == {"alice": 400, "bob": 400}

    def test_three_makers_sequential_leveling(self) -> None:
        """Three distinct fees: sequential leveling."""
        fees = {"alice": 100, "bob": 200, "carol": 300}
        # Step 1: Raise alice (100 -> 200) costs 100. Budget = 1000 - 100 = 900.
        # Step 2: alice+bob at 200, raise to 300 costs 2*100 = 200. Budget = 700.
        # Step 3: all at 300. 700 / 3 = 233 each (remainder 1 sat -> first maker).
        # Final: 534, 533, 533
        result = equalize_maker_fees(fees, 1000)
        assert result == {"alice": 534, "bob": 533, "carol": 533}
        total_added = sum(result[n] - fees[n] for n in fees)
        assert total_added == 1000  # ALL leftover distributed

    def test_partial_leveling_budget_exhausted_mid_step(self) -> None:
        """Budget runs out during leveling — partial raise."""
        fees = {"alice": 100, "bob": 300}
        # Raise alice (100 -> 300) costs 200, but budget = 50.
        # Partial: 50 / 1 = 50. Alice gets 150.
        result = equalize_maker_fees(fees, 50)
        assert result == {"alice": 150, "bob": 300}

    def test_partial_leveling_budget_exhausted_evenly(self) -> None:
        """Budget exactly covers first leveling step, nothing left."""
        fees = {"alice": 100, "bob": 200, "carol": 300}
        # Step 1: Raise alice (100 -> 200) costs 100. Budget = 100 - 100 = 0.
        # Done.
        result = equalize_maker_fees(fees, 100)
        assert result == {"alice": 200, "bob": 200, "carol": 300}

    def test_budget_insufficient_for_per_maker_unit(self) -> None:
        """Budget too small for 1 sat per maker -> remainder distribution handles it."""
        fees = {"alice": 100, "bob": 100, "carol": 100}
        # 2 sats / 3 makers = 0 per maker via floor, but remainder = 2 sats
        # -> first 2 makers each get 1 extra sat
        result = equalize_maker_fees(fees, 2)
        assert result == {"alice": 101, "bob": 101, "carol": 100}
        total_added = sum(result[n] - fees[n] for n in fees)
        assert total_added == 2  # ALL leftover distributed

    def test_total_added_never_exceeds_leftover(self) -> None:
        """Sum of fee increases must equal the leftover budget exactly."""
        fees = {"alice": 50, "bob": 150, "carol": 300, "dave": 310}
        leftover = 777
        result = equalize_maker_fees(fees, leftover)
        total_added = sum(result[n] - fees[n] for n in fees)
        assert total_added == leftover

    def test_fees_never_decrease(self) -> None:
        """No maker's fee should ever decrease."""
        fees = {"alice": 50, "bob": 150, "carol": 300}
        result = equalize_maker_fees(fees, 200)
        for nick in fees:
            assert result[nick] >= fees[nick]

    def test_deterministic_output(self) -> None:
        """Same input always produces same output."""
        fees = {"alice": 100, "bob": 200, "carol": 300}
        r1 = equalize_maker_fees(fees, 500)
        r2 = equalize_maker_fees(fees, 500)
        assert r1 == r2

    def test_many_makers_stress(self) -> None:
        """Handles a large number of makers correctly."""
        fees = {f"maker_{i}": i * 10 for i in range(1, 21)}  # 10, 20, ..., 200
        leftover = 50_000
        result = equalize_maker_fees(fees, leftover)
        total_added = sum(result[n] - fees[n] for n in fees)
        assert total_added == leftover  # ALL leftover distributed
        # All fees should be equal (budget is very generous)
        values = list(result.values())
        assert len(set(values)) <= 2  # at most 1 sat difference from remainder

    def test_two_equal_one_higher(self) -> None:
        """Two low fees and one high fee."""
        fees = {"alice": 100, "bob": 100, "carol": 500}
        # Budget = 800. Raise alice+bob (100 -> 500) costs 2*400 = 800. Exact.
        result = equalize_maker_fees(fees, 800)
        assert result == {"alice": 500, "bob": 500, "carol": 500}

    def test_partial_group_raise(self) -> None:
        """Budget only covers partial raise when group is large."""
        fees = {"a": 100, "b": 100, "c": 100, "d": 200}
        # Raise a,b,c (100 -> 200) costs 3*100 = 300. Budget = 200.
        # Partial: 200 / 3 = 66 each. Remaining: 200 - 198 = 2 sats.
        # Remainder: first 2 makers (a, b) each get 1 extra sat.
        result = equalize_maker_fees(fees, 200)
        assert result == {"a": 167, "b": 167, "c": 166, "d": 200}
        total_added = sum(result[n] - fees[n] for n in fees)
        assert total_added == 200  # ALL leftover distributed
