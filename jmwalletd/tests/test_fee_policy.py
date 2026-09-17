"""Tests for jmwalletd.fee_policy (configset [POLICY] fee overrides).

Regression (issue #566): fee settings written by JAM via ``configset`` were
stored and echoed back by ``configget`` but never applied, so a sat/vB rate
chosen in the UI was ignored and the taker fell back to block-target
estimation, which fails on the neutrino backend.
"""

from __future__ import annotations

from jmwalletd.fee_policy import PolicyFeeOverrides, resolve_policy_fee_overrides
from jmwalletd.models import MAX_MONEY_SATS


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

    def test_max_sweep_fee_change_override(self) -> None:
        result = resolve_policy_fee_overrides({"POLICY": {"max_sweep_fee_change": "0.5"}})
        assert result.max_sweep_fee_change == 0.5

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
            max_sweep_fee_change=0.8,
        )


class TestRequestTxFeeOverride:
    """Regression (issue #636): the ``txfee`` of a single direct-send or
    coinjoin request (the fee picked on JAM's Send page) must replace
    ``[POLICY] tx_fees`` for that request instead of being dropped."""

    def test_request_rate_without_configset(self) -> None:
        result = resolve_policy_fee_overrides(None, request_tx_fee=25000)
        assert result == PolicyFeeOverrides(fee_rate=25.0, block_target=None)

    def test_request_block_target_without_configset(self) -> None:
        result = resolve_policy_fee_overrides({}, request_tx_fee=2)
        assert result == PolicyFeeOverrides(fee_rate=None, block_target=2)

    def test_request_rate_clears_configset_block_target(self) -> None:
        result = resolve_policy_fee_overrides({"POLICY": {"tx_fees": "6"}}, request_tx_fee=40000)
        assert result.fee_rate == 40.0
        assert result.block_target is None

    def test_request_block_target_clears_configset_rate(self) -> None:
        result = resolve_policy_fee_overrides({"POLICY": {"tx_fees": "5000"}}, request_tx_fee=2)
        assert result.fee_rate is None
        assert result.block_target == 2

    def test_request_uses_the_same_threshold_as_tx_fees(self) -> None:
        boundary = resolve_policy_fee_overrides(None, request_tx_fee=1000)
        assert boundary.fee_rate is None
        assert boundary.block_target == 1000

        above = resolve_policy_fee_overrides(None, request_tx_fee=1001)
        assert above.fee_rate == 1.001
        assert above.block_target is None

    def test_request_only_replaces_tx_fees(self) -> None:
        """The rest of the JAM fee settings still come from configset."""
        overrides = {
            "POLICY": {
                "tx_fees": "2500",
                "tx_fees_factor": "0.2",
                "max_cj_fee_abs": "10000",
                "max_cj_fee_rel": "0.001",
                "max_sweep_fee_change": "0.8",
            }
        }
        result = resolve_policy_fee_overrides(overrides, request_tx_fee=3)
        assert result == PolicyFeeOverrides(
            fee_rate=None,
            block_target=3,
            tx_fee_factor=0.2,
            max_cj_fee_abs=10000,
            max_cj_fee_rel="0.001",
            max_sweep_fee_change=0.8,
        )

    def test_omitted_or_zero_request_keeps_configset(self) -> None:
        for request_tx_fee in (None, 0):
            rate = resolve_policy_fee_overrides(
                {"POLICY": {"tx_fees": "5000"}}, request_tx_fee=request_tx_fee
            )
            assert rate.fee_rate == 5.0
            assert rate.block_target is None

            target = resolve_policy_fee_overrides(
                {"POLICY": {"tx_fees": "6"}}, request_tx_fee=request_tx_fee
            )
            assert target.fee_rate is None
            assert target.block_target == 6

            assert (
                resolve_policy_fee_overrides(None, request_tx_fee=request_tx_fee)
                == PolicyFeeOverrides()
            )

    def test_largest_accepted_request_value_still_wins(self) -> None:
        # The request models cap ``txfee`` at the money supply and reject
        # anything larger (see test_models.py), so every value that reaches
        # here converts instead of quietly falling back to the configset fee.
        result = resolve_policy_fee_overrides(
            {"POLICY": {"tx_fees": "5000"}}, request_tx_fee=MAX_MONEY_SATS
        )
        assert result.fee_rate == MAX_MONEY_SATS / 1000.0
        assert result.block_target is None

    def test_request_does_not_modify_configset_store(self) -> None:
        overrides = {"POLICY": {"tx_fees": "5000", "tx_fees_factor": "0.3"}}
        resolve_policy_fee_overrides(overrides, request_tx_fee=2)
        assert overrides == {"POLICY": {"tx_fees": "5000", "tx_fees_factor": "0.3"}}
        assert resolve_policy_fee_overrides(overrides).fee_rate == 5.0
