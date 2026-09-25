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

## Maker UTXO Selection

Set `[maker] merge_algorithm` in `config.toml`, or pass `--merge-algorithm`:
`default`, `gradual`, `greedy`, `greediest`, or `random`.

The default selects the smallest sufficient coin, otherwise funds largest-first.
With probability `(n-2)/n`, it tops up to three inputs using random extras when
the authorized pool has more than two eligible coins and funding needs fewer
than three inputs. Larger inventories therefore favor consolidation without a
fixed threshold. Frozen coins, bonds, locks, confirmations, and mixdepth-zero
privacy restrictions still apply.

The other policies use reference-style value selection or randomized funding
and pruning. Neither `greedy` nor `greediest` spends every eligible coin. Extra
inputs increase taker-paid mining fees and disclose more co-owned coins; neither
consolidation nor randomness guarantees privacy. Existing policy names now use
these revised rules on future rounds, without a wallet migration or startup sweep.
See [selection rules and privacy tradeoffs](../docs/technical/wallet.md#utxo-selection).

## Experimental Channel Buyout Funding

A Taproot maker can use one explicitly prepared private channel buyout with
`jm-maker start --buyout-config buyer.toml --buyout-session SESSION_ID`. This is
experimental and disabled unless both options are given; read
[Experimental Ring Market](../docs/experimental-ring-market.md) first.

The configuration must match the wallet fingerprint, Bitcoin network, and source
mixdepth. A Bitcoin Core backend and an ordinary wallet input for authentication
are required. The maker reserves escrow change, offers only the bound mixdepth,
and consumes the prepared session for at most one round. After reservation or
cancellation, it withdraws offers with the normal publication delay and continues
monitoring settlement. It does not switch to ordinary wallet funding. Stopping
the maker leaves the journal intact; resume monitoring with
`jm-buyout --config buyer.toml serve`.

Existing makers started without both buyout options behave as before. Startup
does not discover or migrate buyout journals.

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

## CoinJoin Miner Fee Policy

Although the taker pays the miner fee, a maker's inputs also depend on the
transaction confirming. Full-node makers reject underpriced CoinJoins before
signing to reduce the risk of delayed confirmation and tied-up inputs.

The required rate is the highest of the local mempool minimum, the backend's
estimate for `min_fee_block_target`, and any configured `min_fee_rate_sat_vb` floor.
Set `[maker] min_fee_block_target = -1` to ignore estimation, while retaining
the mempool minimum and any explicit static floor.

Bitcoin Core estimation can fluctuate. The descriptor backend retains its
existing 1 sat/vB fallback if estimation fails; `-1` also bypasses this fallback.
If no positive source is available and the static floor is disabled, no positive
minimum is imposed. This does not guarantee relay or confirmation. Neutrino
makers cannot verify foreign prevouts and do not enforce this fee policy.

Explicit configured floors are preserved; omitted floors follow the current
configuration default on restart. DEBUG logs identify the resolved floor and its source.

## Direct Connections and Identity Rotation

With automatically created Tor onion services, identity rotation prepares a new
onion address on a randomly assigned local listening port. The initial listener
uses `onion_serving_port`; replacement listeners can log a different
local port. Peers continue to use the configured onion port because Tor forwards
it to the replacement listener's actual port. Standalone Tor must reach that local
address; in Docker, `tor_target_host` must identify the maker container.

Rotation stops accepting new connections on the old listener while existing
connections remain available for generation-specific continuations during grace.
After pending sessions drain and the configured quiet period ends, the maker
announces its replacement identity. If the replacement cannot connect to a
directory, the maker restores the old listener on its previous port. Static onion
services do not rotate.

## Directory Connections

Makers connect to directories concurrently and reconnect unavailable endpoints in
the background. Orderbook responses are sent independently, so a stalled directory
does not block healthy connections. Timed-out connections are closed and recovered
through background reconnection; failed responses are not queued or retried.

## Orderbook Rate Limits

Makers apply per-peer cooldowns and independent maker-wide response budgets for
directory and direct requests, with or without a fidelity bond. One admission covers
all offers and, for directory requests, all connected directory sends. Suppressed
responses are dropped, not queued; capacity replenishes automatically without
interrupting existing CoinJoins. Sustained suppression can affect offer discovery,
and higher limits permit more signing and network load under spam.

See the `[maker]` section in `config.toml.template` for rate-limit and directory
connection settings.

## Receiving Addresses

Use `jm-wallet address --prompt-bip39-passphrase new 0` to issue and reserve a
fresh receiving address in mixdepth 0. Omit the prompt option for wallets without
a BIP39 passphrase. This replaces the removed `jm-maker generate-address`
command, which always derived index 0 and could return an already used address.

Put wallet options before `new`, for example
`jm-wallet address --mnemonic-file FILE --data-dir DIR new 0`. Use `--backend`
instead of the old command's `--backend-type`. Wallet commands use the configured
Bitcoin network (not a separate protocol network); there is no
`--bitcoin-network` option on `address`. The backend must be available for
synchronization and address-history verification.

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
│ --version                     Show the installed JoinMarket NG version and   │
│                               exit.                                          │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ config-init  Initialize the config file with default settings.               │
│ start        Start the maker bot.                                            │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-maker config-init --help</code></summary>

```

 Usage: jm-maker config-init [OPTIONS]

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
<summary><code>jm-maker start --help</code></summary>

```

 Usage: jm-maker start [OPTIONS]

 Start the maker bot.

 Configuration is loaded from ~/.joinmarket-ng/config.toml (or
 $JOINMARKET_DATA_DIR/config.toml),
 environment variables, and CLI arguments. CLI arguments have the highest
 priority.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --backend-type                  <str>                 Backend type:          │
│                                                       descriptor_wallet |    │
│                                                       neutrino               │
│ --bitcoin-network               <mainnet|testnet|sig  Bitcoin network for    │
│                                 net|regtest>          address generation     │
│                                                       (defaults to           │
│                                                       --network)             │
│ --cj-fee-absolute               <int>                 Absolute coinjoin fee  │
│                                                       in sats. Mutually      │
│                                                       exclusive with         │
│                                                       --cj-fee-relative.     │
│                                                       [env var:              │
│                                                       CJ_FEE_ABSOLUTE]       │
│ --cj-fee-relative               <str>                 Relative coinjoin fee  │
│                                                       (e.g., 0.001 = 0.1%)   │
│                                                       [env var:              │
│                                                       CJ_FEE_RELATIVE]       │
│ --config-file                   <path>                Config file path       │
│                                                       (decoupled from data   │
│                                                       dir). Defaults to      │
│                                                       <data-dir>/config.toml │
│                                                       [env var:              │
│                                                       JOINMARKET_CONFIG_FIL… │
│ --data-dir              -d      <path>                Data directory for     │
│                                                       JoinMarket files.      │
│                                                       Defaults to            │
│                                                       ~/.joinmarket-ng       │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --directory             -D      <str>                 Directory servers      │
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
│ --fidelity-bond         -B      <str>                 Specific fidelity bond │
│                                                       to use (format:        │
│                                                       txid:vout)             │
│ --fidelity-bond-index   -I      <int>                 Fidelity bond          │
│                                                       derivation index       │
│                                                       [env var:              │
│                                                       FIDELITY_BOND_INDEX]   │
│ --fidelity-bond-lockt…  -L      <int>                 Fidelity bond          │
│                                                       locktimes to scan for  │
│ --help                                                Show this message and  │
│                                                       exit.                  │
│ --log-level             -l      <str>                 Log level              │
│ --merge-algorithm       -M      <str>                 UTXO selection         │
│                                                       strategy: default,     │
│                                                       gradual, greedy,       │
│                                                       greediest, random      │
│                                                       [env var:              │
│                                                       MERGE_ALGORITHM]       │
│ --min-size                      <int>                 Minimum CoinJoin size  │
│                                                       in sats                │
│ --mixdepth-selection            <str>                 Source mixdepth        │
│                                                       policy: balanced       │
│                                                       (privacy compartments) │
│                                                       or concentrated        │
│                                                       (legacy liquidity      │
│                                                       heuristic)             │
│                                                       [env var:              │
│                                                       MIXDEPTH_SELECTION]    │
│ --mnemonic-file         -f      <path>                Path to mnemonic file  │
│ --network                       <mainnet|testnet|sig  Protocol network       │
│                                 net|regtest>          (mainnet, testnet,     │
│                                                       signet, regtest)       │
│ --neutrino-url                  <str>                 Neutrino REST API URL  │
│                                                       [env var:              │
│                                                       NEUTRINO_URL]          │
│ --no-fidelity-bond                                    Disable fidelity bond  │
│                                                       usage. Skips registry  │
│                                                       lookup and bond proof  │
│                                                       generation even when   │
│                                                       bonds exist in the     │
│                                                       registry.              │
│ --onion-serving-host            <str>                 Bind address for       │
│                                                       incoming connections   │
│                                                       (overrides             │
│                                                       MAKER__ONION_SERVING_… │
│ --onion-serving-port            <int>                 Port for incoming      │
│                                                       .onion connections     │
│                                                       (overrides             │
│                                                       MAKER__ONION_SERVING_… │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
│ --rpc-url                       <str>                 Bitcoin full node RPC  │
│                                                       URL                    │
│                                                       [env var:              │
│                                                       BITCOIN_RPC_URL]       │
│ --staged-credentials-…          <path>                Service                │
│                                                       EnvironmentFile whose  │
│                                                       wallet binding must    │
│                                                       validate before        │
│                                                       startup                │
│                                                       [env var:              │
│                                                       JOINMARKET_STAGED_CRE… │
│ --tor-control-host              <str>                 Tor control port host  │
│                                                       (overrides             │
│                                                       TOR__CONTROL_HOST)     │
│ --tor-control-port              <int>                 Tor control port       │
│                                                       (overrides             │
│                                                       TOR__CONTROL_PORT)     │
│ --tor-cookie-path               <path>                Path to Tor cookie     │
│                                                       auth file (overrides   │
│                                                       TOR__COOKIE_PATH)      │
│ --tor-socks-host                <str>                 Tor SOCKS proxy host   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_HOST)       │
│ --tor-socks-port                <int>                 Tor SOCKS proxy port   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_PORT)       │
│ --tor-target-host               <str>                 Target hostname for    │
│                                                       Tor hidden service     │
│                                                       (overrides             │
│                                                       TOR__TARGET_HOST)      │
│ --tx-fee-contribution           <int>                 Tx fee contribution in │
│                                                       sats                   │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-maker -->
