# Run A CoinJoin

A taker selects makers, coordinates one CoinJoin, and pays their fees plus the
Bitcoin mining fee. A successful transaction does not guarantee anonymity.

## Before You Start

Complete [wallet setup](getting-started.md), wait for eligible confirmed funds,
and keep your Bitcoin backend and Tor running. Stop other processes spending
from this wallet. Check `jm-wallet info` before choosing an amount.

The amount is in **satoshis**, not BTC. Leave room for fees. The default
authentication proof needs an eligible UTXO with at least five confirmations
and a value of at least 20% of the CoinJoin amount.

## Quick Start

Replace `AMOUNT_SATS` with the amount you intend to CoinJoin:

```bash
jm-taker coinjoin --amount AMOUNT_SATS --destination INTERNAL
```

Review the maker and fee preview, then the final transaction fee confirmation.
Do not use `--yes` for your first run. If the preview expires, start again to
obtain current offers.

`INTERNAL` sends the equal output to the next mixdepth in your wallet. Change
stays in the source mixdepth. After broadcast, wait for confirmation and check
`jm-wallet info` and `jm-wallet history`. If the process stops unexpectedly,
[check for a broadcast transaction before retrying](troubleshooting.md#coinjoin-failed-or-stopped).

## Common Use Cases

To choose the source compartment, add `--mixdepth N`. To pay another wallet,
replace `INTERNAL` with a fresh destination address that you have checked on
the correct network. External payments reveal information to the recipient.

A sweep CoinJoins the selected mixdepth's eligible funds, less fees, without
creating taker change:

```bash
jm-taker coinjoin --amount 0 --mixdepth 2 --destination INTERNAL
```

A sweep links the inputs it spends. It is not automatically the best privacy
choice. For manual input selection, use `--select-utxos` with an explicit
`--amount`; omitting the amount sweeps the selection. Selection is limited to
one mixdepth. `--input-utxo TXID:VOUT` selects exact inputs without an interactive
menu; repeat it for each input and specify the source mixdepth.

## Configuration Notes

Set fee limits in `[taker]` in your configuration. Consult the installed
`config.toml.template` for exact settings and defaults, and
`jm-taker coinjoin --help` for per-run overrides. The
[configuration reference](technical/configuration.md) explains precedence.

Maker fees and mining fees are separate costs. More participants and inputs
usually make a larger transaction. Additional equal outputs do not represent
a guaranteed anonymity set. If there are too few suitable offers, wait or
review your amount and budget rather than immediately weakening limits.

The default offer filter selects fees on a shared public grid. See the
[fee quantization discussion and paper](https://github.com/joinmarket-ng/joinmarket-ng/issues/508)
for background; the installed settings reference describes current behavior.
Fee rounding and equalization are optional policies that may be incompatible
with older makers. Keep the defaults unless you understand the compatibility
and cost implications. `tx_fee_factor` adds fee-rate randomization: `0.2`
allows a rate between the base rate and 1.2 times that rate, not 0.2 times it.

## Ignored Makers

Previously problematic makers are avoided when enough alternatives exist, but
the persisted ignored list is a preference, not a permanent ban. Inspect the
reported failure before using `jm-taker clear-ignored-makers` to reset it.

## Tumbler

For a sequence of CoinJoins with several destinations, use the
[tumbler guide](README-tumbler.md). Repeating one command is not a substitute
for considering the entire spending path.

## Docker Deployment

Container operators should use the
[taker Compose configuration](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/taker/docker-compose.yml)
and its environment settings. Do not assume a native installation's paths or
credentials apply inside a container.

## Command Reference

Use `jm-taker --help` or `jm-taker coinjoin --help` for the installed version.
The [component reference](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/taker/README.md)
contains generated command help for the source checkout.
