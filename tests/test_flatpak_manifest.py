from __future__ import annotations

from pathlib import Path
import re
import xml.etree.ElementTree as ET
from typing import Any

import yaml


def _manifest() -> dict[str, Any]:
    manifest_path = (
        Path(__file__).resolve().parents[1] / "flatpak" / "org.joinmarketng.JamNG.yml"
    )
    return yaml.safe_load(manifest_path.read_text(encoding="utf-8"))


def test_application_source_excludes_local_state() -> None:
    manifest = _manifest()
    application_module = next(
        module for module in manifest["modules"] if module["name"] == "jam-ng"
    )
    application_source = application_module["sources"][0]

    assert application_source["type"] == "dir"
    assert application_source["path"] == ".."
    assert {".git", ".flatpak-builder", "build-dir", "flatpak-repo", "tmp"} <= set(
        application_source["skip"]
    )


def test_manifest_builds_recovery_enabled_libsecp256k1() -> None:
    manifest = _manifest()
    secp_module = next(
        module for module in manifest["modules"] if module["name"] == "libsecp256k1"
    )

    assert secp_module["buildsystem"] == "cmake-ninja"
    assert "-DSECP256K1_ENABLE_MODULE_RECOVERY=ON" in secp_module["config-opts"]
    assert "-DSECP256K1_ENABLE_MODULE_MUSIG=ON" in secp_module["config-opts"]
    source = secp_module["sources"][0]
    assert source["type"] == "git"
    assert source["commit"] == "6e2c8bc4ecdc6e71dbe7a368f360d8d453ce435d"


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
