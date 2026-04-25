"""Tests for :mod:`tumbler.persistence`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.persistence import (
    PlanCorruptError,
    PlanNotFoundError,
    delete_plan,
    load_plan,
    plan_path,
    save_plan,
)
from tumbler.plan import Plan


def _build_plan(wallet_name: str = "Satoshi") -> Plan:
    params = TumbleParameters(
        destinations=["bcrt1qdest0000000000000000000000000000000000abc"],
        mixdepth_balances={0: 1_000_000, 1: 500_000},
        seed=1,
    )
    return PlanBuilder(wallet_name, params).build()


class TestPlanPath:
    def test_strips_jmdat_suffix(self, tmp_path: Path) -> None:
        a = plan_path("Satoshi", tmp_path)
        b = plan_path("Satoshi.jmdat", tmp_path)
        assert a == b
        assert a.name == "Satoshi.yaml"

    def test_rejects_unsafe_names(self, tmp_path: Path) -> None:
        for name in ["", "../etc/passwd", "foo/bar", ".", ".."]:
            with pytest.raises(ValueError):
                plan_path(name, tmp_path)

    def test_creates_schedules_subdir(self, tmp_path: Path) -> None:
        plan_path("Satoshi", tmp_path)
        assert (tmp_path / "schedules").is_dir()


class TestSaveLoad:
    def test_roundtrip_preserves_all_fields(self, tmp_path: Path) -> None:
        plan = _build_plan()
        save_plan(plan, tmp_path)
        loaded = load_plan("Satoshi", tmp_path)
        assert loaded.model_dump(mode="json") == plan.model_dump(mode="json")

    def test_save_is_atomic(self, tmp_path: Path) -> None:
        plan = _build_plan()
        save_plan(plan, tmp_path)
        # No stray temp files should remain in the schedules directory.
        schedules = tmp_path / "schedules"
        leftovers = [p for p in schedules.iterdir() if p.name.startswith(".")]
        assert leftovers == []

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(PlanNotFoundError):
            load_plan("nope", tmp_path)

    def test_corrupt_yaml_raises(self, tmp_path: Path) -> None:
        target = plan_path("Bad", tmp_path)
        target.write_text("not: valid: yaml: [[[")
        with pytest.raises(PlanCorruptError):
            load_plan("Bad", tmp_path)

    def test_schema_violation_raises(self, tmp_path: Path) -> None:
        target = plan_path("BadSchema", tmp_path)
        target.write_text(yaml.safe_dump({"wallet_name": "x"}))  # missing destinations
        with pytest.raises(PlanCorruptError):
            load_plan("BadSchema", tmp_path)

    def test_save_updates_updated_at(self, tmp_path: Path) -> None:
        plan = _build_plan()
        first = plan.updated_at
        save_plan(plan, tmp_path)
        assert plan.updated_at >= first

    def test_file_permissions_restrictive(self, tmp_path: Path) -> None:
        plan = _build_plan()
        path = save_plan(plan, tmp_path)
        mode = path.stat().st_mode & 0o777
        # Best effort: at minimum, group/other should not have write.
        assert mode & 0o022 == 0


class TestDelete:
    def test_delete_existing(self, tmp_path: Path) -> None:
        plan = _build_plan()
        save_plan(plan, tmp_path)
        assert delete_plan(plan.wallet_name, tmp_path) is True
        assert delete_plan(plan.wallet_name, tmp_path) is False
