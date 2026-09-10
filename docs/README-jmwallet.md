# Wallet Tasks

Use these tasks after [setting up a wallet](getting-started.md). For options in
your installed version, run `jm-wallet --help` and `jm-wallet <command> --help`.

## Receiving Funds

Reserve a new address before giving it to a payer. Mixdepth 0 is the normal
deposit mixdepth:

```bash
jm-wallet address new 0 --label "payer or purpose"
```

`address new` persists the reservation before printing the address, so it will
not be issued again. Run it once for each payer or invoice. Review reservations
with `jm-wallet address list`; use `jm-wallet address --help` for labeling or
releasing an address.

## Viewing Balances And History

```bash
jm-wallet info
jm-wallet info --extended
jm-wallet history
```

`info` shows balances by mixdepth without issuing or reserving receive addresses.
Use `jm-wallet address new <mixdepth>` when you need an address to give to a payer.
The extended view includes addresses and
UTXO outpoints for review or explicit coin control. `history` shows this
wallet's recorded CoinJoin and send history; it is not a replacement for
independent transaction records.

A low-fee transaction can take hours or days to confirm. `info` refreshes
confirmation status without expiring pending entries; `history` displays the
recorded status without querying the backend.

Background monitoring has configurable limits. Makers use `pending_tx_timeout_min`
(default 60 minutes) for attempts without a recorded TXID and
`pending_tx_abandon_hours` (default 72 hours) for recorded transactions. Takers use
their `pending_tx_abandon_hours` setting (default 24 hours). A deadline stops
automatic checks and records `[TIMED OUT]` in CLI history. This is a local
monitoring outcome: it does not invalidate the transaction, abandon it in
Bitcoin Core, or release its inputs. Existing input reservation limits still apply.

Run `jm-wallet info` to check for a later confirmation, even after a timeout.
This also repairs failed rows from older versions when their recorded TXIDs
are verified as confirmed. On upgrade, expired pending rows time out without
restarting backend polling, and failed rows remain out of background monitoring.
Explicit refresh uses targeted lookups without a full wallet rescan; rows
without a recorded TXID cannot be repaired this way. A 100-day-old transaction
is not repeatedly polled in the background.

CPFP adds a child transaction while preserving the parent's TXID. RBF creates a
different transaction with a new TXID. Automatic linkage between a replacement
and the original CoinJoin history entry is not currently supported.

## Sending Funds

For an ordinary Bitcoin payment, replace the destination, amount in satoshis,
and source mixdepth placeholders:

```bash
jm-wallet send DESTINATION --amount AMOUNT_SATS --mixdepth MIXDEPTH
```

This is a direct transaction, not a CoinJoin. Use `jm-taker coinjoin` for a
CoinJoin, after configuring the taker and reviewing `jm-taker coinjoin --help`.
The [taker guide](README-taker.md) covers that workflow. Taker input eligibility
is separate from ordinary sends: its default five-confirmation PoDLE policy is
not a universal spendability limit for `jm-wallet send`.

### Coin Control

Use the interactive selector when you need to choose inputs deliberately:

```bash
jm-wallet send DESTINATION --select-utxos --amount AMOUNT_SATS
```

A transaction can spend inputs from only one mixdepth. The selection pins the
mixdepth, or pass `--mixdepth` to pin it before choosing inputs. With
`--select-utxos`, omitting `--amount` sweeps every selected input to the
destination. Specify `--amount` unless a sweep is intentional. `--input-utxo`
is the noninteractive alternative; see `jm-wallet send --help`.

## Signing PSBTs

`sign-psbt` reviews and partially signs wallet-owned PSBT inputs offline. It
does not connect to the configured backend or broadcast a transaction:

```bash
jm-wallet sign-psbt --input unsigned.psbt --output signed.psbt
```

Review the displayed inputs, outputs, and fee before confirming. Use
`jm-wallet sign-psbt --help` for supported PSBT input requirements and key
discovery options.

## Reserving Deposit Addresses

Address reservations prevent accidental reuse. `jm-wallet address new 0` is the
normal way to reserve a fresh receive address; `--label` associates it with a
purpose without affecting whether received coins can be spent. A reserved
address can be inspected with `jm-wallet address list` or released only when it
was never handed out.

The extended view shows fresh addresses after the last used or reserved receive
address. Viewing these rows does not reserve them; use `address new` before
giving an address to a payer.

Older versions also reserved one address per mixdepth whenever ordinary `info`
displayed deposit suggestions. Those reservations remain intact on upgrade,
including ones without labels: the wallet cannot tell whether a displayed
address was subsequently handed out. They do not freeze funds or prevent an
expected payment from arriving. Release an old reservation only if you know
the address was never shared.

## Deleting A Wallet

Deletion is permanent. Stop every maker, taker, tumbler, wallet daemon, and
other process using the wallet first. Confirm that a mnemonic backup, any BIP39
passphrase, the local file-encryption password, and any external fidelity-bond
private keys are available and have been tested through a separate recovery
before proceeding.

Start with the scoped plan:

```bash
jm-wallet delete --dry-run --core-wallet-dir "$HOME/.bitcoin/wallets"
```

For a Bitcoin Core descriptor backend, `--core-wallet-dir` must be the local
host's actual Core `-walletdir` containing this wallet's generated
`jm_<fingerprint>_<network>` directory. After checking every planned path and
fingerprint, run the same command without `--dry-run`. Do not replace this
wallet-specific operation with deletion of an entire wallet directory.

For a remote Core whose wallet directory is not mounted locally, use
`--keep-backend-wallet`. It removes JoinMarket's local state but leaves the Core
wallet for that node's administrator to unload and remove on the remote host.
If the displayed fingerprint differs from the mnemonic sidecar, stop and verify
the BIP39 passphrase. Use `--allow-fingerprint-mismatch` only after reviewing
the dry-run plan.

By default, deletion removes the mnemonic and `.meta` file, address metadata,
labels and frozen state, and the history reconstruction cache. It retains
CoinJoin history unless you add `--delete-history`, and the fidelity-bond
registry unless you add `--delete-bond-registry`.

With Neutrino, omit Core options. A reachable, current `neutrino-api` that
supports deleting watched addresses is required before local wallet files are
removed. An unsupported server or active rescan leaves local data intact. Update
`wallet.mnemonic_file` in `config.toml` afterward if it still names the deleted
file.

## Backends

Configure either the recommended `descriptor_wallet` backend with a node you
control, or the `neutrino` backend. See [Configure Backend](install.md#configure-backend)
for setup and [wallet scanning](technical/wallet-scanning.md) when diagnosing
missing known funds.

## Fidelity Bonds

Fidelity bonds have separate keys, locktimes, and recovery considerations. See
[Fidelity Bond Operations](fidelity-bond-operations.md) before creating or
spending one. For restoring an existing wallet, see [Recover A Wallet](recover-wallet.md).

## Command Help

Use `jm-wallet --help` for installed commands and `jm-wallet <command> --help`
for options such as `send`, `delete`, `rescan`, or `recover-bonds`. The
[full command reference](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/jmwallet/README.md)
is maintained alongside the CLI.
