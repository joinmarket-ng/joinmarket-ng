"""Tests for the HWI bond signing script helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sign_bond_psbt import (  # noqa: E402
    outdated_hwi_hint,
    parse_hwi_version,
)


class TestParseHwiVersion:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("3.1.0", (3, 1, 0)),
            ("2.4.0", (2, 4, 0)),
            ("3.1.0.dev1", (3, 1, 0)),
            (" 3.2.1 ", (3, 2, 1)),
            ("10.0.5", (10, 0, 5)),
        ],
    )
    def test_valid_versions(self, version: str, expected: tuple[int, int, int]) -> None:
        assert parse_hwi_version(version) == expected

    @pytest.mark.parametrize("version", ["", "abc", "3.1", "v3.1.0", "3"])
    def test_invalid_versions(self, version: str) -> None:
        assert parse_hwi_version(version) is None


class TestOutdatedHwiHint:
    def test_no_hint_for_recent_versions(self) -> None:
        assert outdated_hwi_hint("3.1.0") is None
        assert outdated_hwi_hint("3.2.0") is None
        assert outdated_hwi_hint("4.0.0") is None

    @pytest.mark.parametrize("version", ["3.0.0", "2.4.0", "2.1.1", "1.2.1"])
    def test_hint_for_old_versions(self, version: str) -> None:
        hint = outdated_hwi_hint(version)
        assert hint is not None
        assert version in hint
        assert "3.1.0" in hint
        assert "pip install -U hwi" in hint
        # The hint must name the affected newer devices (see issue #552).
        assert "Ledger Stax/Flex" in hint

    def test_hint_for_unknown_version(self) -> None:
        hint = outdated_hwi_hint(None)
        assert hint is not None
        assert "pip install -U hwi" in hint

    def test_hint_for_unparseable_version(self) -> None:
        hint = outdated_hwi_hint("weird")
        assert hint is not None
        assert "pip install -U hwi" in hint
