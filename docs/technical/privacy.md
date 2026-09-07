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

The nonce is derived with a domain-separated RFC 6979-style HMAC-SHA256
construction keyed by the UTXO private key. Its transcript binds the UTXO
reference, NUMS index, `P`, and `P2`, preventing nonce reuse across distinct
proofs. Secret response multiplication and addition are delegated to
libsecp256k1 key-tweak operations rather than Python bigint arithmetic.

### NUMS Points And Reuse

`J` is one of 256 deterministic Nothing-Up-My-Sleeve points with no known
discrete-log relation to Bitcoin's generator `G`. The construction hashes the
compressed and uncompressed encodings of `G`, the one-byte index, and a counter
until the result encodes a valid curve point. The implementation and vectors
live in `jmcore/src/jmcore/podle.py`.

One UTXO can produce commitments with multiple NUMS indices:

- index 0 is the first use,
- indices 1 and 2 cover normal retries,
- higher indices require a maker configured with a larger
  `taker_utxo_retries` allowance.

Takers track used commitments in `cmtdata/commitments.json`. Makers maintain
the relayed blacklist in `cmtdata/commitmentlist`.

By default, a PoDLE UTXO needs five confirmations and value of at least 20% of
the CoinJoin amount. Eligible coins are prioritized by confirmations and then
value. Automatic taker coin selection also requires at least one selected input
to have an unused, non-blacklisted commitment index. If the normal funding
choice is exhausted, the taker reselects with a fresh eligible UTXO and spends
that UTXO in the CoinJoin rather than revealing a separate unspent coin. Manual
and explicit input selection remain authoritative and can fail when the chosen
set has no fresh commitment.

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
