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
│ --help                        Show this message and exit.                    │
│ --install-completion          Install completion for the current shell.      │
│ --show-completion             Show completion for the current shell, to copy │
│                               it or customize the installation.              │
│ --version                     Show the installed JoinMarket NG version and   │
│                               exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ clear-ignored-makers  Clear the list of ignored makers.                      │
│ coinjoin              Execute a single CoinJoin transaction.                 │
│ config-init           Initialize the config file with default settings.      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-taker clear-ignored-makers --help</code></summary>

```

 Usage: jm-taker clear-ignored-makers [OPTIONS]

 Clear the list of ignored makers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --config-file          PATH  Config file path (decoupled from data dir).     │
│                              Defaults to <data-dir>/config.toml              │
│                              [env var: JOINMARKET_CONFIG_FILE]               │
│ --data-dir     -d      PATH  Data directory for JoinMarket files             │
│                              [env var: JOINMARKET_DATA_DIR]                  │
│ --help                       Show this message and exit.                     │
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
│ --amount           -a                     INTEGER           Amount in sats   │
│                                                             (0 for sweep;    │
│                                                             with             │
│                                                             --select-utxos,  │
│                                                             defaults to      │
│                                                             sweep)           │
│ --backend          -b                     TEXT              Backend type:    │
│                                                             descriptor_wall… │
│                                                             | neutrino       │
│ --bitcoin-network                         [mainnet|testnet  Bitcoin network  │
│                                           |signet|regtest]  for addresses    │
│                                                             (defaults to     │
│                                                             --network)       │
│ --block-target                            INTEGER           Target blocks    │
│                                                             for fee          │
│                                                             estimation       │
│                                                             (1-1008). Cannot │
│                                                             be used with     │
│                                                             neutrino.        │
│ --bond-exponent                           FLOAT             Exponent for     │
│                                                             fidelity bond    │
│                                                             value            │
│                                                             calculation      │
│                                                             [env var:        │
│                                                             BOND_VALUE_EXPO… │
│ --bondless-allow…                         FLOAT             Fraction of      │
│                                                             allowance slots  │
│                                                             chosen uniformly │
│                                                             from zero-fee    │
│                                                             offers (0.0-1.0) │
│                                                             [env var:        │
│                                                             BONDLESS_MAKERS… │
│ --bondless-zero-…      --no-bondless-…                      Restrict         │
│                                                             allowance spots  │
│                                                             to zero-fee      │
│                                                             offers           │
│                                                             [env var:        │
│                                                             BONDLESS_REQUIR… │
│ --config-file                             PATH              Config file path │
│                                                             (decoupled from  │
│                                                             data dir).       │
│                                                             Defaults to      │
│                                                             <data-dir>/conf… │
│                                                             [env var:        │
│                                                             JOINMARKET_CONF… │
│ --counterparties   -n                     INTEGER           Number of makers │
│ --data-dir                                PATH              Data directory   │
│                                                             (default:        │
│                                                             ~/.joinmarket-ng │
│                                                             or               │
│                                                             $JOINMARKET_DAT… │
│                                                             [env var:        │
│                                                             JOINMARKET_DATA… │
│ --destination      -d                     TEXT              Destination      │
│                                                             address (or      │
│                                                             'INTERNAL' for   │
│                                                             next mixdepth)   │
│                                                             [default:        │
│                                                             INTERNAL]        │
│ --directory        -D                     TEXT              Directory        │
│                                                             servers          │
│                                                             (comma-separate… │
│                                                             [env var:        │
│                                                             DIRECTORY_SERVE… │
│ --equalize-cj-fe…      --no-equalize-…                      Pay all selected │
│                                                             makers the       │
│                                                             highest realized │
│                                                             fee in the       │
│                                                             selected set     │
│ --fee-rate                                FLOAT             Manual fee rate  │
│                                                             in sat/vB.       │
│                                                             Mutually         │
│                                                             exclusive with   │
│                                                             --block-target.  │
│ --help                                                      Show this        │
│                                                             message and      │
│                                                             exit.            │
│ --input-utxo                              TEXT              Explicit input   │
│                                                             UTXO as          │
│                                                             txid:vout        │
│                                                             (repeatable).    │
│                                                             CoinJoin spends  │
│                                                             exactly the      │
│                                                             given UTXOs,     │
│                                                             including for    │
│                                                             sweeps, and      │
│                                                             never adds other │
│                                                             inputs. Every    │
│                                                             UTXO must be     │
│                                                             eligible and     │
│                                                             belong to        │
│                                                             --mixdepth.      │
│                                                             Mutually         │
│                                                             exclusive with   │
│                                                             --select-utxos.  │
│ --log-level        -l                     TEXT              Log level        │
│ --max-abs-fee                             INTEGER           Max absolute fee │
│                                                             in sats          │
│ --max-rel-fee                             TEXT              Max relative fee │
│                                                             (0.001=0.1%)     │
│ --mixdepth         -m                     INTEGER           Source mixdepth  │
│                                                             (default 0; with │
│                                                             --select-utxos,  │
│                                                             derived from the │
│                                                             selection unless │
│                                                             set explicitly;  │
│                                                             --input-utxo     │
│                                                             entries must     │
│                                                             belong to this   │
│                                                             mixdepth)        │
│ --mnemonic-file    -f                     PATH              Path to mnemonic │
│                                                             file             │
│ --network                                 [mainnet|testnet  Protocol network │
│                                           |signet|regtest]  for handshakes   │
│ --neutrino-url                            TEXT              Neutrino REST    │
│                                                             API URL          │
│                                                             [env var:        │
│                                                             NEUTRINO_URL]    │
│ --prompt-bip39-p…                                           Prompt for BIP39 │
│                                                             passphrase       │
│                                                             interactively    │
│ --quantized-offe…      --allow-non-qu…                      Only select      │
│                                                             offers whose     │
│                                                             advertised       │
│                                                             CoinJoin fee is  │
│                                                             on the public    │
│                                                             grid             │
│ --round-up-cj-fe…      --no-round-up-…                      Round selected   │
│                                                             maker fees up to │
│                                                             public fee       │
│                                                             quanta           │
│ --rpc-url                                 TEXT              Bitcoin full     │
│                                                             node RPC URL     │
│                                                             [env var:        │
│                                                             BITCOIN_RPC_URL] │
│ --select-utxos     -s                                       Interactively    │
│                                                             select UTXOs     │
│                                                             (fzf-like TUI)   │
│ --tor-socks-host                          TEXT              Tor SOCKS proxy  │
│                                                             host (overrides  │
│                                                             TOR__SOCKS_HOST) │
│ --tor-socks-port                          INTEGER           Tor SOCKS proxy  │
│                                                             port (overrides  │
│                                                             TOR__SOCKS_PORT) │
│ --yes              -y                                       Skip             │
│                                                             confirmation     │
│                                                             prompt           │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-taker config-init --help</code></summary>

```

 Usage: jm-taker config-init [OPTIONS]

 Initialize the config file with default settings.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --config-file          PATH  Config file path (decoupled from data dir).     │
│                              Defaults to <data-dir>/config.toml              │
│                              [env var: JOINMARKET_CONFIG_FILE]               │
│ --data-dir     -d      PATH  Data directory for JoinMarket files             │
│                              [env var: JOINMARKET_DATA_DIR]                  │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-taker -->
