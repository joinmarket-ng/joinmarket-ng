# Privacy

For practical guidance, start with [privacy practices](best-practices.md).
[Collaborative Transaction Privacy](https://gist.github.com/nothingmuch/d84ba390d89b5b08897af2d95009c2a1)
examines broader transaction-graph attacks and what privacy claims need to establish.

## Mixdepths

Mixdepths are isolated wallet accounts. Inputs for one CoinJoin come from one
mixdepth, equal CoinJoin outputs move to the next mixdepth, and change remains
in the source mixdepth. This separation prevents the wallet from immediately
merging an equal output with its linkable change.

An `INTERNAL` destination uses the next mixdepth and wraps from the final
mixdepth to mixdepth 0. Explicit destinations are used as provided and may be in
another wallet.

Makers can choose how to select a source when several mixdepths can fill the
same request. The default `balanced` policy spends from the largest eligible
balance. This keeps all configured compartments meaningfully active, but tends
to reduce the largest balance, and therefore the maker's maximum offer, over
time. The optional `concentrated` policy ports the reference
`yg-privacyenhanced` cyclic-gap heuristic. It keeps eligible funds in a compact
run around the mixdepth cycle, generally preserving larger offers after the
final mixdepth wraps to mixdepth 0.

`concentrated` is a liquidity policy, not a privacy enhancement. Concentrating
funds in fewer active mixdepths reduces the opportunity to obscure relationships
between equal CoinJoin outputs and the change left in prior rounds. Both policies
are deterministic and may be classified by an authenticated probing taker over
repeated requests. This does not create a pre-authentication mixdepth oracle: the
source is selected only after PoDLE authentication, and every policy must reveal
the selected inputs and output addresses in `!ioauth`.

The maker keeps that cyclic routing without treating every mixdepth 0 coin as
interchangeable. Its default md0 merge pool contains exact protocol CoinJoin
outputs and CoinJoin change only when authoritative local history proves,
recursively, that every wallet input was already in that pool. Deposits,
deposit-derived change, ordinary-send change, mixed ancestry, reconstructed
history, and incomplete legacy history remain single-UTXO only. This keeps
terminal maker funds liquid after they wrap to md0 without merging them with
deposit or withdrawal ancestry.

This lineage rule is compartment hygiene, not a claim that maker change is
unlinkable. Change remains identifiable and its later reuse links rounds; input
consolidation can reveal additional common ownership. Long-running makers
should still minimize unnecessary inputs and avoid distinctive fee settings.

Treat mixdepth boundaries as privacy boundaries when making manual sends. For
the exact HD paths, address branches, and wallet behavior, see
[Technical Wallet Notes](wallet.md#hd-structure).

Automatic fixed-amount direct sends stay within one mixdepth. When the source
is not pinned, they use the highest mixdepth with a fee-sufficient admissible
selection. Selection minimizes script clusters before input count and excess
value, and treats every UTXO sharing a script as one atomic cluster. Frozen or
unconfirmed members prevent automatic partial spending of that cluster. In
mixdepth 0, more than one cluster may be consolidated only when every selected
outpoint has exact CoinJoin provenance. Sweeps require an explicit mixdepth;
manual and explicit input selection remain authoritative.

## PoDLE

Proof of Discrete Log Equivalence (PoDLE) prevents cost-free probing of maker
UTXOs. Without it, a taker could repeatedly request transactions, collect maker
inputs, and abort before paying any CoinJoin cost.

### Protocol Flow

1. The taker commits to `C = SHA256(P2)`, where `P2 = k * J`.
2. The maker accepts the commitment and sends its encryption key.
3. The taker reveals `P = k * G`, `P2`, and a Schnorr-like proof.
4. The maker verifies the commitment, proof, UTXO, and ownership binding.
5. The maker blacklists the commitment immediately.
6. After `!ioauth`, the maker relays `!hp2` through fresh directory
   connections, a random nick, and an isolated Tor stream.

Relay happens after every maker in the current transaction has processed the
commitment. Relaying earlier could make peers reject the same taker's `!auth`.
The ephemeral relay identity keeps the public blacklist broadcast separate from
the maker that consumed the commitment.

### Proof

The proof shows that `P = k * G` and `P2 = k * J` have the same unknown scalar
`k`:

1. Compute nonce commitments `KG = r * G` and `KJ = r * J`.
2. Compute `e = SHA256(KG || KJ || P || P2)`.
3. Compute `s = r + e * k mod n`.
4. Reveal `(P, P2, s, e)`.

The maker reconstructs:

```text
KG = s * G - e * P
KJ = s * J - e * P2
```

and checks the challenge hash and `SHA256(P2) = C`.

For a tr0 BIP86 UTXO, `k` is the Taproot output scalar rather than the wallet's
internal scalar. The taker normalizes the internal key to even Y, applies the
BIP341 `TapTweak`, and normalizes the resulting output key to even Y before
constructing the proof. The maker binds `P` directly to the P2TR witness
program. Legacy and P2WPKH PoDLE proofs continue to use the untweaked private
scalar.

The nonce is derived with a domain-separated RFC 6979-style HMAC-SHA256
construction keyed by the proof scalar `k`. Its transcript binds the UTXO
reference, NUMS index, `P`, and `P2`, preventing nonce reuse across distinct
proofs. Secret response multiplication and addition are delegated to
libsecp256k1 key-tweak operations rather than Python bigint arithmetic.

### NUMS Points And Reuse

`J` is one of 256 deterministic Nothing-Up-My-Sleeve points with no known
discrete-log relation to Bitcoin's generator `G`. The construction hashes the
compressed and uncompressed encodings of `G`, the one-byte index, and a counter
until the result encodes a valid curve point. The implementation and vectors
live in `jmcore/src/jmcore/podle.py`.

One UTXO can produce commitments with multiple NUMS indices. Makers bound reuse
with the configured `taker_utxo_retries` allowance.

Takers track used commitments in `cmtdata/commitments.json`. Makers maintain
the relayed blacklist in `cmtdata/commitmentlist`.

PoDLE eligibility uses `taker_utxo_age` and `taker_utxo_amtpercent`; see
`config.toml.template`. Eligible coins are prioritized by confirmations and then
value. Automatic taker coin selection also requires at least one selected input
to have an unused, non-blacklisted commitment index. If the normal funding
choice is exhausted, the taker reselects with a fresh eligible UTXO and spends
that UTXO in the CoinJoin rather than revealing a separate unspent coin. Manual
and explicit input selection remain authoritative and can fail when the chosen
set has no fresh commitment.

## Maker Identity Renewal

A maker periodically replaces its nick, ephemeral onion service, and
directory connections, at a randomized configured interval
(`identity_renewal_min_sec` / `identity_renewal_max_sec`; see `config.toml.template`).
The replacement offers are built fresh, so their randomized fees and maximum size differ from
the previous ones.

The old identity first stops accepting new direct connections, keeps its
directory routes for a grace period so in-flight rounds can finish, then
disconnects from every directory. After a random quiet interval the new
identity connects. The gap makes it harder to link the two nicks by timing,
so the maker briefly shows no connected directories. This is expected: it is
logged as rotation progress and does not send a disconnection notification.

Ephemeral onions exist only while the maker's Tor control connection stays
open. Every minute the maker asks Tor whether that connection still owns its
onion. This detects a lost control connection (for example after a Tor
restart); it does not prove the onion descriptor is published or reachable.
After two failed checks on the same identity the maker renews its identity
early instead of advertising a dead onion until the next scheduled renewal.
If the control port refuses the check, as some filtering proxies do, the maker
disables this recovery rather than treating the refusal as an outage.

## Fidelity Bonds

Fidelity bonds let makers prove that they have locked bitcoin. Takers can use
the time-value of that locked UTXO when selecting makers, making large-scale
maker identities costly to create.

### Privacy Properties

The bond proof publishes one exact UTXO and associates it with the maker's
identity. Its transaction history, funding source, amount, and later spend are
therefore public linkage points.

Prepare bond funds with coin control and CoinJoin them before locking. Do not
merge unrelated deposits with funds that a probing taker can cause the maker to
spend alongside an advertised bond. A bond's timelock protects availability,
not anonymity.

The locked UTXO cannot participate directly in CoinJoins. After expiry it must
first be redeemed to a regular output.

### Script And Bond Value

The bond is P2WSH with this witness script:

```text
<locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP <pubkey> OP_CHECKSIG
```

Bond value increases with amount and the committed interval from confirmation
to locktime. It remains constant before expiry and decays afterward. One bond
is one UTXO. If an address receives multiple payments, only the largest UTXO
is announced as the bond; additional locked UTXOs do not combine with it.

CLTV validity follows chain median-time-past. Local wall-clock time alone does
not make a bond spendable.

### Proof And Certificate Chain

The 252-byte fidelity-bond proof contains:

| Field | Size | Purpose |
|---|---:|---|
| Nick signature | 72 | Certificate key signs the taker/maker nick pair |
| Certificate signature | 72 | Bond key delegates to the certificate key |
| Certificate public key | 33 | Online key used for nick proofs |
| Certificate expiry | 2 | Absolute 2016-block period, little-endian |
| Bond public key | 33 | Key committed by the CLTV script |
| Transaction ID | 32 | Bond outpoint transaction ID in display order |
| Output index | 4 | Bond outpoint index, little-endian |
| Timelock | 4 | CLTV timestamp, little-endian |

DER signatures are left-padded with `0xff` to fixed 72-byte fields. The
certificate message binds its public key and absolute expiry period:

```text
fidelity-bond-cert|<certificate-pubkey>|<absolute-expiry-period>
```

The trust chain is:

```text
bond key (optionally cold) -> certificate key (hot) -> maker nick proof
```

Separating these keys keeps the bond-spending key offline during normal maker
operation. Compromise of the hot certificate key can permit impersonation while
takers accept the bond proof, but does not spend the bond funds. Reference and
JoinMarket NG takers enforce the authenticated certificate expiry field against
chain height.

Certificate issuance chooses an absolute period, often by adding a configured
number of periods to the current chain period. The signed field is not a
relative duration on the wire.

### Cold Wallet Setup

Cold-key setup, backend use, signer compatibility, certificate renewal,
redemption, migration, and the public hardware-wallet test vector are
operational procedures. See [Fidelity Bond Operations](../fidelity-bond-operations.md)
for the maintained workflow.

## Private Channel Ring Limits

Randomly splitting change across cofunded private Taproot channels obscures
individual ownership amounts from a passive on-chain observer. It is not a
proof that subset-sum or transaction-graph analysis is impossible. Ordinary
makers still receive exact single-owner change, and timing, repeated peers,
amount bounds, later spends, and implementation fingerprints remain evidence.

### Amount Sampling And Transaction Fingerprints

The fundee's pushed balance is its own co-funded contribution, not a gift from
the opener. A small `max_push` relative to channel capacity can distinguish
incoming and outgoing amounts. The default now permits overlapping ranges;
explicit lower limits remain binding, including those advertised by older peers.
See `config.toml.template` for the policy and current defaults. Reserve, fee,
ownership, and per-edge capacity checks still apply.

For each proposed directed cycle, the planner removes sequential interval-width
bias with exact integer rejection sampling. Splits are uniform **conditional on
that cycle**. The accepted cycle prior is proportional to its feasible split
count divided by its rejection envelope, not uniform over all cycles and splits.
A cycle-independent envelope was rejected because mixed narrow/wide push limits
exhausted the attempt budget on otherwise feasible large rings. Planning remains
bounded and can fail without producing a partial plan.

Uniform splits are not automatically uniform ownership partitions. An exact toy
calculation uses three known residuals `(12, 18, 24)`, one input per party,
capacities `[8, 30]`, and uniformly permuted output slots. The hidden partition
assigns an unordered pair of owners to each slot; the observation is the ordered
capacity tuple. Latent splits, cycle directions, and duplicate-value output
permutations are aggregated. Define
`P_guess = sum_observations max_partition P(observation, partition)` and average
conditional min-entropy as `-log2(P_guess)`.

| Outgoing/incoming floors, push ceiling | P_guess | Average bits | Worst-observation bits |
| --- | --- | --- | --- |
| `3 / 2`, `5` | `63/128` | 1.022720 | 1.000000 |
| `3 / 2`, `24` | `164/687` | 2.066614 | 1.321928 |
| `3 / 3`, `24` | `1118/4497` | 2.008043 | 1.192645 |

These results use the implemented per-cycle sampler. Both cycles happen to have
the same envelope in this fixture, so it also matches joint-uniform sampling
here, not in general. The previous sequential sampler's wide-push average was
1.992759 bits, while narrow push showed no sampler gain. Symmetric floors made
both reported entropy measures worse than asymmetric wide push. This model is
not multi-input ownership clustering, a real-world anonymity estimate, or a
claim that uniform plans maximize partition entropy.

New ring transactions use the same current-height or randomly backdated
anti-fee-sniping locktime and non-RBF sequence policy as plain `tr0` transactions.
Makers bound nonzero locktimes against their own chain tip. This removes one
fingerprint, not all ways to identify rings or proof that the entire pit is an
anonymity set. All ring peers must support this transaction policy; older peers
may reject new rounds. Existing unsigned or signed journals retain their exact
transaction bytes, including older zero locktimes; recovery never resamples them.

### Coordinator And Cross-Round Limits

The taker coordinates the ring and learns participant inputs, contributions,
and Lightning identities. Each ring maker learns its two neighbors and its own
channel balances. Unannounced channels and SCID aliases do not hide this
information from the endpoints. Colluding neighbors or a malicious coordinator
have a stronger view than an on-chain observer. Reusing a fidelity bond across
pits also links those maker identities publicly: every orderbook request
receives the bond proofs, and orderbook watchers republish them. A maker that
needs unlinked sw0 and tr0 identities must run them from separate wallets with
separate bonds.

Pairwise split negotiation is not implemented. Writing residuals as
`r_i = out_i + in_i` and capacities as `cap_i = out_i + in_(i+1)` leaves one
cyclic parameter only if no split is known. Any endpoint's split, including a
participating taker's, determines the whole cycle; bounds can also eliminate
the remaining uncertainty. Hiding initial negotiation alone would not help:
prepared reports and full readiness states currently disclose the splits too.
A maker-only ring with a noncolluding coordinator would require a separate
protocol design preserving endpoint accounting and readiness guarantees.

Automatic per-channel or per-ring LND identity rotation is also not implemented.
Managed node generations support allocation and recovery, but do not constitute
a production rotation driver. Existing nick rotation is activity-independent,
not per round. A safe rotation must retain old node keys, bindings, wallet data,
and recovery journals for funded channels; missing state is not permission to
replace them. Even fresh LND keys and nicks would not hide a reused bond outpoint.
Bond-membership proofs are outside this design.

Private channel buyout messages stay between the channel owners, not the
CoinJoin taker. A cooperative key-path spend avoids explicitly identifying the
input as a channel, but an observer with prior knowledge of that funding output
can still follow it. An immediate escrow sweep adds a recognizable graph edge;
it is not equivalent to reusing the escrow directly in another CoinJoin.
Timeout splits and force closes can expose balances, and script-path claims
reveal the escrow's hashlock construction. Routing or rebalancing may change
balances, but must not be assumed to have erased the original allocation.

Lightning settlement has its own threat model: routing peers see adjacent
nodes and local amounts, invoices can disclose route hints, and failed payment
attempts can leak information. A mixdepth-specific node limits cross-mixdepth
identity reuse, not all payment correlation. See the
[transaction-graph analysis](https://gist.github.com/nothingmuch/d84ba390d89b5b08897af2d95009c2a1)
and [Lightning privacy study](https://arxiv.org/abs/2003.12470v1). The
[multiparty-channel liquidity analysis](https://arxiv.org/abs/2601.04835)
does not establish anonymity or buyout security.

## Cryptographic Parameters

JoinMarket uses secp256k1, the same curve used by Bitcoin:

```text
y^2 = x^3 + 7 mod p
p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
n = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
```

Bitcoin's generator point `G` is defined by SEC 2. PoDLE's NUMS points are
alternative generators derived transparently from `G`; knowing a discrete-log
relation between a NUMS point and `G` would break the proof's soundness.

References:

- [SEC 2: Recommended Elliptic Curve Domain Parameters](https://www.secg.org/sec2-v2.pdf)
- [PoDLE specification](https://gist.github.com/AdamISZ/9cbba5e9408d23813ca8)
