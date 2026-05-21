# Swap Input: Privacy Model and Failure Modes

## Goal

A taker that brings an extra UTXO obtained from a Lightning reverse submarine
swap into its CoinJoin transaction looks (on-chain) like a maker: it has an
input, a CoinJoin output, and a "fee" output sized like a real maker fee.

The trick is that the extra input is not the taker's "own" change, it is a
Lightning-funded UTXO. Without it, the taker is the only party with no
incoming fee, which is a strong distinguisher.

## What goes on-chain

Per round the taker:

1. Pre-selects `n` makers locally.
2. Pays two LN invoices to a swap provider (prepay miner fee + main hold
   invoice).
3. Detects the provider's lockup transaction trustlessly via its own
   Bitcoin backend.
4. Sends `!fill`, runs auth, and builds the CoinJoin transaction with
   `n + 1` swap-style inputs (the extra one is the lockup UTXO, spent via
   its HTLC claim path with the preimage).
5. Distributes the leftover sats so every "fee" output (real maker fees +
   the taker's fake fee) equals `max(maker_fee)` where possible. Any sats
   that cannot fully close a maker's gap to the target are still
   distributed as a partial top-up to the lowest-fee maker first; any
   remainder is added to the on-chain transaction fee. The fake fee
   itself is sampled by copying the fee of a real, non-selected
   orderbook offer (picked weighted by fidelity bond value), so it
   always matches an actual orderbook quote.

## What does NOT change

- `cj_amount` is fixed at `!fill` time and cannot be bumped.
- The swap input does not provide privacy by itself; it removes one specific
  fingerprint (asymmetric change) at the cost of introducing a Lightning side
  channel (see below).

## Lightning side channel (Kappos et al. 2021)

LN payments are not anonymous. An attacker observing the routing graph can
often deanonymize sender/receiver of a single-shot payment by combining
balance probing, channel topology and timing. For takers paying a swap
invoice, the practical implications are:

- The taker's LND node identity is correlated with the swap-claim
  transaction on-chain.
- Predictable invoice amounts and timing leak round metadata.

### Mitigations applied

- **MPP (multi-path payments).** The taker pays the main hold invoice via
  LND `SendPaymentV2` with `max_parts=4` so the routing observation is
  spread across multiple HTLCs. LND falls back to a single path if the
  graph offers no MPP route.
- **Nostr discovery over Tor.** Provider discovery and the swap RPC run
  through the same Tor SOCKS transport used by directory traffic, so
  provider selection itself does not leak the taker's IP.
- **Use a high-traffic, well-funded LN node.** A node that pays many LN
  invoices outside the JM context dilutes the correlation set.

The LND connection itself (REST or gRPC) is the operator's
responsibility. If LND runs remotely, operators should put it behind a
Tor hidden service or otherwise hide the network path between the taker
and their node; this is out of scope for the swap input feature.

### Mitigations recommended (not yet wired)

- BOLT12 offers / blinded paths (hides receiver, partially hides sender).
- `lnproxy`-style relays.
- Cashu mints with on-chain redemption from a third party.

## Failure handling

The CoinJoin can fail at three different points relative to the swap state:

### Pre-lockup failure

The taker's swap acquisition fails before the provider broadcasts the lockup.
No on-chain footprint, no LN payment in flight (or both invoices safely
cancelled by LND). The taker prompts the user:

- **retry** — re-run swap acquisition once.
- **plain** — fall back to a plain CoinJoin without the swap input. This is
  worse for privacy (the taker reverts to the asymmetric-change footprint)
  but still completes the round.
- **abort** — give up the round.

Non-interactive runs default to **abort** because silently downgrading to a
non-private flow violates the user's stated privacy preference.

### Post-lockup failure

The lockup is on-chain, the main hold invoice is in-flight, and the CoinJoin
fails (PoDLE rejection, signature fail, broadcast fail, network timeout).
The taker:

1. Cancels the in-flight LND payment task locally. The HTLC at the LN layer
   is then failed by LND when its CLTV expires (the preimage was never
   revealed, so neither side can claim).
2. Does NOT broadcast a CoinJoin spending the lockup. The provider self-
   refunds via the refund path of the HTLC at `timeout_block_height`.
3. **Forfeits the prepay invoice.** The miner-fee invoice was settled when
   the provider broadcast the lockup; the taker cannot reclaim those sats.
   This is the cost of cleanly aborting after lockup.

### Post-broadcast failure

If the CoinJoin transaction is on the wire but is later evicted (RBF race,
mempool full, etc.), the swap input has already been spent with a revealed
preimage; the provider can claim it. No special handling required.

## References

- Kappos et al., "An Empirical Analysis of Privacy in the Lightning Network",
  Financial Cryptography 2021.
- Boltz, "Reverse Submarine Swaps":
  https://docs.boltz.exchange/v/api/lifecycle#reverse-submarine-swaps
- BOLT12 offers and blinded paths:
  https://github.com/lightning/bolts/blob/master/12-offer-encoding.md
