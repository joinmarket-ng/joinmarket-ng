"""Durable advertisement history and its shared proof/lease admission exclusion."""

from __future__ import annotations

import json
import os
import re
import stat
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
BitcoinNetwork = Literal["mainnet", "testnet", "signet", "regtest"]


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
        return _validate_entries(value)

    def through(self, outpoint: Outpoint) -> int:
        """The last period ``outpoint`` may have been advertised in."""
        return _through(self.since, self.advertised_through, outpoint)

    def rentable(self, outpoint: Outpoint, period: int) -> bool:
        """Whether leasing the bond cannot overlap its recorded advertisements."""
        return self.through(outpoint) < period


class BoundAdvertisementRecord(BaseModel):
    """Complete fresh history followed by monotonic advertisements, never reusable freshness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[2]
    wallet_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    network: BitcoinNetwork
    since: Literal[-1]
    advertised_through: dict[str, int]

    @field_validator("advertised_through")
    @classmethod
    def valid_entries(cls, value: dict[str, int]) -> dict[str, int]:
        return _validate_entries(value)

    def through(self, outpoint: Outpoint) -> int:
        return _through(self.since, self.advertised_through, outpoint)

    def rentable(self, outpoint: Outpoint, period: int) -> bool:
        return self.through(outpoint) < period


AdvertisementHistoryRecord = AdvertisementRecord | BoundAdvertisementRecord


def _validate_entries(value: dict[str, int]) -> dict[str, int]:
    for key, period in value.items():
        if not _OUTPOINT_KEY.fullmatch(key) or period < 0:
            raise ValueError("Invalid advertised bond entry")
    return value


def _through(since: int, entries: dict[str, int], outpoint: Outpoint) -> int:
    return max(since, entries.get(_key(outpoint), -1))


def _key(outpoint: Outpoint) -> str:
    return f"{outpoint[0]}:{outpoint[1]}"


def advertisement_record_path(data_dir: Path) -> Path:
    """The advertisement record beside this directory's market store."""
    return get_market_store_path(data_dir).with_name(ADVERTISEMENT_FILE)


def read_advertisements(
    path: Path,
    *,
    wallet_id: str | None = None,
    network: str | None = None,
    require_bound: bool = False,
) -> AdvertisementHistoryRecord | None:
    """Read one snapshot without creating files; absence does not prove no history."""
    try:
        if path.is_symlink():
            raise OSError("advertisement history must not be a symlink")
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError("advertisement history must be a regular file")
            raw = source.read()
    except FileNotFoundError:
        if require_bound:
            raise AdvertisementRecordError("required advertisement history is missing") from None
        return None
    except OSError as exc:
        raise AdvertisementRecordError(f"could not read {path}") from exc
    try:
        document = json.loads(raw)
        if isinstance(document, dict) and document.get("version") == 2:
            bound = BoundAdvertisementRecord.model_validate_json(raw)
            if bound.wallet_id != wallet_id or bound.network != network:
                raise AdvertisementRecordError(
                    "advertisement history wallet/network binding mismatch"
                )
            return bound
        legacy = AdvertisementRecord.model_validate_json(raw)
        if require_bound:
            raise AdvertisementRecordError("required bound advertisement history was replaced")
        return legacy
    except (ValidationError, ValueError, UnicodeError) as exc:
        raise AdvertisementRecordError(
            f"{path} is corrupt; preserve history for explicit review"
        ) from exc


class _LockedAdvertisements:
    """A scoped writer, never a transferable file handle or store owner."""

    def __init__(self, path: Path, current: AdvertisementHistoryRecord | None) -> None:
        self._path = path
        self._current = current
        self._closed = False

    @property
    def snapshot(self) -> AdvertisementHistoryRecord | None:
        # Pydantic's frozen model still contains a mutable dictionary. Do not
        # expose the writer's own dictionary to callers.
        return None if self._current is None else self._current.model_copy(deep=True)

    def record(self, period: int, outpoint: Outpoint | None = None) -> AdvertisementHistoryRecord:
        """Record before proof publication while retaining the admission lock."""
        if self._closed:
            raise AdvertisementRecordError("advertisement history transaction is closed")
        current = self._current
        updated = current or AdvertisementRecord(version=1, since=period + 1, advertised_through={})
        if outpoint is not None and updated.through(outpoint) < period + 1:
            updated = type(updated).model_validate(
                {
                    **updated.model_dump(),
                    "advertised_through": {
                        **updated.advertised_through,
                        _key(outpoint): period + 1,
                    },
                }
            )
        if updated != current:
            try:
                atomic_write_private(self._path, updated.model_dump_json().encode())
                sync_parent_directories(self._path)
            except OSError as exc:
                raise AdvertisementRecordError(f"could not write {self._path}") from exc
            self._current = updated
        return updated.model_copy(deep=True)

    def require_bound(self) -> BoundAdvertisementRecord:
        """Require real bound history; disappearance never selects legacy initialization."""
        if not isinstance(self._current, BoundAdvertisementRecord):
            raise AdvertisementRecordError("required bound advertisement history is unavailable")
        return self._current.model_copy(deep=True)

    def _initialize_fresh(self, record: BoundAdvertisementRecord) -> None:
        if self._closed or self._current is not None:
            raise AdvertisementRecordError(
                "advertisement history already exists or writer is closed"
            )
        try:
            atomic_write_private(self._path, record.model_dump_json().encode(), replace=False)
        except OSError as exc:
            raise AdvertisementRecordError(
                "fresh advertisement history publication is incomplete"
            ) from exc
        self._current = record.model_copy(deep=True)


@contextmanager
def locked_advertisements(
    path: Path,
    *,
    wallet_id: str | None = None,
    network: str | None = None,
    require_bound: bool = False,
    blocking: bool = True,
) -> Iterator[_LockedAdvertisements]:
    """Exclude cooperating processes through a compound admission and durable action.

    The stable sidecar is never removed or replaced. Acquire it before SQLite
    transactions, never across network awaits. Snapshot reads need no sidecar.
    Blocking callers on an event loop must run their compound operation in a
    worker; synchronous admission can instead fail closed with ``blocking=False``.
    """
    lock_path = path.with_name(f"{path.name}.lock")
    with exclusive_file_lock(lock_path, blocking=blocking):
        history = _LockedAdvertisements(
            path,
            read_advertisements(
                path,
                wallet_id=wallet_id,
                network=network,
                require_bound=require_bound,
            ),
        )
        try:
            yield history
        finally:
            history._closed = True


def record_advertisement(
    path: Path,
    period: int,
    outpoint: Outpoint | None = None,
    *,
    wallet_id: str | None = None,
    network: str | None = None,
    require_bound: bool = False,
) -> AdvertisementHistoryRecord:
    """Record through P+1, retaining conservative initialization for unknown history."""
    try:
        with locked_advertisements(
            path, wallet_id=wallet_id, network=network, require_bound=require_bound
        ) as history:
            return history.record(period, outpoint)
    except OSError as exc:
        raise AdvertisementRecordError(f"could not write {path}") from exc


def initialize_fresh_advertisements(
    path: Path,
    wallet_id: str,
    network: str,
    *,
    history_confirmed: bool = False,
) -> None:
    """Publish empty history only on a caller's uninterrupted fresh-generation authority.

    This is not recovery. No runtime or ready-ledger path may call it based on
    missing history, generation origin, or old publication records.
    """
    if history_confirmed is not True:
        raise AdvertisementRecordError(
            "fresh advertisement initialization requires confirmed history"
        )
    record = BoundAdvertisementRecord.model_validate(
        {
            "version": 2,
            "wallet_id": wallet_id,
            "network": network,
            "since": -1,
            "advertised_through": {},
        }
    )
    with locked_advertisements(path, wallet_id=wallet_id, network=network) as history:
        history._initialize_fresh(record)
