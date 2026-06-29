"""
Taker-side coordination for combined (pooled-change) channel-as-change.

Makers do not know which other makers are in a round, but the taker does, so the
taker pairs two combine-capable makers, designates one the channel *funder* and
the other the *folder*, and builds the CoinJoin with a single shared funding
output worth their pooled change (the funder opens one channel for the pooled
total and pushes the folder its own change). This module is the taker's pairing
decision; the actual shared output is built by ``build_coinjoin_tx`` using
:func:`combine_groups_from_pairs`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CombineMaker:
    """A maker's combine candidacy, as seen by the taker after !ioauth."""

    nick: str
    change: int  # the maker's computed change (taker-computed)
    combine_capable: bool
    ln_node: str | None  # 'node_id@host:port', if the maker offered combining


@dataclass(frozen=True)
class CombinePair:
    """A taker-chosen pair of makers sharing one combined funding output."""

    funder: str  # nick that opens the channel
    folder: str  # nick whose change folds into the funder's output
    funder_ln: str
    folder_ln: str
    funder_change: int
    folder_change: int

    @property
    def pooled_total(self) -> int:
        return self.funder_change + self.folder_change

    @property
    def push_to_folder_msat(self) -> int:
        """The funder pushes the folder its own change, so each keeps its balance."""
        return self.folder_change * 1000


def pair_combine_makers(makers: list[CombineMaker]) -> list[CombinePair]:
    """Pair combine-capable makers two at a time (an odd one out is left plain).

    Only makers that opted into combining *and* advertised a Lightning node are
    eligible. Pairing is order-stable; a leftover maker keeps an ordinary change
    output.
    """
    eligible = [m for m in makers if m.combine_capable and m.ln_node]
    pairs: list[CombinePair] = []
    for i in range(0, len(eligible) - 1, 2):
        funder, folder = eligible[i], eligible[i + 1]
        assert funder.ln_node is not None and folder.ln_node is not None
        pairs.append(
            CombinePair(
                funder=funder.nick,
                folder=folder.nick,
                funder_ln=funder.ln_node,
                folder_ln=folder.ln_node,
                funder_change=funder.change,
                folder_change=folder.change,
            )
        )
    return pairs


def combine_groups_from_pairs(pairs: list[CombinePair]) -> dict[str, str]:
    """Return the ``folder_nick -> funder_nick`` map ``build_coinjoin_tx`` expects."""
    return {p.folder: p.funder for p in pairs}


@dataclass(frozen=True)
class CombineRelay:
    """What the taker tells each combine maker so it can verify the shared output."""

    partner_pubkey: str  # the fellow maker's funding pubkey (hex)
    partner_change: int  # the fellow maker's change (so both can size the pool)
    shared_address: str  # the maker-to-maker 2-of-2 funding address


@dataclass(frozen=True)
class CombineSessionPlan:
    """A round's maker-to-maker combine plan.

    ``change_outputs`` and ``combine_groups`` feed ``build_coinjoin_tx``; ``relay``
    is sent per-maker on ``!tx`` so each can recompute and verify the shared 2-of-2.
    """

    change_outputs: dict[str, list[tuple[str, int]]]
    combine_groups: dict[str, str]
    relay: dict[str, CombineRelay]


def plan_combine_session(
    makers: list[tuple[str, str, int]], network: str = "mainnet"
) -> CombineSessionPlan:
    """Pair channel-capable makers into shared 2-of-2 outputs pooling their change.

    ``makers`` is ``(nick, cc_pubkey_hex, change_sats)``; a maker with an empty
    ``cc_pubkey_hex`` is not eligible. Eligible makers are paired two-at-a-time
    (order-stable); the first of each pair is the *funder* whose single
    ``change_outputs`` piece is the shared 2-of-2 address worth the *pooled* change,
    and the second is *folded* into it (no separate change output). An odd maker out
    keeps a plain change output. Both makers of a pair receive a :class:`CombineRelay`.
    """
    from jmswap.channel import combined_funding_address

    eligible = [(n, pk, ch) for (n, pk, ch) in makers if pk]
    change_outputs: dict[str, list[tuple[str, int]]] = {}
    combine_groups: dict[str, str] = {}
    relay: dict[str, CombineRelay] = {}
    for i in range(0, len(eligible) - 1, 2):
        (fn, fpk, fch), (dn, dpk, dch) = eligible[i], eligible[i + 1]
        shared = combined_funding_address([bytes.fromhex(fpk), bytes.fromhex(dpk)], network)
        pooled = fch + dch
        change_outputs[fn] = [(shared, pooled)]
        combine_groups[dn] = fn
        relay[fn] = CombineRelay(partner_pubkey=dpk, partner_change=dch, shared_address=shared)
        relay[dn] = CombineRelay(partner_pubkey=fpk, partner_change=fch, shared_address=shared)
    return CombineSessionPlan(
        change_outputs=change_outputs, combine_groups=combine_groups, relay=relay
    )
