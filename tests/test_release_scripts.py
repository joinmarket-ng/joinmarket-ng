from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
TEST_FINGERPRINT = "111122223333444455556666777788889999AAAA"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def make_env(bin_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env


def run_git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


def test_sign_release_rejects_local_manifest_from_different_commit(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    scripts_dir = repo_dir / "scripts"
    signatures_dir = repo_dir / "signatures"
    bin_dir = tmp_path / "bin"

    scripts_dir.mkdir(parents=True)
    signatures_dir.mkdir(parents=True)
    bin_dir.mkdir()

    (signatures_dir / "trusted-keys.txt").write_text(f"{TEST_FINGERPRINT} Test User\n")
    (scripts_dir / "sign-release.sh").write_text(
        (SCRIPTS_DIR / "sign-release.sh").read_text()
    )

    write_executable(
        bin_dir / "gpg",
        textwrap.dedent(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--fingerprint" ]]; then
    cat <<'EOF'
pub   ed25519 2026-01-01 [SC]
      1111 2222 3333 4444 5555  6666 7777 8888 9999 AAAA
EOF
    exit 0
fi
if [[ "${1:-}" == "--list-secret-keys" ]]; then
    exit 0
fi
if [[ "${1:-}" == "--local-user" ]]; then
    output=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --output)
                output="$2"
                shift 2
                ;;
            *)
                shift
                ;;
        esac
    done
    : > "$output"
    exit 0
fi
if [[ "${1:-}" == "--verify" ]]; then
    exit 0
fi
exit 0
"""
        ),
    )
    write_executable(bin_dir / "jq", "#!/usr/bin/env bash\nexit 0\n")

    run_git(repo_dir, "init")
    (repo_dir / "README.md").write_text("first\n")
    run_git(repo_dir, "add", "README.md")
    run_git(repo_dir, "commit", "-m", "test: first commit")
    old_commit = run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()

    (repo_dir / "README.md").write_text("second\n")
    run_git(repo_dir, "commit", "-am", "test: second commit")
    expected_commit = run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    run_git(repo_dir, "tag", "1.2.3")

    manifest_path = repo_dir / "release-manifest-1.2.3.txt"
    manifest_path.write_text(
        textwrap.dedent(
            f"""# JoinMarket NG Release Manifest
## Git Commit
commit: {old_commit}
source_date_epoch: 1234567890
"""
        )
    )

    result = subprocess.run(
        [
            "bash",
            str(scripts_dir / "sign-release.sh"),
            "1.2.3",
            "--manifest",
            str(manifest_path),
            "--key",
            "TESTKEY",
            "--no-reproduce",
            "--no-push",
        ],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        env=make_env(bin_dir),
        check=False,
    )

    assert result.returncode != 0
    assert "Local manifest commit does not match tag 1.2.3." in result.stdout
    assert old_commit in result.stdout
    assert expected_commit in result.stdout


def test_verify_release_reports_manifest_commit_mismatch_for_local_signature(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / "repo"
    scripts_dir = repo_dir / "scripts"
    signatures_dir = repo_dir / "signatures" / "1.2.3"
    bin_dir = tmp_path / "bin"

    scripts_dir.mkdir(parents=True)
    signatures_dir.mkdir(parents=True)
    bin_dir.mkdir()

    (repo_dir / "signatures" / "trusted-keys.txt").parent.mkdir(
        parents=True, exist_ok=True
    )
    (repo_dir / "signatures" / "trusted-keys.txt").write_text(
        f"{TEST_FINGERPRINT} Test User\n"
    )
    (scripts_dir / "verify-release.sh").write_text(
        (SCRIPTS_DIR / "verify-release.sh").read_text()
    )

    local_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ci_commit = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    local_manifest = signatures_dir / f"{TEST_FINGERPRINT}-manifest.txt"
    local_manifest.write_text(
        textwrap.dedent(
            f"""# JoinMarket NG Release Manifest
## Git Commit
commit: {local_commit}
source_date_epoch: 1234567890

## Per-Platform Layer Digests (for reproducibility verification)

### maker-amd64-layers
sha256:deadbeef
"""
        )
    )
    (signatures_dir / f"{TEST_FINGERPRINT}.sig").write_text("signature\n")

    raw_manifest = "raw-manifest\n"
    manifest_contents = textwrap.dedent(
        f"""# JoinMarket NG Release Manifest
## Git Commit
commit: {ci_commit}
source_date_epoch: 1234567890

## Docker Images
example-manifest: sha256:{hashlib.sha256(raw_manifest.encode()).hexdigest()}
"""
    )

    write_executable(
        bin_dir / "curl",
        textwrap.dedent(
            f"""#!/usr/bin/env bash
set -euo pipefail
output=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o)
            output="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done
cat <<'EOF' > "$output"
{manifest_contents}EOF
"""
        ),
    )
    write_executable(
        bin_dir / "gpg",
        textwrap.dedent(
            f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${{1:-}}" == "--keyserver" ]]; then
    exit 0
fi
if [[ "${{1:-}}" == "--verify" ]]; then
    manifest="${{@: -1}}"
    case "$manifest" in
        *release-manifest-1.2.3.txt)
            exit 1
            ;;
        *{TEST_FINGERPRINT}-manifest.txt)
            exit 0
            ;;
        *)
            exit 1
            ;;
    esac
fi
exit 0
"""
        ),
    )
    write_executable(
        bin_dir / "docker",
        textwrap.dedent(
            f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "buildx" && "$2" == "imagetools" && "$3" == "inspect" ]]; then
    printf '%s' {raw_manifest!r}
    exit 0
fi
exit 1
"""
        ),
    )
    write_executable(bin_dir / "jq", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(scripts_dir / "verify-release.sh"), "1.2.3"],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        env=make_env(bin_dir),
        check=False,
    )

    assert result.returncode != 0
    assert "Manifest commit mismatch!" in result.stdout
    assert local_commit in result.stdout
    assert ci_commit in result.stdout
    assert "Insufficient valid signatures" in result.stdout


def test_build_release_passes_commit_and_ref_build_args(tmp_path: Path) -> None:
    """build-release.sh must propagate JOINMARKET_BUILD_COMMIT/REF to docker
    buildx so wheel metadata stamping in jmcore/setup.py matches CI builds.
    Without these args, /root/.local layer digests diverge between local and CI
    (see jmcore/setup.py:_resolve_commit which writes _build_info.py).
    """
    repo_dir = tmp_path / "repo"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    repo_dir.mkdir()
    (repo_dir / "scripts").mkdir()
    (repo_dir / "scripts" / "build-release.sh").write_text(
        (SCRIPTS_DIR / "build-release.sh").read_text()
    )
    (repo_dir / "scripts" / "build-release.sh").chmod(0o755)

    # Minimal source tree expected by build-release.sh
    (repo_dir / "jmcore" / "src" / "jmcore").mkdir(parents=True)
    (repo_dir / "jmcore" / "src" / "jmcore" / "version.py").write_text(
        '__version__ = "9.9.9"\n'
    )
    # Capture every docker invocation to a log file so we can assert on the
    # buildx args that would be passed for the real build.
    capture_log = tmp_path / "docker-calls.log"
    write_executable(
        bin_dir / "docker",
        textwrap.dedent(
            f"""#!/usr/bin/env bash
# Log all args (newline-separated to survive embedded spaces) and synthesize
# minimal OCI artifacts so build-release.sh's post-build extraction succeeds.
printf '%s\\n' "$@" >> {capture_log}
printf -- '---\\n' >> {capture_log}
case "$1" in
    buildx)
        case "${{2:-}}" in
            inspect)
                # Pretend a docker-container builder is already active.
                printf 'Driver: docker-container\\n'
                exit 0
                ;;
            build)
                # Find the --output dest=<path> and create a minimal OCI tar
                dest=""
                while [[ $# -gt 0 ]]; do
                    case "$1" in
                        --output)
                            # value form: type=oci,dest=/path,...
                            for kv in $(echo "$2" | tr ',' ' '); do
                                case "$kv" in
                                    dest=*) dest="${{kv#dest=}}" ;;
                                esac
                            done
                            shift 2
                            ;;
                        *)
                            shift
                            ;;
                    esac
                done
                tmpd=$(mktemp -d)
                mkdir -p "$tmpd/blobs/sha256"
                # Empty manifest with no layers; jq '.layers[].digest' yields nothing
                manifest_body='{{"layers":[]}}'
                manifest_digest=$(printf '%s' "$manifest_body" | sha256sum | cut -d' ' -f1)
                printf '%s' "$manifest_body" > "$tmpd/blobs/sha256/$manifest_digest"
                printf '{{"manifests":[{{"digest":"sha256:%s"}}]}}' "$manifest_digest" \\
                    > "$tmpd/index.json"
                tar -cf "$dest" -C "$tmpd" .
                exit 0
                ;;
        esac
        ;;
esac
exit 0
"""
        ),
    )
    write_executable(bin_dir / "jq", '#!/usr/bin/env bash\nexec /usr/bin/jq "$@"\n')

    run_git(repo_dir, "init", "--initial-branch=main")
    (repo_dir / "README.md").write_text("hi\n")
    run_git(repo_dir, "add", ".")
    run_git(repo_dir, "commit", "-m", "init")
    expected_commit = run_git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    run_git(repo_dir, "tag", "v9.9.9")

    result = subprocess.run(
        [
            "bash",
            str(repo_dir / "scripts" / "build-release.sh"),
            "9.9.9",
            "--jobs",
            "1",
        ],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        env=make_env(bin_dir),
        check=False,
    )
    assert result.returncode == 0, (
        f"build-release.sh failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    log = capture_log.read_text()
    # The buildx build invocation must include both the commit (full HEAD sha)
    # and the ref (the tag pointing at HEAD), so setup.py stamps deterministic
    # _build_info.py contents into the wheels regardless of build environment.
    assert f"JOINMARKET_BUILD_COMMIT={expected_commit}" in log, log
    assert "JOINMARKET_BUILD_REF=v9.9.9" in log, log
    assert "--bootstrap" in log, log


def test_build_release_retains_postprocessing_failure_logs(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (repo_dir / "scripts").mkdir(parents=True)
    (repo_dir / "scripts" / "build-release.sh").write_text(
        (SCRIPTS_DIR / "build-release.sh").read_text()
    )
    (repo_dir / "scripts" / "build-release.sh").chmod(0o755)
    (repo_dir / "jmcore" / "src" / "jmcore").mkdir(parents=True)
    (repo_dir / "jmcore" / "src" / "jmcore" / "version.py").write_text(
        '__version__ = "9.9.9"\n'
    )

    write_executable(
        bin_dir / "docker",
        textwrap.dedent(
            """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "buildx" && "$2" == "inspect" ]]; then
    printf 'Driver: docker-container\n'
    exit 0
fi
if [[ "$1" == "buildx" && "$2" == "build" ]]; then
    printf 'successful build output\n' >&2
    exit 0
fi
exit 1
"""
        ),
    )
    write_executable(bin_dir / "jq", "#!/usr/bin/env bash\nexit 0\n")
    write_executable(
        bin_dir / "tar",
        "#!/usr/bin/env bash\nprintf 'intentional extraction failure\\n' >&2\nexit 42\n",
    )

    run_git(repo_dir, "init", "--initial-branch=main")
    run_git(repo_dir, "add", ".")
    run_git(repo_dir, "commit", "-m", "init")

    result = subprocess.run(
        [
            "bash",
            str(repo_dir / "scripts" / "build-release.sh"),
            "9.9.9",
            "--jobs",
            "1",
        ],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        env=make_env(bin_dir),
        check=False,
    )

    assert result.returncode != 0
    log_paths = re.findall(r"\(log: ([^)]+)\)", result.stdout)
    assert len(log_paths) == 6, result.stdout
    for log_path in log_paths:
        retained_log = Path(log_path)
        assert retained_log.is_file()
        log_contents = retained_log.read_text()
        assert "successful build output" in log_contents
        assert "intentional extraction failure" in log_contents


def test_verify_release_reproduce_passes_commit_and_ref_build_args() -> None:
    """verify-release.sh --reproduce must pass JOINMARKET_BUILD_COMMIT/REF to
    docker buildx for layer digests to match CI. Asserted via static inspection
    of the script: end-to-end mocking of --reproduce (worktree, OCI export,
    layer comparison) is impractical, but the buildx command is built up in a
    single bash array literal whose contents are easy to verify textually.
    Regression guard for the reproducibility fix.
    """
    script = (SCRIPTS_DIR / "verify-release.sh").read_text()
    # Locate the buildx build command construction. There is exactly one
    # docker buildx build invocation in --reproduce mode.
    assert "docker buildx build" in script
    # Both build-args must be passed; values come from the verifier resolving
    # the manifest commit and the tag pointing at it (or VERSION fallback).
    assert "--build-arg JOINMARKET_BUILD_COMMIT=" in script
    assert "--build-arg JOINMARKET_BUILD_REF=" in script
    # The ref should be derived from a tag pointing at the commit, falling back
    # to the supplied VERSION so detached release commits without tags still
    # work in disaster-recovery scenarios.
    assert 'git -C "$PROJECT_ROOT" tag --points-at "$COMMIT"' in script
