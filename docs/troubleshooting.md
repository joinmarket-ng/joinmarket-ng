---
search:
  boost: 2
---

# Troubleshoot

Start with the symptom below. Keep the wallet and its backups intact while
investigating. Reinstalling, deleting caches, or rescanning repeatedly is not a
general repair procedure.

## Command Not Found

For a native installer-based setup, activate it in the current terminal:

```bash
source ~/.joinmarket-ng/activate.sh
jm-wallet --help
```

Use the matching activation file for a custom installation directory. A missing
`jm-tumbler` or `jm-orderbook-watcher` can mean you installed a restricted profile.
See [installation profiles](install-advanced.md#installation-profiles).
Flatpak commands must run through the [Flatpak CLI wrapper](flatpak.md#running-cli-commands).

## Cannot Connect To Bitcoin Core

Check that the node is running, synced, and reachable at the configured RPC
address. Use either cookie authentication or a username/password pair, not
both. See [backend setup](setup.md#bitcoin-core-recommended).

`RPC error -32601: Method not found` on `listwallets` usually means wallet
support is disabled. The descriptor backend needs a wallet-enabled node.
`bitcoin-cli listwallets` should return a JSON array, not a method error.
Do not expose RPC publicly to solve a connection problem.

## Neutrino TLS, Authentication, Or Sync Errors

Use an `https://` URL, the service's own TLS certificate, and its bearer token.
A certificate or token from another instance will not authenticate this one.
Check service logs for initial sync and compact-filter prefetch completion.
Historical lookups need the filter bodies as well as headers.
Follow [Neutrino setup](setup.md#neutrino-lightweight-alternative) or
[credential rotation](technical/neutrino-tls.md); do not disable verification.

## Tor Or Directory Connection Failed

Wait for Tor to finish bootstrapping. Check the SOCKS listener; makers also
need a control listener and permission to read its authentication cookie.
After a group-permission change, start a new login session.
See [Tor setup](setup.md#tor).

A reachable Tor service does not prove a directory is online. Repeated failures
against one directory can be remote; retain multiple configured directories.
Do not switch production traffic to clearnet or publish Tor credentials.

## Missing Balance Or Slow Sync

Check the selected wallet, network, and BIP39 passphrase first. An incorrect
passphrase derives a different wallet. For Bitcoin Core, inspect scan progress:

```bash
jm-wallet info --scan-status
```

Use the same wallet options as on the failing command. Imported wallets can
take hours to scan, and displayed balances may be incomplete until scanning
finishes. Wait for an active scan instead of starting another.

If a known address or balance remains missing after sync, compare the reported
time and address-index coverage with the original wallet's records. An explicit
`jm-wallet rescan` can repair missing coverage; choose `--scan-depth` only when
known used addresses lie beyond the scanned range. These are potentially
expensive operations, not routine startup steps. See
[recovery](recover-wallet.md#check-coverage-before-rescanning) and
[scan diagnostics](technical/wallet-scanning.md).

Fidelity bonds have separate recovery requirements. A regular wallet scan does
not prove all bonds were found. See [bond recovery](fidelity-bond-operations.md).

## Insufficient Funds Or No Eligible PoDLE UTXO

A displayed balance is not necessarily spendable for the proposed CoinJoin.
Check the source mixdepth, frozen coins, confirmations, amount, and fees.
By default the taker proof needs a UTXO with at least five confirmations and
at least 20% of the CoinJoin amount. Repeated authentication attempts can also
use up the available proofs for a coin. Read the reported reason before retrying;
do not lower proof requirements as a workaround.

## CoinJoin Failed Or Stopped

First establish whether a transaction was broadcast. Check the reported
transaction ID through your own backend, `jm-wallet history`, and the current
wallet state. A terminal timeout or missing confirmation does not prove that
nothing was sent. Avoid a duplicate payment.

If no transaction was broadcast, inspect the cause: insufficient eligible coins,
too few offers within your fee limits, or a connection failure. Wait for new
offers or fix that cause before retrying. Do not blindly increase fee limits.
For a saved tumble, use the [explicit resume procedure](README-tumbler.md#resume-a-failed-plan).

If a maker refuses to sign because the mining fee is too low, normal INFO logs
show the proposed fee rate and the maker's required minimum in sat/vB. The maker
sends that reason to the taker, which also displays it at INFO level. Review the
reported minimum and your mining-fee settings before starting another round.
A definite refusal before signing records the attempt as failed, rather than
pending, on the maker and on the taker when it receives the refusal. Revealed
addresses remain protected against reuse, and an honest refusal does not add
the maker to the taker's ignored list.

## Maker Is Online But Earns Nothing

Takers choose when to transact and which offers to use. Being connected does
not guarantee selection or profit. Check that funded offers were published,
your fee and size limits, and any bond status. Avoid restarting an otherwise
healthy maker just because it has not been selected.

## Release Verification Failed

Do not bypass signature or dependency-hash checks. A release may be incompletely
published; wait and retry the saved updater. If failure persists, check the
[release verification guidance](install-advanced.md#supply-chain-security) and
report the exact error without credentials.

## Report A Problem

Record the command name, expected result, actual error, installation method,
network, and backend. Collect a diagnostic report:

```bash
jm-wallet debug-info
```

The report is designed to omit keys, addresses, balances, and transactions.
Review it before sharing. Logs, screenshots, and configuration files may contain
sensitive information that this command's redaction does not cover. Never send
recovery words, passphrases, wallet files, RPC credentials, or Neutrino tokens.
See [logging settings](technical/configuration.md#logging) for how verbosity and
wallet diagnostic details are controlled.

Search [existing issues](https://github.com/joinmarket-ng/joinmarket-ng/issues)
before filing a report. For a vulnerability, use
[private vulnerability reporting](https://github.com/joinmarket-ng/joinmarket-ng/security/advisories/new)
and follow the [security policy](https://github.com/joinmarket-ng/joinmarket-ng/security/policy).
