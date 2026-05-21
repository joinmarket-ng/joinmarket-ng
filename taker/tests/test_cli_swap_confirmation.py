"""Tests for swap-specific confirmation info formatting in taker CLI."""

from __future__ import annotations

from taker.cli import _build_confirmation_additional_info


class TestSwapConfirmationFormatting:
    def test_includes_swap_fee_when_present(self) -> None:
        info = _build_confirmation_additional_info(
            maker_details=[],
            fee_rate=None,
            mixdepth=1,
            swap_info={
                "provider_fee_pct": 0.5,
                "provider_mining_fee": 1500,
                "swap_fee": 2345,
                "actual_swap_amount": 100_000,
                "padded": False,
            },
        )

        assert info["Source Mixdepth"] == 1
        assert info["Swap Provider Fee"] == "0.5% + 1500 sats"
        assert info["Swap Fee"] == "2,345 sats"
        assert info["Swap Amount"] == "100,000 sats"

    def test_omits_swap_fee_when_not_integer(self) -> None:
        info = _build_confirmation_additional_info(
            maker_details=[],
            fee_rate=None,
            mixdepth=2,
            swap_info={
                "provider_fee_pct": 0.5,
                "provider_mining_fee": 1500,
                "swap_fee": "unknown",
            },
        )

        assert info["Source Mixdepth"] == 2
        assert "Swap Fee" not in info
