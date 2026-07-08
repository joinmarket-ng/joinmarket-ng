"""Tests for jmwalletd.legacy_schedule — Plan -> legacy schedule projection."""

from __future__ import annotations

from tumbler.plan import (
    MakerSessionPhase,
    PhaseStatus,
    Plan,
    TakerCoinjoinPhase,
)

from jmwalletd.legacy_schedule import NO_ROUNDING, plan_to_legacy_schedule

DEST = "bcrt1qpnv3nze7u6ecw63mn06ksxh497a3lryagh233q"
TXID = "a" * 64


def _plan(phases: list, current_phase: int = 0) -> Plan:
    return Plan(
        wallet_name="test_wallet.jmdat",
        destinations=[DEST],
        phases=phases,
        current_phase=current_phase,
    )


class TestEntryShape:
    def test_fraction_amount_entry(self) -> None:
        plan = _plan(
            [
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=2,
                    amount_fraction=0.25,
                    counterparty_count=4,
                    destination="INTERNAL",
                    wait_seconds=90.0,
                    rounding_sigfigs=2,
                )
            ]
        )
        assert plan_to_legacy_schedule(plan) == [[2, 0.25, 4, "INTERNAL", 1.5, 2, 0]]

    def test_absolute_amount_entry(self) -> None:
        plan = _plan(
            [
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=0,
                    amount=150_000,
                    counterparty_count=6,
                    destination=DEST,
                )
            ]
        )
        entry = plan_to_legacy_schedule(plan)[0]
        assert entry == [0, 150_000, 6, DEST, 0.0, NO_ROUNDING, 0]

    def test_sweep_amount_is_zero(self) -> None:
        plan = _plan(
            [
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=4,
                    amount_fraction=0.0,
                    counterparty_count=8,
                    destination=DEST,
                )
            ]
        )
        assert plan_to_legacy_schedule(plan)[0][1] == 0.0

    def test_jam_parses_entries(self) -> None:
        """Every entry must satisfy JAM's isScheduleValue type guard:
        numbers at 0/1/2/4/5, string at 3, number-or-string at 6."""
        plan = _plan(
            [
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=1,
                    amount_fraction=0.5,
                    counterparty_count=5,
                    destination="INTERNAL",
                    status=PhaseStatus.RUNNING,
                    txid=TXID,
                ),
                TakerCoinjoinPhase(
                    index=1,
                    mixdepth=2,
                    amount_fraction=0.0,
                    counterparty_count=5,
                    destination=DEST,
                ),
            ]
        )
        for entry in plan_to_legacy_schedule(plan):
            assert len(entry) == 7
            assert isinstance(entry[0], int | float)
            assert isinstance(entry[1], int | float)
            assert isinstance(entry[2], int | float)
            assert isinstance(entry[3], str)
            assert isinstance(entry[4], int | float)
            assert isinstance(entry[5], int | float)
            assert isinstance(entry[6], int | float | str)


class TestCompletionFlag:
    def test_pending_is_zero(self) -> None:
        plan = _plan(
            [
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=0,
                    amount_fraction=0.5,
                    counterparty_count=5,
                    destination=DEST,
                )
            ]
        )
        assert plan_to_legacy_schedule(plan)[0][6] == 0

    def test_broadcast_unconfirmed_is_txid(self) -> None:
        """A phase past broadcast but not past the confirmation gate must
        surface the txid (JAM shows 'waiting for confirmation')."""
        phase = TakerCoinjoinPhase(
            index=0,
            mixdepth=0,
            amount_fraction=0.5,
            counterparty_count=5,
            destination=DEST,
            status=PhaseStatus.COMPLETED,
            txid=TXID,
        )
        # current_phase still at 0: the runner has not advanced past the gate.
        plan = _plan([phase], current_phase=0)
        assert plan_to_legacy_schedule(plan)[0][6] == TXID

    def test_confirmed_is_one(self) -> None:
        """Once the plan has advanced past a completed phase it is confirmed."""
        phase = TakerCoinjoinPhase(
            index=0,
            mixdepth=0,
            amount_fraction=0.5,
            counterparty_count=5,
            destination=DEST,
            status=PhaseStatus.COMPLETED,
            txid=TXID,
        )
        plan = _plan([phase], current_phase=1)
        assert plan_to_legacy_schedule(plan)[0][6] == 1

    def test_failed_with_txid_keeps_txid(self) -> None:
        phase = TakerCoinjoinPhase(
            index=0,
            mixdepth=0,
            amount_fraction=0.5,
            counterparty_count=5,
            destination=DEST,
            status=PhaseStatus.FAILED,
            txid=TXID,
        )
        assert plan_to_legacy_schedule(_plan([phase]))[0][6] == TXID


class TestMakerSessionFolding:
    def test_maker_session_folds_into_previous_wait(self) -> None:
        plan = _plan(
            [
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=0,
                    amount_fraction=0.5,
                    counterparty_count=5,
                    destination="INTERNAL",
                    wait_seconds=60.0,
                ),
                MakerSessionPhase(index=1, duration_seconds=600.0, wait_seconds=120.0),
                TakerCoinjoinPhase(
                    index=2,
                    mixdepth=1,
                    amount_fraction=0.0,
                    counterparty_count=5,
                    destination=DEST,
                ),
            ]
        )
        schedule = plan_to_legacy_schedule(plan)
        assert len(schedule) == 2
        # 60s own wait + 600s maker session + 120s maker wait = 13 minutes.
        assert schedule[0][4] == 13.0

    def test_leading_maker_session_is_dropped(self) -> None:
        plan = _plan(
            [
                MakerSessionPhase(index=0, duration_seconds=600.0),
                TakerCoinjoinPhase(
                    index=1,
                    mixdepth=0,
                    amount_fraction=0.5,
                    counterparty_count=5,
                    destination=DEST,
                ),
            ]
        )
        schedule = plan_to_legacy_schedule(plan)
        assert len(schedule) == 1
        assert schedule[0][0] == 0

    def test_count_bounded_maker_session_adds_only_wait(self) -> None:
        """A maker session without a time bound has no duration estimate;
        only its inter-phase wait is folded in."""
        plan = _plan(
            [
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=0,
                    amount_fraction=0.5,
                    counterparty_count=5,
                    destination=DEST,
                    wait_seconds=0.0,
                ),
                MakerSessionPhase(index=1, target_cj_count=2, wait_seconds=60.0),
            ]
        )
        assert plan_to_legacy_schedule(plan)[0][4] == 1.0

    def test_empty_plan_gives_empty_schedule(self) -> None:
        assert plan_to_legacy_schedule(_plan([])) == []
