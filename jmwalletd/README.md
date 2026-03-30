# jmwalletd

JAM-compatible HTTP and WebSocket daemon for JoinMarket NG.

## Overview

`jmwalletd` exposes wallet, transaction, and CoinJoin operations over a FastAPI API
compatible with the reference JoinMarket `jmwalletd`, so JAM can talk to JoinMarket NG.

## What It Provides

- REST API under `/api/v1`
- WebSocket notifications on `/ws`, `/api/v1/ws`, and `/jmws`
- JWT auth (access and refresh tokens)
- Orderbook proxy endpoints under `/obwatch/*`

## Run

Install in a virtualenv from repo root:

```bash
python -m pip install -e ./jmwalletd
```

Start the daemon:

```bash
jmwalletd serve
```

Defaults:

- bind host: `127.0.0.1`
- port: `28183`
- TLS: enabled with an auto-generated self-signed cert in `~/.joinmarket-ng/ssl/`

Common options:

```bash
# run plain HTTP (useful in local Docker)
jmwalletd serve --no-tls

# listen on all interfaces
jmwalletd serve --host 0.0.0.0 --no-tls

# custom data dir
jmwalletd serve --data-dir /path/to/data
```

## Configuration

`jmwalletd` uses the shared JoinMarket NG config (`~/.joinmarket-ng/config.toml`) and
the same environment override model as other components.

Important settings usually come from these sections:

- `[network_config]` for network selection (`network`, `bitcoin_network`)
- `[bitcoin]` for backend config (`descriptor_wallet` or `neutrino`)
- `[tor]` for SOCKS and control settings

Orderbook proxy target resolution is:

1. `OBWATCH_URL` env var
2. `orderbook_watcher.http_host` + `orderbook_watcher.http_port`
3. fallback `http://127.0.0.1:8000`

## Development

Run unit tests for this component:

```bash
pytest jmwalletd
```

For Docker-backed integration/e2e coverage, use the root test workflow:

```bash
./scripts/run_all_tests.sh
```
