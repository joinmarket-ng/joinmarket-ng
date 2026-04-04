"""
Offer management for makers.

Creates and manages liquidity offers based on wallet balance and configuration.
Supports multiple simultaneous offers with different fee structures (relative/absolute).
"""

from __future__ import annotations

from jmcore.models import Offer, OfferType
from jmwallet.wallet.service import WalletService
from loguru import logger

from maker.config import MakerConfig, OfferConfig
from maker.fidelity import get_best_fidelity_bond


def _round_maxsize_to_power_of_2(value: int) -> int:
    """Round a satoshi amount down to the nearest power of 2.

    This prevents observers from tracking a maker through offer
    re-announcements by hiding exact balance changes.  Only balance
    shifts that cross a power-of-2 boundary produce a visible offer
    update, and even then only the bucket is revealed.

    Examples:
        150_000_000 (1.5 BTC) → 134_217_728 (≈1.34 BTC, 2^27)
         70_000_000 (0.7 BTC) →  67_108_864 (≈0.67 BTC, 2^26)
         10_000_000 (0.1 BTC) →   8_388_608 (≈0.08 BTC, 2^23)

    Args:
        value: Amount in satoshis (must be > 0)

    Returns:
        Largest power of 2 that is ≤ value, or 0 if value ≤ 0
    """
    if value <= 0:
        return 0
    return 1 << (value.bit_length() - 1)


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
        2. Calculate available amount (balance - dust - max_txfee)
        3. Create offer(s) with configured fee structure(s)
        4. Attach fidelity bond value if available

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

            # Get fidelity bond value if available (shared across all offers)
            fidelity_bond_value = 0
            bond = await get_best_fidelity_bond(self.wallet)
            if bond:
                fidelity_bond_value = bond.bond_value
                logger.info(
                    f"Fidelity bond found: {bond.txid}:{bond.vout} "
                    f"value={bond.value} sats, bond_value={bond.bond_value}"
                )

            # Create an offer for each configuration
            offers: list[Offer] = []
            for offer_id, offer_cfg in enumerate(offer_configs):
                offer = self._create_single_offer(
                    offer_id=offer_id,
                    offer_cfg=offer_cfg,
                    max_balance=max_balance,
                    fidelity_bond_value=fidelity_bond_value,
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

    def _create_single_offer(
        self,
        offer_id: int,
        offer_cfg: OfferConfig,
        max_balance: int,
        fidelity_bond_value: int,
    ) -> Offer | None:
        """
        Create a single offer from configuration.

        Args:
            offer_id: Unique offer ID (0, 1, 2, ...)
            offer_cfg: Offer configuration
            max_balance: Maximum available balance
            fidelity_bond_value: Fidelity bond value to attach

        Returns:
            Offer object or None if creation failed
        """
        try:
            # Reserve dust threshold + tx fee contribution
            max_available = max_balance - max(
                self.config.dust_threshold, offer_cfg.tx_fee_contribution
            )

            if max_available <= offer_cfg.min_size:
                logger.warning(
                    f"Offer {offer_id}: Insufficient balance: "
                    f"max_available={max_available} <= min_size={offer_cfg.min_size} "
                    f"(max_balance={max_balance}, dust_threshold={self.config.dust_threshold})"
                )
                return None

            # Calculate min_size based on offer type
            if offer_cfg.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE):
                cjfee = offer_cfg.cj_fee_relative

                # Validate cj_fee_relative to prevent division by zero
                cj_fee_float = float(offer_cfg.cj_fee_relative)
                if cj_fee_float <= 0:
                    logger.error(
                        f"Offer {offer_id}: Invalid cj_fee_relative: {offer_cfg.cj_fee_relative}. "
                        "Must be > 0 for relative offer types."
                    )
                    return None

                # Calculate minimum size for profitability
                min_size_for_profit = int(1.5 * offer_cfg.tx_fee_contribution / cj_fee_float)
                min_size = max(min_size_for_profit, offer_cfg.min_size)
            else:
                cjfee = str(offer_cfg.cj_fee_absolute)
                min_size = offer_cfg.min_size

            rounded_max = _round_maxsize_to_power_of_2(max_available)

            if rounded_max <= min_size:
                logger.warning(
                    f"Offer {offer_id}: Rounded maxsize too small: "
                    f"rounded_max={rounded_max} <= min_size={min_size} "
                    f"(exact max_available={max_available})"
                )
                return None

            offer = Offer(
                counterparty=self.maker_nick,
                oid=offer_id,
                ordertype=offer_cfg.offer_type,
                minsize=min_size,
                maxsize=rounded_max,
                txfee=offer_cfg.tx_fee_contribution,
                cjfee=cjfee,
                fidelity_bond_value=fidelity_bond_value,
            )

            logger.info(
                f"Created offer {offer_id}: type={offer.ordertype.value}, "
                f"size={min_size}-{rounded_max} (exact={max_available}), "
                f"cjfee={cjfee}, txfee={offer_cfg.tx_fee_contribution}, "
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
