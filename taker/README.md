# JoinMarket Taker Client

Mix your bitcoin for privacy via CoinJoin. Takers initiate transactions and pay small fees to makers.

## Features

- **CoinJoin Initiation**: Start CoinJoin transactions with available makers
- **Schedule-based Mixing**: Tumbler mode for automated multi-round mixing
- **Destination Management**: Multiple output destinations for privacy
- **Fee Negotiation**: Automatic fee negotiation with makers
- **Transaction Monitoring**: Track transaction progress and confirmations
- **Retry Logic**: Automatic retry on failure or maker timeout

## Documentation

For full documentation, see [taker Documentation](https://joinmarket-ng.github.io/joinmarket-ng/README-taker/).

<!-- AUTO-GENERATED HELP START: jm-taker -->

<details>
<summary><code>jm-taker --help</code></summary>

```

 Usage: jm-taker [OPTIONS] COMMAND [ARGS]...

 JoinMarket Taker - Execute CoinJoin transactions

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ coinjoin              Execute a single CoinJoin transaction.                 │
│ tumble                Run a tumbler schedule of CoinJoins.                   │
│ clear-ignored-makers  Clear the list of ignored makers.                      │
│ config-init           Initialize the config file with default settings.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-taker coinjoin --help</code></summary>

```

 Usage: jm-taker coinjoin [OPTIONS]

 Execute a single CoinJoin transaction.

 Configuration is loaded from ~/.joinmarket-ng/config.toml (or
 $JOINMARKET_DATA_DIR/config.toml),
 environment variables, and CLI arguments. CLI arguments have the highest
 priority.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ *  --amount         -a                     INTEGER          Amount in sats   │
│                                                             (0 for sweep)    │
│                                                             [required]       │
│    --destination    -d                     TEXT             Destination      │
│                                                             address (or      │
│                                                             'INTERNAL' for   │
│                                                             next mixdepth)   │
│                                                             [default:        │
│                                                             INTERNAL]        │
│    --mixdepth       -m                     INTEGER          Source mixdepth  │
│                                                             [default: 0]     │
│    --counterparti…  -n                     INTEGER          Number of makers │
│    --mnemonic-file  -f                     PATH             Path to mnemonic │
│                                                             file             │
│    --prompt-bip39…                                          Prompt for BIP39 │
│                                                             passphrase       │
│                                                             interactively    │
│    --network                               [mainnet|testne  Protocol network │
│                                            t|signet|regtes  for handshakes   │
│                                            t]                                │
│    --bitcoin-netw…                         [mainnet|testne  Bitcoin network  │
│                                            t|signet|regtes  for addresses    │
│                                            t]               (defaults to     │
│                                                             --network)       │
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
│    --directory      -D                     TEXT             Directory        │
│                                                             servers          │
│                                                             (comma-separate… │
│                                                             [env var:        │
│                                                             DIRECTORY_SERVE… │
│    --tor-socks-ho…                         TEXT             Tor SOCKS proxy  │
│                                                             host (overrides  │
│                                                             TOR__SOCKS_HOST) │
│    --tor-socks-po…                         INTEGER          Tor SOCKS proxy  │
│                                                             port (overrides  │
│                                                             TOR__SOCKS_PORT) │
│    --max-abs-fee                           INTEGER          Max absolute fee │
│                                                             in sats          │
│    --max-rel-fee                           TEXT             Max relative fee │
│                                                             (0.001=0.1%)     │
│    --fee-rate                              FLOAT            Manual fee rate  │
│                                                             in sat/vB.       │
│                                                             Mutually         │
│                                                             exclusive with   │
│                                                             --block-target.  │
│    --block-target                          INTEGER          Target blocks    │
│                                                             for fee          │
│                                                             estimation       │
│                                                             (1-1008). Cannot │
│                                                             be used with     │
│                                                             neutrino.        │
│    --bondless-all…                         FLOAT            Fraction of time │
│                                                             to choose makers │
│                                                             randomly         │
│                                                             (0.0-1.0)        │
│                                                             [env var:        │
│                                                             BONDLESS_MAKERS… │
│    --bond-exponent                         FLOAT            Exponent for     │
│                                                             fidelity bond    │
│                                                             value            │
│                                                             calculation      │
│                                                             [env var:        │
│                                                             BOND_VALUE_EXPO… │
│    --bondless-zer…      --no-bondless-…                     For bondless     │
│                                                             spots, require   │
│                                                             zero absolute    │
│                                                             fee              │
│                                                             [env var:        │
│                                                             BONDLESS_REQUIR… │
│    --select-utxos   -s                                      Interactively    │
│                                                             select UTXOs     │
│                                                             (fzf-like TUI)   │
│    --yes            -y                                      Skip             │
│                                                             confirmation     │
│                                                             prompt           │
│    --log-level      -l                     TEXT             Log level        │
│    --help                                                   Show this        │
│                                                             message and      │
│                                                             exit.            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-taker tumble --help</code></summary>

```

 Usage: jm-taker tumble [OPTIONS] SCHEDULE_FILE

 Run a tumbler schedule of CoinJoins.

 Configuration is loaded from ~/.joinmarket-ng/config.toml, environment
 variables,
 and CLI arguments. CLI arguments have the highest priority.

╭─ Arguments ──────────────────────────────────────────────────────────────────╮
│ *    schedule_file      PATH  Path to schedule JSON file [required]          │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file         -f      PATH                  Path to mnemonic file  │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
│ --network                       [mainnet|testnet|sig  Bitcoin network        │
│                                 net|regtest]                                 │
│ --backend               -b      TEXT                  Backend type:          │
│                                                       scantxoutset |         │
│                                                       descriptor_wallet |    │
│                                                       neutrino               │
│ --rpc-url                       TEXT                  Bitcoin full node RPC  │
│                                                       URL                    │
│                                                       [env var:              │
│                                                       BITCOIN_RPC_URL]       │
│ --neutrino-url                  TEXT                  Neutrino REST API URL  │
│                                                       [env var:              │
│                                                       NEUTRINO_URL]          │
│ --directory             -D      TEXT                  Directory servers      │
│                                                       (comma-separated)      │
│                                                       [env var:              │
│                                                       DIRECTORY_SERVERS]     │
│ --tor-socks-host                TEXT                  Tor SOCKS proxy host   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_HOST)       │
│ --tor-socks-port                INTEGER               Tor SOCKS proxy port   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_PORT)       │
│ --log-level             -l      TEXT                  Log level              │
│ --help                                                Show this message and  │
│                                                       exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-taker clear-ignored-makers --help</code></summary>

```

 Usage: jm-taker clear-ignored-makers [OPTIONS]

 Clear the list of ignored makers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --data-dir  -d      PATH  Data directory for JoinMarket files                │
│                           [env var: JOINMARKET_DATA_DIR]                     │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-taker config-init --help</code></summary>

```

 Usage: jm-taker config-init [OPTIONS]

 Initialize the config file with default settings.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --data-dir  -d      PATH  Data directory for JoinMarket files                │
│                           [env var: JOINMARKET_DATA_DIR]                     │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-taker -->
