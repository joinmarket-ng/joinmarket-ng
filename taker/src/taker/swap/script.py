"""Backwards-compatible re-export of the swap HTLC script primitives.

The swap HTLC script is a shared Bitcoin protocol primitive and now lives in
:mod:`jmcore.swap_script` so that both the taker (which acquires swap inputs)
and the wallet (which owns the swap claim keys) can use it without depending on
each other. This module re-exports the public names for backwards
compatibility with existing ``taker.swap.script`` imports.
"""

from __future__ import annotations

from jmcore.swap_script import (
    MAX_LOCKTIME_DELTA,
    MIN_LOCKTIME_DELTA,
    SwapScript,
    _push_data,
    _push_int,
)

__all__ = [
    "MAX_LOCKTIME_DELTA",
    "MIN_LOCKTIME_DELTA",
    "SwapScript",
    "_push_data",
    "_push_int",
]
