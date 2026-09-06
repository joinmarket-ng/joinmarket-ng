"""Bounded residual-attribution experiment for co-funded channel rings.

This is an observer-model experiment, not a production privacy claim. It uses
the production planner to create synthetic rings, then gives the observer only
the directed pseudonymous topology, capacities, and exact input residuals.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import random
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from taker.ring_planner import (
    ParticipantChannelLimits,
    RingParty,
    plan_cofunded_ring,
)


OUTGOING_FLOOR = 1_000
INCOMING_FLOOR = 1_000
KNOWN_CLOSE_FEE = 250
DEFAULT_SEED = 20_260_905
DEFAULT_TRIALS = 30
DEFAULT_SIZES = (4, 6)
DEFAULT_CLOSURE_FRACTIONS = (0.0, 0.5, 1.0)
DEFAULT_FORCE_SHARES = (0.0, 0.1, 1.0)
DEFAULT_COLLUSION_FRACTIONS = (0.0, 0.25, 0.5)
DEFAULT_MOVEMENT_FACTORS = (0.0, 0.1, 0.5)
DEFAULT_BOOTSTRAP_RESAMPLES = 400


@dataclass(frozen=True)
class IntegerInterval:
    """Inclusive integer interval used for an outgoing contribution."""

    lower: int
    upper: int

    @property
    def length(self) -> int:
        return max(0, self.upper - self.lower + 1)

    def intersection(self, other: IntegerInterval) -> IntegerInterval:
        return IntegerInterval(
            max(self.lower, other.lower), min(self.upper, other.upper)
        )


@dataclass(frozen=True)
class AssignmentSupport:
    """One owner-to-vertex assignment and all its feasible x_0 values."""

    owner_by_vertex: tuple[int, ...]
    x0_interval: IntegerInterval

    @property
    def funding_weight(self) -> int:
        """Uniform-split weight under the stated funding-only model."""

        return self.x0_interval.length


@dataclass(frozen=True)
class Trial:
    """Synthetic plan plus ground truth retained outside observer inference."""

    seed: int
    size: int
    residuals_by_owner: tuple[int, ...]
    capacities: tuple[int, ...]
    initial_balances: tuple[int, ...]
    true_owner_by_vertex: tuple[int, ...]


@dataclass(frozen=True)
class Observation:
    """Additional observer disclosures layered on public funding data."""

    balance_bounds: dict[int, IntegerInterval]
    known_owner_by_vertex: dict[int, int]
    closed_vertices: tuple[int, ...]
    force_closed_vertices: tuple[int, ...]
    colluding_vertices: tuple[int, ...]
    colluding_owners: frozenset[int]
    movement_bound: int

    @property
    def is_funding_only(self) -> bool:
        return not self.balance_bounds and not self.known_owner_by_vertex


@dataclass(frozen=True)
class Scenario:
    closure_fraction: float
    force_share: float
    collusion_fraction: float
    movement_factor: float


@dataclass(frozen=True)
class ExperimentConfig:
    seed: int = DEFAULT_SEED
    trials: int = DEFAULT_TRIALS
    sizes: tuple[int, ...] = DEFAULT_SIZES
    closure_fractions: tuple[float, ...] = DEFAULT_CLOSURE_FRACTIONS
    force_shares: tuple[float, ...] = DEFAULT_FORCE_SHARES
    collusion_fractions: tuple[float, ...] = DEFAULT_COLLUSION_FRACTIONS
    movement_factors: tuple[float, ...] = DEFAULT_MOVEMENT_FACTORS
    bootstrap_resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES
    force_endpoint_identity_leak: bool = False
    smoke: bool = False

    def __post_init__(self) -> None:
        if not self.smoke and self.trials < DEFAULT_TRIALS:
            raise ValueError(
                f"trials must be at least {DEFAULT_TRIALS} outside --smoke"
            )
        if self.smoke and self.trials < 1:
            raise ValueError("smoke trials must be positive")
        if not self.sizes or any(size not in (4, 6, 8) for size in self.sizes):
            raise ValueError(
                "sizes must be a non-empty selection from 4, 6, and optional 8"
            )
        if self.bootstrap_resamples < 1:
            raise ValueError("bootstrap_resamples must be positive")
        for name, values in (
            ("closure_fractions", self.closure_fractions),
            ("force_shares", self.force_shares),
            ("collusion_fractions", self.collusion_fractions),
            ("movement_factors", self.movement_factors),
        ):
            if not values or any(value < 0.0 or value > 1.0 for value in values):
                raise ValueError(f"{name} must contain values from 0 through 1")


def _loose_limits() -> ParticipantChannelLimits:
    """Homogeneous limits whose only binding constraints are contribution floors."""

    return ParticipantChannelLimits(
        min_channel_capacity=1,
        max_channel_capacity=2_100_000_000_000_000,
        max_push_amount=2_100_000_000_000_000,
        dust_limit=0,
        outgoing_reserve=OUTGOING_FLOOR,
        incoming_reserve=INCOMING_FLOOR,
        commitment_fee=0,
        channel_overhead=0,
        spendable_margin=0,
    )


def _party_id(owner: int) -> str:
    return "taker" if owner == 0 else f"maker-{owner}"


def _sample_residual(source: random.Random) -> int:
    """Use a deliberately synthetic, coarse normal-like residual distribution."""

    sampled = round(source.gauss(100_000, 20_000) / 1_000) * 1_000
    return max(20_000, sampled)


def generate_trial(seed: int, size: int) -> Trial:
    """Create one reproducible production-planned ring and retain its hidden truth."""

    source = random.Random(seed)
    residuals = tuple(_sample_residual(source) for _ in range(size))
    parties = [
        RingParty(_party_id(owner), residual, _loose_limits())
        for owner, residual in enumerate(residuals)
    ]
    plan = plan_cofunded_ring(parties, rng=source)
    owner_for_party = {_party_id(owner): owner for owner in range(size)}
    outgoing_by_party = {item.party_id: item.outgoing for item in plan.contributions}
    true_owner_by_vertex = tuple(owner_for_party[party_id] for party_id in plan.cycle)
    initial_balances = tuple(outgoing_by_party[party_id] for party_id in plan.cycle)
    capacities = tuple(edge.capacity for edge in plan.edges)
    return Trial(
        seed=seed,
        size=size,
        residuals_by_owner=residuals,
        capacities=capacities,
        initial_balances=initial_balances,
        true_owner_by_vertex=true_owner_by_vertex,
    )


def assignment_interval(
    owner_by_vertex: Sequence[int],
    residuals_by_owner: Sequence[int],
    capacities: Sequence[int],
    *,
    outgoing_floor: int = OUTGOING_FLOOR,
    incoming_floor: int = INCOMING_FLOOR,
    balance_bounds: dict[int, IntegerInterval] | None = None,
) -> IntegerInterval | None:
    """Derive the common integer x_0 interval from c_i=x_i+r_(i+1)-x_(i+1)."""

    count = len(owner_by_vertex)
    if count < 2 or len(residuals_by_owner) != count or len(capacities) != count:
        raise ValueError(
            "owners, residuals, and capacities must have the same length of at least two"
        )
    if set(owner_by_vertex) != set(range(count)):
        raise ValueError("owner_by_vertex must be a permutation of owner indexes")
    if outgoing_floor < 0 or incoming_floor < 0:
        raise ValueError("contribution floors must be nonnegative")

    residuals = [residuals_by_owner[owner] for owner in owner_by_vertex]
    deltas = [0]
    for vertex in range(count - 1):
        deltas.append(deltas[-1] + residuals[vertex + 1] - capacities[vertex])
    if deltas[-1] + residuals[0] - capacities[-1] != 0:
        return None

    interval: IntegerInterval | None = None
    for vertex, residual in enumerate(residuals):
        contribution_bounds = IntegerInterval(
            outgoing_floor - deltas[vertex],
            residual - incoming_floor - deltas[vertex],
        )
        interval = (
            contribution_bounds
            if interval is None
            else interval.intersection(contribution_bounds)
        )
        if balance_bounds is not None and vertex in balance_bounds:
            observed = balance_bounds[vertex]
            interval = interval.intersection(
                IntegerInterval(
                    observed.lower - deltas[vertex], observed.upper - deltas[vertex]
                )
            )
        if interval.length == 0:
            return None
    assert interval is not None
    return interval


def enumerate_feasible_assignments(
    residuals_by_owner: Sequence[int],
    capacities: Sequence[int],
    *,
    outgoing_floor: int = OUTGOING_FLOOR,
    incoming_floor: int = INCOMING_FLOOR,
    balance_bounds: dict[int, IntegerInterval] | None = None,
    known_owner_by_vertex: dict[int, int] | None = None,
) -> list[AssignmentSupport]:
    """Enumerate all n! owner mappings and retain every feasible integer split."""

    count = len(residuals_by_owner)
    if len(capacities) != count:
        raise ValueError("residuals and capacities must have the same length")
    known = known_owner_by_vertex or {}
    if any(
        vertex < 0 or vertex >= count or owner < 0 or owner >= count
        for vertex, owner in known.items()
    ):
        raise ValueError("known owner disclosure is outside the ring")

    support: list[AssignmentSupport] = []
    for assignment in itertools.permutations(range(count)):
        if any(assignment[vertex] != owner for vertex, owner in known.items()):
            continue
        interval = assignment_interval(
            assignment,
            residuals_by_owner,
            capacities,
            outgoing_floor=outgoing_floor,
            incoming_floor=incoming_floor,
            balance_bounds=balance_bounds,
        )
        if interval is not None:
            support.append(AssignmentSupport(assignment, interval))
    return support


def _fraction_count(total: int, fraction: float) -> int:
    return min(total, int(total * fraction + 0.5))


def _add_balance_bound(
    bounds: dict[int, IntegerInterval], vertex: int, incoming: IntegerInterval
) -> None:
    existing = bounds.get(vertex)
    bounds[vertex] = incoming if existing is None else existing.intersection(incoming)


def build_observation(
    trial: Trial,
    scenario: Scenario,
    *,
    random_seed: int,
    force_endpoint_identity_leak: bool = False,
) -> Observation:
    """Generate disclosed balances and identities without exposing truth to inference."""

    source = random.Random(random_seed)
    count = trial.size
    closed_count = _fraction_count(count, scenario.closure_fraction)
    closed_vertices = tuple(sorted(source.sample(range(count), closed_count)))
    force_closed_vertices = tuple(
        vertex for vertex in closed_vertices if source.random() < scenario.force_share
    )
    colluder_count = _fraction_count(count, scenario.collusion_fraction)
    colluding_vertices = tuple(sorted(source.sample(range(count), colluder_count)))
    median_residual = statistics.median(trial.residuals_by_owner)
    movement_bound = round(scenario.movement_factor * median_residual)

    balance_bounds: dict[int, IntegerInterval] = {}
    known_owner_by_vertex: dict[int, int] = {}
    for vertex in closed_vertices:
        initial = trial.initial_balances[vertex]
        max_shift = min(movement_bound, initial - KNOWN_CLOSE_FEE)
        shift = source.randint(1, max_shift) if max_shift > 0 else 0
        # A known opener-paid close fee is added back before constructing the interval.
        onchain_close_balance = initial - shift - KNOWN_CLOSE_FEE
        normalized_current = onchain_close_balance + KNOWN_CLOSE_FEE
        _add_balance_bound(
            balance_bounds,
            vertex,
            IntegerInterval(
                normalized_current - movement_bound, normalized_current + movement_bound
            ),
        )

    if force_endpoint_identity_leak:
        for vertex in force_closed_vertices:
            known_owner_by_vertex[vertex] = trial.true_owner_by_vertex[vertex]
            neighbor = (vertex + 1) % count
            known_owner_by_vertex[neighbor] = trial.true_owner_by_vertex[neighbor]

    colluding_owners = frozenset(
        trial.true_owner_by_vertex[vertex] for vertex in colluding_vertices
    )
    for vertex in colluding_vertices:
        for disclosed_vertex in ((vertex - 1) % count, vertex, (vertex + 1) % count):
            known_owner_by_vertex[disclosed_vertex] = trial.true_owner_by_vertex[
                disclosed_vertex
            ]
        # A peer knows the opening balances on both incident ring edges at funding.
        for edge_vertex in ((vertex - 1) % count, vertex):
            initial = trial.initial_balances[edge_vertex]
            _add_balance_bound(
                balance_bounds, edge_vertex, IntegerInterval(initial, initial)
            )

    return Observation(
        balance_bounds=balance_bounds,
        known_owner_by_vertex=known_owner_by_vertex,
        closed_vertices=closed_vertices,
        force_closed_vertices=force_closed_vertices,
        colluding_vertices=colluding_vertices,
        colluding_owners=colluding_owners,
        movement_bound=movement_bound,
    )


def _candidate_owners_at_vertex(
    support: Iterable[AssignmentSupport], vertex: int
) -> set[int]:
    return {candidate.owner_by_vertex[vertex] for candidate in support}


def support_metrics(
    support: Sequence[AssignmentSupport],
    truth: Sequence[int],
    colluding_owners: frozenset[int],
    *,
    funding_posterior_valid: bool,
) -> dict[str, float | int | bool | None]:
    """Report support counts, and posterior metrics only for funding-only observations."""

    target_vertices = [
        vertex for vertex, owner in enumerate(truth) if owner not in colluding_owners
    ]
    owner_counts = [
        _candidate_owners_at_vertex(support, vertex) for vertex in target_vertices
    ]
    result: dict[str, float | int | bool | None] = {
        "support_assignments": len(support),
        "support_integer_splits": sum(
            candidate.funding_weight for candidate in support
        ),
        "ground_truth_in_support": any(
            candidate.owner_by_vertex == tuple(truth) for candidate in support
        ),
        "noncolluding_target_count": len(target_vertices),
        "colluding_owner_count": len(colluding_owners),
        "mean_feasible_owner_count": (
            statistics.fmean(len(owners) for owners in owner_counts)
            if owner_counts
            else None
        ),
        "uniquely_identified_fraction": (
            statistics.fmean(len(owners) == 1 for owners in owner_counts)
            if owner_counts
            else None
        ),
        "mean_realized_label_posterior": None,
        "mean_bayes_vulnerability": None,
        "mean_effective_anonymity": None,
    }
    if not funding_posterior_valid or not target_vertices or not support:
        return result

    total_weight = sum(candidate.funding_weight for candidate in support)
    realized_label_probabilities: list[float] = []
    bayes_vulnerabilities: list[float] = []
    effective_anonymities: list[float] = []
    for vertex in target_vertices:
        weights_by_owner: dict[int, int] = {}
        for candidate in support:
            owner = candidate.owner_by_vertex[vertex]
            weights_by_owner[owner] = (
                weights_by_owner.get(owner, 0) + candidate.funding_weight
            )
        probabilities = [weight / total_weight for weight in weights_by_owner.values()]
        realized_label_probabilities.append(
            weights_by_owner.get(truth[vertex], 0) / total_weight
        )
        bayes_vulnerabilities.append(max(probabilities))
        entropy = -sum(
            probability * math.log(probability)
            for probability in probabilities
            if probability
        )
        effective_anonymities.append(math.exp(entropy))
    result["mean_realized_label_posterior"] = statistics.fmean(
        realized_label_probabilities
    )
    result["mean_bayes_vulnerability"] = statistics.fmean(bayes_vulnerabilities)
    result["mean_effective_anonymity"] = statistics.fmean(effective_anonymities)
    return result


def baseline_plain_change_metrics(
    residuals_by_owner: Sequence[int],
) -> dict[str, float | int]:
    """Exact plain-change attribution, preserving ambiguity when residual values collide."""

    multiplicities: dict[int, int] = {}
    for residual in residuals_by_owner:
        multiplicities[residual] = multiplicities.get(residual, 0) + 1
    candidate_counts = [multiplicities[residual] for residual in residuals_by_owner]
    return {
        "mean_feasible_owner_count": statistics.fmean(candidate_counts),
        "uniquely_identified_fraction": statistics.fmean(
            count == 1 for count in candidate_counts
        ),
        "mean_effective_anonymity": statistics.fmean(candidate_counts),
        "mean_bayes_vulnerability": statistics.fmean(
            1 / count for count in candidate_counts
        ),
    }


def _bootstrap_interval(
    values: Sequence[float], *, seed: int, resamples: int
) -> list[float]:
    if not values:
        return []
    source = random.Random(seed)
    count = len(values)
    means = sorted(
        statistics.fmean(values[source.randrange(count)] for _ in range(count))
        for _ in range(resamples)
    )
    lower_index = math.floor((len(means) - 1) * 0.025)
    upper_index = math.ceil((len(means) - 1) * 0.975)
    return [means[lower_index], means[upper_index]]


def _metric_summary(
    values: Sequence[float], *, seed: int, resamples: int
) -> dict[str, float | int | list[float]] | None:
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "ci95_bootstrap": _bootstrap_interval(values, seed=seed, resamples=resamples),
        "seed_count": len(values),
    }


def _summarize_seed_metrics(
    metrics: Sequence[dict[str, float | int | bool | None]],
    *,
    bootstrap_seed: int,
    resamples: int,
    include_posterior: bool,
) -> dict[str, dict[str, float | int | list[float]] | None]:
    metric_names = [
        "support_assignments",
        "support_integer_splits",
        "noncolluding_target_count",
        "colluding_owner_count",
        "mean_feasible_owner_count",
        "uniquely_identified_fraction",
    ]
    if include_posterior:
        metric_names.extend(
            (
                "mean_realized_label_posterior",
                "mean_bayes_vulnerability",
                "mean_effective_anonymity",
            )
        )
    summary: dict[str, dict[str, float | int | list[float]] | None] = {}
    for offset, name in enumerate(metric_names):
        values = [
            float(value) for metric in metrics if (value := metric[name]) is not None
        ]
        summary[name] = _metric_summary(
            values, seed=bootstrap_seed + offset, resamples=resamples
        )
    return summary


def _scenario_seed(trial_seed: int, scenario: Scenario, size: int) -> int:
    encoded = (
        f"{trial_seed}:{size}:{scenario.closure_fraction:.3f}:{scenario.force_share:.3f}:"
        f"{scenario.collusion_fraction:.3f}:{scenario.movement_factor:.3f}"
    )
    return int.from_bytes(encoded.encode("ascii"), "big")


def _scenario_rows(config: ExperimentConfig) -> tuple[Scenario, ...]:
    return tuple(
        Scenario(closure, force, collusion, movement)
        for closure in config.closure_fractions
        for force in config.force_shares
        for collusion in config.collusion_fractions
        for movement in config.movement_factors
    )


def _trial_seed(config: ExperimentConfig, size: int, trial_index: int) -> int:
    return config.seed + size * 100_003 + trial_index


def primary_paired_contrasts(
    ring_metrics: Sequence[dict[str, float | int | bool | None]],
    baseline_metrics: Sequence[dict[str, float | int]],
    *,
    bootstrap_seed: int,
    resamples: int,
) -> dict[str, object]:
    """Summarize pre-specified synthetic seed-level funding contrasts."""

    if len(ring_metrics) != len(baseline_metrics):
        raise ValueError("ring and baseline metrics must be paired by seed")
    anonymity_differences: list[float] = []
    vulnerability_differences: list[float] = []
    for ring, baseline in zip(ring_metrics, baseline_metrics, strict=True):
        ring_anonymity = ring["mean_effective_anonymity"]
        ring_vulnerability = ring["mean_bayes_vulnerability"]
        if ring_anonymity is None or ring_vulnerability is None:
            raise ValueError("primary contrasts require funding-only posterior metrics")
        anonymity_differences.append(
            float(ring_anonymity) - float(baseline["mean_effective_anonymity"])
        )
        vulnerability_differences.append(
            float(baseline["mean_bayes_vulnerability"]) - float(ring_vulnerability)
        )
    return {
        "scope": "PRIMARY synthetic paired seed-level contrasts, not population estimates.",
        "ring_minus_baseline_effective_anonymity": _metric_summary(
            anonymity_differences,
            seed=bootstrap_seed,
            resamples=resamples,
        ),
        "baseline_minus_ring_bayes_vulnerability": _metric_summary(
            vulnerability_differences,
            seed=bootstrap_seed + 1,
            resamples=resamples,
        ),
    }


def _disclosure_summary(observations: Sequence[Observation]) -> dict[str, object]:
    """Expose rounded closure/collusion counts and sampled force-close rates separately."""

    if not observations:
        raise ValueError("at least one observation is required")
    closed_counts = [len(observation.closed_vertices) for observation in observations]
    force_counts = [
        len(observation.force_closed_vertices) for observation in observations
    ]
    colluder_counts = [
        len(observation.colluding_vertices) for observation in observations
    ]
    force_fractions = [
        force_count / closed_count
        for force_count, closed_count in zip(force_counts, closed_counts, strict=True)
        if closed_count
    ]
    return {
        "closure_edges_per_ring": {
            "quantized_count": closed_counts[0],
            "mean": statistics.fmean(closed_counts),
        },
        "colluding_vertices_per_ring": {
            "quantized_count": colluder_counts[0],
            "mean": statistics.fmean(colluder_counts),
        },
        "force_closed_edges_per_ring": {
            "sampling": "independent Bernoulli draw per closed edge",
            "mean_count": statistics.fmean(force_counts),
            "mean_fraction_of_closed": statistics.fmean(force_fractions)
            if force_fractions
            else None,
        },
        "mean_balance_constrained_edges": statistics.fmean(
            len(observation.balance_bounds) for observation in observations
        ),
        "mean_identity_known_vertices": statistics.fmean(
            len(observation.known_owner_by_vertex) for observation in observations
        ),
    }


def _result_row(
    trial_metrics: Sequence[dict[str, float | int | bool | None]],
    baseline_metrics: Sequence[dict[str, float | int]],
    observations: Sequence[Observation],
    scenario: Scenario,
    *,
    bootstrap_seed: int,
    config: ExperimentConfig,
) -> dict[str, object]:
    posterior_valid = all(observation.is_funding_only for observation in observations)
    all_truth_supported = all(
        bool(metric["ground_truth_in_support"]) for metric in trial_metrics
    )
    metrics_summary = _summarize_seed_metrics(
        trial_metrics,
        bootstrap_seed=bootstrap_seed,
        resamples=config.bootstrap_resamples,
        include_posterior=False,
    )
    posterior_summary = (
        _summarize_seed_metrics(
            trial_metrics,
            bootstrap_seed=bootstrap_seed + 100,
            resamples=config.bootstrap_resamples,
            include_posterior=True,
        )
        if posterior_valid
        else None
    )
    primary_contrasts = (
        primary_paired_contrasts(
            trial_metrics,
            baseline_metrics,
            bootstrap_seed=bootstrap_seed + 200,
            resamples=config.bootstrap_resamples,
        )
        if posterior_valid and scenario == Scenario(0.0, 0.0, 0.0, 0.0)
        else None
    )
    return {
        "scenario": {
            "closure_fraction": scenario.closure_fraction,
            "force_share_of_closed": scenario.force_share,
            "collusion_fraction": scenario.collusion_fraction,
            "movement_bound_as_median_residual_fraction": scenario.movement_factor,
        },
        "inference_mode": "funding_uniform_posterior"
        if posterior_valid
        else "support_only",
        "posterior_condition": (
            "Uniform over feasible integer splits after funding only; realized-label score is not attacker accuracy."
            if posterior_valid
            else None
        ),
        "ground_truth_in_support_all_trials": all_truth_supported,
        "metrics": metrics_summary,
        "posterior_metrics": (
            {
                "mean_realized_label_posterior": posterior_summary[
                    "mean_realized_label_posterior"
                ],
                "mean_bayes_vulnerability": posterior_summary[
                    "mean_bayes_vulnerability"
                ],
                "mean_effective_anonymity": posterior_summary[
                    "mean_effective_anonymity"
                ],
            }
            if posterior_summary is not None
            else None
        ),
        "primary_paired_contrasts": primary_contrasts,
        "force_endpoint_identity_leak_worst_case": config.force_endpoint_identity_leak,
        "disclosures_across_trials": _disclosure_summary(observations),
    }


def _baseline_summary(
    trials: Sequence[Trial], config: ExperimentConfig, bootstrap_seed: int
) -> dict[str, dict[str, float | int | list[float]] | None]:
    metrics = [
        baseline_plain_change_metrics(trial.residuals_by_owner) for trial in trials
    ]
    return {
        name: _metric_summary(
            [float(metric[name]) for metric in metrics],
            seed=bootstrap_seed + index,
            resamples=config.bootstrap_resamples,
        )
        for index, name in enumerate(
            (
                "mean_feasible_owner_count",
                "uniquely_identified_fraction",
                "mean_effective_anonymity",
                "mean_bayes_vulnerability",
            )
        )
    }


def _no_post_funding_movement_exact_close_control(
    trials: Sequence[Trial], config: ExperimentConfig, bootstrap_seed: int
) -> dict[str, object]:
    scenario = Scenario(1.0, 0.0, 0.0, 0.0)
    metrics: list[dict[str, float | int | bool | None]] = []
    for trial in trials:
        observation = build_observation(
            trial,
            scenario,
            random_seed=_scenario_seed(trial.seed, scenario, trial.size),
        )
        support = enumerate_feasible_assignments(
            trial.residuals_by_owner,
            trial.capacities,
            balance_bounds=observation.balance_bounds,
            known_owner_by_vertex=observation.known_owner_by_vertex,
        )
        metrics.append(
            support_metrics(
                support,
                trial.true_owner_by_vertex,
                observation.colluding_owners,
                funding_posterior_valid=False,
            )
        )
    return {
        "name": "no_post_funding_movement_exact_close_null",
        "inference_mode": "support_only",
        "interpretation": "A fully closed ring with no post-funding movement and exact balances is an attribution-leak null control, not a privacy gain.",
        "ground_truth_in_support_all_trials": all(
            bool(metric["ground_truth_in_support"]) for metric in metrics
        ),
        "metrics": _summarize_seed_metrics(
            metrics,
            bootstrap_seed=bootstrap_seed,
            resamples=config.bootstrap_resamples,
            include_posterior=False,
        ),
    }


def equal_residual_circular_flow_null_control() -> dict[str, object]:
    """Show the ownership-support null from a common circular balance shift."""

    residuals = (10, 10, 10, 10)
    capacities = (9, 9, 9, 13)
    current_balances = (2, 3, 4, 5)
    base_support = enumerate_feasible_assignments(
        residuals,
        capacities,
        outgoing_floor=1,
        incoming_floor=1,
    )
    fully_closed_support = enumerate_feasible_assignments(
        residuals,
        capacities,
        outgoing_floor=1,
        incoming_floor=1,
        balance_bounds={
            vertex: IntegerInterval(balance, balance)
            for vertex, balance in enumerate(current_balances)
        },
    )
    return {
        "name": "equal_residual_common_circular_flow_fully_closed_null",
        "interpretation": "A common two-sat circular shift from initial balances (4, 5, 6, 7) to exact close balances preserves the capacity equations. With equal residuals, full close narrows splits but leaves every owner label feasible at every vertex.",
        "base_support_assignments": len(base_support),
        "fully_closed_support_assignments": len(fully_closed_support),
        "base_feasible_owners_per_vertex": len(
            _candidate_owners_at_vertex(base_support, 0)
        ),
        "fully_closed_feasible_owners_per_vertex": len(
            _candidate_owners_at_vertex(fully_closed_support, 0)
        ),
    }


def run_experiment(config: ExperimentConfig) -> dict[str, object]:
    """Run matched seeded trials across all requested observer scenarios."""

    results_by_size: dict[str, object] = {}
    scenarios = _scenario_rows(config)
    for size in config.sizes:
        trials = [
            generate_trial(_trial_seed(config, size, index), size)
            for index in range(config.trials)
        ]
        baseline_metrics = [
            baseline_plain_change_metrics(trial.residuals_by_owner) for trial in trials
        ]
        rows: list[dict[str, object]] = []
        for scenario_index, scenario in enumerate(scenarios):
            trial_metrics: list[dict[str, float | int | bool | None]] = []
            observations: list[Observation] = []
            for trial in trials:
                observation = build_observation(
                    trial,
                    scenario,
                    random_seed=_scenario_seed(trial.seed, scenario, size),
                    force_endpoint_identity_leak=config.force_endpoint_identity_leak,
                )
                observations.append(observation)
                support = enumerate_feasible_assignments(
                    trial.residuals_by_owner,
                    trial.capacities,
                    balance_bounds=observation.balance_bounds,
                    known_owner_by_vertex=observation.known_owner_by_vertex,
                )
                trial_metrics.append(
                    support_metrics(
                        support,
                        trial.true_owner_by_vertex,
                        observation.colluding_owners,
                        funding_posterior_valid=observation.is_funding_only,
                    )
                )
            rows.append(
                _result_row(
                    trial_metrics,
                    baseline_metrics,
                    observations,
                    scenario,
                    bootstrap_seed=config.seed + size * 10_000 + scenario_index * 200,
                    config=config,
                )
            )
        results_by_size[str(size)] = {
            "trial_seeds": [trial.seed for trial in trials],
            "baseline_plain_change": _baseline_summary(
                trials,
                config,
                bootstrap_seed=config.seed + size * 20_000,
            ),
            "controls": [
                _no_post_funding_movement_exact_close_control(
                    trials,
                    config,
                    bootstrap_seed=config.seed + size * 30_000,
                ),
                {
                    "name": "fully_informed_coordinator",
                    "interpretation": "A coordinator that knows every owner-to-vertex mapping has anonymity set size 1 by definition.",
                    "mean_feasible_owner_count": 1.0,
                    "uniquely_identified_fraction": 1.0,
                    "mean_effective_anonymity": 1.0,
                },
            ],
            "sweep": rows,
        }
    return {
        "experiment": "bounded_ring_residual_attribution",
        "seed": config.seed,
        "matched_trials_per_size": config.trials,
        "sizes": list(config.sizes),
        "synthetic_residual_model": "Normal(100000, 20000) sat samples rounded to 1000 sat, floored at 20000 sat.",
        "planner": "taker.ring_planner.plan_cofunded_ring with injected seeded random.Random",
        "funding_model": {
            "topology": "Directed pseudonymous ring with labeled vertices and public capacities.",
            "observer_knows": "Exact owner residuals after input clustering and fees, but not owner-to-vertex mapping.",
            "limits": "Homogeneous loose capacities and pushes; only 1000 sat incoming/outgoing floors bind.",
            "funding_weight": "Each feasible assignment is weighted by its inclusive integer x_0 interval length.",
            "posterior_metrics": "mean_realized_label_posterior scores the generated label and is not attacker accuracy; mean_bayes_vulnerability is the mean per-vertex maximum posterior.",
            "primary_contrasts": "Funding-only all-zero row reports paired ring-minus-baseline effective anonymity and baseline-minus-ring Bayes vulnerability by seed.",
        },
        "close_model": {
            "known_close_fee": KNOWN_CLOSE_FEE,
            "orientation": "Balances are opener-side balances on directed edges; known close fees are normalized out.",
            "movement": "For B>0, generated shifts are nonreversed and bounded by B and the opener balance. Observer constraints use x_i in current_i +/- B.",
            "force_close": "Force closes use the same balance observation. Endpoint identity disclosure is an optional explicit worst-case bound, not an inherent chain fact.",
        },
        "posterior_policy": "Rows with any balance or identity disclosure are SUPPORT ONLY. No close or movement likelihood convolution is claimed.",
        "caveats": [
            "Known topology labels may have rotational symmetry; treating vertices as distinct labels can overstate attribution when no external anchor breaks that symmetry.",
            "Equal transaction outputs are not measured here.",
            "Support is not a real-world expected anonymity estimate.",
            "The synthetic normal-like residual model is noncalibrated and makes no population claim.",
            "The exact-close control assumes no post-funding movement. Reversed and circular flows have no likelihood model here.",
            "This experiment is not a production privacy proof.",
        ],
        "analytical_null_controls": [equal_residual_circular_flow_null_control()],
        "results_by_size": results_by_size,
    }


def _parse_csv_floats(value: str) -> tuple[float, ...]:
    try:
        return tuple(float(part) for part in value.split(",") if part)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a comma-separated numeric list"
        ) from exc


def _parse_csv_ints(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split(",") if part)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a comma-separated integer list"
        ) from exc


def _output_path(value: str) -> Path:
    root = Path(__file__).resolve().parents[1]
    allowed = (root / "tmp").resolve()
    candidate = Path(value).resolve()
    if not candidate.is_relative_to(allowed):
        raise argparse.ArgumentTypeError(
            "JSON output must be under the repository tmp/ directory"
        )
    return candidate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS)
    parser.add_argument("--sizes", type=_parse_csv_ints, default=DEFAULT_SIZES)
    parser.add_argument(
        "--closure-fractions", type=_parse_csv_floats, default=DEFAULT_CLOSURE_FRACTIONS
    )
    parser.add_argument(
        "--force-shares", type=_parse_csv_floats, default=DEFAULT_FORCE_SHARES
    )
    parser.add_argument(
        "--collusion-fractions",
        type=_parse_csv_floats,
        default=DEFAULT_COLLUSION_FRACTIONS,
    )
    parser.add_argument(
        "--movement-factors", type=_parse_csv_floats, default=DEFAULT_MOVEMENT_FACTORS
    )
    parser.add_argument(
        "--bootstrap-resamples", type=int, default=DEFAULT_BOOTSTRAP_RESAMPLES
    )
    parser.add_argument("--force-endpoint-identity-leak", action="store_true")
    parser.add_argument("--output", type=_output_path)
    parser.add_argument(
        "--smoke", action="store_true", help="run one non-reporting smoke trial"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.smoke:
        config = ExperimentConfig(
            seed=args.seed,
            trials=1,
            sizes=(4,),
            closure_fractions=(0.0,),
            force_shares=(0.0,),
            collusion_fractions=(0.0,),
            movement_factors=(0.0,),
            bootstrap_resamples=1,
            smoke=True,
        )
    else:
        config = ExperimentConfig(
            seed=args.seed,
            trials=args.trials,
            sizes=args.sizes,
            closure_fractions=args.closure_fractions,
            force_shares=args.force_shares,
            collusion_fractions=args.collusion_fractions,
            movement_factors=args.movement_factors,
            bootstrap_resamples=args.bootstrap_resamples,
            force_endpoint_identity_leak=args.force_endpoint_identity_leak,
        )
    result = run_experiment(config)
    encoded = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="ascii")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
