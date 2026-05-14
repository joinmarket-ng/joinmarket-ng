"""Tests for jmcore.process_hardening."""

from __future__ import annotations

import contextlib
import resource
import sys

import pytest

from jmcore import process_hardening


class TestDisableCoreDumps:
    def test_sets_rlimit_core_to_zero(self):
        """_disable_core_dumps lowers RLIMIT_CORE to (0, 0)."""
        original = resource.getrlimit(resource.RLIMIT_CORE)
        try:
            ok = process_hardening._disable_core_dumps()
            assert ok is True
            soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
            assert soft == 0
            assert hard == 0
        finally:
            # Best-effort restore for any later test (may fail if hard was
            # lowered, which is fine; pytest processes are short-lived).
            with contextlib.suppress(OSError, ValueError):
                resource.setrlimit(resource.RLIMIT_CORE, original)

    def test_returns_false_when_setrlimit_raises(self, monkeypatch):
        """Failure to lower RLIMIT_CORE is reported, not raised."""

        def boom(*args, **kwargs):
            raise OSError("simulated")

        monkeypatch.setattr(resource, "setrlimit", boom)
        assert process_hardening._disable_core_dumps() is False


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")
class TestSetUndumpableLinux:
    def test_prctl_succeeds_on_linux(self):
        """On Linux, prctl(PR_SET_DUMPABLE, 0) returns True."""
        # This actually sets the flag on the test process; that is fine
        # since it only weakens introspection from non-root peers in the
        # same user namespace and pytest processes are short-lived.
        assert process_hardening._set_undumpable_linux() is True


class TestHardenCurrentProcess:
    def test_calls_both_mitigations(self, monkeypatch):
        called = {"rlim": 0, "prctl": 0}

        def fake_rlim():
            called["rlim"] += 1
            return True

        def fake_prctl():
            called["prctl"] += 1
            return True

        monkeypatch.setattr(process_hardening, "_disable_core_dumps", fake_rlim)
        monkeypatch.setattr(process_hardening, "_set_undumpable_linux", fake_prctl)
        monkeypatch.delenv("JOINMARKET_DISABLE_PROCESS_HARDENING", raising=False)
        process_hardening.harden_current_process()
        assert called["rlim"] == 1
        assert called["prctl"] == 1

    def test_escape_hatch_env_var_skips_mitigations(self, monkeypatch):
        """JOINMARKET_DISABLE_PROCESS_HARDENING=1 skips everything."""
        called = {"rlim": 0, "prctl": 0}

        def fake_rlim():
            called["rlim"] += 1
            return True

        def fake_prctl():
            called["prctl"] += 1
            return True

        monkeypatch.setattr(process_hardening, "_disable_core_dumps", fake_rlim)
        monkeypatch.setattr(process_hardening, "_set_undumpable_linux", fake_prctl)
        monkeypatch.setenv("JOINMARKET_DISABLE_PROCESS_HARDENING", "1")
        process_hardening.harden_current_process()
        assert called["rlim"] == 0
        assert called["prctl"] == 0

    def test_non_fatal_when_mitigations_fail(self, monkeypatch):
        """harden_current_process returns cleanly even if both mitigations fail."""
        monkeypatch.setattr(process_hardening, "_disable_core_dumps", lambda: False)
        monkeypatch.setattr(process_hardening, "_set_undumpable_linux", lambda: False)
        monkeypatch.delenv("JOINMARKET_DISABLE_PROCESS_HARDENING", raising=False)
        # Must not raise.
        process_hardening.harden_current_process()
