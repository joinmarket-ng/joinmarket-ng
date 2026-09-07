# CoinJoin And Wallet Concepts

## What Is CoinJoin

Bitcoin transactions are public. CoinJoin combines inputs from several owners
into one transaction. In JoinMarket, each participant receives an output of
the same amount, making several ownership mappings plausible to an observer.
The transaction, amounts, and subsequent spends remain visible.

An **input** spends an existing **UTXO** (unspent transaction output). An
**output** creates a new UTXO. Wallet balances are totals of these individual
coins, not a single account balance held by a service.

## Makers And Takers

A **taker** arranges a CoinJoin, chooses offers, and pays maker and mining fees.
A **maker** advertises liquidity and signs a valid proposed transaction when
selected, earning its offered fee. Neither role guarantees privacy or profit.

A **tumbler** automates a sequence of CoinJoins and waits, optionally taking
both roles. It does not change the underlying custody or privacy limits.

## Equal Outputs And Change

The equal-value outputs provide the main ownership ambiguity. Any remaining
input value returns as **change**, after accounting for fees. Its distinct
amount can make it easier to link to a participant. Treat change differently
from a CoinJoin's equal output when deciding what to spend next.

## Mixdepths

A mixdepth is a separate account within the same wallet. An `INTERNAL` CoinJoin
moves the equal output to the next mixdepth while change stays behind; the last
mixdepth wraps to the first. This separates coins with different histories.

Mixdepth numbers are not a count of CoinJoins or a privacy rating. Later
consolidation, address reuse, or matching amounts can undo useful separation.

## Why JoinMarket Is Different

There is no global coordinator choosing every round or holding participants'
keys. Directory servers help peers discover and reach one another; each taker
coordinates its own transaction. Tor protects network connections, while
CoinJoin addresses links on the public ledger. Neither replaces the other.

## Fidelity Bonds

A fidelity bond locks bitcoin until a chosen date. A maker proves control of it
to make operating many apparently independent makers more costly. Takers can
favor bonded makers, but a bond does not prove honesty or guarantee selection.
The bond is public and can link its funding history to the maker.

## Recovery Words And Passwords

The **mnemonic** is the set of recovery words from which a wallet derives its
keys. An optional **BIP39 passphrase** changes those keys, so it must be backed
up too. The **wallet-file password** only encrypts the local file. These are
different secrets with different purposes; see [backups and recovery](../recover-wallet.md).

## Privacy Limits

An observer may combine public transactions with exchange records, timing,
network observations, or knowledge gained as a participant. A CoinJoin adds
ambiguity; it does not erase those observations. There is no universal number
of rounds or participants that makes coins untraceable.

Read [privacy practices](best-practices.md) before spending, or the
[threat model](threat-model.md) for the adversaries and assumptions in detail.
For a deeper analysis of transaction graphs and privacy claims, read
[Collaborative Transaction Privacy](https://gist.github.com/nothingmuch/d84ba390d89b5b08897af2d95009c2a1).
