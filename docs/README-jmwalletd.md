# JoinMarket Wallet Daemon (jmwalletd)

JAM-compatible HTTP and WebSocket daemon for JoinMarket NG.

`jmwalletd` exposes wallet, transaction, CoinJoin, maker, and tumbler
operations over a FastAPI-based REST API that is compatible with the reference
JoinMarket `jmwalletd`, so [JAM](https://jamapp.org/) (the JoinMarket web UI)
can talk to JoinMarket NG without modification.

## What It Provides

- REST API under `/api/v1` (wallet, direct send, CoinJoin, maker, tumbler)
- WebSocket notifications on `/ws`, `/api/v1/ws`, and `/jmws`
- JWT authentication (access and refresh tokens)
- Orderbook proxy endpoints under `/obwatch/*` (also without the `/api/v1`
  prefix, as JAM expects)
- Optional serving of the JAM web UI static files

## Installation

`jmwalletd` is not installed by the `install.sh` installer. Install it from a
clone of the repository in a virtualenv. It uses the maker, taker, and tumbler
components at runtime, so install those too:

```bash
python -m venv jmvenv
source jmvenv/bin/activate
python -m pip install -e ./jmcore -e ./jmwallet -e ./maker -e ./taker \
    -e ./tumbler -e ./jmwalletd
```

Alternatively, use the standalone Docker image published by this repository:

```bash
docker pull ghcr.io/joinmarket-ng/joinmarket-ng/jmwalletd:latest
```

For a single-application install that bundles the JAM web UI, jmwalletd, Tor,
and neutrino, see the [Flatpak page](flatpak.md).

## Running

Start the daemon:

```bash
jmwalletd serve
```

Defaults:

- bind host: `127.0.0.1`
- port: `28183`
- TLS: enabled, with an auto-generated self-signed certificate stored in
  `~/.joinmarket-ng/ssl/`

Common options:

```bash
# run plain HTTP (useful behind a reverse proxy or in local Docker)
jmwalletd serve --no-tls

# listen on all interfaces
jmwalletd serve --host 0.0.0.0 --no-tls

# custom data dir and config file
jmwalletd serve --data-dir /path/to/data --config-file /path/to/config.toml
```

The `JMWALLETD_HOST`, `JMWALLETD_NO_TLS`, `JOINMARKET_DATA_DIR`, and
`JOINMARKET_CONFIG_FILE` environment variables mirror the CLI options.

## Connecting JAM

Point JAM at the daemon URL (default `https://127.0.0.1:28183`). With the
default self-signed certificate your browser asks for a one-time exception.

For a combined Docker deployment, use the `standalone-ng` image maintained by
[`joinmarket-webui/jam-docker`](https://github.com/joinmarket-webui/jam-docker/tree/master/standalone-ng).
It runs JAM, jmwalletd, Tor, and the orderbook watcher behind plain HTTP nginx
on container port `80`; jmwalletd remains on its TLS-enabled loopback port
inside the container. The JoinMarket NG repository only publishes the
standalone `jmwalletd` image.

If a JAM build is present in `<data-dir>/jam` (or the system paths
`/usr/share/jmwalletd/jam` and `/app/share/jmwalletd/jam` used by packaged
installs), the daemon serves the UI itself, so opening the daemon URL in a
browser is enough.

## Configuration

`jmwalletd` uses the shared JoinMarket NG config (`~/.joinmarket-ng/config.toml`)
and the same environment override model as the other components (see
[Configuration](technical/configuration.md)).

The most relevant sections:

- `[network_config]` for network selection (`network`, `bitcoin_network`,
  `directory_servers`)
- `[bitcoin]` for the backend (`descriptor_wallet` or `neutrino`)
- `[tor]` for SOCKS and control settings
- `[taker]` policy settings, honored by CoinJoins and tumbles started
  through the API
- `[tumbler]` pacing settings for API-run tumbles

Orderbook proxy target resolution order:

1. `OBWATCH_URL` env var
2. `orderbook_watcher.http_host` + `orderbook_watcher.http_port`
3. fallback `http://127.0.0.1:8000`

## Development

Run the unit tests for this component:

```bash
pytest jmwalletd
```

For Docker-backed integration and JAM-compatibility (reference) coverage, use
the root test workflow:

```bash
./scripts/run_all_tests.sh
```
