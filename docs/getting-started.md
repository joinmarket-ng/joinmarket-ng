# Get Started

Use this path for a new wallet. To bring an existing wallet over, follow
[recovery and migration](recover-wallet.md) instead.

## 1. Choose An Interface

| Interface | Setup |
| --- | --- |
| Command line (CLI, recommended) | [Install](install.md), then [configure Bitcoin and Tor](setup.md) |
| Terminal menu (TUI) | A wrapper over the CLI: complete its setup, then open the [terminal menu](README-tui.md) |
| Browser interface | Use [JAM](https://github.com/joinmarket-webui/jam) and follow its own installation and wallet guides |

The CLI is JoinMarket NG's main interface, and the steps below use it. JAM is a
separate project; its wallet files cannot be opened with `jm-wallet`.

You can install the software, read command help, and create an unfunded wallet
without sending bitcoin. Complete the backup and setup steps before using funds.

## 2. Create And Back Up A Wallet

Check that your backend is synced before continuing. In a terminal where the
installation is activated, run:

```bash
jm-wallet generate
```

The command creates an encrypted wallet file and displays its recovery words.
Record them offline before continuing. The wallet-file password protects this
local file; it is not a substitute for the recovery words.

If you use an additional **BIP39 passphrase**, keep that exact passphrase too.
Different passphrases derive different wallets. Never put recovery words or
passphrases in a shell command, screenshot, support message, or website.
See [what to back up](recover-wallet.md#preserve-what-is-not-in-the-seed).

## 3. Get A Deposit Address

```bash
jm-wallet address new 0 --label "deposit"
```

This reserves a fresh address in mixdepth 0, the first wallet compartment.
Check that it belongs to the intended wallet and network before sending to it.
Generating an address does not fund the wallet.

When ready to use funds, start with an amount you are prepared to risk. Keep
coins with unrelated histories separate. There is no fixed recommended deposit:
a CoinJoin needs enough confirmed funds for its amount, maker fees, and mining
fees, and depends on available offers.

## 4. Check The Balance

```bash
jm-wallet info
```

Wait for the deposit to appear and confirm. The displayed balance can include
coins that are not yet eligible for a CoinJoin. The default taker proof requires
an eligible UTXO with at least five confirmations; a newly confirmed deposit
may still be too young. For missing funds, [check sync first](troubleshooting.md#missing-balance-or-slow-sync).

## 5. Choose Your Next Task

- [Run a CoinJoin](README-taker.md) to move an equal-value output to the next
  mixdepth. Review the proposed amount and fees before approving.
- [Run a maker](README-maker.md) to offer liquidity while you keep the wallet online.
- [Run the tumbler](README-tumbler.md) to schedule a longer sequence with several
  destinations. Learn the single-transaction workflow before leaving it unattended.

A mixdepth is a privacy compartment, not a privacy score. Before spending the
result, read [how change and later payments affect privacy](technical/best-practices.md).
