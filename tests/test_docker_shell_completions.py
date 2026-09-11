"""Shell completions shipped inside the runtime Docker images.

The images do not install Debian's ``bash-completion`` package, so the generated
scripts are copied to ``/etc/bash_completion.d`` and pulled in by a loader that
``/etc/bash.bashrc`` sources for interactive shells.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLETIONS_DIR = REPO_ROOT / "completions"
LOADER = COMPLETIONS_DIR / "jm-completions-loader.sh"

# Images whose bundled CLIs have generated completion scripts. The directory
# server and orderbook watcher entry points are argparse based and have none.
COMPLETION_IMAGES = {
    "maker": REPO_ROOT / "maker" / "Dockerfile",
    "taker": REPO_ROOT / "taker" / "Dockerfile",
    "jmwalletd": REPO_ROOT / "jmwalletd" / "Dockerfile",
}

BUILDER_STAGE_COPY = (
    "COPY completions/*.bash completions/jm-completions-loader.sh /build/completions/"
)
RUNTIME_STAGE_COPY = "COPY --from=builder /build/completions /etc/bash_completion.d"
BASHRC_LINE = (
    "echo '. /etc/bash_completion.d/jm-completions-loader.sh' >> /etc/bash.bashrc"
)


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def test_loader_registers_every_generated_completion() -> None:
    """Sourcing the loader must register a completion for each generated script."""
    commands = sorted(path.stem for path in COMPLETIONS_DIR.glob("*.bash"))
    assert commands, "no generated bash completions found"

    result = _run_bash(f'set -u; . "{LOADER}"; complete -p {" ".join(commands)}')

    assert result.returncode == 0, result.stderr
    registered = {line.rsplit(" ", 1)[-1] for line in result.stdout.splitlines()}
    assert registered == set(commands)


def test_loader_is_a_noop_without_completion_scripts(tmp_path: Path) -> None:
    """An empty completion directory must not break the shell rc that sources it."""
    loader_copy = tmp_path / LOADER.name
    loader_copy.write_text(LOADER.read_text())

    result = _run_bash(f'set -u; . "{loader_copy}"; echo loaded')

    assert result.returncode == 0, result.stderr
    assert "loaded" in result.stdout


@pytest.mark.parametrize("image", sorted(COMPLETION_IMAGES))
def test_runtime_image_ships_and_loads_completions(image: str) -> None:
    content = COMPLETION_IMAGES[image].read_text()

    assert BUILDER_STAGE_COPY in content
    assert RUNTIME_STAGE_COPY in content
    assert BASHRC_LINE in content
    # /etc/bash.bashrc is only writable before the image drops to the jm user.
    if "\nUSER jm\n" in content:
        assert content.index(BASHRC_LINE) < content.index("\nUSER jm\n")


@pytest.mark.parametrize("image", sorted(COMPLETION_IMAGES))
def test_staged_completions_are_timestamp_normalized(image: str) -> None:
    """Normalizing mtimes in the builder keeps the runtime COPY layer reproducible."""
    content = COMPLETION_IMAGES[image].read_text()
    normalize_blocks = [
        block
        for block in content.split("RUN ")
        if "touch -d @${SOURCE_DATE_EPOCH}" in block and "find " in block
    ]

    assert any("/build/completions" in block for block in normalize_blocks), (
        f"{image} builder does not normalize /build/completions timestamps"
    )
    assert content.index(BUILDER_STAGE_COPY) < content.index(RUNTIME_STAGE_COPY)
