#!/usr/bin/env python3
"""Verify or refresh the pinned LND proto and regenerate Python gRPC stubs."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import urllib.request
from pathlib import Path

LND_TAG = "v0.21.1-beta"
PROTO_SHA256 = "9c0f9a8649312711bd849b82430bb5bcd17a3d85bb8c5627c3456214c2aa87b0"
PROTO_URL = (
    f"https://raw.githubusercontent.com/lightningnetwork/lnd/{LND_TAG}/lnrpc/lightning.proto"
)

ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "proto" / "lnd"
PROTO_PATH = PROTO_DIR / "lightning.proto"
OUT_DIR = ROOT / "src" / "jmswap" / "lndrpc"


def _verified_proto(refresh: bool) -> bytes:
    if refresh:
        with urllib.request.urlopen(PROTO_URL, timeout=30) as response:  # noqa: S310
            data = response.read()
    else:
        data = PROTO_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != PROTO_SHA256:
        raise RuntimeError(f"lightning.proto SHA256 {digest} != pinned {PROTO_SHA256}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh", action="store_true", help="download the proto at the pinned LND tag"
    )
    args = parser.parse_args()

    data = _verified_proto(args.refresh)
    PROTO_DIR.mkdir(parents=True, exist_ok=True)
    if args.refresh:
        PROTO_PATH.write_bytes(data)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "__init__.py").write_text(
        '"""Generated LND v0.21.1 gRPC stubs. See scripts/gen_lnd_protos.py."""\n'
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={OUT_DIR}",
            f"--grpc_python_out={OUT_DIR}",
            f"--mypy_out={OUT_DIR}",
            "lightning.proto",
        ],
        check=True,
    )
    grpc_stub = OUT_DIR / "lightning_pb2_grpc.py"
    grpc_stub.write_text(
        grpc_stub.read_text().replace(
            "import lightning_pb2 as lightning__pb2",
            "from . import lightning_pb2 as lightning__pb2",
        )
    )
    print(f"generated {LND_TAG} stubs from SHA256 {PROTO_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
