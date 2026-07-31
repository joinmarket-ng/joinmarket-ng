from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMBINED_RUNTIME_LOCKS = (
    "jmcore/requirements.txt",
    "jmwallet/requirements.txt",
    "jmswap/requirements.txt",
    "maker/requirements.txt",
    "taker/requirements.txt",
    "orderbook_watcher/requirements.txt",
    "jmwalletd/requirements.txt",
)
EXACT_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\;]+)")


def _canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_combined_runtime_locks_have_consistent_exact_pins() -> None:
    pins: dict[str, dict[str, list[str]]] = {}

    for relative_path in COMBINED_RUNTIME_LOCKS:
        for line in (
            (REPO_ROOT / relative_path).read_text(encoding="utf-8").splitlines()
        ):
            match = EXACT_PIN.match(line)
            if match is None:
                continue
            package = _canonical_name(match.group(1))
            version = match.group(2)
            pins.setdefault(package, {}).setdefault(version, []).append(relative_path)

    conflicts = {
        package: versions for package, versions in pins.items() if len(versions) > 1
    }
    assert conflicts == {}, f"combined runtime lock conflicts: {conflicts}"
