"""Resolve reference-style fee overrides from ``configset`` and the request.

JAM (and other clients written against the reference jmwalletd API) set the
fee policy globally rather than per request: they write it to the ``[POLICY]``
config section via ``POST /configset`` and expect the daemon to honor those
values for subsequent operations (direct sends, coinjoins, and tumbles). The
daemon keeps them in ``DaemonState.config_overrides``. A single direct-send or
coinjoin request may additionally carry a ``txfee`` of its own, which overrides
the global ``tx_fees`` for that one request. This module translates both into
the override arguments understood by our config builders (issue #566: the
configset values were stored and echoed back by ``configget`` but never
applied, so a sat/vB fee set in JAM was silently ignored and the taker fell
back to block-target estimation, which fails on the neutrino backend; issue
#636: a request ``txfee`` was accepted and then dropped the same way).

Reference semantics, shared by ``tx_fees`` and a request ``txfee``:

- ``1 <= value <= 1000``: block confirmation target for backend fee
  estimation.
- ``value > 1000``: manual fee rate in satoshis per kilo-vbyte
  (sat/vB = value / 1000).

A request ``txfee`` is never written back to the ``configset`` store, so it
cannot leak into later requests.

Configset values that fail to parse or are out of range are ignored with a
warning, so a bad global override degrades to the configured settings instead
of breaking the operation. A request ``txfee`` is bounded by the request
models instead and rejected there, so an explicit per-send fee is never
silently replaced by a different one.
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
    construction (both derive from a single ``tx_fees`` or request ``txfee``
    value).
    """

    fee_rate: float | None = None
    block_target: int | None = None
    tx_fee_factor: float | None = None
    max_cj_fee_abs: int | None = None
    max_cj_fee_rel: str | None = None
    max_sweep_fee_change: float | None = None


def resolve_policy_fee_overrides(
    config_overrides: Mapping[str, Mapping[str, str]] | None,
    *,
    request_tx_fee: int | None = None,
) -> PolicyFeeOverrides:
    """Parse fee overrides from the in-memory ``configset`` store.

    Accepts the ``DaemonState.config_overrides`` mapping (section -> field ->
    raw string value) and returns the subset of fee policy knobs that our
    taker/spend config builders understand.

    ``request_tx_fee`` is the optional ``txfee`` of a single direct-send or
    coinjoin request. A positive value replaces ``[POLICY] tx_fees`` for that
    request, and since it yields either a rate or a block target, it also
    clears whichever of the two the configset value would have set. ``None``
    or ``0`` keeps the configset value. Only the returned tuple is affected;
    ``config_overrides`` is never modified.
    """
    policy: Mapping[str, str] = (config_overrides or {}).get("POLICY") or {}

    fee_rate, block_target = _parse_tx_fees(policy.get("tx_fees"))
    if request_tx_fee:
        # Bounded by the request models, so it always converts; an unusable
        # value is rejected there instead of falling back to another fee.
        fee_rate, block_target = _split_tx_fee(request_tx_fee)

    return PolicyFeeOverrides(
        fee_rate=fee_rate,
        block_target=block_target,
        tx_fee_factor=_parse_positive_float(policy.get("tx_fees_factor"), "tx_fees_factor"),
        max_cj_fee_abs=_parse_positive_int(policy.get("max_cj_fee_abs"), "max_cj_fee_abs"),
        max_cj_fee_rel=_parse_rel_fee(policy.get("max_cj_fee_rel")),
        max_sweep_fee_change=_parse_positive_float(
            policy.get("max_sweep_fee_change"), "max_sweep_fee_change"
        ),
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
    try:
        return _split_tx_fee(value)
    except OverflowError:
        logger.warning("Ignoring out-of-range [POLICY] tx_fees override: {}", value)
        return None, None


def _split_tx_fee(value: int) -> tuple[float | None, int | None]:
    """Split a positive fee value into ``(fee_rate_sat_vb, block_target)``.

    Raises ``OverflowError`` for a value too large to express in sat/vB.
    """
    if value > TX_FEES_BLOCK_TARGET_MAX:
        # sat/kvB -> sat/vB (reference semantics).
        return value / 1000.0, None
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
