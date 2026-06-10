# Taproot CoinJoin (tr0)

JoinMarket NG can run CoinJoins whose inputs and outputs are
[BIP86](https://github.com/bitcoin/bips/blob/master/bip-0086.mediawiki) taproot
(P2TR) key-path spends, in addition to the default native segwit (P2WPKH)
path. Taproot offers use the dedicated `tr0` order family and are signed with
BIP341 key-path Schnorr signatures.

This builds on the wider taproot specification tracked as JMP-0005.

## Offer types

The maker advertises one offer family; the taker selects makers by the output
family it prefers. The relevant `OfferType` values are:

- `sw0reloffer` / `sw0absoffer`: native segwit (P2WPKH), the default.
- `tr0reloffer` / `tr0absoffer`: taproot (P2TR).

A taker matches both the relative and absolute variant of its preferred family,
so a maker may advertise either fee model. Taproot and segwit equal-value
outputs are never mixed in a single CoinJoin: the anonymity set requires every
equal output to share one script type.

## Configuration

Maker (`[maker]`):

```
offer_type = "tr0reloffer"
```

Setting a `tr0` offer type makes the maker derive a taproot (`p2tr`) wallet for
its CoinJoin and change outputs regardless of `[wallet] address_type`, so the
advertised offer and the on-chain scripts always agree.

Taker (`[taker]`):

```
preferred_offer_type = "tr0reloffer"
```

This selects taproot makers and builds P2TR CoinJoin outputs. The taker's own
wallet `address_type` is independent: it governs the taker's change script and
is set under `[wallet]`. To produce a taproot destination and change, run the
taker with a `p2tr` wallet.

## Signing

Both maker and taker sign P2TR inputs with a single 64-byte BIP341 key-path
Schnorr signature. The taproot sighash commits to every input's amount and
scriptPubKey, so both sides assemble the full prevout set (ordered by input
index) before signing. The signing key is the tweaked BIP86 output key.

Received [silent payment](silent-payments.md) outputs are also spendable as
CoinJoin inputs. They have no BIP32 path, so the wallet recomputes their output
key on demand from the stored tweaks; unlike BIP86 coins this key is the final
taproot output key and carries no extra taptweak. The output type of a CoinJoin
is independent of its input types, so taproot inputs (BIP86 or silent payment)
can fund a CoinJoin of any output family.

## Interoperability

The `tr0` family is specific to JoinMarket NG. The legacy reference
implementation rejects taproot PoDLE commitments, so taproot CoinJoins run only
between NG makers and takers. Segwit (`sw0`) CoinJoins remain fully compatible
with the reference implementation.
