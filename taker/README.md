# JoinMarket Taker Client

Mix your bitcoin for privacy via CoinJoin. Takers initiate transactions and pay small fees to makers.

## Features

- **CoinJoin Initiation**: Start CoinJoin transactions with available makers
- **Schedule-based Mixing**: Tumbler mode for automated multi-round mixing
- **Destination Management**: Multiple output destinations for privacy
- **Fee Negotiation**: Automatic fee negotiation with makers
- **Transaction Monitoring**: Track transaction progress and confirmations
- **Retry Logic**: Automatic retry on failure or maker timeout

Sweeps, including tumbler sweeps, budget for a typical maker input count before negotiation.
Their effective fee rate can differ from the requested target, especially for unusually large
transactions. Rounds must still satisfy the minimum-fee policy, sweep tolerance, and wallet
fee-rate cap. See `config.toml.template` for those settings and current defaults.
Makers whose inputs would break the minimum-fee policy or sweep tolerance are dropped during
authentication, heaviest input set first, and replaced for that round without being ignored.

Before generating or exposing a commitment, the taker checks complete mining-fee
headroom for the proposed maker set, including maker contributions and unreturned
change. The same check applies to replacements. It assumes each maker could use
one input, so it can reject a set that larger actual input counts would make
acceptable, particularly sweeps and rates near the wallet fee cap. Lower the
requested rate or explicitly change the cap only if the higher fee is intended.
Unsigned and final transaction fee checks still apply.

## Commitment History and Recovery

Generating a wallet (`jm-wallet generate`, the TUI or the daemon's wallet creation)
creates an empty history when the new wallet is saved under `<data-dir>/wallets`
and the data directory has no other wallet, transaction history or `cmtdata`
files. A fresh seed has no commitments to reuse. In every other case, including
imported or recovered seeds, explicitly initialize the history before a new taker
first uses its data directory:

```bash
jm-taker init-commitment-history --data-dir /path/to/data
```

Confirm only for a new, unused history. The command never overwrites an existing
file, including a corrupt file or symlink. Takers, including daemon CoinJoins and
tumbler phases, stop on missing, unreadable, invalid, or unsavable history rather
than resetting it or retrying with makers.

On upgrade, valid `cmtdata/commitments.json` files load without migration. Preserve
this file with wallet backups. If it is missing or damaged, stop all takers and
restore a trusted, complete copy before resuming. Existing `.corrupt.*` backups
are evidence for manual recovery, not permission to start empty. Commitment use
cannot be reconstructed from the seed or blockchain; an empty replacement can
reuse commitments and expose links between rounds. Failed writes can leave new
bytes installed even when durability was not confirmed, so preserve and inspect
the file instead of assuming the previous version remains.

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
│ clear-ignored-makers     Clear the list of ignored makers.                   │
│ coinjoin                 Execute a single CoinJoin transaction.              │
│ config-init              Initialize the config file with default settings.   │
│ init-commitment-history  Explicitly create a missing, empty commitment       │
│                          history for a new taker.                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-taker clear-ignored-makers --help</code></summary>

```

 Usage: jm-taker clear-ignored-makers [OPTIONS]

 Clear the list of ignored makers.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --config-file          <path>  Config file path (decoupled from data dir).   │
│                                Defaults to <data-dir>/config.toml            │
│                                [env var: JOINMARKET_CONFIG_FILE]             │
│ --data-dir     -d      <path>  Data directory for JoinMarket files           │
│                                [env var: JOINMARKET_DATA_DIR]                │
│ --help                         Show this message and exit.                   │
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
│ --amount           -a                     <int>             Amount in sats   │
│                                                             (0 for sweep;    │
│                                                             with             │
│                                                             --select-utxos,  │
│                                                             defaults to      │
│                                                             sweep)           │
│ --backend          -b                     <str>             Backend type:    │
│                                                             descriptor_wall… │
│                                                             | neutrino       │
│ --bitcoin-network                         <mainnet|testnet  Bitcoin network  │
│                                           |signet|regtest>  for addresses    │
│                                                             (defaults to     │
│                                                             --network)       │
│ --block-target                            <int>             Target blocks    │
│                                                             for fee          │
│                                                             estimation       │
│                                                             (1-1008). Cannot │
│                                                             be used with     │
│                                                             neutrino.        │
│ --bond-exponent                           <float>           Exponent for     │
│                                                             fidelity bond    │
│                                                             value            │
│                                                             calculation      │
│                                                             [env var:        │
│                                                             BOND_VALUE_EXPO… │
│ --bondless-allow…                         <float>           Fraction of      │
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
│ --config-file                             <path>            Config file path │
│                                                             (decoupled from  │
│                                                             data dir).       │
│                                                             Defaults to      │
│                                                             <data-dir>/conf… │
│                                                             [env var:        │
│                                                             JOINMARKET_CONF… │
│ --counterparties   -n                     <int>             Number of makers │
│ --data-dir                                <path>            Data directory   │
│                                                             (default:        │
│                                                             ~/.joinmarket-ng │
│                                                             or               │
│                                                             $JOINMARKET_DAT… │
│                                                             [env var:        │
│                                                             JOINMARKET_DATA… │
│ --destination      -d                     <str>             Destination      │
│                                                             address (or      │
│                                                             'INTERNAL' for   │
│                                                             next mixdepth)   │
│                                                             [default:        │
│                                                             INTERNAL]        │
│ --directory        -D                     <str>             Directory        │
│                                                             servers          │
│                                                             (comma-separate… │
│                                                             [env var:        │
│                                                             DIRECTORY_SERVE… │
│ --equalize-cj-fe…      --no-equalize-…                      Pay all selected │
│                                                             makers the       │
│                                                             highest realized │
│                                                             fee in the       │
│                                                             selected set     │
│ --fee-rate                                <float>           Manual fee rate  │
│                                                             in sat/vB.       │
│                                                             Mutually         │
│                                                             exclusive with   │
│                                                             --block-target.  │
│ --help                                                      Show this        │
│                                                             message and      │
│                                                             exit.            │
│ --input-utxo                              <str>             Explicit input   │
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
│ --log-level        -l                     <str>             Log level        │
│ --max-abs-fee                             <int>             Max absolute fee │
│                                                             in sats          │
│ --max-rel-fee                             <str>             Max relative fee │
│                                                             (0.001=0.1%)     │
│ --mixdepth         -m                     <int>             Source mixdepth  │
│                                                             (default 0; with │
│                                                             --select-utxos,  │
│                                                             derived from the │
│                                                             selection unless │
│                                                             set explicitly;  │
│                                                             --input-utxo     │
│                                                             entries must     │
│                                                             belong to this   │
│                                                             mixdepth)        │
│ --mnemonic-file    -f                     <path>            Path to mnemonic │
│                                                             file             │
│ --network                                 <mainnet|testnet  Protocol network │
│                                           |signet|regtest>  for handshakes   │
│ --neutrino-url                            <str>             Neutrino REST    │
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
│ --rpc-url                                 <str>             Bitcoin full     │
│                                                             node RPC URL     │
│                                                             [env var:        │
│                                                             BITCOIN_RPC_URL] │
│ --select-utxos     -s                                       Interactively    │
│                                                             select UTXOs     │
│                                                             (fzf-like TUI)   │
│ --tor-socks-host                          <str>             Tor SOCKS proxy  │
│                                                             host (overrides  │
│                                                             TOR__SOCKS_HOST) │
│ --tor-socks-port                          <int>             Tor SOCKS proxy  │
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
│ --config-file          <path>  Config file path (decoupled from data dir).   │
│                                Defaults to <data-dir>/config.toml            │
│                                [env var: JOINMARKET_CONFIG_FILE]             │
│ --data-dir     -d      <path>  Data directory for JoinMarket files           │
│                                [env var: JOINMARKET_DATA_DIR]                │
│ --help                         Show this message and exit.                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<details>
<summary><code>jm-taker init-commitment-history --help</code></summary>

```

 Usage: jm-taker init-commitment-history [OPTIONS]

 Explicitly create a missing, empty commitment history for a new taker.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --config-file          PATH  Config file path (decoupled from data dir)      │
│                              [env var: JOINMARKET_CONFIG_FILE]               │
│ --data-dir     -d      PATH  Data directory for JoinMarket files             │
│                              [env var: JOINMARKET_DATA_DIR]                  │
│ --help                       Show this message and exit.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<!-- AUTO-GENERATED HELP END: jm-taker -->
