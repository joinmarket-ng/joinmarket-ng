"""
YAML persistence for tumbler plans.

Plans live under ``<data_dir>/schedules/<wallet_name>.yaml``. The file is the
canonical source of truth for an in-progress tumble; the runner overwrites it
on every state transition and reads it on startup to resume.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import yaml
from jmcore.paths import get_default_data_dir
from pydantic import ValidationError

from tumbler.plan import Plan

SCHEDULES_SUBDIR = "schedules"


class PlanNotFoundError(FileNotFoundError):
    """Raised when no plan exists for the given wallet."""


class PlanCorruptError(ValueError):
    """Raised when an on-disk plan cannot be parsed or validated."""


def _schedules_dir(data_dir: Path | str | None) -> Path:
    base = Path(data_dir) if data_dir is not None else get_default_data_dir()
    path = base / SCHEDULES_SUBDIR
    path.mkdir(parents=True, exist_ok=True)
    # Mode 0o700 mirrors how the installer hardens the data dir.
    try:
        path.chmod(0o700)
    except OSError:  # pragma: no cover - best effort on exotic filesystems
        pass
    return path


def plan_path(wallet_name: str, data_dir: Path | str | None = None) -> Path:
    """Return the absolute path where ``wallet_name``'s plan is stored."""
    if not wallet_name:
        raise ValueError("wallet_name must be non-empty")
    if "/" in wallet_name or "\\" in wallet_name or wallet_name in {".", ".."}:
        raise ValueError(f"wallet_name is not a safe filename: {wallet_name!r}")
    # Strip a trailing ".jmdat" if the caller passes the full file name; the
    # plan file is keyed by the wallet's stem so both spellings land at the
    # same path.
    stem = wallet_name
    if stem.endswith(".jmdat"):
        stem = stem[: -len(".jmdat")]
    return _schedules_dir(data_dir) / f"{stem}.yaml"


def save_plan(plan: Plan, data_dir: Path | str | None = None) -> Path:
    """
    Atomically write ``plan`` to disk and return the path.

    The write is atomic against concurrent readers: we write to a sibling
    temp file in the same directory and then ``os.replace`` it.
    """
    plan.touch()
    target = plan_path(plan.wallet_name, data_dir)
    payload = plan.model_dump(mode="json")
    # ``sort_keys=False`` preserves field order so the file stays readable.
    serialized = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False)

    directory = target.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=directory)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    try:
        target.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    return target


def load_plan(wallet_name: str, data_dir: Path | str | None = None) -> Plan:
    """Load a plan for ``wallet_name``. Raises if missing or corrupt."""
    target = plan_path(wallet_name, data_dir)
    if not target.exists():
        raise PlanNotFoundError(f"no tumbler plan found for wallet {wallet_name!r}")
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PlanCorruptError(f"plan at {target} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PlanCorruptError(f"plan at {target} is not a mapping")
    try:
        return Plan.model_validate(raw)
    except ValidationError as exc:
        raise PlanCorruptError(f"plan at {target} failed schema validation: {exc}") from exc


def delete_plan(wallet_name: str, data_dir: Path | str | None = None) -> bool:
    """
    Remove a wallet's plan file. Returns ``True`` if a file was removed,
    ``False`` if no plan existed.
    """
    target = plan_path(wallet_name, data_dir)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True
