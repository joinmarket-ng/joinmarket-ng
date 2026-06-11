"""
Root pytest configuration for all JoinMarket NG tests.

This conftest.py provides global pytest options and hooks that apply to
all tests across the project.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from pytest import StashKey

# Define a StashKey for fail_on_skip option
_fail_on_skip_key: StashKey[bool] = StashKey[bool]()

# Proxy environment variables that jmcore's notification layer writes into
# ``os.environ`` (via ``Notifier._ensure_initialized`` when Tor is enabled).
# These are process-global and, if left set, make any later test that uses
# ``urllib`` (notably the directory-server CLI tests, which connect to a local
# mock HTTP server) fail with ``unknown url type: socks5h``. The autouse
# fixture below snapshots and restores them around every test so a notifier
# created in one component's tests cannot leak its proxy into another's.
_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


@pytest.fixture(autouse=True)
def _isolate_proxy_env_and_notifier() -> Iterator[None]:
    """Restore proxy env vars and the global notifier singleton around each test.

    The JoinMarket notifier is a process-global singleton (``jmcore.notifications``)
    and, when Tor is enabled, it sets ``HTTP_PROXY``/``HTTPS_PROXY`` in the
    environment. Without this isolation a Tor-configured notifier created by one
    test (or component) leaks both the singleton and the proxy env into
    unrelated tests, breaking ``urllib``-based tests in other components.
    """
    saved_env = {name: os.environ.get(name) for name in _PROXY_ENV_VARS}

    try:
        from jmcore.notifications import reset_notifier
    except Exception:  # pragma: no cover - jmcore always importable in tests
        reset_notifier = None  # type: ignore[assignment]

    if reset_notifier is not None:
        reset_notifier()

    yield

    if reset_notifier is not None:
        reset_notifier()

    for name, value in saved_env.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom pytest options available globally."""
    parser.addoption(
        "--fail-on-skip",
        action="store_true",
        default=False,
        help="Treat skipped tests as failures (for CI to catch missing setup)",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Store global options in config stash and set default environment variables."""
    config.stash[_fail_on_skip_key] = config.getoption("--fail-on-skip", default=False)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo[None],
) -> pytest.TestReport | None:
    """Convert skipped tests to failures when --fail-on-skip is enabled.

    This hook intercepts test reports and converts 'skipped' outcomes to 'failed'
    when the --fail-on-skip option is set. This is useful for CI pipelines to
    catch tests that are unexpectedly skipped due to missing setup conditions.

    The hook only affects tests that are actually skipped during execution
    (not deselected by markers like -m "not docker").
    """
    from _pytest.runner import pytest_runtest_makereport as orig_makereport

    # Get the original report
    report = orig_makereport(item, call)  # type: ignore[arg-type]

    # Check if --fail-on-skip is enabled
    fail_on_skip = item.config.stash.get(_fail_on_skip_key, False)

    # Convert skip to failure if enabled
    if fail_on_skip and report.skipped:
        # Get the skip reason
        if hasattr(report, "longrepr") and report.longrepr:
            if isinstance(report.longrepr, tuple) and len(report.longrepr) >= 3:
                skip_reason = report.longrepr[2]
            else:
                skip_reason = str(report.longrepr)
        else:
            skip_reason = "Unknown reason"

        # Convert to failure
        report.outcome = "failed"
        report.longrepr = (
            f"Test was skipped but --fail-on-skip is enabled: {skip_reason}"
        )

    return report  # type: ignore[return-value]
