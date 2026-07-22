# JoinMarket Maker Bot

Earn fees by providing liquidity for CoinJoin transactions. Makers passively earn bitcoin while enhancing network privacy.

## Features

- **Order Creation**: Publish offers for CoinJoin participation
- **Offer Management**: Configure fee structures and minimum amounts
- **CoinJoin Participation**: Automatically join taker-initiated transactions
- **Fee Collection**: Earn fees for providing liquidity
- **Fidelity Bonds**: Enhance reputation with fidelity bonds
- **Hidden Service**: Expose maker service via Tor hidden service

## Documentation

For full documentation, see [maker Documentation](https://joinmarket-ng.github.io/joinmarket-ng/README-maker/).

## Multiple Local Instances

If you want to run more than one maker on the same machine, give each maker
its own data directory. The simplest pattern is to pass `--data-dir` (or set
`JOINMARKET_DATA_DIR`) on every `jm-maker` and `jm-wallet` command so each
instance gets its own `config.toml`, wallet files, logs, and local runtime
state.

```bash
mkdir -p ~/jm-maker-a ~/jm-maker-b

jm-maker config-init --data-dir ~/jm-maker-a
jm-maker config-init --data-dir ~/jm-maker-b

jm-wallet generate --data-dir ~/jm-maker-a
jm-wallet generate --data-dir ~/jm-maker-b

jm-maker start \
  --data-dir ~/jm-maker-a \
  --mnemonic-file ~/jm-maker-a/wallets/default.mnemonic

jm-maker start \
  --data-dir ~/jm-maker-b \
  --mnemonic-file ~/jm-maker-b/wallets/default.mnemonic
```

For takers, separate installations are usually unnecessary. One installation
can manage multiple wallet mnemonic files, and you can switch between them
with `--mnemonic-file`. Use separate `--data-dir` values for takers only when
you specifically want isolated config and runtime state.

<!-- AUTO-GENERATED HELP START: jm-maker -->

<details>
<summary><code>jm-maker --help</code></summary>

```

 Usage: jm-maker [OPTIONS] COMMAND [ARGS]...

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help                        Show this message and exit.                    │
│ --install-completion          Install completion for the current shell.      │
│ --show-completion             Show completion for the current shell, to copy │
│                               it or customize the installation.              │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ config-init       Initialize the config file with default settings.          │
│ generate-address  Generate a new receive address.                            │
│ start             Start the maker bot.                                       │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-maker config-init --help</code></summary>

```

 Usage: jm-maker config-init [OPTIONS]

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

<details>
<summary><code>jm-maker generate-address --help</code></summary>

```

 Usage: jm-maker generate-address [OPTIONS]

 Generate a new receive address.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --backend-type                  TEXT                  Backend type           │
│ --bitcoin-network               [mainnet|testnet|sig  Bitcoin network for    │
│                                 net|regtest]          address generation     │
│                                                       (defaults to           │
│                                                       --network)             │
│ --config-file                   PATH                  Config file path       │
│                                                       (decoupled from data   │
│                                                       dir). Defaults to      │
│                                                       <data-dir>/config.toml │
│                                                       [env var:              │
│                                                       JOINMARKET_CONFIG_FIL… │
│ --data-dir                      PATH                  Data directory         │
│                                                       (default:              │
│                                                       ~/.joinmarket-ng or    │
│                                                       $JOINMARKET_DATA_DIR)  │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --help                                                Show this message and  │
│                                                       exit.                  │
│ --log-level             -l      TEXT                  Log level              │
│ --mnemonic-file         -f      PATH                  Path to mnemonic file  │
│ --network                       [mainnet|testnet|sig  Protocol network       │
│                                 net|regtest]                                 │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-maker start --help</code></summary>

```

 Usage: jm-maker start [OPTIONS]

 Start the maker bot.

 Configuration is loaded from ~/.joinmarket-ng/config.toml (or
 $JOINMARKET_DATA_DIR/config.toml),
 environment variables, and CLI arguments. CLI arguments have the highest
 priority.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --backend-type                  TEXT                  Backend type:          │
│                                                       descriptor_wallet |    │
│                                                       neutrino               │
│ --bitcoin-network               [mainnet|testnet|sig  Bitcoin network for    │
│                                 net|regtest]          address generation     │
│                                                       (defaults to           │
│                                                       --network)             │
│ --cj-fee-absolute               INTEGER               Absolute coinjoin fee  │
│                                                       in sats. Mutually      │
│                                                       exclusive with         │
│                                                       --cj-fee-relative.     │
│                                                       [env var:              │
│                                                       CJ_FEE_ABSOLUTE]       │
│ --cj-fee-relative               TEXT                  Relative coinjoin fee  │
│                                                       (e.g., 0.001 = 0.1%)   │
│                                                       [env var:              │
│                                                       CJ_FEE_RELATIVE]       │
│ --config-file                   PATH                  Config file path       │
│                                                       (decoupled from data   │
│                                                       dir). Defaults to      │
│                                                       <data-dir>/config.toml │
│                                                       [env var:              │
│                                                       JOINMARKET_CONFIG_FIL… │
│ --data-dir              -d      PATH                  Data directory for     │
│                                                       JoinMarket files.      │
│                                                       Defaults to            │
│                                                       ~/.joinmarket-ng       │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --directory             -D      TEXT                  Directory servers      │
│                                                       (comma-separated       │
│                                                       host:port)             │
│                                                       [env var:              │
│                                                       DIRECTORY_SERVERS]     │
│ --disable-tor-control                                 Disable Tor control    │
│                                                       port integration       │
│ --dual-offers                                         Create both relative   │
│                                                       and absolute fee       │
│                                                       offers simultaneously. │
│                                                       Each offer gets a      │
│                                                       unique ID (0 for       │
│                                                       relative, 1 for        │
│                                                       absolute). Use with    │
│                                                       --cj-fee-relative and  │
│                                                       --cj-fee-absolute to   │
│                                                       set fees for each.     │
│ --fidelity-bond         -B      TEXT                  Specific fidelity bond │
│                                                       to use (format:        │
│                                                       txid:vout)             │
│ --fidelity-bond-index   -I      INTEGER               Fidelity bond          │
│                                                       derivation index       │
│                                                       [env var:              │
│                                                       FIDELITY_BOND_INDEX]   │
│ --fidelity-bond-lockt…  -L      INTEGER               Fidelity bond          │
│                                                       locktimes to scan for  │
│ --help                                                Show this message and  │
│                                                       exit.                  │
│ --log-level             -l      TEXT                  Log level              │
│ --merge-algorithm       -M      TEXT                  UTXO selection         │
│                                                       strategy: default,     │
│                                                       gradual, greedy,       │
│                                                       random                 │
│                                                       [env var:              │
│                                                       MERGE_ALGORITHM]       │
│ --min-size                      INTEGER               Minimum CoinJoin size  │
│                                                       in sats                │
│ --mnemonic-file         -f      PATH                  Path to mnemonic file  │
│ --network                       [mainnet|testnet|sig  Protocol network       │
│                                 net|regtest]          (mainnet, testnet,     │
│                                                       signet, regtest)       │
│ --neutrino-url                  TEXT                  Neutrino REST API URL  │
│                                                       [env var:              │
│                                                       NEUTRINO_URL]          │
│ --no-fidelity-bond                                    Disable fidelity bond  │
│                                                       usage. Skips registry  │
│                                                       lookup and bond proof  │
│                                                       generation even when   │
│                                                       bonds exist in the     │
│                                                       registry.              │
│ --onion-serving-host            TEXT                  Bind address for       │
│                                                       incoming connections   │
│                                                       (overrides             │
│                                                       MAKER__ONION_SERVING_… │
│ --onion-serving-port            INTEGER               Port for incoming      │
│                                                       .onion connections     │
│                                                       (overrides             │
│                                                       MAKER__ONION_SERVING_… │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
│ --rpc-url                       TEXT                  Bitcoin full node RPC  │
│                                                       URL                    │
│                                                       [env var:              │
│                                                       BITCOIN_RPC_URL]       │
│ --tor-control-host              TEXT                  Tor control port host  │
│                                                       (overrides             │
│                                                       TOR__CONTROL_HOST)     │
│ --tor-control-port              INTEGER               Tor control port       │
│                                                       (overrides             │
│                                                       TOR__CONTROL_PORT)     │
│ --tor-cookie-path               PATH                  Path to Tor cookie     │
│                                                       auth file (overrides   │
│                                                       TOR__COOKIE_PATH)      │
│ --tor-socks-host                TEXT                  Tor SOCKS proxy host   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_HOST)       │
│ --tor-socks-port                INTEGER               Tor SOCKS proxy port   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_PORT)       │
│ --tor-target-host               TEXT                  Target hostname for    │
│                                                       Tor hidden service     │
│                                                       (overrides             │
│                                                       TOR__TARGET_HOST)      │
│ --tx-fee-contribution           INTEGER               Tx fee contribution in │
│                                                       sats                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-maker -->
