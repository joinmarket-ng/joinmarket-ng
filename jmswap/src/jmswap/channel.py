"""
Channel-as-change: emit CoinJoin change as Taproot channel-funding outputs.

`papers/maker-swap-provider.md` (Sections 2, 4.3, 4.7) proposes severing the
slot-level peel chain by emitting a maker's CoinJoin change as a **Taproot
channel-funding output** rather than an ordinary change output: on-chain it is an
ordinary P2TR output with no chaining successor in the maker's wallet, and a
cooperatively closed *used* channel returns a decorrelated balance.

This module implements the on-chain primitives:

* a **simple-taproot channel funding output** keyed by the MuSig2 aggregate of
  the two channel peers' funding keys (key-path only; the commitment-tx script
  complexity lives off-chain), so the funding output and a cooperative close are
  ordinary P2TR key-path spends;
* sum-preserving **split** of one change amount across several funding outputs,
  and **combine** of several makers' change into one shared funding output, both
  designed so the on-chain values do not betray the partition while the peers can
  still verify it off chain (Sections 4.3, 4.7).

The full Lightning dual-funding negotiation (who contributes which change, the
PSBT funding shim, simple-taproot channel state) is the open design item the
paper flags; this module provides the on-chain shapes and their cooperative
spend, validated as correct Taproot/MuSig2.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from random import Random

from jmcore.bitcoin import tagged_hash
from jmcore.musig2 import (
    get_session,
    key_agg,
    nonce_agg,
    nonce_gen,
    partial_sig_agg,
    sign_partial,
)
from jmcore.taproot import (
    TaprootTx,
    TxIn,
    TxOut,
    taproot_output_script,
    taproot_sighash,
)

from jmswap.spend import SwapOutpoint


def sorted_funding_keys(key_a: bytes, key_b: bytes) -> list[bytes]:
    """Return the two 33-byte funding keys in canonical (sorted) order."""
    for key in (key_a, key_b):
        if len(key) != 33 or key[0] not in (0x02, 0x03):
            raise ValueError("funding keys must be 33-byte compressed")
    return sorted([key_a, key_b])


def channel_internal_xonly(key_a: bytes, key_b: bytes) -> bytes:
    """MuSig2 aggregate internal x-only key for a 2-of-2 channel funding output."""
    return key_agg(sorted_funding_keys(key_a, key_b)).aggregate_xonly()


def channel_funding_output(key_a: bytes, key_b: bytes) -> tuple[bytes, bytes]:
    """Return ``(scriptpubkey, internal_xonly)`` for a simple-taproot channel.

    The output is a key-path-only P2TR (no script tree) keyed by the MuSig2
    aggregate of the sorted funding keys, BIP341-tweaked with an empty merkle
    root (the standard key-spend-only shape).
    """
    internal = channel_internal_xonly(key_a, key_b)
    return taproot_output_script(internal, None), internal


def channel_funding_address(key_a: bytes, key_b: bytes, network: str = "mainnet") -> str:
    from jmcore.bitcoin import scriptpubkey_to_address

    spk, _ = channel_funding_output(key_a, key_b)
    return scriptpubkey_to_address(spk, network)


def _channel_taptweak(internal_xonly: bytes) -> bytes:
    """The BIP341 TapTweak scalar for a key-spend-only output (empty merkle root)."""
    return tagged_hash("TapTweak", internal_xonly)


def cooperative_close(
    key_a: bytes,
    key_b: bytes,
    funding: SwapOutpoint,
    settlement_outputs: list[TxOut],
    privkey_a: bytes,
    privkey_b: bytes,
    *,
    locktime: int = 0,
) -> TaprootTx:
    """Cooperatively close a channel: a 2-of-2 MuSig2 key-path spend.

    Produces a 1-in / N-out P2TR transaction (an ordinary key-path spend) paying
    the settled balances. The interactive flow is the same MuSig2 primitives
    split across the two peers; both keys are taken here for tests/tooling.
    """
    keys = sorted_funding_keys(key_a, key_b)
    priv_for = {
        key_a: privkey_a,
        key_b: privkey_b,
    }
    internal = key_agg(keys).aggregate_xonly()
    tx = TaprootTx(
        inputs=[TxIn(txid=funding.txid, vout=funding.vout)],
        outputs=settlement_outputs,
        locktime=locktime,
    )
    sighash = taproot_sighash(tx, 0, [funding.value], [funding.scriptpubkey])
    sn1, pn1 = nonce_gen(keys[0])
    sn2, pn2 = nonce_gen(keys[1])
    aggnonce = nonce_agg([pn1, pn2])
    session = get_session(aggnonce, keys, [_channel_taptweak(internal)], sighash)
    ps1 = sign_partial(sn1, priv_for[keys[0]], session)
    ps2 = sign_partial(sn2, priv_for[keys[1]], session)
    sig = partial_sig_agg([ps1, ps2], session)
    tx.witnesses = [[sig]]
    return tx


# --- Sum-preserving change splitting / combining ----------------------------


@dataclass(frozen=True)
class ChangeSplit:
    """A sum-preserving split of one change amount across funding outputs."""

    total: int
    amounts: tuple[int, ...]

    def verify(self) -> bool:
        return sum(self.amounts) == self.total and all(a > 0 for a in self.amounts)


def split_change(
    total: int, parts: int, *, dust: int = 330, rng: Random | None = None
) -> ChangeSplit:
    """Split ``total`` into ``parts`` randomized, sum-preserving amounts.

    Each part is at least ``dust``. The randomization (sum-preserving) hides the
    partition in the on-chain values while peers who know the inputs can still
    verify it off chain (paper Section 2, 4.3).
    """
    if parts < 1:
        raise ValueError("parts must be >= 1")
    if total < parts * dust:
        raise ValueError("total too small to split above the dust threshold")
    if parts == 1:
        return ChangeSplit(total=total, amounts=(total,))
    # Distribute the spendable surplus above the per-part dust floor at random.
    surplus = total - parts * dust
    # `parts - 1` random cut points partition the surplus.
    randbelow: Callable[[int], int] = secrets.randbelow if rng is None else rng.randrange
    cuts = sorted(randbelow(surplus + 1) for _ in range(parts - 1))
    prev = 0
    amounts: list[int] = []
    for cut in (*cuts, surplus):
        amounts.append(dust + (cut - prev))
        prev = cut
    return ChangeSplit(total=total, amounts=tuple(amounts))


@dataclass(frozen=True)
class CombinedFunding:
    """A channel funding output pooled from several makers' change residuals."""

    residuals: tuple[int, ...]  # each maker's residual contribution
    fee: int = 0

    @property
    def total(self) -> int:
        """The single on-chain funding amount (sum of residuals minus shared fee)."""
        return sum(self.residuals) - self.fee

    def verify(self, observed_amount: int) -> bool:
        """A peer that knows the residuals verifies the shared output's value."""
        return observed_amount == self.total and self.total > 0


# --- Combined (multi-maker) channel funding --------------------------------


def combined_funding_output(pubkeys: list[bytes]) -> tuple[bytes, bytes]:
    """Return ``(scriptpubkey, internal_xonly)`` for an N-of-N shared output.

    A single key-path-only P2TR keyed by the MuSig2 aggregate of all makers'
    (sorted) funding keys, used as the *combined* channel funding output pooling
    several makers' change (paper Section 4.7). With two makers this is the
    in-family dual-funded channel (the counterparty is a fellow maker).
    """
    if len(pubkeys) < 2:
        raise ValueError("combined funding needs at least two makers")
    for key in pubkeys:
        if len(key) != 33 or key[0] not in (0x02, 0x03):
            raise ValueError("funding keys must be 33-byte compressed")
    internal = key_agg(sorted(pubkeys)).aggregate_xonly()
    return taproot_output_script(internal, None), internal


def combined_funding_address(pubkeys: list[bytes], network: str = "mainnet") -> str:
    """Bech32m address of the combined (maker-to-maker) shared 2-of-2 funding output."""
    from jmcore.bitcoin import scriptpubkey_to_address

    spk, _ = combined_funding_output(pubkeys)
    return scriptpubkey_to_address(spk, network)


@dataclass(frozen=True)
class CombinedChannelPlan:
    """A taker-computed plan for a combined (pooled-change) channel.

    The funder opens a single channel for the pooled total and *pushes* every
    other maker its own change, so each maker keeps its balance while a single
    shared funding output appears on-chain (no per-maker change to subset-sum
    against). Makers never talk to each other: the taker pairs them and relays
    the funding pubkeys and amounts.
    """

    funder_pubkey: bytes
    peer_pubkey: bytes  # the channel counterparty (a fellow maker)
    contributions: tuple[tuple[bytes, int], ...]  # (maker_pubkey, change) for all makers
    fee: int = 0

    @property
    def total(self) -> int:
        return sum(change for _, change in self.contributions) - self.fee

    @property
    def push_to_peer_msat(self) -> int:
        """Amount the funder pushes to the (single) peer: the peer's own change."""
        for pubkey, change in self.contributions:
            if pubkey == self.peer_pubkey:
                return change * 1000
        raise ValueError("peer is not among the contributions")

    def funder_balance(self) -> int:
        """The funder's retained balance (its own change)."""
        for pubkey, change in self.contributions:
            if pubkey == self.funder_pubkey:
                return change
        raise ValueError("funder is not among the contributions")


def plan_combined_channel(
    contributions: list[tuple[bytes, int]], fee: int = 0
) -> CombinedChannelPlan:
    """Pair two makers' change into one channel (the taker's coordination step).

    Picks the first maker as the funder and the second as the in-family channel
    peer; the funder opens a channel for the pooled total and pushes the peer its
    change. Currently supports pools of two (the paper's set-divisible point);
    larger pools are an N-party interactive-funding extension.
    """
    if len(contributions) != 2:
        raise ValueError("combined channel currently pools exactly two makers")
    funder_pubkey = contributions[0][0]
    peer_pubkey = contributions[1][0]
    return CombinedChannelPlan(
        funder_pubkey=funder_pubkey,
        peer_pubkey=peer_pubkey,
        contributions=tuple(contributions),
        fee=fee,
    )
