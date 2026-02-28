# JoinMarket-NG Installation Guide

Complete guide for installing JoinMarket-NG on Linux, macOS, and Raspberry Pi.

## System Requirements

- **Python**: 3.11 or higher (3.14 recommended)
- **OS**: Linux, macOS, or Raspberry Pi OS
- **Disk**: ~100MB for software, plus backend storage
- **Network**: Internet connection (Tor installed automatically)

---

## Quick Installation

**One-line install** (Linux/macOS):

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
```

The installer will:
- Install system dependencies (asks for confirmation)
- Install and configure Tor
- Create Python virtual environment at `~/.joinmarket-ng/venv/`
- Install JoinMarket-NG (maker and taker by default)
- Create config file at `~/.joinmarket-ng/config.toml`

### Installation Options

```bash
# Maker only
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --maker

# Taker only
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --taker

# Specific version
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --version 0.9.0

# Skip Tor (configure manually)
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --skip-tor
```

### After Installation

Start a new terminal or run:

```bash
source ~/.joinmarket-ng/activate.sh
```

### Updating

```bash
# Latest version
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --update

# Specific version
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --update --version 0.9.0
```

Restart any running maker/taker processes after updating.

---

## Manual Installation

For development or custom setups.

### System Dependencies

**Debian/Ubuntu/Raspberry Pi OS:**

```bash
sudo apt update
sudo apt install -y git build-essential libffi-dev libsodium-dev \
  pkg-config python3 python3-venv python3-pip
```

**macOS:**

```bash
brew install libsodium pkg-config python3
```

### Clone and Install

```bash
git clone https://github.com/joinmarket-ng/joinmarket-ng.git
cd joinmarket-ng

# Create virtual environment
python3 -m venv jmvenv
source jmvenv/bin/activate

# Install packages (in order)
cd jmcore && pip install -e . && cd ..
cd jmwallet && pip install -e . && cd ..
cd maker && pip install -e . && cd ..  # if using maker
cd taker && pip install -e . && cd ..  # if using taker
```

### Development Dependencies

```bash
cd jmcore && pip install -r requirements-dev.txt && cd ..
cd jmwallet && pip install -r requirements-dev.txt && cd ..
```

### Raspberry Pi Notes

**Python 3.11+:**

If your system Python is older than 3.11:

```bash
sudo apt install -y software-properties-common
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.11 python3.11-venv
```

Use `python3.11` instead of `python3` when creating the virtual environment.

**Memory:** Raspberry Pi 4 with 4GB+ RAM recommended. For less RAM, use Neutrino backend.

---

## Backend Setup

JoinMarket-NG requires a Bitcoin blockchain backend. Choose one:

### Option A: Bitcoin Core (Full Node)

**Pros:** Maximum privacy, trustlessness, full compatibility
**Cons:** ~600GB disk space, several days to sync

1. Install Bitcoin Core (v24+) from https://bitcoincore.org/en/download/

2. Configure `bitcoin.conf`:

```conf
server=1
rpcuser=yourusername
rpcpassword=yourpassword
rpcport=8332

# Optional: Reduce bandwidth
maxconnections=8
```

3. Start Bitcoin Core and wait for sync

4. Test connection:

```bash
bitcoin-cli getblockchaininfo
```

### Option B: Neutrino (Light Client)

**Pros:** ~500MB disk space, syncs in minutes
**Cons:** Less privacy than full node, some maker limitations

1. Install Docker:

```bash
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
```

2. Run Neutrino server:

```bash
docker run -d \
  --name neutrino \
  --restart unless-stopped \
  -p 8334:8334 \
  -v neutrino-data:/data/neutrino \
  -e NETWORK=mainnet \
  ghcr.io/m0wer/neutrino-api
```

Or download binaries from [neutrino-api releases](https://github.com/m0wer/neutrino-api/releases).

---

## Configuration

JoinMarket-NG uses TOML config at `~/.joinmarket-ng/config.toml`.

### Backend Configuration

Edit your config file:

```bash
nano ~/.joinmarket-ng/config.toml
```

**For Bitcoin Core:**

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_user = "your_rpc_user"
rpc_password = "your_rpc_password"
```

**For Neutrino:**

```toml
[bitcoin]
backend_type = "neutrino"
neutrino_url = "http://127.0.0.1:8334"
```

### Common Settings

```toml
[network]
network = "mainnet"  # mainnet, testnet, signet, regtest

[tor]
socks_host = "127.0.0.1"
socks_port = 9050

[maker]
cj_fee_relative = 0.001  # 0.1% fee
min_size = 100000        # Minimum 100k sats

[taker]
counterparty_count = 3   # Makers per CoinJoin
```

### Configuration Priority

Settings are loaded in order (highest priority first):

1. CLI arguments (`--backend neutrino`)
2. Environment variables (`BITCOIN__RPC_URL`)
3. Config file (`~/.joinmarket-ng/config.toml`)
4. Default values

---

## Tor Setup

JoinMarket-NG requires Tor for privacy. The installer configures Tor automatically.

### Automated Setup

When you run `install.sh`, it will:

1. Detect and install Tor (apt/brew)
2. Configure with localhost-only bindings:
   ```conf
   SocksPort 127.0.0.1:9050
   ControlPort 127.0.0.1:9051
   CookieAuthentication 1
   ```
3. Restart Tor and verify connectivity
4. Backup existing config before changes

### Manual Setup

If you used `--skip-tor` or need manual configuration:

**Install Tor:**

```bash
# Linux
sudo apt install -y tor

# macOS
brew install tor
```

**Configure** (`/etc/tor/torrc` on Linux, `$(brew --prefix)/etc/tor/torrc` on macOS):

```conf
SocksPort 127.0.0.1:9050
ControlPort 127.0.0.1:9051
CookieAuthentication 1
```

**Start Tor:**

```bash
# Linux
sudo systemctl start tor
sudo systemctl enable tor

# macOS
brew services start tor
```

**Verify:**

```bash
# SOCKS proxy
curl --socks5-hostname 127.0.0.1:9050 https://check.torproject.org/api/ip

# Control port (for makers)
nc -z 127.0.0.1 9051 && echo "Control port accessible"
```

### Component Requirements

| Component | SOCKS Proxy | Control Port |
|-----------|-------------|--------------|
| Taker | Yes | No |
| Orderbook Watcher | Yes | No |
| Maker | Yes | Yes |
| Directory Server | No | No (needs hidden service in torrc) |

Makers use the control port to create ephemeral hidden services dynamically.

### DoS Defense for Makers

For makers experiencing DoS attacks, use persistent hidden services with built-in defenses:

```conf
HiddenServiceDir /var/lib/tor/maker_hs
HiddenServiceVersion 3
HiddenServicePort 8765 127.0.0.1:8765

# Rate limiting (Tor 0.4.2+)
HiddenServiceEnableIntroDoSDefense 1
HiddenServiceEnableIntroDoSRatePerSec 25
HiddenServiceEnableIntroDoSBurstPerSec 200

# PoW defense (Tor 0.4.8+ with --enable-gpl)
HiddenServicePoWDefensesEnabled 1
HiddenServicePoWQueueRate 250
HiddenServicePoWQueueBurst 2500
```

| Feature | Ephemeral HS | Persistent HS |
|---------|--------------|---------------|
| Intro Point Rate Limiting | Not supported | Tor 0.4.2+ |
| PoW Defense | Tor 0.4.9.2+ | Tor 0.4.8+ |

### Troubleshooting Tor

**"Could not connect to Tor SOCKS proxy"**
- Verify Tor is running: `systemctl status tor`
- Check port: `nc -z 127.0.0.1 9050`

**"Could not authenticate to Tor control port"**
- Ensure `CookieAuthentication 1` in torrc
- Check cookie file permissions
- Add user to `debian-tor` group: `sudo usermod -a -G debian-tor $USER`

**"Control port not accessible"**
- Verify `ControlPort` in torrc
- Restart Tor after config changes

---

## Next Steps

### 1. Create a Wallet

```bash
mkdir -p ~/.joinmarket-ng/wallets
jm-wallet generate --save --prompt-password \
  --output ~/.joinmarket-ng/wallets/default.mnemonic
```

**IMPORTANT:** Write down the mnemonic - it's your only backup!

### 2. Start Using JoinMarket

**As a Taker** (mix your coins):

```bash
jm-taker coinjoin -f ~/.joinmarket-ng/wallets/default.mnemonic --amount 1000000
```

See [Taker](README-taker.md) for schedules and tumbler mode.

**As a Maker** (earn fees):

```bash
jm-maker start -f ~/.joinmarket-ng/wallets/default.mnemonic
```

See [Maker](README-maker.md) for configuration.

---

## Troubleshooting

### Build Errors

**"Could NOT find PkgConfig" / "CMake configuration failed"**

Install build dependencies:

```bash
# Debian/Ubuntu
sudo apt install -y build-essential libffi-dev libsodium-dev pkg-config

# macOS
brew install libsodium pkg-config
```

### Python Errors

**"python3: command not found"**

```bash
# Debian/Ubuntu
sudo apt install python3

# macOS
brew install python3
```

**"pip: command not found"**

```bash
sudo apt install python3-pip
# or
python3 -m ensurepip
```

**"externally-managed-environment" / "No module named 'venv'"**

```bash
sudo apt install python3-venv
```

### Installation Issues

**Installation takes a long time**

Some dependencies (like `coincurve`) compile from source. This is normal, especially on Raspberry Pi.

**curl | bash not working**

Download and run manually:

```bash
curl -o install.sh https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh
chmod +x install.sh
./install.sh
```

---

## Docker Deployment

For production or isolated environments:

```bash
git clone https://github.com/joinmarket-ng/joinmarket-ng.git
cd joinmarket-ng

# Maker
cd maker && docker-compose up -d

# Taker
cd taker && docker-compose up -d
```

See component READMEs for Docker-specific configuration.

---

## Uninstalling

```bash
# Remove virtual environment
rm -rf ~/.joinmarket-ng/venv/

# Remove data directory (CONTAINS WALLETS!)
# rm -rf ~/.joinmarket-ng/

# Remove shell integration from ~/.bashrc or ~/.zshrc
```

**Warning:** Back up your mnemonics before deleting `~/.joinmarket-ng/`!

---

## Getting Help

- **Technical docs:** [Technical Documentation](technical/index.md)
- **Component guides:** [Maker](README-maker.md), [Taker](README-taker.md)
- **Telegram:** https://t.me/joinmarketorg
- **SimpleX:** https://smp12.simplex.im/g#bx_0bFdk7OnttE0jlytSd73jGjCcHy2qCrhmEzgWXTk
