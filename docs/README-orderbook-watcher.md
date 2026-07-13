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
- `ORDERBOOK_WATCHER__MEMPOOL_API_URL`
- `ORDERBOOK_WATCHER__MEMPOOL_API_USE_TOR` (defaults to `true`)

Mempool API lookups are disabled by default. When enabled, the watcher routes
them through Tor by default. Set `mempool_api_use_tor = false` only when a
direct endpoint is intentional, such as a local service. Direct access exposes
the watcher's source IP and the bond transactions it queries; it also ignores
`HTTP_PROXY` and `HTTPS_PROXY` inherited by the process.

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

## Testing

Backend unit tests:

```bash
pytest orderbook_watcher
```

Frontend (web UI) tests run the real static files in a headless browser with
fixture payloads; they need Node.js but no Docker stack:

```bash
cd tests/playwright
npm install && npx playwright install chromium
npm run test:obwatcher
```

They also run in CI (`test-playwright` job) and as part of
`scripts/run_all_tests.sh` and `scripts/run_parallel_tests.sh`.

## Related Docs

- [Directory Server](README-directory-server.md)
- [Installation](install.md)
- [Protocol](technical/protocol.md)
