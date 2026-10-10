"""The unsafe pre-authorizer image is isolated to an explicit test fixture."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("exit_code", [0, 17])
def test_legacy_build_uses_only_original_patches_and_cleans_its_context(
    tmp_path: Path, exit_code: int
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "build-legacy-buyout-lnd.sh"
    )
    script = scripts / source.name
    shutil.copyfile(source, script)
    patches = tmp_path / "lnd" / "patches"
    patches.mkdir(parents=True)
    (patches.parent / "Dockerfile").write_text("FROM fixture\n")
    for name in (
        "0001-original.patch",
        "0002-quiescence.patch",
        "0003-authorizer.patch",
    ):
        (patches / name).write_text(name)
    commands = tmp_path / "bin"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "context = Path(sys.argv[-1])\n"
        "Path(os.environ['CAPTURE']).write_text(json.dumps({\n"
        "    'args': sys.argv[1:],\n"
        "    'dockerfile': (context / 'Dockerfile').read_text(),\n"
        "    'patches': {p.name: p.read_text() for p in (context / 'patches').iterdir()},\n"
        "}))\n"
        "sys.exit(int(os.environ['BUILD_EXIT']))\n"
    )
    docker.chmod(0o755)
    capture = tmp_path / "captured.json"
    result = subprocess.run(
        ["bash", str(script)],
        env={
            **os.environ,
            "PATH": f"{commands}:{os.environ['PATH']}",
            "CAPTURE": str(capture),
            "BUILD_EXIT": str(exit_code),
            "JM_BUYOUT_LEGACY_LND_IMAGE": "test-only:legacy-v1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == exit_code, result.stderr
    observed = json.loads(capture.read_text())
    assert observed["args"][:3] == ["build", "-t", "test-only:legacy-v1"]
    assert observed["dockerfile"] == "FROM fixture\n"
    assert observed["patches"] == {
        "0001-original.patch": "0001-original.patch",
        "0002-quiescence.patch": "0002-quiescence.patch",
    }
    assert not Path(observed["args"][-1]).exists()
    assert not list((tmp_path / "tmp").iterdir())
