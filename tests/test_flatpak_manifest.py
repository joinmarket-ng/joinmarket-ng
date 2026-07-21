from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET

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


def test_latest_appstream_release_matches_project_version() -> None:
    project_root = Path(__file__).resolve().parents[1]
    version_text = (
        project_root / "jmcore" / "src" / "jmcore" / "version.py"
    ).read_text(encoding="utf-8")
    version_match = re.search(r'^__version__ = "([^"]+)"$', version_text, re.MULTILINE)
    assert version_match is not None

    root = ET.parse(
        project_root / "flatpak" / "org.joinmarketng.JamNG.metainfo.xml"
    ).getroot()
    releases = root.find("releases")
    assert releases is not None
    latest_release = releases.find("release")
    assert latest_release is not None
    assert latest_release.get("version") == version_match.group(1)
