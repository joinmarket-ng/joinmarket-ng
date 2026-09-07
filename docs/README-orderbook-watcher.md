# Orderbook Watcher

The orderbook watcher aggregates maker offers from directory servers and exposes a web UI/API.

## Install

The [recommended installation](install.md) includes the watcher. For a
watcher-only installation or to add it to an existing installation, follow
[installation profiles](install-advanced.md#installation-profiles). This also
repairs older installations missing the watcher entry point.

From a source checkout:

```bash
python -m pip install -e ./jmcore
python -m pip install -e ./jmwallet
python -m pip install -e ./orderbook_watcher
```

Run:

```bash
jm-orderbook-watcher
```

The former `orderbook-watcher` command still works but is deprecated and
will be removed in a future release.

The reference JoinMarket implementation's `ob-watcher.py` is a separate
program. Its presence does not install the JoinMarket NG console script.

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
Set `orderbook_watcher.http_host = "0.0.0.0"` only when remote access is intentional and
protected by an appropriate firewall or reverse proxy.

## Web UI

The orderbook table's **Pick Chance** column estimates how often each offer is
included in a CoinJoin and displays the result as `1/N`. For example, `1/100`
means an offer is expected to be selected in one of every 100 rounds.

The estimate uses nine makers (the average of the default randomized range of
8 to 10), the default 5% bondless allowance, active bonded SW0 offers, and
zero-fee bondless SW0 offers. It calculates selection without replacement and
uses bond-value weighting for the default bonded slots; allowance slots select
uniformly from zero-fee offers. Fee and amount filtering are intentionally
omitted, so the estimate assumes every counted offer passes the taker's limits.
Offers sharing one active fidelity bond UTXO count as one candidate, matching
the taker's bond deduplication. Each sibling row shows the bond-level chance
with an asterisk because the omitted fee and amount filters determine which
offer the taker keeps.
Bonded inclusion chances come from a deterministic simulation of this
without-replacement chooser when weights or fee categories differ. The UI shows
`N/A` when fewer than nine qualifying offers are available. Hover over a Pick
Chance value, or select it by touch or keyboard, to see how that value was determined.

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
`scripts/run_parallel_tests.sh`.

## Related Docs

- [Directory Server](README-directory-server.md)
- [Installation](install.md)
- [Protocol](technical/protocol.md)
