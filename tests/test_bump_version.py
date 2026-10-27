from __future__ import annotations

import importlib.util
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def test_release_updates_the_swap_component_with_the_other_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_bump_version_module()
    # Exercise the real release inventory against copies, never the checkout.
    projects = []
    for source in module.PYPROJECT_FILES:
        project = tmp_path / source.parent.name / source.name
        project.parent.mkdir()
        project.write_text(source.read_text())
        projects.append(project)
    assert {project.parent.name for project in projects} >= {"jmcore", "jmswap"}
    monkeypatch.setattr(module, "PYPROJECT_FILES", projects)

    module.update_pyproject_files("1.2.3")

    for project in projects:
        assert tomllib.loads(project.read_text())["project"]["version"] == "1.2.3"


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


def test_update_install_script_only_touches_the_assignment_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install.sh contains the literal text DEFAULT_VERSION=" inside the
    refresh_installer sed expression; the version bump must rewrite only the
    top-level assignment. An unanchored substitution corrupted the sed
    expression when tagging 0.39.1."""
    module = _load_bump_version_module()
    install_script = tmp_path / "install.sh"
    sed_line = (
        "    candidate_version=$(sed -n"
        ' \'s/^DEFAULT_VERSION="\\([^"]*\\)".*/\\1/p\' "$candidate")\n'
    )
    install_script.write_text(
        '#!/usr/bin/env bash\nDEFAULT_VERSION="1.0.0"  # Updated on each release\n'
        + sed_line
    )
    monkeypatch.setattr(module, "INSTALL_SCRIPT", install_script)

    module.update_install_script("1.0.1")

    content = install_script.read_text()
    assert 'DEFAULT_VERSION="1.0.1"  # Updated on each release' in content
    assert sed_line in content


def test_update_install_script_requires_exactly_one_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_bump_version_module()
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/usr/bin/env bash\necho no assignment here\n")
    monkeypatch.setattr(module, "INSTALL_SCRIPT", install_script)

    with pytest.raises(RuntimeError, match="found 0"):
        module.update_install_script("1.0.1")


def test_repo_install_script_has_exactly_one_default_version_assignment() -> None:
    """Guards the anchored-substitution contract against future install.sh
    refactors that would make the release bump ambiguous."""
    install_script = Path(__file__).resolve().parents[1] / "install.sh"
    lines = install_script.read_text().splitlines()
    assignments = [line for line in lines if line.startswith('DEFAULT_VERSION="')]
    assert len(assignments) == 1


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
