from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from jmswap.models import SwapRecord


@dataclass(frozen=True)
class LoadError:
    path: Path
    error: str


@dataclass(frozen=True)
class LoadReport:
    records: list[SwapRecord]
    errors: list[LoadError]


class SwapStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)

    def _path(self, swap_id: str) -> Path:
        if len(swap_id) != 64 or any(char not in "0123456789abcdef" for char in swap_id):
            raise ValueError("invalid swap ID")
        return self.directory / f"{swap_id}.json"

    def save(self, record: SwapRecord) -> None:
        path = self._path(record.id)
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".swap-", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(record.model_dump_json(by_alias=True))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
            self._fsync_directory()
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def load(self, swap_id: str) -> SwapRecord | None:
        path = self._path(swap_id)
        if not path.exists():
            return None
        return SwapRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def scan(self) -> LoadReport:
        records: list[SwapRecord] = []
        errors: list[LoadError] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(SwapRecord.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                errors.append(LoadError(path, str(exc)))
        return LoadReport(records, errors)

    def _fsync_directory(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
