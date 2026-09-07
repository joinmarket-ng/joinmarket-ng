# JoinMarket NG

JoinMarket NG is a self-custodial Bitcoin wallet for CoinJoin. It connects to the
JoinMarket market, where **takers** pay to arrange CoinJoins and **makers** offer
liquidity in exchange for fees. You keep control of your keys.

## Start Here

[Get started](getting-started.md) walks through choosing an interface, setting up
a wallet, and preparing for your first CoinJoin. Already have a wallet?
[Recover or migrate it](recover-wallet.md) before using it here.

## What Do You Need To Do?

| Goal | Guide |
| --- | --- |
| Install or connect a Bitcoin node | [Install](install.md) and [set up Bitcoin and Tor](setup.md) |
| Receive, check a balance, or send a payment | [Manage your wallet](README-jmwallet.md) |
| Make a CoinJoin now | [Run a CoinJoin](README-taker.md) |
| Automate several CoinJoins | [Run the tumbler](README-tumbler.md) |
| Offer liquidity and earn fees | [Run a maker](README-maker.md) |
| Back up or restore a wallet | [Back up and recover](recover-wallet.md) |
| Fix an error or a missing balance | [Troubleshoot](troubleshooting.md) |
| Upgrade an existing installation | [Update](update.md) |

## Before You Use Funds

CoinJoin creates ambiguity about ownership; it does not make transactions
invisible or guarantee anonymity. Change, address reuse, and later spending can
reveal links. Read [CoinJoin and wallet concepts](technical/concepts.md) and
[privacy practices](technical/best-practices.md). Back up your recovery material
before depositing funds.

Find short answers in [common questions](faq.md), exact options in
[reference](reference.md), and development material under
[contribute](technical/development.md).

## Get Help

Ask general questions in the [JoinMarket Telegram community](https://t.me/joinmarketorg)
or [SimpleX community](https://smp12.simplex.im/g#bx_0bFdk7OnttE0jlytSd73jGjCcHy2qCrhmEzgWXTk).
For a reproducible problem, follow [reporting a problem](troubleshooting.md#report-a-problem).
Never share a seed, passphrase, wallet file, or configuration containing credentials.
