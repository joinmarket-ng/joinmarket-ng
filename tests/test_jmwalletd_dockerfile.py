from __future__ import annotations

import re
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
JMWALLETD_DOCKERFILE = REPO_ROOT / "jmwalletd" / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
SIGNET_COMPOSE_FILE = REPO_ROOT / "docker-compose.jam-ng-signet.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yaml"
PARALLEL_TEST_SCRIPT = REPO_ROOT / "scripts" / "run_parallel_tests.sh"
PLAYWRIGHT_API_HELPER = (
    REPO_ROOT / "tests" / "playwright" / "fixtures" / "jmwalletd-api.ts"
)
PLAYWRIGHT_GLOBAL_SETUP = REPO_ROOT / "tests" / "playwright" / "global-setup.ts"
RELEASE_SCRIPTS = (
    REPO_ROOT / "scripts" / "build-release.sh",
    REPO_ROOT / "scripts" / "sign-release.sh",
    REPO_ROOT / "scripts" / "verify-release.sh",
)
STANDALONE_NG_IMAGE = "ghcr.io/joinmarket-webui/jam-dev-standalone-ng:master"
STANDALONE_NG_CONTEXT = (
    "https://github.com/joinmarket-webui/jam-docker.git#master:standalone-ng"
)


def test_jmwalletd_dockerfile_only_builds_the_standalone_daemon() -> None:
    content = JMWALLETD_DOCKERFILE.read_text()
    stages = re.findall(r"^FROM\s+.*\s+AS\s+(\S+)\s*$", content, re.MULTILINE)

    assert stages == ["builder", "jmwalletd"]
    assert "jam-builder" not in content
    assert "AS jam-ng" not in content


def test_playwright_uses_jam_docker_standalone_ng() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    service = compose["services"]["jam-playwright"]

    assert service["image"] == f"${{JAM_NG_IMAGE:-{STANDALONE_NG_IMAGE}}}"
    assert service["build"]["context"] == (
        f"${{JAM_DOCKER_CONTEXT:-{STANDALONE_NG_CONTEXT}}}"
    )
    assert service["build"]["args"]["JM_NG_REPO_REF"] == "${JM_NG_REPO_REF:-main}"
    assert service["build"]["args"]["SKIP_RELEASE_VERIFICATION"] == (
        "${SKIP_RELEASE_VERIFICATION:-true}"
    )
    assert service["ports"] == ["29183:80"]
    assert service["environment"] == [
        "BITCOIN__BACKEND_TYPE=descriptor_wallet",
        "BITCOIN__RPC_URL=http://jm-bitcoin:18443",
        "BITCOIN__RPC_COOKIE_FILE=/shared/.cookie",
        "NETWORK_CONFIG__NETWORK=testnet",
        "NETWORK_CONFIG__BITCOIN_NETWORK=regtest",
        "NETWORK_CONFIG__DIRECTORY_SERVERS=jm-directory:5222,jm-directory2:5223",
        "LOGGING__LEVEL=DEBUG",
        "DIRECTORY_NODES=jm-directory:5222",
    ]


def test_signet_uses_jam_docker_standalone_ng_port() -> None:
    compose = yaml.safe_load(SIGNET_COMPOSE_FILE.read_text())
    service = compose["services"]["jam"]

    assert service["image"] == f"${{JAM_NG_IMAGE:-{STANDALONE_NG_IMAGE}}}"
    assert (
        "traefik.http.services.jam-ng-signet.loadbalancer.server.port=80"
        in service["labels"]
    )
    assert service["healthcheck"]["test"][-1].endswith(",80),5); s.close()")


def test_playwright_ci_builds_the_checked_out_joinmarket_ref() -> None:
    workflow = CI_WORKFLOW.read_text()

    assert (
        "JM_NG_REPO: ${{ github.server_url }}/"
        "${{ github.event.pull_request.head.repo.full_name || github.repository }}"
        in workflow
    )
    assert (
        "JM_NG_REPO_REF: "
        "${{ github.event.pull_request.head.ref || github.ref_name }}" in workflow
    )


def test_release_and_ci_matrices_publish_jmwalletd_but_not_jam_ng() -> None:
    ci_jobs = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]
    release_jobs = yaml.safe_load(RELEASE_WORKFLOW.read_text())["jobs"]

    matrices = [
        ci_jobs[job]["strategy"]["matrix"]["include"]
        for job in ("build-images", "build-arm64", "build-armv7", "publish-images")
    ]
    matrices.append(release_jobs["publish-docker"]["strategy"]["matrix"]["include"])

    for matrix in matrices:
        images = {entry["image"] for entry in matrix}
        assert "jmwalletd" in images
        assert "jam-ng" not in images


def test_release_scripts_build_jmwalletd_but_not_jam_ng() -> None:
    for script_path in RELEASE_SCRIPTS:
        arrays = re.findall(
            r"^\s*IMAGES=\(([^)]*)\)", script_path.read_text(), re.MULTILINE
        )
        assert arrays, f"No release image array found in {script_path}"
        for array in arrays:
            assert '"jmwalletd"' in array
            assert '"jam-ng"' not in array


def test_parallel_playwright_uses_standalone_ng_http_endpoint() -> None:
    script = PARALLEL_TEST_SCRIPT.read_text()
    playwright_runner = script.split("run_suite_playwright()", maxsplit=1)[1].split(
        "run_suite_jmwallet()", maxsplit=1
    )[0]

    assert (
        'curl -sf "http://127.0.0.1:${jam_pw_port}/api/v1/session"' in playwright_runner
    )
    assert 'JAM_URL="http://localhost:${jam_pw_port}"' in playwright_runner
    assert 'JMWALLETD_URL="http://localhost:${jam_pw_port}"' in playwright_runner
    assert "NODE_TLS_REJECT_UNAUTHORIZED" not in playwright_runner


def test_parallel_runner_imports_this_worktree() -> None:
    script = PARALLEL_TEST_SCRIPT.read_text()

    assert (
        'export PYTHONPATH="${PROJECT_PYTHONPATH%:}${PYTHONPATH:+:$PYTHONPATH}"'
        in script
    )
    for component in ("jmcore", "jmwallet", "jmwalletd", "maker", "taker"):
        assert f'"$PROJECT_ROOT/{component}/src"' in script


def test_parallel_runner_uses_shared_images() -> None:
    script = PARALLEL_TEST_SCRIPT.read_text()

    assert (
        'SHARED_IMAGE_PROJECT="${JM_SHARED_IMAGE_PROJECT:-$(default_shared_image_project)}"'
        in script
    )
    assert "tr -cs 'a-z0-9_-' '-'" in script
    assert '[[ ! "$SHARED_IMAGE_PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]]' in script
    assert 'COMPOSE_PROJECT_NAME="$SHARED_IMAGE_PROJECT" docker compose' in script
    assert "docker compose config --environment" in script
    expected = {
        "DIRECTORY_SERVER_IMAGE": "directory",
        "ORDERBOOK_WATCHER_IMAGE": "orderbook-watcher",
        "MAKER_IMAGE": "maker",
        "TAKER_IMAGE": "taker",
        "JMWALLETD_IMAGE": "jmwalletd",
    }
    for variable, service in expected.items():
        assert (
            f'export {variable}="${{{variable.lower()}:-'
            f'${{SHARED_IMAGE_PROJECT}}-{service}:latest}}"' in script
        )


def test_playwright_uses_standalone_ng_authorization_header() -> None:
    sources = PLAYWRIGHT_API_HELPER.read_text() + PLAYWRIGHT_GLOBAL_SETUP.read_text()
    bearer_headers = re.findall(
        r'["\']?([\w-]+)["\']?\s*:\s*`Bearer \$\{',
        sources,
    )

    assert bearer_headers
    assert {header.lower() for header in bearer_headers} == {"x-jm-authorization"}
