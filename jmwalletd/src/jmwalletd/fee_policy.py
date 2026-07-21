"""Resolve reference-style ``[POLICY]`` fee overrides set through ``configset``.

JAM (and other clients written against the reference jmwalletd API) do not
send fee parameters with each request. Instead they write them to the
``[POLICY]`` config section via ``POST /configset`` and expect the daemon to
honor those values for subsequent operations (direct sends, coinjoins, and
tumbles). The daemon keeps them in ``DaemonState.config_overrides``; this
module translates them into the override arguments understood by our config
builders (issue #566: the values used to be stored and echoed back by
``configget`` but never applied, so a sat/vB fee set in JAM was silently
ignored and the taker fell back to block-target estimation, which fails on
the neutrino backend).

Reference semantics for ``tx_fees``:

- ``1 <= tx_fees <= 1000``: block confirmation target for backend fee
  estimation.
- ``tx_fees > 1000``: manual fee rate in satoshis per kilo-vbyte
  (sat/vB = tx_fees / 1000).

Values that fail to parse or are out of range are ignored with a warning so
a bad override degrades to the configured settings instead of breaking the
operation.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import NamedTuple

from loguru import logger

# Reference [POLICY] tx_fees threshold: above this the value is a fee rate in
# sat/kvB, at or below it is a block confirmation target.
TX_FEES_BLOCK_TARGET_MAX = 1000


class PolicyFeeOverrides(NamedTuple):
    """Fee-related overrides parsed from the ``[POLICY]`` config section.

    ``None`` fields mean "no override; use the configured settings value".
    ``fee_rate`` (sat/vB) and ``block_target`` are mutually exclusive by
    construction (both derive from ``tx_fees``).
    """

    fee_rate: float | None = None
    block_target: int | None = None
    tx_fee_factor: float | None = None
    max_cj_fee_abs: int | None = None
    max_cj_fee_rel: str | None = None


def resolve_policy_fee_overrides(
    config_overrides: Mapping[str, Mapping[str, str]] | None,
) -> PolicyFeeOverrides:
    """Parse fee overrides from the in-memory ``configset`` store.

    Accepts the ``DaemonState.config_overrides`` mapping (section -> field ->
    raw string value) and returns the subset of fee policy knobs that our
    taker/spend config builders understand.
    """
    if not config_overrides:
        return PolicyFeeOverrides()
    policy = config_overrides.get("POLICY")
    if not policy:
        return PolicyFeeOverrides()

    fee_rate, block_target = _parse_tx_fees(policy.get("tx_fees"))
    return PolicyFeeOverrides(
        fee_rate=fee_rate,
        block_target=block_target,
        tx_fee_factor=_parse_positive_float(policy.get("tx_fees_factor"), "tx_fees_factor"),
        max_cj_fee_abs=_parse_positive_int(policy.get("max_cj_fee_abs"), "max_cj_fee_abs"),
        max_cj_fee_rel=_parse_rel_fee(policy.get("max_cj_fee_rel")),
    )


def _parse_tx_fees(raw: str | None) -> tuple[float | None, int | None]:
    """Return ``(fee_rate_sat_vb, block_target)`` from a raw ``tx_fees`` value."""
    if raw is None:
        return None, None
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("Ignoring invalid [POLICY] tx_fees override: {!r}", raw)
        return None, None
    if value <= 0:
        logger.warning("Ignoring non-positive [POLICY] tx_fees override: {}", value)
        return None, None
    if value > TX_FEES_BLOCK_TARGET_MAX:
        # sat/kvB -> sat/vB (reference semantics).
        try:
            fee_rate = value / 1000.0
        except OverflowError:
            logger.warning("Ignoring out-of-range [POLICY] tx_fees override: {}", value)
            return None, None
        if not math.isfinite(fee_rate):
            logger.warning("Ignoring out-of-range [POLICY] tx_fees override: {}", value)
            return None, None
        return fee_rate, None
    return None, value


def _parse_positive_float(raw: str | None, field: str) -> float | None:
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except ValueError:
        logger.warning("Ignoring invalid [POLICY] {} override: {!r}", field, raw)
        return None
    if not math.isfinite(value) or value < 0:
        logger.warning("Ignoring out-of-range [POLICY] {} override: {}", field, value)
        return None
    return value


def _parse_positive_int(raw: str | None, field: str) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        logger.warning("Ignoring invalid [POLICY] {} override: {!r}", field, raw)
        return None
    if value <= 0:
        logger.warning("Ignoring non-positive [POLICY] {} override: {}", field, value)
        return None
    return value


def _parse_rel_fee(raw: str | None) -> str | None:
    """Validate a relative-fee override; returned as a string (decimal fraction)."""
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        value = float(text)
    except ValueError:
        logger.warning("Ignoring invalid [POLICY] max_cj_fee_rel override: {!r}", raw)
        return None
    if not math.isfinite(value) or value <= 0:
        logger.warning("Ignoring out-of-range [POLICY] max_cj_fee_rel override: {}", value)
        return None
    return text
