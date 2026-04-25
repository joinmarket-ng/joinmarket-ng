# Tumbler

The `tumbler` package builds and runs multi-phase CoinJoin plans that move
funds from a wallet's funded mixdepths to a list of destination addresses
while obscuring the link between origin and destination UTXOs.

It is consumed as a library and via a small CLI; nothing in `tumbler`
depends on `jmwalletd`. The HTTP daemon is one possible host, the CLI is
another, and a third-party caller can drive a `Plan` directly from Python.

## Problem

A wallet whose origin is publicly visible — a KYC'd buy, a reused
address, an observed UTXO graph — is hard to spend privately. A single
CoinJoin already breaks naive co-spending heuristics, but two analytical
signals survive a simple taker-only loop:

- **Subset-sum recovery.** A taker's input set must satisfy
  `sum(inputs) = cj_amount + change + cj_fees + tx_fee`. With non-equal
  CoinJoin outputs that constraint is well below the Lagarias–Odlyzko
  density bound, so an observer can recover the taker's true input set
  from the joined input pool with off-the-shelf subset-sum solving.
- **Role fingerprint.** A wallet that is *always* a taker has a
  different on-chain footprint than one that alternates roles: fees flow
  one way, UTXO ages drift one way, and the timing of activity is
  taker-driven. A long taker-only run is a worse mix than a shorter
  mixed-role one.

`tumbler` addresses both signals by chaining taker CoinJoins with
bounded maker sessions inside a single plan, and persists the plan as
human-readable YAML so a crashed daemon (or a curious operator) can
resume from a known state.

## Architecture

`tumbler` exposes three public symbols:

- `Plan` — a Pydantic model describing the full tumble: parameters,
  destinations, an ordered list of phases, and per-phase state.
- `PlanBuilder` — builds a `Plan` from per-mixdepth balances, a
  destination list, and `PlanParameters`.
- `TumbleRunner` — consumes a `Plan` and drives it to completion,
  persisting after every state transition.

```
caller (CLI, jmwalletd, library user)
   |
   v
TumbleRunner  --->  taker.Taker.do_coinjoin   (per CoinJoin)
   |          \-->  maker.MakerBot.start/stop  (per maker session)
   v
jmwallet.WalletService
```

No other package imports anything else from `tumbler`. There is no
network code, no new crypto, and no protocol-level change to JoinMarket
CoinJoin.

### Phases

A `Plan` is an ordered list of phases. The runner executes them one at a
time; a phase completes (or fails) before the next begins. Two phase
kinds are supported:

- `taker_coinjoin` — a single CoinJoin as taker. Carries `mixdepth`,
  `amount_sats` (with `0` meaning sweep), `destination` (an external
  address or the literal `"INTERNAL"`), `counterparty_count`, and an
  optional `rounding` digit count.
- `maker_session` — runs `MakerBot` for a bounded window or until a
  target number of CoinJoins have been served, whichever comes first.
  Carries `maker_session_seconds`, `maker_session_idle_timeout_seconds`,
  and an offer template (ordertype, minsize, fees).

Phases are separated by an `asyncio.sleep` sampled from an exponential
distribution with mean `time_lambda_seconds`. The first phase of any
plan is a `taker_coinjoin` sweep with `destination=INTERNAL`: every
funded mixdepth's UTXOs go through a CoinJoin before any external
destination is touched.

### Plan layout

A typical plan has two stages:

- **Stage 1 — origin cleavage.** For each non-empty mixdepth, in
  descending order, append a `taker_coinjoin` sweep into the next
  mixdepth with `destination=INTERNAL`. This step alone is what makes
  the resulting wallet hard to link to the original UTXO set.
- **Stage 2 — role-mixed body.** For each mixdepth on the way to the
  external destination, optionally insert a `maker_session`, then
  append `mintxcount - 1` fractional taker CoinJoins, then a final
  taker sweep. The very last sweep in stage 2 targets the user's
  destination address; every other sweep targets `INTERNAL`.

Maker sessions are inserted only when `include_maker_sessions=True`.
Without them the plan reduces to a pure taker chain similar in spirit
to the reference tumbler.

### Subset-sum mitigation

A maker session sits between two taker phases. The session consumes
UTXOs selected by *other* takers — subsets we did not control — and
creates new CoinJoin output and change outputs matched to other
participants' amounts. By the time the next taker phase fires, the
wallet's UTXO graph has new points whose linkage back to the pre-maker
set is mediated by taker-chosen subsets, which raises the cost of
subset-sum recovery from "off-the-shelf solver" to "simulate or
participate in the tumble."

This does not close the subset-sum vulnerability completely; closing
it requires protocol-level changes (standard denominations, or
taker-matches-maker-change). The mitigation composes cleanly with any
future protocol fix.

### Plan persistence (YAML)

A plan is stored at `<data_dir>/schedules/<walletname>.yaml`, one file
per wallet, overwritten in place on every state transition. There is no
database, no WAL, and no journal; the YAML file is the full source of
truth.

```yaml
plan_id: 01HP5K9K2H3XR0QW0A1B2C3D4E
wallet_name: alice.jmdat
created_at: 2026-04-22T12:34:56Z
updated_at: 2026-04-22T12:41:03Z
destinations:
  - bcrt1q...abc
parameters:
  maker_count_min: 5
  maker_count_max: 9
  mintxcount: 2
  time_lambda_seconds: 180
  include_maker_sessions: true
  max_phase_retries: 3
  seed: 8f3a2b6e7c1d4f59
phases:
  - index: 0
    kind: taker_coinjoin
    status: completed
    mixdepth: 0
    amount_sats: 0
    destination: INTERNAL
    counterparty_count: 7
    started_at: 2026-04-22T12:34:56Z
    finished_at: 2026-04-22T12:38:02Z
    txid: 9ab4...c7
    attempt_count: 0
  - index: 1
    kind: maker_session
    status: pending
    maker_session_seconds: 21600
    maker_session_idle_timeout_seconds: 1800
    offer:
      ordertype: sw0reloffer
      minsize: 500000
      cjfee_r: "0.0002"
      cjfee_a: 1000
      txfee: 0
    attempt_count: 0
  - index: 2
    kind: taker_coinjoin
    status: pending
    mixdepth: 1
    amount_sats: 0
    destination: bcrt1q...abc
    counterparty_count: 7
    attempt_count: 0
current_phase: 1
status: running
```

Phase `status` is one of `pending | running | completed | failed |
cancelled`. On startup the runner reloads the file: if `current_phase`
points to a `running` phase it resumes that phase, otherwise it
continues with the next `pending` one.

YAML is chosen over JSON for legibility — operators open this file with
an editor during support — and over TOML for clean nesting of phase
records with heterogeneous schemas per `kind`.

### Failure handling and retries

Each taker phase has a retry budget given by
`PlanParameters.max_phase_retries` (default 3, range 0–20). When a
taker phase fails the runner increments `attempt_count`, applies a
tweak inspired by the reference `tweak_tumble_schedule`, persists, and
retries:

- `counterparty_count` is reduced by one toward
  `parameters.maker_count_min`. The minimum is honoured; the runner
  will not retry below it.
- If the phase originally targeted an external destination, the
  destination is rewritten to `"INTERNAL"`. The retry happens at the
  same mixdepth and a later phase is responsible for actually paying
  the destination — typically the next per-mixdepth block.

When `attempt_count` reaches `max_phase_retries` the phase remains
`failed` and the whole plan transitions to `failed`. A failed maker
session is not retried; the runner proceeds to the next phase.

A crashed `jmwalletd` resumes from the persisted plan. Worst case a
single phase is attempted twice, which for a taker phase means a
duplicate CoinJoin (extra fee cost only) and for a maker phase means a
short double-maker window.

## Toy example

```python
from pathlib import Path
from tumbler import Plan, PlanBuilder, PlanParameters, TumbleRunner

# Wallet has 2 BTC in mixdepth 0; everything else is empty.
balances = {0: 200_000_000, 1: 0, 2: 0, 3: 0, 4: 0}
destinations = ["bcrt1qexample..."]

params = PlanParameters(
    maker_count_min=5,
    maker_count_max=9,
    mintxcount=2,
    time_lambda_seconds=120.0,
    include_maker_sessions=True,
    max_phase_retries=3,
    seed=42,
)

plan: Plan = PlanBuilder(
    wallet_name="alice.jmdat",
    destinations=destinations,
    mixdepth_balances=balances,
    parameters=params,
).build()

# Persist and run. The runner is responsible for executing real CoinJoins
# and maker sessions through your taker / maker / wallet adapters.
runner = TumbleRunner(plan=plan, data_dir=Path("/tmp/jm-tumbler"), ...)
await runner.run()
```

For a 5-mixdepth wallet with funds only in mixdepth 0, no maker
sessions, and a single destination, the resulting plan has nine phases:
one stage-1 sweep and four stage-2 blocks (one fractional taker phase
plus one taker sweep per mixdepth).

The CLI `tumbler` driver is a thin wrapper around the same library
calls. It enforces a recommended minimum of three destinations to keep
the final-mixdepth payout from collapsing onto a single address; tests
and library callers can opt out via `--allow-few-destinations` (CLI)
or by passing a single destination directly to `PlanBuilder`.
