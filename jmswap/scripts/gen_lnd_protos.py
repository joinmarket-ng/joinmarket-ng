#!/usr/bin/env python3
"""Generate the LND gRPC Python stubs from the vendored protocol schemas.

Stubs land in ``jmswap/src/jmswap/lndrpc/``. Imports emitted by protoc are
rewritten to package-relative imports so the stubs work under ``jmswap.lndrpc``.
Re-run this after bumping a vendored proto.

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
    schemas = ("lightning.proto", "channelescrow.proto", "router.proto")
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
    modules = tuple(Path(schema).stem for schema in schemas)
    for schema in schemas:
        generated_files = (
            OUT_DIR / f"{Path(schema).stem}_pb2.py",
            OUT_DIR / f"{Path(schema).stem}_pb2.pyi",
            OUT_DIR / f"{Path(schema).stem}_pb2_grpc.py",
        )
        for generated_file in generated_files:
            text = generated_file.read_text()
            for stem in modules:
                module = f"{stem}_pb2"
                aliases = (module.replace("_", "__"), f"_{module}")
                for alias in aliases:
                    text = text.replace(
                        f"import {module} as {alias}",
                        f"from . import {module} as {alias}",
                    )
            generated_file.write_text(text)
    print(f"generated stubs in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
