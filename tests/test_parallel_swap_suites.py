"""Exercise parallel-runner swap wiring without Docker or retained fixture deletion."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts/run_parallel_tests.sh").read_text()
RING_SCRIPT = (ROOT / "scripts/run-ring-e2e.sh").read_text()


def _function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", SCRIPT, re.MULTILINE | re.DOTALL)
    assert match is not None
    return match.group()


def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body],
        env={
            **os.environ,
            "PARALLEL_DIR": str(tmp_path),
            "SCRIPT_DIR": str(tmp_path),
            "PROJECT_PREFIX": "jmpt-i7",
            "SKIP_BUILD": "1",
            # Even an inherited destructive reset must not affect a full run.
            "E2E_RESET": "1",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("suite", ["ring", "ring-no-taker", "lnd-external"])
def test_ring_delegation_is_isolated_and_preserves_failure(
    tmp_path: Path, suite: str
) -> None:
    child = tmp_path / "run-ring-e2e.sh"
    child.write_text('#!/bin/bash\nprintf "%s\\n" "$1"\nenv | sort\nexit 23\n')
    child.chmod(0o755)
    arrays = []
    for name in ("SUITE_SLOT", "SVC_SLOT"):
        match = re.search(
            rf"^declare -A {name}=\(.*?^\)", SCRIPT, re.MULTILINE | re.DOTALL
        )
        assert match is not None
        arrays.append(match.group())
    function = "run_suite_" + suite.replace("-", "_")
    result = _run(
        tmp_path,
        "\n".join(
            [
                "log_suite() { :; }",
                "PORT_BAND_BASE=20000; INSTANCE=7; INSTANCE_STRIDE=2000; SUITE_STRIDE=64",
                "JM_TEST_STATIC_IPAM=0",
                *arrays,
                _function("host_port"),
                _function("generate_swap_ipam_override"),
                _function(function),
                function,
            ]
        ),
    )
    assert result.returncode == 23
    log = (tmp_path / f"{suite}.log").read_text()
    prefix = "RING_E2E" if suite in {"ring", "ring-no-taker"} else "LND_EXTERNAL"
    assert f"{prefix}_PROJECT=jmpt-i7-{suite}" in log
    assert f"{prefix}_DATA_DIR={tmp_path}/{suite}-state" in log
    assert "E2E_RESET=0\n" in log
    if suite in {"ring", "ring-no-taker"}:
        assert "RING_E2E_NO_BUILD=1\n" in log
    ports = [
        int(value)
        for value in re.findall(rf"^{prefix}_\w+_PORT=(\d+)$", log, re.MULTILINE)
    ]
    assert len(ports) == (
        9 if suite == "ring" else 7 if suite == "ring-no-taker" else 3
    )
    assert len(set(ports)) == len(ports)
    assert all(34000 <= port < 36000 for port in ports)


@pytest.mark.parametrize("suite", ["ring", "ring-no-taker", "lnd-external", "jmswap"])
def test_generic_cleanup_cannot_delete_swap_fixtures(
    tmp_path: Path, suite: str
) -> None:
    sentinel = tmp_path / "retained-journal"
    sentinel.write_text("signed")
    result = _run(
        tmp_path,
        "\n".join(
            [
                'compose_cmd() { echo "unsafe compose call"; exit 99; }',
                'rm() { echo "unsafe deletion"; exit 99; }',
                _function("cleanup_suite"),
                f"cleanup_suite {suite}",
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert sentinel.read_text() == "signed"


def test_new_tests_are_in_default_and_native_invocations(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "\n".join(
            [
                "log_suite() { :; }",
                'pytest() { printf "%s\\n" "$*"; }',
                "generate_swap_ipam_override() { :; }",
                _function("run_suite_unit"),
                _function("run_suite_jmswap"),
                "run_suite_unit; run_suite_jmswap",
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    units = (tmp_path / "unit.log").read_text().splitlines()
    assert "--cov=jmswap" in units[0]
    assert "jmswap/" in units[0]
    assert "jmwallet/" in units[0]
    assert "jmcore/" in units[0]
    assert "tests/" in units[1]
    native = (tmp_path / "jmswap.log").read_text()
    assert "jmswap/ -m docker --fail-on-skip" in native
    for suite in ("jmswap", "ring", "lnd-external"):
        function = "run_suite_" + suite.replace("-", "_")
        assert f'launch_suite "{suite}" {function}' in SCRIPT
        assert re.search(rf"{suite}\)\s+{function} ;;", SCRIPT)


def test_native_swap_suite_uses_opt_in_subnet(tmp_path: Path) -> None:
    slots = re.search(
        r"^declare -A SUITE_SLOT=\(.*?^\)", SCRIPT, re.MULTILINE | re.DOTALL
    )
    assert slots is not None
    result = _run(
        tmp_path,
        "\n".join(
            [
                "log_suite() { :; }",
                'pytest() { printf "override=%s maker=%s\\n" "$JM_BUYOUT_COMPOSE_OVERRIDE_FILE" "$JM_BUYOUT_MAKER_COMPOSE_OVERRIDE_FILE"; }',
                "JM_TEST_STATIC_IPAM=1; INSTANCE=12",
                slots.group(),
                _function("suite_subnet"),
                _function("generate_swap_ipam_override"),
                _function("run_suite_jmswap"),
                "run_suite_jmswap",
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    override = tmp_path / "docker-compose.jmswap.ipam.yml"
    maker_override = tmp_path / "docker-compose.jmswap.ipam.maker.yml"
    assert (tmp_path / "jmswap.log").read_text() == (
        f"override={override} maker={maker_override}\n"
    )
    assert "10.241.150.0/24" in override.read_text()
    assert "10.241.151.0/24" in maker_override.read_text()


def test_unit_suite_ignores_operator_configuration(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "\n".join(
            [
                "log_suite() { :; }",
                'JOINMARKET_CONFIG_FILE="/operator/config.toml"',
                'pytest() { printf "home=%s data=%s config=%s\\n" "$HOME" "$JOINMARKET_DATA_DIR" "${JOINMARKET_CONFIG_FILE-<unset>}"; }',
                _function("run_suite_unit"),
                "run_suite_unit",
                'printf "after=%s\\n" "$JOINMARKET_CONFIG_FILE"',
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    lines = (tmp_path / "unit.log").read_text().splitlines()
    assert len(lines) == 2
    assert lines[0] == lines[1]
    assert lines[0].startswith(f"home={tmp_path}/unit-config.")
    home = Path(lines[0].split(" data=", 1)[0].removeprefix("home="))
    assert lines[0] == f"home={home} data={home} config=<unset>"
    assert not (home / "config.toml").exists()
    assert "after=/operator/config.toml" in result.stdout


def test_reference_host_helpers_do_not_downgrade_project_packages(
    tmp_path: Path,
) -> None:
    (tmp_path / "joinmarket-clientserver/src/jmclient").mkdir(parents=True)
    result = _run(
        tmp_path,
        "\n".join(
            [
                f'PROJECT_ROOT="{tmp_path}"',
                "log_info() { :; }",
                'pip() { printf "%s\\n" "$*"; }',
                _function("setup_reference_implementation"),
                "setup_reference_implementation",
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    assert "mnemonic==0.20" not in result.stdout
    assert "pyjwt==2.4.0" not in result.stdout


def test_opt_in_ipam_is_disjoint_across_suites_and_instances(tmp_path: Path) -> None:
    slots = re.search(
        r"^declare -A SUITE_SLOT=\(.*?^\)", SCRIPT, re.MULTILINE | re.DOTALL
    )
    assert slots is not None
    result = _run(
        tmp_path,
        "\n".join(
            [
                slots.group(),
                _function("suite_subnet"),
                _function("generate_swap_ipam_override"),
                "INSTANCE=8; JM_TEST_STATIC_IPAM=1",
                "generate_swap_ipam_override ring ring-network; printf '\\n'",
                "generate_swap_ipam_override lnd-external default; printf '\\n'",
                "INSTANCE=9; suite_subnet ring 0; printf '\\n'",
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "10.241.56.0/24"
    ring = (tmp_path / "docker-compose.ring.ipam.yml").read_text()
    external = (tmp_path / "docker-compose.lnd-external.ipam.yml").read_text()
    assert "10.241.24.0/24" in ring
    assert "10.241.25.0/24" in ring
    assert "10.241.26.0/24" in external
    assert external.count("  default:") == 1


def test_generic_compose_override_only_sets_subnets_when_opted_in(
    tmp_path: Path,
) -> None:
    arrays = []
    for name in ("SUITE_SLOT", "SVC_SLOT"):
        match = re.search(
            rf"^declare -A {name}=\(.*?^\)", SCRIPT, re.MULTILINE | re.DOTALL
        )
        assert match is not None
        arrays.append(match.group())
    result = _run(
        tmp_path,
        "\n".join(
            [
                "INSTANCE=8; INSTANCE_STRIDE=2000; SUITE_STRIDE=64; PORT_BAND_BASE=20000",
                "CONTAINER_PREFIX=jm-i8; JM_TEST_STATIC_IPAM=1",
                *arrays,
                _function("host_port"),
                _function("suite_subnet"),
                _function("generate_override"),
                "generate_override e2e",
                "JM_TEST_STATIC_IPAM=0; generate_override playwright",
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    e2e = (tmp_path / "docker-compose.e2e.override.yml").read_text()
    playwright = (tmp_path / "docker-compose.playwright.override.yml").read_text()
    assert "10.241.0.0/24" in e2e and "10.241.1.0/24" in e2e
    assert "  jm-network:\n    ipam:" in e2e
    assert "ipam:" not in playwright


@pytest.mark.parametrize(
    "function,variable",
    [("ring_compose", "RING_E2E"), ("lnd_external_compose", "LND_EXTERNAL")],
)
def test_swap_compose_includes_ipam_override_only_when_supplied(
    tmp_path: Path, function: str, variable: str
) -> None:
    compose_function = re.search(
        rf"^{function}\(\) \{{\n.*?^\}}", RING_SCRIPT, re.MULTILINE | re.DOTALL
    )
    assert compose_function is not None
    result = _run(
        tmp_path,
        "\n".join(
            [
                'docker() { printf "%s\\n" "$@"; }',
                f'{variable}_PROJECT="test-project"',
                f'{variable}_IPAM_FILE="{tmp_path}/ipam.yml"',
                f'PROJECT_ROOT="{ROOT}"',
                compose_function.group(),
                f"{function} config",
                f'{variable}_IPAM_FILE=""',
                f"{function} config",
            ]
        ),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count(str(tmp_path / "ipam.yml")) == 1
    assert result.stdout.count("config\n") == 2
