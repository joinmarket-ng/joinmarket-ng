#!/usr/bin/env python3
"""Update Flatpak sources and pinned JAM Docker dependencies."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


USER_AGENT = "joinmarket-ng-dependency-updater/1.0"
JAM_REPO_URL = "https://github.com/joinmarket-webui/jam.git"
JAM_DOCKER_REPO_URL = "https://github.com/joinmarket-webui/jam-docker.git"
SEMVER_NUMBER = r"(?:0|[1-9]\d*)"
SEMVER_PRERELEASE_IDENTIFIER = (
    rf"(?:{SEMVER_NUMBER}|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
)
SEMVER_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"

LIBEVENT_RE = re.compile(
    r"(?ms)(- name: libevent\b.*?url:\s*)(\S+)(\s*\n\s*sha256:\s*)([a-f0-9]+)"
)
TOR_RE = re.compile(
    r"(?ms)(- name: tor\b.*?url:\s*)(\S+)(\s*\n\s*sha256:\s*)([a-f0-9]+)"
)
LIBSODIUM_RE = re.compile(
    r"(?ms)(- name: libsodium\b.*?url:\s*)(\S+)(\s*\n\s*sha256:\s*)([a-f0-9]+)"
)
NEUTRINO_AMD64_RE = re.compile(
    r"(?ms)(url:\s*)(https://github\.com/m0wer/neutrino-api/releases/download/\S+/"
    r"neutrinod-linux-amd64)(\s*\n\s*sha256:\s*)([a-f0-9]+)"
)
NEUTRINO_ARM64_RE = re.compile(
    r"(?ms)(url:\s*)(https://github\.com/m0wer/neutrino-api/releases/download/\S+/"
    r"neutrinod-linux-arm64)(\s*\n\s*sha256:\s*)([a-f0-9]+)"
)
JAM_FRONTEND_MODULE_RE = re.compile(
    r"(?ms)^  - name: jam-frontend[ \t]*\n.*?(?=^  - name: |\Z)"
)
JAM_ARCHIVE_RE = re.compile(
    r"(?m)(^      - type: archive[ \t]*\n"
    r"        url: https://codeload\.github\.com/joinmarket-webui/jam/tar\.gz/)"
    r"([a-f0-9]{40})"
    r"([ \t]*\n        archive-type: tar-gzip[ \t]*\n        sha256: )"
    r"([a-f0-9]{64})([ \t]*$)"
)
JAM_PLAYWRIGHT_BASE_SERVICE_RE = re.compile(
    r"(?ms)^  jam-playwright-base:\s*\n.*?(?=^  [A-Za-z0-9][A-Za-z0-9_-]*:\s*$|\Z)"
)
JAM_DOCKER_CONTEXT_RE = re.compile(
    r"(\$\{JAM_DOCKER_CONTEXT:-https://github\.com/joinmarket-webui/"
    r"jam-docker\.git#)([a-f0-9]{40})(:standalone-ng\})"
)
JAM_DOCKER_TEST_COMMIT_RE = re.compile(
    r'(?m)^(JAM_DOCKER_COMMIT = ")([a-f0-9]{40})(")$'
)
JAM_REPO_TEST_REF_RE = re.compile(r'(?m)^(JAM_REPO_REF = ")([^"\s]+)(")$')
JAM_REPO_REF_RE = re.compile(
    r"(?m)(^\s*JAM_REPO_REF:\s*\$\{JAM_REPO_REF:-)([^}\s]+)(\}[ \t]*$)"
)
JAM_RELEASE_TAG_RE = re.compile(
    rf"^v(?P<major>{SEMVER_NUMBER})\."
    rf"(?P<minor>{SEMVER_NUMBER})\."
    rf"(?P<patch>{SEMVER_NUMBER})"
    rf"(?:-(?P<prerelease>{SEMVER_PRERELEASE_IDENTIFIER}"
    rf"(?:\.{SEMVER_PRERELEASE_IDENTIFIER})*))?"
    rf"(?:\+{SEMVER_BUILD_IDENTIFIER}(?:\.{SEMVER_BUILD_IDENTIFIER})*)?$"
)


class UpdateError(RuntimeError):
    pass


def fetch_bytes(url: str) -> bytes:
    request = Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    )
    with urlopen(request, timeout=120) as response:
        return response.read()


def fetch_text(url: str) -> str:
    return fetch_bytes(url).decode("utf-8", "replace")


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_text(url))


def sha256_url(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    with urlopen(request, timeout=120) as response:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def latest_release(repo: str) -> dict[str, Any]:
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    data = fetch_json(url)
    if not isinstance(data, dict):
        raise UpdateError(f"Invalid response from {url}")
    return data


def pick_asset_url(
    release: dict[str, Any],
    predicate: Callable[[str], bool],
    description: str,
) -> str:
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("GitHub release payload has no assets array")

    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if not isinstance(name, str):
            continue
        if not predicate(name):
            continue
        url = asset.get("browser_download_url")
        if isinstance(url, str) and url:
            return url

    raise UpdateError(f"Could not find asset: {description}")


def latest_libsodium_source_url(release: dict[str, Any]) -> str:
    try:
        return pick_asset_url(
            release,
            lambda name: (
                re.fullmatch(r"libsodium-\d+\.\d+\.\d+\.tar\.gz", name) is not None
            ),
            "libsodium source tarball",
        )
    except UpdateError:
        pass

    tag_name = release.get("tag_name")
    if isinstance(tag_name, str):
        version_match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:-RELEASE)?", tag_name)
        if version_match is not None:
            version = version_match.group(1)
            return (
                "https://download.libsodium.org/libsodium/releases/"
                f"libsodium-{version}.tar.gz"
            )

    tarball_url = release.get("tarball_url")
    if isinstance(tarball_url, str) and tarball_url:
        return tarball_url

    raise UpdateError("Could not determine libsodium source tarball URL")


def latest_tor_version() -> str:
    html = fetch_text("https://dist.torproject.org/")
    versions = set(re.findall(r"tor-(0\.4\.\d+\.\d+)\.tar\.gz", html))
    if not versions:
        raise UpdateError("Could not find Tor versions on dist.torproject.org")
    return max(
        versions, key=lambda version: tuple(int(part) for part in version.split("."))
    )


def _validate_commit(commit: str, description: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise UpdateError(f"Unexpected {description} commit format: {commit}")
    return commit


def latest_remote_commit(repo_url: str, ref: str, description: str) -> str:
    output = subprocess.check_output(
        ["git", "ls-remote", repo_url, ref],
        text=True,
    ).strip()
    if not output:
        raise UpdateError(f"Could not fetch latest {description} commit")
    lines = output.splitlines()
    if len(lines) != 1:
        raise UpdateError(f"Expected one {description} ref, received {len(lines)}")
    return _validate_commit(lines[0].split()[0], description)


def parse_jam_remote_tags(output: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise UpdateError(f"Unexpected JAM tag ref: {line}")
        commit, remote_ref = parts
        refs[remote_ref] = _validate_commit(commit, "JAM tag")

    tags: dict[str, str] = {}
    for remote_ref, commit in refs.items():
        if not remote_ref.startswith("refs/tags/") or remote_ref.endswith("^{}"):
            continue
        tag = remote_ref.removeprefix("refs/tags/")
        tags[tag] = refs.get(f"{remote_ref}^{{}}", commit)
    return tags


def jam_release_sort_key(
    tag: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    match = JAM_RELEASE_TAG_RE.fullmatch(tag)
    if match is None:
        raise UpdateError(f"Unexpected JAM release tag format: {tag}")

    prerelease = match.group("prerelease")
    prerelease_key: tuple[tuple[int, int | str], ...] = ()
    if prerelease is not None:
        prerelease_key = tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in prerelease.split(".")
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        1 if prerelease is None else 0,
        prerelease_key,
    )


def latest_jam_release() -> tuple[str, str]:
    output = subprocess.check_output(
        ["git", "ls-remote", "--tags", JAM_REPO_URL],
        text=True,
    ).strip()
    tags = {
        tag: commit
        for tag, commit in parse_jam_remote_tags(output).items()
        if JAM_RELEASE_TAG_RE.fullmatch(tag) is not None
    }
    if not tags:
        raise UpdateError("Could not find a versioned JAM release tag")
    latest_tag = max(tags, key=jam_release_sort_key)
    return latest_tag, tags[latest_tag]


def latest_jam_docker_commit() -> str:
    return latest_remote_commit(
        JAM_DOCKER_REPO_URL,
        "refs/heads/master",
        "jam-docker master",
    )


def extract_url_sha(pattern: re.Pattern[str], text: str, name: str) -> tuple[str, str]:
    match = pattern.search(text)
    if not match:
        raise UpdateError(f"Could not find {name} in Flatpak manifest")
    return match.group(2), match.group(4)


def replace_url_sha(
    pattern: re.Pattern[str], text: str, url: str, sha256: str, name: str
) -> str:
    def _replacement(match: re.Match[str]) -> str:
        return f"{match.group(1)}{url}{match.group(3)}{sha256}"

    updated, count = pattern.subn(_replacement, text, count=1)
    if count != 1:
        raise UpdateError(f"Failed to update {name} in Flatpak manifest")
    return updated


def _jam_archive_source(text: str) -> tuple[re.Match[str], re.Match[str]]:
    modules = list(JAM_FRONTEND_MODULE_RE.finditer(text))
    if len(modules) != 1:
        raise UpdateError(f"Expected one jam-frontend module, found {len(modules)}")
    module_text = modules[0].group(0)
    source_count = len(re.findall(r"(?m)^      - type:", module_text))
    matches = list(JAM_ARCHIVE_RE.finditer(module_text))
    if source_count != 1 or len(matches) != 1:
        raise UpdateError("Expected one checksum-pinned jam-frontend archive source")
    return modules[0], matches[0]


def extract_jam_archive(text: str) -> tuple[str, str]:
    _, source = _jam_archive_source(text)
    return source.group(2), source.group(4)


def replace_jam_archive(text: str, commit: str, sha256: str) -> str:
    _validate_commit(commit, "JAM Flatpak source")
    if re.fullmatch(r"[a-f0-9]{64}", sha256) is None:
        raise UpdateError("Unexpected JAM Flatpak archive SHA256 format")
    module, source = _jam_archive_source(text)
    replacement = f"{source.group(1)}{commit}{source.group(3)}{sha256}{source.group(5)}"
    start = module.start() + source.start()
    end = module.start() + source.end()
    return f"{text[:start]}{replacement}{text[end:]}"


def extract_jam_compose_pins(text: str) -> tuple[str, str]:
    service_matches = list(JAM_PLAYWRIGHT_BASE_SERVICE_RE.finditer(text))
    if len(service_matches) != 1:
        raise UpdateError(
            "Expected one jam-playwright-base service in Compose file, "
            f"found {len(service_matches)}"
        )
    service_text = service_matches[0].group(0)

    context_matches = list(JAM_DOCKER_CONTEXT_RE.finditer(service_text))
    context_match = context_matches[0] if len(context_matches) == 1 else None
    if context_match is None:
        raise UpdateError(
            "Expected one pinned jam-docker context in jam-playwright-base service, "
            f"found {len(context_matches)}"
        )
    ref_matches = list(JAM_REPO_REF_RE.finditer(service_text))
    ref_match = ref_matches[0] if len(ref_matches) == 1 else None
    if ref_match is None:
        raise UpdateError(
            "Expected one pinned JAM repository ref in jam-playwright-base service, "
            f"found {len(ref_matches)}"
        )
    return ref_match.group(2), context_match.group(2)


def replace_jam_compose_pins(text: str, jam_ref: str, jam_docker_commit: str) -> str:
    extract_jam_compose_pins(text)
    service_match = JAM_PLAYWRIGHT_BASE_SERVICE_RE.search(text)
    if service_match is None:  # Guaranteed by extract_jam_compose_pins.
        raise UpdateError("Could not find jam-playwright-base service in Compose file")
    service_text = service_match.group(0)
    updated, ref_count = JAM_REPO_REF_RE.subn(
        lambda match: f"{match.group(1)}{jam_ref}{match.group(3)}",
        service_text,
    )
    updated, context_count = JAM_DOCKER_CONTEXT_RE.subn(
        lambda match: f"{match.group(1)}{jam_docker_commit}{match.group(3)}",
        updated,
    )
    if ref_count != 1 or context_count != 1:
        raise UpdateError("Failed to update pinned JAM dependencies in Compose file")
    return f"{text[: service_match.start()]}{updated}{text[service_match.end() :]}"


def extract_jam_test_commit(text: str) -> str:
    matches = list(JAM_DOCKER_TEST_COMMIT_RE.finditer(text))
    if len(matches) != 1:
        raise UpdateError(
            "Expected one JAM_DOCKER_COMMIT in JAM Dockerfile tests, "
            f"found {len(matches)}"
        )
    return matches[0].group(2)


def replace_jam_test_commit(text: str, commit: str) -> str:
    extract_jam_test_commit(text)
    updated, count = JAM_DOCKER_TEST_COMMIT_RE.subn(
        lambda match: f"{match.group(1)}{commit}{match.group(3)}",
        text,
    )
    if count != 1:
        raise UpdateError("Failed to update JAM_DOCKER_COMMIT in JAM Dockerfile tests")
    return updated


def extract_jam_test_ref(text: str) -> str:
    matches = list(JAM_REPO_TEST_REF_RE.finditer(text))
    if len(matches) != 1:
        raise UpdateError(
            f"Expected one JAM_REPO_REF in JAM Dockerfile tests, found {len(matches)}"
        )
    return matches[0].group(2)


def replace_jam_test_ref(text: str, jam_ref: str) -> str:
    extract_jam_test_ref(text)
    updated, count = JAM_REPO_TEST_REF_RE.subn(
        lambda match: f"{match.group(1)}{jam_ref}{match.group(3)}",
        text,
    )
    if count != 1:
        raise UpdateError("Failed to update JAM_REPO_REF in JAM Dockerfile tests")
    return updated


def report_url_sha(
    name: str, current_url: str, current_sha: str, latest_url: str, latest_sha: str
) -> bool:
    changed = current_url != latest_url or current_sha != latest_sha
    if changed:
        print(f"[UPDATE] {name}")
        if current_url != latest_url:
            print(f"  URL:    {current_url}")
            print(f"  New:    {latest_url}")
        if current_sha != latest_sha:
            print(f"  SHA256: {current_sha}")
            print(f"  New:    {latest_sha}")
    else:
        print(f"[OK] {name} is up to date")
    return changed


def report_jam_docker_pins(compose_commit: str, test_commit: str, latest: str) -> bool:
    changed = compose_commit != latest or test_commit != latest
    if changed:
        print("[UPDATE] jam-docker")
        if compose_commit != latest:
            print(f"  Compose commit: {compose_commit}")
            print(f"  New:            {latest}")
        if test_commit != latest:
            print(f"  Test commit:    {test_commit}")
            print(f"  New:            {latest}")
    else:
        print("[OK] jam-docker is up to date")
    return changed


def report_jam_release_pins(compose_ref: str, test_ref: str, latest: str) -> bool:
    changed = compose_ref != latest or test_ref != latest
    if changed:
        print("[UPDATE] JAM release")
        if compose_ref != latest:
            print(f"  Compose ref: {compose_ref}")
            print(f"  New:         {latest}")
        if test_ref != latest:
            print(f"  Test ref:    {test_ref}")
            print(f"  New:         {latest}")
    else:
        print("[OK] JAM release is up to date")
    return changed


def report_version(name: str, current: str, latest: str) -> bool:
    changed = current != latest
    if changed:
        print(f"[UPDATE] {name}")
        print(f"  Version: {current}")
        print(f"  New:     {latest}")
    else:
        print(f"[OK] {name} is up to date")
    return changed


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    default_manifest_path = project_root / "flatpak" / "org.joinmarketng.JamNG.yml"
    default_compose_path = project_root / "docker-compose.yml"
    default_jam_pin_test_path = project_root / "tests" / "test_jmwalletd_dockerfile.py"

    parser = argparse.ArgumentParser(
        description="Update Flatpak sources and pinned JAM Docker dependencies"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check for updates without modifying files",
    )
    parser.add_argument(
        "--jam-frontend-commit",
        metavar="SHA",
        help=(
            "Explicitly select a JAM Flatpak frontend commit and pin its archive checksum "
            "(otherwise retain the current frontend)"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=default_manifest_path,
        help="Path to Flatpak manifest (default: flatpak/org.joinmarketng.JamNG.yml)",
    )
    parser.add_argument(
        "--compose",
        type=Path,
        default=default_compose_path,
        help="Path to Compose file containing JAM pins (default: docker-compose.yml)",
    )
    parser.add_argument(
        "--jam-pin-test",
        type=Path,
        default=default_jam_pin_test_path,
        help=(
            "Path to the test containing JAM_REPO_REF and JAM_DOCKER_COMMIT "
            "(default: tests/test_jmwalletd_dockerfile.py)"
        ),
    )
    args = parser.parse_args()

    manifest_path = args.manifest
    if not manifest_path.is_file():
        raise UpdateError(f"Flatpak manifest not found: {manifest_path}")
    compose_path = args.compose
    if not compose_path.is_file():
        raise UpdateError(f"Compose file not found: {compose_path}")
    jam_pin_test_path = args.jam_pin_test
    if not jam_pin_test_path.is_file():
        raise UpdateError(f"JAM Docker pin test not found: {jam_pin_test_path}")

    manifest_text = manifest_path.read_text(encoding="utf-8")
    compose_text = compose_path.read_text(encoding="utf-8")
    jam_pin_test_text = jam_pin_test_path.read_text(encoding="utf-8")

    current_libevent_url, current_libevent_sha = extract_url_sha(
        LIBEVENT_RE, manifest_text, "libevent"
    )
    current_tor_url, current_tor_sha = extract_url_sha(TOR_RE, manifest_text, "tor")
    current_libsodium_url, current_libsodium_sha = extract_url_sha(
        LIBSODIUM_RE, manifest_text, "libsodium"
    )
    current_neutrino_amd64_url, current_neutrino_amd64_sha = extract_url_sha(
        NEUTRINO_AMD64_RE,
        manifest_text,
        "neutrino-api (amd64)",
    )
    current_neutrino_arm64_url, current_neutrino_arm64_sha = extract_url_sha(
        NEUTRINO_ARM64_RE,
        manifest_text,
        "neutrino-api (arm64)",
    )
    current_jam_commit, current_jam_sha = extract_jam_archive(manifest_text)
    current_jam_ref, current_jam_docker_commit = extract_jam_compose_pins(compose_text)
    current_jam_test_ref = extract_jam_test_ref(jam_pin_test_text)
    current_jam_test_commit = extract_jam_test_commit(jam_pin_test_text)

    # A newer tag alone says nothing about whether it retains the features in
    # the selected Flatpak frontend. Only an explicit commit opts into replacing it.
    jam_commit = (
        _validate_commit(args.jam_frontend_commit, "JAM Flatpak source")
        if args.jam_frontend_commit is not None
        else current_jam_commit
    )
    jam_url = f"https://codeload.github.com/joinmarket-webui/jam/tar.gz/{jam_commit}"
    jam_sha = (
        sha256_url(jam_url) if args.jam_frontend_commit is not None else current_jam_sha
    )
    if args.jam_frontend_commit is None:
        print(
            "[INFO] Keeping the independently pinned JAM Flatpak frontend "
            f"at {current_jam_commit} (use --jam-frontend-commit to change it)"
        )
    elif jam_commit != current_jam_commit or jam_sha != current_jam_sha:
        print(
            "[ACTION] Review the new frontend and update the exact pin expectations "
            "in tests/test_flatpak_manifest.py before merging"
        )

    libevent_release = latest_release("libevent/libevent")
    latest_libevent_url = pick_asset_url(
        libevent_release,
        lambda name: name.startswith("libevent-") and name.endswith(".tar.gz"),
        "libevent source tarball",
    )
    latest_libevent_sha = sha256_url(latest_libevent_url)

    latest_tor = latest_tor_version()
    latest_tor_url = f"https://dist.torproject.org/tor-{latest_tor}.tar.gz"
    latest_tor_sha = sha256_url(latest_tor_url)

    libsodium_release = latest_release("jedisct1/libsodium")
    latest_libsodium_url = latest_libsodium_source_url(libsodium_release)
    latest_libsodium_sha = sha256_url(latest_libsodium_url)

    neutrino_release = latest_release("m0wer/neutrino-api")
    latest_neutrino_amd64_url = pick_asset_url(
        neutrino_release,
        lambda name: name == "neutrinod-linux-amd64",
        "neutrino-api linux amd64 binary",
    )
    latest_neutrino_arm64_url = pick_asset_url(
        neutrino_release,
        lambda name: name == "neutrinod-linux-arm64",
        "neutrino-api linux arm64 binary",
    )
    latest_neutrino_amd64_sha = sha256_url(latest_neutrino_amd64_url)
    latest_neutrino_arm64_sha = sha256_url(latest_neutrino_arm64_url)

    latest_jam_ref, _ = latest_jam_release()
    latest_jam_docker = latest_jam_docker_commit()

    changed = [
        report_url_sha(
            "libevent",
            current_libevent_url,
            current_libevent_sha,
            latest_libevent_url,
            latest_libevent_sha,
        ),
        report_url_sha(
            "tor", current_tor_url, current_tor_sha, latest_tor_url, latest_tor_sha
        ),
        report_url_sha(
            "libsodium",
            current_libsodium_url,
            current_libsodium_sha,
            latest_libsodium_url,
            latest_libsodium_sha,
        ),
        report_url_sha(
            "neutrino-api (amd64)",
            current_neutrino_amd64_url,
            current_neutrino_amd64_sha,
            latest_neutrino_amd64_url,
            latest_neutrino_amd64_sha,
        ),
        report_url_sha(
            "neutrino-api (arm64)",
            current_neutrino_arm64_url,
            current_neutrino_arm64_sha,
            latest_neutrino_arm64_url,
            latest_neutrino_arm64_sha,
        ),
        report_jam_release_pins(
            current_jam_ref,
            current_jam_test_ref,
            latest_jam_ref,
        ),
        report_url_sha(
            "JAM Flatpak source",
            f"https://codeload.github.com/joinmarket-webui/jam/tar.gz/{current_jam_commit}",
            current_jam_sha,
            jam_url,
            jam_sha,
        ),
        report_jam_docker_pins(
            current_jam_docker_commit,
            current_jam_test_commit,
            latest_jam_docker,
        ),
    ]
    updates_needed = sum(1 for item in changed if item)

    if args.check:
        if updates_needed:
            print(f"[WARN] {updates_needed} external dependency update(s) available")
            return 1
        print("[INFO] External dependencies are up to date")
        return 0

    if updates_needed == 0:
        print("[INFO] No external dependency updates needed")
        return 0

    updated_manifest = manifest_text
    updated_manifest = replace_url_sha(
        LIBEVENT_RE,
        updated_manifest,
        latest_libevent_url,
        latest_libevent_sha,
        "libevent",
    )
    updated_manifest = replace_url_sha(
        TOR_RE, updated_manifest, latest_tor_url, latest_tor_sha, "tor"
    )
    updated_manifest = replace_url_sha(
        LIBSODIUM_RE,
        updated_manifest,
        latest_libsodium_url,
        latest_libsodium_sha,
        "libsodium",
    )
    updated_manifest = replace_url_sha(
        NEUTRINO_AMD64_RE,
        updated_manifest,
        latest_neutrino_amd64_url,
        latest_neutrino_amd64_sha,
        "neutrino-api (amd64)",
    )
    updated_manifest = replace_url_sha(
        NEUTRINO_ARM64_RE,
        updated_manifest,
        latest_neutrino_arm64_url,
        latest_neutrino_arm64_sha,
        "neutrino-api (arm64)",
    )
    updated_manifest = replace_jam_archive(updated_manifest, jam_commit, jam_sha)
    updated_compose = replace_jam_compose_pins(
        compose_text,
        latest_jam_ref,
        latest_jam_docker,
    )
    updated_jam_pin_test = replace_jam_test_commit(
        jam_pin_test_text,
        latest_jam_docker,
    )
    updated_jam_pin_test = replace_jam_test_ref(
        updated_jam_pin_test,
        latest_jam_ref,
    )

    manifest_path.write_text(updated_manifest, encoding="utf-8")
    compose_path.write_text(updated_compose, encoding="utf-8")
    jam_pin_test_path.write_text(updated_jam_pin_test, encoding="utf-8")
    print(f"[INFO] Applied {updates_needed} external dependency update(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        raise SystemExit(2)
