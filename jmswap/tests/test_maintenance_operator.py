from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import jmswap.maintenance as maintenance
from jmswap.maintenance import (
    MaintenanceLockError,
    MaintenanceStateError,
    claim_spend_channels,
    enroll_channel,
    reconcile_maintenance_admission,
    resolve_maintenance_payment,
)

LOCAL = "02" + "11" * 32
PEER = "03" + "22" * 32
POINT = "aa" * 32 + ":1"
RETURN_POINT = "bb" * 32 + ":2"
ORIGIN = "cc" * 32
PAYMENT_HASH = "dd" * 32


def read_state(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "maintenance-v1.json").read_text())


def write_state(directory: Path, state: dict[str, Any]) -> None:
    (directory / "maintenance-v1.json").write_text(json.dumps(state))


def enrolled_state(directory: Path, *, status: str = "enrolled") -> dict[str, Any]:
    assert enroll_channel(directory, LOCAL, PEER, POINT, ORIGIN)
    state = read_state(directory)
    state["enrollments"][POINT]["status"] = status
    state["admission_channels"] = []
    write_state(directory, state)
    return state


def add_payment_journal(state: dict[str, Any]) -> None:
    state["payment"] = {
        "amount_sat": 1_000,
        "expires_at": 130,
        "fee_budget_msat": 0,
        "middle_peer_node_id": PEER,
        "outgoing_channel_id": 101,
        "outgoing_channel_point": POINT,
        "payment_hash": PAYMENT_HASH,
        "payment_request": "lnbcrt-maintenance",
        "phase": "unresolved",
        "return_channel_point": RETURN_POINT,
        "return_peer_node_id": PEER,
    }


def test_legacy_admission_marker_stays_absent_until_explicit_reconciliation(tmp_path: Path) -> None:
    state = enrolled_state(tmp_path)
    state.pop("admission_channels")
    state.pop("spend_guards")
    write_state(tmp_path, state)

    assert maintenance.maintenance_status(tmp_path, LOCAL)["unreconciled_admission_count"] == 1
    assert "admission_channels" not in read_state(tmp_path)
    assert "spend_guards" not in read_state(tmp_path)

    reconcile_maintenance_admission(tmp_path, LOCAL, [POINT], confirmed_idle=True)

    updated = read_state(tmp_path)
    assert updated["admission_channels"] == [POINT]
    assert updated["enrollments"] == state["enrollments"]
    assert updated["spend_guards"] == {}


def test_admission_reconciliation_preserves_enrollment_and_collateral_guard_state(
    tmp_path: Path,
) -> None:
    state = enrolled_state(tmp_path)
    enrollments = state["enrollments"]
    spend_guards = state["spend_guards"]

    reconcile_maintenance_admission(tmp_path, LOCAL, [POINT], confirmed_idle=True)

    updated = read_state(tmp_path)
    assert updated["admission_channels"] == [POINT]
    assert updated["enrollments"] == enrollments
    assert updated["enrollments"][POINT]["status"] == "enrolled"
    assert updated["spend_guards"] == spend_guards
    assert updated["payment"] is None


def test_admission_reconciliation_rejects_existing_spend_guard_without_mutation(
    tmp_path: Path,
) -> None:
    enrolled_state(tmp_path)
    assert claim_spend_channels(tmp_path, LOCAL, "operator-spend", [POINT])
    before = (tmp_path / "maintenance-v1.json").read_bytes()

    with pytest.raises(MaintenanceStateError, match="local spend"):
        reconcile_maintenance_admission(tmp_path, LOCAL, [POINT], confirmed_idle=True)

    assert (tmp_path / "maintenance-v1.json").read_bytes() == before


def test_admission_reconciliation_rejects_current_payment_journal_overlap(tmp_path: Path) -> None:
    state = enrolled_state(tmp_path)
    add_payment_journal(state)
    write_state(tmp_path, state)
    before = (tmp_path / "maintenance-v1.json").read_bytes()

    with pytest.raises(MaintenanceStateError, match="maintenance obligation"):
        reconcile_maintenance_admission(tmp_path, LOCAL, [POINT], confirmed_idle=True)

    assert (tmp_path / "maintenance-v1.json").read_bytes() == before


@pytest.mark.parametrize("status", ["pending_validation", "fee_init_intent", "fee_init_unknown"])
def test_admission_reconciliation_rejects_non_enrolled_fee_states(
    tmp_path: Path, status: str
) -> None:
    enrolled_state(tmp_path, status=status)

    with pytest.raises(MaintenanceStateError, match="fully enrolled"):
        reconcile_maintenance_admission(tmp_path, LOCAL, [POINT], confirmed_idle=True)

    assert read_state(tmp_path)["admission_channels"] == []
    assert read_state(tmp_path)["enrollments"][POINT]["status"] == status


def test_active_service_lock_prevents_operator_mutations(tmp_path: Path) -> None:
    state = enrolled_state(tmp_path)
    add_payment_journal(state)
    write_state(tmp_path, state)
    lock = maintenance._Flock(tmp_path / "maintenance-v1.service.lock")
    assert lock.acquire(blocking=False)
    before = (tmp_path / "maintenance-v1.json").read_bytes()
    try:
        with pytest.raises(MaintenanceLockError, match="stop the maintenance service"):
            reconcile_maintenance_admission(tmp_path, LOCAL, [POINT], confirmed_idle=True)
        with pytest.raises(MaintenanceLockError, match="stop the maintenance service"):
            resolve_maintenance_payment(
                tmp_path,
                LOCAL,
                PAYMENT_HASH,
                "failed",
                confirmed_no_inflight=True,
                recorded_at=100,
                next_due=161,
            )
    finally:
        lock.release()

    assert (tmp_path / "maintenance-v1.json").read_bytes() == before


def test_payment_resolution_requires_exact_current_hash_without_mutation(tmp_path: Path) -> None:
    state = enrolled_state(tmp_path)
    add_payment_journal(state)
    write_state(tmp_path, state)
    before = (tmp_path / "maintenance-v1.json").read_bytes()

    with pytest.raises(MaintenanceStateError, match="differs from the current"):
        resolve_maintenance_payment(
            tmp_path,
            LOCAL,
            "ee" * 32,
            "failed",
            confirmed_no_inflight=True,
            recorded_at=100,
            next_due=161,
        )

    assert (tmp_path / "maintenance-v1.json").read_bytes() == before


def test_payment_resolution_durably_records_operator_audit_and_future_schedule(
    tmp_path: Path,
) -> None:
    state = enrolled_state(tmp_path)
    add_payment_journal(state)
    write_state(tmp_path, state)
    enrollments = state["enrollments"]
    spend_guards = state["spend_guards"]

    resolve_maintenance_payment(
        tmp_path,
        LOCAL,
        PAYMENT_HASH,
        "succeeded",
        confirmed_no_inflight=True,
        recorded_at=100,
        next_due=161,
    )

    updated = read_state(tmp_path)
    assert updated["payment"] is None
    assert updated["next_due"] == 161
    assert updated["operator_payment_resolution"] == {
        "payment_hash": PAYMENT_HASH,
        "recorded_at": 100,
        "terminal_status": "succeeded",
    }
    assert updated["enrollments"] == enrollments
    assert updated["spend_guards"] == spend_guards
    assert maintenance.maintenance_status(tmp_path, LOCAL)["operator_payment_resolution"] == {
        "payment_hash": PAYMENT_HASH,
        "recorded_at": 100,
        "terminal_status": "succeeded",
    }
