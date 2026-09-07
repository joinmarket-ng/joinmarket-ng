<p align="center">
  <img src="media/logo.svg" alt="JoinMarket NG Logo" width="200"/>
</p>

# JoinMarket NG

JoinMarket NG is a modern implementation of the JoinMarket CoinJoin protocol for Bitcoin privacy.

Bitcoin's public ledger makes every transaction visible. Without careful privacy practices, payments
can expose a user's transaction history, balance, and financial relationships. CoinJoin improves
privacy by combining inputs from several users into one transaction with equal-value outputs. An
observer can see the transaction, but cannot reliably determine which participant owns which
equal-value output.

## Why JoinMarket

JoinMarket organizes CoinJoins as an open market instead of relying on a central coordinator:

- **Makers** offer bitcoin liquidity and earn fees for participating in CoinJoins.
- **Takers** choose offers, build a CoinJoin, and pay the makers they select.

Participants discover each other through redundant directory servers, then exchange sensitive
transaction data through end-to-end encrypted messages, either over direct peer-to-peer connections
or directory relays. Each taker coordinates its own CoinJoin, and every participant keeps control of
their keys. There is no single service that schedules every round, selects every participant, or
holds users' funds.

The market gives makers an economic reason to keep liquidity available. Takers can initiate a
CoinJoin when they need one instead of waiting for rounds run by a central service. This combination
of decentralization and persistent, incentivized liquidity is what makes JoinMarket an important
part of Bitcoin's privacy infrastructure.

## Why JoinMarket NG

JoinMarket NG is an independent implementation built for maintainability, auditability, and modern
Bitcoin infrastructure. Its modular, strictly typed Python codebase supports Bitcoin Core and a
lightweight Neutrino backend, with Tor integrated throughout the network architecture.

Most importantly, JoinMarket NG is wire-compatible with the reference JoinMarket implementation.
Makers and takers from both implementations participate in the same market, so a new codebase does
not fragment existing liquidity. Independent implementations reduce reliance on any one codebase,
make protocol assumptions easier to test, and help the JoinMarket network remain adaptable over
time.

## Start Here

- [Get started](https://joinmarket-ng.github.io/joinmarket-ng/getting-started/):
  choose an interface, set up a backend, back up a wallet, and prepare for a CoinJoin.
- [Use your wallet](https://joinmarket-ng.github.io/joinmarket-ng/README-jmwallet/):
  receive, check balances, and send.
- [Common questions](https://joinmarket-ng.github.io/joinmarket-ng/faq/) and
  [troubleshooting](https://joinmarket-ng.github.io/joinmarket-ng/troubleshooting/).
- [Reference](https://joinmarket-ng.github.io/joinmarket-ng/reference/):
  commands, settings, specialized operations, and protocol details.
- [Contribute](https://joinmarket-ng.github.io/joinmarket-ng/technical/development/):
  development setup, tests, and documentation preview.

CoinJoin does not guarantee anonymity. Before using funds, read the
[privacy practices](https://joinmarket-ng.github.io/joinmarket-ng/technical/best-practices/)
and [backup requirements](https://joinmarket-ng.github.io/joinmarket-ng/recover-wallet/).

## Community

- Telegram: https://t.me/joinmarketorg
- SimpleX: https://smp12.simplex.im/g#bx_0bFdk7OnttE0jlytSd73jGjCcHy2qCrhmEzgWXTk

## License

MIT: [LICENSE](LICENSE)

## Acknowledgements

JoinMarket NG builds on the work of the original JoinMarket project. Special thanks to Adam Gibson (@AdamISZ) and all past and present JoinMarket contributors.

Thanks to @1440000bytes (Floppy) for the ongoing external audit, and to @L3ftBlank for beta testing and contributions. And to everyone who has opened an issue, submitted a PR, or joined a discussion. You're part of this too!

Sustained by grants from [OpenSats](https://opensats.org/) and the [HRF Bitcoin Development Fund](https://hrf.org/program/financial-freedom/bitcoin-development-fund/). Keeping this project free, open, and independent.

## Donations

JoinMarket NG accepts Bitcoin onchain donations through [Silent Payments](https://bips.dev/352/).
Many wallets can send to Silent Payment addresses; see the current
[wallet support list](https://silentpayments.xyz/docs/wallets/). [Sparrow Wallet](https://sparrowwallet.com/)
is a good option.

```text
sp1qqt3jvfalrvtjksvmul943cpt3vvx0aydg0fegz4kzagu2dw9zp2x2qjyydsrdzmcf5ltr973zsadcktyqdfzrzkmml2guta6p664fu8e4uvmvmq4
```

For Lightning Network donations, use this BOLT12 invoice:

```text
lno1pgx55mmfdexkzuntv46zqnj8zcssyy55ll6edeyh455s9n2lr9nnaypqj57eqcjadrpzayd4rfzuqvkn
```
