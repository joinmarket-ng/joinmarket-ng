# JoinMarket Maker Bot

Earn fees by providing liquidity for CoinJoin transactions. Makers passively earn bitcoin while enhancing network privacy.

## Installation

Install JoinMarket-NG with the maker component:

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --maker
```

See [Installation](install.md) for complete installation instructions including:

- Backend setup (Bitcoin Core or Neutrino)
- Tor configuration
- Manual installation for developers

## Prerequisites

**Tor is REQUIRED for production use.** Makers need Tor for privacy and to advertise .onion addresses for direct peer connections.

See [Installation - Tor Setup](install.md#tor-setup) for installation and configuration instructions.

The maker bot tries to auto-detect Tor configuration. For manual setup, see [Environment Variables](#environment-variables).

## Quick Start

### 1. Create a Wallet

Generate a new encrypted wallet file:

```bash
jm-wallet generate --output ~/.joinmarket-ng/wallets/default.mnemonic
```

**IMPORTANT**: Write down the displayed mnemonic - it's your only backup!

Or import an existing mnemonic (e.g., migrating from reference implementation):

```bash
jm-wallet import --output ~/.joinmarket-ng/wallets/default.mnemonic
```

See [jmwallet](README-jmwallet.md) for wallet management details.

### 2. Check Balance & Get Deposit Address

```bash
# View balance and addresses
jm-wallet info --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic --backend neutrino

# Or use jm-maker to get a specific address
jm-maker generate-address --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

### 3. Fund Your Wallet

Send bitcoin to displayed addresses. For best results, spread funds across multiple mixdepths (0-4).

**Minimum**: ~100,000 sats per mixdepth to create offers.

### 4. Start Earning Fees

#### Option A: Bitcoin Core Full Node (Recommended)

For maximum trustlessness, privacy, and compatibility with all takers. Configure your Bitcoin Core credentials in the config file:

```bash
nano ~/.joinmarket-ng/config.toml
```

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_user = "your_rpc_user"
rpc_password = "your_rpc_password"
```

Start maker bot:

```bash
jm-maker start --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

#### Option B: Neutrino Backend

Lightweight alternative if you cannot run a full node. Note that Neutrino makers can only participate in CoinJoins with takers that support `neutrino_compat` mode.

Start Neutrino server:

```bash
docker run -d \
  --name neutrino \
  -p 8334:8334 \
  -v neutrino-data:/data/neutrino \
  -e NETWORK=mainnet \
  -e LOG_LEVEL=info \
  ghcr.io/m0wer/neutrino-api
```

**Note**: Pre-built binaries available at [m0wer/neutrino-api releases](https://github.com/m0wer/neutrino-api/releases).

Configure in `~/.joinmarket-ng/config.toml`:

```toml
[bitcoin]
backend_type = "neutrino"
neutrino_url = "http://127.0.0.1:8334"
```

Start maker bot:

```bash
jm-maker start --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

The bot will:

- Sync your wallet
- Create offers based on available balance
- Create an ephemeral Tor .onion address (if Tor control available)
- Connect to directory servers and wait for takers

> **⚠️ Production Warning:** Without Tor control access, maker falls back to `NOT-SERVING-ONION` mode (all traffic via directory). Check logs for Tor warnings.

## Configuration

All settings can be configured in `~/.joinmarket-ng/config.toml`. CLI arguments and environment variables override the config file.

### Default Fee Settings

The defaults are sensible for most users:

- **Fee model**: Relative fees (0.1%)
- **Minimum size**: 100,000 sats

To customize fees, add to your config file:

```toml
[maker]
cj_fee_relative = 0.001   # 0.1% fee
min_size = 100000         # Minimum CoinJoin size in sats
```

### Offer Types and Fees

JoinMarket supports two fee models. The maker bot **automatically detects** which model to use based on which fee parameter you provide:

#### Relative Fees (Default)

Charge a percentage of the CoinJoin amount:

- **Auto-selected when**: You provide `--cj-fee-relative` (or neither fee parameter)
- **Offer type**: `sw0reloffer`
- **Default**: 0.1% (0.001)
- **Example**: 0.2% of 1 BTC = 200,000 sats
- **Pros**: Scales with transaction size, competitive for large amounts
- **Cons**: May earn less on small transactions

#### Absolute Fees

Charge a fixed satoshi amount regardless of CoinJoin size:

- **Auto-selected when**: You provide `--cj-fee-absolute`
- **Offer type**: `sw0absoffer`
- **Default**: Not used (relative is default)
- **Example**: Fixed 1000 sats per CoinJoin
- **Pros**: Predictable earnings, better for small transactions
- **Cons**: May be uncompetitive for large amounts

**Important**: Only provide ONE fee parameter. If you provide both, the maker will exit with an error.

### Custom Fee Settings

```bash
# Relative fee (0.2%) - auto-selects sw0reloffer
jm-maker start \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --backend-type neutrino \
  --cj-fee-relative 0.002 \
  --min-size 200000

# Absolute fee (1000 sats) - auto-selects sw0absoffer
jm-maker start \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --backend-type neutrino \
  --cj-fee-absolute 1000 \
  --min-size 200000
```

### Fidelity Bonds (Advanced)

Increase offer visibility by locking bitcoin for a period. See wallet CLI:

```bash
# Generate bond address (saves to registry for auto-discovery)
jm-wallet generate-bond-address \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --locktime 1735689600

# List existing bonds (also updates registry with UTXO info)
jm-wallet list-bonds --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic

# Discover bonds by scanning specific locktimes (updates registry)
jm-wallet list-bonds --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --locktime 1735689600

# View registry entries
jm-wallet registry-list

# Sync registry with blockchain (check funding status)
jm-wallet registry-sync --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

**Auto-discovery**: Bonds created with `generate-bond-address` are saved to the bond registry (`~/.joinmarket-ng/fidelity_bonds.json`). The maker bot **automatically discovers** these bonds at startup - no need to specify locktimes manually.

If you need to manually specify locktimes (e.g., for bonds created outside this tool):

```bash
jm-maker start \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --fidelity-bond-locktimes 1735689600
```

## Migrating from JoinMarket Reference Implementation

If you have an existing maker on the reference implementation (JoinMarket-Org/joinmarket-clientserver), migration is simple - **all you need is your mnemonic**.

### Quick Migration

1. **Import your mnemonic** with the interactive import command:

```bash
jm-wallet import --output ~/.joinmarket-ng/wallets/default.mnemonic
```

This provides word-by-word entry with Tab completion and auto-complete when only one BIP39 word matches your prefix. The mnemonic is validated and optionally encrypted.

Alternatively, pass the mnemonic via environment variable:

```bash
MNEMONIC="your twelve or twenty-four word mnemonic phrase here ..." \
  jm-wallet import --output ~/.joinmarket-ng/wallets/default.mnemonic
```

2. **Start the maker** - fidelity bonds are auto-discovered:

```bash
jm-maker start --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

That's it! The maker will:

- Derive all addresses from your mnemonic (same BIP84 paths as reference implementation)
- **Automatically discover any existing fidelity bonds** by scanning the blockchain
- Start serving offers with your existing balance

### Fidelity Bond Recovery

JoinMarket-NG can scan all 960 possible fidelity bond timelocks (Jan 2020 - Dec 2099) to find your existing bonds. This is useful when migrating from another wallet or recovering from backup.

**Full bond recovery** (scans all timelocks, may take time on mainnet):

```bash
jm-wallet recover-bonds --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

**Quick discovery** (if you know the locktime):

```bash
jm-wallet list-bonds --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --locktime 1735689600
```

Both commands update the bond registry (`fidelity_bonds.json`) with discovered bonds and their UTXO information.

**BIP39 Passphrase**: If your wallet uses a BIP39 passphrase (13th/25th word), you must provide it via interactive prompt or environment variable:

```bash
# Via interactive prompt
jm-wallet recover-bonds --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --prompt-bip39-passphrase

# Via environment variable
BIP39_PASSPHRASE="your passphrase" jm-wallet recover-bonds \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

Note: The BIP39 passphrase is intentionally NOT read from `config.toml` for security reasons.

## Docker Deployment

A production-ready `docker-compose.yml` is provided in this directory with:

- **Bitcoin Core backend** for maximum trustlessness and compatibility
- **Tor** with control port for ephemeral .onion address creation
- **Logging limits** to prevent disk exhaustion from log flooding
- **Resource limits** for CPU and memory
- **Health checks** for service dependencies

> **Note**: Bitcoin Core is strongly recommended for makers. Neutrino-based makers can only
> participate in CoinJoins with takers that support `neutrino_compat` mode, limiting your
> potential earnings and network compatibility.

### Quick Start

1. **Create Tor configuration directory:**

```bash
mkdir -p tor/conf tor/run
```

2. **Create `tor/conf/torrc`:**

```torc
# Minimal Tor configuration for JoinMarket maker
SocksPort 0.0.0.0:9050
ControlPort 0.0.0.0:9051
CookieAuthentication 1
CookieAuthFile /var/lib/tor/control_auth_cookie
DataDirectory /var/lib/tor
Log notice stdout
```

3. **Ensure your wallet is ready:**

```bash
mkdir -p ~/.joinmarket-ng/wallets
# Create or copy your mnemonic file to ~/.joinmarket-ng/wallets/default.mnemonic
```

4. **Update RPC credentials** in `docker-compose.yml` (change `rpcuser`/`rpcpassword`).

5. **Start the maker:**

```bash
docker-compose up -d
```

> **Note**: Initial Bitcoin Core sync can take several hours to days depending on hardware.

### Using Neutrino Instead of Bitcoin Core

If you cannot run a full node, Neutrino is available as a lightweight alternative.
Be aware this limits compatibility with takers.

Replace the `bitcoind` service with `neutrino` and update maker environment:

```yaml
environment:
  - BACKEND_TYPE=neutrino
  - BITCOIN__NEUTRINO_URL=http://neutrino:8334

# Replace bitcoind service with:
neutrino:
  image: ghcr.io/m0wer/neutrino-api
  environment:
    - NETWORK=mainnet
  volumes:
    - neutrino-data:/data/neutrino
```

### Customizing Fees

Edit the environment section in `docker-compose.yml`:

```yaml
environment:
  # Relative fee (0.1% - default)
  - MAKER__CJ_FEE_RELATIVE=0.001
  # OR absolute fee (uncomment one, not both)
  # - MAKER__CJ_FEE_ABSOLUTE=1000
  - MAKER__MIN_SIZE=100000
```

### Viewing Logs

```bash
docker-compose logs -f maker
```

## Environment Variables

### Required

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLET__MNEMONIC_FILE` | - | Path to encrypted mnemonic file (recommended) |
| `WALLET__MNEMONIC` | - | Direct mnemonic phrase (not recommended for production) |

### Tor Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TOR__SOCKS_HOST` | `127.0.0.1` | Tor SOCKS proxy host |
| `TOR__SOCKS_PORT` | `9050` | Tor SOCKS proxy port |
| `TOR__CONTROL_ENABLED` | Auto-detect | Enable Tor control port (auto-detected in Docker) |
| `TOR__CONTROL_HOST` | `127.0.0.1` | Tor control host |
| `TOR__CONTROL_PORT` | `9051` | Tor control port |
| `TOR__COOKIE_PATH` | Auto-detect | Path to Tor cookie auth file |

### Backend Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BITCOIN__BACKEND_TYPE` | `descriptor_wallet` | Backend type: `descriptor_wallet`, `scantxoutset`, or `neutrino` |
| `BITCOIN__RPC_URL` | `http://localhost:8332` | Bitcoin Core RPC URL (descriptor_wallet and scantxoutset) |
| `BITCOIN__RPC_USER` | - | Bitcoin Core RPC username (descriptor_wallet and scantxoutset) |
| `BITCOIN__RPC_PASSWORD` | - | Bitcoin Core RPC password (descriptor_wallet and scantxoutset) |
| `BITCOIN__NEUTRINO_URL` | `http://localhost:8334` | Neutrino REST API URL (neutrino only) |

### Network Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `NETWORK__NETWORK` | `mainnet` | Protocol network: `mainnet`, `testnet`, `signet`, `regtest` |
| `NETWORK__BITCOIN_NETWORK` | `$NETWORK__NETWORK` | Bitcoin network for address generation (if different from protocol network) |
| `NETWORK__DIRECTORY_SERVERS` | (network defaults) | JSON array of directory servers (e.g., `["host1:port1", "host2:port2"]`) |

### Fee Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MAKER__MIN_SIZE` | `100000` | Minimum CoinJoin size in sats |
| `MAKER__CJ_FEE_RELATIVE` | `0.001` | Relative fee (0.001 = 0.1%) - auto-selects `sw0reloffer` type |
| `MAKER__CJ_FEE_ABSOLUTE` | - | Absolute fee in sats - auto-selects `sw0absoffer` type if set |
| `MAKER__TX_FEE_CONTRIBUTION` | `0` | Transaction fee contribution in sats |

> **Important:** Only set ONE of `MAKER__CJ_FEE_RELATIVE` or `MAKER__CJ_FEE_ABSOLUTE`. The offer type is automatically selected based on which you provide.

### Advanced Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MAKER__FIDELITY_BOND_LOCKTIMES` | Auto-discover | JSON array of Unix timestamps for fidelity bond locktimes (e.g., `["1893456000", "1924992000"]`) |
| `MAKER__MERGE_ALGORITHM` | `default` | UTXO selection: `default`, `gradual`, `greedy`, `random` |
| `NETWORK__ONION_SERVING_HOST` | `127.0.0.1` | Bind address for incoming connections (set to `0.0.0.0` in Docker) |
| `NETWORK__ONION_SERVING_PORT` | `5222` | Port for incoming .onion connections |
| `NETWORK__TOR_TARGET_HOST` | `127.0.0.1` | Target hostname for Tor hidden service (set to service name in Docker Compose) |
| `WALLET__JOINMARKET_DATA_DIR` | `~/.joinmarket-ng` | Data directory for history and blacklist |

## CLI Reference

```bash
# Start maker bot
jm-maker start [OPTIONS]

# Generate receive address
jm-maker generate-address [OPTIONS]

# See all options
jm-maker start --help
```

### Key Options

| Option | Default | Description |
|--------|---------|-------------|
| `--mnemonic-file` | - | Path to encrypted wallet file |
| `--backend-type` | descriptor_wallet | Backend: descriptor_wallet, scantxoutset, or neutrino |
| `--cj-fee-relative` | 0.001 | Relative fee (0.001 = 0.1%) - auto-selects sw0reloffer |
| `--cj-fee-absolute` | - | Absolute fee in sats - auto-selects sw0absoffer |
| `--min-size` | 100000 | Minimum CoinJoin size in sats |
| `--tor-control-host` | Auto-detect | Tor control port host |
| `--tor-control-port` | 9051 | Tor control port |
| `--tor-cookie-path` | Auto-detect | Path to Tor cookie auth file |
| `--disable-tor-control` | - | Disable ephemeral hidden service creation (NOT recommended) |

Use env vars for RPC credentials (see jmwallet README).

## Security

- Wallet files are encrypted - keep your password safe
- Bot verifies all transactions before signing
- All directory connections go through Tor
- Never expose your mnemonic or share wallet files
- File permissions automatically set to 600

### Spam Protection

The maker includes automatic rate limiting with exponential backoff to prevent orderbook spam attacks:

- **Normal**: 1 response per 10 seconds
- **After 10 violations**: Backoff to 60 seconds (moderate)
- **After 50 violations**: Backoff to 300 seconds (severe)
- **After 100 violations**: Peer banned for 1 hour

Thresholds are configurable via environment variables if needed (see config.py).

## Troubleshooting

**"No offers created"**
- Check balance: `jm-wallet info --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic`
- Need at least 100,000 sats per mixdepth by default

**"Failed to connect to directory server"**
- Ensure Tor is running
- Check network connectivity

**"Transaction verification failed"**
- Safety feature - invalid transaction from taker
- Your funds are safe, no action needed

## Command Reference

<!-- AUTO-GENERATED HELP START: jm-maker -->

<details>
<summary><code>jm-maker --help</code></summary>

```

 Usage: jm-maker [OPTIONS] COMMAND [ARGS]...

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ───────────────────────────────────────────────────────────────────╮
│ start              Start the maker bot.                                      │
│ generate-address   Generate a new receive address.                           │
│ config-init        Initialize the config file with default settings.         │
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
│ --mnemonic-file         -f      PATH                  Path to mnemonic file  │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
│ --data-dir              -d      PATH                  Data directory for     │
│                                                       JoinMarket files.      │
│                                                       Defaults to            │
│                                                       ~/.joinmarket-ng       │
│                                                       [env var:              │
│                                                       JOINMARKET_DATA_DIR]   │
│ --network                       [mainnet|testnet|sig  Protocol network       │
│                                 net|regtest]          (mainnet, testnet,     │
│                                                       signet, regtest)       │
│ --bitcoin-network               [mainnet|testnet|sig  Bitcoin network for    │
│                                 net|regtest]          address generation     │
│                                                       (defaults to           │
│                                                       --network)             │
│ --backend-type                  TEXT                  Backend type:          │
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
│ --min-size                      INTEGER               Minimum CoinJoin size  │
│                                                       in sats                │
│ --cj-fee-relative               TEXT                  Relative coinjoin fee  │
│                                                       (e.g., 0.001 = 0.1%)   │
│                                                       [env var:              │
│                                                       CJ_FEE_RELATIVE]       │
│ --cj-fee-absolute               INTEGER               Absolute coinjoin fee  │
│                                                       in sats. Mutually      │
│                                                       exclusive with         │
│                                                       --cj-fee-relative.     │
│                                                       [env var:              │
│                                                       CJ_FEE_ABSOLUTE]       │
│ --tx-fee-contribution           INTEGER               Tx fee contribution in │
│                                                       sats                   │
│ --directory             -D      TEXT                  Directory servers      │
│                                                       (comma-separated       │
│                                                       host:port)             │
│                                                       [env var:              │
│                                                       DIRECTORY_SERVERS]     │
│ --tor-socks-host                TEXT                  Tor SOCKS proxy host   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_HOST)       │
│ --tor-socks-port                INTEGER               Tor SOCKS proxy port   │
│                                                       (overrides             │
│                                                       TOR__SOCKS_PORT)       │
│ --tor-control-host              TEXT                  Tor control port host  │
│                                                       (overrides             │
│                                                       TOR__CONTROL_HOST)     │
│ --tor-control-port              INTEGER               Tor control port       │
│                                                       (overrides             │
│                                                       TOR__CONTROL_PORT)     │
│ --tor-cookie-path               PATH                  Path to Tor cookie     │
│                                                       auth file (overrides   │
│                                                       TOR__COOKIE_PATH)      │
│ --disable-tor-control                                 Disable Tor control    │
│                                                       port integration       │
│ --onion-serving-host            TEXT                  Bind address for       │
│                                                       incoming connections   │
│                                                       (overrides             │
│                                                       MAKER__ONION_SERVING_… │
│ --onion-serving-port            INTEGER               Port for incoming      │
│                                                       .onion connections     │
│                                                       (overrides             │
│                                                       MAKER__ONION_SERVING_… │
│ --tor-target-host               TEXT                  Target hostname for    │
│                                                       Tor hidden service     │
│                                                       (overrides             │
│                                                       TOR__TARGET_HOST)      │
│ --fidelity-bond-lockt…  -L      INTEGER               Fidelity bond          │
│                                                       locktimes to scan for  │
│ --fidelity-bond-index   -I      INTEGER               Fidelity bond          │
│                                                       derivation index       │
│                                                       [env var:              │
│                                                       FIDELITY_BOND_INDEX]   │
│ --fidelity-bond         -B      TEXT                  Specific fidelity bond │
│                                                       to use (format:        │
│                                                       txid:vout)             │
│ --no-fidelity-bond                                    Disable fidelity bond  │
│                                                       usage. Skips registry  │
│                                                       lookup and bond proof  │
│                                                       generation even when   │
│                                                       bonds exist in the     │
│                                                       registry.              │
│ --merge-algorithm       -M      TEXT                  UTXO selection         │
│                                                       strategy: default,     │
│                                                       gradual, greedy,       │
│                                                       random                 │
│                                                       [env var:              │
│                                                       MERGE_ALGORITHM]       │
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
│ --log-level             -l      TEXT                  Log level              │
│ --help                                                Show this message and  │
│                                                       exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-maker generate-address --help</code></summary>

```

 Usage: jm-maker generate-address [OPTIONS]

 Generate a new receive address.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --mnemonic-file         -f      PATH                  Path to mnemonic file  │
│ --prompt-bip39-passph…                                Prompt for BIP39       │
│                                                       passphrase             │
│                                                       interactively          │
│ --network                       [mainnet|testnet|sig  Protocol network       │
│                                 net|regtest]                                 │
│ --bitcoin-network               [mainnet|testnet|sig  Bitcoin network for    │
│                                 net|regtest]          address generation     │
│                                                       (defaults to           │
│                                                       --network)             │
│ --backend-type                  TEXT                  Backend type           │
│ --log-level             -l      TEXT                  Log level              │
│ --help                                                Show this message and  │
│                                                       exit.                  │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>

<details>
<summary><code>jm-maker config-init --help</code></summary>

```

 Usage: jm-maker config-init [OPTIONS]

 Initialize the config file with default settings.

╭─ Options ────────────────────────────────────────────────────────────────────╮
│ --data-dir  -d      PATH  Data directory for JoinMarket files                │
│                           [env var: JOINMARKET_DATA_DIR]                     │
│ --help                    Show this message and exit.                        │
╰──────────────────────────────────────────────────────────────────────────────╯
```

</details>


<!-- AUTO-GENERATED HELP END: jm-maker -->
