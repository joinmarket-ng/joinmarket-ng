from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


def _entrypoint_text() -> str:
    entrypoint = (
        Path(__file__).resolve().parents[1] / "flatpak" / "jam-ng-entrypoint.sh"
    )
    return entrypoint.read_text(encoding="utf-8")


def test_neutrino_start_reads_prefetch_related_settings_from_config() -> None:
    text = _entrypoint_text()

    assert 'read_config_value "bitcoin" "neutrino_clearnet_initial_sync" "true"' in text
    assert 'read_config_value "bitcoin" "neutrino_prefetch_filters" "true"' in text
    assert (
        'read_config_value "bitcoin" "neutrino_prefetch_lookback_blocks" "105120"'
        in text
    )


def test_neutrino_start_passes_prefetch_env_to_neutrinod() -> None:
    text = _entrypoint_text()

    assert 'CLEARNET_INITIAL_SYNC="${clearnet_initial_sync}" \\' in text
    assert 'PREFETCH_FILTERS="${prefetch_filters}" \\' in text
    assert 'PREFETCH_LOOKBACK="${prefetch_lookback_blocks}" \\' in text


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")
@pytest.mark.parametrize(
    "existing", [None, b"[bitcoin]\nbackend_type = 'descriptor_wallet'\n"]
)
def test_flatpak_config_setup_uses_shared_starter_and_preserves_existing(
    tmp_path: Path, existing: bytes | None
) -> None:
    from jmcore.settings import _get_bundled_template, generate_config_starter

    config = tmp_path / "config.toml"
    if existing is not None:
        config.write_bytes(existing)
    (tmp_path / "config.toml.template").write_text("# stale reference\n")
    setup = (
        _entrypoint_text().split("setup_data_dir() {", 1)[1].split("\nsave_env()", 1)[0]
    )
    script = f"""
set -e
setup_data_dir() {{{setup}
stop_stale_instance() {{ :; }}
sed() {{ printf '%s\\n' '# test torrc'; }}
python3() {{ {shlex.quote(sys.executable)} "$@"; }}
DATA_DIR={shlex.quote(str(tmp_path))}
TOR_DIR="$DATA_DIR/tor"
TOR_DATA_DIR="$TOR_DIR/data"
NET_DATA_DIR="$DATA_DIR/network"
NEUTRINO_DATA_DIR="$DATA_DIR/neutrino"
LOG_DIR="$DATA_DIR/logs"
PIDFILE_DIR="$DATA_DIR/pids"
TORRC="$TOR_DIR/torrc"
CONFIG_FILE="$DATA_DIR/config.toml"
setup_data_dir
"""
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "JOINMARKET_DATA_DIR": str(tmp_path),
            "BITCOIN__BACKEND_TYPE": "neutrino",
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == (
        existing if existing is not None else generate_config_starter().encode()
    )
    assert (tmp_path / "config.toml.template").read_text() == _get_bundled_template()
    if existing is None:
        assert tomllib.loads(config.read_text())["bitcoin"] == {}
    assert config.stat().st_mode & 0o777 == 0o600
