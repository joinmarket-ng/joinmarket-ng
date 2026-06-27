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
