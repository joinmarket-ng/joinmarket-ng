from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
IMAGES = {
    "directory-server",
    "directory-server-debug",
    "orderbook-watcher",
    "maker",
    "taker",
    "jmwalletd",
}
PLATFORMS = {"linux/amd64", "linux/arm64", "linux/arm/v7"}
PRODUCTION_LOCKS = {
    "directory_server/requirements.txt",
    "jmcore/requirements.txt",
    "jmwallet/requirements.txt",
    "jmwalletd/requirements.txt",
    "maker/requirements.txt",
    "orderbook_watcher/requirements.txt",
    "taker/requirements.txt",
    "tumbler/requirements.txt",
}
BITCOINTX_PACKAGES = {"jmcore", "jmwallet", "jmwalletd"}
BITCOINTX_VERSION = "2.1.1"
BITCOINTX_WHEEL_SHA256 = (
    "2f82999aa557da5f501bf10ca51dd830bcf16aba27ed8d976065c798454c11c6"
)
RUNTIME_IMAGE_STAGES = {
    "directory_server/Dockerfile": {"production", "debug"},
    "jmwalletd/Dockerfile": {"jmwalletd"},
    "maker/Dockerfile": {"production"},
    "orderbook_watcher/Dockerfile": {"production"},
    "taker/Dockerfile": {"production"},
}


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_dependency_updates_remain_maintainer_driven() -> None:
    assert not (REPO_ROOT / ".github" / "dependabot.yml").exists()


def test_third_party_actions_are_pinned_to_commit_shas() -> None:
    """Mutable tags let a moved or compromised upstream tag change workflow
    code; only GitHub-owned actions (immutable releases) and local actions
    may be referenced by tag."""
    unpinned: list[str] = []
    for workflow in sorted(WORKFLOWS.glob("*.yaml")):
        for lineno, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), start=1
        ):
            match = re.search(r"uses:\s*([^\s#]+)", line)
            if not match:
                continue
            ref = match.group(1)
            if ref.startswith(("./", "actions/", "github/")):
                continue
            _, _, version = ref.partition("@")
            if not re.fullmatch(r"[0-9a-f]{40}", version):
                unpinned.append(f"{workflow.name}:{lineno}: {ref}")
    assert unpinned == []


def _dockerfile_stage(path: str, stage: str) -> str:
    lines = (REPO_ROOT / path).read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.endswith(f" AS {stage}")
    )
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start + 1)
            if line.startswith("FROM ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_python_security_workflow_audits_locks_and_fresh_resolution() -> None:
    jobs = _workflow("security.yaml")["jobs"]

    lock_matrix = jobs["audit-production-locks"]["strategy"]["matrix"]
    assert set(lock_matrix["requirements"]) == PRODUCTION_LOCKS

    fresh_steps = jobs["audit-fresh-resolution"]["steps"]
    fresh_commands = "\n".join(step.get("run", "") for step in fresh_steps)
    assert '--path "$target_site_packages" --skip-editable' in fresh_commands
    assert "requirements-security.txt" in fresh_commands


def test_codeql_uses_extended_queries_for_supported_sources() -> None:
    jobs = _workflow("codeql.yaml")["jobs"]
    analyze = jobs["analyze"]

    assert set(analyze["strategy"]["matrix"]["language"]) == {
        "python",
        "javascript-typescript",
        "actions",
    }
    init = next(
        step for step in analyze["steps"] if step["name"] == "Initialize CodeQL"
    )
    assert init["with"]["queries"] == "+security-extended"


def test_image_scanner_covers_published_images_and_defaults_to_all_platforms() -> None:
    workflow = _workflow("image-security.yaml")
    matrix = workflow["jobs"]["scan"]["strategy"]["matrix"]
    workflow_text = (WORKFLOWS / "image-security.yaml").read_text(encoding="utf-8")

    assert set(matrix["image"]) == IMAGES
    assert all(platform in workflow_text for platform in PLATFORMS)
    assert '\'["main", "latest"]\'' in workflow_text
    assert "severity: HIGH,CRITICAL" in workflow_text
    assert "ignore-unfixed: true" in workflow_text
    assert "exit-code: '1'" in workflow_text


def test_runtime_images_exclude_python_package_installers() -> None:
    for dockerfile, stages in RUNTIME_IMAGE_STAGES.items():
        for stage in stages:
            stage_text = _dockerfile_stage(dockerfile, stage)
            assert "/usr/local/bin/pip*" in stage_text
            assert "/ensurepip" in stage_text
            assert "/site-packages/pip" in stage_text
            assert "/site-packages/pip-*.dist-info" in stage_text

    jmwalletd_builder = _dockerfile_stage("jmwalletd/Dockerfile", "builder")
    assert "/opt/venv/bin/pip*" in jmwalletd_builder
    assert (
        "/opt/venv/lib/python${PYTHON_VERSION%.*}/site-packages/pip"
        in jmwalletd_builder
    )


def test_runtime_images_include_native_secp256k1() -> None:
    for dockerfile, stages in RUNTIME_IMAGE_STAGES.items():
        for stage in stages:
            assert "libsecp256k1-2=0.5.0-2+b1" in _dockerfile_stage(dockerfile, stage)


def test_macos_uses_secp256k1_homebrew_formula() -> None:
    installer = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    setup_action = (
        REPO_ROOT / ".github/actions/setup-python-deps/action.yaml"
    ).read_text(encoding="utf-8")

    assert "brew list secp256k1" in installer
    assert 'missing_deps+=("secp256k1")' in installer
    assert "brew install secp256k1" in setup_action
    assert "brew list libsecp256k1" not in installer
    assert "brew install libsecp256k1" not in setup_action


def test_bitcointx_dependency_is_pinned_to_release_wheel() -> None:
    expected_url = (
        "https://github.com/m0wer/python-bitcointx/releases/download/"
        f"python-bitcointx-v{BITCOINTX_VERSION}/"
        f"python_bitcointx-{BITCOINTX_VERSION}-py3-none-any.whl"
    )
    for package in BITCOINTX_PACKAGES:
        manifest = (REPO_ROOT / package / "pyproject.toml").read_text(encoding="utf-8")
        assert "coincurve" not in manifest
        assert expected_url in manifest
        assert BITCOINTX_WHEEL_SHA256 in manifest

        for lock_name in ("requirements.txt", "requirements-dev.txt"):
            lock = (REPO_ROOT / package / lock_name).read_text(encoding="utf-8")
            assert "coincurve" not in lock
            assert expected_url in lock
            assert BITCOINTX_WHEEL_SHA256 in lock


def test_main_and_release_promotions_depend_on_image_scans() -> None:
    ci_jobs = _workflow("ci.yaml")["jobs"]
    release_jobs = _workflow("release.yaml")["jobs"]

    assert {"scan-amd64", "scan-arm64", "scan-armv7"}.issubset(
        set(ci_jobs["publish-images"]["needs"])
    )
    assert release_jobs["scan-candidate-images"]["needs"] == "build-candidate-images"
    assert set(release_jobs["promote-docker-images"]["needs"]) == {
        "prepare",
        "scan-candidate-images",
    }
    assert set(release_jobs["create-release"]["needs"]) == {
        "prepare",
        "promote-docker-images",
    }

    candidate_matrix = release_jobs["build-candidate-images"]["strategy"]["matrix"][
        "include"
    ]
    promotion_matrix = release_jobs["promote-docker-images"]["strategy"]["matrix"][
        "include"
    ]
    assert {entry["image"] for entry in candidate_matrix} == IMAGES
    assert {entry["image"] for entry in promotion_matrix} == IMAGES


def test_create_release_uses_explicit_repository_context_and_existing_tag() -> None:
    release_job = _workflow("release.yaml")["jobs"]["create-release"]
    create_release = next(
        step for step in release_job["steps"] if step["name"] == "Create Release"
    )

    assert create_release["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert create_release["env"]["GH_REPO"] == "${{ github.repository }}"
    assert (
        'gh release create "${{ needs.prepare.outputs.version }}"'
        in create_release["run"]
    )
    assert "--verify-tag" in create_release["run"]


def test_releases_start_as_prereleases_awaiting_signature_quorum() -> None:
    """Tag pushes must not publish a release users can see via
    releases/latest before the trusted signature quorum exists; promotion
    happens in promote-release.yaml once signatures land on main."""
    release_jobs = _workflow("release.yaml")["jobs"]
    create_release = next(
        step
        for step in release_jobs["create-release"]["steps"]
        if step["name"] == "Create Release"
    )

    assert "--prerelease" in create_release["run"]
    assert "Awaiting signature quorum" in create_release["run"]

    # The old tag-checkout cross-check never saw local-first signatures
    # (they are committed after the tag commit); the check now lives in
    # verify-release.sh, run by the promotion gate from main.
    manifest_steps = release_jobs["generate-manifest"]["steps"]
    assert all(
        step.get("name") != "Verify against pre-signed local manifests"
        for step in manifest_steps
    )


def test_promote_release_gates_publication_on_signature_quorum() -> None:
    # Annotate with non-str keys: YAML parses the bare `on` key as boolean True.
    workflow: dict[Any, Any] = yaml.safe_load(
        (WORKFLOWS / "promote-release.yaml").read_text(encoding="utf-8")
    )

    triggers = workflow[True]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["push"]["paths"] == ["signatures/**"]
    assert "workflow_dispatch" in triggers
    # Race guard: signatures pushed before the Release workflow creates the
    # pre-release (observed with 0.39.1) must still promote once it exists.
    assert triggers["workflow_run"]["workflows"] == ["Release"]
    assert triggers["workflow_run"]["types"] == ["completed"]

    assert workflow["concurrency"]["group"] == "promote-release"
    assert workflow["permissions"] == {"contents": "read"}

    job = workflow["jobs"]["promote"]
    assert job["permissions"] == {"contents": "write"}

    checkout = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout")
    )
    assert checkout["with"]["fetch-depth"] == 0

    promote = next(
        step for step in job["steps"] if step["name"] == "Promote signed pre-releases"
    )
    assert promote["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert promote["env"]["GH_REPO"] == "${{ github.repository }}"
    run = promote["run"]
    # Full verification (signatures, installer quorum, digests) gates the flip.
    assert './scripts/verify-release.sh "$version" --require-installer' in run
    assert "--prerelease=false" in run
    # Insufficient signatures leave the pre-release untouched without failing.
    assert "leaving as pre-release" in run
    # Promoting an old patch release must not steal `latest` from a newer one.
    assert "sort -V" in run
