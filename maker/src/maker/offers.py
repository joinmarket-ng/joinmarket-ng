"""
Offer management for makers.

Creates and manages liquidity offers based on wallet balance and configuration.
Supports multiple simultaneous offers with different fee structures (relative/absolute).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

from jmcore.constants import DUST_THRESHOLD
from jmcore.models import (
    MAX_RELATIVE_FEE_EXPONENT,
    MAX_RELATIVE_FEE_PRECISION,
    Offer,
    is_absolute_offer_type,
    offer_output_script_type,
)
from jmcore.randomness import secure_random
from jmwallet.wallet.service import WalletService
from loguru import logger

from maker.config import MakerConfig, OfferConfig
from maker.fidelity import get_best_fidelity_bond
from maker.offer_math import max_fillable_cj_amount

if TYPE_CHECKING:
    # A maker without a prepared channel buyout never imports jmswap at runtime.
    from jmswap.coinjoin_funding import ChannelBuyout


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
        result = secure_random.uniform(value * (1.0 - factor), value * (1.0 + factor))
    if low is not None and result < low:
        return float(low)
    return result


def _format_relative_cjfee(value: Decimal) -> str:
    """Format a relative CJ fee without scientific notation or trailing zeros.

    Decimal arithmetic retains configured precision and avoids converting tiny
    valid fees into zero before they are advertised.
    """
    formatted = format(value, "f")
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted


def _quantize_relative_cjfee(value: Decimal, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    """Limit a relative fee to the shared wire validator's Decimal bounds."""
    if value.is_zero():
        return value

    exponent = max(
        value.adjusted() - MAX_RELATIVE_FEE_PRECISION + 1,
        -MAX_RELATIVE_FEE_EXPONENT,
    )
    return value.quantize(Decimal(1).scaleb(exponent), rounding=rounding)


class OfferManager:
    """
    Creates and manages offers for the maker bot.

    Supports creating multiple offers simultaneously, each with a unique offer ID.
    This allows makers to advertise both relative and absolute fee offers at the same time.
    """

    def __init__(
        self,
        wallet: WalletService,
        config: MakerConfig,
        maker_nick: str,
        buyout: ChannelBuyout | None = None,
        buyout_mixdepth: int = -1,
    ):
        self.wallet = wallet
        self.config = config
        self.maker_nick = maker_nick
        # Optional prepared channel buyout and the wallet mixdepth its binding
        # was validated against at startup (see MakerBot). When set, offers
        # describe that one mixdepth funded by channel value; when unset the
        # maker advertises ordinary liquidity exactly as before.
        self.buyout = buyout
        self.buyout_mixdepth = buyout_mixdepth
        self._validate_offer_types_match_wallet()
        # Max mixdepth balance the most recent create_offers() result was built
        # from. The bot compares fresh wallet state against this to decide when
        # announced offers are stale; None until offers have been created.
        # create_offers() records it optimistically: a caller that does not
        # adopt (announce) the returned offers must restore the previous value,
        # otherwise the un-announced balance would look like the announced one
        # and no rescan would ever retry.
        self.offer_balance: int | None = None

    async def get_mixdepth_offer_balances(self) -> dict[int, int]:
        """Per-mixdepth balance available for offers under the maker's policy.

        Excludes fidelity bonds, unconfirmed coins below ``min_confirmations``,
        restricted mixdepth 0 coins, and inputs locked by in-flight rounds.

        With a prepared channel buyout the result describes the buyout instead
        (see :meth:`_buyout_offer_balances`), because a bound round can only be
        funded from the one mixdepth the buyout names.
        """
        locked_outpoints = self.wallet.get_locked_input_outpoints()
        restrict_md0 = not self.config.allow_mixdepth_zero_merge
        md0_mergeable_outpoints = (
            await self.wallet.get_maker_rotation_lineage_outpoints() if restrict_md0 else None
        )
        balances: dict[int, int] = {}
        for mixdepth in range(self.wallet.mixdepth_count):
            balances[mixdepth] = await self.wallet.get_balance_for_offers(
                mixdepth,
                min_confirmations=self.config.min_confirmations,
                restrict_md0=restrict_md0,
                md0_mergeable_outpoints=md0_mergeable_outpoints,
                exclude=locked_outpoints,
            )
        if self.buyout is None:
            return balances
        return self._buyout_offer_balances(self.buyout, balances)

    def _buyout_offer_balances(
        self, buyout: ChannelBuyout, balances: dict[int, int]
    ) -> dict[int, int]:
        """Replace ordinary liquidity with what the prepared buyout can fund.

        Only the bound mixdepth may participate: spending another one would
        link funds the buyout binding never authorized, and
        ``CoinJoinSession`` refuses such a round anyway. The bound mixdepth
        advertises its ordinary balance plus the channel value that is not
        reserved for the escrow change output, which is exactly the budget
        fill-time selection will accept.

        A buyout whose durable record is no longer ``ACCEPTED`` (it was
        reserved for a round, canceled or settled) or whose record no longer
        matches this runtime's binding withdraws *all* liquidity. Falling back
        to ordinary funds would serve a different round than the operator
        prepared, so an unusable buyout means no offers at all.
        """
        from jmswap.buyout_signing import matches_runtime_binding

        withdrawn: dict[int, int] = dict.fromkeys(balances, 0)
        try:
            record = buyout.buyer.store.get(buyout.session_id)
        except Exception as e:
            logger.warning("Withdrawing offers: the prepared buyout record is unreadable")
            logger.bind(sensitive=True).warning(f"Prepared buyout record is unreadable: {e}")
            return withdrawn
        if record.role != "buyer" or record.state != "ACCEPTED":
            logger.info(
                f"Withdrawing offers: the prepared buyout is no longer accepted "
                f"(state={record.state})"
            )
            return withdrawn
        if not matches_runtime_binding(record, buyout.buyer.runtime_binding):
            logger.warning("Withdrawing offers: the prepared buyout is not bound to this runtime")
            return withdrawn

        wallet_balance = balances.get(self.buyout_mixdepth, 0)
        if wallet_balance <= 0:
            # A buyout always needs an ordinary wallet input for the !ioauth
            # ownership proof, so channel value alone is not fillable.
            logger.info(
                "Withdrawing offers: the mixdepth bound to the prepared buyout has no "
                "ordinary liquidity"
            )
            return withdrawn
        budget = wallet_balance + buyout.total_value - buyout.minimum_change
        if budget <= 0:
            logger.info("Withdrawing offers: the prepared buyout leaves no fillable budget")
            return withdrawn
        withdrawn[self.buyout_mixdepth] = budget
        return withdrawn

    async def get_max_offer_balance(self) -> int:
        """Largest single-mixdepth balance available for offers (0 when none)."""
        return max((await self.get_mixdepth_offer_balances()).values(), default=0)

    def _validate_offer_types_match_wallet(self) -> None:
        """Reject a configured offer family the wallet cannot serve at all.

        A rigid JMP-0010 pit requires the offer family's script type
        (sw0 -> p2wpkh, tr0 -> p2tr) to match the wallet's address_type;
        CoinJoinSession.__init__ already enforces this per round, but only
        at !fill time -- a p2wpkh-wallet maker configured to announce tr0
        offers would advertise them, get filled, and only then discover the
        mismatch and refuse, wasting a round-trip and looking like a flaky
        maker to the taker. Checking here at OfferManager construction
        (maker startup) catches the misconfiguration immediately instead.
        """
        wallet_type = getattr(self.wallet, "address_type", None)
        if wallet_type not in ("p2wpkh", "p2tr"):
            return
        for oc in self.config.get_effective_offer_configs():
            pit_type = offer_output_script_type(oc.offer_type)
            if pit_type != wallet_type:
                raise ValueError(
                    f"Configured offer_type {oc.offer_type.value!r} implies a "
                    f"{pit_type!r} pit but the wallet is {wallet_type!r}; a rigid "
                    f"JMP-0010 pit requires them to match (configure a "
                    f"{wallet_type!r} offer family instead)."
                )

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
            balances = await self.get_mixdepth_offer_balances()
            # Record what these offers are built from, even when the result is
            # empty, so a later balance change is detected and re-evaluated.
            self.offer_balance = max(balances.values(), default=0)

            available_mixdepths = {md: bal for md, bal in balances.items() if bal > 0}

            if not available_mixdepths:
                logger.warning("No mixdepth with positive balance")
                return []

            logger.bind(sensitive=True).debug(
                f"Mixdepth balances (excluding fidelity bonds): {balances}"
            )

            max_mixdepth = max(available_mixdepths, key=lambda md: available_mixdepths[md])
            max_balance = available_mixdepths[max_mixdepth]
            logger.bind(sensitive=True).info(
                f"Selected mixdepth {max_mixdepth} with balance {max_balance} sats"
            )

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
                logger.bind(sensitive=True).info(
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
            # Nothing was announced from this attempt; force the next rescan
            # to try again rather than treating the announced offers as current.
            self.offer_balance = None
            logger.error("Failed to create offers")
            logger.bind(sensitive=True).error(f"Failed to create offers: {e}")
            raise

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

        if not is_absolute_offer_type(offer_cfg.offer_type):
            cj_fee = Decimal(offer_cfg.cj_fee_relative)
            if cj_fee <= 0:
                logger.error(f"Invalid cj_fee_relative: {offer_cfg.cj_fee_relative}. Must be > 0.")
                return None
            factor = Decimal(str(offer_cfg.cjfee_factor))
            lower = cj_fee
            upper = cj_fee
            if factor <= 0:
                randomized_cj_fee = cj_fee
            else:
                lower = cj_fee * (Decimal(1) - factor)
                upper = cj_fee * (Decimal(1) + factor)
                randomized_cj_fee = lower + (upper - lower) * Decimal(str(secure_random.random()))
                # A factor of one has a zero lower endpoint. ``random()`` can
                # theoretically return zero, which is invalid on the wire.
                if randomized_cj_fee <= 0 or randomized_cj_fee >= 1:
                    randomized_cj_fee = cj_fee

            randomized_cj_fee = _quantize_relative_cjfee(randomized_cj_fee)

            # Quantization can move a sampled value outside the configured
            # interval. Round endpoints inward before clamping so advertised
            # fees remain within the configured randomization bounds.
            minimum_positive_fee = Decimal(1).scaleb(-MAX_RELATIVE_FEE_EXPONENT)
            lower_bound = max(
                _quantize_relative_cjfee(lower, ROUND_CEILING),
                minimum_positive_fee,
            )
            upper_bound = _quantize_relative_cjfee(upper, ROUND_FLOOR)
            if lower_bound <= upper_bound:
                randomized_cj_fee = min(max(randomized_cj_fee, lower_bound), upper_bound)
            else:
                randomized_cj_fee = _quantize_relative_cjfee(cj_fee)

            cjfee_str = _format_relative_cjfee(randomized_cj_fee)
            return cjfee_str, randomized_txfee, float(randomized_cj_fee)
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

        ``max_balance`` is the gross mixdepth balance. The exact ceiling that
        :meth:`_create_single_offer` will enforce is recomputed here from the
        randomized relative-offer terms so split suppression cannot create an
        unfillable relative range.

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
            if not is_absolute_offer_type(cfg.offer_type):
                if rel_idx is not None:
                    return empty  # two relative offers -> not a dual rel+abs pair
                rel_idx = idx
            else:
                if abs_idx is not None:
                    return empty  # two absolute offers
                abs_idx = idx

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
        # Use the same exact requirement calculation as fill-time selection.
        rel_randomized_txfee = randomized_fees[rel_idx][1]
        rel_offer = self._offer_terms(
            rel_idx,
            rel_cfg,
            randomized_fees[rel_idx][0],
            rel_randomized_txfee,
        )
        rel_max_ceiling = max_fillable_cj_amount(rel_offer, max_balance)
        if rel_max_ceiling is None:
            rel_max_ceiling = -1

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
            logger.bind(sensitive=True).info(
                f"Dual-offer auto-split: intersection ({intersection} sats) "
                f"is at or above the usable balance ({rel_max_ceiling} sats, "
                f"gross={max_balance}); rel offer suppressed, abs offer covers "
                "its full fillable range"
            )
            suppressed.add(rel_idx)
            overrides[abs_idx] = (abs_min, None)
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

    def _offer_terms(
        self,
        offer_id: int,
        offer_cfg: OfferConfig,
        cjfee: str,
        txfee: int,
        fidelity_bond_value: int = 0,
    ) -> Offer:
        """Build an Offer carrying terms used by pure liquidity calculations."""
        return Offer(
            counterparty=self.maker_nick,
            oid=offer_id,
            ordertype=offer_cfg.offer_type,
            minsize=0,
            maxsize=0,
            txfee=txfee,
            cjfee=cjfee,
            fidelity_bond_value=fidelity_bond_value,
        )

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

            terms = self._offer_terms(
                offer_id,
                offer_cfg,
                cjfee_str,
                randomized_txfee,
                fidelity_bond_value,
            )
            max_available = max_fillable_cj_amount(terms, max_balance)
            if max_available is None:
                logger.warning(f"Offer {offer_id}: insufficient fillable liquidity")
                logger.bind(sensitive=True).warning(
                    f"Offer {offer_id}: Insufficient balance for mandatory maker change "
                    f"(max_balance={max_balance})"
                )
                return None
            # Apply dual-offer ceiling (caps the abs offer at the intersection).
            if max_size_override is not None:
                max_available = min(max_available, max_size_override)

            effective_min_size = offer_cfg.min_size
            if min_size_override is not None:
                effective_min_size = max(effective_min_size, min_size_override)

            if max_available <= effective_min_size:
                logger.warning(f"Offer {offer_id}: insufficient fillable liquidity")
                logger.bind(sensitive=True).warning(
                    f"Offer {offer_id}: Insufficient balance: "
                    f"max_available={max_available} <= min_size={effective_min_size} "
                    f"(max_balance={max_balance})"
                )
                return None

            # Determine base_min_size: for relative offers enforce a
            # profitability floor using the already-randomized fee values.
            if not is_absolute_offer_type(offer_cfg.offer_type):
                min_size_for_profit = (
                    int(1.5 * randomized_txfee / numeric_cjfee) if numeric_cjfee > 0 else 0
                )
                base_min_size = max(min_size_for_profit, effective_min_size)
            else:
                base_min_size = effective_min_size

            # Keep the minimum stable so common configured values remain shared
            # across makers. Preserve the dual-offer seam and the dust floor.
            if min_size_override is not None:
                min_size = max(int(effective_min_size), DUST_THRESHOLD)
            else:
                min_size = max(base_min_size, DUST_THRESHOLD)

            # Randomize max_size downward from available balance.  The
            # dual-offer auto-split pins this edge too.
            if max_size_override is not None:
                randomized_max_size = int(max_available)
            elif offer_cfg.size_factor > 0 and max_available > 0:
                randomized_max_size = int(
                    secure_random.uniform(
                        max_available * (1.0 - offer_cfg.size_factor), max_available
                    )
                )
            else:
                randomized_max_size = max_available

            if randomized_max_size <= min_size:
                logger.warning(
                    f"Offer {offer_id}: Randomized maxsize too small: "
                    f"max_size={randomized_max_size} <= min_size={min_size} "
                    f"(max_available={max_available})"
                )
                return None

            offer = terms.model_copy(update={"minsize": min_size, "maxsize": randomized_max_size})

            logger.info(
                f"Created offer {offer_id}: type={offer.ordertype.value}, "
                f"size={min_size}-{randomized_max_size} "
                f"(max_available={max_available}), "
                f"cjfee={cjfee_str}, txfee={randomized_txfee}, "
                f"bond_value={fidelity_bond_value}"
            )

            return offer

        except Exception as e:
            logger.error(f"Failed to create offer {offer_id}")
            logger.bind(sensitive=True).error(f"Failed to create offer {offer_id}: {e}")
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
