#!/usr/bin/env python3
"""Update Flatpak manifest source versions and checksums."""

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


USER_AGENT = "joinmarket-ng-flatpak-updater/1.0"

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
JAM_COMMIT_RE = re.compile(r"(?ms)(- name: jam-frontend\b.*?commit:\s*)([a-f0-9]+)")


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


def latest_tor_version() -> str:
    html = fetch_text("https://dist.torproject.org/")
    versions = set(re.findall(r"tor-(0\.4\.\d+\.\d+)\.tar\.gz", html))
    if not versions:
        raise UpdateError("Could not find Tor versions on dist.torproject.org")
    return max(
        versions, key=lambda version: tuple(int(part) for part in version.split("."))
    )


def latest_jam_commit() -> str:
    output = subprocess.check_output(
        [
            "git",
            "ls-remote",
            "https://github.com/joinmarket-webui/jam.git",
            "refs/heads/v2",
        ],
        text=True,
    ).strip()
    if not output:
        raise UpdateError("Could not fetch latest JAM v2 commit")
    commit = output.split()[0]
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise UpdateError(f"Unexpected JAM commit format: {commit}")
    return commit


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


def extract_jam_commit(text: str) -> str:
    match = JAM_COMMIT_RE.search(text)
    if not match:
        raise UpdateError("Could not find jam-frontend commit in Flatpak manifest")
    return match.group(2)


def replace_jam_commit(text: str, commit: str) -> str:
    def _replacement(match: re.Match[str]) -> str:
        return f"{match.group(1)}{commit}"

    updated, count = JAM_COMMIT_RE.subn(_replacement, text, count=1)
    if count != 1:
        raise UpdateError("Failed to update jam-frontend commit in Flatpak manifest")
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


def report_commit(name: str, current: str, latest: str) -> bool:
    changed = current != latest
    if changed:
        print(f"[UPDATE] {name}")
        print(f"  Commit: {current}")
        print(f"  New:    {latest}")
    else:
        print(f"[OK] {name} is up to date")
    return changed


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    default_manifest_path = project_root / "flatpak" / "org.joinmarketng.JamNG.yml"

    parser = argparse.ArgumentParser(
        description="Update Flatpak manifest dependency versions and hashes"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check for updates without modifying files",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=default_manifest_path,
        help="Path to Flatpak manifest (default: flatpak/org.joinmarketng.JamNG.yml)",
    )
    args = parser.parse_args()

    manifest_path = args.manifest
    if not manifest_path.is_file():
        raise UpdateError(f"Flatpak manifest not found: {manifest_path}")

    manifest_text = manifest_path.read_text(encoding="utf-8")

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
    current_jam_commit = extract_jam_commit(manifest_text)

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
    latest_libsodium_url = pick_asset_url(
        libsodium_release,
        lambda name: (
            re.fullmatch(r"libsodium-\d+\.\d+\.\d+\.tar\.gz", name) is not None
        ),
        "libsodium source tarball",
    )
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

    latest_commit = latest_jam_commit()

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
        report_commit("jam-frontend", current_jam_commit, latest_commit),
    ]
    updates_needed = sum(1 for item in changed if item)

    if args.check:
        if updates_needed:
            print(f"[WARN] {updates_needed} Flatpak dependency update(s) available")
            return 1
        print("[INFO] Flatpak dependencies are up to date")
        return 0

    if updates_needed == 0:
        print("[INFO] No Flatpak dependency updates needed")
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
    updated_manifest = replace_jam_commit(updated_manifest, latest_commit)

    manifest_path.write_text(updated_manifest, encoding="utf-8")
    print(f"[INFO] Applied {updates_needed} Flatpak dependency update(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except UpdateError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        raise SystemExit(2)
