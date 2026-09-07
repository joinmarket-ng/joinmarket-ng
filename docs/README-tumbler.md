# Run The Tumbler

The tumbler schedules several CoinJoins, waits, and optional maker sessions,
then sends funds to several destination addresses. It is a long-running wallet
operation, not an instant payment service.

## Prerequisites

- A backed-up, funded wallet with eligible confirmed coins.
- A running Bitcoin backend and Tor, including Tor control for maker sessions.
- At least three fresh destination addresses on the correct network, under
  your control. Do not use expiring payment requests.
- Time for the plan to run and a fee budget you have reviewed.

Use the complete installation profile, which includes `jm-tumbler`. Stop other
makers, takers, and direct sends using the wallet before running a plan.

## Using The Tumbler

### Build A Plan

Replace the wallet path and all three destination placeholders:

```bash
jm-tumbler plan \
  --mnemonic-file /path/to/wallet.mnemonic \
  --destination DESTINATION_1 \
  --destination DESTINATION_2 \
  --destination DESTINATION_3
jm-tumbler status --mnemonic-file /path/to/wallet.mnemonic
```

Planning does not broadcast transactions. Review the destinations, source
mixdepths, schedule, and fee estimate before running. Estimates are not a
promise of exact final costs or payouts.

### Run The Plan

```bash
jm-tumbler run --mnemonic-file /path/to/wallet.mnemonic
```

With Neutrino, supply `--fee-rate RATE` in sat/vB explicitly. Use a rate and fee
limits you accept; do not copy an arbitrary rate from an example. Settings in
`[tumbler]` and run options are described by your `config.toml.template` and
`jm-tumbler run --help`.

### Resume A Failed Plan

To stop, press Ctrl-C and let the current phase finish. Keep the saved plan.
An interrupted, failed, or stale-running plan requires explicit resumption:

```bash
jm-tumbler status --mnemonic-file /path/to/wallet.mnemonic
jm-tumbler run --mnemonic-file /path/to/wallet.mnemonic --resume
```

Check the wallet's recent transactions and resolve the reported failure first.
Resumption retains completed phases and retries unfinished ones. With Neutrino,
provide the fee rate again. Do not run a second copy to work around a stuck run.

### Cancel Or Restart

`jm-tumbler delete --mnemonic-file /path/to/wallet.mnemonic` removes a saved plan,
not transactions already broadcast. Stop the running process and inspect the
wallet before deleting or replacing a plan. Deletion loses its resume record.

## Why A Tumbler? Privacy Rationale

Amounts, timing, change, and later spending can link activity even after a
CoinJoin. Multiple transactions, waits, roles, and destinations aim to reduce
these clues; they do not guarantee anonymity or make a known total disappear.

The production workflow requires at least three destinations. Fewer destinations
make matching incoming and outgoing totals easier. Do not use the testing-only
override with funds whose privacy matters, and do not immediately recombine
the payouts. See [privacy practices](technical/best-practices.md).

## Fees And Safety

Each taker phase pays maker and mining fees, including internal CoinJoins.
Review the total plan estimate, not just one transaction's cost. Shortening
waits or removing maker sessions changes the strategy, not just its duration.
Maker selection and earnings are not guaranteed.

## Concurrency With jmwalletd

The daemon blocks manual sends and maker/taker starts while its tumble runs.
Do not rely on this protection across independent CLI processes or wallet copies.

## Command Reference

Use `jm-tumbler --help` and each subcommand's `--help` for current options.
The [component reference](https://github.com/joinmarket-ng/joinmarket-ng/blob/main/tumbler/README.md)
contains generated help. Contributors can consult the
[scheduler design](technical/tumbler-redesign.md).
