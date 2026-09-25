#!/usr/bin/env python3
"""Explicitly build the pinned MuSig2-capable native library into a supplied prefix."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import tarfile
import urllib.request
from pathlib import Path

# Upstream libsecp256k1 v0.8.0. Verify the archive before extracting or building it.
COMMIT = "6e2c8bc4ecdc6e71dbe7a368f360d8d453ce435d"
ARCHIVE_SHA256 = "3fe9fd705f4fdf2fe90d6e04b6c1fedd7e8f244a119315886f6468f52c2dfc33"
ARCHIVE_URL = f"https://codeload.github.com/bitcoin-core/secp256k1/tar.gz/{COMMIT}"


def build_library(prefix: Path, build_dir: Path) -> None:
    """Build only when explicitly invoked; never escalate privileges or change loader settings."""
    prefix = prefix.resolve()
    build_dir = build_dir.resolve()
    build_dir.mkdir(parents=True, exist_ok=True)
    archive = build_dir / f"secp256k1-{COMMIT}.tar.gz"
    if not archive.exists():
        with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
            raise ValueError("libsecp256k1 archive SHA-256 mismatch; refusing to build")
        archive.write_bytes(data)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise ValueError("libsecp256k1 archive SHA-256 mismatch; refusing to build")
    with tarfile.open(archive) as source_archive:
        source_archive.extractall(build_dir, filter="data")

    source = build_dir / f"secp256k1-{COMMIT}"
    output = build_dir / "build"
    subprocess.run(
        [
            "cmake",
            "-S",
            str(source),
            "-B",
            str(output),
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
            "-DCMAKE_INSTALL_LIBDIR=lib",
            "-DBUILD_SHARED_LIBS=ON",
            "-DSECP256K1_ENABLE_MODULE_RECOVERY=ON",
            "-DSECP256K1_ENABLE_MODULE_ECDH=ON",
            "-DSECP256K1_ENABLE_MODULE_EXTRAKEYS=ON",
            "-DSECP256K1_ENABLE_MODULE_SCHNORRSIG=ON",
            "-DSECP256K1_ENABLE_MODULE_MUSIG=ON",
            "-DSECP256K1_BUILD_TESTS=OFF",
            "-DSECP256K1_BUILD_BENCHMARK=OFF",
            "-DSECP256K1_BUILD_EXHAUSTIVE_TESTS=OFF",
            "-DSECP256K1_BUILD_CTIME_TESTS=OFF",
        ],
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(output), "--config", "Release", "-j", "2"], check=True
    )
    subprocess.run(
        ["cmake", "--install", str(output), "--config", "Release"], check=True
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--build-dir", required=True, type=Path)
    args = parser.parse_args()
    build_library(args.prefix, args.build_dir)


if __name__ == "__main__":
    main()
