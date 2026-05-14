"""Best-effort process hardening for daemons that hold sensitive material.

JoinMarket daemons (jmwalletd, maker, taker) keep secrets in memory:
seed mnemonics, BIP32 extended keys, derived private keys, NaCl encryption
keys, and signed PSBTs. Two cheap, OS-level mitigations meaningfully shrink
the blast radius if the host is compromised or misconfigured:

* ``RLIMIT_CORE = 0`` disables core dumps for this process. Without it,
  any crash (segfault, abort, OOM kill on some configurations) can spill
  the full address space to disk via core(5) or systemd-coredump(8).
* ``prctl(PR_SET_DUMPABLE, 0)`` on Linux prevents ``/proc/$pid/mem`` and
  ``ptrace`` from non-privileged peers in the same user namespace, and
  also disables core dumps independently of RLIMIT_CORE. It is the
  defense-in-depth pair for the rlimit setting.

Neither protects against:

* root-on-host (which can re-enable dumpable, raise the rlimit, or just
  read ``/proc/$pid/mem`` directly), or
* swap-to-disk of anonymous pages (mitigated by encrypted swap, see
  ``docs/technical/security.md``), or
* hibernation images.

Call ``harden_current_process()`` once at daemon start, as early as
possible after argument parsing and before any wallet or key material
is loaded. The function is intentionally non-fatal: on platforms or in
sandboxes where these calls are not available, it logs a debug message
and returns. Tests can monkeypatch the underlying syscalls.
"""

from __future__ import annotations

import os
import sys

from loguru import logger


def _disable_core_dumps() -> bool:
    """Set RLIMIT_CORE to (0, 0); return True on success."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows/exotic builds
        logger.debug("resource module unavailable; cannot disable core dumps")
        return False
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError) as exc:
        logger.debug("could not set RLIMIT_CORE=0: {}", exc)
        return False
    return True


def _set_undumpable_linux() -> bool:
    """Linux: prctl(PR_SET_DUMPABLE, 0); return True on success."""
    if sys.platform != "linux":
        return False
    try:
        import ctypes
        import ctypes.util
    except ImportError:  # pragma: no cover
        return False
    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        logger.debug("libc not found; cannot prctl(PR_SET_DUMPABLE, 0)")
        return False
    try:
        libc = ctypes.CDLL(libc_name, use_errno=True)
    except OSError as exc:  # pragma: no cover
        logger.debug("cannot load libc: {}", exc)
        return False
    # PR_SET_DUMPABLE == 4 on Linux (see <sys/prctl.h>).
    pr_set_dumpable = 4
    rc = libc.prctl(pr_set_dumpable, 0, 0, 0, 0)
    if rc != 0:
        errno = ctypes.get_errno()
        logger.debug("prctl(PR_SET_DUMPABLE, 0) failed: errno={}", errno)
        return False
    return True


def harden_current_process() -> None:
    """Apply best-effort process hardening for daemons holding secrets.

    Idempotent and non-fatal. Safe to call from any daemon entry point
    before sensitive state is loaded.
    """
    if os.environ.get("JOINMARKET_DISABLE_PROCESS_HARDENING") == "1":
        logger.warning("process hardening disabled via JOINMARKET_DISABLE_PROCESS_HARDENING=1")
        return
    rlim_ok = _disable_core_dumps()
    prctl_ok = _set_undumpable_linux()
    logger.debug(
        "process hardening: RLIMIT_CORE={} PR_SET_DUMPABLE={}",
        "off" if rlim_ok else "skip",
        "off" if prctl_ok else "skip",
    )
