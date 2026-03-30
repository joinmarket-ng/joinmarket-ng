# Installation

This page covers the minimum path to install JoinMarket NG and run your first commands.

For day-to-day usage, continue with:

- [Wallet guide](README-jmwallet.md)
- [Taker guide](README-taker.md)
- [Maker guide](README-maker.md)

## Requirements

- Linux or macOS
- Python 3.11+
- A Bitcoin backend:
  - `descriptor_wallet` (Bitcoin Core, recommended), or
  - `neutrino` (light client)

## Recommended Install (Linux/macOS)

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
source ~/.joinmarket-ng/activate.sh
```

What this does:

- creates `~/.joinmarket-ng/venv`
- installs `jmcore`, `jmwallet`, `jm-maker`, and `jm-taker`
- creates `~/.joinmarket-ng/config.toml`
- installs/configures Tor unless you pass `--skip-tor`

Common options:

```bash
# taker only
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --taker

# maker only
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --maker

# skip Tor setup
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --skip-tor

# update existing installation
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash -s -- --update
```

## Configure Backend

Edit `~/.joinmarket-ng/config.toml`.

### Bitcoin Core (`descriptor_wallet`, recommended)

```toml
[bitcoin]
backend_type = "descriptor_wallet"
rpc_url = "http://127.0.0.1:8332"
rpc_user = "your_rpc_user"
rpc_password = "your_rpc_password"
```

### Neutrino (light client)

```toml
[bitcoin]
backend_type = "neutrino"
neutrino_url = "http://127.0.0.1:8334"
```

Neutrino server example:

```bash
docker run -d \
  --name neutrino \
  --restart unless-stopped \
  -p 8334:8334 \
  -v neutrino-data:/data/neutrino \
  -e NETWORK=mainnet \
  ghcr.io/m0wer/neutrino-api
```

## First Run

Create a wallet and inspect addresses:

```bash
jm-wallet generate
jm-wallet info
```

Then either:

```bash
# mix coins as taker
jm-taker coinjoin --amount 1000000 --destination INTERNAL

# or run maker bot
jm-maker start
```

## Manual Install (from source)

Use this for development or custom environments.

Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y git build-essential libffi-dev libsodium-dev pkg-config python3 python3-venv
```

macOS:

```bash
brew install libsodium pkg-config python3
```

Install packages:

```bash
git clone https://github.com/joinmarket-ng/joinmarket-ng.git
cd joinmarket-ng
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

python -m pip install -e ./jmcore
python -m pip install -e ./jmwallet
python -m pip install -e ./maker
python -m pip install -e ./taker
```

## Tor Notes

- Taker and orderbook watcher require Tor SOCKS (`127.0.0.1:9050`)
- Maker additionally uses Tor control (`127.0.0.1:9051`) for ephemeral onion services
- Directory server usually runs as a Tor hidden service in Docker (see [Directory Server](README-directory-server.md))

## Troubleshooting

- `jm-wallet: command not found`: run `source ~/.joinmarket-ng/activate.sh`
- build dependency errors on Linux: install `build-essential libffi-dev libsodium-dev pkg-config`
- Python venv issues: install `python3-venv`
- RPC failures: verify Bitcoin Core is reachable and credentials in `config.toml` are correct

## Next Docs

- [Wallet](README-jmwallet.md)
- [Taker](README-taker.md)
- [Maker](README-maker.md)
- [Technical Documentation](technical/index.md)
