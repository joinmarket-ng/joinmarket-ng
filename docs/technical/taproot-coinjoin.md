# Taproot CoinJoin (tr0)

JoinMarket NG can run CoinJoins whose inputs and outputs are
[BIP86](https://github.com/bitcoin/bips/blob/master/bip-0086.mediawiki) taproot
(P2TR) key-path spends, in addition to the default native segwit (P2WPKH)
path. Taproot offers use the dedicated `tr0` order family and are signed with
BIP341 key-path Schnorr signatures.

This builds on the wider taproot specification tracked as JMP-0005.

## Rigid pit

The `tr0` and `sw0` offer families are independent, rigid "pits". Within one
CoinJoin every input, every equal-amount output, and every change output shares
a single script type, fixed by the offer family:

- `sw0` -> P2WPKH everywhere.
- `tr0` -> P2TR everywhere.

There is no per-transaction or taker-chosen output type, and the two pits never
mix in one transaction. Keeping the whole transaction uniform maximizes the
anonymity set and avoids fingerprinting participants by script type. A maker MAY
serve both pits at once (advertising `sw0` and `tr0` offers), but as two separate
liquidity pools: a `tr0` fill is funded entirely from P2TR coins and pays P2TR
outputs, a `sw0` fill entirely from P2WPKH.

## Offer types

The maker advertises one offer family per offer; the taker selects makers by the
pit it wants. The relevant `OfferType` values are:

- `sw0reloffer` / `sw0absoffer`: native segwit (P2WPKH), the default.
- `tr0reloffer` / `tr0absoffer`: taproot (P2TR).

A taker matches both the relative and absolute variant of its preferred family,
so a maker may advertise either fee model.

## Configuration

Maker (`[maker]`):

```
offer_type = "tr0reloffer"
```

Setting a `tr0` offer type makes the maker derive a taproot (`p2tr`) wallet for
its CoinJoin and change outputs, and select only P2TR inputs, regardless of
`[wallet] address_type`, so the advertised offer and the on-chain scripts always
agree.

Taker (`[taker]`):

```
preferred_offer_type = "tr0reloffer"
```

This selects taproot makers and produces a uniformly P2TR transaction: the
taker's equal output, change, and inputs are all P2TR. Because the pit is rigid,
run a `p2tr` wallet (`[wallet] address_type = "p2tr"`) so the taker has P2TR
inputs to spend; a P2WPKH-only wallet has no eligible `tr0` inputs. The payment
destination may be any P2TR address.

## Signing

Both maker and taker sign P2TR inputs with a single 64-byte BIP341 key-path
Schnorr signature. Every CoinJoin input MUST use `SIGHASH_DEFAULT`: a 64-byte
signature with no trailing sighash byte. Other sighash flags would let a
participant leave outputs uncommitted and rewrite the transaction after signing,
so verifiers reject any signature that is not exactly 64 bytes. The taproot
sighash commits to every input's amount and scriptPubKey, so both sides assemble
the full prevout set (ordered by input index) before signing. The signing key is
the tweaked BIP86 output key.

The taker's PoDLE commitment for a taproot UTXO commits to the tweaked BIP86
output key (the on-chain key), not the raw internal key, so the maker's PoDLE
binding (`x_only(P) == program`) succeeds.

Received [silent payment](silent-payments.md) outputs are ordinary key-path P2TR
UTXOs, so they fit the rigid `tr0` pit directly. They have no BIP32 path, so the
wallet recomputes their output key on demand from the stored tweaks; unlike BIP86
coins this key is the final taproot output key and carries no extra taptweak.

## Interoperability

The `tr0` family is specific to JoinMarket NG. The legacy reference
implementation rejects taproot PoDLE commitments, so taproot CoinJoins run only
between NG makers and takers. Segwit (`sw0`) CoinJoins remain fully compatible
with the reference implementation.
