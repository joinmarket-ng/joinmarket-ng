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

### Amount verification (anti-shorting)

The provider controls both the on-chain amount it promises and the amount it
actually locks, while the taker pays a fixed LN invoice. If the locked output
were smaller than expected, the CoinJoin would still balance because the taker
silently tops up the shortfall from its own UTXOs, so the loss would go
unnoticed. Two checks prevent this:

- Before paying, the promised `onchainAmount` is bounded by the configured
  `max_swap_fee_pct` (plus the provider's mining fee) relative to the invoice
  amount; a provider that promises less is rejected with no funds committed.
- At lockup detection, a UTXO is only accepted if its value is at least the
  promised on-chain amount; a short lockup is ignored and the round aborts
  into the post-lockup path (cancel payment, forfeit prepay).

### Pre-lockup failure

The taker's swap acquisition fails before the provider broadcasts the lockup.
No on-chain footprint. If the failure happens after the main hold-invoice
payment task was started (for example the prepay payment raised), that task is
cancelled so no HTLC is orphaned; LND fails any in-flight HTLC at CLTV expiry.
The taker prompts the user:

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

The cancellation runs in a `finally` block around the whole CoinJoin, so it
fires on every non-success exit (fill, auth, build, signing, broadcast, or
an unexpected exception), not just the acquisition phase. On success the
payment is left running so the provider can settle the hold invoice once the
broadcast CoinJoin reveals the preimage on-chain.

### Hold-invoice payment timeout

The main payment is a hold invoice: it only settles after the taker reveals
the preimage by broadcasting the CoinJoin, which the provider observes when
the lockup UTXO is spent. Lockup confirmation plus CoinJoin negotiation
routinely exceeds a minute on mainnet, so the in-flight payment must use a
long client-side timeout (`hold_invoice_timeout`, default 3600s). A short
timeout would make LND cancel the HTLC before settlement, forfeiting the
provider's lockup. The prepay (miner-fee) invoice keeps a short timeout since
it settles as soon as both HTLCs arrive at the provider.


### Post-broadcast failure

If the CoinJoin transaction is on the wire but is later evicted (RBF race,
mempool full, etc.), the swap input has already been spent with a revealed
preimage. Two sub-cases matter:

- The CoinJoin (or a conflicting spend of the same lockup) eventually confirms.
  The provider settles the hold invoice from the on-chain preimage; nothing is
  owed and recovery marks the record resolved.
- The CoinJoin never confirms but the preimage is already public. The provider
  can settle the hold invoice and, after `timeout_block_height`, refund the
  lockup. To avoid racing that refund, the taker reclaims the still-unspent
  lockup via the recovery flow below (the unilateral claim path is always
  available while the output is unspent).

To keep this window from ever opening close to the refund height, the taker
re-checks the current height immediately before broadcasting. If fewer than
`BROADCAST_LOCKTIME_SAFETY_MARGIN` blocks remain before `timeout_block_height`,
it aborts the broadcast, keeps the preimage secret, and reclaims the lockup
instead, trading the swap fee for the principal.

## Fund recovery and persistence

A reverse swap locks real on-chain funds before the CoinJoin spends them. Any
crash, abort, or never-confirming CoinJoin between lockup and confirmation must
never strand those funds. Recovery is built on two facts:

- All swap key material (claim key and preimage) is derived deterministically
  from the wallet seed via a per-swap BIP-85 index. Given the index, the wallet
  alone can rebuild the witness and sign the claim; the taker never holds a raw
  private key.
- Each lockup is written to an encrypted per-wallet record the moment it is
  detected, so recovery does not depend on in-memory state surviving a crash.

### Recovery store

Records live under `<data_dir>/swaps/<wallet_fingerprint>/<swap_id>.swap`, one
file per swap. Each file is encrypted with a key derived from the wallet seed
(PBKDF2-HMAC-SHA256, 600k iterations, per-file random salt, Fernet). A record
holds only the BIP-85 `swap_index`, the HTLC script, the lockup outpoint, the
refund height, and a status; it never stores a private key or preimage. Because
the encryption key comes from the seed, the store is portable: the same seed on
another machine can decrypt and act on the records (the design mirrors Boltz's
seed-derived rescue file, but keeps the per-swap index local for privacy).

Wallets without an on-disk `data_dir` (ephemeral/in-memory) skip persistence;
their lockups remain claimable from the seed but are not auto-reconciled.

### Reconciliation outcomes

`swap-recover` (and the in-process watcher) scans every non-terminal record and
reconciles it against the chain:

- Lockup gone, CoinJoin txid known and seen: marked **resolved** (CoinJoin won).
- Lockup gone, no CoinJoin txid: marked **refunded** (provider self-refunded).
- Lockup unspent, our CoinJoin still visible in the mempool: held as
  **pending** so we never double-spend our own in-flight CoinJoin (override with
  `--force`).
- Lockup unspent, no CoinJoin in flight: swept by a unilateral claim to a fresh
  wallet address and marked **recovered**.
- Lockup below the dust threshold: left unswept (not economical to claim).

On a light client without mempool access, the in-flight check cannot be proven,
so the taker holds off claiming unless `--force` is passed; the startup sweep
still runs but the periodic watcher does not.

### Operator usage

- Automatic: on taker startup a one-shot sweep runs, and when the backend has
  mempool access a background watcher re-checks every
  `SWAP_RECOVERY_POLL_INTERVAL` seconds.
- Manual: `jm-taker swap-recover` loads the wallet, reconciles records, and
  sweeps claimable lockups without connecting to any directory server. Use
  `--dry-run` to preview and `--force` to claim despite an apparent in-flight
  CoinJoin.

## References

- Kappos et al., "An Empirical Analysis of Privacy in the Lightning Network",
  Financial Cryptography 2021. https://arxiv.org/abs/2003.12470
- Boltz, "Reverse Submarine Swaps":
  https://docs.boltz.exchange/v/api/lifecycle#reverse-submarine-swaps
- BOLT12 offers and blinded paths:
  https://github.com/lightning/bolts/blob/master/12-offer-encoding.md
