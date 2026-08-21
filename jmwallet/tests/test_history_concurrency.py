"""Cross-process serialization tests for the shared history CSV."""

from __future__ import annotations

import multiprocessing
import os
import queue
from pathlib import Path
from typing import Any

import pytest

from jmwallet.history import (
    TransactionHistoryEntry,
    append_history_entry,
    read_history,
    update_transaction_confirmation,
)


def _entry(txid: str, *, success: bool = True) -> TransactionHistoryEntry:
    return TransactionHistoryEntry(
        timestamp=f"2024-01-01T00:00:{txid[-1]}0",
        txid=txid,
        success=success,
        failure_reason="" if success else "Pending confirmation",
    )


def _append_after_start(
    data_dir: str,
    txid: str,
    start: Any,
    completed: Any,
) -> None:
    start.wait(10)
    append_history_entry(_entry(txid), Path(data_dir))
    completed.put(("append", txid))


def _delayed_confirmation_update(
    data_dir: str,
    snapshot_ready: Any,
    allow_write: Any,
    completed: Any,
) -> None:
    import jmwallet.history as history

    original_write = history._write_history_entries_atomic

    def delayed_write(entries: Any, history_path: Any) -> bool:
        snapshot_ready.set()
        if not allow_write.wait(10):
            return False
        return original_write(entries, history_path)

    history._write_history_entries_atomic = delayed_write
    try:
        completed.put(
            (
                "update",
                update_transaction_confirmation("pending-tx", 1, Path(data_dir)),
            )
        )
    finally:
        history._write_history_entries_atomic = original_write


@pytest.mark.skipif(os.name not in {"posix", "nt"}, reason="no supported file-locking primitive")
class TestHistoryConcurrency:
    """History mutations retain changes made by parallel processes."""

    def test_simultaneous_appends_retain_both_entries(self, tmp_path: Path) -> None:
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        completed = context.Queue()
        processes = [
            context.Process(
                target=_append_after_start,
                args=(str(tmp_path), txid, start, completed),
            )
            for txid in ("append-a", "append-b")
        ]
        for process in processes:
            process.start()
        start.set()

        assert {completed.get(timeout=10), completed.get(timeout=10)} == {
            ("append", "append-a"),
            ("append", "append-b"),
        }
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0

        assert {entry.txid for entry in read_history(tmp_path)} == {"append-a", "append-b"}

    def test_append_waits_for_rewrite_and_preserves_both_changes(self, tmp_path: Path) -> None:
        append_history_entry(_entry("pending-tx", success=False), tmp_path)

        context = multiprocessing.get_context("spawn")
        snapshot_ready = context.Event()
        allow_write = context.Event()
        completed = context.Queue()
        rewrite = context.Process(
            target=_delayed_confirmation_update,
            args=(str(tmp_path), snapshot_ready, allow_write, completed),
        )
        rewrite.start()
        assert snapshot_ready.wait(10)

        start_append = context.Event()
        start_append.set()
        append = context.Process(
            target=_append_after_start,
            args=(str(tmp_path), "appended-tx", start_append, completed),
        )
        append.start()
        with pytest.raises(queue.Empty):
            completed.get(timeout=0.2)

        allow_write.set()
        assert {completed.get(timeout=10), completed.get(timeout=10)} == {
            ("append", "appended-tx"),
            ("update", True),
        }
        for process in (rewrite, append):
            process.join(timeout=10)
            assert process.exitcode == 0

        entries = {entry.txid: entry for entry in read_history(tmp_path)}
        assert set(entries) == {"pending-tx", "appended-tx"}
        assert entries["pending-tx"].confirmations == 1
        assert entries["pending-tx"].success is True
