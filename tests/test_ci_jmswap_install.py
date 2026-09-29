"""CI installs local jmswap before components that require its unpublished package."""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_windows_install_accepts_both_main_and_pr_source_layouts() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yaml").read_text())
    steps = workflow["jobs"]["test-install-windows"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Manual pip install (Windows has no install.sh path)"
    )

    assert '$projects = @("./jmcore", "./jmwallet", "./taker")' in script
    assert (
        'if (Test-Path ./jmswap/pyproject.toml) { $projects += "./jmswap" }' in script
    )
    assert "pip install @projects" in script


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
