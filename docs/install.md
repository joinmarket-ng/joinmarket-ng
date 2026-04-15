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
- installs static shell completion scripts for bash and zsh (near-instant tab completion)

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

## Updating

When you run `install.sh --update`, the installer:

- Upgrades all installed Python packages to the specified (or latest) version
- Checks your config for new settings: compares `config.toml` against the latest template and prints any new sections or keys that are available
- Refreshes shell completions and Tor configuration

Your existing config is never modified. If new settings are available, the installer prints them so you can add them manually from `config.toml.template`.

## Configure Backend

Edit `~/.joinmarket-ng/config.toml`.

If this is a manual/source install and the file does not exist yet:

```bash
mkdir -p ~/.joinmarket-ng/wallets
chmod 700 ~/.joinmarket-ng ~/.joinmarket-ng/wallets
curl -fsSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/config.toml.template -o ~/.joinmarket-ng/config.toml
```

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
neutrino_url = "https://127.0.0.1:8334"
neutrino_tls_cert = "~/.joinmarket-ng/neutrino/tls.cert"
neutrino_auth_token_file = "~/.joinmarket-ng/neutrino/auth_token"
```

JoinMarket NG does not generate this cert/token itself today. You need to
copy them from your neutrino-api instance once, then keep them in:

- `~/.joinmarket-ng/neutrino/tls.cert`
- `~/.joinmarket-ng/neutrino/auth_token`

Create the directory:

```bash
mkdir -p ~/.joinmarket-ng/neutrino
chmod 700 ~/.joinmarket-ng/neutrino
```

Neutrino server example (Docker):

On Linux, add your user to the Docker group once (skip if Docker already works without `sudo`):

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

```bash
docker run -d \
  --name neutrino \
  --restart unless-stopped \
  -p 8334:8334 \
  -v neutrino-data:/data/neutrino \
  -e NETWORK=mainnet \
  ghcr.io/m0wer/neutrino-api
```

Copy credentials from neutrino-api into JoinMarket NG config directory:

```bash
docker cp neutrino:/data/neutrino/tls.cert ~/.joinmarket-ng/neutrino/tls.cert
docker cp neutrino:/data/neutrino/auth_token ~/.joinmarket-ng/neutrino/auth_token
chmod 600 ~/.joinmarket-ng/neutrino/tls.cert ~/.joinmarket-ng/neutrino/auth_token
```

If you previously used `http://` neutrino:

1. Switch `neutrino_url` to `https://...`
2. Add `neutrino_tls_cert`
3. Add `neutrino_auth_token_file` (or `neutrino_auth_token`)
4. Restart JoinMarket NG

On low-power hardware, initial Neutrino sync can take significantly longer (for example, Raspberry Pi 4: ~20 minutes sync plus long prefetch).

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

Shell completions are pre-generated and installed automatically by the installer.
For editable (development) installs, source the static scripts from the repo:

```bash
# bash
source completions/jm-wallet.bash
source completions/jm-maker.bash
source completions/jm-taker.bash
source completions/jmwalletd.bash

# zsh (add to .zshrc)
for f in completions/*.zsh; do source "$f"; done
```

To regenerate after CLI changes:

```bash
python scripts/generate_completions.py
```

## Tor Notes

- Taker and orderbook watcher require Tor SOCKS (`127.0.0.1:9050`)
- Maker additionally uses Tor control (`127.0.0.1:9051`) for ephemeral onion services
- If you edit Tor config, restart Tor (`sudo systemctl restart tor` on Linux, `brew services restart tor` on macOS)
- Directory server usually runs as a Tor hidden service in Docker (see [Directory Server](README-directory-server.md))

On Debian/Ubuntu maker setups, Tor cookie auth often requires `debian-tor` group access:

```bash
sudo usermod -aG debian-tor "$USER"
newgrp debian-tor
```

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
