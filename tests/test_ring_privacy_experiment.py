from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "ring_privacy_experiment.py"


def _load_experiment() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ring_privacy_experiment", SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_known_toy_derives_all_integer_splits() -> None:
    experiment = _load_experiment()
    interval = experiment.assignment_interval(
        (0, 1, 2, 3),
        (6, 7, 8, 9),
        (6, 7, 8, 9),
        outgoing_floor=1,
        incoming_floor=1,
    )

    assert interval is not None
    assert (interval.lower, interval.upper, interval.length) == (1, 5, 5)
    for x0 in range(interval.lower, interval.upper + 1):
        outgoing = (x0, x0 + 1, x0 + 2, x0 + 3)
        assert all(
            1 <= value <= residual - 1
            for value, residual in zip(outgoing, (6, 7, 8, 9))
        )
        assert tuple(
            outgoing[index] + (6, 7, 8, 9)[(index + 1) % 4] - outgoing[(index + 1) % 4]
            for index in range(4)
        ) == (6, 7, 8, 9)


def test_ground_truth_is_in_funding_support() -> None:
    experiment = _load_experiment()
    trial = experiment.generate_trial(71, 4)
    support = experiment.enumerate_feasible_assignments(
        trial.residuals_by_owner,
        trial.capacities,
    )

    assert any(item.owner_by_vertex == trial.true_owner_by_vertex for item in support)


def test_disclosures_cannot_increase_support() -> None:
    experiment = _load_experiment()
    trial = experiment.generate_trial(72, 4)
    base_support = experiment.enumerate_feasible_assignments(
        trial.residuals_by_owner,
        trial.capacities,
    )
    observation = experiment.build_observation(
        trial,
        experiment.Scenario(0.5, 1.0, 0.25, 0.1),
        random_seed=73,
        force_endpoint_identity_leak=True,
    )
    disclosed_support = experiment.enumerate_feasible_assignments(
        trial.residuals_by_owner,
        trial.capacities,
        balance_bounds=observation.balance_bounds,
        known_owner_by_vertex=observation.known_owner_by_vertex,
    )

    assert len(disclosed_support) <= len(base_support)
    assert any(
        item.owner_by_vertex == trial.true_owner_by_vertex for item in disclosed_support
    )


def test_no_post_funding_movement_exact_close_has_no_unique_residual_ambiguity() -> (
    None
):
    experiment = _load_experiment()
    residuals = (6, 7, 8, 9)
    capacities = (6, 7, 8, 9)
    observation = {
        vertex: experiment.IntegerInterval(vertex + 2, vertex + 2)
        for vertex in range(4)
    }
    support = experiment.enumerate_feasible_assignments(
        residuals,
        capacities,
        outgoing_floor=1,
        incoming_floor=1,
        balance_bounds=observation,
    )
    metrics = experiment.support_metrics(
        support,
        (0, 1, 2, 3),
        frozenset(),
        funding_posterior_valid=False,
    )

    assert len(support) == 1
    assert metrics["mean_feasible_owner_count"] == 1.0
    assert metrics["uniquely_identified_fraction"] == 1.0


def test_equal_residual_circular_flow_is_an_owner_support_null() -> None:
    experiment = _load_experiment()
    control = experiment.equal_residual_circular_flow_null_control()

    assert control["base_support_assignments"] == 24
    assert control["fully_closed_support_assignments"] == 24
    assert control["base_feasible_owners_per_vertex"] == 4
    assert control["fully_closed_feasible_owners_per_vertex"] == 4


def test_plain_change_baseline_retains_residual_collisions() -> None:
    experiment = _load_experiment()
    metrics = experiment.baseline_plain_change_metrics((100, 200, 100, 300))

    assert metrics == {
        "mean_feasible_owner_count": 1.5,
        "uniquely_identified_fraction": 0.5,
        "mean_effective_anonymity": 1.5,
        "mean_bayes_vulnerability": 0.75,
    }


def test_funding_metrics_include_known_bayes_vulnerability() -> None:
    experiment = _load_experiment()
    support = [
        experiment.AssignmentSupport((0, 1), experiment.IntegerInterval(1, 3)),
        experiment.AssignmentSupport((1, 0), experiment.IntegerInterval(1, 1)),
    ]
    metrics = experiment.support_metrics(
        support,
        (0, 1),
        frozenset(),
        funding_posterior_valid=True,
    )

    assert metrics["mean_realized_label_posterior"] == 0.75
    assert metrics["mean_bayes_vulnerability"] == 0.75


def test_primary_paired_contrasts_are_deterministic_seed_level_differences() -> None:
    experiment = _load_experiment()
    ring = [
        {"mean_effective_anonymity": 3.0, "mean_bayes_vulnerability": 0.2},
        {"mean_effective_anonymity": 4.0, "mean_bayes_vulnerability": 0.1},
    ]
    baseline = [
        {"mean_effective_anonymity": 1.0, "mean_bayes_vulnerability": 0.8},
        {"mean_effective_anonymity": 2.0, "mean_bayes_vulnerability": 0.6},
    ]
    first = experiment.primary_paired_contrasts(
        ring,
        baseline,
        bootstrap_seed=101,
        resamples=9,
    )
    second = experiment.primary_paired_contrasts(
        ring,
        baseline,
        bootstrap_seed=101,
        resamples=9,
    )

    assert first == second
    assert first["ring_minus_baseline_effective_anonymity"]["mean"] == 2.0
    assert first["baseline_minus_ring_bayes_vulnerability"]["mean"] == 0.55


def test_force_share_is_sampled_for_each_closed_edge() -> None:
    experiment = _load_experiment()
    trial = experiment.generate_trial(74, 4)
    observations = [
        experiment.build_observation(
            trial,
            experiment.Scenario(1.0, 0.1, 0.0, 0.0),
            random_seed=seed,
        )
        for seed in range(100)
    ]
    force_count = sum(
        len(observation.force_closed_vertices) for observation in observations
    )

    assert 0 < force_count < len(observations) * trial.size


def test_smoke_run_is_deterministic() -> None:
    experiment = _load_experiment()
    config = experiment.ExperimentConfig(
        seed=77,
        trials=1,
        sizes=(4,),
        closure_fractions=(0.0, 1.0),
        force_shares=(0.0,),
        collusion_fractions=(0.0,),
        movement_factors=(0.0,),
        bootstrap_resamples=5,
        smoke=True,
    )

    assert experiment.run_experiment(config) == experiment.run_experiment(config)


def test_cli_smoke() -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--smoke", "--seed", "78"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    result = json.loads(completed.stdout)

    assert result["matched_trials_per_size"] == 1
    assert result["sizes"] == [4]
