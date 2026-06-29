# Bidirectional Lightning swap provider

A JoinMarket maker's coins are linked across its CoinJoin slots by structural
edges that no fee policy hides: the change peel chain, the common-input co-spend,
and the reused equal-output forward edge. Being a **bidirectional Lightning swap
provider** is a deployable, self-financing partial fix: a swap client requests
*their* amount, so clearing swaps from the maker's existing wallet displaces the
prior-slot coins that would otherwise chain, fragmenting the maker's slots into
many effective wallets while earning swap fees. The full analysis is in
`papers/maker-swap-provider.md`; this page documents the implementation
(`jmswap`).

## Protocol

Each swap locks funds in a single Taproot (P2TR) output. The internal key is the
**MuSig2** aggregate of the provider and client keys, so a cooperative
settlement is an ordinary BIP340 key-path spend, indistinguishable from any
other P2TR spend (the privacy-preserving happy path). The output's script tree
adds two trustless fallbacks: a preimage **claim** leaf and an absolute-timelock
**refund** leaf. The leaf scripts are byte-for-byte compatible with Boltz v2.

The provider is always the swap "server" and never pays a swap fee:

- **Swap-in** (provider receives on-chain, pays Lightning) is a submarine swap:
  the client funds the HTLC and is paid over Lightning; the provider claims the
  HTLC with the preimage learned from settling that payment.
- **Swap-out** (provider pays on-chain, receives Lightning) is a reverse swap:
  the provider issues a hold invoice, funds the HTLC to the client's claim key
  once the invoice is paid, watches the client's on-chain claim for the revealed
  preimage, then settles the hold invoice.
- **Whole-coin third role**: the provider absorbs the client's whole UTXO at the
  base fee with no client change, the simplest on-chain shape that severs the
  peel chain (it concentrates linkage onto the provider's own wallet, which it
  manages separately).
- **Channel-as-change** (experimental, off by default): the maker can emit its
  CoinJoin change as a Taproot channel-funding output (a simple-taproot 2-of-2),
  so the change has no chaining successor in the wallet. Today both funding keys
  are derived from the maker's seed, so the output is tracked on-chain and
  cooperatively swept back with ``jm-maker swap-channel-sweep`` (an ordinary
  P2TR key-path spend, validated against bitcoind). Opening a *real Lightning
  channel* whose funding output is the CoinJoin output is also supported: a
  forked LDK Server (built from the patches in ``ldk_server/patches/``) exposes
  externally-funded channel opens via LDK's
  ``unsafe_manual_funding_transaction_generated``, driven by
  ``jmswap.request_external_channel_funding`` /
  ``complete_external_channel_funding`` (the maker uses the returned funding
  address as its CoinJoin change output, then supplies the resulting outpoint).
  This is validated live: a channel opens with its funding output supplied by an
  external transaction. The model is **taker-driven and needs no maker-to-maker
  coordination**: makers do not know which other makers are in a round, so a
  maker simply channelizes *its own* change. It opens an externally-funded
  channel to an LN peer of its own choosing for its (deterministically computed)
  change amount and hands the taker the channel's funding address as an ordinary
  change address; the taker builds and broadcasts the CoinJoin as usual, and the
  maker supplies the resulting outpoint once it has co-signed the transaction.
  The exact-change-amount bookkeeping in the live CoinJoin is implemented and
  validated end to end (a maker opens a real channel from its CoinJoin change in
  a real maker<->taker round).

A **combined** variant pools several makers' change into one shared funding
output. Because makers do not know each other, this is also taker-coordinated:
the taker (which knows all participants) pairs two combine-capable makers,
relays each one's funding pubkey to the other, and builds the CoinJoin with a
single shared output (a MuSig2 2-of-2 of the two makers, worth the pooled
change) instead of two separate change outputs. The funder opens an
externally-funded channel for the pooled total and **pushes** the peer maker its
own change, so each maker keeps its balance behind one on-chain output (no
per-maker change to subset-sum against, and the counterparty stays in-family).
The on-chain primitive (`combined_funding_output`), the push economics
(`plan_combined_channel`) and the in-family cooperative close are implemented and
the combined channel-open-with-push is validated live; the taker-side pairing
(`taker.combine.pair_combine_makers`) and the shared-output construction
(`build_coinjoin_tx`'s `combine_groups`, which folds the folder maker's change
into the funder's single output) are implemented and unit-tested. The remaining
work is the protocol-message round that carries the pairing at runtime: a maker
advertising combine-capability and its Lightning node, and the extra taker->funder
"open a channel to the folder and push it its change" exchange (after `!ioauth`,
once the taker knows the change amounts) that yields the funder's funding
address.

### Channel-as-change is maker-to-maker only

A channel funding output reveals its outpoint to the co-signing counterparty, so
opening a channel-as-change to an **external** peer would hand the maker's
funding<->wallet link to an outside observer: it is no privacy improvement and is
never used. A valid channel-as-change always joins two CoinJoin participants; a
maker that cannot be paired falls back to a plain change output.

### Knapsack pairing (taker-arranged, beats the fixed pool)

A rigid pool of two makers' *exact* change defeats the per-output change-match,
but a whole-round subset-sum solver that hypothesises group-funded shared outputs
can uniquely invert a fixed pool whenever the pool size relates simply to the
participant count (the `coinjoin-simulator` `maker-swap-provider` study measures
the fixed pool attributed ~1.0 at 4 makers and ~0.94 at 6, an even/odd sawtooth).
The robust construction is **knapsack** pairing (`taker.knapsack.plan_knapsack`):
the taker pools *every* change-bearing maker and splits each change into
grid-quantized pieces that share one round-wide grid value, emitted as
*overlapping* maker-to-maker 2-of-2 outputs. Because many makers emit an
equal-valued grid piece, a grid piece of one maker is interchangeable with a grid
piece of another without changing any output value or any maker's total, so the
whole-round partition is combinatorially ambiguous (low-density subset-sum
hardness); the study drives the maximal attacker's attribution to ~0 at *every*
set size, where the fixed pool fails, at the cost of ~2 channel opens per maker
(two pieces, the minimum that leaves no single output equal to the residual). An
**allowance** of makers may open no channel at all: their plain change is diffused
into the same grid pool, so on chain they are indistinguishable from the channel
makers (their plain pieces still chain across slots, the honest limit). The
planner is implemented and unit-tested (`taker/tests/test_knapsack.py`); like the
combined variant its piece *amounts* feed `build_coinjoin_tx`'s per-maker
`change_outputs` and the maker-to-maker 2-of-2 *addresses* come from the channel
layer, with the runtime protocol-message round (relaying each piece's pairing) the
remaining wiring.

### On-chain-only takers: sweep under homogeneous fees

Once makers are this hard to cluster, a pure on-chain taker (no Lightning, never a
maker) regains anonymity by **sweeping** its denomination coin
mixdepth-to-mixdepth (no change, no co-spend) under homogeneous fees: its only
cross-round edge is the ambiguous reused-equal-output, so a matching attacker
links none of its tumble hops. Leaving change re-introduces the taker's own change
peel chain and CIOH, and distinct per-maker fees expose it by fee-fingerprint
elimination even when sweeping. The taker should therefore sweep and prefer a
uniform-fee market (the `maker-swap-provider` study Section 4.10).

### Why Boltz v2 (not papa-swap)

A more on-chain-efficient design exists (supertestnet's *papa-swap*: one funding
transaction plus pre-signed spends, roughly halving footprint and latency). We
chose the Boltz v2 HTLC instead for the first implementation because: it is a
**deployed standard** (Boltz, Electrum, many wallets), so a maker can clear
swaps against the existing market; its claim/refund are **signed at spend time**,
so fees track the market and can be RBF-bumped (papa-swap pre-signs spends at
fixed fees, which is risky on the timelock-bounded refund path); and its
**MuSig2 2-of-2 cooperative close** reuses exactly the same Taproot/MuSig2
machinery as the channel-as-change funding output, rather than papa-swap's
separate pre-signed tx0-tx3 state machine (with its two race conditions and a
two-week recovery window). papa-swap's single-transaction efficiency is an
attractive future optimization, but Boltz v2 is the better v1 trade-off.

Discovery and signalling run over **Nostr**: the provider advertises its terms
as an addressable event and answers swap RPCs sent as NIP-04 encrypted DMs; the
client discovers providers and calls those RPCs. The client always recomputes
the swap address from the parameters it agreed to before funding, so the
provider cannot pin a swap to a script the client did not accept.

## Operating it

Set `[maker.swap_provider]` in the config (see `config.toml.template`) to enable
the role, choose directions, fee terms, and the Nostr relays, and point it at an
**LDK Server** Lightning node for the invoices and payments. The `swap`
docker-compose profile brings up a Nostr relay and two LDK Server nodes
(`ldk-provider`, `ldk-client`) on regtest for end-to-end testing. The library
itself (`jmswap`) is standalone and depends only on `jmcore`.

## Privacy fidelity (what is actually achieved)

A privacy audit of the code against the design surfaced four points; three are
now addressed and one remains a deployment discipline:

- **Cooperative key-path swap claim (addressed for both directions).** Both swap
  directions now settle with a 2-of-2 MuSig2 key-path spend (an ordinary P2TR
  signature, no preimage or tapscript on-chain), falling back to the script path
  only if the counterparty does not cooperate. For swap-out the client claims and
  the provider co-signs (`coopclaim`); for swap-in the provider receives the coins
  and chooses the destination, the client co-signs and broadcasts on the
  provider's behalf (`coopclaim_in`) after the provider has paid the invoice. The
  swap-out path is validated live; both fall back trustlessly to the script path.
- **Split / combined / knapsack channel-as-change (addressed for the per-output
  change-match; whole-round hardened by knapsack).** A maker can emit its change as
  several funding outputs (`build_coinjoin_tx`'s `change_outputs`) so no single
  output equals its exact residual, and two makers can pool their change into one
  shared output; both defeat the per-output change-match. A fixed pool is still
  invertible by the whole-round (CoinJoin-Sudoku) solver at set-divisible sizes, so
  the robust construction is the taker-arranged **knapsack** pairing
  (`taker.knapsack.plan_knapsack`): grid-quantized overlapping maker-to-maker pieces
  that keep the whole-round partition combinatorially ambiguous at every set size,
  with an allowance of non-channel makers hidden in the same pool. Channel-as-change
  is maker-to-maker only (an external-peer channel is never used).
- **On-chain fidelity-bond verification (addressed).** Providers advertise a real
  timelocked bond and clients verify it on-chain (`jmswap.verify_provider_bond` /
  `select_bonded_providers`): a sybil that merely sets `bond_value` is filtered
  out.
- **The self-2-of-2 channel-change recovery sweep re-links the funds** (it spends
  the funding output back into the wallet). The privacy holds only if the output
  is used as a *real* channel and cooperatively closed (a decorrelated balance);
  the sweep is a recovery tool, not the privacy path.

What *is* achieved as implemented: the cross-slot peel-chain break (the deployable
~100-effective-wallets win), per-swap key isolation, a swap-in coin whose only
on-chain parent is the funder's transaction (a 1-in/1-out claim, no co-spend),
combined funding that defeats the single-owner partition, and cooperative
sweeps/closes that are themselves ordinary P2TR key-path spends.

## Limitations

The break is structural and on-chain, not perfection. A force close re-exposes
the channel/HTLC script on chain; the swap counterparty and network-level
observers can correlate the Lightning legs; and timing can associate the swap,
fill and settlement. The robust protections are a decentralised many-provider
market, cooperative closes, and batching/decoys (the `ln-subgraph` direction).
Bitcoin's transparency makes a perfect maker-anonymity guarantee impossible; the
goal is to raise the analyst's cost substantially with deployable steps.
