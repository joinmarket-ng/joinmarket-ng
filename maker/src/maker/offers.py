"""
Offer management for makers.

Creates and manages liquidity offers based on wallet balance and configuration.
Supports multiple simultaneous offers with different fee structures (relative/absolute).
"""

from __future__ import annotations

import random

from jmcore.constants import DUST_THRESHOLD
from jmcore.models import Offer, OfferType
from jmwallet.wallet.service import WalletService
from loguru import logger

from maker.config import MakerConfig, OfferConfig
from maker.fidelity import get_best_fidelity_bond


def _randomize(value: float, factor: float, low: float | None = None) -> float:
    """Sample uniformly from ``[value*(1-factor), value*(1+factor)]``.

    When ``factor`` is 0 the input value is returned unchanged.  When ``low``
    is provided the result is clamped from below to that value (e.g. the dust
    threshold for sizes).  Returning a float lets callers cast to int where
    appropriate so we do not lose precision for relative fees.
    """
    if factor <= 0:
        result = float(value)
    else:
        result = random.uniform(value * (1.0 - factor), value * (1.0 + factor))
    if low is not None and result < low:
        return float(low)
    return result


def _format_relative_cjfee(value: float) -> str:
    """Format a relative CJ fee without scientific notation or trailing zeros.

    Mirrors the canonicalization performed by
    :meth:`maker.config.OfferConfig.normalize_cj_fee_relative` so that wire
    values stay compact and round-trip through the validator.
    """
    formatted = f"{value:.10f}".rstrip("0").rstrip(".")
    return formatted if formatted else "0"


class OfferManager:
    """
    Creates and manages offers for the maker bot.

    Supports creating multiple offers simultaneously, each with a unique offer ID.
    This allows makers to advertise both relative and absolute fee offers at the same time.
    """

    def __init__(self, wallet: WalletService, config: MakerConfig, maker_nick: str):
        self.wallet = wallet
        self.config = config
        self.maker_nick = maker_nick

    async def create_offers(self) -> list[Offer]:
        """
        Create offers based on wallet balance and configuration.

        Logic:
        1. Find mixdepth with maximum balance available for offers (excludes fidelity bonds)
        2. Randomize fees for each offer independently
        3. When exactly one relative and one absolute offer are configured,
           compute the fee intersection from the *randomized* fees and split
           size ranges there so the two offers cover disjoint, contiguous
           ranges without leaking the unrandomized fee values (issue #88)
        4. Assign and randomize size ranges, create Offer objects
        5. Attach fidelity bond value if available

        Returns:
            List of offers. Each offer gets a unique oid (0, 1, 2, ...).
        """
        try:
            balances = {}
            for mixdepth in range(self.wallet.mixdepth_count):
                # Use balance for offers (excludes fidelity bonds)
                balance = await self.wallet.get_balance_for_offers(
                    mixdepth,
                    min_confirmations=self.config.min_confirmations,
                    restrict_md0=not self.config.allow_mixdepth_zero_merge,
                )
                balances[mixdepth] = balance

            available_mixdepths = {md: bal for md, bal in balances.items() if bal > 0}

            if not available_mixdepths:
                logger.warning("No mixdepth with positive balance")
                return []

            logger.debug(f"Mixdepth balances (excluding fidelity bonds): {balances}")

            max_mixdepth = max(available_mixdepths, key=lambda md: available_mixdepths[md])
            max_balance = available_mixdepths[max_mixdepth]
            logger.info(f"Selected mixdepth {max_mixdepth} with balance {max_balance} sats")

            # Get effective offer configurations
            offer_configs = self.config.get_effective_offer_configs()

            # Step 1: randomize fees for every offer before touching sizes.
            # Storing (cjfee_str, randomized_txfee, numeric_cjfee) where
            # numeric_cjfee is a float for relative offers and an int for
            # absolute offers -- used only for the intersection calculation.
            randomized_fees: list[tuple[str, int, float]] = []
            for cfg in offer_configs:
                fees = self._randomize_offer_fees(cfg)
                if fees is None:
                    # Invalid config (e.g. non-positive relative fee) -- will
                    # be caught again in _create_single_offer; record a sentinel
                    # so indices stay aligned.
                    randomized_fees.append(("", 0, 0.0))
                else:
                    randomized_fees.append(fees)

            # Step 2: compute size-range overrides from the *randomized* fees.
            # This means the advertised size boundary reveals nothing about the
            # unrandomized fee configuration.  ``suppressed_indices`` lists
            # offers that the auto-split has rendered dominated and that must
            # be skipped entirely (rather than emitted with a degenerate range
            # that would trip the "Insufficient balance" warning).
            size_overrides, suppressed_indices = self._compute_dual_offer_size_overrides(
                offer_configs, randomized_fees, max_balance
            )

            # Get fidelity bond value if available (shared across all offers)
            fidelity_bond_value = 0
            bond = await get_best_fidelity_bond(self.wallet)
            if bond:
                fidelity_bond_value = bond.bond_value
                logger.info(
                    f"Fidelity bond found: {bond.txid}:{bond.vout} "
                    f"value={bond.value} sats, bond_value={bond.bond_value}"
                )

            # Step 3: create Offer objects with pre-randomized fees and
            # intersection-derived size bounds.
            offers: list[Offer] = []
            for offer_id, offer_cfg in enumerate(offer_configs):
                if offer_id in suppressed_indices:
                    logger.info(
                        f"Offer {offer_id}: suppressed by dual-offer auto-split "
                        f"(dominated by the companion offer across the usable range)"
                    )
                    continue
                cjfee_str, rand_txfee, numeric_cjfee = randomized_fees[offer_id]
                min_override, max_override = size_overrides.get(offer_id, (None, None))
                offer = self._create_single_offer(
                    offer_id=offer_id,
                    offer_cfg=offer_cfg,
                    max_balance=max_balance,
                    fidelity_bond_value=fidelity_bond_value,
                    cjfee_str=cjfee_str,
                    randomized_txfee=rand_txfee,
                    numeric_cjfee=numeric_cjfee,
                    min_size_override=min_override,
                    max_size_override=max_override,
                )
                if offer:
                    offers.append(offer)

            if not offers:
                logger.warning("No valid offers could be created")
                return []

            logger.info(f"Created {len(offers)} offer(s)")
            return offers

        except Exception as e:
            logger.error(f"Failed to create offers: {e}")
            return []

    def _randomize_offer_fees(
        self,
        offer_cfg: OfferConfig,
    ) -> tuple[str, int, float] | None:
        """Randomize the fees for a single offer configuration.

        Returns ``(cjfee_str, randomized_txfee, numeric_cjfee)`` where:

        - ``cjfee_str`` is the wire-format CJ fee string.
        - ``randomized_txfee`` is the randomized tx-fee contribution in sats.
        - ``numeric_cjfee`` is a float representation of the CJ fee used for
          the intersection calculation: the randomized relative fee (as a
          fraction) for relative offers, or the randomized absolute fee (in
          sats, *without* the txfee component) for absolute offers.

        Returns ``None`` if the config is invalid (e.g. non-positive relative
        fee).
        """
        randomized_txfee = int(
            _randomize(offer_cfg.tx_fee_contribution, offer_cfg.txfee_contribution_factor, low=0)
        )

        if offer_cfg.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE):
            cj_fee_float = float(offer_cfg.cj_fee_relative)
            if cj_fee_float <= 0:
                logger.error(f"Invalid cj_fee_relative: {offer_cfg.cj_fee_relative}. Must be > 0.")
                return None
            randomized_cj_fee_float = _randomize(cj_fee_float, offer_cfg.cjfee_factor)
            if randomized_cj_fee_float <= 0:
                randomized_cj_fee_float = cj_fee_float
            cjfee_str = _format_relative_cjfee(randomized_cj_fee_float)
            return cjfee_str, randomized_txfee, randomized_cj_fee_float
        else:
            # Absolute offer: randomize the CJ fee and add the txfee
            # contribution for the wire value, but keep them separate so the
            # intersection math can use the pure CJ fee.
            randomized_cj_fee_int = int(
                _randomize(offer_cfg.cj_fee_absolute, offer_cfg.cjfee_factor)
            )
            if randomized_cj_fee_int < 0:
                randomized_cj_fee_int = 0
            cjfee_str = str(randomized_cj_fee_int + randomized_txfee)
            return cjfee_str, randomized_txfee, float(randomized_cj_fee_int)

    def _compute_dual_offer_size_overrides(
        self,
        offer_configs: list[OfferConfig],
        randomized_fees: list[tuple[str, int, float]],
        max_balance: int,
    ) -> tuple[dict[int, tuple[int | None, int | None]], set[int]]:
        """Compute per-offer size-range overrides for dual rel+abs offers.

        The intersection is computed from the *randomized* fees so that the
        advertised size boundary does not leak information about the
        unrandomized fee configuration.

        Returns a tuple ``(overrides, suppressed)`` where:

        - ``overrides`` maps offer index to ``(min_size_override,
          max_size_override)``.  When the maker advertises exactly one
          relative offer and one absolute offer, the absolute offer is
          capped at the fee intersection
          ``x = randomized_abs_fee / randomized_rel_fee`` and the
          relative offer is floored at the same point so the two offers
          cover disjoint, contiguous size ranges:

          * abs offer: ``[cfg.min_size, intersection]``
          * rel offer: ``[intersection, max_available]``

        - ``suppressed`` is the set of offer indices that the auto-split
          has rendered fully dominated and that the caller must skip.

        ``max_balance`` is the gross mixdepth balance.  The actual ceiling
        that :meth:`_create_single_offer` will enforce as ``max_available``
        (``max_balance`` minus the dust threshold and the rel offer's
        randomized tx-fee contribution) is recomputed here so the
        intersection check uses the same usable balance.  Otherwise an
        intersection falling in the band ``(max_available, max_balance]``
        would be treated as "inside the usable range" here but rejected
        later as "min_size > max_available", producing a misleading
        "Insufficient balance" warning.

        Returns ``({}, set())`` for any non-dual configuration (single
        offer, two same-type offers, three or more offers, etc.) so
        existing behaviour is preserved.
        """
        empty: tuple[dict[int, tuple[int | None, int | None]], set[int]] = ({}, set())
        if len(offer_configs) != 2:
            return empty

        # Find which offer is relative and which is absolute.
        rel_idx: int | None = None
        abs_idx: int | None = None
        for idx, cfg in enumerate(offer_configs):
            if cfg.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE):
                if rel_idx is not None:
                    return empty  # two relative offers -> not a dual rel+abs pair
                rel_idx = idx
            elif cfg.offer_type in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE):
                if abs_idx is not None:
                    return empty  # two absolute offers
                abs_idx = idx
            else:  # pragma: no cover - guarded by OfferType enum
                return empty

        if rel_idx is None or abs_idx is None:
            return empty

        rel_cfg = offer_configs[rel_idx]
        abs_cfg = offer_configs[abs_idx]

        # Use the already-randomized numeric fees for the intersection so the
        # boundary does not reveal the unrandomized configuration.
        randomized_rel_fee: float = randomized_fees[rel_idx][2]
        randomized_abs_fee: float = randomized_fees[abs_idx][2]

        if randomized_rel_fee <= 0 or randomized_abs_fee <= 0:
            # Pathological values (randomized into non-positive territory or
            # configured as zero); skip the auto-split.
            return empty

        intersection = int(randomized_abs_fee / randomized_rel_fee)

        # Lower floor for the abs offer is its own configured min_size.
        abs_min = abs_cfg.min_size
        # Upper ceiling for the rel offer is the wallet-derived max_available
        # for that offer (i.e. after subtracting the dust threshold and the
        # rel offer's randomized tx-fee contribution).  Using the gross
        # ``max_balance`` here would let an intersection fall in the band
        # ``(max_available, max_balance]`` and produce an unfillable rel
        # offer with ``min_size > max_available``.
        rel_randomized_txfee = randomized_fees[rel_idx][1]
        rel_max_ceiling = max_balance - max(self.config.dust_threshold, rel_randomized_txfee)

        overrides: dict[int, tuple[int | None, int | None]] = {}
        suppressed: set[int] = set()

        if intersection <= abs_min:
            # The relative offer is cheaper everywhere above ``abs_min``;
            # the absolute offer would never undercut it.  Drop the abs
            # offer entirely so the rel offer covers the full range.
            logger.info(
                f"Dual-offer auto-split: intersection ({intersection} sats) "
                f"is at or below abs.min_size ({abs_min} sats); "
                f"abs offer suppressed, rel offer covers "
                f"[{max(rel_cfg.min_size, abs_min)}, {rel_max_ceiling}]"
            )
            suppressed.add(abs_idx)
            overrides[rel_idx] = (max(rel_cfg.min_size, abs_min), None)
            return overrides, suppressed

        if intersection >= rel_max_ceiling:
            # The absolute offer is cheaper across the entire usable range;
            # the relative offer would never beat it.  Drop the rel offer.
            logger.info(
                f"Dual-offer auto-split: intersection ({intersection} sats) "
                f"is at or above the usable balance ({rel_max_ceiling} sats, "
                f"gross={max_balance}); rel offer suppressed, abs offer "
                f"covers [{abs_min}, {rel_max_ceiling}]"
            )
            suppressed.add(rel_idx)
            overrides[abs_idx] = (abs_min, rel_max_ceiling)
            return overrides, suppressed

        # Standard case: the intersection sits strictly inside the usable
        # range, so each offer covers one side of it.
        overrides[abs_idx] = (abs_min, intersection)
        overrides[rel_idx] = (intersection, None)
        logger.info(
            f"Dual-offer auto-split at CJ amount {intersection} sats "
            f"(randomized abs={randomized_abs_fee} sats / randomized rel={randomized_rel_fee}): "
            f"abs offer covers [{abs_min}, {intersection}], "
            f"rel offer covers [{intersection}, {rel_max_ceiling}]"
        )
        return overrides, suppressed

    def _create_single_offer(
        self,
        offer_id: int,
        offer_cfg: OfferConfig,
        max_balance: int,
        fidelity_bond_value: int,
        cjfee_str: str,
        randomized_txfee: int,
        numeric_cjfee: float,
        min_size_override: int | None = None,
        max_size_override: int | None = None,
    ) -> Offer | None:
        """
        Create a single offer from pre-randomized fees and size bounds.

        Args:
            offer_id: Unique offer ID (0, 1, 2, ...)
            offer_cfg: Offer configuration
            max_balance: Maximum available balance
            fidelity_bond_value: Fidelity bond value to attach
            cjfee_str: Pre-randomized wire-format CJ fee string.
            randomized_txfee: Pre-randomized tx-fee contribution in sats.
            numeric_cjfee: Numeric CJ fee (relative fraction or absolute sats)
                used for the profitability floor calculation.
            min_size_override: Floor for min_size from the dual-offer
                intersection split (pins the seam; no size randomization
                applied to this boundary).
            max_size_override: Ceiling for max_size from the dual-offer
                intersection split (pins the seam; no size randomization
                applied to this boundary).

        Returns:
            Offer object or None if creation failed
        """
        try:
            if not cjfee_str:
                # Sentinel from an invalid config recorded in _randomize_offer_fees.
                logger.error(f"Offer {offer_id}: invalid fee config, skipping")
                return None

            # Reserve dust threshold + randomized tx fee contribution.
            max_available = max_balance - max(self.config.dust_threshold, randomized_txfee)
            # Apply dual-offer ceiling (caps the abs offer at the intersection).
            if max_size_override is not None:
                max_available = min(max_available, max_size_override)

            effective_min_size = offer_cfg.min_size
            if min_size_override is not None:
                effective_min_size = max(effective_min_size, min_size_override)

            if max_available <= effective_min_size:
                logger.warning(
                    f"Offer {offer_id}: Insufficient balance: "
                    f"max_available={max_available} <= min_size={effective_min_size} "
                    f"(max_balance={max_balance}, dust_threshold={self.config.dust_threshold})"
                )
                return None

            # Determine base_min_size: for relative offers enforce a
            # profitability floor using the already-randomized fee values.
            if offer_cfg.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE):
                min_size_for_profit = (
                    int(1.5 * randomized_txfee / numeric_cjfee) if numeric_cjfee > 0 else 0
                )
                base_min_size = max(min_size_for_profit, effective_min_size)
            else:
                base_min_size = effective_min_size

            # Randomize min_size (clamped to dust threshold).  The dual-offer
            # auto-split pins the boundary at the intersection; no randomization
            # is applied to that edge so the two offers stay seamless.
            if min_size_override is not None:
                randomized_min_size = max(int(effective_min_size), DUST_THRESHOLD)
            else:
                randomized_min_size = int(
                    _randomize(base_min_size, offer_cfg.size_factor, low=DUST_THRESHOLD)
                )

            # Randomize max_size downward from available balance.  The
            # dual-offer auto-split pins this edge too.
            if max_size_override is not None:
                randomized_max_size = int(max_available)
            elif offer_cfg.size_factor > 0 and max_available > 0:
                randomized_max_size = int(
                    random.uniform(max_available * (1.0 - offer_cfg.size_factor), max_available)
                )
            else:
                randomized_max_size = max_available

            if randomized_max_size <= randomized_min_size:
                logger.warning(
                    f"Offer {offer_id}: Randomized maxsize too small: "
                    f"max_size={randomized_max_size} <= min_size={randomized_min_size} "
                    f"(max_available={max_available})"
                )
                return None

            offer = Offer(
                counterparty=self.maker_nick,
                oid=offer_id,
                ordertype=offer_cfg.offer_type,
                minsize=randomized_min_size,
                maxsize=randomized_max_size,
                txfee=randomized_txfee,
                cjfee=cjfee_str,
                fidelity_bond_value=fidelity_bond_value,
            )

            logger.info(
                f"Created offer {offer_id}: type={offer.ordertype.value}, "
                f"size={randomized_min_size}-{randomized_max_size} "
                f"(max_available={max_available}), "
                f"cjfee={cjfee_str}, txfee={randomized_txfee}, "
                f"bond_value={fidelity_bond_value}"
            )

            return offer

        except Exception as e:
            logger.error(f"Failed to create offer {offer_id}: {e}")
            return None

    def validate_offer_fill(self, offer: Offer, amount: int) -> tuple[bool, str]:
        """
        Validate a fill request for an offer.

        Args:
            offer: The offer being filled
            amount: Requested amount

        Returns:
            (is_valid, error_message)
        """
        if amount < offer.minsize:
            return False, f"Amount {amount} below minimum {offer.minsize}"

        if amount > offer.maxsize:
            return False, f"Amount {amount} above maximum {offer.maxsize}"

        return True, ""

    def get_offer_by_id(self, offers: list[Offer], offer_id: int) -> Offer | None:
        """
        Find an offer by its ID.

        Args:
            offers: List of current offers
            offer_id: Offer ID to find

        Returns:
            Offer with matching oid, or None if not found
        """
        for offer in offers:
            if offer.oid == offer_id:
                return offer
        return None
