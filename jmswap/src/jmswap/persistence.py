"""
Crash-safe persistence for in-flight swaps.

A swap involves on-chain funds locked under a timelock, so a provider or client
must be able to recover (claim or refund) an in-flight swap after a restart.
:class:`SwapStore` persists each :class:`~jmswap.models.SwapContract` as an
atomically-written JSON file keyed by ``swap_id``; the server/client reload them
on startup to resume or refund.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from jmswap.models import SwapContract


class SwapStore:
    """A directory of swap contracts persisted as atomic JSON files."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, swap_id: str) -> Path:
        safe = swap_id.replace("/", "_").replace("..", "_")
        return self._dir / f"{safe}.json"

    def save(self, contract: SwapContract) -> None:
        """Persist ``contract`` atomically (write to a temp file then rename)."""
        path = self._path(contract.swap_id)
        data = contract.model_dump_json()
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".swap-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def load(self, swap_id: str) -> SwapContract | None:
        path = self._path(swap_id)
        if not path.exists():
            return None
        return SwapContract.model_validate_json(path.read_text())

    def list(self) -> list[SwapContract]:
        contracts: list[SwapContract] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                contracts.append(SwapContract.model_validate_json(path.read_text()))
            except Exception:  # noqa: BLE001 - skip corrupt records
                continue
        return contracts

    def delete(self, swap_id: str) -> None:
        with contextlib.suppress(OSError):
            self._path(swap_id).unlink()
