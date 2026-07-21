from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def _load_bump_version_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location(
        "bump_version_under_test", script_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_flatpak_metainfo_prepends_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_bump_version_module()
    metainfo = tmp_path / "app.metainfo.xml"
    metainfo.write_text(
        """<component>
  <releases>
    <release version="1.0.0" date="2026-01-01"/>
  </releases>
</component>
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "FLATPAK_METAINFO", metainfo)

    module.update_flatpak_metainfo("1.1.0", release_date="2026-02-02")
    module.update_flatpak_metainfo("1.1.0", release_date="2026-02-03")

    releases = ET.parse(metainfo).getroot().findall("./releases/release")
    assert [(release.get("version"), release.get("date")) for release in releases] == [
        ("1.1.0", "2026-02-03"),
        ("1.0.0", "2026-01-01"),
    ]


def test_update_flatpak_metainfo_dry_run_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_bump_version_module()
    metainfo = tmp_path / "app.metainfo.xml"
    original = """<component>
  <releases>
  </releases>
</component>
"""
    metainfo.write_text(original, encoding="utf-8")
    monkeypatch.setattr(module, "FLATPAK_METAINFO", metainfo)

    module.update_flatpak_metainfo("1.0.0", dry_run=True, release_date="2026-01-01")

    assert metainfo.read_text(encoding="utf-8") == original
