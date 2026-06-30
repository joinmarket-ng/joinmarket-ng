#!/usr/bin/env python3
"""Generate the LND gRPC python stubs from the vendored lightning.proto.

Stubs land in ``jmswap/src/jmswap/lndrpc/`` as ``lightning_pb2.py`` and
``lightning_pb2_grpc.py``. The grpc stub's ``import lightning_pb2`` is rewritten
to a package-relative import so the stubs work when imported as
``jmswap.lndrpc``. Re-run this after bumping the vendored proto.

Usage: python jmswap/scripts/gen_lnd_protos.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # jmswap/
PROTO_DIR = ROOT / "proto" / "lnd"
OUT_DIR = ROOT / "src" / "jmswap" / "lndrpc"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "__init__.py").write_text(
        '"""Generated LND gRPC stubs (do not edit; see scripts/gen_lnd_protos.py)."""\n'
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={OUT_DIR}",
            f"--grpc_python_out={OUT_DIR}",
            f"--mypy_out={OUT_DIR}",  # typed .pyi stubs (mypy-protobuf)
            "lightning.proto",
        ],
        check=True,
    )
    # Rewrite the absolute import emitted by protoc into a package-relative one.
    grpc_stub = OUT_DIR / "lightning_pb2_grpc.py"
    text = grpc_stub.read_text()
    text = text.replace(
        "import lightning_pb2 as lightning__pb2",
        "from . import lightning_pb2 as lightning__pb2",
    )
    grpc_stub.write_text(text)
    print(f"generated stubs in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
