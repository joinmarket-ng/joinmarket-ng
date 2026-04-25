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
from pathlib import Path
from typing import Any

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

    def __init__(self, balance_sats: int = 5_000_000) -> None:
        self._counter = 0
        # Per-(mixdepth, change) index counters mirror WalletService behaviour.
        self._next_index: dict[tuple[int, int], int] = {}
        # Per-mixdepth balance used by the runner's amount_fraction resolver.
        self._balances: dict[int, int] = {m: balance_sats for m in range(5)}

    def get_next_address_index(self, mixdepth: int, change: int) -> int:
        idx = self._next_index.get((mixdepth, change), 0)
        self._next_index[(mixdepth, change)] = idx + 1
        return idx

    def get_change_address(self, mixdepth: int, index: int) -> str:
        self._counter += 1
        return f"bcrt1qfake{mixdepth}{index}{self._counter:04d}"

    async def get_balance(
        self,
        mixdepth: int,
        include_fidelity_bonds: bool = True,
        min_confirmations: int = 0,
    ) -> int:
        return self._balances.get(mixdepth, 0)


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
    ``(amount, destination, mixdepth=0, counterparty_count=None) -> str | None``.
    Accepting only those kwargs means the test suite fails loudly if the
    runner ever leaks a stray ``rounding`` / ``amount_fraction`` kwarg again.
    """

    def __init__(self, phase: Any) -> None:
        self.phase = phase
        self.started = False
        self.stopped_with: dict[str, Any] | None = None
        self.do_coinjoin_kwargs: dict[str, Any] | None = None

    async def start(self) -> None:
        self.started = True

    async def do_coinjoin(
        self,
        amount: int,
        destination: str,
        mixdepth: int = 0,
        counterparty_count: int | None = None,
    ) -> str | None:
        self.do_coinjoin_kwargs = {
            "amount": amount,
            "destination": destination,
            "mixdepth": mixdepth,
            "counterparty_count": counterparty_count,
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


def _plan(tmp_path: Path, *, include_maker: bool = False) -> Plan:
    params = TumbleParameters(
        destinations=["bcrt1qdest0000000000000000000000000000000000zzz"],
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
) -> RunnerContext:
    async def zero_sleep(_: float) -> None:
        return None

    return RunnerContext(
        wallet_service=FakeWalletService(),  # type: ignore[arg-type]
        wallet_name="RunnerTest",
        data_dir=tmp_path,
        taker_factory=taker_factory,
        maker_factory=maker_factory,
        sleep=zero_sleep,
    )


# --------------------------------------------------------------------------- tests


class TestRunnerHappyPath:
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
    Exercise the ``tweak_tumble_schedule`` equivalent: on a failed
    taker-coinjoin phase the runner should rearm the same phase with a
    lower ``counterparty_count`` and an ``INTERNAL`` destination, up to
    ``max_phase_retries`` times before failing the whole plan.
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

    async def test_retry_swaps_external_destination_to_internal(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        # Find a phase that targets an external destination (not INTERNAL).
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
                        return None
                return f"txid-{phase.index}"

            t.do_coinjoin = flaky  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        # First attempt saw the real address; retry saw an INTERNAL-derived
        # wallet address (resolved via FakeWalletService.get_change_address
        # → starts with "bcrt1qfake").
        assert observed[0] == original_destination
        assert observed[1].startswith("bcrt1qfake")
        # The phase record itself now carries the INTERNAL sentinel.
        assert result.phases[target_index].destination == "INTERNAL"

    async def test_retry_lowers_counterparty_count_toward_minimum(self, tmp_path: Path) -> None:
        plan = _plan(tmp_path)
        # Force-set the first phase's counterparty_count above the floor
        # so the tweak has room to reduce it.
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
                        return None
                return "tx"

            t.do_coinjoin = flaky  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.COMPLETED
        # Two calls: first at the original count, second one lower.
        assert len(seen_cp) == 2
        assert seen_cp[0] == plan.parameters.maker_count_min + 2
        assert seen_cp[1] == plan.parameters.maker_count_min + 1

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
                return None

            t.do_coinjoin = always_fail  # type: ignore[assignment]
            return t

        runner = TumbleRunner(plan, _ctx(tmp_path, taker_factory=make_taker))
        result = await runner.run()

        assert result.status == PlanStatus.FAILED
        assert calls["n"] == 1
        assert result.phases[0].attempt_count == 0


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

    async def test_runner_resolves_amount_fraction_via_wallet_balance(self, tmp_path: Path) -> None:
        """Stage-2 fractional CJ phases must be converted to int sats via
        :meth:`WalletService.get_balance` at dispatch time.
        """
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
        captured: list[dict[str, Any]] = []

        async def make_taker(phase: Any) -> FakeTaker:
            t = FakeTaker(phase)

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
        # 25% of 4_000_000 == 1_000_000 sats.
        assert 1_000_000 in {c["amount"] for c in frac_calls}

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
