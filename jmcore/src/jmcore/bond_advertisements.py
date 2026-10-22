"""Durable advertisement history and its shared proof/lease admission exclusion."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jmcore.paths import get_market_store_path
from jmcore.secure_files import atomic_write_private, exclusive_file_lock, sync_parent_directories

ADVERTISEMENT_FILE = "advertised-bonds.json"
Outpoint = tuple[str, int]
_OUTPOINT_KEY = re.compile(r"[0-9a-f]{64}:[0-9]{1,10}")


class AdvertisementRecordError(Exception):
    """The advertised-bond record is unreadable or could not be written."""


class AdvertisementRecord(BaseModel):
    """The periods through which this data directory may have advertised a bond.

    Every bond counts as advertised through ``since``, the period after the one
    the record was created in, covering earlier runs and versions without this
    record. Individual advertisements extend their bond through the following
    period too. Every field is required, so truncated history is never empty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    since: int = Field(ge=0)
    advertised_through: dict[str, int]

    @field_validator("advertised_through")
    @classmethod
    def valid_entries(cls, value: dict[str, int]) -> dict[str, int]:
        for key, period in value.items():
            if not _OUTPOINT_KEY.fullmatch(key) or period < 0:
                raise ValueError("Invalid advertised bond entry")
        return value

    def through(self, outpoint: Outpoint) -> int:
        """The last period ``outpoint`` may have been advertised in."""
        return max(self.since, self.advertised_through.get(_key(outpoint), -1))

    def rentable(self, outpoint: Outpoint, period: int) -> bool:
        """Whether leasing the bond cannot overlap its recorded advertisements."""
        return self.through(outpoint) < period


def _key(outpoint: Outpoint) -> str:
    return f"{outpoint[0]}:{outpoint[1]}"


def advertisement_record_path(data_dir: Path) -> Path:
    """The advertisement record beside this directory's market store."""
    return get_market_store_path(data_dir).with_name(ADVERTISEMENT_FILE)


def read_advertisements(path: Path) -> AdvertisementRecord | None:
    """Read one snapshot without creating files; absence does not prove no history."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AdvertisementRecordError(f"could not read {path}") from exc
    try:
        return AdvertisementRecord.model_validate_json(raw)
    except ValidationError as exc:
        raise AdvertisementRecordError(
            f"{path} is corrupt; deleting it is safe and pauses bond rental for one period"
        ) from exc


class _LockedAdvertisements:
    """A scoped writer, never a transferable file handle or store owner."""

    def __init__(self, path: Path, current: AdvertisementRecord | None) -> None:
        self._path = path
        self._current = current
        self._closed = False

    @property
    def snapshot(self) -> AdvertisementRecord | None:
        # Pydantic's frozen model still contains a mutable dictionary. Do not
        # expose the writer's own dictionary to callers.
        return None if self._current is None else self._current.model_copy(deep=True)

    def record(self, period: int, outpoint: Outpoint | None = None) -> AdvertisementRecord:
        """Record before proof publication while retaining the admission lock."""
        if self._closed:
            raise AdvertisementRecordError("advertisement history transaction is closed")
        current = self._current
        updated = current or AdvertisementRecord(version=1, since=period + 1, advertised_through={})
        if outpoint is not None and updated.through(outpoint) < period + 1:
            updated = AdvertisementRecord(
                version=1,
                since=updated.since,
                advertised_through={**updated.advertised_through, _key(outpoint): period + 1},
            )
        if updated != current:
            try:
                atomic_write_private(self._path, updated.model_dump_json().encode())
                sync_parent_directories(self._path)
            except OSError as exc:
                raise AdvertisementRecordError(f"could not write {self._path}") from exc
            self._current = updated
        return updated.model_copy(deep=True)


@contextmanager
def locked_advertisements(path: Path, *, blocking: bool = True) -> Iterator[_LockedAdvertisements]:
    """Exclude cooperating processes through a compound admission and durable action.

    The stable sidecar is never removed or replaced. Acquire it before SQLite
    transactions, never across network awaits. Snapshot reads need no sidecar.
    Blocking callers on an event loop must run their compound operation in a
    worker; synchronous admission can instead fail closed with ``blocking=False``.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    with exclusive_file_lock(lock_path, blocking=blocking):
        history = _LockedAdvertisements(path, read_advertisements(path))
        try:
            yield history
        finally:
            history._closed = True


def record_advertisement(
    path: Path, period: int, outpoint: Outpoint | None = None
) -> AdvertisementRecord:
    """Record through P+1, retaining conservative initialization for unknown history."""
    try:
        with locked_advertisements(path) as history:
            return history.record(period, outpoint)
    except OSError as exc:
        raise AdvertisementRecordError(f"could not write {path}") from exc
