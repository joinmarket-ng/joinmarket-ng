# JoinMarket Tumbler

High-level CoinJoin scheduler for joinmarket-ng.

This README is the short practical entry point. For user-facing concepts and
privacy rationale, see [`docs/README-tumbler.md`](../docs/README-tumbler.md).
For implementation details, see
[`docs/technical/tumbler-redesign.md`](../docs/technical/tumbler-redesign.md).

## Quick Start

Build a plan:

```bash
jm-tumbler plan \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --destination bc1qdest1... \
  --destination bc1qdest2... \
  --destination bc1qdest3...
```

Inspect it:

```bash
jm-tumbler status --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

Run it:

```bash
jm-tumbler run --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

Delete it:

```bash
jm-tumbler delete --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

Notes:

- Use at least three destination addresses for real tumbles.
- Progress is persisted to `plan.yaml`, so `jm-tumbler run` can resume.
- While a tumble is active, `jmwalletd` blocks manual taker/maker operations on
  the same wallet.

## Development

Install editable packages:

```bash
pip install -e jmcore -e jmwallet -e taker -e maker
pip install -e tumbler[dev]
```

Run tests:

```bash
pytest tumbler/tests
```

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
│    --data-dir                              PATH             Data directory   │
│                                                             (default:        │
│                                                             ~/.joinmarket-ng │
│                                                             or               │
│                                                             $JOINMARKET_DAT… │
│                                                             [env var:        │
│                                                             JOINMARKET_DATA… │
│    --config-file                           PATH             Config file path │
│                                                             (decoupled from  │
│                                                             data dir).       │
│                                                             Defaults to      │
│                                                             <data-dir>/conf… │
│                                                             [env var:        │
│                                                             JOINMARKET_CONF… │
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
│ --data-dir                         PATH  Data directory (default:            │
│                                          ~/.joinmarket-ng or                 │
│                                          $JOINMARKET_DATA_DIR)               │
│                                          [env var: JOINMARKET_DATA_DIR]      │
│ --config-file                      PATH  Config file path (decoupled from    │
│                                          data dir). Defaults to              │
│                                          <data-dir>/config.toml              │
│                                          [env var: JOINMARKET_CONFIG_FILE]   │
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
│ --data-dir                         PATH  Data directory (default:            │
│                                          ~/.joinmarket-ng or                 │
│                                          $JOINMARKET_DATA_DIR)               │
│                                          [env var: JOINMARKET_DATA_DIR]      │
│ --config-file                      PATH  Config file path (decoupled from    │
│                                          data dir). Defaults to              │
│                                          <data-dir>/config.toml              │
│                                          [env var: JOINMARKET_CONFIG_FILE]   │
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
│                                                       gating). Defaults to   │
│                                                       the                    │
│                                                       tumbler.min_confirmat… │
│                                                       setting (6).           │
│ --counterparties                INTEGER RANGE         Override the           │
│                                 [1<=x<=20]            counterparty count for │
│                                                       every phase at         │
│                                                       runtime. Useful when   │
│                                                       the configured count   │
│                                                       is unavailable on the  │
│                                                       chosen network.        │
│ --resume                                              Resume a plan that     │
│                                                       ended in a terminal    │
│                                                       state (FAILED,         │
│                                                       CANCELLED, or          │
│                                                       stuck-RUNNING).        │
│                                                       Completed phases are   │
│                                                       kept; all other phases │
│                                                       are reset to PENDING   │
│                                                       and the runner picks   │
│                                                       up at the first        │
│                                                       non-completed phase.   │
│                                                       Has no effect on a     │
│                                                       plan that is already   │
│                                                       PENDING.               │
│ --data-dir                      PATH                  Data directory         │
│                                                       (default:              │
│                                                       ~/.joinmarket-ng or    │
│                                                       $JOINMARKET_DATA_DIR)  │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --config-file                   PATH                  Config file path       │
│                                                       (decoupled from data   │
│                                                       dir). Defaults to      │
│                                                       <data-dir>/config.toml │
│                                                       [env var:              │
│                                                       JOINMARKET_CONFIG_FIL… │
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
│ --data-dir     -d      PATH  Data directory for JoinMarket files             │
│                              [env var: JOINMARKET_DATA_DIR]                  │
│ --config-file          PATH  Config file path (decoupled from data dir).     │
│                              Defaults to <data-dir>/config.toml              │
│                              [env var: JOINMARKET_CONFIG_FILE]               │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-tumbler -->
