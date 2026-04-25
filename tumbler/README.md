# JoinMarket Tumbler

High-level CoinJoin scheduler for joinmarket-ng. Plans a role-mixed tumble
across destinations and persists progress to a human-readable YAML file so
that long-running schedules survive restarts.

## Features

- **Role-mixed schedules**: interleaves taker CoinJoins, maker sessions, and
  bondless taker bursts to diversify on-chain signatures.
- **YAML persistence**: each plan is stored as a readable `plan.yaml` that can
  be inspected, resumed, or cancelled.
- **Concurrency-safe**: the runner coordinates with `jmwalletd` so manual
  taker/maker operations are blocked while a tumble is in progress.
- **Idle-timeout fallback**: maker phases exit gracefully when no CoinJoin is
  served within a configurable window, preventing indefinite waits.
- **CLI and HTTP**: drive the scheduler standalone via `jm-tumbler`, or through
  the tumbler endpoints exposed by `jmwalletd`.

## Documentation

For full documentation, see
[tumbler Documentation](https://joinmarket-ng.github.io/joinmarket-ng/README-tumbler/).

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

## Install (editable)

```
pip install -e jmcore -e jmwallet -e taker -e maker
pip install -e tumbler[dev]
```

## Tests

```
pytest tumbler/tests
```

## Design notes

See [`docs/technical/tumbler-redesign.md`](../docs/technical/tumbler-redesign.md)
for the full design.
