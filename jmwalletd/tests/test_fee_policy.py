"""Tests for jmwalletd.fee_policy (configset [POLICY] fee overrides).

Regression (issue #566): fee settings written by JAM via ``configset`` were
stored and echoed back by ``configget`` but never applied, so a sat/vB rate
chosen in the UI was ignored and the taker fell back to block-target
estimation, which fails on the neutrino backend.
"""

from __future__ import annotations

from jmwalletd.fee_policy import PolicyFeeOverrides, resolve_policy_fee_overrides


class TestResolvePolicyFeeOverrides:
    def test_empty_store_returns_no_overrides(self) -> None:
        assert resolve_policy_fee_overrides(None) == PolicyFeeOverrides()
        assert resolve_policy_fee_overrides({}) == PolicyFeeOverrides()
        assert resolve_policy_fee_overrides({"POLICY": {}}) == PolicyFeeOverrides()

    def test_other_sections_are_ignored(self) -> None:
        overrides = {"LOGGING": {"tx_fees": "5000"}}
        assert resolve_policy_fee_overrides(overrides) == PolicyFeeOverrides()

    def test_tx_fees_above_1000_is_sat_per_kvb_rate(self) -> None:
        result = resolve_policy_fee_overrides({"POLICY": {"tx_fees": "5000"}})
        assert result.fee_rate == 5.0
        assert result.block_target is None

    def test_tx_fees_1001_is_roughly_one_sat_vb(self) -> None:
        result = resolve_policy_fee_overrides({"POLICY": {"tx_fees": "1001"}})
        assert result.fee_rate == 1.001
        assert result.block_target is None

    def test_tx_fees_at_or_below_1000_is_block_target(self) -> None:
        result = resolve_policy_fee_overrides({"POLICY": {"tx_fees": "3"}})
        assert result.fee_rate is None
        assert result.block_target == 3

        boundary = resolve_policy_fee_overrides({"POLICY": {"tx_fees": "1000"}})
        assert boundary.fee_rate is None
        assert boundary.block_target == 1000

    def test_invalid_tx_fees_values_are_ignored(self) -> None:
        for bad in ("abc", "", "0", "-5", "1.5", "9" * 1000):
            result = resolve_policy_fee_overrides({"POLICY": {"tx_fees": bad}})
            assert result.fee_rate is None
            assert result.block_target is None

    def test_tx_fees_factor(self) -> None:
        result = resolve_policy_fee_overrides({"POLICY": {"tx_fees_factor": "0.3"}})
        assert result.tx_fee_factor == 0.3

    def test_tx_fees_factor_zero_disables_randomization(self) -> None:
        result = resolve_policy_fee_overrides({"POLICY": {"tx_fees_factor": "0"}})
        assert result.tx_fee_factor == 0.0

    def test_invalid_tx_fees_factor_is_ignored(self) -> None:
        for bad in ("nope", "-0.2", "nan", "inf"):
            result = resolve_policy_fee_overrides({"POLICY": {"tx_fees_factor": bad}})
            assert result.tx_fee_factor is None

    def test_max_cj_fee_overrides(self) -> None:
        result = resolve_policy_fee_overrides(
            {"POLICY": {"max_cj_fee_abs": "30000", "max_cj_fee_rel": "0.0003"}}
        )
        assert result.max_cj_fee_abs == 30000
        assert result.max_cj_fee_rel == "0.0003"

    def test_invalid_max_cj_fee_values_are_ignored(self) -> None:
        result = resolve_policy_fee_overrides(
            {"POLICY": {"max_cj_fee_abs": "lots", "max_cj_fee_rel": "-1"}}
        )
        assert result.max_cj_fee_abs is None
        assert result.max_cj_fee_rel is None

        for bad in ("nan", "inf"):
            result = resolve_policy_fee_overrides({"POLICY": {"max_cj_fee_rel": bad}})
            assert result.max_cj_fee_rel is None

    def test_combined_jam_fee_settings(self) -> None:
        """The full set JAM's fee modal writes in one save."""
        overrides = {
            "POLICY": {
                "tx_fees": "2500",
                "tx_fees_factor": "0.2",
                "max_cj_fee_abs": "10000",
                "max_cj_fee_rel": "0.001",
                "max_sweep_fee_change": "0.8",
            }
        }
        result = resolve_policy_fee_overrides(overrides)
        assert result == PolicyFeeOverrides(
            fee_rate=2.5,
            block_target=None,
            tx_fee_factor=0.2,
            max_cj_fee_abs=10000,
            max_cj_fee_rel="0.001",
        )
