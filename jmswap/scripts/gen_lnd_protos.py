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
LIGHTNING_PROTO_SHA256 = "9c0f9a8649312711bd849b82430bb5bcd17a3d85bb8c5627c3456214c2aa87b0"
LIGHTNING_PROTO_URL = (
    f"https://raw.githubusercontent.com/lightningnetwork/lnd/{LND_TAG}/lnrpc/lightning.proto"
)
ROUTER_PROTO_SHA256 = "7ab9fd77ee214183eecefe063a00093348518462ce4dc34682a3a831c33ed7fd"
ROUTER_PROTO_URL = (
    f"https://raw.githubusercontent.com/lightningnetwork/lnd/{LND_TAG}/lnrpc/routerrpc/router.proto"
)
INVOICES_PROTO_SHA256 = "1724f72aa2ed070c8978f5e8a40bc828a2859bbf5a56274ffa1577a65f3f05e7"
INVOICES_PROTO_URL = f"https://raw.githubusercontent.com/lightningnetwork/lnd/{LND_TAG}/lnrpc/invoicesrpc/invoices.proto"

ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "proto" / "lnd"
OUT_DIR = ROOT / "src" / "jmswap" / "lndrpc"


def _verified_proto(name: str, url: str, digest: str, refresh: bool) -> bytes:
    path = PROTO_DIR / f"{name}.proto"
    if refresh:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            data = response.read()
    else:
        data = path.read_bytes()
    actual_digest = hashlib.sha256(data).hexdigest()
    if actual_digest != digest:
        raise RuntimeError(f"{name}.proto SHA256 {actual_digest} != pinned {digest}")
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh", action="store_true", help="download the proto at the pinned LND tag"
    )
    args = parser.parse_args()

    PROTO_DIR.mkdir(parents=True, exist_ok=True)
    protos = (
        ("lightning", LIGHTNING_PROTO_URL, LIGHTNING_PROTO_SHA256),
        ("router", ROUTER_PROTO_URL, ROUTER_PROTO_SHA256),
        ("invoices", INVOICES_PROTO_URL, INVOICES_PROTO_SHA256),
    )
    for name, url, digest in protos:
        data = _verified_proto(name, url, digest, args.refresh)
        if args.refresh:
            (PROTO_DIR / f"{name}.proto").write_bytes(data)
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
            "router.proto",
            "invoices.proto",
            "channelescrow.proto",
        ],
        check=True,
    )
    for proto_name in ("lightning", "router", "invoices", "channelescrow"):
        grpc_stub = OUT_DIR / f"{proto_name}_pb2_grpc.py"
        grpc_stub.write_text(
            grpc_stub.read_text().replace(
                f"import {proto_name}_pb2 as {proto_name}__pb2",
                f"from . import {proto_name}_pb2 as {proto_name}__pb2",
            )
        )
    for proto_name in ("router", "invoices"):
        for suffix in ("_pb2.py", "_pb2.pyi", "_pb2_grpc.py"):
            generated_path = OUT_DIR / f"{proto_name}{suffix}"
            generated_path.write_text(
                generated_path.read_text()
                .replace(
                    "import lightning_pb2 as lightning__pb2",
                    "from . import lightning_pb2 as lightning__pb2",
                )
                .replace(
                    "import lightning_pb2 as _lightning_pb2",
                    "from . import lightning_pb2 as _lightning_pb2",
                )
            )
    for generated_path in (
        OUT_DIR / "channelescrow_pb2.py",
        OUT_DIR / "channelescrow_pb2_grpc.py",
    ):
        generated_path.write_text(f"# ruff: noqa\n{generated_path.read_text()}")
    print(f"generated {LND_TAG} stubs from pinned LND schemas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
