"""
Coin selection algorithms for wallet spending.

Provides UTXO selection strategies for CoinJoin transactions and sweeps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from jmcore.bitcoin import estimate_vsize, get_address_type
from jmcore.randomness import secure_random

from jmwallet.wallet.models import UTXOInfo


class DirectSendSearchLimitError(ValueError):
    """Raised when bounded selection cannot prove the optimal input set."""


@dataclass
class DirectSendSelection:
    """Fee-aware result of privacy-preserving direct-send coin selection."""

    utxos: list[UTXOInfo]
    fee: int
    change_amount: int
    vsize: int


@dataclass(frozen=True)
class _DirectSendScriptGroup:
    """Atomic direct-send input group for one scriptPubKey."""

    scriptpubkey: str
    utxos: tuple[UTXOInfo, ...]
    value: int
    all_coinjoin_outputs: bool


def estimate_direct_send_fee(
    utxos: list[UTXOInfo], destination: str, fee_rate: float, *, has_change: bool
) -> tuple[int, int]:
    """Estimate the direct-send fee and vsize.

    P2WSH inputs (expired fidelity bonds being swept) are larger than P2WPKH
    inputs because their witness carries the timelock script, so size them as
    such or the resulting fee rate falls below the requested one.

    Returns ``(fee, vsize)``.
    """
    input_types = ["p2wsh" if utxo.is_p2wsh else "p2wpkh" for utxo in utxos]
    try:
        destination_type = get_address_type(destination)
    except ValueError:
        # Direct-send currently accepts bech32 destinations, but preserve the
        # wallet's conservative P2WPKH fallback for callers of this pure helper.
        destination_type = "p2wpkh"
    output_types = [destination_type]
    if has_change:
        output_types.append("p2wpkh")
    vsize = estimate_vsize(input_types, output_types)
    return math.ceil(vsize * fee_rate), vsize


def _evaluate_direct_send_selection(
    utxos: list[UTXOInfo],
    amount_sats: int,
    destination: str,
    fee_rate: float,
    dust_threshold: int,
) -> DirectSendSelection | None:
    """Return fee/change details when *utxos* can fund a direct send."""
    total_value = sum(utxo.value for utxo in utxos)
    fee_with_change, vsize_with_change = estimate_direct_send_fee(
        utxos, destination, fee_rate, has_change=True
    )
    change_amount = total_value - amount_sats - fee_with_change
    if change_amount >= dust_threshold:
        return DirectSendSelection(utxos, fee_with_change, change_amount, vsize_with_change)

    fee_without_change, vsize_without_change = estimate_direct_send_fee(
        utxos, destination, fee_rate, has_change=False
    )
    actual_fee = total_value - amount_sats
    if actual_fee >= fee_without_change:
        return DirectSendSelection(utxos, actual_fee, 0, vsize_without_change)
    return None


def _direct_send_score(
    groups: tuple[_DirectSendScriptGroup, ...],
) -> tuple[int, int, int, tuple[str, ...]]:
    """Return the direct-send privacy objective for an admissible group set."""
    utxos = [utxo for group in groups for utxo in group.utxos]
    outpoints = tuple(sorted(utxo.outpoint for utxo in utxos))
    return len(groups), len(utxos), sum(utxo.value for utxo in utxos), outpoints


def _direct_send_greedy_baseline(
    groups: list[_DirectSendScriptGroup],
    amount_sats: int,
    destination: str,
    fee_rate: float,
    dust_threshold: int,
    *,
    mixdepth: int,
    restrict_md0: bool,
) -> tuple[tuple[_DirectSendScriptGroup, ...], DirectSendSelection] | None:
    """Build a deterministic valid baseline before bounded optimization."""
    ordered_groups = sorted(
        groups,
        key=lambda group: (-group.value, len(group.utxos), group.scriptpubkey),
    )

    # A singleton is necessarily the least possible script-group count.
    for group in ordered_groups:
        selected = list(group.utxos)
        result = _evaluate_direct_send_selection(
            selected, amount_sats, destination, fee_rate, dust_threshold
        )
        if result is not None:
            return (group,), result

    if mixdepth == 0 and restrict_md0:
        ordered_groups = [group for group in ordered_groups if group.all_coinjoin_outputs]

    selected_groups: list[_DirectSendScriptGroup] = []
    for group in ordered_groups:
        selected_groups.append(group)
        selected = [utxo for selected_group in selected_groups for utxo in selected_group.utxos]
        result = _evaluate_direct_send_selection(
            selected, amount_sats, destination, fee_rate, dust_threshold
        )
        if result is not None:
            return tuple(selected_groups), result
    return None


def select_direct_send_utxos(
    utxos: list[UTXOInfo],
    amount_sats: int,
    destination: str,
    fee_rate: float,
    *,
    mixdepth: int,
    min_confirmations: int = 1,
    dust_threshold: int = 546,
    restrict_md0: bool = True,
    max_search_nodes: int = 100_000,
    excluded_outpoints: set[tuple[str, int]] | None = None,
) -> DirectSendSelection:
    """Select direct-send inputs without joining independent script clusters.

    Every eligible regular UTXO at a scriptPubKey is selected atomically. The
    selector minimizes script groups, then input count, then selected value.
    In mixdepth zero, multiple groups are allowed only when every selected
    input is an exact CoinJoin output. Excluded outpoints block their entire
    regular script group.
    """
    if amount_sats <= 0:
        raise ValueError("Direct-send amount must be positive")
    if not math.isfinite(fee_rate) or fee_rate < 0:
        raise ValueError(
            f"Direct-send fee rate must be a finite non-negative value, got {fee_rate!r}"
        )
    if min_confirmations < 0:
        raise ValueError("Minimum confirmations cannot be negative")
    if dust_threshold < 0:
        raise ValueError("Dust threshold cannot be negative")
    if max_search_nodes <= 0:
        raise ValueError("Direct-send selection search limit must be positive")

    # A frozen, under-confirmed, or CoinJoin-leased regular coin marks its
    # whole script cluster unavailable. Spending only its eligible sibling
    # would still link it.
    excluded_outpoints = {(txid.lower(), vout) for txid, vout in (excluded_outpoints or set())}
    regular_utxos = [
        utxo for utxo in utxos if utxo.mixdepth == mixdepth and not utxo.is_fidelity_bond
    ]
    blocked_scripts = {
        utxo.scriptpubkey
        for utxo in regular_utxos
        if (
            utxo.frozen
            or utxo.confirmations < min_confirmations
            or (utxo.txid, utxo.vout) in excluded_outpoints
        )
    }
    eligible_utxos = [
        utxo
        for utxo in regular_utxos
        if utxo.scriptpubkey not in blocked_scripts
        and not utxo.frozen
        and utxo.confirmations >= min_confirmations
        and (utxo.txid, utxo.vout) not in excluded_outpoints
    ]

    grouped_utxos: dict[str, list[UTXOInfo]] = {}
    for utxo in eligible_utxos:
        grouped_utxos.setdefault(utxo.scriptpubkey, []).append(utxo)
    groups = [
        _DirectSendScriptGroup(
            scriptpubkey=scriptpubkey,
            utxos=tuple(sorted(group_utxos, key=lambda utxo: utxo.outpoint)),
            value=sum(utxo.value for utxo in group_utxos),
            all_coinjoin_outputs=all(utxo.coinjoin_output for utxo in group_utxos),
        )
        for scriptpubkey, group_utxos in grouped_utxos.items()
    ]
    if not groups:
        raise ValueError(f"No eligible direct-send UTXOs in mixdepth {mixdepth}")

    baseline = _direct_send_greedy_baseline(
        groups,
        amount_sats,
        destination,
        fee_rate,
        dust_threshold,
        mixdepth=mixdepth,
        restrict_md0=restrict_md0,
    )
    best_groups: tuple[_DirectSendScriptGroup, ...] | None = None
    best_result: DirectSendSelection | None = None
    best_score: tuple[int, int, int, tuple[str, ...]] | None = None
    if baseline is not None:
        best_groups, best_result = baseline
        best_score = _direct_send_score(best_groups)

    nodes_visited = 0
    search_exhausted = False
    search_group_limit = len(best_groups) if best_groups is not None else len(groups)

    def search_group_count(
        candidates: list[_DirectSendScriptGroup],
        required_count: int,
        start: int,
        selected_groups: list[_DirectSendScriptGroup],
    ) -> bool:
        """Search one script-group count, returning False when the node cap hits."""
        nonlocal best_groups, best_result, best_score, nodes_visited
        if len(selected_groups) == required_count:
            nodes_visited += 1
            if nodes_visited > max_search_nodes:
                return False
            candidate_groups = tuple(selected_groups)
            candidate_utxos = sorted(
                [utxo for group in candidate_groups for utxo in group.utxos],
                key=lambda utxo: utxo.outpoint,
            )
            result = _evaluate_direct_send_selection(
                candidate_utxos, amount_sats, destination, fee_rate, dust_threshold
            )
            if result is None:
                return True
            score = _direct_send_score(candidate_groups)
            if best_score is None or score < best_score:
                best_groups = candidate_groups
                best_result = result
                best_score = score
            return True

        remaining_needed = required_count - len(selected_groups)
        last_start = len(candidates) - remaining_needed
        for index in range(start, last_start + 1):
            selected_groups.append(candidates[index])
            if not search_group_count(candidates, required_count, index + 1, selected_groups):
                selected_groups.pop()
                return False
            selected_groups.pop()
        return True

    for group_count in range(1, search_group_limit + 1):
        candidates = groups
        if mixdepth == 0 and restrict_md0 and group_count > 1:
            candidates = [group for group in groups if group.all_coinjoin_outputs]
        if len(candidates) < group_count:
            continue
        if not search_group_count(candidates, group_count, 0, []):
            search_exhausted = True
            break
        if best_groups is not None and len(best_groups) == group_count:
            break

    if search_exhausted:
        raise DirectSendSearchLimitError(
            f"Direct-send selection search exceeded {max_search_nodes:,} candidates "
            "before proving an optimal privacy-admissible selection"
        )

    if best_result is None:
        if len(groups) == 1:
            raise ValueError(f"Insufficient eligible funds in mixdepth {mixdepth}")
        if mixdepth == 0 and restrict_md0:
            raise ValueError(
                "No privacy-admissible sufficient direct-send selection in mixdepth 0; "
                "multiple script clusters require exact CoinJoin outputs"
            )
        raise ValueError(f"No sufficient eligible direct-send UTXOs in mixdepth {mixdepth}")

    return best_result


def _maker_funding_prefix(utxos: list[UTXOInfo], target_amount: int) -> list[UTXOInfo]:
    """Take the first sufficient prefix of an already funded maker pool.

    Inspect at least one coin even when maker fees cover the funding target:
    the maker still needs an input to authenticate its participation.
    """
    total = 0
    for end, utxo in enumerate(utxos, start=1):
        total += utxo.value
        if total >= target_amount:
            return utxos[:end]
    return utxos.copy()


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
        md0_mergeable_outpoints: set[str] | None = None,
        exclude: set[tuple[str, int]] | None = None,
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
            restrict_md0: When True (default), mixdepth 0 UTXOs without exact
                          maker-rotation provenance are restricted to a single
                          UTXO. Exact CoinJoin outputs and recursively proven
                          CoinJoin-only change can be merged. Set to False to
                          disable the restriction.
            md0_mergeable_outpoints: Exact md0 outpoints authorized by the maker's
                                     recursive rotation-lineage policy. When omitted,
                                     only exact CoinJoin outputs are mergeable.
            exclude: ``(txid, vout)`` outpoints that must not be selected. Used to
                     skip inputs already locked by another in-flight CoinJoin round
                     (this or another process) so concurrent rounds never pick the
                     same UTXO and build conflicting transactions.
        """
        utxos = self.utxo_cache.get(mixdepth, [])

        eligible = [utxo for utxo in utxos if utxo.confirmations >= min_confirmations]

        # Filter out frozen UTXOs (never auto-selected)
        eligible = [utxo for utxo in eligible if not utxo.frozen]

        # Filter out UTXOs locked by another in-flight CoinJoin round.
        if exclude:
            eligible = [utxo for utxo in eligible if (utxo.txid, utxo.vout) not in exclude]

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
        # linking deposits/fidelity bonds. Protocol-backed CoinJoin outputs are
        # exempt because they already have CoinJoin privacy.
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

            # Split eligible UTXOs by exact CoinJoin-output provenance.
            mergeable = [
                u
                for u in eligible
                if (
                    u.outpoint in md0_mergeable_outpoints
                    if md0_mergeable_outpoints is not None
                    else u.coinjoin_output
                )
            ]
            mergeable_outpoints = {u.outpoint for u in mergeable}
            non_mergeable = [u for u in eligible if u.outpoint not in mergeable_outpoints]

            remaining = target_amount - total

            # Try CJ output pool first (can be merged safely)
            mergeable_pool_value = sum(u.value for u in mergeable)
            if mergeable_pool_value >= remaining:
                for utxo in mergeable:
                    selected.append(utxo)
                    total += utxo.value
                    if total >= target_amount:
                        return selected

            # Try single largest non-CJ UTXO
            if non_mergeable and non_mergeable[0].value >= remaining:
                selected.append(non_mergeable[0])
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

            largest_non_mergeable = non_mergeable[0].value if non_mergeable else 0
            raise ValueError(
                f"Insufficient funds: rotation-lineage pool has {mergeable_pool_value}, "
                f"largest non-lineage UTXO has {largest_non_mergeable}, "
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
        *,
        exclude: set[tuple[str, int]] | None = None,
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
            exclude: ``(txid, vout)`` outpoints that must not be included.
                Used to skip inputs already locked by another in-flight
                CoinJoin round.

        Returns:
            List of all eligible UTXOs in the mixdepth
        """
        utxos = self.utxo_cache.get(mixdepth, [])
        eligible = [utxo for utxo in utxos if utxo.confirmations >= min_confirmations]
        # Filter out frozen UTXOs (never auto-selected)
        eligible = [utxo for utxo in eligible if not utxo.frozen]
        if exclude:
            eligible = [utxo for utxo in eligible if (utxo.txid, utxo.vout) not in exclude]
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
        md0_mergeable_outpoints: set[str] | None = None,
        exclude: set[tuple[str, int]] | None = None,
    ) -> list[UTXOInfo]:
        """
        Select UTXOs with merge algorithm for maker UTXO consolidation.

        Policies operate only within the authorized, eligible source pool.
        Additional inputs consolidate coins but disclose more co-owned outpoints
        to the taker and increase the mining fee paid by the taker.

        Args:
            mixdepth: Mixdepth to select from
            target_amount: Minimum target amount in satoshis
            min_confirmations: Minimum confirmations required
            merge_algorithm: Selection strategy:
                - "default": Smallest sufficient coin, otherwise largest-first;
                  with probability (n-2)/n, top up to three inputs using random
                  extras, where n is the eligible pool size and n > 2.
                - "gradual": Smallest-first subtarget prefix, pruning its smallest
                  leading coins while funding remains sufficient.
                - "greedy": Smallest-first prefix, pruning unnecessary preceding
                  coins largest-first while retaining the crossing coin.
                - "greediest": Unpruned smallest-first subtarget prefix.
                - "random": Random funding prefix, then randomly prune individually
                  unnecessary inputs. Randomizes identities, not just input count.
            include_fidelity_bonds: If True, include fidelity bond UTXOs.
                                    Defaults to False since they should never be
                                    automatically spent in CoinJoins.
            restrict_md0: When True (default), mixdepth 0 UTXOs without exact
                          maker-rotation provenance are restricted to a single
                          UTXO. Exact CoinJoin outputs and recursively proven
                          CoinJoin-only change can be merged. Set to False to
                          disable the restriction.
            md0_mergeable_outpoints: Exact md0 outpoints authorized by the maker's
                                     recursive rotation-lineage policy. When omitted,
                                     only exact CoinJoin outputs are mergeable.
            exclude: ``(txid, vout)`` outpoints that must not be selected. Used by
                     makers to avoid committing the same UTXO to two concurrent
                     CoinJoin sessions (which would create conflicting, mutually
                     double-spending transactions).

        Returns:
            List of selected UTXOs

        Raises:
            ValueError: If insufficient funds
        """
        utxos = self.utxo_cache.get(mixdepth, [])
        eligible = [utxo for utxo in utxos if utxo.confirmations >= min_confirmations]

        # Filter out frozen UTXOs (never auto-selected)
        eligible = [utxo for utxo in eligible if not utxo.frozen]

        # Filter out UTXOs already committed to another in-flight session.
        if exclude:
            eligible = [utxo for utxo in eligible if (utxo.txid, utxo.vout) not in exclude]

        # Filter out fidelity bond UTXOs by default
        if not include_fidelity_bonds:
            eligible = [utxo for utxo in eligible if not utxo.is_fidelity_bond]

        # Sort by value descending for efficient selection
        eligible.sort(key=lambda u: u.value, reverse=True)

        if mixdepth == 0 and restrict_md0:
            if not eligible:
                raise ValueError("Insufficient funds: no eligible UTXOs in mixdepth 0")

            mergeable = [
                u
                for u in eligible
                if (
                    u.outpoint in md0_mergeable_outpoints
                    if md0_mergeable_outpoints is not None
                    else u.coinjoin_output
                )
            ]
            mergeable_outpoints = {u.outpoint for u in mergeable}
            non_mergeable = [u for u in eligible if u.outpoint not in mergeable_outpoints]

            mergeable_pool_value = sum(u.value for u in mergeable)
            largest_non_mergeable = non_mergeable[0].value if non_mergeable else 0

            if mergeable_pool_value >= target_amount:
                return self._select_maker_pool(mergeable, target_amount, merge_algorithm)
            elif largest_non_mergeable >= target_amount:
                # Never mix unrelated deposits, regardless of the merge policy.
                return [
                    min(
                        (u for u in non_mergeable if u.value >= target_amount),
                        key=lambda u: u.value,
                    )
                ]
            else:
                raise ValueError(
                    f"Insufficient funds: rotation-lineage pool has {mergeable_pool_value}, "
                    f"largest non-lineage UTXO has {largest_non_mergeable}, "
                    f"need {target_amount}. "
                    f"Cannot merge non-CJ md0 UTXOs for privacy reasons."
                )

        total = sum(utxo.value for utxo in eligible)
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

        return self._select_maker_pool(eligible, target_amount, merge_algorithm)

    @staticmethod
    def _select_maker_pool(
        pool: list[UTXOInfo],
        target_amount: int,
        merge_algorithm: str,
    ) -> list[UTXOInfo]:
        """Select from a sufficient, authorized pool sorted by value descending.

        Gradual, greedy and greediest mirror clientserver's value-based policies
        for positive targets. No policy may pull in coins outside this pool.
        """
        if not pool:
            return []

        if merge_algorithm == "random":
            shuffled = pool.copy()
            secure_random.shuffle(shuffled)
            selected = _maker_funding_prefix(shuffled, target_amount)
            total = sum(u.value for u in selected)
            pruning_order = selected.copy()
            secure_random.shuffle(pruning_order)
            removed: set[str] = set()
            for utxo in pruning_order:
                if len(selected) - len(removed) > 1 and total - utxo.value >= target_amount:
                    removed.add(utxo.outpoint)
                    total -= utxo.value
            selected = [u for u in selected if u.outpoint not in removed]
            # Selection/pruning order must not determine the disclosed auth input.
            secure_random.shuffle(selected)
            return selected

        ascending = sorted(pool, key=lambda u: u.value)
        if merge_algorithm in ("gradual", "greediest"):
            smaller = [u for u in ascending if u.value < target_amount]
            if not smaller or sum(u.value for u in smaller) < target_amount:
                return [next(u for u in ascending if u.value >= target_amount)]
            selected = _maker_funding_prefix(smaller, target_amount)
            if merge_algorithm == "greediest":
                return selected
            total = sum(u.value for u in selected)
            start = 0
            while total - selected[start].value >= target_amount:
                total -= selected[start].value
                start += 1
            return selected[start:]

        if merge_algorithm == "greedy":
            prefix = _maker_funding_prefix(ascending, target_amount)
            total = sum(u.value for u in prefix)
            if total == target_amount:
                return prefix
            selected = [prefix[-1]]
            for utxo in reversed(prefix[:-1]):
                if total - utxo.value >= target_amount:
                    total -= utxo.value
                else:
                    selected.append(utxo)
            return selected

        # Unknown strings retain the old largest-first fallback; configuration
        # parsing rejects them before a production maker reaches this method.
        sufficient = (
            next((u for u in ascending if u.value >= target_amount), None)
            if merge_algorithm == "default"
            else None
        )
        selected = (
            [sufficient] if sufficient is not None else _maker_funding_prefix(pool, target_amount)
        )
        n = len(pool)
        if merge_algorithm == "default" and n > 2 and len(selected) < 3:
            if secure_random.randrange(n) >= 2:
                selected_outpoints = {u.outpoint for u in selected}
                remaining = [u for u in pool if u.outpoint not in selected_outpoints]
                selected.extend(secure_random.sample(remaining, 3 - len(selected)))
        if merge_algorithm == "default":
            secure_random.shuffle(selected)
        return selected
