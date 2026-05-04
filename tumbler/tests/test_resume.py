"""Tests for ``--resume`` recovery from terminal-state plans.

When a plan ends in FAILED/CANCELLED -- or its previous process crashed
mid-run leaving the plan stuck in RUNNING -- the operator must be able to
re-attach to it and continue with the remaining phases instead of being
forced to start over with a fresh plan and a fresh PoDLE budget.

These tests cover both layers:
- ``_reset_plan_for_resume``: the pure rollback logic (preserves COMPLETED
  phases and ``attempt_count``, rewinds everything else to PENDING, advances
  ``current_phase`` to the first non-completed phase).
- ``run --resume``: the CLI wiring that gates resume behind the explicit
  flag and rejects it on COMPLETED plans (nothing to resume).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.cli import _reset_plan_for_resume, app
from tumbler.persistence import load_plan, save_plan
from tumbler.plan import PhaseStatus, Plan, PlanStatus

runner = CliRunner()


def _build_plan(wallet_name: str = "jm_abc12345_regtest") -> Plan:
    """Build a small, deterministic plan we can mutate in tests."""
    params = TumbleParameters(
        destinations=[
            "bcrt1qdest0000000000000000000000000000000000aaa",
            "bcrt1qdest0000000000000000000000000000000000bbb",
            "bcrt1qdest0000000000000000000000000000000000ccc",
        ],
        mixdepth_balances={0: 1_000_000, 1: 500_000, 2: 0, 3: 0, 4: 0},
        seed=1,
    )
    return PlanBuilder(wallet_name, params).build()


class _FakeSettings:
    """Same minimal settings stand-in used by the rest of the tumbler CLI tests."""

    class _Net:
        def __init__(self, network: str) -> None:
            class _N:
                value = network

            self.network = _N()
            self.bitcoin_network = None

    class _Bitcoin:
        def __init__(self, backend_type: str) -> None:
            self.backend_type = backend_type

    def __init__(self, data_dir: Path, network: str = "regtest", backend: str = "") -> None:
        self._data_dir = data_dir
        self.network_config = self._Net(network)
        self.bitcoin = self._Bitcoin(backend)

    def get_data_dir(self) -> Path:
        return self._data_dir


class TestResetPlanForResume:
    def test_rolls_back_failed_and_running_keeps_completed(self) -> None:
        """COMPLETED phases are sacred; FAILED/RUNNING/CANCELLED rewind to PENDING.

        ``current_phase`` must land on the first non-completed phase so the
        runner picks up exactly where the previous attempt died, not at
        phase 0.
        """
        plan = _build_plan()
        assert len(plan.phases) >= 4, "test setup expects >=4 phases"

        # Construct a realistic post-crash state:
        #   phase 0: COMPLETED (must stay completed)
        #   phase 1: COMPLETED
        #   phase 2: FAILED with error + timestamps + attempts  (rollback)
        #   phase 3: RUNNING (stuck after crash)                 (rollback)
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        plan.phases[0].status = PhaseStatus.COMPLETED
        plan.phases[1].status = PhaseStatus.COMPLETED
        plan.phases[2].status = PhaseStatus.FAILED
        plan.phases[2].error = "boom"
        plan.phases[2].started_at = now
        plan.phases[2].finished_at = now
        plan.phases[2].attempt_count = 3
        plan.phases[3].status = PhaseStatus.RUNNING
        plan.phases[3].started_at = now
        plan.phases[3].attempt_count = 1
        plan.status = PlanStatus.FAILED
        plan.error = "boom"
        plan.current_phase = 2

        rolled = _reset_plan_for_resume(plan)

        assert rolled == 2  # phase 2 + phase 3
        assert plan.phases[0].status == PhaseStatus.COMPLETED
        assert plan.phases[1].status == PhaseStatus.COMPLETED
        assert plan.phases[2].status == PhaseStatus.PENDING
        assert plan.phases[3].status == PhaseStatus.PENDING
        # Error/timestamps cleared on rolled-back phases.
        assert plan.phases[2].error is None
        assert plan.phases[2].started_at is None
        assert plan.phases[2].finished_at is None
        assert plan.phases[3].started_at is None
        # attempt_count is preserved so retry budgets carry across resumes.
        assert plan.phases[2].attempt_count == 3
        assert plan.phases[3].attempt_count == 1
        # Plan-level state reset; current_phase points at the first
        # non-completed phase (index 2), not the start of the plan.
        assert plan.status == PlanStatus.PENDING
        assert plan.error is None
        assert plan.current_phase == 2

    def test_all_completed_advances_current_phase_past_end(self) -> None:
        """If every phase is already completed, ``_reset_plan_for_resume``
        should park ``current_phase`` past the end so the runner immediately
        sees the plan as done -- this is the safety check behind the
        "nothing to resume" CLI rejection of COMPLETED plans.
        """
        plan = _build_plan()
        for ph in plan.phases:
            ph.status = PhaseStatus.COMPLETED

        rolled = _reset_plan_for_resume(plan)

        assert rolled == 0
        assert plan.current_phase == len(plan.phases)
        assert plan.status == PlanStatus.PENDING


class TestRunResumeFlag:
    def test_terminal_plan_without_resume_flag_is_rejected(self, tmp_path: Path) -> None:
        """A FAILED plan must not auto-restart -- the operator has to
        explicitly opt in with ``--resume`` so they don't accidentally
        re-run a plan they intended to delete and rebuild.
        """
        plan = _build_plan()
        plan.status = PlanStatus.FAILED
        plan.error = "boom"
        save_plan(plan, tmp_path)

        settings = _FakeSettings(tmp_path)

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None

        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value=plan.wallet_name),
            patch("tumbler.cli._run_plan") as m_run,
        ):
            result = runner.invoke(app, ["run", "-w", plan.wallet_name])

        assert result.exit_code == 1
        # Runner must NOT have been invoked: the terminal-state guard
        # short-circuits before we touch the runner.
        m_run.assert_not_called()
        # Plan on disk is untouched.
        reloaded = load_plan(plan.wallet_name, tmp_path)
        assert reloaded is not None
        assert reloaded.status == PlanStatus.FAILED

    def test_resume_on_completed_plan_is_rejected(self, tmp_path: Path) -> None:
        """``--resume`` must refuse to operate on an already-COMPLETED plan;
        otherwise we'd silently re-CoinJoin nothing or, worse, replay phases
        the user thought were finished.
        """
        plan = _build_plan()
        for ph in plan.phases:
            ph.status = PhaseStatus.COMPLETED
        plan.status = PlanStatus.COMPLETED
        save_plan(plan, tmp_path)

        settings = _FakeSettings(tmp_path)

        class _Resolved:
            mnemonic = "abandon " * 11 + "about"
            bip39_passphrase = ""
            creation_height = None

        with (
            patch("tumbler.cli.setup_cli", return_value=settings),
            patch("tumbler.cli.ensure_config_file"),
            patch("tumbler.cli.resolve_mnemonic", return_value=_Resolved()),
            patch("tumbler.cli._wallet_name_from_mnemonic", return_value=plan.wallet_name),
            patch("tumbler.cli._run_plan") as m_run,
        ):
            result = runner.invoke(app, ["run", "-w", plan.wallet_name, "--resume"])

        assert result.exit_code == 1
        m_run.assert_not_called()
        # Plan stays COMPLETED on disk -- _reset_plan_for_resume must NOT
        # have run.
        reloaded = load_plan(plan.wallet_name, tmp_path)
        assert reloaded is not None
        assert reloaded.status == PlanStatus.COMPLETED
        assert all(ph.status == PhaseStatus.COMPLETED for ph in reloaded.phases)
