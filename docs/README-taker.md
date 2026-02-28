# JoinMarket Taker Client

Mix your bitcoin for privacy via CoinJoin. Takers initiate transactions and pay small fees to makers.

## Installation

Install JoinMarket-NG with the taker component:

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --taker
```

See [Installation](install.md) for complete installation instructions including:

- Backend setup (Bitcoin Core or Neutrino)
- Tor configuration
- Manual installation for developers

## Quick Start

### 1. Create a Wallet

Generate an encrypted wallet file:

```bash
mkdir -p ~/.joinmarket-ng/wallets
jm-wallet generate --save --prompt-password --output ~/.joinmarket-ng/wallets/default.mnemonic
```

**IMPORTANT**: Write down the displayed mnemonic - it's your only backup!

See [Jmwallet](README-jmwallet.md) for wallet management details.

### 2. Check Balance & Get Deposit Address

```bash
# View balance and addresses
jm-wallet info --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic --backend neutrino
```

### 3. Fund Your Wallet

Send bitcoin to one of the displayed addresses.

### 4. Execute a CoinJoin

#### Option A: Bitcoin Core Full Node (Recommended)

For maximum trustlessness and privacy. Configure your Bitcoin Core credentials in the config file:

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

Execute CoinJoin:

```bash
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 1000000
```

#### Option B: Neutrino Backend

Lightweight alternative if you cannot run a full node.

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

Mix to next mixdepth (recommended for privacy):

```bash
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 1000000
```

This mixes 1,000,000 sats (0.01 BTC) to the next mixdepth in your wallet.

## Common Use Cases

### Mix Within Your Wallet

Default behavior - sends to next mixdepth (INTERNAL):

```bash
jm-taker coinjoin --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic --amount 500000
```

### Send to External Address

Mix and send to a specific address:

```bash
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 500000 \
  --destination bc1qexampleaddress...
```

### Sweep Entire Mixdepth

Use `--amount 0` to sweep all funds from a mixdepth:

```bash
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 0 \
  --mixdepth 2
```

### Enhanced Privacy (More Makers)

More counterparties = better privacy:

```bash
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 1000000 \
  --counterparties 6
```

## Tumbler (Automated Mixing)

For maximum privacy, use the tumbler to execute multiple CoinJoins over time.

### Create Schedule

Save as `schedule.json`:

```json
{
  "entries": [
    {
      "mixdepth": 0,
      "amount": 500000,
      "counterparty_count": 4,
      "destination": "INTERNAL",
      "wait_time": 300
    },
    {
      "mixdepth": 1,
      "amount": 0,
      "counterparty_count": 5,
      "destination": "bc1qfinaladdress...",
      "wait_time": 0
    }
  ]
}
```

**Fields**:

- `amount`: Sats (integer), fraction 0-1 (float), or 0 (sweep all)
- `destination`: Bitcoin address or "INTERNAL" for next mixdepth
- `wait_time`: Seconds to wait after this CoinJoin

### Run Tumbler

```bash
jm-taker tumble schedule.json --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic
```

## Configuration

All settings can be configured in `~/.joinmarket-ng/config.toml`. CLI arguments and environment variables override the config file.

### Default Settings

Sensible defaults for most users:

- **Destination**: INTERNAL (next mixdepth)
- **Counterparties**: 3 makers
- **Max absolute fee**: 500 sats per maker
- **Max relative fee**: 0.1% (0.001)

To customize, add to your config file:

```toml
[taker]
counterparty_count = 4
max_cj_fee_abs = 1000
max_cj_fee_rel = 0.002
```

### Custom Fee Limits

Lower fees (may find fewer makers):

```bash
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 1000000 \
  --max-abs-fee 200 \
  --max-rel-fee 0.0005
```

### Bondless Maker Selection

The taker uses fidelity bonds to select makers, but occasionally selects makers randomly to give bondless makers a chance. This is controlled by `--bondless-allowance` (default 12.5%).

To reduce the economic incentive for sybil attacks by bondless makers, the `--bondless-zero-fee` option (enabled by default) ensures that bondless maker spots only go to makers charging zero absolute fees. This removes the incentive to run many bondless bots to collect more fees.

```bash
# Disable zero-fee requirement for bondless spots (not recommended)
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 1000000 \
  --no-bondless-zero-fee

# Adjust bondless maker allowance
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 1000000 \
  --bondless-allowance 0.2
```

## Docker Deployment

A production-ready `docker-compose.yml` is provided in this directory with:

- **Bitcoin Core backend** for maximum trustlessness and privacy
- **Tor** for privacy (SOCKS proxy only - takers don't need control port)
- **Logging limits** to prevent disk exhaustion from log flooding
- **Resource limits** for CPU and memory
- **Health checks** for service dependencies

### Quick Start

1. **Create Tor configuration directory:**

```bash
mkdir -p tor/conf tor/data tor/run
```

2. **Create `tor/conf/torrc`:**

```torc
SocksPort 0.0.0.0:9050
DataDirectory /var/lib/tor
Log notice stdout
```

3. **Ensure your wallet is ready:**

```bash
mkdir -p ~/.joinmarket-ng/wallets
# Create or copy your mnemonic file to ~/.joinmarket-ng/wallets/default.mnemonic
```

4. **Update RPC credentials** in `docker-compose.yml` (change `rpcuser`/`rpcpassword`).

5. **Start Bitcoin Core and Tor:**

```bash
docker-compose up -d bitcoind tor
```

> **Note**: Initial Bitcoin Core sync can take several hours to days depending on hardware.

6. **Run a CoinJoin:**

```bash
docker-compose run --rm taker jm-taker coinjoin --amount 1000000
```

### Running the Tumbler

```bash
# Create schedule file
cat > ~/.joinmarket-ng/schedule.json << 'EOF'
{
  "entries": [
    {"mixdepth": 0, "amount": 500000, "counterparty_count": 4, "destination": "INTERNAL", "wait_time": 300},
    {"mixdepth": 1, "amount": 0, "counterparty_count": 5, "destination": "INTERNAL", "wait_time": 0}
  ]
}
EOF

# Run tumbler
docker-compose run --rm taker jm-taker tumble /home/jm/.joinmarket-ng/schedule.json
```

### Using Neutrino Instead of Bitcoin Core

If you cannot run a full node, Neutrino is available as a lightweight alternative.

Replace the `bitcoind` service with `neutrino` and update taker environment:

```yaml
environment:
  - BITCOIN__BACKEND_TYPE=neutrino
  - BITCOIN__NEUTRINO_URL=http://neutrino:8334

# Replace bitcoind service with:
neutrino:
  image: ghcr.io/m0wer/neutrino-api
  environment:
    - NETWORK=mainnet
  volumes:
    - neutrino-data:/data/neutrino
```

### Viewing Logs

```bash
docker-compose logs -f taker
```

Note: Takers only need Tor SOCKS proxy (port 9050) - they don't serve a hidden service, so no control port is needed.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `WALLET__MNEMONIC_FILE` | - | Path to mnemonic file (recommended) |
| `WALLET__MNEMONIC` | - | Direct mnemonic phrase (not recommended for production) |
| `BITCOIN__BACKEND_TYPE` | `descriptor_wallet` | Backend: `descriptor_wallet`, `scantxoutset`, or `neutrino` |
| `NETWORK__NETWORK` | `mainnet` | Protocol network for handshakes |
| `NETWORK__BITCOIN_NETWORK` | `$NETWORK__NETWORK` | Bitcoin network for address generation |
| `BITCOIN__RPC_URL` | `http://localhost:8332` | Bitcoin Core RPC URL (descriptor_wallet and scantxoutset) |
| `BITCOIN__RPC_USER` | - | Bitcoin Core RPC username (descriptor_wallet and scantxoutset) |
| `BITCOIN__RPC_PASSWORD` | - | Bitcoin Core RPC password (descriptor_wallet and scantxoutset) |
| `BITCOIN__NEUTRINO_URL` | `http://localhost:8334` | Neutrino REST API URL (neutrino only) |
| `NETWORK__DIRECTORY_SERVERS` | (mainnet defaults) | JSON array of directory servers (e.g., `["host1:port1", "host2:port2"]`) |
| `TAKER__COINJOIN_AMOUNT` | `1000000` | Default CoinJoin amount in sats |
| `TAKER__MIN_MAKERS` | `4` | Minimum number of makers |
| `TAKER__MAX_CJ_FEE_REL` | `0.001` | Maximum relative fee (0.1%) |
| `TAKER__MAX_CJ_FEE_ABS` | `5000` | Maximum absolute fee in sats |
| `TAKER__BONDLESS_MAKERS_ALLOWANCE` | `0.125` | Fraction of time to choose makers randomly (0.0-1.0) |
| `TAKER__BOND_VALUE_EXPONENT` | `1.3` | Exponent for fidelity bond value calculation |
| `TAKER__BONDLESS_REQUIRE_ZERO_FEE` | `true` | Require zero absolute fee for bondless maker spots |
| `TOR__SOCKS_HOST` | `127.0.0.1` | Tor SOCKS proxy host |
| `TOR__SOCKS_PORT` | `9050` | Tor SOCKS proxy port |
| `LOGGING__SENSITIVE_LOGGING` | `false` | Enable sensitive logging (set to `true`) |

## CLI Reference

```bash
# Execute single CoinJoin
jm-taker coinjoin [OPTIONS]

# Run tumbler schedule
jm-taker tumble SCHEDULE_FILE [OPTIONS]

# See all options
jm-taker coinjoin --help
jm-taker tumble --help
```

### Key Options

| Option | Default | Description |
|--------|---------|-------------|
| `--amount` | (required) | Amount in sats, 0 for sweep |
| `--destination` | INTERNAL | Address or INTERNAL for next mixdepth |
| `--mixdepth` | 0 | Source mixdepth (0-4) |
| `--counterparties` | 3 | Number of makers (more = better privacy) |
| `--backend` | descriptor_wallet | Backend: descriptor_wallet, scantxoutset, or neutrino |
| `--max-abs-fee` | 500 | Max absolute fee per maker (sats) |
| `--max-rel-fee` | 0.001 | Max relative fee (0.1%) |
| `--bondless-allowance` | 0.125 | Fraction of time to choose makers randomly (0.0-1.0) |
| `--bond-exponent` | 1.3 | Exponent for fidelity bond value calculation |
| `--bondless-zero-fee` | enabled | Require zero absolute fee for bondless spots |

Use env vars for RPC credentials (see jmwallet README).

## Privacy Tips

1. **Use INTERNAL destination**: Keeps funds in your wallet across mixdepths
2. **Multiple CoinJoins**: Use tumbler for enhanced privacy over time
3. **More counterparties**: `--counterparties 6` increases anonymity set
4. **Avoid round amounts**: Makes your output harder to identify
5. **Wait between mixes**: Add `wait_time` in tumbler schedules
6. **All via Tor**: Directory connections automatically use Tor

## Security

- Wallet files are encrypted - keep your password safe
- Transactions verified before signing
- PoDLE commitments prevent sybil attacks
- All directory connections via Tor
- Never expose your mnemonic or share wallet files

## Troubleshooting

**"No suitable makers found"**
- Check directory server connectivity
- Lower fee limits if too strict
- Try during peak hours

**"PoDLE commitment failed"**
- Need 5+ confirmations on UTXOs
- UTXO must be ≥20% of CoinJoin amount

**"Insufficient balance"**
- Check: `jm-wallet info --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic`
- Reserve some balance for fees

**"CoinJoin timeout"**
- Try fewer counterparties
- Network might be slow

## Command Reference

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
│ coinjoin               Execute a single CoinJoin transaction.                │
│ tumble                 Run a tumbler schedule of CoinJoins.                  │
│ clear-ignored-makers   Clear the list of ignored makers.                     │
│ config-init            Initialize the config file with default settings.     │
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
