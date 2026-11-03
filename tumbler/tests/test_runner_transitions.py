"""
Tests for :class:`tumbler.runner.TumbleRunner`.

These tests fake out the taker and maker so we can drive the runner's state
machine deterministically, focusing on:

* phase/plan status transitions,
* YAML persistence on every state change,
* cancellation propagation between phases,
* the guarantee that per-phase teardown runs even on exception.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from taker.podle_manager import CommitmentHistoryError

from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.persistence import load_plan, save_plan
from tumbler.plan import (
    MakerSessionPhase,
    PhaseStatus,
    Plan,
    PlanStatus,
    TakerCoinjoinPhase,
)
from tumbler.runner import RunnerContext, TumbleRunner

# --------------------------------------------------------------------------- fakes


class FakeWalletService:
    name = "TestWallet"

    def __init__(self, balance_sats: int = 5_000_000, mixdepth_count: int = 5) -> None:
        self._counter = 0
        self.mixdepth_count = mixdepth_count
        # Per-(mixdepth, change) index counters mirror WalletService behaviour.
        self._next_index: dict[tuple[int, int], int] = {}
        # Per-mixdepth balance used by the runner's amount_fraction resolver.
        self._balances: dict[int, int] = {m: balance_sats for m in range(mixdepth_count)}
        self._coinjoin_balances: dict[int, int] = dict(self._balances)
        self._locked_outpoints: set[tuple[str, int]] = set()
        self.coinjoin_balance_calls: list[tuple[int, int, set[tuple[str, int]]]] = []

    def _check_mixdepth(self, mixdepth: int) -> None:
        # Mirror WalletService.get_address: out-of-range mixdepth is an error.
        if mixdepth >= self.mixdepth_count:
            raise ValueError(f"Mixdepth {mixdepth} exceeds maximum {self.mixdepth_count}")

    def get_next_address_index(self, mixdepth: int, change: int) -> int:
        self._check_mixdepth(mixdepth)
        idx = self._next_index.get((mixdepth, change), 0)
        self._next_index[(mixdepth, change)] = idx + 1
        return idx

    def get_change_address(self, mixdepth: int, index: int) -> str:
        self._check_mixdepth(mixdepth)
        self._counter += 1
        return f"bcrt1qfake{mixdepth}{index}{self._counter:04d}"

    def get_new_internal_address(self, mixdepth: int) -> str:
        index = self.get_next_address_index(mixdepth, 1)
        return self.get_change_address(mixdepth, index)

    async def get_balance(
        self,
        mixdepth: int,
        include_fidelity_bonds: bool = True,
        min_confirmations: int = 0,
    ) -> int:
        return self._balances.get(mixdepth, 0)

    async def get_coinjoin_balance(
        self,
        mixdepth: int,
        min_confirmations: int = 0,
        *,
        restrict_md0: bool = True,
        exclude: set[tuple[str, int]] | None = None,
    ) -> int:
        del restrict_md0
        excluded = exclude or set()
        self.coinjoin_balance_calls.append((mixdepth, min_confirmations, excluded))
        return self._coinjoin_balances.get(mixdepth, 0)

    def get_locked_input_outpoints(self) -> set[tuple[str, int]]:
        return set(self._locked_outpoints)


class FakeTakerResult:
    """Defensive path: fakes returning an object with a ``.txid`` attribute.

    The real ``Taker.do_coinjoin`` returns a ``str | None``, but the runner
    still has a defensive branch for non-string returns; this fixture exercises
    that branch in a dedicated test.
    """

    def __init__(self, txid: str) -> None:
        self.txid = txid


class FakeTaker:
    """Successful taker: records call and returns a deterministic txid string.

    Mirrors the real ``Taker.do_coinjoin`` signature
    ``(amount, destination, mixdepth=0, counterparty_count=None,
    penalized_maker_keys=None) -> str | None``. Accepting only those kwargs means
    the test suite fails loudly if the runner ever leaks a stray
    ``rounding`` / ``amount_fraction`` kwarg again.
    """

    def __init__(self, phase: Any) -> None:
        self.phase = phase
        self.started = False
        self.stopped_with: dict[str, Any] | None = None
        self.do_coinjoin_kwargs: dict[str, Any] | None = None
        self.state = "idle"
        self.last_failure_reason: str | None = None
        self.config = SimpleNamespace(taker_utxo_age=0)
        # Default successful maker identities. Tests override per-instance.
        self.last_used_nicks: set[str] = set()
        self.last_used_maker_keys: set[str] = set()

    async def start(self) -> None:
        self.started = True

    async def do_coinjoin(
        self,
        amount: int,
        destination: str,
        mixdepth: int = 0,
        counterparty_count: int | None = None,
        exclude_nicks: set[str] | None = None,
        penalized_maker_keys: set[str] | None = None,
    ) -> str | None:
        self.do_coinjoin_kwargs = {
            "amount": amount,
            "destination": destination,
            "mixdepth": mixdepth,
            "counterparty_count": counterparty_count,
            "exclude_nicks": exclude_nicks,
            "penalized_maker_keys": penalized_maker_keys,
        }
        # txid derived from inputs so tests can assert stable output.
        return f"txid-{mixdepth}-{amount}"

    async def stop(self, close_wallet: bool = True) -> None:
        self.stopped_with = {"close_wallet": close_wallet}


class ExplodingTaker(FakeTaker):
    async def do_coinjoin(
        self,
        amount: int,
        destination: str,
        mixdepth: int = 0,
        counterparty_count: int | None = None,
        exclude_nicks: set[str] | None = None,
        penalized_maker_keys: set[str] | None = None,
    ) -> str | None:
        raise RuntimeError("simulated failure")


class FakeMaker:
    def __init__(self, phase: MakerSessionPhase, *, run_seconds: float = 0.0) -> None:
        self.phase = phase
        self.run_seconds = run_seconds
        self.started = False
        self.stopped = False
        self.coinjoins_completed = 0

    async def start(self) -> None:
        self.started = True
        if self.run_seconds > 0:
            await asyncio.sleep(self.run_seconds)

    async def stop(self) -> None:
        self.stopped = True


# --------------------------------------------------------------------------- helpers


def _plan(tmp_path: Path, *, include_maker: bool = False, n_destinations: int = 1) -> Plan:
    params = TumbleParameters(
        destinations=[
            f"bcrt1qdest{i}000000000000000000000000000000000zzz" for i in range(n_destinations)
        ],
        mixdepth_balances={0: 5_000_000, 1: 0, 2: 0, 3: 0, 4: 0},
        seed=1,
        include_maker_sessions=include_maker,
        mintxcount=2,
    )
    plan = PlanBuilder("RunnerTest", params).build()
    # Zero out waits so the runner doesn't block the tests.
    for p in plan.phases:
        p.wait_seconds = 0.0
    save_plan(plan, tmp_path)
    return plan


def _ctx(
    tmp_path: Path,
    *,
    taker_factory: Any,
    maker_factory: Any | None = None,
    sleep: Any | None = None,
    retry_delay_seconds: float = 0.0,
) -> RunnerContext:
    async def zero_sleep(_: float) -> None:
        return None

    return RunnerContext(
        wallet_service=FakeWalletService(),  # type: ignore[arg-type]
        wallet_name="RunnerTest",
        data_dir=tmp_path,
        taker_factory=taker_factory,
        maker_factory=maker_factory,
        sleep=sleep or zero_sleep,
        retry_delay_seconds=retry_delay_seconds,
    )


# --------------------------------------------------------------------------- tests


class TestRunnerHappyPath:
    def test_runner_rejects_old_plan_invalid_for_active_wallet_network(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path)
        plan.destinations = ["bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"]
        ctx = _ctx(tmp_path, taker_factory=lambda _: FakeTaker(_))
        ctx.wallet_service.network = "regtest"

        with pytest.raises(ValueError, match="invalid destination"):
            TumbleRunner(plan, ctx)

    def test_runner_rejects_plan_for_different_wallet_network(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        plan.network = "mainnet"
        plan.destinations = ["bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"]
        ctx = _ctx(tmp_path, taker_factory=lambda _: FakeTaker(_))
        ctx.wallet_service.network = "regtest"

        with pytest.raises(ValueError, match="does not match active wallet network"):
            TumbleRunner(plan, ctx)

    async def test_taker_only_plan_completes(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        assert all(isinstance(p, TakerCoinjoinPhase) for p in plan.phases)

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        assert all(p.status == PhaseStatus.COMPLETED for p in result.phases)
        # Persistence: on disk matches in-memory.
        on_disk = load_plan("RunnerTest", tmp_path)
        assert on_disk.status == PlanStatus.COMPLETED

    async def test_internal_destination_is_resolved_from_wallet(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        # Use the first phase (stage-1 sweep to INTERNAL).
        stage1 = plan.phases[0]
        assert isinstance(stage1, TakerCoinjoinPhase)
        assert stage1.destination == "INTERNAL"

        seen: list[dict[str, Any]] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def capture(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str:
                seen.append(
                    {
                        "amount": amount,
                        "destination": destination,
                        "mixdepth": mixdepth,
                        "counterparty_count": counterparty_count,
                    }
                )
                return "tx"

            t.do_coinjoin = capture  # type: ignore[assignment]
            return t

        await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert seen[0]["destination"].startswith("bcrt1qfake")

    async def test_records_txid_on_phase(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert all(isinstance(p, TakerCoinjoinPhase) and p.txid is not None for p in plan.phases)


class TestRunnerFailure:
    @pytest.mark.parametrize("constructor_failure", [False, True])
    async def test_history_failure_is_persisted_without_retry(
        self,
        tmp_path: Path,
        constructor_failure: bool,
    ) -> None:
        plan = _plan(tmp_path)
        error = CommitmentHistoryError("restore trusted commitment history")
        takers: list[FakeTaker] = []
        factory_calls = 0

        async def make_taker(phase: Any) -> FakeTaker:
            nonlocal factory_calls
            factory_calls += 1
            if constructor_failure:
                raise error
            taker = FakeTaker(phase)
            taker.do_coinjoin = AsyncMock(side_effect=error)  # type: ignore[method-assign]
            takers.append(taker)
            return taker

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        runner._try_tweak_for_retry = AsyncMock(return_value=False)  # type: ignore[method-assign]
        result = await runner.run()

        assert result.status == PlanStatus.FAILED
        assert result.error == str(error)
        assert result.phases[0].status == PhaseStatus.FAILED
        assert result.phases[0].error == str(error)
        assert result.phases[0].attempt_count == 0
        assert factory_calls == 1
        runner._try_tweak_for_retry.assert_not_awaited()
        persisted = load_plan(result.wallet_name, tmp_path)
        assert persisted.status == PlanStatus.FAILED
        assert persisted.error == str(error)
        if takers:
            assert takers[0].stopped_with == {"close_wallet": False}

    async def test_phase_failure_marks_plan_failed(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            return ExplodingTaker(phase)

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.FAILED
        assert result.phases[0].status == PhaseStatus.FAILED
        assert "simulated failure" in (result.phases[0].error or "")
        # Subsequent phases remain pending.
        assert all(p.status == PhaseStatus.PENDING for p in result.phases[1:])

    async def test_taker_stop_called_even_on_failure(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        takers: list[ExplodingTaker] = []

        async def make_taker(phase: Any) -> ExplodingTaker:
            t = ExplodingTaker(phase)
            takers.append(t)
            return t

        await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert takers[0].stopped_with == {"close_wallet": False}


class TestRunnerRetry:
    """
    A failed taker-coinjoin phase is rearmed unchanged (with an optional retry
    delay) up to ``max_phase_retries`` times before failing the whole plan.
    """

    async def test_retry_succeeds_on_second_attempt(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        # Keep retry budget at default (3). Pick the first phase and
        # verify attempt_count starts at 0.
        assert plan.phases[0].attempt_count == 0

        attempts: list[dict[str, Any]] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def flaky(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                attempts.append(
                    {
                        "phase_index": phase.index,
                        "destination": destination,
                        "counterparty_count": counterparty_count,
                    }
                )
                # Fail the first attempt of phase 0, succeed afterwards.
                if phase.index == 0 and phase.attempt_count == 0:
                    t.state = "failed"
                    t.last_failure_reason = "No eligible UTXOs in mixdepth 1"
                    return None  # signals TakerPhaseError inside runner
                return f"txid-{phase.index}-{phase.attempt_count}"

            t.do_coinjoin = flaky  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        assert result.phases[0].status == PhaseStatus.COMPLETED
        assert result.phases[0].attempt_count == 1
        # Phase 0 was attempted twice; later phases only once each.
        phase_0_attempts = [a for a in attempts if a["phase_index"] == 0]
        assert len(phase_0_attempts) == 2

    async def test_retry_preserves_nonterminal_external_destination(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path, n_destinations=2)
        # Find the first phase that targets an external destination.
        target = next(
            p
            for p in plan.phases
            if isinstance(p, TakerCoinjoinPhase) and p.destination != "INTERNAL"
        )
        target_index = target.index
        original_destination = target.destination
        assert original_destination.startswith("bcrt1q")

        observed: list[str] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def flaky(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                if phase.index == target_index:
                    observed.append(destination)
                    if phase.attempt_count == 0:
                        t.state = "failed"
                        t.last_failure_reason = "maker negotiation failed"
                        return None
                return f"txid-{phase.index}"

            t.do_coinjoin = flaky  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        assert observed == [original_destination, original_destination]
        assert result.phases[target_index].destination == original_destination

    async def test_retry_keeps_terminal_external_destination(self, tmp_path: Path) -> None:
        """The plan's final external sweep remains unchanged on retry."""
        plan = _plan(tmp_path)
        # With one destination the only external phase is the terminal sweep.
        target = next(
            p
            for p in plan.phases
            if isinstance(p, TakerCoinjoinPhase) and p.destination != "INTERNAL"
        )
        assert target.index == plan.phases[-1].index
        target_index = target.index
        original_destination = target.destination

        observed: list[str] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def flaky(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                if phase.index == target_index:
                    observed.append(destination)
                    if phase.attempt_count == 0:
                        t.state = "failed"
                        t.last_failure_reason = "maker negotiation failed"
                        return None
                return f"txid-{phase.index}"

            t.do_coinjoin = flaky  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        # Both attempts targeted the external address; no INTERNAL swap.
        assert observed == [original_destination, original_destination]
        assert result.phases[target_index].destination == original_destination

    async def test_retry_keeps_counterparty_count_unchanged(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        stage1 = plan.phases[0]
        assert isinstance(stage1, TakerCoinjoinPhase)
        stage1.counterparty_count = plan.parameters.maker_count_min + 2

        seen_cp: list[int | None] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def flaky(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                if phase.index == 0:
                    seen_cp.append(counterparty_count)
                    if phase.attempt_count == 0:
                        t.state = "failed"
                        t.last_failure_reason = "temporary maker failure"
                        return None
                return "tx"

            t.do_coinjoin = flaky  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        # Two calls: first at the original count, second one unchanged.
        assert len(seen_cp) == 2
        assert seen_cp[0] == plan.parameters.maker_count_min + 2
        assert seen_cp[1] == plan.parameters.maker_count_min + 2

    async def test_retry_waits_before_rearming_phase(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def flaky(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                if phase.index == 0 and phase.attempt_count == 0:
                    t.state = "failed"
                    t.last_failure_reason = (
                        "No eligible UTXOs in mixdepth 1; wait for more confirmations"
                    )
                    return None
                return "tx"

            t.do_coinjoin = flaky  # type: ignore[assignment]
            return t

        runner = TumbleRunner(
            plan,
            _ctx(
                tmp_path,
                taker_factory=make_taker,
                sleep=fake_sleep,
                retry_delay_seconds=7.0,
            ),
        )
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        # One retry after the first failure -> 7s backoff logged through sleep.
        assert 7.0 in sleeps

    async def test_retry_waits_for_low_confirmation_failures(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def flaky(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                if phase.index == 0 and phase.attempt_count == 0:
                    t.state = "failed"
                    t.last_failure_reason = (
                        "No eligible UTXOs in mixdepth 1; wait for more confirmations"
                    )
                    return None
                return "tx"

            t.do_coinjoin = flaky  # type: ignore[assignment]
            return t

        runner = TumbleRunner(
            plan,
            _ctx(
                tmp_path,
                taker_factory=make_taker,
                sleep=fake_sleep,
                retry_delay_seconds=11.0,
            ),
        )
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        assert 11.0 in sleeps

    async def test_retry_budget_exhaustion_fails_plan(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        # Tighten the retry budget to keep the test quick and explicit.
        plan.parameters = plan.parameters.model_copy(update={"max_phase_retries": 2})

        call_counter = {"n": 0}

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def always_fail(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                call_counter["n"] += 1
                t.state = "failed"
                t.last_failure_reason = "persistent failure"
                return None  # always fails → TakerPhaseError

            t.do_coinjoin = always_fail  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.FAILED
        assert result.phases[0].status == PhaseStatus.FAILED
        # One initial attempt + ``max_phase_retries`` retries = 3 calls.
        assert call_counter["n"] == 3
        assert result.phases[0].attempt_count == 2
        # Subsequent phases never ran.
        assert all(p.status == PhaseStatus.PENDING for p in result.phases[1:])

    async def test_retry_budget_zero_disables_retries(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        plan.parameters = plan.parameters.model_copy(update={"max_phase_retries": 0})

        calls = {"n": 0}

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def always_fail(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                calls["n"] += 1
                t.state = "failed"
                t.last_failure_reason = "persistent failure"
                return None

            t.do_coinjoin = always_fail  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.FAILED
        assert calls["n"] == 1
        assert result.phases[0].attempt_count == 0


class TestTeardownReleasesInputLocks:
    """Persisted input locks from a failed round must not leak across phases.

    Locks live in the wallet metadata file with a multi-minute TTL, so a
    leaked lock surfaces on the next attempt as "No eligible UTXOs ... locked
    by another in-flight CoinJoin" until the TTL expires.
    """

    async def test_failed_taker_releases_locks_on_teardown(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        plan.parameters = plan.parameters.model_copy(update={"max_phase_retries": 0})
        released: list[FakeTaker] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def always_fail(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                t.state = "failed"
                t.last_failure_reason = "persistent failure"
                return None

            t.do_coinjoin = always_fail  # type: ignore[assignment]
            t.release_input_locks = lambda: released.append(t)  # type: ignore[attr-defined]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.FAILED
        assert len(released) == 1

    async def test_completed_taker_keeps_locks(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        released: list[FakeTaker] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)
            original = t.do_coinjoin

            async def succeed(*args: Any, **kwargs: Any) -> str | None:
                t.state = "complete"
                return await original(*args, **kwargs)

            t.do_coinjoin = succeed  # type: ignore[assignment]
            t.release_input_locks = lambda: released.append(t)  # type: ignore[attr-defined]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        # Inputs are spent on success; locks are left to expire on their own.
        assert released == []


class TestRunnerCancellation:
    async def test_request_stop_between_phases(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))

        # Request stop after the first phase persists.
        original = runner._persist  # type: ignore[attr-defined]
        calls = {"n": 0}

        def hooked() -> None:
            original()
            calls["n"] += 1
            if calls["n"] == 3:  # after the first phase transitions
                runner.request_stop()

        runner._persist = hooked  # type: ignore[attr-defined,method-assign]
        result = await runner.run()
        assert result.status == PlanStatus.CANCELLED
        # At least the first phase completed before cancel.
        assert any(p.status == PhaseStatus.COMPLETED for p in result.phases)

    async def test_request_stop_during_retry_backoff_cancels_plan(self, tmp_path: Path) -> None:
        """A stop arriving during the retry back-off must cancel cleanly.

        Regression: previously ``_try_tweak_for_retry`` invoked
        ``_wait_interruptibly`` without catching ``_StopRequestedError``, so a
        stop that arrived between a phase failure and its retry crashed the
        runner with an unhandled exception, leaving the plan in ``FAILED``
        (and surfacing as "tumbler runner crashed" in jmwalletd). The plan
        should instead transition to ``CANCELLED``.
        """
        plan = _plan(tmp_path)

        async def fake_sleep(seconds: float) -> None:
            # Yield control so the stop flag can be observed mid-wait.
            await asyncio.sleep(0)

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def always_fail(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                t.state = "failed"
                t.last_failure_reason = "Not enough offers: need 2, found 0"
                return None

            t.do_coinjoin = always_fail  # type: ignore[assignment]
            return t

        runner = TumbleRunner(
            plan,
            _ctx(
                tmp_path,
                taker_factory=make_taker,
                sleep=fake_sleep,
                retry_delay_seconds=5.0,
            ),
        )

        # Trip the stop flag as soon as the retry path persists its
        # ``attempt_count`` bump, which happens just before the back-off wait.
        original = runner._persist  # type: ignore[attr-defined]
        stopped = {"done": False}

        def hooked() -> None:
            original()
            if not stopped["done"] and plan.phases[0].attempt_count >= 1:
                stopped["done"] = True
                runner.request_stop()

        runner._persist = hooked  # type: ignore[attr-defined,method-assign]
        result = await runner.run()

        assert result.status == PlanStatus.CANCELLED
        assert result.phases[0].status == PhaseStatus.CANCELLED

    async def test_stop_and_wait_force_cancels_stuck_phase(self, tmp_path: Path) -> None:
        """``stop_and_wait`` must not hang when a phase is stuck deep in a
        network exchange that never polls the stop event. After the grace
        period it force-cancels the task and marks the plan CANCELLED.
        """
        plan = _plan(tmp_path)
        started = asyncio.Event()

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def hang(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
                exclude_nicks: set[str] | None = None,
                penalized_maker_keys: set[str] | None = None,
            ) -> str | None:
                started.set()
                # Stuck forever; does not observe the cooperative stop event.
                await asyncio.sleep(3600)
                return None

            t.do_coinjoin = hang  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), timeout=1.0)

        result = await asyncio.wait_for(runner.stop_and_wait(task, grace_period=0.1), timeout=2.0)

        assert result.status == PlanStatus.CANCELLED
        assert task.done()
        # The stuck phase is recorded as cancelled, not left RUNNING.
        assert plan.phases[0].status == PhaseStatus.CANCELLED

    async def test_stop_and_wait_returns_plan_when_task_already_done(self, tmp_path: Path) -> None:
        """If the task already finished, ``stop_and_wait`` returns its plan
        as-is without forcing a status change."""
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(task, timeout=2.0)
        assert task.result().status == PlanStatus.COMPLETED

        result = await asyncio.wait_for(runner.stop_and_wait(task), timeout=2.0)
        assert result.status == PlanStatus.COMPLETED

    async def test_stop_grace_period_includes_hanging_teardown(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        started = asyncio.Event()

        class HangingStopTaker(FakeTaker):
            async def do_coinjoin(self, *args: Any, **kwargs: Any) -> str | None:
                started.set()
                await asyncio.sleep(3600)
                return None

            async def stop(self, close_wallet: bool = True) -> None:
                await asyncio.sleep(3600)

        async def make_taker(phase: Any) -> HangingStopTaker:
            return HangingStopTaker(phase)

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), timeout=1.0)

        result = await asyncio.wait_for(runner.stop_and_wait(task, grace_period=0.1), timeout=1.0)

        assert result.status == PlanStatus.CANCELLED
        assert task.done()
        assert load_plan("RunnerTest", tmp_path).status == PlanStatus.CANCELLED

    async def test_cancelling_stop_persists_cancelled_after_runner_stops(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path)
        started = asyncio.Event()

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def hang(*args: Any, **kwargs: Any) -> str | None:
                started.set()
                await asyncio.sleep(3600)
                return None

            t.do_coinjoin = hang  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        task = asyncio.create_task(runner.run())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        stop_task = asyncio.create_task(runner.stop_and_wait(task, grace_period=30.0))
        await asyncio.sleep(0)
        stop_task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        with contextlib.suppress(asyncio.CancelledError):
            await task

        persisted = load_plan("RunnerTest", tmp_path)
        assert persisted.status == PlanStatus.CANCELLED
        assert persisted.phases[0].status == PhaseStatus.CANCELLED

    async def test_cancelling_stop_keeps_running_state_if_runner_ignores_cancel(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        plan.status = PlanStatus.RUNNING
        runner._persist()
        started = asyncio.Event()
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()

        async def stubborn_runner() -> Plan:
            started.set()
            while not release.is_set():
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancellation_seen.set()
            return plan

        task = asyncio.create_task(stubborn_runner())
        await started.wait()
        stop_task = asyncio.create_task(runner.stop_and_wait(task, grace_period=1.0))
        await asyncio.wait_for(cancellation_seen.wait(), timeout=2.0)
        stop_task.cancel()

        try:
            try:
                await stop_task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("stop cancellation was not propagated")
            assert not task.done()
            assert load_plan("RunnerTest", tmp_path).status == PlanStatus.RUNNING
        finally:
            release.set()
            task.cancel()
            await asyncio.wait_for(task, timeout=1.0)


class TestRunnerMakerPhase:
    async def test_maker_phase_runs_until_duration(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path, include_maker=True)
        maker_phases = [p for p in plan.phases if isinstance(p, MakerSessionPhase)]
        assert maker_phases, "expected at least one maker session"
        # Give it a very short duration so the test is quick.
        for mp in maker_phases:
            mp.duration_seconds = 0.05
            mp.target_cj_count = None

        built: list[FakeMaker] = []

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        async def make_maker(phase: MakerSessionPhase) -> FakeMaker:
            m = FakeMaker(phase, run_seconds=10.0)
            built.append(m)
            return m

        # Real asyncio.sleep here so the 0.05s deadline elapses.
        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            maker_factory=make_maker,
        )
        result = await TumbleRunner(plan, ctx).run()
        assert result.status == PlanStatus.COMPLETED
        assert all(m.stopped for m in built)

    async def test_maker_phase_exits_on_idle_timeout(self, tmp_path: Path) -> None:
        """Maker phase should exit successfully when idle_timeout elapses with no CJ served."""
        plan = _plan(tmp_path, include_maker=True)
        maker_phases = [p for p in plan.phases if isinstance(p, MakerSessionPhase)]
        assert maker_phases, "expected at least one maker session"
        # Remove duration/target bounds (target must stay if duration is removed,
        # but the idle fallback must fire first). Keep a large target and no
        # duration, plus a short idle timeout.
        for mp in maker_phases:
            mp.duration_seconds = None
            mp.target_cj_count = 1_000_000
            mp.idle_timeout_seconds = 0.05

        built: list[FakeMaker] = []

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        async def make_maker(phase: MakerSessionPhase) -> FakeMaker:
            m = FakeMaker(phase, run_seconds=10.0)
            built.append(m)
            return m

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            maker_factory=make_maker,
        )
        result = await TumbleRunner(plan, ctx).run()
        assert result.status == PlanStatus.COMPLETED
        assert all(m.stopped for m in built)
        # No CJs were served and the target was not met; exit was due to idle.
        for mp in maker_phases:
            assert mp.cj_served == 0

    async def test_failed_maker_session_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """A crashing maker session must not fail the whole plan.

        Maker sessions are optional privacy enhancers; per the documented
        design a failed session is not retried and the runner proceeds with
        the next phase (marking the session SKIPPED, error preserved).
        """
        plan = _plan(tmp_path, include_maker=True)
        maker_phases = [p for p in plan.phases if isinstance(p, MakerSessionPhase)]
        assert maker_phases, "expected at least one maker session"
        for mp in maker_phases:
            mp.duration_seconds = 0.05

        class CrashingMaker(FakeMaker):
            async def start(self) -> None:
                raise RuntimeError("tor unavailable")

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        async def make_maker(phase: MakerSessionPhase) -> CrashingMaker:
            return CrashingMaker(phase)

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            maker_factory=make_maker,
        )
        result = await TumbleRunner(plan, ctx).run()

        assert result.status == PlanStatus.COMPLETED
        for mp in maker_phases:
            assert mp.status == PhaseStatus.SKIPPED
            assert mp.error is not None and "tor unavailable" in mp.error
        # All taker phases still ran to completion.
        taker_phases = [p for p in result.phases if isinstance(p, TakerCoinjoinPhase)]
        assert all(p.status == PhaseStatus.COMPLETED for p in taker_phases)


# --------------------------------------------------------------- taker-interop


class TestRunnerTakerInterop:
    """Regression tests for the runner -> Taker.do_coinjoin signature contract.

    The reference taker ``Taker.do_coinjoin(amount, destination, mixdepth,
    counterparty_count) -> str | None`` does *not* accept ``rounding`` or
    ``amount_fraction`` and returns the broadcast txid as a plain string.
    These tests lock in the runner's adherence to that contract.
    """

    async def test_runner_does_not_forward_rounding_or_amount_fraction(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path)
        captured: list[dict[str, Any]] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)
            real_do_coinjoin = t.do_coinjoin

            async def trace(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                captured.append(
                    {
                        "amount": amount,
                        "destination": destination,
                        "mixdepth": mixdepth,
                        "counterparty_count": counterparty_count,
                    }
                )
                return await real_do_coinjoin(amount, destination, mixdepth, counterparty_count)

            t.do_coinjoin = trace  # type: ignore[assignment]
            return t

        result = await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert result.status == PlanStatus.COMPLETED
        assert captured, "expected at least one taker invocation"
        # Runner must pass only the four taker-native kwargs. Any extra
        # kwarg (e.g. ``rounding``, ``amount_fraction``) would have raised
        # TypeError above when FakeTaker.do_coinjoin was called.
        for call in captured:
            assert set(call.keys()) == {
                "amount",
                "destination",
                "mixdepth",
                "counterparty_count",
            }
            # ``amount`` must be an int (int sats), never a float fraction.
            assert isinstance(call["amount"], int)

    async def test_runner_resolves_fraction_from_coinjoin_selectable_balance(
        self, tmp_path: Path
    ) -> None:
        """Fractional phases use the same capacity constraints as selection."""
        plan = _plan(tmp_path)
        fractional = next(
            (p for p in plan.phases if isinstance(p, TakerCoinjoinPhase) and p.amount_fraction),
            None,
        )
        assert fractional is not None, "expected a fractional phase in the plan"
        # Force a deterministic fraction and balance so we can assert exact sats.
        fractional.amount = None
        fractional.amount_fraction = 0.25

        wallet = FakeWalletService(balance_sats=4_000_000)
        wallet._coinjoin_balances[fractional.mixdepth] = 100_000
        wallet._locked_outpoints = {("aa" * 32, 1)}
        captured: list[dict[str, Any]] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)
            t.config.taker_utxo_age = 5

            async def trace(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                captured.append({"amount": amount, "mixdepth": mixdepth})
                return "tx-frac"

            t.do_coinjoin = trace  # type: ignore[assignment]
            return t

        async def zero_sleep(_: float) -> None:
            return None

        ctx = RunnerContext(
            wallet_service=wallet,  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            sleep=zero_sleep,
        )
        await TumbleRunner(plan, ctx).run()

        frac_calls = [c for c in captured if c["mixdepth"] == fractional.mixdepth]
        assert frac_calls, "expected a call for the fractional phase's mixdepth"
        # The ordinary wallet balance is 4M, but only 100k is selectable after
        # md0/bond/confirmation/lock constraints. Resolve 25% from that source.
        assert 25_000 in {c["amount"] for c in frac_calls}
        assert (
            fractional.mixdepth,
            5,
            {("aa" * 32, 1)},
        ) in wallet.coinjoin_balance_calls

    async def test_runner_accepts_plain_string_txid(self, tmp_path: Path) -> None:
        """Runner must treat ``do_coinjoin``'s str return value as the txid."""
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def always_string(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                return "deadbeef" * 8

            t.do_coinjoin = always_string  # type: ignore[assignment]
            return t

        result = await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert result.status == PlanStatus.COMPLETED
        taker_phases = [p for p in result.phases if isinstance(p, TakerCoinjoinPhase)]
        assert taker_phases
        assert all(p.txid == "deadbeef" * 8 for p in taker_phases)

    async def test_runner_treats_none_return_as_failure(self, tmp_path: Path) -> None:
        """``Taker.do_coinjoin`` returns ``None`` on CJ failure; runner must fail the phase."""
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def returns_none(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                return None

            t.do_coinjoin = returns_none  # type: ignore[assignment]
            return t

        result = await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert result.status == PlanStatus.FAILED
        assert result.phases[0].status == PhaseStatus.FAILED
        assert "no txid" in (result.phases[0].error or "")

    async def test_runner_clean_failure_has_no_traceback(self, tmp_path: Path, caplog: Any) -> None:
        """A ``None`` return must log as a plain error, not a traceback.

        The taker itself logs the underlying cause (e.g. "not enough compatible
        makers"). The runner should surface it without adding Python-exception
        noise on top.
        """
        import logging

        from tumbler.runner import TakerPhaseError

        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def returns_none(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                return None

            t.do_coinjoin = returns_none  # type: ignore[assignment]
            return t

        # Surface loguru records via the stdlib logging handler that caplog
        # uses, matching how other tests in this module capture logs.
        from loguru import logger as loguru_logger

        handler_id = loguru_logger.add(
            lambda msg: logging.getLogger("tumbler").error(msg.record["message"]),
            level="ERROR",
        )
        try:
            with caplog.at_level(logging.ERROR, logger="tumbler"):
                await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        finally:
            loguru_logger.remove(handler_id)

        # No traceback/exception chain should appear in captured records.
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "Traceback" not in joined
        # And the exception class is public/usable.
        assert issubclass(TakerPhaseError, Exception)

    async def test_confirmation_txid_logs_are_sensitive(self, tmp_path: Path) -> None:
        """Confirmation progress must not expose transaction identifiers by default."""
        from loguru import logger

        plan = _plan(tmp_path)
        phase = plan.phases[0]
        assert isinstance(phase, TakerCoinjoinPhase)
        phase.txid = "ab" * 32

        async def get_confirmations(_txid: str) -> int:
            return 1

        ctx = _ctx(tmp_path, taker_factory=lambda _: FakeTaker(_))
        ctx.min_confirmations_between_phases = 1
        ctx.get_confirmations = get_confirmations
        records: list[tuple[str, dict[str, object]]] = []
        handler_id = logger.add(
            lambda message: records.append(
                (message.record["message"], dict(message.record["extra"]))
            )
        )
        try:
            await TumbleRunner(plan, ctx)._wait_for_phase_confirmations(phase)
        finally:
            logger.remove(handler_id)

        txid_records = [record for record in records if phase.txid in record[0]]
        assert txid_records
        assert all(extra.get("sensitive") is True for _, extra in txid_records)

    async def test_runner_still_accepts_object_with_txid_attr(self, tmp_path: Path) -> None:
        """Defensive path: legacy fakes returning ``FakeTakerResult`` must still work."""
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def returns_object(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> Any:
                return FakeTakerResult(txid="obj-txid-xyz")

            t.do_coinjoin = returns_object  # type: ignore[assignment]
            return t

        result = await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert result.status == PlanStatus.COMPLETED
        taker_phases = [p for p in result.phases if isinstance(p, TakerCoinjoinPhase)]
        assert all(p.txid == "obj-txid-xyz" for p in taker_phases)


class TestMakerRepeatPenalty:
    """The runner penalizes the previous phase's successful maker identities.

    The penalty window is one phase deep and retains full selection support.
    """

    async def test_first_phase_has_no_penalty(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        captured: list[set[str] | None] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)
            t.last_used_nicks = {f"maker_{phase.index}_a", f"maker_{phase.index}_b"}
            original = t.do_coinjoin

            async def spy(**kwargs: Any) -> str | None:
                captured.append(kwargs.get("penalized_maker_keys"))
                return await original(**kwargs)

            t.do_coinjoin = spy  # type: ignore[assignment]
            return t

        await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert captured, "expected at least one taker phase"
        # Nothing has been used before the first phase.
        assert captured[0] is None

    async def test_resumed_runner_penalizes_persisted_previous_keys(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        plan.previous_phase_maker_keys = ["nick:previous", "bond:previous"]
        save_plan(plan, tmp_path)
        resumed_plan = load_plan("RunnerTest", tmp_path)
        captured: list[set[str] | None] = []

        async def make_taker(phase: Any) -> FakeTaker:
            taker = FakeTaker(phase)
            original = taker.do_coinjoin

            async def spy(**kwargs: Any) -> str | None:
                captured.append(kwargs.get("penalized_maker_keys"))
                return await original(**kwargs)

            taker.do_coinjoin = spy  # type: ignore[assignment]
            return taker

        await TumbleRunner(resumed_plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert captured[0] == {"nick:previous", "bond:previous"}

    async def test_subsequent_phase_penalizes_previous_keys(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        captured: list[set[str] | None] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)
            t.last_used_maker_keys = {
                f"nick:maker_{phase.index}_a",
                f"bond:{phase.index:064x}:0",
            }
            original = t.do_coinjoin

            async def spy(**kwargs: Any) -> str | None:
                captured.append(kwargs.get("penalized_maker_keys"))
                return await original(**kwargs)

            t.do_coinjoin = spy  # type: ignore[assignment]
            return t

        result = await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert result.status == PlanStatus.COMPLETED
        taker_phases = [p for p in result.phases if isinstance(p, TakerCoinjoinPhase)]
        assert len(captured) == len(taker_phases)
        # Only the previous successful phase is carried forward.
        for i in range(1, len(taker_phases)):
            prev_index = taker_phases[i - 1].index
            expected = {
                f"nick:maker_{prev_index}_a",
                f"bond:{prev_index:064x}:0",
            }
            assert captured[i] == expected

    async def test_nickname_fallback_and_empty_history_clear(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        captured: list[set[str] | None] = []
        call_index = {"i": 0}

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)
            # Only the first phase reports nicks; subsequent phases report none.
            if call_index["i"] == 0:
                t.last_used_nicks = {"maker_a", "maker_b"}
            else:
                t.last_used_nicks = set()
            call_index["i"] += 1
            original = t.do_coinjoin

            async def spy(**kwargs: Any) -> str | None:
                captured.append(kwargs.get("penalized_maker_keys"))
                return await original(**kwargs)

            t.do_coinjoin = spy  # type: ignore[assignment]
            return t

        await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert captured[0] is None
        if len(captured) >= 2:
            assert captured[1] == {"nick:maker_a", "nick:maker_b"}
        if len(captured) >= 3:
            assert captured[2] is None

    async def test_legacy_taker_without_penalty_kwarg(self, tmp_path: Path) -> None:
        # Older taker builds (or simple test fakes) may not accept the new
        # kwarg. The runner must fall back gracefully so the phase still runs.
        plan = _plan(tmp_path)
        seen_kwargs: list[dict[str, Any]] = []

        class LegacyTaker(FakeTaker):
            async def do_coinjoin(  # type: ignore[override]
                self,
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
            ) -> str | None:
                # No ``penalized_maker_keys`` kwarg: TypeError on first attempt
                # forces the runner's fallback path.
                seen_kwargs.append(
                    {
                        "amount": amount,
                        "destination": destination,
                        "mixdepth": mixdepth,
                        "counterparty_count": counterparty_count,
                    }
                )
                return f"legacy-{mixdepth}-{amount}"

        async def make_taker(phase: Any) -> LegacyTaker:
            t = LegacyTaker(phase)
            t.last_used_nicks = {"maker_x"}
            return t

        result = await TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker)).run()
        assert result.status == PlanStatus.COMPLETED
        assert seen_kwargs, "legacy taker should still have been invoked"


class TestConfirmationGate:
    """The runner must wait for each phase's txid(s) to confirm before the
    next phase starts. This mirrors the reference tumbler's ``restart_waiter``
    and avoids the next phase hitting the Taker's ``taker_utxo_age`` wall on
    an unconfirmed UTXO.
    """

    async def test_waits_for_confirmations_between_taker_phases(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        # Reduce to 2 phases so the test asserts on a single gate.
        plan.phases = plan.phases[:2]

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        # First two polls per txid return 0 confirmations, third returns enough.
        polls: dict[str, int] = {}

        async def get_confirmations(txid: str) -> int | None:
            polls[txid] = polls.get(txid, 0) + 1
            return polls[txid] - 1  # 0, 1, 2, ...

        async def zero_sleep(_: float) -> None:
            return None

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            sleep=zero_sleep,
            min_confirmations_between_phases=2,
            get_confirmations=get_confirmations,
            confirmation_poll_interval=0.0,
        )
        result = await TumbleRunner(plan, ctx).run()
        assert result.status == PlanStatus.COMPLETED
        # get_confirmations was polled at least ``min_conf + 1`` times for the
        # first phase's txid before the gate released.
        first_txid = result.phases[0].txid  # type: ignore[attr-defined]
        assert first_txid is not None
        assert polls[first_txid] >= 3

    async def test_last_phase_does_not_gate(self, tmp_path: Path) -> None:
        """No waiting after the final phase — nothing depends on it."""
        plan = _plan(tmp_path)
        plan.phases = plan.phases[:1]

        polls: list[str] = []

        async def get_confirmations(txid: str) -> int | None:
            polls.append(txid)
            return 0  # would never be "enough" if called

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        async def zero_sleep(_: float) -> None:
            return None

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            sleep=zero_sleep,
            min_confirmations_between_phases=2,
            get_confirmations=get_confirmations,
            confirmation_poll_interval=0.0,
        )
        result = await TumbleRunner(plan, ctx).run()
        assert result.status == PlanStatus.COMPLETED
        assert polls == []  # never polled because there's no next phase

    async def test_stop_during_confirmation_wait_cancels_plan(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        polls = 0

        async def get_confirmations(txid: str) -> int | None:
            nonlocal polls
            polls += 1
            return 0  # never confirms

        async def zero_sleep(_: float) -> None:
            return None

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            sleep=zero_sleep,
            min_confirmations_between_phases=2,
            get_confirmations=get_confirmations,
            confirmation_poll_interval=0.01,
        )
        runner = TumbleRunner(plan, ctx)
        task = asyncio.create_task(runner.run())

        async def _trigger_stop() -> None:
            # Let the runner reach the confirmation wait, then cancel.
            for _ in range(100):
                await asyncio.sleep(0.01)
                if polls > 0:
                    break
            runner.request_stop()

        await asyncio.gather(task, _trigger_stop())
        assert runner.plan.status == PlanStatus.CANCELLED

    async def test_gate_disabled_when_get_confirmations_is_none(self, tmp_path: Path) -> None:
        """Backwards-compat: default ctx (no callback) never polls."""
        plan = _plan(tmp_path)

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()
        assert result.status == PlanStatus.COMPLETED

    async def test_evicted_txid_falls_back_after_timeout(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """An evicted txid has no live matching UTXO and remains unresolved.

        The confirmation-history resolver returns ``None`` in this case. The
        runner must use ``confirmation_unknown_timeout`` rather than polling
        forever.
        """
        from datetime import UTC, datetime, timedelta

        from tumbler import runner as runner_mod

        plan = _plan(tmp_path)
        plan.phases = plan.phases[:2]

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        polls = 0

        async def get_confirmations(_txid: str) -> int | None:
            nonlocal polls
            polls += 1
            return None  # History has no matching live UTXO for this txid.

        # Drive a virtual clock so the timeout triggers without real waits.
        base = datetime(2026, 1, 1, tzinfo=UTC)
        ticks = {"n": 0}

        def fake_now() -> datetime:
            # Advance by 60s every call so the 300s timeout is reached
            # within a handful of polls.
            ticks["n"] += 1
            return base + timedelta(seconds=60 * ticks["n"])

        monkeypatch.setattr(runner_mod, "_now", fake_now)

        async def zero_sleep(_: float) -> None:
            return None

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            sleep=zero_sleep,
            min_confirmations_between_phases=2,
            get_confirmations=get_confirmations,
            confirmation_poll_interval=0.0,
            confirmation_progress_log_interval=0.0,
            confirmation_unknown_timeout=300.0,
        )
        result = await TumbleRunner(plan, ctx).run()
        assert result.status == PlanStatus.COMPLETED
        # We polled at least until the timeout fired and then proceeded.
        assert polls >= 1
        # The first phase's txid was persisted to disk before the gate ran,
        # so a restart could resume from a known broadcast txid.
        first_phase = result.phases[0]
        assert isinstance(first_phase, TakerCoinjoinPhase)
        assert first_phase.txid is not None

    async def test_unknown_timeout_disabled_keeps_polling(self, tmp_path: Path) -> None:
        """``confirmation_unknown_timeout=0`` preserves the strict legacy
        behaviour: keep polling until the txid resolves, even if it takes
        many polls of ``None``."""
        plan = _plan(tmp_path)
        plan.phases = plan.phases[:2]

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        polls = 0

        async def get_confirmations(_txid: str) -> int | None:
            nonlocal polls
            polls += 1
            # First 3 polls: unresolved. Then start returning real numbers.
            if polls < 4:
                return None
            return polls - 3  # 1, 2, 3, ...

        async def zero_sleep(_: float) -> None:
            return None

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            sleep=zero_sleep,
            min_confirmations_between_phases=2,
            get_confirmations=get_confirmations,
            confirmation_poll_interval=0.0,
            confirmation_unknown_timeout=0.0,  # disabled
        )
        result = await TumbleRunner(plan, ctx).run()
        assert result.status == PlanStatus.COMPLETED
        # Polled past the unresolved phase and into the resolving phase.
        assert polls >= 5

    async def test_phase_txid_persisted_before_confirmation_gate(self, tmp_path: Path) -> None:
        """The phase's broadcast txid must hit disk *before* the runner
        enters the (potentially long) confirmation wait, so a daemon
        restart mid-wait does not lose the broadcast record."""
        plan = _plan(tmp_path)
        plan.phases = plan.phases[:2]

        observed_txids: list[str | None] = []

        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        first_seen = {"done": False}

        async def get_confirmations(txid: str) -> int | None:
            # On the first poll, snapshot the persisted plan from disk to
            # confirm the txid was already saved.
            if not first_seen["done"]:
                first_seen["done"] = True
                persisted = load_plan(plan.wallet_name, tmp_path)
                first_persisted_phase = persisted.phases[0]
                assert isinstance(first_persisted_phase, TakerCoinjoinPhase)
                assert first_persisted_phase.status == PhaseStatus.AWAITING_CONFIRMATION
                assert first_persisted_phase.finished_at is None
                observed_txids.append(first_persisted_phase.txid)
            return 99  # immediately satisfy the gate

        async def zero_sleep(_: float) -> None:
            return None

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            sleep=zero_sleep,
            min_confirmations_between_phases=2,
            get_confirmations=get_confirmations,
            confirmation_poll_interval=0.0,
        )
        result = await TumbleRunner(plan, ctx).run()
        assert result.status == PlanStatus.COMPLETED
        assert observed_txids and observed_txids[0] is not None

    async def test_resumed_confirmation_wait_does_not_replay_coinjoin(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        plan.phases = plan.phases[:2]
        first = plan.phases[0]
        assert isinstance(first, TakerCoinjoinPhase)
        first.status = PhaseStatus.AWAITING_CONFIRMATION
        first.txid = "ab" * 32
        plan.current_phase = 0

        invoked_phases: list[int] = []

        async def make_taker(phase: TakerCoinjoinPhase) -> FakeTaker:
            invoked_phases.append(phase.index)
            return FakeTaker(phase)

        async def get_confirmations(_txid: str) -> int | None:
            return 6

        ctx = RunnerContext(
            wallet_service=FakeWalletService(),  # type: ignore[arg-type]
            wallet_name="RunnerTest",
            data_dir=tmp_path,
            taker_factory=make_taker,
            min_confirmations_between_phases=2,
            get_confirmations=get_confirmations,
            confirmation_poll_interval=0.0,
        )

        result = await TumbleRunner(plan, ctx).run()

        assert result.status == PlanStatus.COMPLETED
        assert first.status == PhaseStatus.COMPLETED
        assert first.index not in invoked_phases


class TestInterPhaseWaitLogging:
    """The inter-phase ``wait_seconds`` delay must be visible in the logs.

    Without this, an operator watching the journal sees the confirmation
    gate clear and then nothing for hours -- indistinguishable from the
    runner being stuck. The runner emits a single start/eta line.
    """

    async def test_inter_phase_wait_emits_single_local_eta_log(
        self, tmp_path: Path, caplog: Any
    ) -> None:
        import logging

        from loguru import logger as loguru_logger

        plan = _plan(tmp_path)
        plan.phases = plan.phases[:2]
        # Set a non-trivial wait so the wait branch fires; sleep is
        # patched to a no-op so the test runs instantly.
        plan.phases[0].wait_seconds = 600.0

        # Bridge loguru -> stdlib logging so caplog captures runner output.
        class _PropagateHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                logging.getLogger(record.name).handle(record)

        sink_id = loguru_logger.add(_PropagateHandler(), level="INFO", format="{message}")
        try:
            caplog.set_level(logging.INFO)

            async def make_taker(phase: Any) -> FakeTaker:
                return FakeTaker(phase)

            async def get_confirmations(_txid: str) -> int | None:
                return 99  # immediately satisfy the confirmation gate

            async def zero_sleep(_: float) -> None:
                return None

            ctx = RunnerContext(
                wallet_service=FakeWalletService(),  # type: ignore[arg-type]
                wallet_name="RunnerTest",
                data_dir=tmp_path,
                taker_factory=make_taker,
                sleep=zero_sleep,
                min_confirmations_between_phases=2,
                get_confirmations=get_confirmations,
                confirmation_poll_interval=0.0,
            )
            result = await TumbleRunner(plan, ctx).run()
        finally:
            loguru_logger.remove(sink_id)

        assert result.status == PlanStatus.COMPLETED
        wait_logs = [
            r.getMessage()
            for r in caplog.records
            if "inter-phase delay before phase 1" in r.getMessage()
        ]
        assert len(wait_logs) == 1, f"expected single wait log; got {wait_logs}"
        assert "sleeping 600s" in wait_logs[0]
        assert "until " in wait_logs[0]
        assert "+" in wait_logs[0] or "-" in wait_logs[0], wait_logs[0]


class TestInternalDestinationMixdepth:
    async def test_internal_wraps_using_wallet_mixdepth_count(self, tmp_path: Path) -> None:
        async def make_taker(phase: Any) -> FakeTaker:
            return FakeTaker(phase)

        # (mixdepth_count, current mixdepth, expected next mixdepth after wrap).
        # The buggy hardcoded "% 5" would request mixdepth 3 (raises), 1 and 0.
        cases = [(3, 2, 0), (6, 5, 0), (8, 4, 5)]
        for mixdepth_count, mixdepth, expected_next in cases:
            ctx = RunnerContext(
                wallet_service=FakeWalletService(mixdepth_count=mixdepth_count),  # type: ignore[arg-type]
                wallet_name="RunnerTest",
                data_dir=tmp_path,
                taker_factory=make_taker,
            )
            runner = TumbleRunner(_plan(tmp_path), ctx)
            phase = TakerCoinjoinPhase(
                index=0,
                mixdepth=mixdepth,
                amount=100_000,
                counterparty_count=2,
                destination="INTERNAL",
            )
            address = await runner._resolve_destination(phase)
            assert address.startswith(f"bcrt1qfake{expected_next}")


class TestRunnerSkipsUnfundedPhases:
    """Regression coverage: taker phases against mixdepths with no spendable
    funds must be SKIPPED, not retried into a plan-wide failure.

    Two field reports drove this: (1) all funds of a mixdepth were frozen
    after the plan was built, and (2) the plan's stage-2 chain visited a
    mixdepth that never received coins. In both cases the taker fails with
    "No eligible UTXOs in mixdepth ..." forever, and before this fix the
    runner burned the whole retry budget (with hours of back-off) and then
    failed the entire tumble.
    """

    @staticmethod
    def _unfunded_taker_factory(
        unfunded_mixdepths: set[int],
        reason_by_mixdepth: dict[int, str] | None = None,
    ) -> Any:
        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

            async def do_cj(
                amount: int,
                destination: str,
                mixdepth: int = 0,
                counterparty_count: int | None = None,
                exclude_nicks: set[str] | None = None,
                penalized_maker_keys: set[str] | None = None,
            ) -> str | None:
                if mixdepth in unfunded_mixdepths:
                    t.state = "failed"
                    reasons = reason_by_mixdepth or {}
                    t.last_failure_reason = reasons.get(
                        mixdepth,
                        f"No eligible UTXOs in mixdepth {mixdepth} (no UTXOs present)",
                    )
                    return None
                return f"txid-{phase.index}"

            t.do_coinjoin = do_cj  # type: ignore[assignment]
            return t

        return make_taker

    async def test_frozen_mixdepth_phases_are_skipped_and_plan_completes(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path)
        ctx = _ctx(
            tmp_path,
            taker_factory=self._unfunded_taker_factory(
                {1},
                {1: "No eligible UTXOs in mixdepth 1 (2 UTXOs: 2 frozen)"},
            ),
        )
        # Mixdepth 1 lost its funds after planning (e.g. the user froze them).
        ctx.wallet_service._balances[1] = 0  # type: ignore[attr-defined]

        result = await TumbleRunner(plan, ctx).run()

        assert result.status == PlanStatus.COMPLETED
        md1_phases = [
            p for p in result.phases if isinstance(p, TakerCoinjoinPhase) and p.mixdepth == 1
        ]
        other_phases = [
            p for p in result.phases if isinstance(p, TakerCoinjoinPhase) and p.mixdepth != 1
        ]
        assert md1_phases, "plan unexpectedly contains no mixdepth-1 phases"
        assert all(p.status == PhaseStatus.SKIPPED for p in md1_phases)
        # Skipping consumes no retry budget.
        assert all(p.attempt_count == 0 for p in md1_phases)
        assert all(p.status == PhaseStatus.COMPLETED for p in other_phases)
        # Persistence: skipped statuses survive the YAML round-trip.
        on_disk = load_plan("RunnerTest", tmp_path)
        assert [str(p.status) for p in on_disk.phases] == [str(p.status) for p in result.phases]

    async def test_entirely_unfunded_plan_completes_with_all_phases_skipped(
        self, tmp_path: Path
    ) -> None:
        plan = _plan(tmp_path)
        ctx = _ctx(
            tmp_path,
            taker_factory=self._unfunded_taker_factory({0, 1, 2, 3, 4}),
        )
        ctx.wallet_service._balances = dict.fromkeys(range(5), 0)  # type: ignore[attr-defined]

        result = await TumbleRunner(plan, ctx).run()

        assert result.status == PlanStatus.COMPLETED
        assert all(p.status == PhaseStatus.SKIPPED for p in result.phases)

    async def test_unfunded_hint_with_spendable_balance_still_retries(self, tmp_path: Path) -> None:
        """A "No eligible UTXOs" failure on a mixdepth that *does* have a
        spendable balance (e.g. UTXOs merely lack confirmations) keeps the
        ordinary retry behaviour and eventually fails the plan."""
        plan = _plan(tmp_path)
        plan.parameters.max_phase_retries = 1
        ctx = _ctx(
            tmp_path,
            taker_factory=self._unfunded_taker_factory(
                {0},
                {0: "No eligible UTXOs in mixdepth 0 (1 UTXOs: 1 below 5 confirmation(s))"},
            ),
        )
        # Balance stays at the FakeWalletService default (> 0).

        result = await TumbleRunner(plan, ctx).run()

        assert result.status == PlanStatus.FAILED
        assert result.phases[0].status == PhaseStatus.FAILED
        assert result.phases[0].attempt_count == 1

    async def test_skip_check_tolerates_wallet_without_bond_kwarg(self, tmp_path: Path) -> None:
        """Wallet fakes (and older wallet services) without the
        ``include_fidelity_bonds`` kwarg must not break the skip check."""

        class LegacyWallet(FakeWalletService):
            async def get_balance(self, mixdepth: int) -> int:  # type: ignore[override]
                return self._balances.get(mixdepth, 0)

        plan = _plan(tmp_path)
        ctx = _ctx(
            tmp_path,
            taker_factory=self._unfunded_taker_factory({0, 1, 2, 3, 4}),
        )
        wallet = LegacyWallet()
        wallet._balances = dict.fromkeys(range(5), 0)
        ctx.wallet_service = wallet  # type: ignore[assignment]

        result = await TumbleRunner(plan, ctx).run()

        assert result.status == PlanStatus.COMPLETED
        assert all(p.status == PhaseStatus.SKIPPED for p in result.phases)
