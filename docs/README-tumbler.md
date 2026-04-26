# JoinMarket Tumbler

High-level CoinJoin scheduler that mixes a wallet across one or more
destinations by interleaving taker CoinJoins with optional maker sessions.
Each plan is persisted as YAML so long-running schedules can be inspected,
paused, or resumed.

## Installation

Install JoinMarket-NG with the tumbler component (it depends on `taker` and
`maker`):

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --tumbler
```

See [Installation](install.md) for backend setup, Tor configuration, and
manual install.

## Concepts

A **plan** is a list of ordered **phases**. Each phase is one of:

- `TakerCoinjoinPhase`: a single CoinJoin that advances funds across mixdepths
  or toward a destination address.
- `MakerSessionPhase`: runs a maker bot for a bounded window, so the wallet
  alternates between "taker" and "maker" signatures on-chain.

Plans are stored under `$JOINMARKET_DATA_DIR/tumbler/<wallet>/plan.yaml`. The
same file is shared with `jmwalletd`, so a plan started from the CLI can be
inspected (and vice versa) via the `/tumbler` HTTP endpoints.

## Prerequisites

- A funded joinmarket wallet (mnemonic file).
- A working Bitcoin backend (`descriptor_wallet` or `neutrino`).
- Tor for production use (maker phases rely on it).

## Quick Start

### 1) Build a plan

```bash
jm-tumbler plan \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --destination bc1qdest1... \
  --destination bc1qdest2...
```

This inspects the wallet, picks reasonable defaults for maker counts and
timings, and writes `plan.yaml`. Inspect it:

```bash
jm-tumbler status --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

### 2) Run the plan

```bash
jm-tumbler run --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

The runner executes phases in order, persists progress after every transition,
and exits cleanly on `SIGINT`/`SIGTERM` (progress is kept; resume by calling
`run` again).

### 3) Cancel or restart

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

The defaults are chosen to mirror the reference implementation's
[Tumbler Guide](https://github.com/JoinMarket-Org/joinmarket-clientserver/blob/master/docs/tumblerguide.md)
so that joinmarket-ng plans are statistically indistinguishable from
reference plans on-chain. Concretely:

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
- **Long, randomized waits.** The default `time_lambda_seconds=3600`
  (one hour mean, exponentially distributed) plus a 3x multiplier on
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

## Worked example

A typical mainnet tumble with three destinations, a wallet holding
0.50 BTC across two mixdepths, on a node with `estimatesmartfee` available:

```bash
jm-tumbler plan \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --backend descriptor_wallet \
  --rpc-url http://user:pass@127.0.0.1:8332 \
  --destination bc1qdest1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --destination bc1qdest2xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --destination bc1qdest3xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The output summarises the plan and an upfront fee estimate:

```
plan: 18 phases (12 taker CJs, 6 maker sessions)
balance: 0.50000000 BTC across mixdepths {0: 30000000, 1: 20000000}
fee rate: 18 sat/vB (estimated)
worst-case maker fees: 0.00071400 BTC (0.14%)
worst-case miner fees: 0.00194400 BTC (0.39%)
worst-case total cost: 0.00265800 BTC (0.53%)
estimated runtime: 14h - 36h (mean, 90th percentile)
```

Then run it (this blocks the terminal; safe to `Ctrl-C` and resume):

```bash
jm-tumbler run \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --backend descriptor_wallet \
  --rpc-url http://user:pass@127.0.0.1:8332 \
  --directory <directory-onion-1>,<directory-onion-2>,<directory-onion-3>
```

While the tumble is in progress, `jmwalletd` returns `409 Conflict` for
manual taker/maker calls on the same wallet (see "Concurrency" above).

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

Built-in safety properties (verified by the test suite, see
`tumbler/tests/test_privacy_invariants.py`):

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

Tumbler defaults live in `config.toml` under the `[tumbler]` section.
Run `jm-tumbler config-init` to write a copy of `config.toml.template`
to your data directory; relevant keys are documented inline there.
Per-plan knobs (`--maker-count-min`, `--mincjamount-sats`,
`--maker-sessions/--no-maker-sessions`, `--seed`, ...) override the
config for one invocation only.

<!-- AUTO-GENERATED HELP START: jm-tumbler -->

<details>
<summary><code>jm-tumbler --help</code></summary>

```

 Usage: jm-tumbler [OPTIONS] COMMAND [ARGS]...

 JoinMarket tumbler - role-mixed CoinJoin schedules with YAML-persisted state

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --install-completion          Install completion for the current shell.      │
│ --show-completion             Show completion for the current shell, to copy │
│                               it or customize the installation.              │
│ --help                        Show this message and exit.                    │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ plan         Build a tumbler plan for the given destinations and persist it. │
│ status       Print the current plan for the given wallet.                    │
│ delete       Delete the on-disk plan for ``wallet_name``.                    │
│ run          Execute the saved plan for a wallet to completion.              │
│ config-init  Initialize the config file with default settings.               │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler plan --help</code></summary>

```

 Usage: jm-tumbler plan [OPTIONS]

 Build a tumbler plan for the given destinations and persist it.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --destination    -d                     TEXT             External         │
│                                                             destination      │
│                                                             address          │
│                                                             (repeatable)     │
│                                                             [required]       │
│    --mnemonic-file  -f                     PATH             Path to mnemonic │
│                                                             file             │
│    --prompt-bip39…                                          Prompt for BIP39 │
│                                                             passphrase       │
│                                                             interactively    │
│    --wallet-name    -w                     TEXT             Wallet           │
│                                                             identifier for   │
│                                                             the plan file;   │
│                                                             defaults to the  │
│                                                             mnemonic         │
│                                                             fingerprint      │
│    --network                               [mainnet|testne  Bitcoin network  │
│                                            t|signet|regtes                   │
│                                            t]                                │
│    --backend        -b                     TEXT             Backend type:    │
│                                                             scantxoutset |   │
│                                                             descriptor_wall… │
│                                                             | neutrino       │
│    --rpc-url                               TEXT             Bitcoin full     │
│                                                             node RPC URL     │
│                                                             [env var:        │
│                                                             BITCOIN_RPC_URL] │
│    --neutrino-url                          TEXT             Neutrino REST    │
│                                                             API URL          │
│                                                             [env var:        │
│                                                             NEUTRINO_URL]    │
│    --force                                                  Overwrite an     │
│                                                             existing pending │
│                                                             plan             │
│    --seed                                  INTEGER          Seed the plan    │
│                                                             builder RNG for  │
│                                                             reproducible     │
│                                                             schedules        │
│    --maker-count-…                         INTEGER          Minimum          │
│                                                             counterparty     │
│                                                             count per CJ;    │
│                                                             defaults to      │
│                                                             settings.taker.… │
│    --maker-count-…                         INTEGER          Maximum          │
│                                                             counterparty     │
│                                                             count per CJ;    │
│                                                             defaults to      │
│                                                             settings.taker.… │
│    --mincjamount-…                         INTEGER          Minimum CJ       │
│                                                             amount in sats   │
│                                                             [default:        │
│                                                             100000]          │
│    --maker-sessio…      --no-maker-ses…                     [default:        │
│                                                             maker-sessions]  │
│    --allow-few-de…                                          Override the     │
│                                                             recommended      │
│                                                             minimum of 3     │
│                                                             destinations.    │
│                                                             Intended for     │
│                                                             development and  │
│                                                             automated        │
│                                                             testing only:    │
│                                                             fewer            │
│                                                             destinations     │
│                                                             expose users to  │
│                                                             pairwise         │
│                                                             re-aggregation   │
│                                                             heuristics.      │
│    --log-level      -l                     TEXT                              │
│    --help                                                   Show this        │
│                                                             message and      │
│                                                             exit.            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler status --help</code></summary>

```

 Usage: jm-tumbler status [OPTIONS]

 Print the current plan for the given wallet.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --wallet-name              -w      TEXT  Wallet identifier; defaults to the  │
│                                          mnemonic fingerprint                │
│ --mnemonic-file            -f      PATH  Path to mnemonic file               │
│ --prompt-bip39-passphrase                Prompt for BIP39 passphrase         │
│                                          interactively                       │
│ --log-level                -l      TEXT                                      │
│ --help                                   Show this message and exit.         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler delete --help</code></summary>

```

 Usage: jm-tumbler delete [OPTIONS]

 Delete the on-disk plan for ``wallet_name``.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --wallet-name              -w      TEXT  Wallet identifier; defaults to the  │
│                                          mnemonic fingerprint                │
│ --mnemonic-file            -f      PATH  Path to mnemonic file               │
│ --prompt-bip39-passphrase                Prompt for BIP39 passphrase         │
│                                          interactively                       │
│ --yes                      -y            Skip confirmation prompt            │
│ --log-level                -l      TEXT                                      │
│ --help                                   Show this message and exit.         │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler run --help</code></summary>

```

 Usage: jm-tumbler run [OPTIONS]

 Execute the saved plan for a wallet to completion.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file         -f      PATH                  Path to mnemonic file  │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
│ --wallet-name           -w      TEXT                  Wallet identifier;     │
│                                                       defaults to the        │
│                                                       mnemonic fingerprint   │
│ --network                       [mainnet|testnet|sig                         │
│                                 net|regtest]                                 │
│ --backend               -b      TEXT                                         │
│ --rpc-url                       TEXT                  [env var:              │
│                                                       BITCOIN_RPC_URL]       │
│ --neutrino-url                  TEXT                  [env var:              │
│                                                       NEUTRINO_URL]          │
│ --directory             -D      TEXT                  [env var:              │
│                                                       DIRECTORY_SERVERS]     │
│ --tor-socks-host                TEXT                  Tor SOCKS host         │
│                                                       override               │
│ --tor-socks-port                INTEGER               Tor SOCKS port         │
│                                                       override               │
│ --fee-rate                      FLOAT                 Manual fee rate in     │
│                                                       sat/vB (mutually       │
│                                                       exclusive with         │
│                                                       --block-target).       │
│                                                       Required when the      │
│                                                       backend is neutrino.   │
│ --block-target                  INTEGER               Target blocks for fee  │
│                                                       estimation (mutually   │
│                                                       exclusive with         │
│                                                       --fee-rate). Not       │
│                                                       supported with the     │
│                                                       neutrino backend.      │
│ --min-confirmations             INTEGER               Confirmations required │
│                                                       before the next phase  │
│                                                       starts (0 disables     │
│                                                       gating)                │
│                                                       [default: 5]           │
│ --counterparties                INTEGER RANGE         Override the           │
│                                 [1<=x<=20]            counterparty count for │
│                                                       every phase at         │
│                                                       runtime. Useful when   │
│                                                       the configured count   │
│                                                       is unavailable on the  │
│                                                       chosen network.        │
│ --log-level             -l      TEXT                                         │
│ --help                                                Show this message and  │
│                                                       exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler config-init --help</code></summary>

```

 Usage: jm-tumbler config-init [OPTIONS]

 Initialize the config file with default settings.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --data-dir  -d      PATH  Data directory for JoinMarket files                │
│                           [env var: JOINMARKET_DATA_DIR]                     │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-tumbler -->

## Design notes

See [`technical/tumbler-redesign.md`](technical/tumbler-redesign.md) for the
full design document covering phase kinds, persistence format, runner state
machine, and interop with `taker` and `maker`.
