"""Hermetic regression tests for the installer's trusted-copy refresh path."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
GPG_TIMEOUT = 30


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("gpg") is None,
    reason="bash and gpg are required for trusted installer tests",
)


@dataclass(frozen=True)
class SigningKey:
    """A disposable primary key and its dedicated signing subkey."""

    home: Path
    primary_fingerprint: str
    signing_subkey_fingerprint: str
    public_key: Path


@dataclass(frozen=True)
class ReleaseFixture:
    """Local files served by the mocked curl transport."""

    transport_dir: Path
    asset: Path
    signatures_dir: Path


def _gpg(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "gpg",
            "--no-options",
            "--homedir",
            str(home),
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=GPG_TIMEOUT,
        check=True,
    )


def _generate_signing_key(root: Path, name: str) -> SigningKey:
    home = root / name
    home.mkdir(mode=0o700)
    uid = f"{name} <{name}@example.invalid>"
    _gpg(home, "--quick-generate-key", uid, "ed25519", "cert", "0")

    primary_fingerprint = next(
        line.split(":")[9]
        for line in _gpg(home, "--with-colons", "--list-keys", uid).stdout.splitlines()
        if line.startswith("fpr:")
    )
    _gpg(home, "--quick-add-key", primary_fingerprint, "ed25519", "sign", "0")

    fingerprints = [
        line.split(":")[9]
        for line in _gpg(
            home, "--with-colons", "--list-secret-keys", uid
        ).stdout.splitlines()
        if line.startswith("fpr:")
    ]
    assert len(fingerprints) == 2
    public_key = root / f"{name}.asc"
    _gpg(home, "--armor", "--output", str(public_key), "--export", primary_fingerprint)
    return SigningKey(home, fingerprints[0], fingerprints[1], public_key)


@pytest.fixture(scope="session")
def signing_keys(
    tmp_path_factory: pytest.TempPathFactory,
) -> Generator[tuple[SigningKey, SigningKey, SigningKey], None, None]:
    root = tmp_path_factory.mktemp("trusted-installer-gpg")
    keys = (
        _generate_signing_key(root, "trusted-one"),
        _generate_signing_key(root, "trusted-two"),
        _generate_signing_key(root, "attacker"),
    )
    try:
        yield keys
    finally:
        gpgconf = shutil.which("gpgconf")
        if gpgconf is not None:
            for key in keys:
                subprocess.run(
                    [gpgconf, "--homedir", str(key.home), "--kill", "gpg-agent"],
                    capture_output=True,
                    text=True,
                    timeout=GPG_TIMEOUT,
                    check=False,
                )


def _write_mock_curl(bin_dir: Path) -> None:
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -euo pipefail

            destination=""
            url=""
            while (($#)); do
                case "$1" in
                    -o)
                        destination="$2"
                        shift 2
                        ;;
                    https://*)
                        url="$1"
                        shift
                        ;;
                    *)
                        shift
                        ;;
                esac
            done

            case "$url" in
                */releases/download/*/install.sh)
                    source_path="$JMNG_TEST_TRANSPORT/asset/install.sh"
                    ;;
                */signatures/pubkeys/*.asc)
                    source_path="$JMNG_TEST_TRANSPORT/pubkeys/${url##*/}"
                    ;;
                */signatures/*/*.install.sh.sig)
                    source_path="$JMNG_TEST_TRANSPORT/signatures/${url##*/}"
                    ;;
                *)
                    exit 22
                    ;;
            esac

            [[ -n "$destination" && -f "$source_path" ]] || exit 22
            cp "$source_path" "$destination"
            """
        )
    )
    curl.chmod(curl.stat().st_mode | stat.S_IXUSR)


def _create_release_fixture(
    tmp_path: Path,
    trusted_keys: tuple[SigningKey, SigningKey],
    candidate: str,
) -> ReleaseFixture:
    transport_dir = tmp_path / "transport"
    asset = transport_dir / "asset" / "install.sh"
    signatures_dir = transport_dir / "signatures"
    pubkeys_dir = transport_dir / "pubkeys"
    asset.parent.mkdir(parents=True)
    signatures_dir.mkdir(parents=True)
    pubkeys_dir.mkdir(parents=True)
    asset.write_text(candidate)
    for key in trusted_keys:
        shutil.copy2(key.public_key, pubkeys_dir / f"{key.primary_fingerprint}.asc")
    return ReleaseFixture(transport_dir, asset, signatures_dir)


def _sign_installer(key: SigningKey, installer: Path, signature: Path) -> None:
    _gpg(
        key.home,
        "--local-user",
        f"{key.signing_subkey_fingerprint}!",
        "--detach-sign",
        "--output",
        str(signature),
        str(installer),
    )


def _candidate_script(version: str = "1.2.3", protocol: int = 1) -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env bash
        DEFAULT_VERSION="{version}"
        INSTALLER_PROTOCOL={protocol}
        {{
            printf 'executed=true\\n'
            printf 'verified=%s\\n' "${{JMNG_VERIFIED_INSTALLER:-}}"
            printf 'data=%s\\n' "$JOINMARKET_DATA_DIR"
            printf 'venv=%s\\n' "$JMNG_VENV_DIR"
            for argument in "$@"; do
                printf 'arg=%s\\n' "$argument"
            done
        }} > "$JMNG_TEST_MARKER"
        """
    )


def _run_refresh(
    tmp_path: Path,
    release: ReleaseFixture,
    trusted_keys: tuple[SigningKey, SigningKey],
    *,
    version: str = "v1.2.3",
    parent_version: str = "1.0.0",
    mode: str = "update",
    auto_yes: bool = True,
    install_version: str = "",
    args: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    marker = tmp_path / "candidate-marker"
    data_dir = tmp_path / "data"
    venv_dir = tmp_path / "venv"
    data_dir.mkdir(exist_ok=True)
    saved_installer = data_dir / "install.sh"
    if not saved_installer.exists():
        saved_installer.write_text(f'DEFAULT_VERSION="{parent_version}"\n')
    _write_mock_curl(bin_dir)

    script = f"""\
source {shlex.quote(str(INSTALL_SH))}
set +e
TRUSTED_GPG_FINGERPRINTS=("$TEST_TRUSTED_ONE" "$TEST_TRUSTED_TWO")
REQUIRED_GPG_SIGNATURES=2
GITHUB_REPO="fixture/repo"
DEFAULT_VERSION="$TEST_PARENT_VERSION"
MODE="$TEST_MODE"
AUTO_YES="$TEST_AUTO_YES"
INSTALL_VERSION="$TEST_INSTALL_VERSION"
get_latest_version() {{ printf '%s\\n' "$TEST_VERSION"; }}
refresh_installer "$@"
status=$?
printf 'EXIT:%s\\n' "$status"
"""
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "JMNG_TEST_TRANSPORT": str(release.transport_dir),
        "JMNG_TEST_MARKER": str(marker),
        "JOINMARKET_DATA_DIR": str(data_dir),
        "JMNG_VENV_DIR": str(venv_dir),
        "TEST_TRUSTED_ONE": trusted_keys[0].primary_fingerprint,
        "TEST_TRUSTED_TWO": trusted_keys[1].primary_fingerprint,
        "TEST_VERSION": version,
        "TEST_PARENT_VERSION": parent_version,
        "TEST_MODE": mode,
        "TEST_AUTO_YES": "true" if auto_yes else "false",
        "TEST_INSTALL_VERSION": install_version,
    }
    result = subprocess.run(
        ["bash", "-c", script, "refresh-installer", *args],
        capture_output=True,
        text=True,
        timeout=GPG_TIMEOUT,
        check=False,
        env=env,
    )
    return result, marker


def _marker_values(marker: Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in marker.read_text().splitlines():
        key, value = line.split("=", maxsplit=1)
        values.setdefault(key, []).append(value)
    return values


def _sign_with_trusted_quorum(
    release: ReleaseFixture, trusted_keys: tuple[SigningKey, SigningKey]
) -> None:
    for key in trusted_keys:
        _sign_installer(
            key,
            release.asset,
            release.signatures_dir / f"{key.primary_fingerprint}.install.sh.sig",
        )


def test_refresh_accepts_distinct_trusted_signing_subkeys(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    release = _create_release_fixture(tmp_path, trusted_keys, _candidate_script())
    _sign_with_trusted_quorum(release, trusted_keys)

    result, marker = _run_refresh(
        tmp_path,
        release,
        trusted_keys,
        args=("--maker", "--skip-tor"),
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert (
        trusted_keys[0].signing_subkey_fingerprint
        != trusted_keys[0].primary_fingerprint
    )
    assert (
        f"Installer signature from {trusted_keys[0].primary_fingerprint}"
        in result.stdout
    )
    assert (
        f"Installer signature from {trusted_keys[1].primary_fingerprint}"
        in result.stdout
    )
    values = _marker_values(marker)
    assert values["executed"] == ["true"]
    assert values["data"] == [str(tmp_path / "data")]
    assert values["venv"] == [str(tmp_path / "venv")]
    assert values["arg"] == [
        "--maker",
        "--skip-tor",
        "--update",
        "--yes",
        "--version",
        "v1.2.3",
    ]
    assert not Path(values["verified"][0]).exists()


def test_tampered_authenticated_installer_never_executes(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    release = _create_release_fixture(tmp_path, trusted_keys, _candidate_script())
    _sign_with_trusted_quorum(release, trusted_keys)
    release.asset.write_text(
        _candidate_script() + "printf 'tampered\\n' >> \"$JMNG_TEST_MARKER\"\n"
    )

    result, marker = _run_refresh(tmp_path, release, trusted_keys)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "found 0" in result.stdout
    assert not marker.exists()


def test_attacker_signature_under_trusted_filenames_is_rejected(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    attacker = signing_keys[2]
    release = _create_release_fixture(tmp_path, trusted_keys, _candidate_script())
    attacker_signature = release.signatures_dir / "attacker.install.sh.sig"
    _sign_installer(attacker, release.asset, attacker_signature)
    for key in trusted_keys:
        shutil.copy2(
            attacker_signature,
            release.signatures_dir / f"{key.primary_fingerprint}.install.sh.sig",
        )

    result, marker = _run_refresh(tmp_path, release, trusted_keys)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "found 0" in result.stdout
    assert not marker.exists()


def test_duplicate_signature_from_one_trusted_signer_does_not_meet_quorum(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    release = _create_release_fixture(tmp_path, trusted_keys, _candidate_script())
    first_signature = (
        release.signatures_dir / f"{trusted_keys[0].primary_fingerprint}.install.sh.sig"
    )
    _sign_installer(trusted_keys[0], release.asset, first_signature)
    shutil.copy2(
        first_signature,
        release.signatures_dir
        / f"{trusted_keys[1].primary_fingerprint}.install.sh.sig",
    )

    result, marker = _run_refresh(tmp_path, release, trusted_keys)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "found 1" in result.stdout
    assert not marker.exists()


@pytest.mark.parametrize("missing_asset", [True, False], ids=["download", "signature"])
def test_missing_release_material_fails_closed_and_preserves_old_copy(
    tmp_path: Path,
    signing_keys: tuple[SigningKey, SigningKey, SigningKey],
    missing_asset: bool,
) -> None:
    trusted_keys = signing_keys[:2]
    release = _create_release_fixture(tmp_path, trusted_keys, _candidate_script())
    _sign_with_trusted_quorum(release, trusted_keys)
    old_copy = tmp_path / "data" / "install.sh"
    old_copy.parent.mkdir()
    old_copy.write_text("known trusted copy\n")
    if missing_asset:
        release.asset.unlink()
    else:
        (
            release.signatures_dir
            / f"{trusted_keys[1].primary_fingerprint}.install.sh.sig"
        ).unlink()

    result, marker = _run_refresh(tmp_path, release, trusted_keys)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert old_copy.read_text() == "known trusted copy\n"
    assert not marker.exists()
    if missing_asset:
        assert "Installer asset unavailable" in result.stdout
    else:
        assert "found 1" in result.stdout


@pytest.mark.parametrize(
    ("candidate_version", "protocol"),
    [("1.2.4", 1), ("1.2.3", 2)],
    ids=["wrong-version", "wrong-protocol"],
)
def test_signed_candidate_must_match_selected_release_and_protocol(
    tmp_path: Path,
    signing_keys: tuple[SigningKey, SigningKey, SigningKey],
    candidate_version: str,
    protocol: int,
) -> None:
    trusted_keys = signing_keys[:2]
    release = _create_release_fixture(
        tmp_path,
        trusted_keys,
        _candidate_script(version=candidate_version, protocol=protocol),
    )
    _sign_with_trusted_quorum(release, trusted_keys)

    result, marker = _run_refresh(tmp_path, release, trusted_keys)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert (
        "does not match release v1.2.3 or lacks trusted-copy support" in result.stdout
    )
    assert not marker.exists()


def test_signed_older_installer_is_rejected(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    release = _create_release_fixture(
        tmp_path, trusted_keys, _candidate_script(version="0.9.9")
    )
    _sign_with_trusted_quorum(release, trusted_keys)

    result, marker = _run_refresh(
        tmp_path,
        release,
        trusted_keys,
        version="v0.9.9",
        parent_version="1.0.0",
    )

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert (
        "Refusing to replace installer 1.0.0 with older installer v0.9.9"
        in result.stdout
    )
    assert not marker.exists()


def test_revoked_signer_is_rejected(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    release = _create_release_fixture(tmp_path, trusted_keys, _candidate_script())
    _sign_with_trusted_quorum(release, trusted_keys)
    key = trusted_keys[0]
    revoked_home = tmp_path / "revoked-home"
    revoked_home.mkdir(mode=0o700)
    _gpg(revoked_home, "--import", str(key.public_key))
    certificate = key.home / "openpgp-revocs.d" / f"{key.primary_fingerprint}.rev"
    revocation = tmp_path / "revocation.asc"
    revocation.write_text(certificate.read_text().replace(":-----BEGIN", "-----BEGIN"))
    _gpg(revoked_home, "--import", str(revocation))
    public_key = release.transport_dir / "pubkeys" / f"{key.primary_fingerprint}.asc"
    public_key.unlink()
    _gpg(
        revoked_home,
        "--armor",
        "--output",
        str(public_key),
        "--export",
        key.primary_fingerprint,
    )

    result, marker = _run_refresh(tmp_path, release, trusted_keys)

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "found 1" in result.stdout
    assert not marker.exists()


def test_embedded_trust_anchors_are_distinct_and_match_release_signers() -> None:
    source = INSTALL_SH.read_text()
    block = source.split("TRUSTED_GPG_FINGERPRINTS=(", 1)[1].split(")", 1)[0]
    fingerprints = re.findall(r'"([A-F0-9]{40})"', block)
    documented = {
        line.split()[0]
        for line in (REPO_ROOT / "signatures/trusted-keys.txt").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert len(fingerprints) == len(set(fingerprints))
    assert len(fingerprints) >= 2
    assert set(fingerprints) == documented


def _instrumented_live_candidate(version: str = "1.2.3") -> str:
    source = re.sub(
        r'^DEFAULT_VERSION="[^"]+"',
        f'DEFAULT_VERSION="{version}"',
        INSTALL_SH.read_text(),
        count=1,
        flags=re.MULTILINE,
    )
    dispatch = '(return 0 2>/dev/null) || main "$@"'
    instrumentation = textwrap.dedent(
        """\
        check_system_dependencies() { :; }
        prepare_release() { :; }
        setup_virtualenv() { :; }
        update_packages() { :; }
        migrate_config() { :; }
        create_shell_integration() { :; }
        setup_tor() { :; }
        cleanup_install() { :; }
        refresh_installer() {
            printf 'recursive-refresh=true\\n' >> "$JMNG_TEST_MARKER"
            return 1
        }
        save_trusted_installer() {
            {
                printf 'authenticated=%s\\n' "$INSTALLER_AUTHENTICATED"
                printf 'verified=%s\\n' "${JMNG_VERIFIED_INSTALLER:-}"
                printf 'data=%s\\n' "$DATA_DIR"
                printf 'venv=%s\\n' "$VENV_DIR"
                printf 'mode=%s\\n' "$MODE"
                printf 'auto_yes=%s\\n' "$AUTO_YES"
                printf 'version=%s\\n' "$INSTALL_VERSION"
                printf 'maker=%s\\n' "$INSTALL_MAKER"
                printf 'skip_tor=%s\\n' "$SKIP_TOR"
            } > "$JMNG_TEST_MARKER"
        }
        """
    )
    assert source.count(dispatch) == 1
    return source.replace(dispatch, f"{instrumentation}\n{dispatch}")


def _instrumented_lifecycle_candidate(
    trusted_keys: tuple[SigningKey, SigningKey],
    version: str,
    *,
    fail_packages: bool = False,
) -> str:
    """Return a live installer with only external installation work neutralized."""

    source, version_replacements = re.subn(
        r'^DEFAULT_VERSION="[^"]+"',
        f'DEFAULT_VERSION="{version}"',
        INSTALL_SH.read_text(),
        count=1,
        flags=re.MULTILINE,
    )
    trusted_fingerprints = (
        "TRUSTED_GPG_FINGERPRINTS=(\n"
        + "".join(f'    "{key.primary_fingerprint}"\n' for key in trusted_keys)
        + ")"
    )
    source, fingerprint_replacements = re.subn(
        r'TRUSTED_GPG_FINGERPRINTS=\(\n(?:    "[0-9A-F]+"\n)+\)',
        trusted_fingerprints,
        source,
        count=1,
    )
    assert version_replacements == 1
    assert fingerprint_replacements == 1

    package_handler = (
        "install_packages() { return 1; }"
        if fail_packages
        else "install_packages() { :; }"
    )
    instrumentation = textwrap.dedent(
        f"""\
        get_latest_version() {{ printf '%s\\n' "$JMNG_TEST_LATEST_VERSION"; }}
        check_system_dependencies() {{ :; }}
        check_python_version() {{ :; }}
        ask_components() {{ :; }}
        prepare_release() {{
            VERSION="$INSTALL_VERSION"
            printf 'lifecycle-mode=%s\\n' "$MODE"
        }}
        setup_virtualenv() {{ mkdir -p "$VENV_DIR"; }}
        {package_handler}
        update_packages() {{ :; }}
        migrate_config() {{ :; }}
        setup_tor() {{ :; }}
        setup_data_directory() {{ mkdir -p "$DATA_DIR"; }}
        create_shell_integration() {{ :; }}
        print_completion() {{ :; }}
        """
    )
    dispatch = '(return 0 2>/dev/null) || main "$@"'
    assert source.count(dispatch) == 1
    return source.replace(dispatch, f"{instrumentation}\n{dispatch}")


def _write_persistence_failure_command(bin_dir: Path, command: str) -> None:
    real_command = shutil.which(command)
    assert real_command is not None
    destination = '"$JOINMARKET_DATA_DIR"/.install.sh.*'
    if command == "mv":
        destination = '"$JOINMARKET_DATA_DIR/install.sh"'
    wrapper = bin_dir / command
    wrapper.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [[ "${{!#}}" == {destination} ]]; then
                exit 1
            fi
            exec {shlex.quote(real_command)} "$@"
            """
        )
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)


def _run_lifecycle_installer(
    tmp_path: Path,
    installer: Path,
    release: ReleaseFixture,
    *,
    data_dir: Path,
    venv_dir: Path,
    latest_version: str,
    args: tuple[str, ...],
    persistence_failure: str | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "lifecycle-bin"
    if not bin_dir.exists():
        _write_mock_curl(bin_dir)
    if persistence_failure is not None:
        _write_persistence_failure_command(bin_dir, persistence_failure)

    return subprocess.run(
        ["bash", str(installer), *args],
        capture_output=True,
        text=True,
        timeout=GPG_TIMEOUT,
        check=False,
        stdin=subprocess.DEVNULL,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "JMNG_TEST_TRANSPORT": str(release.transport_dir),
            "JOINMARKET_DATA_DIR": str(data_dir),
            "JMNG_VENV_DIR": str(venv_dir),
            "JMNG_TEST_LATEST_VERSION": latest_version,
        },
    )


def test_authenticated_child_does_not_refresh_again_and_preserves_context(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    release = _create_release_fixture(
        tmp_path, trusted_keys, _instrumented_live_candidate()
    )
    _sign_with_trusted_quorum(release, trusted_keys)

    result, marker = _run_refresh(
        tmp_path,
        release,
        trusted_keys,
        args=("--maker", "--skip-tor"),
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    values = _marker_values(marker)
    assert "recursive-refresh" not in values
    assert values == {
        "authenticated": ["true"],
        "verified": [""],
        "data": [str(tmp_path / "data")],
        "venv": [str(tmp_path / "venv")],
        "mode": ["update"],
        "auto_yes": ["true"],
        "version": ["v1.2.3"],
        "maker": ["true"],
        "skip_tor": ["true"],
    }


def test_newer_bootstrap_hands_off_to_signed_stable_installer(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    data_dir = tmp_path / "data"
    venv_dir = tmp_path / "venv"
    bootstrap = tmp_path / "bootstrap-install.sh"
    bootstrap.write_text(_instrumented_lifecycle_candidate(trusted_keys, "1.2.4"))
    stable_candidate = _instrumented_lifecycle_candidate(trusted_keys, "1.2.3")
    release = _create_release_fixture(tmp_path, trusted_keys, stable_candidate)
    _sign_with_trusted_quorum(release, trusted_keys)

    result = _run_lifecycle_installer(
        tmp_path,
        bootstrap,
        release,
        data_dir=data_dir,
        venv_dir=venv_dir,
        latest_version="v1.2.3",
        args=("--yes", "--maker", "--skip-tor"),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Refusing to replace installer" not in result.stdout
    assert stable_candidate == (data_dir / "install.sh").read_text()


def test_bootstrap_respects_existing_saved_installer_rollback_baseline(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    saved_copy = data_dir / "install.sh"
    saved_source = _instrumented_lifecycle_candidate(trusted_keys, "1.2.4")
    saved_copy.write_text(saved_source)
    bootstrap = tmp_path / "bootstrap-install.sh"
    bootstrap.write_text(_instrumented_lifecycle_candidate(trusted_keys, "1.2.5"))
    release = _create_release_fixture(
        tmp_path,
        trusted_keys,
        _instrumented_lifecycle_candidate(trusted_keys, "1.2.3"),
    )
    _sign_with_trusted_quorum(release, trusted_keys)

    result = _run_lifecycle_installer(
        tmp_path,
        bootstrap,
        release,
        data_dir=data_dir,
        venv_dir=tmp_path / "venv",
        latest_version="v1.2.3",
        args=("--yes", "--update", "--maker", "--skip-tor"),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "An authenticated local updater already exists" in result.stdout
    assert f'bash "{saved_copy}" --update' in result.stdout
    assert (
        "Refusing to replace installer 1.2.4 with older installer v1.2.3"
        in result.stdout
    )
    assert saved_copy.read_text() == saved_source


def test_bootstrap_fails_closed_for_invalid_saved_installer_version(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    saved_copy = data_dir / "install.sh"
    saved_copy.write_text("saved installer without version metadata\n")
    bootstrap = tmp_path / "bootstrap-install.sh"
    bootstrap.write_text(_instrumented_lifecycle_candidate(trusted_keys, "1.2.4"))
    release = _create_release_fixture(
        tmp_path,
        trusted_keys,
        _instrumented_lifecycle_candidate(trusted_keys, "1.2.3"),
    )
    _sign_with_trusted_quorum(release, trusted_keys)

    result = _run_lifecycle_installer(
        tmp_path,
        bootstrap,
        release,
        data_dir=data_dir,
        venv_dir=tmp_path / "venv",
        latest_version="v1.2.3",
        args=("--yes", "--update", "--maker", "--skip-tor"),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Saved trusted installer has invalid version metadata" in result.stdout
    assert saved_copy.read_text() == "saved installer without version metadata\n"


def test_saved_trusted_installer_still_rejects_signed_downgrade(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    saved_copy = data_dir / "install.sh"
    saved_source = _instrumented_lifecycle_candidate(trusted_keys, "1.2.4")
    saved_copy.write_text(saved_source)
    release = _create_release_fixture(
        tmp_path,
        trusted_keys,
        _instrumented_lifecycle_candidate(trusted_keys, "1.2.3"),
    )
    _sign_with_trusted_quorum(release, trusted_keys)

    result = _run_lifecycle_installer(
        tmp_path,
        saved_copy,
        release,
        data_dir=data_dir,
        venv_dir=tmp_path / "venv",
        latest_version="v1.2.3",
        args=("--yes", "--update", "--maker", "--skip-tor"),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert (
        "Refusing to replace installer 1.2.4 with older installer v1.2.3"
        in result.stdout
    )
    assert saved_copy.read_text() == saved_source


@pytest.mark.parametrize("legacy_venv", [False, True], ids=["fresh", "legacy-update"])
def test_trusted_copy_lifecycle_persists_and_refreshes_again(
    tmp_path: Path,
    signing_keys: tuple[SigningKey, SigningKey, SigningKey],
    legacy_venv: bool,
) -> None:
    trusted_keys = signing_keys[:2]
    data_dir = tmp_path / "custom-data"
    venv_dir = tmp_path / "custom-venv"
    data_dir.mkdir()
    config = data_dir / "config.toml"
    config.write_text("config-sentinel = 'preserve me'\n")
    legacy_sentinel = venv_dir / "legacy-venv-sentinel"
    if legacy_venv:
        venv_dir.mkdir()
        legacy_sentinel.write_text("preserve legacy venv\n")

    bootstrap = tmp_path / "bootstrap-install.sh"
    bootstrap.write_text(_instrumented_lifecycle_candidate(trusted_keys, "1.2.3"))
    first_candidate = _instrumented_lifecycle_candidate(trusted_keys, "1.2.3")
    release = _create_release_fixture(tmp_path, trusted_keys, first_candidate)
    _sign_with_trusted_quorum(release, trusted_keys)

    first = _run_lifecycle_installer(
        tmp_path,
        bootstrap,
        release,
        data_dir=data_dir,
        venv_dir=venv_dir,
        latest_version="v1.2.3",
        args=("--yes", "--maker", "--skip-tor"),
    )

    saved_copy = data_dir / "install.sh"
    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stdout.count("Authenticating the current installer") == 1
    assert first.stdout.count("Trusted installer saved") == 1
    assert "An authenticated local updater already exists" not in first.stdout
    assert first_candidate == saved_copy.read_text()
    assert config.read_text() == "config-sentinel = 'preserve me'\n"
    assert venv_dir.is_dir()
    assert f'"{trusted_keys[0].primary_fingerprint}"' in saved_copy.read_text()
    assert f'"{trusted_keys[1].primary_fingerprint}"' in saved_copy.read_text()
    expected_first_mode = "update" if legacy_venv else "install"
    assert f"lifecycle-mode={expected_first_mode}" in first.stdout
    if legacy_venv:
        assert legacy_sentinel.read_text() == "preserve legacy venv\n"

    second_candidate = _instrumented_lifecycle_candidate(trusted_keys, "1.2.4")
    release.asset.write_text(second_candidate)
    for key in trusted_keys:
        signature = release.signatures_dir / f"{key.primary_fingerprint}.install.sh.sig"
        signature.unlink()
        _sign_installer(key, release.asset, signature)

    second = _run_lifecycle_installer(
        tmp_path,
        saved_copy,
        release,
        data_dir=data_dir,
        venv_dir=venv_dir,
        latest_version="v1.2.4",
        args=("--yes", "--update", "--maker", "--skip-tor"),
    )

    assert second.returncode == 0, second.stdout + second.stderr
    assert second.stdout.count("Authenticating the current installer") == 1
    assert second.stdout.count("Trusted installer saved") == 1
    assert "An authenticated local updater already exists" not in second.stdout
    assert second_candidate == saved_copy.read_text()
    assert config.read_text() == "config-sentinel = 'preserve me'\n"
    assert "lifecycle-mode=update" in second.stdout
    if legacy_venv:
        assert legacy_sentinel.read_text() == "preserve legacy venv\n"


def test_authenticated_candidate_failure_preserves_existing_saved_copy(
    tmp_path: Path, signing_keys: tuple[SigningKey, SigningKey, SigningKey]
) -> None:
    trusted_keys = signing_keys[:2]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old_copy = data_dir / "install.sh"
    old_source = _instrumented_lifecycle_candidate(trusted_keys, "1.2.3")
    old_copy.write_text(old_source)
    bootstrap = tmp_path / "bootstrap-install.sh"
    bootstrap.write_text(old_source)
    failed_candidate = _instrumented_lifecycle_candidate(
        trusted_keys, "1.2.3", fail_packages=True
    )
    release = _create_release_fixture(tmp_path, trusted_keys, failed_candidate)
    _sign_with_trusted_quorum(release, trusted_keys)

    result = _run_lifecycle_installer(
        tmp_path,
        bootstrap,
        release,
        data_dir=data_dir,
        venv_dir=tmp_path / "venv",
        latest_version="v1.2.3",
        args=("--yes", "--maker", "--skip-tor"),
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert result.stdout.count("Installer signature from") == 2
    assert "Trusted installer saved" not in result.stdout
    assert old_copy.read_text() == old_source
    assert list(data_dir.glob(".install.sh.*")) == []


@pytest.mark.parametrize("command", ["cp", "mv"], ids=["copy", "rename"])
def test_authenticated_persistence_failure_preserves_existing_saved_copy(
    tmp_path: Path,
    signing_keys: tuple[SigningKey, SigningKey, SigningKey],
    command: str,
) -> None:
    trusted_keys = signing_keys[:2]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old_copy = data_dir / "install.sh"
    old_source = _instrumented_lifecycle_candidate(trusted_keys, "1.2.3")
    old_copy.write_text(old_source)
    bootstrap = tmp_path / "bootstrap-install.sh"
    bootstrap.write_text(old_source)
    candidate = _instrumented_lifecycle_candidate(trusted_keys, "1.2.3")
    release = _create_release_fixture(tmp_path, trusted_keys, candidate)
    _sign_with_trusted_quorum(release, trusted_keys)

    result = _run_lifecycle_installer(
        tmp_path,
        bootstrap,
        release,
        data_dir=data_dir,
        venv_dir=tmp_path / "venv",
        latest_version="v1.2.3",
        args=("--yes", "--maker", "--skip-tor"),
        persistence_failure=command,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert result.stdout.count("Installer signature from") == 2
    assert "Could not save the trusted installer" in result.stdout
    assert old_copy.read_text() == old_source
    assert list(data_dir.glob(".install.sh.*")) == []


def _run_save_trusted_installer(
    source: Path, data_dir: Path, *, skip_verify: bool, authenticated: bool
) -> subprocess.CompletedProcess[str]:
    script = f"""\
source {shlex.quote(str(INSTALL_SH))}
set +e
DATA_DIR={shlex.quote(str(data_dir))}
INSTALLER_SOURCE={shlex.quote(str(source))}
SKIP_VERIFY={"true" if skip_verify else "false"}
INSTALLER_AUTHENTICATED={"true" if authenticated else "false"}
save_trusted_installer
status=$?
printf 'EXIT:%s\\n' "$status"
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=GPG_TIMEOUT,
        check=False,
    )


def test_save_trusted_installer_replaces_copy_atomically(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old_copy = data_dir / "install.sh"
    old_copy.write_text("old trusted installer\n")
    old_inode = old_copy.stat().st_ino
    source = tmp_path / "authenticated-install.sh"
    source.write_text("new trusted installer\n")

    result = _run_save_trusted_installer(
        source, data_dir, skip_verify=False, authenticated=True
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert old_copy.read_text() == "new trusted installer\n"
    assert old_copy.stat().st_ino != old_inode
    assert stat.S_IMODE(old_copy.stat().st_mode) == 0o700
    assert list(data_dir.glob(".install.sh.*")) == []


@pytest.mark.parametrize(
    ("skip_verify", "authenticated"),
    [(True, True), (False, False)],
    ids=["verification-optout", "unauthenticated"],
)
def test_unverified_run_never_overwrites_saved_installer(
    tmp_path: Path, skip_verify: bool, authenticated: bool
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old_copy = data_dir / "install.sh"
    old_copy.write_text("known trusted installer\n")
    source = tmp_path / "untrusted-install.sh"
    source.write_text("replacement must not persist\n")

    result = _run_save_trusted_installer(
        source,
        data_dir,
        skip_verify=skip_verify,
        authenticated=authenticated,
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "Trusted installer was not changed by this unverified run" in result.stdout
    assert old_copy.read_text() == "known trusted installer\n"
