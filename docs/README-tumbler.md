# JoinMarket Tumbler

High-level CoinJoin scheduler that mixes a wallet across multiple destinations
by interleaving taker CoinJoins, maker sessions, and bondless taker bursts.
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
- `BondlessTakerBurstPhase`: a burst of small same-mixdepth CoinJoins with
  orderbook-matched rounding, used to add noise to the subset-sum signature
  of the funds.

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
│                                                             count per CJ     │
│                                                             [default: 5]     │
│    --maker-count-…                         INTEGER          Maximum          │
│                                                             counterparty     │
│                                                             count per CJ     │
│                                                             [default: 9]     │
│    --mincjamount-…                         INTEGER          Minimum CJ       │
│                                                             amount in sats   │
│                                                             [default:        │
│                                                             100000]          │
│    --maker-sessio…      --no-maker-ses…                     [default:        │
│                                                             maker-sessions]  │
│    --bondless-bur…      --no-bondless-…                     [default:        │
│                                                             bondless-bursts] │
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
│ *  --wallet-name  -w      TEXT  Wallet identifier [required]                 │
│    --log-level    -l      TEXT                                               │
│    --help                       Show this message and exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler delete --help</code></summary>

```

 Usage: jm-tumbler delete [OPTIONS]

 Delete the on-disk plan for ``wallet_name``.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --wallet-name  -w      TEXT  [required]                                   │
│    --yes          -y            Skip confirmation prompt                     │
│    --log-level    -l      TEXT                                               │
│    --help                       Show this message and exit.                  │
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
│ --min-confirmations             INTEGER               Confirmations required │
│                                                       before the next phase  │
│                                                       starts (0 disables     │
│                                                       gating)                │
│                                                       [default: 5]           │
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
