"""The pinned upstream LND release that managed nodes run.

Digests come from the release manifest ``manifest-v0.21.4-beta.txt``, whose
signature by Olaoluwa Osuntokun (key A5B6 1896 952D 9FDA 83BC 054C DC42 612E
8923 7182) was verified when they were pinned. Updating the release means
verifying a new manifest the same way and replacing every digest here.
"""

from __future__ import annotations

import hashlib
import io
import platform
import sys
import tarfile
from pathlib import Path

LND_VERSION = "v0.21.4-beta"
_RELEASE_URL = "https://github.com/lightningnetwork/lnd/releases/download"

# platform -> (archive digest, lnd digest, lncli digest)
_DIGESTS: dict[str, tuple[str, str, str]] = {
    "linux-amd64": (
        "beffe4949a5e46410d90c0e6d020a1b685468440b0004fc4d59740a6291178fb",
        "f19efdbde7cbd449d8b3e67bf3e69d0fdc7e6fabed7bed7599829d201c5aa1e8",
        "7d53b3ab15b7f080c9698a668db7e3169438b9857301ecca5ed9137b3635ec1f",
    ),
    "linux-arm64": (
        "bc5fc872bf12002e1a1ea42b16a13e2f836c2e2439817f336a323bddf680b708",
        "2858e13b56ea67b6f69b23a60ded43d5226f3c3fe2e9132179dc9da484cb8b31",
        "d6b9117598e4e894499542cebf7cfcc7fe00dbd891d9e180a1b127c550b995bf",
    ),
    "darwin-amd64": (
        "9676ad611e904aa6c2a0d7752bd371e525d7d6b6307305b5423d721700dd9261",
        "314b1b3345c733bcc489c2a48d35a764bb1dc7cb16197691edd63418f9aa65dd",
        "7b80f9bb58e5a5677e709e86dff41592e1424d4a4c45dca1983f56ac1b50caf0",
    ),
    "darwin-arm64": (
        "68893592bbd1ecc18eaf4911a2206da3e1fcd1b716f575b087f9270cd3829e03",
        "a10871099a1f739ff71d6c5507755f0dac256f17aaa8369fd274198a1ac1f544",
        "31d7d632c3d15af103f2ea2b716285a46ceca23b2c785a56f12f551ea830a46c",
    ),
}
_MACHINES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


class LndReleaseError(Exception):
    """The release cannot be installed or failed verification."""


def current_platform() -> str:
    system = {"linux": "linux", "darwin": "darwin"}.get(sys.platform)
    machine = _MACHINES.get(platform.machine().lower())
    name = f"{system}-{machine}"
    if system is None or machine is None or name not in _DIGESTS:
        raise LndReleaseError(f"no pinned LND build for {sys.platform}/{platform.machine()}")
    return name


def install_dir(data_dir: Path) -> Path:
    return data_dir / "lightning" / "bin" / f"lnd-{LND_VERSION}"


def default_lnd_binary(data_dir: Path) -> Path:
    return install_dir(data_dir) / "lnd"


def archive_url(platform_name: str) -> str:
    return f"{_RELEASE_URL}/{LND_VERSION}/lnd-{platform_name}-{LND_VERSION}.tar.gz"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_verified(archive: bytes, platform_name: str, destination: Path) -> Path:
    """Verify the archive and both binaries against the pinned digests, then install."""
    archive_digest, lnd_digest, lncli_digest = _DIGESTS[platform_name]
    if _sha256(archive) != archive_digest:
        raise LndReleaseError("LND archive digest does not match the pinned release")
    prefix = f"lnd-{platform_name}-{LND_VERSION}"
    binaries: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for name, digest in (("lnd", lnd_digest), ("lncli", lncli_digest)):
            member = tar.getmember(f"{prefix}/{name}")
            handle = tar.extractfile(member) if member.isfile() else None
            if handle is None:
                raise LndReleaseError(f"{name} is missing from the LND archive")
            data = handle.read()
            if _sha256(data) != digest:
                raise LndReleaseError(f"{name} digest does not match the pinned release")
            binaries[name] = data
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, data in binaries.items():
        temporary = destination / f".{name}.tmp"
        temporary.write_bytes(data)
        temporary.chmod(0o700)
        temporary.replace(destination / name)
    return destination / "lnd"


def is_installed(binary: Path) -> bool:
    """True when ``binary`` is the pinned lnd for this platform."""
    try:
        return _sha256(binary.read_bytes()) == _DIGESTS[current_platform()][1]
    except (OSError, LndReleaseError):
        return False


def download_and_install(data_dir: Path, *, proxy: str | None = None) -> Path:
    """Download the pinned release from GitHub and install it under ``data_dir``."""
    import httpx

    platform_name = current_platform()
    binary = default_lnd_binary(data_dir)
    if is_installed(binary):
        return binary
    with httpx.Client(follow_redirects=True, timeout=300.0, proxy=proxy) as client:
        response = client.get(archive_url(platform_name))
        response.raise_for_status()
    return extract_verified(response.content, platform_name, install_dir(data_dir))
