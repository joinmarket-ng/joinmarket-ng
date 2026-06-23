from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
JMWALLETD_DOCKERFILE = REPO_ROOT / "jmwalletd" / "Dockerfile"


def test_jam_builder_uses_native_build_platform() -> None:
    """The JAM frontend stage must build on the native build host.

    Pinning the jam-builder stage to a fixed cross-architecture platform
    (for example ``linux/amd64``) forces the Node.js toolchain to run under
    QEMU emulation on a native arm64 builder, where it segfaults (exit code
    139). The frontend output is platform-independent static JS, so the stage
    is built on ``BUILDPLATFORM`` instead. This is a regression guard for the
    ARM64 CI build failure.
    """
    content = JMWALLETD_DOCKERFILE.read_text()

    # The default must resolve to the native build host platform, never a
    # hard-coded cross-architecture platform.
    arg_match = re.search(
        r"^ARG\s+JAM_BUILDER_PLATFORM=(?P<value>\S+)\s*$",
        content,
        re.MULTILINE,
    )
    assert arg_match is not None, "JAM_BUILDER_PLATFORM ARG not found"
    assert arg_match.group("value") == "${BUILDPLATFORM}", (
        "JAM_BUILDER_PLATFORM must default to ${BUILDPLATFORM} so the JAM "
        "frontend builds natively and avoids QEMU emulation (segfault on "
        f"native arm64 builders); found {arg_match.group('value')!r}"
    )

    # The jam-builder stage must consume the ARG rather than re-pinning a
    # platform inline.
    from_match = re.search(
        r"^FROM\s+--platform=(?P<value>\S+)\s+node:.*AS jam-builder\s*$",
        content,
        re.MULTILINE,
    )
    assert from_match is not None, "jam-builder FROM line not found"
    assert from_match.group("value") == "${JAM_BUILDER_PLATFORM}", (
        "jam-builder must build for ${JAM_BUILDER_PLATFORM}; found "
        f"{from_match.group('value')!r}"
    )
