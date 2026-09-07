---
search:
  boost: 2
---

# Install JoinMarket NG

This is the recommended Linux and macOS path. It installs the command-line
applications; configure a Bitcoin backend before creating or using a wallet.

## Before You Begin

- Linux or macOS, `curl`, and permission to install system packages. On macOS,
  install [Homebrew](https://brew.sh/) first.
- Python 3.11 or later. The installer checks this requirement and installs the
  supported build and verification dependencies where it can.
- A Bitcoin backend. Use your own synced, wallet-enabled Bitcoin Core node when
  possible. A Neutrino service is the lightweight alternative.

Windows and source installs are in [advanced installation](install-advanced.md).
For a browser interface, see the separate [JAM project](https://github.com/joinmarket-webui/jam).

## Install

The first command is an HTTPS bootstrap, so it trusts GitHub and your TLS
connection for this one download. The downloaded installer then verifies the
signed release and saves an authenticated local updater for later use.

```bash
curl -sSL https://raw.githubusercontent.com/joinmarket-ng/joinmarket-ng/main/install.sh | bash
source ~/.joinmarket-ng/activate.sh
```

The installer offers to install missing dependencies and configure Tor. On
Linux distributions without `apt`, install the dependencies it reports with
your system package manager.

## Continue

1. [Configure Bitcoin and Tor](setup.md).
2. [Create or import a wallet](getting-started.md).
3. Read the [taker guide](README-taker.md) or [maker guide](README-maker.md)
   before participating in CoinJoins.

## More Guides

### Configure Backend

[Set up Bitcoin Core, Neutrino, and Tor](setup.md#configure-backend).

### Tor Notes

[Tor requirements and manual configuration](setup.md#tor).

### Neutrino Service

For the [Neutrino setup path](setup.md#neutrino-lightweight-alternative), start
the service with Docker, then return to that guide for credentials and sync:

```bash
docker run -d \
  --name neutrino \
  --restart unless-stopped \
  -p 127.0.0.1:8334:8334 \
  -v neutrino-data:/data/neutrino \
  -e NETWORK=mainnet \
  -e PREFETCH_FILTERS=true \
  -e PREFETCH_LOOKBACK=105120 \
  ghcr.io/m0wer/neutrino-api:latest
```

### Updating

[Update an installation](update.md).

### Supply-chain Security

[Review the installer trust model](install-advanced.md#supply-chain-security).

### Manual Bootstrap Verification (Optional)

[Verify a bootstrap manually](install-advanced.md#manual-bootstrap-verification-optional).

### Manual Install from Source

[Install from source](install-advanced.md#manual-install-from-source).

### Windows (Manual Install)

[Install on Windows](install-advanced.md#windows-manual-install).

### Troubleshooting

[Troubleshoot installation and backend problems](troubleshooting.md).

### Tracking Wallet Sync Progress

[Check sync progress before considering a rescan](troubleshooting.md).

### Uninstall

[Remove software or a wallet safely](install-advanced.md#removal-and-wallet-safety).
