"""
Coin selection algorithms for wallet spending.

Provides UTXO selection strategies for CoinJoin transactions and sweeps.
"""

from __future__ import annotations

from jmwallet.wallet.models import UTXOInfo


class CoinSelectionMixin:
    """Mixin providing coin selection capabilities.

    Expects the host class to provide ``utxo_cache`` (dict[int, list[UTXOInfo]]).
    """

    # Declared for mypy -- actually set by the host class __init__
    utxo_cache: dict[int, list[UTXOInfo]]

    def select_utxos(
        self,
        mixdepth: int,
        target_amount: int,
        min_confirmations: int = 1,
        include_utxos: list[UTXOInfo] | None = None,
        include_fidelity_bonds: bool = False,
        *,
        restrict_md0: bool = True,
    ) -> list[UTXOInfo]:
        """
        Select UTXOs for spending from a mixdepth.
        Uses simple greedy selection strategy.

        Args:
            mixdepth: Mixdepth to select from
            target_amount: Target amount in satoshis
            min_confirmations: Minimum confirmations required
            include_utxos: List of UTXOs that MUST be included in selection
            include_fidelity_bonds: If True, include fidelity bond UTXOs in automatic
                                    selection. Defaults to False to prevent accidentally
                                    spending bonds.
            restrict_md0: When True (default), mixdepth 0 non-CJ-output UTXOs are
                          restricted to a single UTXO.  CoinJoin outputs (label
                          ``"cj-out"``) are exempt and can be merged.  Set to False
                          to disable the restriction.
        """
        utxos = self.utxo_cache.get(mixdepth, [])

        eligible = [utxo for utxo in utxos if utxo.confirmations >= min_confirmations]

        # Filter out frozen UTXOs (never auto-selected)
        eligible = [utxo for utxo in eligible if not utxo.frozen]

        # Filter out fidelity bond UTXOs by default
        if not include_fidelity_bonds:
            eligible = [utxo for utxo in eligible if not utxo.is_fidelity_bond]

        # Filter out included UTXOs from eligible pool to avoid duplicates
        included_txid_vout = set()
        if include_utxos:
            included_txid_vout = {(u.txid, u.vout) for u in include_utxos}
            eligible = [u for u in eligible if (u.txid, u.vout) not in included_txid_vout]

        eligible.sort(key=lambda u: u.value, reverse=True)

        # Mixdepth 0 restriction: avoid merging non-CoinJoin UTXOs to prevent
        # linking deposits/fidelity bonds.  CoinJoin outputs (label "cj-out")
        # are exempt because they already have CoinJoin privacy.
        # When restrict_md0 is False the restriction is skipped entirely.
        if mixdepth == 0 and restrict_md0:
            # Start with mandatory UTXOs if any
            selected: list[UTXOInfo] = []
            total = 0
            if include_utxos:
                for utxo in include_utxos:
                    selected.append(utxo)
                    total += utxo.value
            if total >= target_amount:
                return selected

            # Split eligible UTXOs into CJ outputs (mergeable) and others (single only)
            cj_outs = [u for u in eligible if u.label == "cj-out"]
            non_cj = [u for u in eligible if u.label != "cj-out"]

            remaining = target_amount - total

            # Try CJ output pool first (can be merged safely)
            cj_pool_value = sum(u.value for u in cj_outs)
            if cj_pool_value >= remaining:
                for utxo in cj_outs:
                    selected.append(utxo)
                    total += utxo.value
                    if total >= target_amount:
                        return selected

            # Try single largest non-CJ UTXO
            if non_cj and non_cj[0].value >= remaining:
                selected.append(non_cj[0])
                return selected

            if not eligible:
                # Provide a helpful message when unconfirmed funds exist
                all_utxos = self.utxo_cache.get(mixdepth, [])
                unconfirmed_total = sum(
                    u.value
                    for u in all_utxos
                    if not u.frozen
                    and not u.is_fidelity_bond
                    and u.confirmations < min_confirmations
                )
                if unconfirmed_total > 0:
                    raise ValueError(
                        f"Insufficient confirmed funds: no eligible UTXOs in mixdepth 0 "
                        f"({unconfirmed_total:,} sats are unconfirmed and require "
                        f"{min_confirmations} confirmation(s) before use)"
                    )
                raise ValueError("Insufficient funds: no eligible UTXOs in mixdepth 0")

            largest_non_cj = non_cj[0].value if non_cj else 0
            raise ValueError(
                f"Insufficient funds: CJ-output pool has {cj_pool_value}, "
                f"largest non-CJ UTXO has {largest_non_cj}, "
                f"need {remaining}. "
                f"Cannot merge non-CJ md0 UTXOs for privacy reasons."
            )

        selected = []
        total = 0

        # Add mandatory UTXOs first
        if include_utxos:
            for utxo in include_utxos:
                selected.append(utxo)
                total += utxo.value

        if total >= target_amount:
            # Already enough with mandatory UTXOs
            return selected

        for utxo in eligible:
            selected.append(utxo)
            total += utxo.value
            if total >= target_amount:
                break

        if total < target_amount:
            # Compute total balance including unconfirmed UTXOs to give a helpful diagnosis
            all_utxos = self.utxo_cache.get(mixdepth, [])
            unconfirmed_total = sum(
                u.value for u in all_utxos if not u.frozen and u.confirmations < min_confirmations
            )
            if unconfirmed_total > 0:
                raise ValueError(
                    f"Insufficient confirmed funds: need {target_amount:,} sats, "
                    f"have {total:,} confirmed sats "
                    f"({unconfirmed_total:,} sats are unconfirmed and require "
                    f"{min_confirmations} confirmation(s) before use)"
                )
            raise ValueError(
                f"Insufficient funds: need {target_amount:,} sats, have {total:,} sats"
            )

        return selected

    def get_all_utxos(
        self,
        mixdepth: int,
        min_confirmations: int = 1,
        include_fidelity_bonds: bool = False,
    ) -> list[UTXOInfo]:
        """
        Get all UTXOs from a mixdepth for sweep operations.

        Unlike select_utxos(), this returns ALL eligible UTXOs regardless of
        target amount. Used for sweep mode to ensure no change output.

        Args:
            mixdepth: Mixdepth to get UTXOs from
            min_confirmations: Minimum confirmations required
            include_fidelity_bonds: If True, include fidelity bond UTXOs.
                                    Defaults to False to prevent accidentally
                                    spending bonds in sweeps.

        Returns:
            List of all eligible UTXOs in the mixdepth
        """
        utxos = self.utxo_cache.get(mixdepth, [])
        eligible = [utxo for utxo in utxos if utxo.confirmations >= min_confirmations]
        # Filter out frozen UTXOs (never auto-selected)
        eligible = [utxo for utxo in eligible if not utxo.frozen]
        if not include_fidelity_bonds:
            eligible = [utxo for utxo in eligible if not utxo.is_fidelity_bond]
        return eligible

    def select_utxos_with_merge(
        self,
        mixdepth: int,
        target_amount: int,
        min_confirmations: int = 1,
        merge_algorithm: str = "default",
        include_fidelity_bonds: bool = False,
        *,
        restrict_md0: bool = True,
    ) -> list[UTXOInfo]:
        """
        Select UTXOs with merge algorithm for maker UTXO consolidation.

        Unlike regular select_utxos(), this method can select MORE UTXOs than
        strictly necessary based on the merge algorithm. Since takers pay tx fees,
        makers can add extra inputs "for free" to consolidate their UTXOs.

        Args:
            mixdepth: Mixdepth to select from
            target_amount: Minimum target amount in satoshis
            min_confirmations: Minimum confirmations required
            merge_algorithm: Selection strategy:
                - "default": Minimum UTXOs needed (same as select_utxos)
                - "gradual": +1 additional UTXO beyond minimum
                - "greedy": ALL eligible UTXOs from the mixdepth
                - "random": +0 to +2 additional UTXOs randomly
            include_fidelity_bonds: If True, include fidelity bond UTXOs.
                                    Defaults to False since they should never be
                                    automatically spent in CoinJoins.
            restrict_md0: When True (default), mixdepth 0 non-CJ-output UTXOs are
                          restricted to a single UTXO.  CoinJoin outputs (label
                          ``"cj-out"``) are exempt and can be merged.  Set to False
                          to disable the restriction.

        Returns:
            List of selected UTXOs

        Raises:
            ValueError: If insufficient funds
        """
        utxos = self.utxo_cache.get(mixdepth, [])
        eligible = [utxo for utxo in utxos if utxo.confirmations >= min_confirmations]

        # Filter out frozen UTXOs (never auto-selected)
        eligible = [utxo for utxo in eligible if not utxo.frozen]

        # Filter out fidelity bond UTXOs by default
        if not include_fidelity_bonds:
            eligible = [utxo for utxo in eligible if not utxo.is_fidelity_bond]

        # Sort by value descending for efficient selection
        eligible.sort(key=lambda u: u.value, reverse=True)

        if mixdepth == 0 and restrict_md0:
            if not eligible:
                raise ValueError("Insufficient funds: no eligible UTXOs in mixdepth 0")

            # CJ outputs can be merged; non-CJ outputs are single-UTXO only
            cj_outs = [u for u in eligible if u.label == "cj-out"]
            non_cj = [u for u in eligible if u.label != "cj-out"]

            cj_pool_value = sum(u.value for u in cj_outs)
            largest_non_cj = non_cj[0].value if non_cj else 0

            if cj_pool_value >= target_amount:
                # Select from CJ outputs (greedy by value, then apply merge)
                selected: list[UTXOInfo] = []
                total = 0
                for utxo in cj_outs:
                    selected.append(utxo)
                    total += utxo.value
                    if total >= target_amount:
                        break
                # Apply merge algorithm to remaining CJ outputs only
                min_count = len(selected)
                remaining_cj = cj_outs[min_count:]
                selected = self._apply_merge_extras(selected, remaining_cj, merge_algorithm)
                return selected
            elif largest_non_cj >= target_amount:
                return [non_cj[0]]
            else:
                raise ValueError(
                    f"Insufficient funds: CJ-output pool has {cj_pool_value}, "
                    f"largest non-CJ UTXO has {largest_non_cj}, "
                    f"need {target_amount}. "
                    f"Cannot merge non-CJ md0 UTXOs for privacy reasons."
                )

        # First, select minimum needed (greedy by value)
        selected = []
        total = 0

        for utxo in eligible:
            selected.append(utxo)
            total += utxo.value
            if total >= target_amount:
                break

        if total < target_amount:
            all_utxos = self.utxo_cache.get(mixdepth, [])
            unconfirmed_total = sum(
                u.value for u in all_utxos if not u.frozen and u.confirmations < min_confirmations
            )
            if unconfirmed_total > 0:
                raise ValueError(
                    f"Insufficient confirmed funds: need {target_amount:,} sats, "
                    f"have {total:,} confirmed sats "
                    f"({unconfirmed_total:,} sats are unconfirmed and require "
                    f"{min_confirmations} confirmation(s) before use)"
                )
            raise ValueError(
                f"Insufficient funds: need {target_amount:,} sats, have {total:,} sats"
            )

        # Record where minimum selection ends
        min_count = len(selected)

        # Get remaining eligible UTXOs not yet selected
        remaining = eligible[min_count:]

        # Apply merge algorithm to add additional UTXOs
        selected = self._apply_merge_extras(selected, remaining, merge_algorithm)

        return selected

    @staticmethod
    def _apply_merge_extras(
        selected: list[UTXOInfo],
        remaining: list[UTXOInfo],
        merge_algorithm: str,
    ) -> list[UTXOInfo]:
        """Apply merge algorithm to add extra UTXOs beyond the minimum selection.

        Args:
            selected: Already-selected UTXOs (minimum needed).
            remaining: Eligible UTXOs not yet selected, sorted by value descending.
            merge_algorithm: ``"default"`` | ``"gradual"`` | ``"greedy"`` | ``"random"``.

        Returns:
            Extended ``selected`` list (may be mutated in-place).
        """
        import random as rand_module

        if merge_algorithm == "greedy":
            # Add ALL remaining UTXOs
            selected.extend(remaining)
        elif merge_algorithm == "gradual" and remaining:
            # Add exactly 1 more UTXO (smallest to preserve larger ones)
            remaining_sorted = sorted(remaining, key=lambda u: u.value)
            selected.append(remaining_sorted[0])
        elif merge_algorithm == "random" and remaining:
            # Add 0-2 additional UTXOs randomly
            extra_count = rand_module.randint(0, min(2, len(remaining)))
            if extra_count > 0:
                # Prefer smaller UTXOs for consolidation
                remaining_sorted = sorted(remaining, key=lambda u: u.value)
                selected.extend(remaining_sorted[:extra_count])
        # "default" - no additional UTXOs

        return selected
