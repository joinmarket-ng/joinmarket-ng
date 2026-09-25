"""
Orderbook management and order selection for taker.

Implements:
- Orderbook fetching from directory nodes
- Order filtering by fee limits and amount ranges
- Maker selection algorithms (fidelity bond weighted, random, cheapest)
- Fee calculation for CoinJoin transactions
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from decimal import Decimal
from typing import Any

from jmcore.bitcoin import (
    calculate_relative_fee,
    calculate_sweep_amount,
)
from jmcore.fee_quantization import QUANT_ABS, QUANT_REL, quantize_abs_up, quantize_rel_up
from jmcore.models import Offer, OfferType, is_absolute_offer_type
from jmcore.paths import get_ignored_makers_path
from jmcore.protocol import (
    FEATURE_COFUNDED_CHANNEL_RING_V1,
    FEATURE_NEUTRINO_COMPAT,
    get_nick_version,
)
from jmcore.randomness import secure_random
from loguru import logger

from taker.config import MaxCjFee

DEFAULT_MAKER_REPEAT_PENALTY = 0.8
_MAX_REPEAT_PENALTY_DRAWS = 64


def maker_nick_selection_key(nick: str) -> str:
    """Return the history key used to recognize a maker nickname."""
    return f"nick:{nick}"


def maker_selection_keys(offer: Offer) -> set[str]:
    """Return stable selection-history keys for an offer.

    A verified positive-value bond is the strongest public identity available
    across nickname changes. The nickname remains useful for bondless makers
    and for a bonded maker that rotates its advertised bond.
    """
    keys = {maker_nick_selection_key(offer.counterparty)}
    bond_data = offer.fidelity_bond_data
    if not bond_data or offer.fidelity_bond_value <= 0:
        return keys

    txid = bond_data.get("utxo_txid")
    vout = bond_data.get("utxo_vout")
    if isinstance(txid, str) and isinstance(vout, int):
        keys.add(f"bond:{txid}:{vout}")
    return keys


def choose_with_repeat_penalty(
    offers: list[Offer],
    n: int,
    choose_fn: Callable[[list[Offer], int], list[Offer]],
    penalized_maker_keys: set[str] | None,
    repeat_penalty: float = DEFAULT_MAKER_REPEAT_PENALTY,
) -> list[Offer]:
    """Tilt a baseline maker-set chooser away from recent counterparties.

    A baseline set with ``k`` avoidable repeated makers is accepted with
    probability ``repeat_penalty ** k``. This preserves full support and leaves
    canonical fidelity-bond values untouched. Repeats forced by a thin pool do
    not count toward ``k`` because their common factor cannot change the
    normalized selection distribution.

    The draw bound prevents pathological custom maker counts or chooser
    implementations from stalling selection. On exhaustion, the final valid
    baseline draw is returned, so diversity policy can never fail a CoinJoin.
    """
    if not 0 < repeat_penalty <= 1:
        raise ValueError("repeat_penalty must be in the interval (0, 1]")
    if not penalized_maker_keys or repeat_penalty == 1:
        return choose_fn(offers, n)

    fresh_offer_count = sum(
        not bool(maker_selection_keys(offer) & penalized_maker_keys) for offer in offers
    )
    selected: list[Offer] = []
    for attempt in range(1, _MAX_REPEAT_PENALTY_DRAWS + 1):
        selected = choose_fn(offers, n)
        overlap = sum(
            bool(maker_selection_keys(offer) & penalized_maker_keys) for offer in selected
        )
        unavoidable_overlap = max(0, len(selected) - fresh_offer_count)
        avoidable_overlap = max(0, overlap - unavoidable_overlap)
        if secure_random.random() <= repeat_penalty**avoidable_overlap:
            if attempt > 1:
                logger.debug(
                    f"Accepted maker set after {attempt} diversity draws "
                    f"({avoidable_overlap} avoidable repeats)"
                )
            return selected

    logger.debug(
        f"Maker diversity draw limit reached; accepting baseline set with "
        f"repeat_penalty={repeat_penalty}"
    )
    return selected


def is_quantized_cj_fee(offer: Offer) -> bool:
    """Return whether an offer advertises a fee exactly on its public grid."""
    if is_absolute_offer_type(offer.ordertype):
        return int(offer.cjfee) in QUANT_ABS
    return Decimal(str(offer.cjfee)) in QUANT_REL


def _paid_fee_policy(offer: Offer, round_up_cj_fees: bool) -> int | Decimal | None:
    """Return the fee policy paid for an offer, or None when it cannot be rounded."""
    if is_absolute_offer_type(offer.ordertype):
        advertised_absolute = int(offer.cjfee)
        return quantize_abs_up(advertised_absolute) if round_up_cj_fees else advertised_absolute

    advertised_relative = Decimal(str(offer.cjfee))
    return quantize_rel_up(advertised_relative) if round_up_cj_fees else advertised_relative


def calculate_cj_fee(offer: Offer, cj_amount: int, round_up_cj_fees: bool = False) -> int:
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
    policy = _paid_fee_policy(offer, round_up_cj_fees)
    if policy is None:
        raise ValueError(f"Offer from {offer.counterparty} has no upper fee quantum")
    if is_absolute_offer_type(offer.ordertype):
        return int(policy)
    return calculate_relative_fee(cj_amount, str(policy))


def calculate_cj_fee_plan(
    offers: Iterable[Offer],
    cj_amount: int,
    round_up_cj_fees: bool = False,
    equalize_cj_fees: bool = False,
) -> dict[str, int]:
    """Return the paid fee for each selected maker.

    Equalization compares realized satoshi fees, which keeps absolute and
    relative offers comparable at the actual CoinJoin amount.
    """
    fees = {
        offer.counterparty: calculate_cj_fee(offer, cj_amount, round_up_cj_fees) for offer in offers
    }
    if equalize_cj_fees and fees:
        target = max(fees.values())
        return dict.fromkeys(fees, target)
    return fees


def _calculate_equalized_sweep_amount(
    offers: list[Offer],
    available: int,
    round_up_cj_fees: bool,
) -> int:
    """Find the largest sweep amount funded by a uniform maker fee."""
    low = 0
    high = max(available, 0)
    result = 0
    while low <= high:
        candidate = (low + high) // 2
        total_fee = sum(
            calculate_cj_fee_plan(
                offers,
                candidate,
                round_up_cj_fees=round_up_cj_fees,
                equalize_cj_fees=True,
            ).values()
        )
        if candidate + total_fee <= available:
            result = candidate
            low = candidate + 1
        else:
            high = candidate - 1
    return result


def is_fee_within_limits(
    offer: Offer,
    cj_amount: int,
    max_cj_fee: MaxCjFee,
    round_up_cj_fees: bool = False,
) -> bool:
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
    policy = _paid_fee_policy(offer, round_up_cj_fees)
    if policy is None:
        return False
    if is_absolute_offer_type(offer.ordertype):
        return int(policy) <= max_cj_fee.abs_fee
    return Decimal(policy) <= Decimal(max_cj_fee.rel_fee)


def filter_offers(
    offers: list[Offer],
    cj_amount: int,
    max_cj_fee: MaxCjFee,
    ignored_makers: set[str] | None = None,
    allowed_types: set[OfferType] | None = None,
    min_nick_version: int | None = None,
    required_features: set[str] | None = None,
    require_quantized_cj_fees: bool = False,
    round_up_cj_fees: bool = False,
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
        excluded_label = "maker" if len(ignored_makers) == 1 else "makers"
        logger.debug(
            f"Filtering offers: {len(ignored_makers)} {excluded_label} excluded from this selection"
        )

    eligible = []
    exclusion_counts = {
        "selection exclusion": 0,
        "nick version": 0,
        "known missing feature": 0,
        "offer type": 0,
        "non-quantized fee": 0,
        "below minsize": 0,
        "above maxsize": 0,
        "fee limit": 0,
    }

    for offer in offers:
        # Filter by maker
        if offer.counterparty in ignored_makers:
            exclusion_counts["selection exclusion"] += 1
            continue

        # Filter by nick version (reserved for potential future reference compatibility)
        # NOTE: This is NOT used for neutrino detection - that uses handshake features
        if min_nick_version is not None:
            nick_version = get_nick_version(offer.counterparty)
            if nick_version < min_nick_version:
                exclusion_counts["nick version"] += 1
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
                exclusion_counts["known missing feature"] += 1
                logger.debug(
                    f"Ignoring offer from {offer.counterparty}: missing required features {missing}"
                )
                continue

        # Filter by offer type
        if offer.ordertype not in allowed_types:
            exclusion_counts["offer type"] += 1
            logger.debug(
                f"Ignoring offer from {offer.counterparty}: "
                f"type {offer.ordertype} not in allowed types"
            )
            continue

        if require_quantized_cj_fees and not is_quantized_cj_fee(offer):
            exclusion_counts["non-quantized fee"] += 1
            logger.debug(f"Ignoring offer from {offer.counterparty}: fee is not on the public grid")
            continue

        # Filter by amount range
        if cj_amount < offer.minsize:
            exclusion_counts["below minsize"] += 1
            logger.bind(sensitive=True).trace(
                f"Ignoring offer from {offer.counterparty}: "
                f"amount {cj_amount} < minsize {offer.minsize}"
            )
            continue

        if cj_amount > offer.maxsize:
            exclusion_counts["above maxsize"] += 1
            logger.bind(sensitive=True).debug(
                f"Ignoring offer from {offer.counterparty}: "
                f"amount {cj_amount} > maxsize {offer.maxsize}"
            )
            continue

        # Filter by fee limits
        if not is_fee_within_limits(offer, cj_amount, max_cj_fee, round_up_cj_fees):
            exclusion_counts["fee limit"] += 1
            policy = _paid_fee_policy(offer, round_up_cj_fees)
            fee = (
                calculate_cj_fee(offer, cj_amount, round_up_cj_fees) if policy is not None else None
            )
            logger.bind(sensitive=True).trace(
                f"Ignoring offer from {offer.counterparty}: fee {fee} exceeds limits"
            )
            continue

        eligible.append(offer)

    exclusion_summary = ", ".join(
        f"{reason}={count}" for reason, count in exclusion_counts.items() if count
    )
    offer_label = "offer" if len(eligible) == 1 else "offers"
    summary = f"Filtered {len(offers)} offers to {len(eligible)} eligible {offer_label}"
    logger.info(f"{summary} ({exclusion_summary})" if exclusion_summary else summary)
    return eligible


def dedupe_offers_by_maker(
    offers: list[Offer],
    cj_amount: int = 100_000_000,
    round_up_cj_fees: bool = False,
) -> list[Offer]:
    """
    Keep only the cheapest offer from each maker.

    Args:
        offers: List of offers (possibly multiple per maker)
        cj_amount: CoinJoin amount used to compare realized fees
        round_up_cj_fees: Whether comparisons include taker fee rounding

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
        sorted_offers = sorted(
            maker_offers,
            key=lambda o: calculate_cj_fee(o, cj_amount, round_up_cj_fees),
        )
        result.append(sorted_offers[0])
        if len(maker_offers) > 1:
            logger.debug(f"Kept cheapest of {len(maker_offers)} offers from {maker}")

    return result


def dedupe_offers_by_bond(
    offers: list[Offer], cj_amount: int, round_up_cj_fees: bool = False
) -> list[Offer]:
    """
    Deduplicate offers by fidelity bond UTXO, keeping only the cheapest per bond.

    This is a sybil protection measure: if two different counterparties (nicks)
    share the same fidelity bond UTXO, we should only select one of them.
    Otherwise, an attacker could create multiple nicks backed by the same bond
    and get selected multiple times in the same CoinJoin.

    Offers without a verified, positive-value fidelity bond are passed through
    unchanged. Unverified or expired proofs must not displace a verified offer
    that uses the same UTXO.

    Args:
        offers: List of offers (possibly from different makers using same bond)
        cj_amount: The actual CoinJoin amount for accurate fee comparison
        round_up_cj_fees: Whether comparisons include taker fee rounding

    Returns:
        List with at most one offer per bond UTXO (the cheapest), plus all unbonded offers
    """
    # Group bonded offers by bond UTXO
    by_bond: dict[str, list[Offer]] = {}
    unbonded: list[Offer] = []

    for offer in offers:
        bond_key = None
        if offer.fidelity_bond_data and offer.fidelity_bond_value > 0:
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
        sorted_offers = sorted(
            bond_offers,
            key=lambda o: calculate_cj_fee(o, cj_amount, round_up_cj_fees),
        )
        result.append(sorted_offers[0])
        if len(bond_offers) > 1:
            kept = sorted_offers[0]
            dropped = [o.counterparty for o in sorted_offers[1:]]
            kept_fee = calculate_cj_fee(kept, cj_amount, round_up_cj_fees)
            logger.warning(
                f"Bond sybil protection: Kept {kept.counterparty} (fee={kept_fee}), "
                f"dropped {dropped} sharing same bond UTXO {bond_key[:16]}..."
            )

    # Add unbonded offers unchanged
    result.extend(unbonded)

    return result


def _offer_confirms_features(offer: Offer, required_features: set[str]) -> bool:
    """Check whether *offer* is confirmed to support all required features.

    A feature is confirmed via the handshake-derived ``features`` dict, or,
    for ``neutrino_compat`` only, via the deprecated ``!neutrino`` offer flag
    (kept for parity with the pre-check in ``Taker.do_coinjoin``).
    """
    return all(
        offer.features.get(feature)
        or (feature == FEATURE_NEUTRINO_COMPAT and offer.neutrino_compat)
        for feature in required_features
    )


def offer_supports_cofunded_channel_ring_v1(offer: Offer) -> bool:
    """Recognize backend-validated ring support without changing offer selection."""
    return offer.features.get(FEATURE_COFUNDED_CHANNEL_RING_V1, False)


def prefer_offers_with_confirmed_features(
    offers: list[Offer], n: int, required_features: set[str] | None
) -> list[Offer]:
    """Prefer offers whose makers are confirmed to support the required features.

    ``filter_offers`` only drops offers *known* to lack a required feature;
    offers with unknown feature status (empty ``features`` dict, e.g. relayed
    by a directory without ``peerlist_features``) pass through so they can be
    revalidated during the handshake. But when enough confirmed-compatible
    offers exist to fill all ``n`` slots, selecting an unknown-status maker is
    an avoidable risk: if it turns out to be incompatible the round needs a
    replacement pass (or fails). Restrict the pool to confirmed offers in that
    case; otherwise keep the unknown-status offers as fallback candidates.
    """
    if not required_features:
        return offers

    confirmed = [o for o in offers if _offer_confirms_features(o, required_features)]
    unknown_count = len(offers) - len(confirmed)
    if unknown_count and len(confirmed) >= n:
        logger.info(
            f"Preferring {len(confirmed)} offers with confirmed features "
            f"{sorted(required_features)}; skipping {unknown_count} offers with "
            f"unknown feature status (enough confirmed makers for {n} slots)"
        )
        return confirmed
    if unknown_count:
        logger.info(
            f"Feature fallback: confirmed={len(confirmed)}, requested={n}, "
            f"unknown={unknown_count}; retaining unknown-status offers as candidates"
        )
    return offers


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

    return secure_random.sample(offers, n)


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
        return secure_random.sample(offers, n)

    selected = []
    remaining_offers = list(enumerate(offers))
    remaining_weights = list(weights)

    for _ in range(n):
        if not remaining_offers:
            break

        # Weighted random selection
        total = sum(remaining_weights)
        r = secure_random.uniform(0, total)
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
    bondless_makers_allowance: float = 0.05,
    bondless_require_zero_fee: bool = True,
    cj_amount: int = 0,
) -> list[Offer]:
    """
    Choose n offers using per-slot probabilistic selection.

    **Fee policy** (when ``bondless_require_zero_fee`` is True):
    Bondless offers (``fidelity_bond_value == 0``) that charge a non-zero
    advertised CoinJoin fee are removed before selection. This prevents an
    attacker from flooding the orderbook with fee-charging bondless offers to
    steal fees while still allowing genuine zero-fee bondless makers to
    participate. Allowance slots then select only from zero-fee offers, whether
    bonded or bondless.

    **Per-slot selection** (for each of the *n* slots independently):

    * With probability ``bondless_makers_allowance``: pick **uniformly at
      random** from the zero-fee offers. Bonded and bondless zero-fee makers
      compete equally for these slots.
    * Otherwise: pick from the bonded pool (``fidelity_bond_value > 0``)
      **weighted by bond value**.

    Fallback: if the chosen pool is empty, the other pool is tried.

    This mirrors the reference JoinMarket implementation and ensures:

    * High-bond makers are strongly favored (~95% of slots with the default
      0.05 allowance).
    * Zero-fee makers compete uniformly for the remaining allowance slots.

    Args:
        offers: Eligible offers (already filtered and deduped).
        n: Number of offers to choose.
        bondless_makers_allowance: Per-slot probability of uniform zero-fee
            selection (0.0-1.0).
        bondless_require_zero_fee: If True, pre-filter removes bondless
            offers with a non-zero advertised CoinJoin fee and allowance slots
            only select zero-fee offers. If False, allowance slots select
            uniformly from all offers.
        cj_amount: CoinJoin amount (reserved for future fee filtering).

    Returns:
        Selected offers.
    """
    # --- Pre-filter: remove bondless offers charging a fee ---
    if bondless_require_zero_fee:
        filtered: list[Offer] = []
        removed = 0
        for o in offers:
            if o.fidelity_bond_value == 0 and _is_nonzero_cj_fee(o):
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

    if len(remaining) <= n:
        return remaining

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

        allowance_slot = secure_random.random() < bondless_makers_allowance
        if allowance_slot:
            allowance_pool = (
                [offer for offer in remaining if not _is_nonzero_cj_fee(offer)]
                if bondless_require_zero_fee
                else remaining
            )
            if allowance_pool:
                picked = secure_random.choice(allowance_pool)
        else:
            # Bonded slot: pick weighted by bond value
            picked = _pick_weighted_bonded(remaining)

        if picked is None:
            if allowance_slot:
                picked = _pick_weighted_bonded(remaining)
            else:
                allowance_pool = (
                    [offer for offer in remaining if not _is_nonzero_cj_fee(offer)]
                    if bondless_require_zero_fee
                    else remaining
                )
                if allowance_pool:
                    picked = secure_random.choice(allowance_pool)

        if picked is None:
            logger.warning(f"No eligible offer pool after {len(selected)}/{n} picks")
            break

        selected.append(picked)
        remaining.remove(picked)

    logger.debug(
        f"Final selection: {len(selected)} makers "
        f"({sum(1 for o in selected if o.fidelity_bond_value > 0)} bonded, "
        f"{sum(1 for o in selected if o.fidelity_bond_value == 0)} bondless)"
    )
    return selected


def _is_nonzero_cj_fee(offer: Offer) -> bool:
    """Check whether an offer advertises a non-zero CoinJoin fee."""
    return Decimal(str(offer.cjfee)) != 0


def _pick_weighted_bonded(pool: list[Offer]) -> Offer | None:
    """Pick one offer from *pool* weighted by fidelity_bond_value."""
    bonded = [(o, o.fidelity_bond_value) for o in pool if o.fidelity_bond_value > 0]
    if not bonded:
        return None
    total = sum(w for _, w in bonded)
    r = secure_random.uniform(0, total)
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
    bondless_makers_allowance: float = 0.05,
    bondless_require_zero_fee: bool = True,
    required_features: set[str] | None = None,
    penalized_maker_keys: set[str] | None = None,
    maker_repeat_penalty: float = DEFAULT_MAKER_REPEAT_PENALTY,
    require_quantized_cj_fees: bool = False,
    round_up_cj_fees: bool = False,
    equalize_cj_fees: bool = False,
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
        bondless_makers_allowance: Probability of uniform zero-fee selection vs bond weighting
        bondless_require_zero_fee: If True, allowance spots only select zero-fee offers
        required_features: Feature names that makers must support (passed to filter_offers)
        penalized_maker_keys: Recent maker nick and bond keys to probabilistically penalize
        maker_repeat_penalty: Per-repeated-maker acceptance multiplier
        require_quantized_cj_fees: Require advertised fees to be on the public grid
        round_up_cj_fees: Round selected maker fees up to public fee quanta
        equalize_cj_fees: Pay every selected maker the highest realized fee
        allowed_types: Offer types allowed for this round, restricting makers to a single
            output script family (rigid pit, JMP-0010). Defaults to all sw0* types.

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
        require_quantized_cj_fees=require_quantized_cj_fees,
        round_up_cj_fees=round_up_cj_fees,
        allowed_types=allowed_types,
    )

    # Dedupe by maker (keep cheapest offer per counterparty)
    deduped_by_maker = dedupe_offers_by_maker(eligible, cj_amount, round_up_cj_fees)

    # Dedupe by bond UTXO (sybil protection: keep cheapest offer per bond)
    # This must come after maker dedup so we compare the best offer from each nick
    deduped = dedupe_offers_by_bond(deduped_by_maker, cj_amount, round_up_cj_fees)

    # When enough makers confirm the required features, avoid unknown-status
    # ones (they may fail feature negotiation mid-session and need replacement).
    deduped = prefer_offers_with_confirmed_features(deduped, n, required_features)

    if len(deduped) < n:
        logger.warning(
            f"Not enough makers: need {n}, found {len(deduped)} (from {len(offers)} total offers)"
        )
        n = len(deduped)

    # Select makers
    selected = choose_with_repeat_penalty(
        deduped,
        n,
        choose_fn,
        penalized_maker_keys,
        maker_repeat_penalty,
    )

    # Build result
    result = {offer.counterparty: offer for offer in selected}

    # Calculate total fee
    fee_plan = calculate_cj_fee_plan(
        selected,
        cj_amount,
        round_up_cj_fees=round_up_cj_fees,
        equalize_cj_fees=equalize_cj_fees,
    )
    total_fee = sum(fee_plan.values())

    logger.info(
        f"Selected {len(result)} makers from {len(offers)} offers, total fee: {total_fee} sats"
    )
    if equalize_cj_fees and fee_plan:
        logger.info(
            f"Maker fee equalization enabled: paying {max(fee_plan.values())} sats per maker"
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
    bondless_makers_allowance: float = 0.05,
    bondless_require_zero_fee: bool = True,
    required_features: set[str] | None = None,
    penalized_maker_keys: set[str] | None = None,
    maker_repeat_penalty: float = DEFAULT_MAKER_REPEAT_PENALTY,
    require_quantized_cj_fees: bool = False,
    round_up_cj_fees: bool = False,
    equalize_cj_fees: bool = False,
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
        bondless_makers_allowance: Probability of uniform zero-fee selection vs bond weighting
        bondless_require_zero_fee: If True, allowance spots only select zero-fee offers
        required_features: Feature names that makers must support (passed to filter_offers)
        penalized_maker_keys: Recent maker nick and bond keys to probabilistically penalize
        maker_repeat_penalty: Per-repeated-maker acceptance multiplier
        require_quantized_cj_fees: Require advertised fees to be on the public grid
        round_up_cj_fees: Round selected maker fees up to public fee quanta
        equalize_cj_fees: Pay every selected maker the highest realized fee
        allowed_types: Offer types allowed for this round, restricting makers to a single
            output script family (rigid pit, JMP-0010). Defaults to all sw0* types.

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
        require_quantized_cj_fees=require_quantized_cj_fees,
        round_up_cj_fees=round_up_cj_fees,
        allowed_types=allowed_types,
    )

    # Dedupe by maker
    deduped_by_maker = dedupe_offers_by_maker(eligible, estimated_cj_amount, round_up_cj_fees)

    # Dedupe by bond UTXO (sybil protection)
    # Use estimated_cj_amount for fee comparison since we don't know exact amount yet
    deduped = dedupe_offers_by_bond(deduped_by_maker, estimated_cj_amount, round_up_cj_fees)

    # When enough makers confirm the required features, avoid unknown-status
    # ones (they may fail feature negotiation mid-session and need replacement).
    deduped = prefer_offers_with_confirmed_features(deduped, n, required_features)

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
    selected = choose_with_repeat_penalty(
        deduped,
        n,
        choose_fn,
        penalized_maker_keys,
        maker_repeat_penalty,
    )

    # Now solve for exact cj_amount.
    if equalize_cj_fees:
        cj_amount = _calculate_equalized_sweep_amount(
            selected,
            total_input_value - my_txfee,
            round_up_cj_fees,
        )
    else:
        sum_abs_fees = 0
        rel_fees = []

        for offer in selected:
            if is_absolute_offer_type(offer.ordertype):
                policy = _paid_fee_policy(offer, round_up_cj_fees)
                assert isinstance(policy, int)
                sum_abs_fees += policy
            else:
                policy = _paid_fee_policy(offer, round_up_cj_fees)
                assert isinstance(policy, Decimal)
                rel_fees.append(str(policy))

        available = total_input_value - my_txfee - sum_abs_fees
        cj_amount = calculate_sweep_amount(available, rel_fees)

    # Verify this works for all selected offers
    for offer in selected:
        if cj_amount < offer.minsize or cj_amount > offer.maxsize:
            logger.error(
                f"Sweep amount {cj_amount} outside range for {offer.counterparty}: "
                f"{offer.minsize}-{offer.maxsize}"
            )
            return {}, 0, 0

    result = {offer.counterparty: offer for offer in selected}
    fee_plan = calculate_cj_fee_plan(
        selected,
        cj_amount,
        round_up_cj_fees=round_up_cj_fees,
        equalize_cj_fees=equalize_cj_fees,
    )
    total_fee = sum(fee_plan.values())

    logger.bind(sensitive=True).info(
        f"Sweep: selected {len(result)} makers, cj_amount={cj_amount}, fee={total_fee}"
    )

    return result, cj_amount, total_fee


class OrderbookManager:
    """Manages orderbook state and maker selection."""

    def __init__(
        self,
        max_cj_fee: MaxCjFee,
        bondless_makers_allowance: float = 0.05,
        bondless_require_zero_fee: bool = True,
        data_dir: Any = None,  # Path | None, but avoid import
        own_wallet_nicks: set[str] | None = None,
        require_quantized_cj_fees: bool = False,
        round_up_cj_fees: bool = False,
        equalize_cj_fees: bool = False,
        allowed_types: set[OfferType] | None = None,
    ):
        self.max_cj_fee = max_cj_fee
        self.bondless_makers_allowance = bondless_makers_allowance
        self.bondless_require_zero_fee = bondless_require_zero_fee
        self.require_quantized_cj_fees = require_quantized_cj_fees
        self.round_up_cj_fees = round_up_cj_fees
        self.equalize_cj_fees = equalize_cj_fees
        self.allowed_types = allowed_types
        self.offers: list[Offer] = []
        self.bonds: dict[str, Any] = {}  # maker -> bond info
        self.ignored_makers: set[str] = set()
        self.honest_makers: set[str] = set()

        # Own wallet nicks to exclude from peer selection (e.g., same wallet's maker nick)
        # This is populated from state files and protects against self-CoinJoins
        self.own_wallet_nicks: set[str] = own_wallet_nicks or set()
        if self.own_wallet_nicks:
            logger.info("Excluding own wallet identities from peer selection")
            logger.bind(sensitive=True).info(
                f"Excluding own wallet nicks from peer selection: {self.own_wallet_nicks}"
            )

        # Persistence for ignored makers
        self.ignored_makers_path = get_ignored_makers_path(data_dir)
        self._load_ignored_makers()

    def _load_ignored_makers(self) -> None:
        """Load ignored makers from disk."""
        if not self.ignored_makers_path.exists():
            logger.debug("No existing ignored makers file")
            logger.bind(sensitive=True).debug(
                f"No existing ignored makers file at {self.ignored_makers_path}"
            )
            return

        try:
            with open(self.ignored_makers_path, encoding="utf-8") as f:
                for line in f:
                    maker = line.strip()
                    if maker:
                        self.ignored_makers.add(maker)
            if self.ignored_makers:
                logger.info(f"Loaded {len(self.ignored_makers)} ignored makers")
                logger.bind(sensitive=True).info(
                    f"Loaded {len(self.ignored_makers)} ignored makers from "
                    f"{self.ignored_makers_path}"
                )
        except Exception as e:
            logger.error("Failed to load ignored makers")
            logger.bind(sensitive=True).error(
                f"Failed to load ignored makers from {self.ignored_makers_path}: {e}"
            )

    def _save_ignored_makers(self) -> None:
        """Save ignored makers to disk."""
        try:
            # Ensure parent directory exists
            self.ignored_makers_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.ignored_makers_path, "w", encoding="utf-8") as f:
                for maker in sorted(self.ignored_makers):
                    f.write(maker + "\n")
                f.flush()
            logger.debug(f"Saved {len(self.ignored_makers)} ignored makers")
            logger.bind(sensitive=True).debug(
                f"Saved {len(self.ignored_makers)} ignored makers to {self.ignored_makers_path}"
            )
        except Exception as e:
            logger.error("Failed to save ignored makers")
            logger.bind(sensitive=True).error(
                f"Failed to save ignored makers to {self.ignored_makers_path}: {e}"
            )

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
                logger.debug("Deleted ignored makers file")
                logger.bind(sensitive=True).debug(f"Deleted {self.ignored_makers_path}")
        except Exception as e:
            logger.error("Failed to delete ignored makers file")
            logger.bind(sensitive=True).error(f"Failed to delete {self.ignored_makers_path}: {e}")

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
        penalized_maker_keys: set[str] | None = None,
    ) -> tuple[dict[str, Offer], int]:
        """
        Select makers for a CoinJoin.

        Args:
            cj_amount: Target amount
            n: Number of makers
            honest_only: Only select from honest makers
            min_nick_version: Minimum required nick version (e.g., 6 for neutrino takers)
            exclude_nicks: Caller-supplied soft exclusion nicks. If not enough eligible
                makers remain after applying both hard and soft exclusions, this set is
                relaxed rather than failing. ``ignored_makers`` is treated the same way.
            hard_exclude_nicks: Strict exclusion nicks. Never relaxed. Use this for
                makers that just rejected/failed inside the *current* CoinJoin
                attempt (re-asking them would just fail again) and for any caller
                that genuinely cannot include the nick.
            required_features: Feature names that makers must support
            penalized_maker_keys: Recent maker nick and bond keys to penalize with
                :data:`DEFAULT_MAKER_REPEAT_PENALTY` while retaining full support

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
            penalized_maker_keys=penalized_maker_keys,
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
        penalized_maker_keys: set[str] | None,
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
            penalized_maker_keys=penalized_maker_keys,
            require_quantized_cj_fees=self.require_quantized_cj_fees,
            round_up_cj_fees=self.round_up_cj_fees,
            equalize_cj_fees=self.equalize_cj_fees,
            allowed_types=self.allowed_types,
        )
        if len(result) >= n or not soft:
            return result, fee

        logger.warning(
            f"Only {len(result)}/{n} makers available with soft exclusions "
            f"({len(soft)} ignored or caller-excluded nicks). Topping up from "
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
            penalized_maker_keys=penalized_maker_keys,
            require_quantized_cj_fees=self.require_quantized_cj_fees,
            round_up_cj_fees=self.round_up_cj_fees,
            equalize_cj_fees=self.equalize_cj_fees,
            allowed_types=self.allowed_types,
        )
        result.update(topup_result)
        if not self.equalize_cj_fees:
            return result, fee + topup_fee
        fee_plan = calculate_cj_fee_plan(
            result.values(),
            cj_amount,
            round_up_cj_fees=self.round_up_cj_fees,
            equalize_cj_fees=True,
        )
        return result, sum(fee_plan.values())

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
        penalized_maker_keys: set[str] | None = None,
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
            penalized_maker_keys: Recent maker nick and bond keys to penalize while
                retaining full support

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
            penalized_maker_keys=penalized_maker_keys,
            require_quantized_cj_fees=self.require_quantized_cj_fees,
            round_up_cj_fees=self.round_up_cj_fees,
            equalize_cj_fees=self.equalize_cj_fees,
            allowed_types=self.allowed_types,
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
            penalized_maker_keys=penalized_maker_keys,
            require_quantized_cj_fees=self.require_quantized_cj_fees,
            round_up_cj_fees=self.round_up_cj_fees,
            equalize_cj_fees=self.equalize_cj_fees,
            allowed_types=self.allowed_types,
        )
