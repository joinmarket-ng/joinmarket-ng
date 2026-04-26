# JoinMarket Tumbler

High-level CoinJoin scheduler that mixes a wallet across one or more
destinations by interleaving taker CoinJoins with optional maker sessions.
Each plan is persisted as YAML so long-running schedules can be inspected,
paused, or resumed.

Three or more destination addresses are crucial for privacy. If coins enter
the tumbler as X sats and leave to only one or two destinations as roughly
X minus fees, that final amount pattern becomes a strong fingerprint that can
undo much of the privacy gained inside the tumble. The CLI therefore treats
fewer than three destinations as a development/testing-only mode.

See [Installation](install.md) for backend setup, Tor configuration, and manual
install. For a short command-oriented guide, see
[`../tumbler/README.md`](../tumbler/README.md).

## Concepts

A **plan** is a list of ordered **phases**. Each phase is one of:

- `TakerCoinjoinPhase`: a single CoinJoin that advances funds across mixdepths
  or toward a destination address.
- `MakerSessionPhase`: runs a maker bot for a bounded window, so the wallet
  alternates between "taker" and "maker" signatures on-chain. The taker still
  chooses the equal CoinJoin amount for that transaction; as a maker we do not
  get to force our change to match the taker's change, we simply receive
  whatever maker-side change falls out from our selected inputs and the taker-
  chosen CoinJoin amount.

Plans are stored under `$JOINMARKET_DATA_DIR/tumbler/<wallet>/plan.yaml`. The
same file is shared with `jmwalletd`, so a plan started from the CLI can be
inspected (and vice versa) via the `/tumbler` HTTP endpoints.

## Prerequisites

- A funded joinmarket wallet (mnemonic file).
- A working Bitcoin backend (`descriptor_wallet` or `neutrino`).
- Tor for production use (maker phases rely on it).

## Using the tumbler

### Build a plan

```bash
jm-tumbler plan \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --destination bc1qdest1... \
  --destination bc1qdest2... \
  --destination bc1qdest3...
```

This inspects the wallet, picks reasonable defaults for maker counts and
timings, and writes `plan.yaml`. Inspect it:

```bash
jm-tumbler status --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

### Run the plan

```bash
jm-tumbler run --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

The runner executes phases in order, persists progress after every transition,
and exits cleanly on `SIGINT`/`SIGTERM` (progress is kept; resume by calling
`run` again).

### Cancel or restart

```bash
jm-tumbler delete --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

## Concurrency with jmwalletd

While a tumble is in progress, `jmwalletd` blocks manual taker and maker calls
(`/docoinjoin`, `/directsend`, `/startmaker`) to avoid colliding with the
scheduler. Requests return `409 Conflict` until the tumble finishes or is
stopped.

## Idle-timeout fallback

`MakerSessionPhase` accepts `idle_timeout_seconds`: if the maker is never
selected as a counterparty during that window, the phase exits gracefully as
completed. This prevents a tumble from stalling when no taker shows up.

## Why a tumbler? Privacy rationale

A single CoinJoin breaks the obvious link between an input and the equal-
amount output, but a chain analyst with patience can still re-aggregate
funds by following amount, timing, and address-reuse heuristics. A tumbler
defeats those heuristics by spreading the wallet's exit across many
CoinJoins, several mixdepths, and at least three external destinations.

The defaults are chosen to mirror the reference implementation's tumbler guide
so that joinmarket-ng plans are statistically indistinguishable from reference
plans on-chain. Concretely:

- **At least three destinations.** Two destinations let an observer
  pair-match outputs by elimination; three or more force genuine ambiguity.
  The CLI refuses fewer than three unless `--allow-few-destinations` is
  set (development only).
- **Multiple CJs per mixdepth before sweeping.** Each mixdepth ships out
  several fractional payouts and then a final sweep. A single sweep would
  expose the full balance and trivially re-link to it.
- **Random fractional amounts with a 5% floor.** Fractions are sampled
  uniformly from the "sorted knives" scheme and clamped to >= 0.05; the
  per-mixdepth sum is normalized to leave >= 0.05 for the trailing sweep.
  This guarantees at least one sweep transaction (which empties UTXO
  metadata) without producing dust-sized payouts.
- **Significant-figure rounding (default 25% of phases).** Non-sweep CJ
  amounts are rounded to a random number of significant figures
  (1-5, weighted toward 4) so the sat-precise wallet balance does not
  leak through to the chain. Disable with `rounding_chance=0.0`.
- **Maker rounds between taker phases.** When `--maker-sessions` is on
  (default), every taker mixdepth is preceded by a bounded maker session.
  This alternates the wallet's on-chain signature between "taker" and
  "maker" roles so timing-correlation against orderbook activity is
  much harder.
- **Long, randomized waits.** The default `time_lambda_seconds=21600`
  (six hour mean, exponentially distributed) plus a 3x multiplier on
  stage-1 sweeps matches the reference defaults. Tuning this much lower
  largely defeats the timing-correlation defence.
- **Stage-1 cleavage.** The plan splits naturally into "stage 1" (sweep
  every funded mixdepth out of its starting UTXO set, breaking the link
  to pre-tumble history) and "stage 2" (drain the resulting internal
  balances out to the external destinations). Stage-1 phases get the
  longer wait by design.
- **Non-overlapping makers.** Within one tumble, the runner remembers
  which counterparty nicks were used in the previous phase and excludes
  them from the next maker selection. A coordinated set of malicious
  makers cannot trivially intersect across phases.

If you reduce any of these knobs (smaller waits, fewer destinations,
disabled maker sessions, disabled rounding) you trade real privacy for
speed. The plan estimator (`jm-tumbler plan` output) prints the resulting
worst-case fees so you can see what you are paying for.

## What a plan looks like

A typical tumble has two stages:

- Stage 1 sweeps each funded mixdepth internally first. This is the step that
  breaks the direct link to the wallet's original UTXO set.
- Stage 2 alternates optional maker sessions with taker CoinJoins that move
  funds toward the final destinations.

For a wallet funded in two mixdepths and three destinations, a plan often looks
roughly like this:

| Phase | Kind | Mixdepth | Destination | Purpose |
| --- | --- | --- | --- | --- |
| 0 | taker | 1 | INTERNAL | Stage-1 sweep |
| 1 | taker | 0 | INTERNAL | Stage-1 sweep |
| 2 | maker | 1 | n/a | Role mixing |
| 3 | taker | 1 | INTERNAL | Fractional payout |
| 4 | taker | 1 | INTERNAL | Sweep forward |
| 5 | maker | 2 | n/a | Role mixing |
| 6 | taker | 2 | INTERNAL | Fractional payout |
| 7 | taker | 2 | bc1qdest1... | Final payout |
| 8 | maker | 3 | n/a | Role mixing |
| 9 | taker | 3 | INTERNAL | Fractional payout |
| 10 | taker | 3 | bc1qdest2... | Final payout |
| 11 | maker | 4 | n/a | Role mixing |
| 12 | taker | 4 | INTERNAL | Fractional payout |
| 13 | taker | 4 | bc1qdest3... | Final payout |

The exact shape varies with wallet balances, destination count, seed, and
whether maker sessions are enabled.

## Example payout shape

Suppose the wallet starts with 0.50000000 BTC, the plan estimator reports a
worst-case total cost of 0.00265800 BTC, and the three destination-bearing
stage-2 sweeps eventually land like this:

| Destination | Received amount |
| --- | --- |
| `bc1qdest1...` | 0.17340000 BTC |
| `bc1qdest2...` | 0.16195000 BTC |
| `bc1qdest3...` | 0.16199200 BTC |

Those payouts sum to 0.49734200 BTC, which is exactly the starting 0.50000000
BTC minus the 0.00265800 BTC worst-case fees from the estimator. In a real run,
the actual split and actual fees vary with sampled fractions, maker selection,
and the prevailing fee rate, but this is the shape to expect: several unequal
final sweeps whose total matches the wallet balance minus fees.

## Fees and safety

Three cost components contribute to the worst case:

1. **Counterparty (maker) fees.** Each taker CJ pays each counterparty
   either an absolute fee (`max_cj_fee_abs`) or a fraction of the CJ
   amount (`max_cj_fee_rel`), whichever is larger. The plan estimator
   uses these caps as an upper bound; actual fees depend on the orders
   selected at runtime.
2. **Miner fees.** Estimated per phase from the same coarse vsize
   model the taker uses for its own prompts: ~68 vB per P2WPKH input,
   ~31 vB per P2WPKH output, plus ~11 vB fixed overhead. If `--block-target`
   is set the runner asks the backend for an
   estimate; otherwise `--fee-rate` applies; otherwise the estimator
   falls back to 10 sat/vB and labels the source as `fallback`.
3. **Tumbler-internal CJs.** Stage 1 doesn't reach an external
   destination - it only cleaves the pre-tumble link - but those CJs
   still cost both maker and miner fees. They are included in the
   estimate.

Built-in safety properties:

- The estimator's reported total balance equals the configured per-
  mixdepth balance map (no silent field drift).
- The worst-case total fee never exceeds the starting balance: a tumble
  cannot drain the wallet to the protocol.
- Every external destination receives at least one payout in every plan,
  regardless of seed.
- Fractional amounts always sum to less than 1.0 with a 0.05 reserve, so
  every mixdepth ends with a sweep transaction.

If a phase fails (network blip, fee-rate spike rejecting the bid), the
runner retries up to `max_phase_retries` (default 3) before marking the
plan failed. The YAML file is updated on every transition, so a crashed
process can be resumed by re-running `jm-tumbler run`.

## Configuration

Tumbler defaults live in `config.toml` under the `[tumbler]` section. Run
`jm-tumbler config-init` to write a copy of `config.toml.template` to your data
directory; relevant keys are documented inline there. Per-plan knobs such as
`--maker-count-min`, `--mincjamount-sats`, `--maker-sessions/--no-maker-sessions`,
and `--seed` override the config for one invocation only.

## Design notes

See [`technical/tumbler-redesign.md`](technical/tumbler-redesign.md) for
architecture, persistence, retry behavior, and other implementation details.
