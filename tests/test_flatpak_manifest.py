from __future__ import annotations

from pathlib import Path

import yaml


def test_application_source_excludes_local_state() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[1] / "flatpak" / "org.joinmarketng.JamNG.yml"
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    application_module = next(
        module for module in manifest["modules"] if module["name"] == "jam-ng"
    )
    application_source = application_module["sources"][0]

    assert application_source["type"] == "dir"
    assert application_source["path"] == ".."
    assert {".git", ".flatpak-builder", "build-dir", "flatpak-repo", "tmp"} <= set(
        application_source["skip"]
    )
