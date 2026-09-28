"""CI installs local jmswap before components that require its unpublished package."""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    ("components", "expected"),
    [
        (
            "jmcore,jmwallet,maker,taker,jmswap",
            ["jmcore", "jmwallet", "jmswap", "maker", "taker"],
        ),
        ("jmcore,jmwallet,taker", ["jmcore", "jmwallet", "jmswap", "taker"]),
        ("jmcore,jmwallet", ["jmcore", "jmwallet"]),
    ],
)
def test_setup_installs_swap_before_dependents_without_duplicates(
    components: str, expected: list[str]
) -> None:
    action = (ROOT / ".github/actions/setup-python-deps/action.yaml").read_text()
    step = action.split("    - name: Install dependencies\n", 1)[1].split(
        "    - name:", 1
    )[0]
    script = textwrap.dedent(step.split("      run: |\n", 1)[1])
    script = script.replace("${{ inputs.components }}", components).replace(
        "${{ inputs.install-dev }}", "true"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", 'pip() { printf "PIP:%s\\n" "$*"; }\n' + script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert [line for line in result.stdout.splitlines() if line.startswith("PIP:")] == [
        f"PIP:install -e {package}[dev]" for package in expected
    ]


def test_image_and_installer_locks_agree_on_idna() -> None:
    locks = [
        ROOT / package / "requirements.txt"
        for package in ("jmcore", "jmwallet", "jmswap", "maker", "taker", "jmwalletd")
    ]
    versions: list[str] = []
    for lock in locks:
        match = re.search(r"^idna==([^ ]+)", lock.read_text(), re.MULTILINE)
        assert match is not None, lock
        versions.append(match.group(1))
    assert len(set(versions)) == 1, list(zip(locks, versions, strict=True))
