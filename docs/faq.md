---
search:
  boost: 2
---

# Common Questions

For errors, missing balances, or stalled transactions, use
[troubleshooting](troubleshooting.md).

## What Are Makers And Takers?

Makers offer liquidity and earn fees when selected. Takers arrange a CoinJoin
and pay maker and mining fees. Start with [one CoinJoin](README-taker.md) or
[maker operation](README-maker.md). You can use either role, but do not run
independent spenders against the same wallet simultaneously.

## Does JoinMarket Ever Take Custody Of My Bitcoin?

No service holds your keys. Your software signs your inputs locally. This
removes custodial risk, not software, host-security, or backup risk.

## How Much Bitcoin Do I Need?

There is no universal deposit amount. A taker needs eligible confirmed coins
for the CoinJoin amount and fees, plus available maker offers. A maker needs
funds eligible under its offer and wallet policies; being online with a balance
does not guarantee selection. See [getting started](getting-started.md).

## What Fees Does A Taker Pay?

Selected makers' CoinJoin fees and the Bitcoin mining fee for the transaction.
More inputs and participants usually make the transaction larger. Review the
fee preview and your limits in the [taker guide](README-taker.md#configuration-notes).

## Why Is My Balance Missing Or Not Spendable?

The wallet may be syncing, using a different network or passphrase, or holding
coins that are frozen, too young, locked, or in another mixdepth. Check
[balance and eligibility diagnostics](troubleshooting.md#missing-balance-or-slow-sync)
before rescanning or changing settings.

## What Are Mixdepths?

Separate accounts within one wallet. An internal CoinJoin moves the equal
output to the next account and leaves change behind. A higher number is not
a privacy score. See [mixdepths](technical/concepts.md#mixdepths).

## Why Are Equal Outputs Special, And What Is Change?

Equal amounts make several ownership mappings plausible. Change has a distinct
amount and can be more linkable. Do not assume both have the same privacy
properties. See [equal outputs and change](technical/concepts.md#equal-outputs-and-change).

## Does CoinJoin Make Coins Untraceable?

No. Transactions and amounts remain public. Timing, change, later merging,
address reuse, and outside information can narrow the possibilities.
Read [privacy practices](technical/best-practices.md).

## How Many CoinJoins Are Enough?

There is no universal number. Consider the whole spending path and what your
observer knows. More rounds cannot undo every later mistake. The
[threat model](technical/threat-model.md) explains the limits.

## Should I Consolidate UTXOs?

Only when you accept linking the inputs you spend together. Saving fees can
undo useful separation between histories. A sweep also links its inputs even
though it avoids taker change. See [mixdepth hygiene](technical/best-practices.md#mixdepth-hygiene).

## Can An Observer Tell Which Participant Was The Taker?

Transactions do not label roles, but amounts, timing, and repeated activity
can suggest them. These are heuristics, not proof.

Repeatedly using only one role creates more behavioral information. JoinMarket
NG's tumbler can interleave maker sessions with taker transactions, but role
mixing cannot repair address reuse or careless consolidation. For deeper,
experimental analysis, see
[Making JoinMarket makers harder to follow](https://gist.github.com/m0wer/a228c625fcb6a27c32e298ec903dfc44).

## When Should I Use The Tumbler?

When you want a longer sequence with several destinations rather than one
CoinJoin. It requires time, a reviewed fee budget, and safe destination handling.
Follow the [tumbler guide](README-tumbler.md), including its resume procedure.

## What Are Fidelity Bonds?

Time-locked funds a maker proves it controls. They make large numbers of maker
identities more costly but also publicly link the bond to the maker. They are
optional. Read [bond operations](fidelity-bond-operations.md) before locking funds.

## Can I Use An Existing JoinMarket Or JAM Wallet?

Protocol compatibility does not mean wallet-file compatibility. Native CLI,
JoinMarket NG's daemon, and the reference implementation use different formats.
Follow [recovery and migration](recover-wallet.md); do not rename a wallet file
to make it appear compatible.

## Do I Need Tor If CoinJoin Already Changes The Transaction Graph?

Yes for production use. Tor limits network-level linkage, while CoinJoin
addresses on-chain linkage. See [Tor setup](setup.md#tor).

## What Must I Back Up?

The mnemonic and any BIP39 passphrase, plus separate recovery material for
external-key bonds. Preserve wallet files and metadata for labels, reservations,
and accounting. The file-encryption password is not the BIP39 passphrase.
See [backups and recovery](recover-wallet.md).

## Can An Exchange Reject Coins That Have CoinJoin History?

Yes. Recipients set their own policies and can change them. No wallet can
promise acceptance, and acceptance is not a measure of privacy.

## Where Are The Exact Commands And Defaults?

Use the installed command's `--help` and the `config.toml.template` alongside
your config. The [reference index](reference.md) links to generated help and
specialized guides. For software upgrades, follow [updating](update.md).
