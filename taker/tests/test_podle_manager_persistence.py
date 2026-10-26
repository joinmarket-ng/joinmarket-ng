"""Durability of the taker's used PoDLE commitment record."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from loguru import logger

from taker.podle_manager import PoDLEManager

_SAVE_WITH_SIZE_LIMIT = textwrap.dedent(
    """
    import resource
    import signal
    import sys
    from pathlib import Path

    from taker.podle_manager import PoDLEManager

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    manager = PoDLEManager(data_dir=Path(sys.argv[1]))
    limit = manager.filepath.stat().st_size // 2
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, resource.RLIM_INFINITY))
    manager.used_commitments.add("ff" * 32)
    manager._save()
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="needs RLIMIT_FSIZE")
def test_failed_save_keeps_previous_used_commitments(tmp_path: Path) -> None:
    """A write that fails part way (disk full) must not lose earlier commitments."""
    manager = PoDLEManager(data_dir=tmp_path)
    manager.used_commitments = {f"{i:064x}" for i in range(200)}
    manager._save()

    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    subprocess.run(
        [sys.executable, "-c", _SAVE_WITH_SIZE_LIMIT, str(tmp_path)],
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )

    assert PoDLEManager(data_dir=tmp_path).used_commitments >= manager.used_commitments


def test_corrupt_commitments_file_is_not_discarded(tmp_path: Path) -> None:
    filepath = PoDLEManager(data_dir=tmp_path).filepath
    corrupt = '{"used": ["' + "ab" * 32 + '", '
    filepath.write_text(corrupt)

    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(message.record["message"]))
    try:
        manager = PoDLEManager(data_dir=tmp_path)
        manager.used_commitments.add("cd" * 32)
        manager._save()
    finally:
        logger.remove(handler_id)

    preserved = [p for p in filepath.parent.iterdir() if p.read_text() == corrupt]
    assert preserved, "corrupt commitments file was overwritten"
    assert any(str(preserved[0]) in record for record in records)
    assert PoDLEManager(data_dir=tmp_path).used_commitments == {"cd" * 32}
