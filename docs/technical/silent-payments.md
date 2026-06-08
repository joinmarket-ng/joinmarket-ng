# Silent Payments (BIP352)

JoinMarket NG supports receiving via [BIP352 Silent Payments](https://github.com/bitcoin/bips/blob/master/bip-0352.mediawiki).
A silent payment address is a single, static address that a recipient can
publish once. Every payment to it lands at a unique taproot output that an
outside observer cannot link to the address or to other payments.

The motivating use case is a maker that wants to receive anonymous donations
without linking them to its fidelity bond or other wallet funds.

## How it works

A silent payment address encodes two public keys: a scan key and a spend key.
A sender combines the public keys of the transaction inputs with the
recipient's scan key (ECDH) to derive a fresh taproot output key per payment.
The recipient scans the chain with the scan private key to detect outputs and
recovers a per-output tweak that, combined with the spend key, yields the
spending key.

Keys are derived from the wallet seed using the BIP352 paths (always hardened
at the account level):

```
scan_private_key:  m / 352' / coin_type' / 0' / 1' / 0
spend_private_key: m / 352' / coin_type' / 0' / 0' / 0
```

`coin_type` is `0` on mainnet and `1` otherwise. The address human-readable
prefix is `sp` on mainnet and `tsp` on test networks.

## Usage

Display the wallet's silent payment address:

```
jm-wallet silent-payment-address --network mainnet
```

Optionally derive a labeled address (label `m >= 1`) to distinguish payment
sources. Label `m = 0` is reserved for change and is never published.

Scan a block range for incoming payments (requires the descriptor_wallet
Bitcoin Core backend, which serves full blocks with prevout data):

```
jm-wallet scan-silent-payments --start-height 840000
```

Detected outputs are reported with their `txid:vout`, value, and the unique
taproot address they landed on. The change label (`m = 0`) is always scanned
for in addition to any `--labels`.

## Privacy: why outputs are treated like mixdepth-0 deposits

A received silent payment is a fresh, unlinkable taproot UTXO. From the
wallet's perspective it has the same privacy properties and dangers as a
mixdepth-0 deposit:

- Co-spending it with the fidelity bond or with other deposits links those
  funds together and undoes the unlinkability the sender paid for.
- Advertising it as maker liquidity, or merging it with other mixdepth-0
  UTXOs, has the same risk.

JoinMarket already restricts mixdepth-0 non-CoinJoin UTXOs from being merged
(`allow_mixdepth_zero_merge`, default off) and selects at most one such UTXO at
a time. Silent payment outputs inherit this protection: before using them, mix
them, ideally with a sweep tumble that randomizes timing so the donation is not
trivially correlated with the maker's activity.

A passive taker can also receive silent payments and CoinJoin them. The wallet
surfaces a detected silent payment output as an ordinary taproot UTXO
(`register_silent_payment_utxos`) so coin selection can pick it, and recomputes
its key-path signing key on demand from the stored tweaks
(`resolve_p2tr_signing_key`). The CoinJoin itself should use a taproot output
family (`preferred_offer_type = "tr0reloffer"`); see
[Taproot CoinJoin](taproot-coinjoin.md). The mixdepth-0 co-spending warning
above still applies.

## Status and limitations

- The cryptographic core (address encoding/decoding, sender output derivation,
  receiver scanning, output key recovery) is implemented in
  `jmcore.silentpayments` and validated against the full upstream BIP352 test
  vectors.
- Address derivation and display are wired into the wallet.
- On-chain scanning is wired through the descriptor (Bitcoin Core) backend
  using `getblock` verbosity 3 (`scan_silent_payments`). Light clients cannot
  serve the required prevout data and are unsupported for scanning.
- BIP340 taproot key-path signing is implemented (`sign_p2tr_input`), so
  detected outputs can be spent: the recovered output key is the final taproot
  output key and is signed directly.
- Received outputs can be spent as taproot CoinJoin inputs (see
  [Taproot CoinJoin](taproot-coinjoin.md)).
- Paying *to* a silent payment address *through* a CoinJoin is not supported and
  is rejected by the taker. A BIP352 receiver derives the output key from the
  sum of all of a transaction's inputs, but in a CoinJoin the inputs come from
  several parties whose private keys no single sender knows, so the taker cannot
  compute the key the recipient scans for and the payment would be undetectable
  and unspendable. Receiving silent payments and mixing the resulting ordinary
  taproot coins is unaffected.
- Remaining follow-ups: persisting scan progress / importing detected outputs
  as first-class wallet UTXOs across restarts, and an automatic background
  coinjoin sweep with randomized timing for received deposits.
