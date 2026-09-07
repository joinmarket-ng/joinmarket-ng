"""Hermetic config lifecycle regressions for the source installer."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
CHANGELOG_HELPER = REPO_ROOT / "scripts/config_changelog.py"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def run_shell(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"source {shlex.quote(str(INSTALL_SH))}\n{script}"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def write_release_file(release: Path, path: str, content: str) -> None:
    destination = release / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)


def source_reader() -> str:
    return """
read_release_file() {
    local path="$1"
    if [[ -f "$TEST_RELEASE/$path" ]]; then
        command cat "$TEST_RELEASE/$path"
    else
        return 1
    fi
}
"""


def config_env(tmp_path: Path, release: Path) -> dict[str, str]:
    return {
        "JOINMARKET_DATA_DIR": str(tmp_path / "data"),
        "TEST_RELEASE": str(release),
        "TMPDIR": str(tmp_path),
    }


@pytest.mark.parametrize(
    "original",
    [
        b"[bitcoin]\n# rpc_url = 'http://127.0.0.1:8332'\n[wallet]\n# legacy = true\n",
        b"[bitcoin]\nrpc_password = 'do-not-print'\n",
    ],
    ids=["legacy-full", "sparse"],
)
def test_existing_configs_are_byte_identical_and_symlink_aliases_survive(
    tmp_path: Path, original: bytes
) -> None:
    release = tmp_path / "release"
    write_release_file(
        release,
        "jmcore/src/jmcore/data/config-starter.toml.template",
        "[bitcoin]\n# starter\n",
    )
    write_release_file(
        release, "jmcore/src/jmcore/data/config.toml.template", "[bitcoin]\n# full\n"
    )
    data = tmp_path / "data"
    data.mkdir()
    configured_config = tmp_path / "configured-config.toml"
    configured_config.write_bytes(original)
    (data / "config.toml").symlink_to(configured_config)
    configured_reference = tmp_path / "configured-reference.toml"
    configured_reference.write_text("old reference\n")
    (data / "config.toml.template").symlink_to(configured_reference)

    result = run_shell(
        f"""
SKIP_VERIFY=true
{source_reader()}
setup_data_directory
""",
        config_env(tmp_path, release),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (data / "config.toml").is_symlink()
    assert configured_config.read_bytes() == original
    assert (data / "config.toml.template").is_symlink()
    assert configured_reference.read_text() == "[bitcoin]\n# full\n"


def test_failed_reference_read_preserves_previous_reference(tmp_path: Path) -> None:
    release = tmp_path / "release"
    data = tmp_path / "data"
    data.mkdir()
    original_config = b"[bitcoin]\nrpc_password = 'preserve me'\n"
    original_reference = b"[bitcoin]\n# previous reference\n"
    (data / "config.toml").write_bytes(original_config)
    (data / "config.toml.template").write_bytes(original_reference)

    result = run_shell(
        f"""
SKIP_VERIFY=true
{source_reader()}
setup_data_directory
""",
        config_env(tmp_path, release),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (data / "config.toml").read_bytes() == original_config
    assert (data / "config.toml.template").read_bytes() == original_reference
    assert not list(data.glob(".config.toml.template.*"))


def test_reference_alias_cannot_overwrite_user_config(tmp_path: Path) -> None:
    release = tmp_path / "release"
    write_release_file(
        release, "jmcore/src/jmcore/data/config.toml.template", "[bitcoin]\n# full\n"
    )
    data = tmp_path / "data"
    data.mkdir()
    config = data / "config.toml"
    original = b"[bitcoin]\nrpc_password = 'preserve me'\n"
    config.write_bytes(original)
    reference = data / "config.toml.template"
    reference.symlink_to(config)

    result = run_shell(
        f"SKIP_VERIFY=true\n{source_reader()}\nsetup_data_directory\n",
        config_env(tmp_path, release),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == original
    assert reference.is_symlink()
    assert "aliases config.toml" in result.stdout


def test_inline_fallback_is_staged_when_no_release_templates_exist(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()

    result = run_shell(
        f"""
SKIP_VERIFY=true
{source_reader()}
setup_data_directory
""",
        config_env(tmp_path, release),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    config = (tmp_path / "data/config.toml").read_text()
    assert "# Uncomment settings to override built-in defaults." in config
    assert "# Full current reference: config.toml.template in this directory." in config
    assert not list((tmp_path / "data").glob(".config.toml.*"))


def write_report_release(release: Path, template: str) -> None:
    write_release_file(release, "jmcore/src/jmcore/data/config.toml.template", template)
    write_release_file(
        release, "scripts/config_changelog.py", CHANGELOG_HELPER.read_text()
    )


def test_report_uses_only_old_and_target_templates_with_section_labels(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    old_template = "[maker]\n# fee = 1\n"
    target_template = "[maker]\n# fee = 2\n"
    write_report_release(release, target_template)
    snapshot = tmp_path / "old-template.toml"
    snapshot.write_text(old_template)
    data = tmp_path / "data"
    data.mkdir()
    secret = "wallet-secret-must-not-appear"
    (data / "config.toml").write_text(f"[bitcoin]\nrpc_password = '{secret}'\n")

    result = run_shell(
        f"""
SKIP_VERIFY=true
AUTO_YES=true
VERSION=2.0.0
CONFIG_TEMPLATE_SNAPSHOT={shlex.quote(str(snapshot))}
CONFIG_TEMPLATE_LABEL="previously installed"
{source_reader()}
report_config_template_changes
""",
        config_env(tmp_path, release),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--- config.toml.template (previously installed)" in result.stdout
    assert "+++ config.toml.template (2.0.0)" in result.stdout
    assert "@@" in result.stdout and "[maker]" in result.stdout
    assert "-# fee = 1" in result.stdout
    assert "+# fee = 2" in result.stdout
    assert secret not in result.stdout + result.stderr


def test_unchanged_template_update_is_quiet(tmp_path: Path) -> None:
    release = tmp_path / "release"
    template = "[maker]\n# fee = 1\n"
    write_report_release(release, template)
    snapshot = tmp_path / "old-template.toml"
    snapshot.write_text(template)

    result = run_shell(
        f"""
SKIP_VERIFY=true
AUTO_YES=true
CONFIG_TEMPLATE_SNAPSHOT={shlex.quote(str(snapshot))}
{source_reader()}
report_config_template_changes
""",
        config_env(tmp_path, release),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def test_missing_or_unparseable_snapshot_is_nonfatal(tmp_path: Path) -> None:
    release = tmp_path / "release"
    write_report_release(release, "[maker]\n# fee = 2\n")
    invalid_snapshot = tmp_path / "invalid-template.toml"
    invalid_snapshot.write_text("[maker\n")

    for snapshot in ("", str(invalid_snapshot)):
        result = run_shell(
            f"""
SKIP_VERIFY=true
AUTO_YES=true
CONFIG_TEMPLATE_SNAPSHOT={shlex.quote(snapshot)}
{source_reader()}
report_config_template_changes
""",
            config_env(tmp_path, release),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Configuration comparison unavailable" in result.stdout
        assert "release notes" in result.stdout


def test_missing_target_diff_helper_is_nonfatal(tmp_path: Path) -> None:
    release = tmp_path / "release"
    write_release_file(
        release, "jmcore/src/jmcore/data/config.toml.template", "[maker]\n# fee = 2\n"
    )
    snapshot = tmp_path / "old-template.toml"
    snapshot.write_text("[maker]\n# fee = 1\n")

    result = run_shell(
        f"""
SKIP_VERIFY=true
AUTO_YES=true
CONFIG_TEMPLATE_SNAPSHOT={shlex.quote(str(snapshot))}
{source_reader()}
report_config_template_changes
""",
        config_env(tmp_path, release),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Configuration comparison unavailable" in result.stdout


def test_update_snapshots_old_package_before_replacing_it(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    package = package_root / "jmcore"
    (package / "data").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    old_template = "[maker]\n# fee = 1\n"
    target_template = "[maker]\n# fee = 2\n"
    (package / "data/config.toml.template").write_text(old_template)

    release = tmp_path / "release"
    write_report_release(release, target_template)
    marker = tmp_path / "snapshot-before-update"
    data = tmp_path / "data"
    data.mkdir()
    secret = "installed-config-secret"
    (data / "config.toml").write_text(f"[bitcoin]\nrpc_password = '{secret}'\n")
    venv = tmp_path / "venv"
    venv.mkdir()

    result = run_shell(
        f"""
SKIP_VERIFY=true
check_system_dependencies() {{ :; }}
prepare_release() {{ VERSION=2.0.0; INSTALL_VERSION=2.0.0; }}
setup_virtualenv() {{ :; }}
update_packages() {{
    command cat "$CONFIG_TEMPLATE_SNAPSHOT" > {shlex.quote(str(marker))}
    printf %s {shlex.quote(target_template)} > {shlex.quote(str(package / "data/config.toml.template"))}
}}
migrate_config() {{ :; }}
create_shell_integration() {{ :; }}
save_trusted_installer() {{ :; }}
{source_reader()}
main --update --skip-verify --skip-tor --yes
""",
        {
            **config_env(tmp_path, release),
            "JMNG_VENV_DIR": str(venv),
            "PYTHONPATH": str(package_root),
        },
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == old_template
    assert "--- config.toml.template (previously installed)" in result.stdout
    assert "+++ config.toml.template (2.0.0)" in result.stdout
    assert secret not in result.stdout + result.stderr
