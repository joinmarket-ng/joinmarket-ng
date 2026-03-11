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

### Swap Input (Fee Camouflage)

Add a swap input to make your CoinJoin indistinguishable from a maker's on-chain footprint. The taker acquires an extra UTXO via a Lightning reverse submarine swap that covers all fees plus a fake "fee earned" amount:

```bash
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 1000000 \
  --swap-input
```

With a specific swap provider (bypasses Nostr discovery):

```bash
jm-taker coinjoin \
  --mnemonic-file ~/.joinmarket-ng/wallets/default.mnemonic \
  --amount 1000000 \
  --swap-input \
  --swap-provider http://swap.example.com:9999
```

Or enable permanently in `~/.joinmarket-ng/config.toml`:

```toml
[swap]
enabled = true
# provider_url = "http://swap.example.com:9999"  # optional, otherwise uses Nostr discovery
# max_swap_fee_pct = 2.0

# When the swap amount is padded to the provider minimum, distribute leftover
# sats across maker fees to equalize them (improves privacy). Default: true
# equalize_fees = true

# Optional: LND connection for automatic invoice payment
# If configured, the taker pays swap invoices automatically via its own LND node.
# If not configured, the invoice is logged for manual payment (or the provider
# must be in mock mode for testing).
# lnd_rest_url = "https://127.0.0.1:8081"
# lnd_cert_path = "/path/to/tls.cert"
# lnd_macaroon_path = "/path/to/admin.macaroon"
```

**How it works:**

1. **Early discovery**: Before the CoinJoin confirmation prompt, the taker discovers a swap provider (via Nostr relays or direct URL) and checks that the expected swap amount meets the provider's minimum. The confirmation summary shows full swap details including provider fee, mining fee, and whether padding was applied.

2. **Fee equalization**: If the swap amount is padded up to the provider's minimum (e.g., from 5,000 to 20,000 sats), the leftover sats are distributed across maker fees using a sequential leveling algorithm. This makes all maker fees more uniform, preventing an observer from matching individual CoinJoin outputs to specific orderbook offers. Disable with `equalize_fees = false`.

3. **Stream isolation**: All swap-related Tor connections use separate circuits from directory and peer traffic, preventing traffic correlation.

4. **Nostr DM-based RPC**: When the provider is discovered via Nostr (no direct HTTP URL), all swap communication uses NIP-04 encrypted direct messages (kind 25582) over Nostr relays. The taker generates a fresh ephemeral keypair per session, encrypts requests with ECDH + AES-256-CBC, and signs events with BIP-340 Schnorr. Responses are correlated by `reply_to` field. This eliminates any direct network connection to the provider.

5. **Prepay invoice**: If the provider returns a `minerFeeInvoice` (a prepay for mining fees), the taker pays it before the main hold invoice. This matches the Electrum swap server protocol.

6. **Trustless lockup detection**: Instead of trusting the provider's `/swapstatus` endpoint, the taker monitors for the lockup UTXO directly on-chain using its own Bitcoin node (`BlockchainBackend`). This provides trustless confirmation that the provider has locked funds into the swap contract.

The swap input is best-effort: if the swap provider is unreachable or the swap fails, the CoinJoin proceeds normally without it. Not compatible with sweep mode (`--amount 0`).

See [Swap Input](technical/privacy.md#swap-input-taker-fee-camouflage) for the privacy rationale and protocol details.

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
| `SWAP__ENABLED` | `false` | Enable swap input for fee camouflage |
| `SWAP__PROVIDER_URL` | - | Direct swap provider URL (bypasses Nostr discovery) |
| `SWAP__MAX_SWAP_FEE_PCT` | `2.0` | Maximum swap fee as percentage of swap amount |
| `SWAP__EQUALIZE_FEES` | `true` | Distribute leftover sats across maker fees to equalize them |
| `SWAP__LND_REST_URL` | - | LND REST API URL for automatic invoice payment |
| `SWAP__LND_CERT_PATH` | - | Path to LND TLS certificate |
| `SWAP__LND_MACAROON_PATH` | - | Path to LND admin macaroon |

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
| `--swap-input` | disabled | Enable swap input for fee camouflage |
| `--swap-provider` | - | Direct swap provider URL (bypasses Nostr discovery) |

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
