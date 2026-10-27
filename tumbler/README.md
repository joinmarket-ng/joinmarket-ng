# JoinMarket Tumbler

High-level CoinJoin scheduler for joinmarket-ng.

For planning, safe operation, and resuming an interrupted tumble, use the
[tumbler guide](../docs/README-tumbler.md). Generated command help lives below.
For implementation details, see
[`docs/technical/tumbler-redesign.md`](../docs/technical/tumbler-redesign.md).

## Development

Install editable packages:

```bash
pip install -e jmcore -e jmwallet -e jmswap -e taker -e maker
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
│ --help                        Show this message and exit.                    │
│ --install-completion          Install completion for the current shell.      │
│ --show-completion             Show completion for the current shell, to copy │
│                               it or customize the installation.              │
│ --version                     Show the installed JoinMarket NG version and   │
│                               exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ config-init  Initialize the config file with default settings.               │
│ delete       Delete the on-disk plan for ``wallet_name``.                    │
│ plan         Build a tumbler plan for the given destinations and persist it. │
│ run          Execute the saved plan for a wallet to completion.              │
│ status       Print the current plan for the given wallet.                    │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler config-init --help</code></summary>

```

 Usage: jm-tumbler config-init [OPTIONS]

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
<summary><code>jm-tumbler delete --help</code></summary>

```

 Usage: jm-tumbler delete [OPTIONS]

 Delete the on-disk plan for ``wallet_name``.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --config-file                      <path>  Config file path (decoupled from  │
│                                            data dir). Defaults to            │
│                                            <data-dir>/config.toml            │
│                                            [env var: JOINMARKET_CONFIG_FILE] │
│ --data-dir                         <path>  Data directory (default:          │
│                                            ~/.joinmarket-ng or               │
│                                            $JOINMARKET_DATA_DIR)             │
│                                            [env var: JOINMARKET_DATA_DIR]    │
│ --help                                     Show this message and exit.       │
│ --log-level                -l      <str>                                     │
│ --mnemonic-file            -f      <path>  Path to mnemonic file             │
│ --prompt-bip39-passphrase                  Prompt for BIP39 passphrase       │
│                                            interactively                     │
│ --wallet-name              -w      <str>   Wallet identifier; defaults to    │
│                                            the mnemonic fingerprint          │
│ --yes                      -y              Skip confirmation prompt          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler plan --help</code></summary>

```

 Usage: jm-tumbler plan [OPTIONS]

 Build a tumbler plan for the given destinations and persist it.

╭─ Options ────────────────────────────────────────────────────────────────────╮
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
│    --backend        -b                     <str>            Backend type:    │
│                                                             descriptor_wall… │
│                                                             | neutrino       │
│    --config-file                           <path>           Config file path │
│                                                             (decoupled from  │
│                                                             data dir).       │
│                                                             Defaults to      │
│                                                             <data-dir>/conf… │
│                                                             [env var:        │
│                                                             JOINMARKET_CONF… │
│    --data-dir                              <path>           Data directory   │
│                                                             (default:        │
│                                                             ~/.joinmarket-ng │
│                                                             or               │
│                                                             $JOINMARKET_DAT… │
│                                                             [env var:        │
│                                                             JOINMARKET_DATA… │
│ *  --destination    -d                     <str>            External         │
│                                                             destination      │
│                                                             address          │
│                                                             (repeatable)     │
│                                                             [required]       │
│    --force                                                  Overwrite an     │
│                                                             existing pending │
│                                                             plan             │
│    --help                                                   Show this        │
│                                                             message and      │
│                                                             exit.            │
│    --log-level      -l                     <str>                             │
│    --maker-count-…                         <int>            Maximum          │
│                                                             counterparty     │
│                                                             count per CJ;    │
│                                                             defaults to      │
│                                                             settings.taker.… │
│    --maker-count-…                         <int>            Minimum          │
│                                                             counterparty     │
│                                                             count per CJ;    │
│                                                             defaults to      │
│                                                             settings.taker.… │
│    --maker-sessio…      --no-maker-ses…                     [default:        │
│                                                             maker-sessions]  │
│    --mincjamount-…                         <int>            Minimum CJ       │
│                                                             amount in sats   │
│                                                             [default:        │
│                                                             100000]          │
│    --mnemonic-file  -f                     <path>           Path to mnemonic │
│                                                             file             │
│    --network                               <mainnet|testne  Bitcoin network  │
│                                            t|signet|regtes                   │
│                                            t>                                │
│    --neutrino-url                          <str>            Neutrino REST    │
│                                                             API URL          │
│                                                             [env var:        │
│                                                             NEUTRINO_URL]    │
│    --prompt-bip39…                                          Prompt for BIP39 │
│                                                             passphrase       │
│                                                             interactively    │
│    --rpc-url                               <str>            Bitcoin full     │
│                                                             node RPC URL     │
│                                                             [env var:        │
│                                                             BITCOIN_RPC_URL] │
│    --seed                                  <int>            Seed the plan    │
│                                                             builder RNG for  │
│                                                             reproducible     │
│                                                             schedules        │
│    --wallet-name    -w                     <str>            Wallet           │
│                                                             identifier for   │
│                                                             the plan file;   │
│                                                             defaults to the  │
│                                                             mnemonic         │
│                                                             fingerprint      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler run --help</code></summary>

```

 Usage: jm-tumbler run [OPTIONS]

 Execute the saved plan for a wallet to completion.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --backend               -b      <str>                                        │
│ --block-target                  <int>                 Target blocks for fee  │
│                                                       estimation (mutually   │
│                                                       exclusive with         │
│                                                       --fee-rate). Not       │
│                                                       supported with the     │
│                                                       neutrino backend.      │
│ --config-file                   <path>                Config file path       │
│                                                       (decoupled from data   │
│                                                       dir). Defaults to      │
│                                                       <data-dir>/config.toml │
│                                                       [env var:              │
│                                                       JOINMARKET_CONFIG_FIL… │
│ --counterparties                <int range>           Override the           │
│                                 [1<=x<=20]            counterparty count for │
│                                                       every phase at         │
│                                                       runtime. Useful when   │
│                                                       the configured count   │
│                                                       is unavailable on the  │
│                                                       chosen network.        │
│ --data-dir                      <path>                Data directory         │
│                                                       (default:              │
│                                                       ~/.joinmarket-ng or    │
│                                                       $JOINMARKET_DATA_DIR)  │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --directory             -D      <str>                 [env var:              │
│                                                       DIRECTORY_SERVERS]     │
│ --fee-rate                      <float>               Manual fee rate in     │
│                                                       sat/vB (mutually       │
│                                                       exclusive with         │
│                                                       --block-target).       │
│                                                       Required when the      │
│                                                       backend is neutrino.   │
│ --help                                                Show this message and  │
│                                                       exit.                  │
│ --log-level             -l      <str>                                        │
│ --min-confirmations             <int>                 Confirmations required │
│                                                       before the next phase  │
│                                                       starts (0 disables     │
│                                                       gating). Defaults to   │
│                                                       the                    │
│                                                       tumbler.min_confirmat… │
│                                                       setting (6).           │
│ --mnemonic-file         -f      <path>                Path to mnemonic file  │
│ --network                       <mainnet|testnet|sig                         │
│                                 net|regtest>                                 │
│ --neutrino-url                  <str>                 [env var:              │
│                                                       NEUTRINO_URL]          │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
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
│ --rpc-url                       <str>                 [env var:              │
│                                                       BITCOIN_RPC_URL]       │
│ --tor-socks-host                <str>                 Tor SOCKS host         │
│                                                       override               │
│ --tor-socks-port                <int>                 Tor SOCKS port         │
│                                                       override               │
│ --wallet-name           -w      <str>                 Wallet identifier;     │
│                                                       defaults to the        │
│                                                       mnemonic fingerprint   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-tumbler status --help</code></summary>

```

 Usage: jm-tumbler status [OPTIONS]

 Print the current plan for the given wallet.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --config-file                      <path>  Config file path (decoupled from  │
│                                            data dir). Defaults to            │
│                                            <data-dir>/config.toml            │
│                                            [env var: JOINMARKET_CONFIG_FILE] │
│ --data-dir                         <path>  Data directory (default:          │
│                                            ~/.joinmarket-ng or               │
│                                            $JOINMARKET_DATA_DIR)             │
│                                            [env var: JOINMARKET_DATA_DIR]    │
│ --help                                     Show this message and exit.       │
│ --log-level                -l      <str>                                     │
│ --mnemonic-file            -f      <path>  Path to mnemonic file             │
│ --prompt-bip39-passphrase                  Prompt for BIP39 passphrase       │
│                                            interactively                     │
│ --wallet-name              -w      <str>   Wallet identifier; defaults to    │
│                                            the mnemonic fingerprint          │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-tumbler -->
