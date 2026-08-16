#!/usr/bin/env python3
"""Generate the LND gRPC Python stubs from the vendored protocol schemas.

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
    schemas = ("lightning.proto", "channelescrow.proto")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{PROTO_DIR}",
            f"--python_out={OUT_DIR}",
            f"--grpc_python_out={OUT_DIR}",
            f"--mypy_out={OUT_DIR}",  # typed .pyi stubs (mypy-protobuf)
            *schemas,
        ],
        check=True,
    )
    # Rewrite absolute imports emitted by protoc into package-relative imports.
    for schema in schemas:
        grpc_stub = OUT_DIR / f"{Path(schema).stem}_pb2_grpc.py"
        module = f"{Path(schema).stem}_pb2"
        text = grpc_stub.read_text()
        text = text.replace(
            f"import {module} as {module.replace('_', '__')}",
            f"from . import {module} as {module.replace('_', '__')}",
        )
        grpc_stub.write_text(text)
    print(f"generated stubs in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
