# Run A Maker

A maker keeps liquidity online and offers to participate in other users'
CoinJoins for a fee. Selection and earnings are not guaranteed. The running
process can sign transactions, so treat the host as a hot-wallet system.

## Prerequisites

Complete [wallet setup](getting-started.md), back up the recovery material,
and keep the backend and [Tor SOCKS and control services](setup.md#tor) running.
Do not run another spender against the same wallet.

There is no universal minimum wallet balance. Eligible coins, your offer limits,
and taker demand determine whether your wallet can participate.

## Quick Start

```bash
jm-wallet info
jm-maker start
```

The maker syncs, publishes offers for eligible funds, and waits for takers.
Check that the logs show funded offers and directory connections, not just a
running process. Use Ctrl-C to request a clean shutdown and let it finish.

By default, fresh deposits and deposit-derived change in mixdepth 0 are kept out
of maker rotation. A wallet funded only there may need an
[internal taker CoinJoin](README-taker.md) before it has maker liquidity.
Recovered wallets may also lack the history needed to prove eligibility. Do not
disable this safeguard simply because the displayed balance exceeds an offer.

## Set Your Fees

Use `[maker]` in `config.toml` for lasting settings, or `jm-maker start --help`
for one-run overrides. Choose the fee model and offer size limits deliberately;
there is no fee setting that guarantees profit.

The defaults use shared fee bands to reduce distinctive pricing. Unusual or
randomized fees can fingerprint your maker and be excluded by takers that
require those bands. The [fee quantization discussion and paper](https://github.com/joinmarket-ng/joinmarket-ng/issues/508)
explain the evidence and proposed mitigations. Consult the installed `config.toml.template` before
changing fee or mixdepth-selection policies. The
[configuration guide](technical/configuration.md) explains precedence.

## Fidelity Bonds

A bond can make an offer more likely to be selected, but locks funds until its
expiry and publicly links the bond to the maker. It is optional, not a startup
requirement. Read [fidelity bond operations](fidelity-bond-operations.md) before
locking funds; external-signing bonds require separate recovery material.

For experimental delegated or externally owned bonds and the separate
offline-owner market authorization flow, see the
[Experimental Credential Market](credential-market.md).

## Migration From JoinMarket Reference

The network protocol is compatible; wallet files and configuration are not
interchangeable. Follow [recovery and migration](recover-wallet.md), compare
addresses and all mixdepth balances, and recover existing bonds before starting
the maker. Retain the original wallet backup and do not run both copies.

## Running As A Service

First establish that interactive startup works. Then follow
[unattended maker operation](maker-service.md). Auto-start requires access to
wallet decryption credentials without a terminal prompt; storing them on the
host changes the protection offered by wallet-file encryption.

## Logs

Watch connection, offer, and transaction errors. Check `jm-wallet history` for
completed activity and `jm-wallet info` for the current balance. With systemd,
use `journalctl -u jm-maker -f`. Review logs for private data before sharing.

For a maker with no earnings or repeated failures, use
[troubleshooting](troubleshooting.md#maker-is-online-but-earns-nothing).

## Multiple Local Instances

Give each maker its own wallet and data directory. Pass its `--data-dir` and
`--mnemonic-file` consistently to wallet and maker commands; do not run the same
seed as independent makers. See the
[component reference](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/maker/README.md#multiple-local-instances)
for an example.

## Docker Deployment

Use the [maker Compose configuration](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/maker/docker-compose.yml)
for container-specific paths, networking, and restart policy.

## Command Reference

`jm-maker start --help` describes the installed version's options. Generated
help also lives in the [component reference](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/maker/README.md).
