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
from taker.fee_quantization import FeeQuantizer


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


def offer_is_ng(offer: Offer) -> bool:
    """Whether an offer comes from a JoinMarket-NG maker (vs a legacy one).

    NG makers advertise a non-empty feature set during the handshake (e.g.
    ``neutrino_compat``); legacy JoinMarket clientserver makers advertise none.
    The distinction matters for fee homogenization: NG makers accept being paid
    more than their advertised fee (they verify change with ``>=``), while legacy
    makers reject any overpayment (exact ``!=`` match) and would refuse to sign.

    The predicate is deliberately conservative: a false negative only means we
    pay an NG maker its exact advertised fee, which it always accepts.
    """
    return bool(offer.features) or offer.neutrino_compat


def maker_paid_fee(offer: Offer, cj_amount: int, fee_quantizer: FeeQuantizer | None) -> int:
    """Fee the taker actually pays a maker for ``cj_amount``.

    With an active quantizer, NG makers are paid the homogenized per-slot fee so
    their per-maker fee fingerprint is removed. Legacy makers are always paid
    their exact advertised fee: they reject any overpayment, so homogenizing them
    would make them refuse to sign and abort the whole CoinJoin.
    """
    exact = calculate_cj_fee(offer, cj_amount)
    if fee_quantizer is None or not offer_is_ng(offer):
        return exact
    return fee_quantizer.paid_fee(exact, cj_amount)


def _selected_total_fee(
    selected: list[Offer], cj_amount: int, fee_quantizer: FeeQuantizer | None
) -> int:
    """Total maker fee for a set of selected offers (quantization-aware)."""
    return sum(maker_paid_fee(o, cj_amount, fee_quantizer) for o in selected)


def is_fee_within_limits(
    offer: Offer,
    cj_amount: int,
    max_cj_fee: MaxCjFee,
    fee_quantizer: FeeQuantizer | None = None,
) -> bool:
    """
    Check if an offer's fee is within the configured limits.

    When ``fee_quantizer`` is active (issue #508), eligibility is stricter: the
    maker's advertised fee at ``cj_amount`` must be at or below the homogenized
    per-slot quantum, so that every selected maker can be paid the same fee.

    Otherwise the legacy per-type limit check applies:
    For absolute offers: check cjfee <= abs_fee
    For relative offers: check cjfee <= rel_fee

    It's a logical OR - an offer passes if it meets either limit for its type.

    Args:
        offer: The maker's offer
        cj_amount: The CoinJoin amount (used for quantized eligibility)
        max_cj_fee: Fee limits configuration
        fee_quantizer: Optional quantization policy

    Returns:
        True if fee is acceptable
    """
    if fee_quantizer is not None and fee_quantizer.active:
        slot_fee = fee_quantizer.slot_fee(cj_amount)
        if slot_fee is not None:
            return calculate_cj_fee(offer, cj_amount) <= slot_fee

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
    required_features: set[str] | None = None,
    fee_quantizer: FeeQuantizer | None = None,
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
        required_features: Feature names that makers must support. Offers from makers
            that are known NOT to support a required feature are filtered out. Offers
            with unknown feature status (empty features dict) pass through, since
            compatibility will be verified later during the handshake.

    Returns:
        List of eligible offers
    """
    if ignored_makers is None:
        ignored_makers = set()

    if allowed_types is None:
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

        # Filter by required features (e.g., neutrino_compat).
        # Only reject offers where we KNOW the maker lacks a required feature
        # (features dict is populated but the feature is missing/false).
        # Offers with empty features (unknown status) pass through -- they will
        # be verified during the handshake in _phase_auth().
        if required_features and offer.features:
            missing = {f for f in required_features if not offer.features.get(f)}
            if missing:
                logger.debug(
                    f"Ignoring offer from {offer.counterparty}: missing required features {missing}"
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
        if not is_fee_within_limits(offer, cj_amount, max_cj_fee, fee_quantizer):
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
    bondless_makers_allowance: float = 0.2,
    bondless_require_zero_fee: bool = True,
    cj_amount: int = 0,
) -> list[Offer]:
    """
    Choose n offers using per-slot probabilistic selection.

    **Pre-filtering** (when ``bondless_require_zero_fee`` is True):
    Bondless offers (``fidelity_bond_value == 0``) that charge a non-zero
    absolute fee are removed before selection.  This prevents an attacker
    from flooding the orderbook with fee-charging bondless offers to steal
    fees while still allowing genuine zero-fee bondless makers to participate.
    Relative-fee offers are kept because their effective fee depends on the
    CoinJoin amount and is evaluated elsewhere.

    **Per-slot selection** (for each of the *n* slots independently):

    * With probability ``bondless_makers_allowance``: pick **uniformly at
      random** from all remaining offers (bonded and bondless alike).  This
      gives every surviving offer equal probability, so a rare bondless maker
      naturally has low selection odds (``~ allowance / total_offers`` per
      slot).
    * Otherwise: pick from the bonded pool (``fidelity_bond_value > 0``)
      **weighted by bond value**.

    Fallback: if the chosen pool is empty the other pool is tried, then
    uniform random over everything remaining.

    This mirrors the reference JoinMarket implementation and ensures:

    * High-bond makers are strongly favoured (~80% of slots with default
      0.2 allowance).
    * When many bondless zero-fee makers exist, roughly
      ``n * bondless_makers_allowance`` of them appear in the final set
      (e.g. 2 out of 10).
    * When only a few bondless makers exist, each has low individual
      selection probability (proportional to ``1 / total_remaining``),
      avoiding taker fingerprinting.
    * Smaller bonded makers also benefit from the uniform-random slots.

    Args:
        offers: Eligible offers (already filtered and deduped).
        n: Number of offers to choose.
        bondless_makers_allowance: Per-slot probability of uniform-random
            selection (0.0-1.0).
        bondless_require_zero_fee: If True, pre-filter removes bondless
            offers with non-zero absolute fee.
        cj_amount: CoinJoin amount (reserved for future fee filtering).

    Returns:
        Selected offers.
    """
    if len(offers) <= n:
        return offers[:]

    # --- Pre-filter: remove bondless offers charging a fee ---
    if bondless_require_zero_fee:
        filtered: list[Offer] = []
        removed = 0
        for o in offers:
            if o.fidelity_bond_value == 0 and _is_nonzero_absolute_fee(o):
                removed += 1
            else:
                filtered.append(o)
        if removed:
            logger.debug(f"Pre-filter: removed {removed} bondless offers with non-zero fee")
        if len(filtered) <= n:
            return filtered[:]
        remaining = filtered
    else:
        remaining = offers[:]

    selected: list[Offer] = []

    bonded_count = sum(1 for o in remaining if o.fidelity_bond_value > 0)
    logger.debug(
        f"Selection pool: {len(remaining)} offers ({bonded_count} bonded, "
        f"{len(remaining) - bonded_count} bondless), picking {n} with "
        f"bondless_allowance={bondless_makers_allowance}"
    )

    for _i in range(n):
        if not remaining:
            logger.warning(f"Exhausted offer pool after {len(selected)}/{n} picks")
            break

        picked: Offer | None = None

        if random.random() < bondless_makers_allowance:
            # Bondless slot: pick uniformly from ALL remaining offers.
            # Bonded and bondless compete on equal footing here, so a rare
            # bondless maker has probability ~1/len(remaining).
            picked = random.choice(remaining)
        else:
            # Bonded slot: pick weighted by bond value
            picked = _pick_weighted_bonded(remaining)

        if picked is None:
            # Bonded pool empty -- fall back to uniform random
            picked = random.choice(remaining)

        selected.append(picked)
        remaining.remove(picked)

    logger.debug(
        f"Final selection: {len(selected)} makers "
        f"({sum(1 for o in selected if o.fidelity_bond_value > 0)} bonded, "
        f"{sum(1 for o in selected if o.fidelity_bond_value == 0)} bondless)"
    )
    return selected


def _is_nonzero_absolute_fee(offer: Offer) -> bool:
    """Check if an offer charges a non-zero absolute fee."""
    return (
        offer.ordertype in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE)
        and int(offer.cjfee) != 0
    )


def _pick_weighted_bonded(pool: list[Offer]) -> Offer | None:
    """Pick one offer from *pool* weighted by fidelity_bond_value."""
    bonded = [(o, o.fidelity_bond_value) for o in pool if o.fidelity_bond_value > 0]
    if not bonded:
        return None
    total = sum(w for _, w in bonded)
    r = random.uniform(0, total)
    cumulative = 0
    for offer, weight in bonded:
        cumulative += weight
        if r <= cumulative:
            return offer
    return bonded[-1][0]  # float rounding guard


def choose_orders(
    offers: list[Offer],
    cj_amount: int,
    n: int,
    max_cj_fee: MaxCjFee,
    choose_fn: Callable[[list[Offer], int], list[Offer]] | None = None,
    ignored_makers: set[str] | None = None,
    min_nick_version: int | None = None,
    bondless_makers_allowance: float = 0.2,
    bondless_require_zero_fee: bool = True,
    required_features: set[str] | None = None,
    fee_quantizer: FeeQuantizer | None = None,
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
        required_features: Feature names that makers must support (passed to filter_offers)
        fee_quantizer: Optional fee homogenization policy (issue #508). When active,
            offers are filtered against the per-slot quantum and the returned total
            fee reflects the homogenized fee actually paid to each maker.

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
        required_features=required_features,
        fee_quantizer=fee_quantizer,
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

    # Calculate total fee (homogenized when quantization is active)
    total_fee = _selected_total_fee(selected, cj_amount, fee_quantizer)

    logger.info(
        f"Selected {len(result)} makers from {len(offers)} offers, total fee: {total_fee} sats"
    )

    return result, total_fee


def _solve_sweep_quantized(
    available: int, selected: list[Offer], fee_quantizer: FeeQuantizer
) -> int:
    """Solve the sweep cj_amount for a (possibly mixed) set of selected makers.

    ``available`` is ``total_input - my_txfee``. We need the largest ``cj_amount``
    such that ``cj_amount + sum(maker_paid_fee(offer, cj_amount)) <= available``,
    where each maker's paid fee follows the per-maker policy: NG makers are paid
    the homogenized slot fee, legacy makers their exact advertised fee. Because
    every per-maker fee is non-decreasing in ``cj_amount`` (relative fees grow,
    absolute fees stay flat), the left-hand side is strictly increasing, so a
    binary search finds the unique maximum.
    """

    def total_spent(cj_amount: int) -> int:
        fees = sum(maker_paid_fee(o, cj_amount, fee_quantizer) for o in selected)
        return cj_amount + fees

    if available <= 0 or total_spent(1) > available:
        return 0

    lo, hi = 1, available
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if total_spent(mid) <= available:
            lo = mid
        else:
            hi = mid - 1
    return lo


def choose_sweep_orders(
    offers: list[Offer],
    total_input_value: int,
    my_txfee: int,
    n: int,
    max_cj_fee: MaxCjFee,
    choose_fn: Callable[[list[Offer], int], list[Offer]] | None = None,
    ignored_makers: set[str] | None = None,
    min_nick_version: int | None = None,
    bondless_makers_allowance: float = 0.2,
    bondless_require_zero_fee: bool = True,
    required_features: set[str] | None = None,
    fee_quantizer: FeeQuantizer | None = None,
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
        required_features: Feature names that makers must support (passed to filter_offers)
        fee_quantizer: Optional fee homogenization policy (issue #508). When active,
            the sweep amount is solved against the per-slot quantum so every maker
            is paid the same homogenized fee.

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
        required_features=required_features,
        fee_quantizer=fee_quantizer,
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
    if fee_quantizer is not None and fee_quantizer.active:
        cj_amount = _solve_sweep_quantized(total_input_value - my_txfee, selected, fee_quantizer)
    else:
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
    total_fee = _selected_total_fee(selected, cj_amount, fee_quantizer)

    logger.info(f"Sweep: selected {len(result)} makers, cj_amount={cj_amount}, fee={total_fee}")

    return result, cj_amount, total_fee


class OrderbookManager:
    """Manages orderbook state and maker selection."""

    def __init__(
        self,
        max_cj_fee: MaxCjFee,
        bondless_makers_allowance: float = 0.2,
        bondless_require_zero_fee: bool = True,
        data_dir: Any = None,  # Path | None, but avoid import
        own_wallet_nicks: set[str] | None = None,
        fee_quantizer: FeeQuantizer | None = None,
    ):
        self.max_cj_fee = max_cj_fee
        self.bondless_makers_allowance = bondless_makers_allowance
        self.bondless_require_zero_fee = bondless_require_zero_fee
        self.fee_quantizer = fee_quantizer
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
        hard_exclude_nicks: set[str] | None = None,
        required_features: set[str] | None = None,
    ) -> tuple[dict[str, Offer], int]:
        """
        Select makers for a CoinJoin.

        Args:
            cj_amount: Target amount
            n: Number of makers
            honest_only: Only select from honest makers
            min_nick_version: Minimum required nick version (e.g., 6 for neutrino takers)
            exclude_nicks: Soft exclusion nicks. Preferred to be excluded (used by
                tumbler to avoid repeating makers across phases), but if not enough
                eligible makers remain after applying both hard and soft exclusions,
                we relax this set rather than fail. ``ignored_makers`` is treated
                the same way (best-effort avoidance).
            hard_exclude_nicks: Strict exclusion nicks. Never relaxed. Use this for
                makers that just rejected/failed inside the *current* CoinJoin
                attempt (re-asking them would just fail again) and for any caller
                that genuinely cannot include the nick.
            required_features: Feature names that makers must support

        Returns:
            (selected offers dict, total fee)
        """
        return self._select_with_soft_fallback(
            cj_amount=cj_amount,
            n=n,
            honest_only=honest_only,
            min_nick_version=min_nick_version,
            exclude_nicks=exclude_nicks,
            hard_exclude_nicks=hard_exclude_nicks,
            required_features=required_features,
        )

    def _select_with_soft_fallback(
        self,
        cj_amount: int,
        n: int,
        honest_only: bool,
        min_nick_version: int | None,
        exclude_nicks: set[str] | None,
        hard_exclude_nicks: set[str] | None,
        required_features: set[str] | None,
    ) -> tuple[dict[str, Offer], int]:
        """Select makers, falling back to soft-excluded ones if needed.

        First pass uses the union of hard and soft exclusions. If that yields
        fewer than ``n`` makers, we retry without the soft exclusions so the
        CoinJoin can still proceed (best-effort avoidance, see issue
        ``coinjoin must not fail because of soft blacklist``).
        """
        available_offers = (
            [o for o in self.offers if o.counterparty in self.honest_makers]
            if honest_only
            else self.offers
        )

        hard = self.own_wallet_nicks.copy()
        if hard_exclude_nicks:
            hard.update(hard_exclude_nicks)

        soft = self.ignored_makers.copy()
        if exclude_nicks:
            soft.update(exclude_nicks)
        # Never let a hard-excluded nick sneak back in through the soft set.
        soft.difference_update(hard)

        result, fee = choose_orders(
            offers=available_offers,
            cj_amount=cj_amount,
            n=n,
            max_cj_fee=self.max_cj_fee,
            ignored_makers=hard | soft,
            min_nick_version=min_nick_version,
            bondless_makers_allowance=self.bondless_makers_allowance,
            bondless_require_zero_fee=self.bondless_require_zero_fee,
            required_features=required_features,
            fee_quantizer=self.fee_quantizer,
        )
        if len(result) >= n or not soft:
            return result, fee

        logger.warning(
            f"Only {len(result)}/{n} makers available with soft exclusions "
            f"(ignored / previously-used: {len(soft)} nicks). Topping up from "
            "soft-excluded pool to avoid failing the CoinJoin."
        )
        # Top-up: keep the strict pick (so we don't accidentally drop the
        # soft-clean makers) and only ask choose_orders for the missing slots
        # from the soft-excluded pool. Already-selected nicks are added to
        # ``ignored_makers`` for the second call so the same maker isn't
        # picked twice.
        missing = n - len(result)
        already_picked = set(result.keys())
        topup_result, topup_fee = choose_orders(
            offers=available_offers,
            cj_amount=cj_amount,
            n=missing,
            max_cj_fee=self.max_cj_fee,
            ignored_makers=hard | already_picked,
            min_nick_version=min_nick_version,
            bondless_makers_allowance=self.bondless_makers_allowance,
            bondless_require_zero_fee=self.bondless_require_zero_fee,
            required_features=required_features,
            fee_quantizer=self.fee_quantizer,
        )
        result.update(topup_result)
        return result, fee + topup_fee

    def select_makers_for_sweep(
        self,
        total_input_value: int,
        my_txfee: int,
        n: int,
        honest_only: bool = False,
        min_nick_version: int | None = None,
        exclude_nicks: set[str] | None = None,
        hard_exclude_nicks: set[str] | None = None,
        required_features: set[str] | None = None,
    ) -> tuple[dict[str, Offer], int, int]:
        """
        Select makers for a sweep CoinJoin.

        Args:
            total_input_value: Total input value
            my_txfee: Taker's tx fee portion
            n: Number of makers
            honest_only: Only select from honest makers
            min_nick_version: Minimum required nick version (e.g., 6 for neutrino takers)
            exclude_nicks: Soft exclusion nicks (best-effort; relaxed if not enough
                makers remain). See :meth:`select_makers` for full semantics.
            hard_exclude_nicks: Strict exclusion nicks (never relaxed).
            required_features: Feature names that makers must support

        Returns:
            (selected offers dict, cj_amount, total fee)
        """
        available_offers = (
            [o for o in self.offers if o.counterparty in self.honest_makers]
            if honest_only
            else self.offers
        )

        hard = self.own_wallet_nicks.copy()
        if hard_exclude_nicks:
            hard.update(hard_exclude_nicks)

        soft = self.ignored_makers.copy()
        if exclude_nicks:
            soft.update(exclude_nicks)
        soft.difference_update(hard)

        result = choose_sweep_orders(
            offers=available_offers,
            total_input_value=total_input_value,
            my_txfee=my_txfee,
            n=n,
            max_cj_fee=self.max_cj_fee,
            ignored_makers=hard | soft,
            min_nick_version=min_nick_version,
            bondless_makers_allowance=self.bondless_makers_allowance,
            bondless_require_zero_fee=self.bondless_require_zero_fee,
            required_features=required_features,
            fee_quantizer=self.fee_quantizer,
        )
        if len(result[0]) >= n or not soft:
            return result

        logger.warning(
            f"Sweep: only {len(result[0])}/{n} makers available with soft exclusions "
            f"({len(soft)} nicks). Retrying without soft exclusions to avoid "
            "failing the sweep."
        )
        return choose_sweep_orders(
            offers=available_offers,
            total_input_value=total_input_value,
            my_txfee=my_txfee,
            n=n,
            max_cj_fee=self.max_cj_fee,
            ignored_makers=hard,
            min_nick_version=min_nick_version,
            bondless_makers_allowance=self.bondless_makers_allowance,
            bondless_require_zero_fee=self.bondless_require_zero_fee,
            required_features=required_features,
            fee_quantizer=self.fee_quantizer,
        )
