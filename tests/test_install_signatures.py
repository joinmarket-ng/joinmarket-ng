"""Hermetic regression tests for installer release signature verification."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"
FIRST_FINGERPRINT = "1C53A412D11EF3051704419C44912E1E03005B31"
SECOND_FINGERPRINT = "9253062A4F92D63459085CA62D230520212A5901"
UNTRUSTED_FINGERPRINT = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
COMMIT = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _run_verification(
    *,
    signatures: tuple[str, ...] = (FIRST_FINGERPRINT,),
    shared_available: bool = True,
    local_manifests: tuple[str, ...] = (),
    unavailable_signatures: tuple[str, ...] = (),
    bad_signatures: tuple[str, ...] = (),
    signer_mismatch: bool = False,
    shared_commit: str = COMMIT,
    local_commit: str = COMMIT,
) -> subprocess.CompletedProcess[str]:
    """Run ``verify_release_signature`` with curl and GPG fully stubbed."""

    script = f'''\
source "{INSTALL_SH}"
set +e

FIRST_FINGERPRINT="{FIRST_FINGERPRINT}"
SECOND_FINGERPRINT="{SECOND_FINGERPRINT}"
SIGNATURES="{" ".join(signatures)}"
SHARED_AVAILABLE="{"true" if shared_available else "false"}"
LOCAL_MANIFESTS="{" ".join(local_manifests)}"
UNAVAILABLE_SIGNATURES="{" ".join(unavailable_signatures)}"
BAD_SIGNATURES="{" ".join(bad_signatures)}"
SIGNER_MISMATCH="{"true" if signer_mismatch else "false"}"
SHARED_COMMIT="{shared_commit}"
LOCAL_COMMIT="{local_commit}"
CURL_LOG="$(mktemp)"

print_header() {{ :; }}
print_info() {{ echo "INFO: $1"; }}
print_success() {{ echo "OK: $1"; }}
print_warning() {{ echo "WARN: $1"; }}
print_error() {{ echo "ERR: $1"; }}

contains_word() {{
    local needle="$1"
    local words="$2"
    local word
    for word in $words; do
        [[ "$word" == "$needle" ]] && return 0
    done
    return 1
}}

curl() {{
    local destination=""
    local index
    for ((index = 1; index <= $#; index++)); do
        if [[ "${{!index}}" == "-o" ]]; then
            local next=$((index + 1))
            destination="${{!next}}"
        fi
    done
    local url=""
    local arg
    for arg in "$@"; do
        [[ "$arg" == http* ]] && url="$arg"
    done
    printf '%s\n' "$url" >> "$CURL_LOG"
    case "$url" in
        *trusted-keys.txt)
            printf '%s trusted-one\n%s trusted-two\n' "$FIRST_FINGERPRINT" "$SECOND_FINGERPRINT" > "$destination"
            ;;
        *signatures/pubkeys/*.asc)
            : > "$destination"
            ;;
        *contents/signatures/*)
            : > "$destination"
            local fingerprint
            for fingerprint in $SIGNATURES; do
                printf '"name": "%s.sig"\n' "$fingerprint" >> "$destination"
            done
            ;;
        *release-manifest-*.txt)
            [[ "$SHARED_AVAILABLE" == "true" ]] || return 22
            printf 'commit: %s\n' "$SHARED_COMMIT" > "$destination"
            ;;
        *signatures/*/*.sig)
            local signature="${{url##*/}}"
            signature="${{signature%.sig}}"
            contains_word "$signature" "$UNAVAILABLE_SIGNATURES" && return 22
            : > "$destination"
            ;;
        *-manifest.txt)
            local manifest="${{url##*/}}"
            manifest="${{manifest%-manifest.txt}}"
            contains_word "$manifest" "$LOCAL_MANIFESTS" || return 22
            printf 'commit: %s\n' "$LOCAL_COMMIT" > "$destination"
            ;;
        *) return 22 ;;
    esac
}}

gpg() {{
    case " $* " in
        *" --import "*) return 0 ;;
    esac

    local signature=""
    local manifest=""
    local arg
    for arg in "$@"; do
        case "$arg" in
            *.sig) signature="$arg" ;;
            *manifest*.txt) manifest="$arg" ;;
        esac
    done
    local fingerprint="${{signature##*/}}"
    fingerprint="${{fingerprint%.sig}}"
    local kind="local"
    [[ "$manifest" == *release-manifest-* ]] && kind="shared"
    contains_word "$fingerprint:$kind" "$BAD_SIGNATURES" && return 1

    local signer="$fingerprint"
    if [[ "$SIGNER_MISMATCH" == "true" ]]; then
        signer="$SECOND_FINGERPRINT"
        [[ "$fingerprint" == "$SECOND_FINGERPRINT" ]] && signer="$FIRST_FINGERPRINT"
    fi
    printf '[GNUPG:] VALIDSIG %s 0 0 0 0 0 0 0 0\n' "$signer"
}}

SKIP_VERIFY=false
verify_release_signature "v9.9.9" "{COMMIT}"
echo "EXIT:$?"
echo "CURL_LOG_START"
cat "$CURL_LOG"
rm -f "$CURL_LOG"
'''
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_ci_first_signature_uses_shared_manifest_without_local_fetch() -> None:
    result = _run_verification()

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "release-manifest-v9.9.9.txt" in result.stdout
    assert f"{FIRST_FINGERPRINT}-manifest.txt" not in result.stdout


def test_local_first_signature_falls_back_when_shared_asset_is_unavailable() -> None:
    result = _run_verification(
        shared_available=False, local_manifests=(FIRST_FINGERPRINT,)
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert "Shared release manifest unavailable" in result.stdout
    assert f"{FIRST_FINGERPRINT}-manifest.txt" in result.stdout


def test_one_valid_signature_succeeds_when_another_signature_is_unavailable() -> None:
    result = _run_verification(
        signatures=(FIRST_FINGERPRINT, SECOND_FINGERPRINT),
        unavailable_signatures=(SECOND_FINGERPRINT,),
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert f"Failed to fetch signature {SECOND_FINGERPRINT}.sig" in result.stdout
    assert f"{SECOND_FINGERPRINT}-manifest.txt" not in result.stdout


def test_shared_manifest_outage_uses_a_valid_local_manifest() -> None:
    result = _run_verification(
        shared_available=False, local_manifests=(FIRST_FINGERPRINT,)
    )

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr


def test_bad_signatures_are_rejected() -> None:
    result = _run_verification(
        local_manifests=(FIRST_FINGERPRINT,),
        bad_signatures=(f"{FIRST_FINGERPRINT}:shared", f"{FIRST_FINGERPRINT}:local"),
    )

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "could not be verified" in result.stdout


def test_signature_signer_must_match_trusted_filename() -> None:
    result = _run_verification(
        signer_mismatch=True, local_manifests=(FIRST_FINGERPRINT,)
    )

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "was made by" in result.stdout


@pytest.mark.parametrize("shared_available", [True, False])
def test_manifest_commit_must_match_install_commit(shared_available: bool) -> None:
    result = _run_verification(
        shared_available=shared_available,
        local_manifests=(FIRST_FINGERPRINT,),
        shared_commit="different-commit",
        local_commit="different-commit",
    )

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "!= install commit" in result.stdout


def test_untrusted_signature_file_is_ignored() -> None:
    result = _run_verification(signatures=(FIRST_FINGERPRINT, UNTRUSTED_FINGERPRINT))

    assert "EXIT:0" in result.stdout, result.stdout + result.stderr
    assert (
        f"Ignoring signature from untrusted key {UNTRUSTED_FINGERPRINT}"
        in result.stdout
    )


def test_zero_valid_signatures_fail_verification() -> None:
    result = _run_verification(
        signatures=(FIRST_FINGERPRINT, SECOND_FINGERPRINT),
        unavailable_signatures=(FIRST_FINGERPRINT, SECOND_FINGERPRINT),
    )

    assert "EXIT:1" in result.stdout, result.stdout + result.stderr
    assert "No trusted signature attested" in result.stdout
