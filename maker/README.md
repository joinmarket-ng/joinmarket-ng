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

## Direct Connections and Identity Rotation

With automatically created Tor onion services, identity rotation prepares a new
onion address on a randomly assigned local listening port. The initial listener
uses `onion_serving_port` (normally 5222); replacement listeners can log a different
local port. Peers continue to use the configured onion port because Tor forwards
it to the replacement listener's actual port. Standalone Tor must reach that local
address; in Docker, `tor_target_host` must identify the maker container.

Rotation stops accepting new connections on the old listener while existing
connections remain available for generation-specific continuations during grace.
After pending sessions drain and the configured quiet period ends, the maker
announces its replacement identity. If the replacement cannot connect to a
directory, the maker restores the old listener on its previous port. Static onion
services do not rotate.

Upgrading applies the listener lifecycle fix when the maker restarts. Existing
configuration, wallet data, and Tor settings require no migration.

## Directory Connections

Startup connects to configured directories concurrently and starts serving once a
directory is ready. Slow or unreachable endpoints cannot hold up a healthy connection
and accumulate incoming requests into a burst. Unfinished connection attempts are
closed; the background reconnect task fills in missing directories. It first runs
after a 60-second settling delay plus `directory_reconnect_interval` (normally five
minutes). Subsequent passes use that interval.

`directory_startup_timeout` bounds the initial connection attempts and retries as a
whole. If none succeed, the maker still starts and relies on background reconnection.
Tor connection failures and reconnect delays are separate from orderbook response
limits.

Orderbook responses are sent to connected directories concurrently. Each directory
has five seconds to flush a complete response, including all offers. This bounds
local write backpressure; it does not wait for a reply from the taker. A timed-out
connection is aborted and recovered through the same background reconnect task.
Healthy directories can receive responses while another directory's write is stalled.
Severe congestion can trigger this disconnect even if the directory is otherwise
reachable. The response budget is unchanged, and failed sends are not queued or retried.

## Orderbook Rate Limits

Makers limit `!orderbook` responses to keep unsolicited requests from exhausting
signing and network resources. These limits apply with or without a fidelity bond.

- Directory requests normally allow one response per requester nick every 10 seconds.
  The first copy from each additional configured directory during that cooldown
  is ignored without adding a spam violation. Further copies from the same directory
  still count as violations. The fanout exemption does not apply during escalated
  backoff or a ban, and it does not extend the cooldown.
- Direct connections have their own per-connection cooldown, normally 30 seconds.
- Directory responses have a maker-wide budget of 200 requests per burst,
  replenished continuously at 20 requests per second.
- Direct responses have an independent maker-wide budget of 20 requests per burst,
  replenished continuously at two requests per second. Exhausting either transport's
  budget does not consume the other transport's capacity.
- One admission covers all of that maker's offers and, for directory requests, all
  connected directory sends. It is not charged per offer or per directory.
  These aggregate budgets are fixed; the per-peer `orderbook_*` settings do not change them.

`Suppressing !orderbook response (directory response budget exhausted; refills automatically)`
means a response was dropped, not queued. No restart is needed to replenish capacity.
A later request can succeed once capacity is available and its separate per-peer or
per-connection cooldown has elapsed. Existing CoinJoin sessions are not aborted by
this limit, but sustained suppression can prevent new takers from discovering offers.
These limits bound response admissions, not network-wide traffic or service fairness:
multiple identities can still compete for one transport's budget, and response work
increases with the number of offers and connected directories.

Upgrading from the shared 20-request, one-per-second budget automatically applies
the new independent limits when the maker restarts. No configuration or wallet-state
migration is needed.

Directory suppression warnings and direct-path debug messages are each throttled to
once every 10 seconds; they do not count every dropped response. An aggregate
`Orderbook response admission since startup` INFO message is emitted after the first
10 minutes and hourly afterward when there has been activity. It includes elapsed
time, admitted and globally suppressed requests for each transport, and ignored
directory fanout copies, without peer identifiers. The counters are cumulative;
compare successive summaries to assess sustained pressure. Admissions are work
attempts, not confirmation of successful delivery. Persistent warnings or growing
suppression counts warrant investigation before changing rate-limit policy.

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
│ --mixdepth-selection            TEXT                  Source mixdepth        │
│                                                       policy: balanced       │
│                                                       (privacy compartments) │
│                                                       or concentrated        │
│                                                       (legacy liquidity      │
│                                                       heuristic)             │
│                                                       [env var:              │
│                                                       MIXDEPTH_SELECTION]    │
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
