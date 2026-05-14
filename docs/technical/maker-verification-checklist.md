# Maker Transaction Verification Checklist

Before signing any CoinJoin transaction, a maker MUST verify the unsigned
transaction proposed by the taker. A bug or omission here can lead to direct
loss of funds, so this page documents the invariants enforced by
`maker/src/maker/tx_verification.py` (`verify_unsigned_transaction`) as a
reference for auditors and alternative implementations.

The verification function returns `(False, reason)` on any failure. The maker
MUST refuse to sign and log the specific reason. A failure must never be
treated as a soft warning.

## Inputs to the Verifier

The caller passes:

- the unsigned transaction hex,
- the set of UTXOs we are contributing (`our_utxos`, keyed by `(txid, vout)`),
- our declared CoinJoin output address (`cj_address`),
- our declared change output address (`change_address`),
- the CoinJoin amount in satoshis (`amount`),
- the CoinJoin fee (`cjfee`, absolute or relative depending on `offer_type`),
- our transaction-fee contribution in satoshis (`txfee`),
- the offer type (`OfferType.ABSOLUTE` or `OfferType.RELATIVE`),
- the active network (mainnet, testnet, signet, regtest).

## Structural Checks

- The transaction parses successfully via `jmcore.bitcoin.parse_transaction`.
- Version is one of `1`, `2`, or `3` (v3 is permitted for TRUC / BIP-431
  policy compatibility).
- The transaction has at least one input and at least one output.
- Every output script must be decodable to an address for the active network;
  otherwise the script is surfaced as hex and address comparisons will fail.

## Input Invariants

- Every UTXO we declared in `our_utxos` is present as an input of the
  transaction. The taker is allowed to add other inputs, but ours must all be
  there. Missing UTXOs are reported as
  `Our UTXOs not included in transaction: {...}`.

## Output Invariants

For each output, the verifier compares its address against our declared
CoinJoin and change addresses and counts occurrences.

- The CoinJoin address appears exactly once. Zero occurrences or duplicates
  are a failure.
- The change address appears exactly once. Zero occurrences or duplicates are
  a failure.
- The CoinJoin output value is `>= amount`.
- The change output value is `>= expected_change_value`, where
  `expected_change_value = sum(our_inputs) - amount - txfee + real_cjfee`.

Additional outputs (other peers' CoinJoin outputs, other change outputs) are
permitted and ignored.

## Fee and Profit Invariants

- `real_cjfee = calculate_cj_fee(offer_type, cjfee, amount)`.
  - For `OfferType.ABSOLUTE`, `real_cjfee` equals the configured absolute fee.
  - For `OfferType.RELATIVE`, `real_cjfee` is the relative rate applied to
    `amount`.
- `potentially_earned = real_cjfee - txfee` must be strictly greater than
  zero. A non-positive value is rejected with
  `Negative profit calculated: ...`.

## Failure Handling

- On any failing check, `verify_unsigned_transaction` returns
  `(False, reason)` and the maker MUST NOT sign.
- Any exception during parsing or verification is caught and turned into a
  failure with `Verification error: ...`. The transaction is never trusted on
  exception paths.

## Reference

- Implementation: `maker/src/maker/tx_verification.py`.
- Legacy reference: `joinmarket-clientserver/src/jmclient/maker.py:verify_unsigned_tx()`.
- Related security context: [Security](security.md), [Protocol](protocol.md).
