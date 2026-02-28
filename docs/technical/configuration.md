### Config File

JoinMarket NG uses TOML configuration at `~/.joinmarket-ng/config.toml`.

**Priority (highest to lowest):**

1. CLI arguments
2. Environment variables
3. Config file
4. Built-in defaults

**Auto-Generation:** On first run, config is created with all settings commented out, showing defaults.

**Environment Variable Mapping:**

| Config File | Environment Variable |
|-------------|---------------------|
| `[tor]` `socks_host` | `TOR__SOCKS_HOST` |
| `[bitcoin]` `rpc_url` | `BITCOIN__RPC_URL` |
| `[maker]` `min_size` | `MAKER__MIN_SIZE` |

**Configuration Sections:**

| Section | Description |
|---------|-------------|
| `[tor]` | SOCKS proxy and control port |
| `[bitcoin]` | Backend settings (RPC, Neutrino) |
| `[network]` | Protocol network, directory servers |
| `[wallet]` | HD wallet structure |
| `[notifications]` | Push notification settings |
| `[logging]` | Log level and options |
| `[maker]` | Maker-specific settings |
| `[taker]` | Taker-specific settings |

**Example:**

```toml
[tor]
socks_host = "127.0.0.1"
socks_port = 9050

[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_user = "jm"
rpc_password = "secret"

[network]
network = "mainnet"

[maker]
min_size = 50000
cj_fee_relative = "0.002"
merge_algorithm = "gradual"
```

### Tor Integration

All components use Tor for privacy:

| Component | SOCKS Proxy | Hidden Service |
|-----------|-------------|----------------|
| Directory Server | No | Permanent |
| Maker | Yes | Ephemeral (recommended) |
| Taker | Yes | No |
| Orderbook Watcher | Yes | No |

**Directory Server:** Requires permanent hidden service in torrc:

```
HiddenServiceDir /var/lib/tor/directory_hs
HiddenServiceVersion 3
HiddenServicePort 5222 127.0.0.1:5222
```

**Maker:** Uses SOCKS proxy for outgoing + ephemeral hidden service via control port:

```bash
jm-maker start \
  --socks-host=127.0.0.1 --socks-port=9050 \
  --tor-control-enabled \
  --tor-control-host=127.0.0.1 --tor-control-port=9051
```

Creates fresh `.onion` each session for better privacy.

**Taker/Orderbook:** SOCKS proxy only for outgoing connections.

### Notifications

Push notifications via [Apprise](https://github.com/caronc/apprise) supporting 100+ services.

**Configuration:**

```toml
[notifications]
urls = ["gotify://your-server.com/token", "tgram://bot/chat"]
include_amounts = true
include_txids = false  # Privacy risk
use_tor = true
```

**Environment variables:**

| Variable | Description |
|----------|-------------|
| `NOTIFICATIONS__URLS` | JSON array of Apprise URLs |
| `NOTIFICATIONS__INCLUDE_AMOUNTS` | Include satoshi amounts |
| `NOTIFICATIONS__INCLUDE_TXIDS` | Include transaction IDs |
| `NOTIFICATIONS__USE_TOR` | Route through Tor |

**Per-event toggles:** `notify_fill`, `notify_signing`, `notify_confirmed`, etc.

**Example URLs:**

```bash
# Gotify
export NOTIFICATIONS__URLS='["gotify://host/token"]'

# Telegram
export NOTIFICATIONS__URLS='["tgram://bot_token/chat_id"]'

# Multiple services
export NOTIFICATIONS__URLS='["gotify://host/token", "tgram://bot/chat"]'
```

### Transaction Policies

**Dust Threshold:**

Default: 27,300 satoshis (reference implementation compatible)

This is higher than Bitcoin Core's relay dust (546 sats for P2WPKH) to avoid creating outputs that may be expensive to spend relative to their value.

| Scenario | Threshold |
|----------|-----------|
| Change output < threshold | Donated to fees |
| CoinJoin output < threshold | Transaction rejected |

**Minimum Relay Fee:**

Bitcoin Core default: 1.0 sat/vB

For sub-satoshi fee rates, configure `minrelaytxfee` in `bitcoin.conf`:

```ini
minrelaytxfee=0.0000001  # 0.1 sat/vB
```

**Bitcoin Amount Handling:**

All amounts are **integer satoshis** internally. Do not use float or Decimal.

---
