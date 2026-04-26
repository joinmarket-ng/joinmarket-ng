"""
Tumbler-specific overrides for makers spawned during a plan.

The tumbler runs the maker bot between taker phases (``MakerSessionPhase``)
to diversify the role/timing of the funded wallet's CoinJoin participation.
Two policies must be enforced for *those* maker sessions, regardless of how
the user has configured the standalone ``maker`` bot:

1. **Zero absolute fee, sw0absoffer.** The session is short-lived and
   bondless (see #2), so the offer would otherwise be ignored by takers
   filtering on fees and bonds. A 0-sat absolute offer is the cheapest
   way to be picked. Absolute offers only advertise/use ``cjfee_a``;
   ``cj_fee_relative`` is irrelevant in this mode.

2. **No fidelity bond.** Reusing a long-term bond from the funded wallet
   would link every tumbler maker session (and therefore the inputs they
   spend) to the same identity across phases, defeating the point of the
   tumble. The session re-announces under a fresh nick anyway, but the
   bond itself is the strongest cross-phase fingerprint and must be
   suppressed explicitly.

Multi-offer (``offer_configs``) is also cleared so the absolute-fee policy
is not silently overridden by ``MakerConfig.get_effective_offers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jmcore.models import OfferType

if TYPE_CHECKING:
    from maker.config import MakerConfig


def apply_tumbler_maker_policy(config: MakerConfig) -> MakerConfig:
    """Force the tumbler-specific maker offer/bond policy onto ``config``.

    Mutates and returns ``config`` for convenience. The function is
    idempotent: re-applying it has no effect.
    """
    config.offer_type = OfferType.SW0_ABSOLUTE
    config.cj_fee_absolute = 0
    # Absolute offers ignore cj_fee_relative, but pin it to a harmless
    # default instead of carrying through an operator-specific value into
    # a tumbler-controlled session and then mentioning that value in docs.
    config.cj_fee_relative = "0.001"
    config.no_fidelity_bond = True
    # Multi-offer takes precedence over the single-offer fields when
    # non-empty (see ``MakerConfig.get_effective_offers``); a non-empty
    # list would silently re-introduce the user's relative-fee or
    # non-zero-absolute offers under tumbler control.
    config.offer_configs = []
    return config
