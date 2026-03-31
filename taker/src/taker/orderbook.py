"""
Orderbook management and order selection for taker.

Implements:
- Orderbook fetching from directory nodes
- Order filtering by fee limits and amount ranges
- Maker selection algorithms (fidelity bond weighted, random, cheapest)
- Fee calculation for CoinJoin transactions
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

from jmcore.bitcoin import (
    calculate_relative_fee,
    calculate_sweep_amount,
)
from jmcore.models import Offer, OfferType
from jmcore.models import calculate_cj_fee as _calculate_cj_fee_raw
from jmcore.paths import get_ignored_makers_path
from jmcore.protocol import get_nick_version
from loguru import logger

from taker.config import MaxCjFee


def calculate_cj_fee(offer: Offer, cj_amount: int) -> int:
    """
    Calculate the CoinJoin fee for a specific offer and amount.

    Convenience wrapper around jmcore.models.calculate_cj_fee that accepts
    an Offer object directly.

    Args:
        offer: The maker's offer
        cj_amount: The CoinJoin amount in satoshis

    Returns:
        Fee in satoshis
    """
    return _calculate_cj_fee_raw(offer.ordertype, offer.cjfee, cj_amount)


def is_fee_within_limits(offer: Offer, cj_amount: int, max_cj_fee: MaxCjFee) -> bool:
    """
    Check if an offer's fee is within the configured limits.

    For absolute offers: check cjfee <= abs_fee
    For relative offers: check cjfee <= rel_fee

    It's a logical OR - an offer passes if it meets either limit for its type.

    Args:
        offer: The maker's offer
        cj_amount: The CoinJoin amount (not used in the new logic)
        max_cj_fee: Fee limits configuration

    Returns:
        True if fee is acceptable
    """
    if offer.ordertype in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE):
        # For absolute offers, check against absolute limit directly
        return int(offer.cjfee) <= max_cj_fee.abs_fee
    else:
        # For relative offers, check against relative limit directly
        # Compare by calculating fee on a large reference amount
        ref_amount = 100_000_000_000  # 1000 BTC
        fee_val = calculate_relative_fee(ref_amount, str(offer.cjfee))
        limit_val = calculate_relative_fee(ref_amount, max_cj_fee.rel_fee)
        return fee_val <= limit_val


def filter_offers(
    offers: list[Offer],
    cj_amount: int,
    max_cj_fee: MaxCjFee,
    ignored_makers: set[str] | None = None,
    allowed_types: set[OfferType] | None = None,
    min_nick_version: int | None = None,
) -> list[Offer]:
    """
    Filter offers based on amount range, fee limits, and other criteria.

    Args:
        offers: List of all offers
        cj_amount: Target CoinJoin amount
        max_cj_fee: Fee limits
        ignored_makers: Set of maker nicks to exclude
        allowed_types: Set of allowed offer types (default: all sw0* types)
        min_nick_version: Minimum nick version for reference compatibility (not used for
            neutrino detection - that uses handshake features instead)

    Returns:
        List of eligible offers
    """
    if ignored_makers is None:
        ignored_makers = set()

    if allowed_types is None:
        # Default to all standard SW types for backward compatibility
        allowed_types = {OfferType.SW0_RELATIVE, OfferType.SW0_ABSOLUTE}

    if ignored_makers:
        logger.debug(
            f"Filtering offers: {len(ignored_makers)} makers in ignored list: {ignored_makers}"
        )

    eligible = []

    for offer in offers:
        # Filter by maker
        if offer.counterparty in ignored_makers:
            logger.info(f"Ignoring offer from {offer.counterparty} (in ignored list)")
            continue

        # Filter by nick version (reserved for potential future reference compatibility)
        # NOTE: This is NOT used for neutrino detection - that uses handshake features
        if min_nick_version is not None:
            nick_version = get_nick_version(offer.counterparty)
            if nick_version < min_nick_version:
                logger.debug(
                    f"Ignoring offer from {offer.counterparty}: "
                    f"nick version {nick_version} < required {min_nick_version}"
                )
                continue

        # Filter by offer type
        if offer.ordertype not in allowed_types:
            logger.debug(
                f"Ignoring offer from {offer.counterparty}: "
                f"type {offer.ordertype} not in allowed types"
            )
            continue

        # Filter by amount range
        if cj_amount < offer.minsize:
            logger.trace(
                f"Ignoring offer from {offer.counterparty}: "
                f"amount {cj_amount} < minsize {offer.minsize}"
            )
            continue

        if cj_amount > offer.maxsize:
            logger.debug(
                f"Ignoring offer from {offer.counterparty}: "
                f"amount {cj_amount} > maxsize {offer.maxsize}"
            )
            continue

        # Filter by fee limits
        if not is_fee_within_limits(offer, cj_amount, max_cj_fee):
            fee = calculate_cj_fee(offer, cj_amount)
            logger.trace(f"Ignoring offer from {offer.counterparty}: fee {fee} exceeds limits")
            continue

        eligible.append(offer)

    logger.info(f"Filtered {len(offers)} offers to {len(eligible)} eligible offers")
    return eligible


def dedupe_offers_by_maker(offers: list[Offer]) -> list[Offer]:
    """
    Keep only the cheapest offer from each maker.

    Args:
        offers: List of offers (possibly multiple per maker)

    Returns:
        List with at most one offer per maker (the cheapest)
    """
    by_maker: dict[str, list[Offer]] = {}

    for offer in offers:
        if offer.counterparty not in by_maker:
            by_maker[offer.counterparty] = []
        by_maker[offer.counterparty].append(offer)

    result = []
    for maker, maker_offers in by_maker.items():
        # Sort by absolute fee equivalent at some reference amount (1 BTC)
        reference_amount = 100_000_000  # 1 BTC
        sorted_offers = sorted(maker_offers, key=lambda o: calculate_cj_fee(o, reference_amount))
        result.append(sorted_offers[0])
        if len(maker_offers) > 1:
            logger.debug(f"Kept cheapest of {len(maker_offers)} offers from {maker}")

    return result


def dedupe_offers_by_bond(offers: list[Offer], cj_amount: int) -> list[Offer]:
    """
    Deduplicate offers by fidelity bond UTXO, keeping only the cheapest per bond.

    This is a sybil protection measure: if two different counterparties (nicks)
    share the same fidelity bond UTXO, we should only select one of them.
    Otherwise, an attacker could create multiple nicks backed by the same bond
    and get selected multiple times in the same CoinJoin.

    Offers without a fidelity bond are passed through unchanged.

    Args:
        offers: List of offers (possibly from different makers using same bond)
        cj_amount: The actual CoinJoin amount for accurate fee comparison

    Returns:
        List with at most one offer per bond UTXO (the cheapest), plus all unbonded offers
    """
    # Group bonded offers by bond UTXO
    by_bond: dict[str, list[Offer]] = {}
    unbonded: list[Offer] = []

    for offer in offers:
        bond_key = None
        if offer.fidelity_bond_data:
            # Use txid:vout as unique key
            bond_key = (
                f"{offer.fidelity_bond_data['utxo_txid']}:{offer.fidelity_bond_data['utxo_vout']}"
            )

        if bond_key:
            if bond_key not in by_bond:
                by_bond[bond_key] = []
            by_bond[bond_key].append(offer)
        else:
            unbonded.append(offer)

    # For each bond UTXO, keep only the cheapest offer
    result = []
    for bond_key, bond_offers in by_bond.items():
        sorted_offers = sorted(bond_offers, key=lambda o: calculate_cj_fee(o, cj_amount))
        result.append(sorted_offers[0])
        if len(bond_offers) > 1:
            kept = sorted_offers[0]
            dropped = [o.counterparty for o in sorted_offers[1:]]
            kept_fee = calculate_cj_fee(kept, cj_amount)
            logger.warning(
                f"Bond sybil protection: Kept {kept.counterparty} (fee={kept_fee}), "
                f"dropped {dropped} sharing same bond UTXO {bond_key[:16]}..."
            )

    # Add unbonded offers unchanged
    result.extend(unbonded)

    return result


# Order chooser functions (selection algorithms)


def random_order_choose(offers: list[Offer], n: int) -> list[Offer]:
    """
    Choose n offers randomly.

    Args:
        offers: Eligible offers
        n: Number of offers to choose

    Returns:
        Selected offers
    """
    if len(offers) <= n:
        return offers[:]

    return random.sample(offers, n)


def cheapest_order_choose(offers: list[Offer], n: int, cj_amount: int = 0) -> list[Offer]:
    """
    Choose n cheapest offers.

    Args:
        offers: Eligible offers
        n: Number of offers to choose
        cj_amount: CoinJoin amount for fee calculation (default uses 1 BTC)

    Returns:
        Selected offers (sorted by fee, cheapest first)
    """
    if cj_amount == 0:
        cj_amount = 100_000_000  # 1 BTC

    sorted_offers = sorted(offers, key=lambda o: calculate_cj_fee(o, cj_amount))
    return sorted_offers[:n]


def weighted_order_choose(
    offers: list[Offer], n: int, cj_amount: int = 0, exponent: float = 3.0
) -> list[Offer]:
    """
    Choose n offers with exponential weighting by inverse fee.

    Cheaper offers are more likely to be selected.

    Args:
        offers: Eligible offers
        n: Number of offers to choose
        cj_amount: CoinJoin amount for fee calculation
        exponent: Higher values favor cheaper offers more strongly

    Returns:
        Selected offers
    """
    if len(offers) <= n:
        return offers[:]

    if cj_amount == 0:
        cj_amount = 100_000_000  # 1 BTC

    # Calculate weights (inverse fee, exponentially weighted)
    fees = [calculate_cj_fee(o, cj_amount) for o in offers]
    max_fee = max(fees) if fees else 1
    weights = [(max_fee - fee + 1) ** exponent for fee in fees]

    total_weight = sum(weights)
    if total_weight == 0:
        return random.sample(offers, n)

    selected = []
    remaining_offers = list(enumerate(offers))
    remaining_weights = list(weights)

    for _ in range(n):
        if not remaining_offers:
            break

        # Weighted random selection
        total = sum(remaining_weights)
        r = random.uniform(0, total)
        cumulative = 0

        for i, (idx, offer) in enumerate(remaining_offers):
            cumulative += remaining_weights[i]
            if r <= cumulative:
                selected.append(offer)
                remaining_offers.pop(i)
                remaining_weights.pop(i)
                break

    return selected


def fidelity_bond_weighted_choose(
    offers: list[Offer],
    n: int,
    bondless_makers_allowance: float = 0.125,
    bondless_require_zero_fee: bool = True,
    cj_amount: int = 0,
) -> list[Offer]:
    """
    Choose n offers with mixed fidelity bond and random selection.

    Strategy:
    1. Calculate proportion of bonded slots: round(n * (1 - bondless_makers_allowance))
    2. Fill bonded slots using weighted selection by bond value
    3. Fill remaining slots randomly from ALL remaining offers (bonded or bondless)

    "Bondless" means bond-agnostic (equal probability), not anti-bond. The bondless
    slots give all remaining makers equal opportunity regardless of their bond status.

    This ensures high-bond makers are prioritized while still allowing new/bondless
    makers to participate in a predictable proportion.

    Args:
        offers: Eligible offers
        n: Number of offers to choose
        bondless_makers_allowance: Proportion of slots for random selection (0.0-1.0)
        bondless_require_zero_fee: If True, bondless spots only select zero-fee offers
        cj_amount: CoinJoin amount for fee filtering (unused currently)

    Returns:
        Selected offers
    """
    if len(offers) <= n:
        return offers[:]

    # Log bonded offers for debugging
    bonded_offers = [o for o in offers if o.fidelity_bond_value > 0]
    logger.debug(
        f"Found {len(bonded_offers)} offers with fidelity bond: "
        f"{[o.counterparty for o in bonded_offers]}"
    )

    # Calculate split: prioritize bonded makers, fill remainder with random
    # Use round() for fair rounding instead of floor
    num_bonded = round(n * (1 - bondless_makers_allowance))
    num_bondless = n - num_bonded

    logger.debug(
        f"Selection split: {num_bonded} bonded, {num_bondless} bondless "
        f"(allowance={bondless_makers_allowance})"
    )

    selected: list[Offer] = []
    remaining_offers = offers[:]  # Copy to modify

    # 1. Select Bonded Makers (weighted by bond value)
    if num_bonded > 0:
        # Build pool of (offer, bond_value) pairs
        pool = [(o, o.fidelity_bond_value) for o in remaining_offers]
        # Remove zero-bond offers from bonded pool
        bonded_pool = [(o, w) for o, w in pool if w > 0]

        total_bond = sum(w for _, w in bonded_pool)

        if total_bond == 0 or len(bonded_pool) == 0:
            logger.debug(
                f"No fidelity bonds found for {num_bonded} bonded slots, "
                "will fill from bondless pool"
            )
            # Don't increment num_bondless here, we'll handle shortage at the end
        else:
            # Weighted selection without replacement
            for _ in range(min(num_bonded, len(bonded_pool))):
                if not bonded_pool:
                    break

                current_total = sum(w for _, w in bonded_pool)
                r = random.uniform(0, current_total)
                cumulative = 0

                for i, (offer, weight) in enumerate(bonded_pool):
                    cumulative += weight
                    if r <= cumulative:
                        selected.append(offer)
                        remaining_offers.remove(offer)
                        bonded_pool.pop(i)
                        break

    # 2. Fill remaining slots (bondless selection - uniform random from all remaining)
    slots_remaining = n - len(selected)
    if slots_remaining > 0:
        if not remaining_offers:
            logger.warning(
                f"Not enough offers to fill {slots_remaining} remaining slots "
                f"(selected {len(selected)}/{n})"
            )
            return selected

        # For bondless slots: select uniformly from all remaining offers
        # (both bonded and bondless makers have equal probability)
        candidates = remaining_offers

        # Optionally filter to zero-fee offers only
        if bondless_require_zero_fee:
            zero_fee_candidates = [
                o
                for o in candidates
                if (
                    o.ordertype in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE)
                    and int(o.cjfee) == 0
                )
                or (
                    o.ordertype not in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE)
                    # For relative offers, we can't strictly say fee is 0 without amount,
                    # but usually 'zero fee' implies 0 absolute.
                    # The original logic included relative fee offers in the eligible list.
                    # We'll stick to that.
                )
            ]

            if len(zero_fee_candidates) >= slots_remaining:
                candidates = zero_fee_candidates
                logger.debug(
                    f"Bondless slots: filtered to {len(candidates)} zero-fee offers "
                    f"(bonded + bondless)"
                )
            else:
                logger.warning(
                    f"Not enough zero-fee offers for bondless selection "
                    f"({len(zero_fee_candidates)} < {slots_remaining}), "
                    "using all remaining offers"
                )

        # Uniform random selection for remaining slots
        picked = random_order_choose(candidates, slots_remaining)
        selected.extend(picked)

    logger.debug(f"Final selection: {len(selected)} makers chosen")
    return selected


def choose_orders(
    offers: list[Offer],
    cj_amount: int,
    n: int,
    max_cj_fee: MaxCjFee,
    choose_fn: Callable[[list[Offer], int], list[Offer]] | None = None,
    ignored_makers: set[str] | None = None,
    min_nick_version: int | None = None,
    bondless_makers_allowance: float = 0.125,
    bondless_require_zero_fee: bool = True,
    allowed_types: set[OfferType] | None = None,
) -> tuple[dict[str, Offer], int]:
    """
    Choose n orders from the orderbook for a CoinJoin.

    Args:
        offers: All offers from orderbook
        cj_amount: Target CoinJoin amount
        n: Number of makers to select
        max_cj_fee: Fee limits
        choose_fn: Selection algorithm (default: fidelity_bond_weighted_choose)
        ignored_makers: Makers to exclude
        min_nick_version: Minimum required nick version (e.g., 6 for neutrino takers)
        bondless_makers_allowance: Probability of random selection vs fidelity bond weighting
        bondless_require_zero_fee: If True, bondless spots only select zero absolute fee offers
        allowed_types: Set of allowed offer types (passed to filter_offers)

    Returns:
        (dict of counterparty -> offer, total_cj_fee)
    """
    if choose_fn is None:
        # Use partial to bind bondless_makers_allowance and bondless_require_zero_fee
        from functools import partial

        choose_fn = partial(
            fidelity_bond_weighted_choose,
            bondless_makers_allowance=bondless_makers_allowance,
            bondless_require_zero_fee=bondless_require_zero_fee,
            cj_amount=cj_amount,
        )

    # Filter offers
    eligible = filter_offers(
        offers=offers,
        cj_amount=cj_amount,
        max_cj_fee=max_cj_fee,
        ignored_makers=ignored_makers,
        min_nick_version=min_nick_version,
        allowed_types=allowed_types,
    )

    # Dedupe by maker (keep cheapest offer per counterparty)
    deduped_by_maker = dedupe_offers_by_maker(eligible)

    # Dedupe by bond UTXO (sybil protection: keep cheapest offer per bond)
    # This must come after maker dedup so we compare the best offer from each nick
    deduped = dedupe_offers_by_bond(deduped_by_maker, cj_amount)

    if len(deduped) < n:
        logger.warning(
            f"Not enough makers: need {n}, found {len(deduped)} (from {len(offers)} total offers)"
        )
        n = len(deduped)

    # Select makers
    selected = choose_fn(deduped, n)

    # Build result
    result = {offer.counterparty: offer for offer in selected}

    # Calculate total fee
    total_fee = sum(calculate_cj_fee(offer, cj_amount) for offer in selected)

    logger.info(
        f"Selected {len(result)} makers from {len(offers)} offers, total fee: {total_fee} sats"
    )

    return result, total_fee


def choose_sweep_orders(
    offers: list[Offer],
    total_input_value: int,
    my_txfee: int,
    n: int,
    max_cj_fee: MaxCjFee,
    choose_fn: Callable[[list[Offer], int], list[Offer]] | None = None,
    ignored_makers: set[str] | None = None,
    min_nick_version: int | None = None,
    bondless_makers_allowance: float = 0.125,
    bondless_require_zero_fee: bool = True,
    allowed_types: set[OfferType] | None = None,
) -> tuple[dict[str, Offer], int, int]:
    """
    Choose n orders for a sweep transaction (no change).

    For sweeps, we need to solve for cj_amount such that:
    my_change = total_input - cj_amount - sum(cjfees) - my_txfee = 0

    Args:
        offers: All offers from orderbook
        total_input_value: Total value of taker's inputs
        my_txfee: Taker's portion of transaction fee
        n: Number of makers to select
        max_cj_fee: Fee limits
        choose_fn: Selection algorithm
        ignored_makers: Makers to exclude
        min_nick_version: Minimum required nick version (e.g., 6 for neutrino takers)
        bondless_makers_allowance: Probability of random selection vs fidelity bond weighting
        bondless_require_zero_fee: If True, bondless spots only select zero absolute fee offers
        allowed_types: Set of allowed offer types (passed to filter_offers)

    Returns:
        (dict of counterparty -> offer, cj_amount, total_cj_fee)
    """
    if choose_fn is None:
        from functools import partial

        choose_fn = partial(
            fidelity_bond_weighted_choose,
            bondless_makers_allowance=bondless_makers_allowance,
            bondless_require_zero_fee=bondless_require_zero_fee,
        )

    if ignored_makers is None:
        ignored_makers = set()

    # For sweep, we need to find offers that work for the available amount
    # First estimate: cj_amount = total_input - my_txfee - estimated_fees
    # Assume ~0.1% per maker for estimation
    estimated_rel_fees = ["0.001"] * n
    estimated_cj_amount = calculate_sweep_amount(total_input_value - my_txfee, estimated_rel_fees)

    # Filter with estimated amount
    eligible = filter_offers(
        offers=offers,
        cj_amount=estimated_cj_amount,
        max_cj_fee=max_cj_fee,
        ignored_makers=ignored_makers,
        min_nick_version=min_nick_version,
        allowed_types=allowed_types,
    )

    # Dedupe by maker
    deduped_by_maker = dedupe_offers_by_maker(eligible)

    # Dedupe by bond UTXO (sybil protection)
    # Use estimated_cj_amount for fee comparison since we don't know exact amount yet
    deduped = dedupe_offers_by_bond(deduped_by_maker, estimated_cj_amount)

    logger.debug(
        f"After deduplication: {len(deduped)} unique makers from {len(eligible)} eligible offers"
    )
    if len(deduped) < len(eligible):
        # Show which makers had multiple offers
        from collections import Counter

        maker_counts = Counter(o.counterparty for o in eligible)
        multi_offer_makers = {m: c for m, c in maker_counts.items() if c > 1}
        if multi_offer_makers:
            logger.debug(f"Makers with multiple offers: {multi_offer_makers}")

    if len(deduped) < n:
        logger.warning(
            f"Not enough makers for sweep: need {n}, found {len(deduped)} "
            f"(filtered from {len(offers)} total offers)"
        )
        # Can't proceed if we don't have at least 1 maker (minimum for a CoinJoin)
        if len(deduped) < 1:
            logger.error(
                "No makers available. "
                "Try relaxing fee limits or checking if makers are in ignored list."
            )
            return {}, 0, 0
        n = len(deduped)

    if n == 0:
        return {}, 0, 0

    # Select makers
    selected = choose_fn(deduped, n)

    # Now solve for exact cj_amount
    sum_abs_fees = 0
    rel_fees = []

    for offer in selected:
        if offer.ordertype in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE):
            sum_abs_fees += int(offer.cjfee)
        else:
            rel_fees.append(str(offer.cjfee))

    available = total_input_value - my_txfee - sum_abs_fees
    cj_amount = calculate_sweep_amount(available, rel_fees)

    # Verify this works for all selected offers
    for offer in selected:
        if cj_amount < offer.minsize or cj_amount > offer.maxsize:
            logger.error(
                f"Sweep amount {cj_amount} outside range for {offer.counterparty}: "
                f"{offer.minsize}-{offer.maxsize}"
            )
            # Could retry with fewer makers here

    result = {offer.counterparty: offer for offer in selected}
    total_fee = sum(calculate_cj_fee(offer, cj_amount) for offer in selected)

    logger.info(f"Sweep: selected {len(result)} makers, cj_amount={cj_amount}, fee={total_fee}")

    return result, cj_amount, total_fee


class OrderbookManager:
    """Manages orderbook state and maker selection."""

    def __init__(
        self,
        max_cj_fee: MaxCjFee,
        bondless_makers_allowance: float = 0.125,
        bondless_require_zero_fee: bool = True,
        data_dir: Any = None,  # Path | None, but avoid import
        own_wallet_nicks: set[str] | None = None,
    ):
        self.max_cj_fee = max_cj_fee
        self.bondless_makers_allowance = bondless_makers_allowance
        self.bondless_require_zero_fee = bondless_require_zero_fee
        self.offers: list[Offer] = []
        self.bonds: dict[str, Any] = {}  # maker -> bond info
        self.ignored_makers: set[str] = set()
        self.honest_makers: set[str] = set()

        # Own wallet nicks to exclude from peer selection (e.g., same wallet's maker nick)
        # This is populated from state files and protects against self-CoinJoins
        self.own_wallet_nicks: set[str] = own_wallet_nicks or set()
        if self.own_wallet_nicks:
            logger.info(f"Excluding own wallet nicks from peer selection: {self.own_wallet_nicks}")

        # Persistence for ignored makers
        self.ignored_makers_path = get_ignored_makers_path(data_dir)
        self._load_ignored_makers()

    def _load_ignored_makers(self) -> None:
        """Load ignored makers from disk."""
        if not self.ignored_makers_path.exists():
            logger.debug(f"No existing ignored makers file at {self.ignored_makers_path}")
            return

        try:
            with open(self.ignored_makers_path, encoding="utf-8") as f:
                for line in f:
                    maker = line.strip()
                    if maker:
                        self.ignored_makers.add(maker)
            if self.ignored_makers:
                logger.info(
                    f"Loaded {len(self.ignored_makers)} ignored makers from "
                    f"{self.ignored_makers_path}"
                )
        except Exception as e:
            logger.error(f"Failed to load ignored makers from {self.ignored_makers_path}: {e}")

    def _save_ignored_makers(self) -> None:
        """Save ignored makers to disk."""
        try:
            # Ensure parent directory exists
            self.ignored_makers_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.ignored_makers_path, "w", encoding="utf-8") as f:
                for maker in sorted(self.ignored_makers):
                    f.write(maker + "\n")
                f.flush()
            logger.debug(
                f"Saved {len(self.ignored_makers)} ignored makers to {self.ignored_makers_path}"
            )
        except Exception as e:
            logger.error(f"Failed to save ignored makers to {self.ignored_makers_path}: {e}")

    def update_offers(self, offers: list[Offer]) -> None:
        """Update orderbook with new offers."""
        self.offers = offers
        logger.info(f"Updated orderbook with {len(offers)} offers")

    def add_ignored_maker(self, maker: str) -> None:
        """Add a maker to the ignored list and persist to disk."""
        self.ignored_makers.add(maker)
        logger.info(f"Added {maker} to ignored makers list")
        self._save_ignored_makers()

    def clear_ignored_makers(self) -> None:
        """Clear all ignored makers and delete the persistence file."""
        count = len(self.ignored_makers)
        self.ignored_makers.clear()
        logger.info(f"Cleared {count} ignored makers")

        # Delete the file if it exists
        try:
            if self.ignored_makers_path.exists():
                self.ignored_makers_path.unlink()
                logger.debug(f"Deleted {self.ignored_makers_path}")
        except Exception as e:
            logger.error(f"Failed to delete {self.ignored_makers_path}: {e}")

    def add_honest_maker(self, maker: str) -> None:
        """Mark a maker as honest (completed a CoinJoin successfully)."""
        self.honest_makers.add(maker)
        logger.debug(f"Added {maker} to honest makers list")

    def select_makers(
        self,
        cj_amount: int,
        n: int,
        honest_only: bool = False,
        min_nick_version: int | None = None,
        exclude_nicks: set[str] | None = None,
        allowed_types: set[OfferType] | None = None,
    ) -> tuple[dict[str, Offer], int]:
        """
        Select makers for a CoinJoin.

        Args:
            cj_amount: Target amount
            n: Number of makers
            honest_only: Only select from honest makers
            min_nick_version: Minimum required nick version (e.g., 6 for neutrino takers)
            exclude_nicks: Additional nicks to exclude (e.g., current session makers)
            allowed_types: Set of allowed offer types (passed to choose_orders)

        Returns:
            (selected offers dict, total fee)
        """
        available_offers = self.offers

        if honest_only:
            available_offers = [o for o in self.offers if o.counterparty in self.honest_makers]

        # Combine ignored_makers, own_wallet_nicks, and any additional excluded nicks
        combined_ignored = self.ignored_makers.copy()
        combined_ignored.update(self.own_wallet_nicks)
        if exclude_nicks:
            combined_ignored.update(exclude_nicks)

        return choose_orders(
            offers=available_offers,
            cj_amount=cj_amount,
            n=n,
            max_cj_fee=self.max_cj_fee,
            ignored_makers=combined_ignored,
            min_nick_version=min_nick_version,
            bondless_makers_allowance=self.bondless_makers_allowance,
            bondless_require_zero_fee=self.bondless_require_zero_fee,
            allowed_types=allowed_types,
        )

    def select_makers_for_sweep(
        self,
        total_input_value: int,
        my_txfee: int,
        n: int,
        honest_only: bool = False,
        min_nick_version: int | None = None,
        exclude_nicks: set[str] | None = None,
        allowed_types: set[OfferType] | None = None,
    ) -> tuple[dict[str, Offer], int, int]:
        """
        Select makers for a sweep CoinJoin.

        Args:
            total_input_value: Total input value
            my_txfee: Taker's tx fee portion
            n: Number of makers
            honest_only: Only select from honest makers
            min_nick_version: Minimum required nick version (e.g., 6 for neutrino takers)
            exclude_nicks: Additional nicks to exclude (e.g., current session makers)
            allowed_types: Set of allowed offer types (passed to choose_sweep_orders)

        Returns:
            (selected offers dict, cj_amount, total fee)
        """
        available_offers = self.offers

        if honest_only:
            available_offers = [o for o in self.offers if o.counterparty in self.honest_makers]

        # Combine ignored_makers, own_wallet_nicks, and any additional excluded nicks
        combined_ignored = self.ignored_makers.copy()
        combined_ignored.update(self.own_wallet_nicks)
        if exclude_nicks:
            combined_ignored.update(exclude_nicks)

        return choose_sweep_orders(
            offers=available_offers,
            total_input_value=total_input_value,
            my_txfee=my_txfee,
            n=n,
            max_cj_fee=self.max_cj_fee,
            ignored_makers=combined_ignored,
            min_nick_version=min_nick_version,
            bondless_makers_allowance=self.bondless_makers_allowance,
            bondless_require_zero_fee=self.bondless_require_zero_fee,
            allowed_types=allowed_types,
        )
