"""Regression tests for wallet CLI process hardening."""

from __future__ import annotations

from unittest.mock import patch

from jmwallet.cli import main


def test_main_hardens_before_dispatching_cli() -> None:
    calls: list[str] = []

    with (
        patch(
            "jmcore.process_hardening.harden_current_process",
            side_effect=lambda: calls.append("harden"),
        ),
        patch("jmwallet.cli.app", side_effect=lambda: calls.append("app")),
    ):
        main()

    assert calls == ["harden", "app"]
