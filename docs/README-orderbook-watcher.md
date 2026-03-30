# Orderbook Watcher

The orderbook watcher aggregates maker offers from directory servers and exposes a web UI/API.

## Install

From source:

```bash
python -m pip install -e ./jmcore
python -m pip install -e ./jmwallet
python -m pip install -e ./orderbook_watcher
```

Run:

```bash
orderbook-watcher
```

## Configuration

Use `~/.joinmarket-ng/config.toml` and/or env vars.

Important settings:

- `NETWORK_CONFIG__NETWORK`
- `NETWORK_CONFIG__DIRECTORY_SERVERS`
- `DIRECTORY_NODES` (optional comma-separated override)
- `TOR__SOCKS_HOST`, `TOR__SOCKS_PORT`
- `ORDERBOOK_WATCHER__HTTP_HOST`, `ORDERBOOK_WATCHER__HTTP_PORT`
- `ORDERBOOK_WATCHER__UPDATE_INTERVAL`

## Docker

`orderbook_watcher/docker-compose.yml` provides watcher + Tor.

From `orderbook_watcher/`:

```bash
mkdir -p tor/conf tor/data tor/run
cat > tor/conf/torrc << 'EOF'
SocksPort 0.0.0.0:9050
ControlPort 0.0.0.0:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
Log notice stdout
EOF

docker compose up -d
```

UI is exposed on `http://localhost:8000` by default.

## API Endpoints

- `GET /` UI
- `GET /orderbook.json` aggregated offers
- `GET /health` healthcheck

## Related Docs

- [Directory Server](README-directory-server.md)
- [Installation](install.md)
- [Protocol](technical/protocol.md)
