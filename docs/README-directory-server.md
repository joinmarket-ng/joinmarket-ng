# JoinMarket Directory Server

Relay server for peer discovery and message routing in the JoinMarket network.

## Features

- **Peer Discovery**: Register and discover active peers
- **Message Routing**: Forward public broadcasts and private messages
- **Connection Management**: Handle peer connections and disconnections
- **Handshake Protocol**: Verify peer compatibility and network
- **High Performance**: Async I/O with optimized message handling
- **Observability**: Structured logging with loguru
- **Tor Hidden Service**: Run behind Tor for privacy (via separate container)

## Running

### Docker Compose quick start (Recommended)

Use the `directory_server/docker-compose.yml` deployment for production-style operation
behind a Tor hidden service. The compose stack isolates the directory server on an internal
network with no direct internet access; only the Tor container bridges both internal and
external networks.

1. Go to the `directory_server` directory.
2. Run `docker-compose up` (or `docker-compose up -d` for non-interactive run)

Retrieve your `.onion` address:

```bash
cat tor/data/hostname
```

#### Docker Notes

##### Network Architecture

```
Internet <--> [tor container] <-- internal network --> [directory_server container]
```

- `directory_server` lives on `joinmarket_directory_internal` (bridge, internal) -- no
  direct internet access, no information leakage.
- `tor` bridges `joinmarket_directory_internal` and `joinmarket_directory_external`,
  exposing the directory server only as a Tor hidden service.

##### Debug Image

A debug variant with pdbpp and memray pre-installed is available:

```bash
docker pull ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:main-debug
```

Profile memory with memray:

```bash
docker run -it --rm \
  -v $(pwd)/memray-output:/app/memray-output \
  ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:main-debug \
  memray run -o /app/memray-output/profile.bin -m directory_server.main
```

To attach memray to a running container, add `cap_add: [SYS_PTRACE]` to the
service in `docker-compose.yml`, then:

```bash
docker exec -it joinmarket_directory_server \
  python -m memray attach 1 --verbose
```

### Installation without Docker

See [Installation](install.md) for general setup. For local development:

```bash
cd joinmarket-ng
source jmvenv/bin/activate  # If you used install.sh
# OR create venv: python3 -m venv jmvenv && source jmvenv/bin/activate

# Install jmcore first
cd jmcore
pip install -e .

# Install directory server
cd ../directory_server
pip install -e .

# Development
pip install -e ".[dev]"
```

#### Configuration

Create a `.env` file or set environment variables:

```bash
# Network
NETWORK=mainnet  # mainnet, testnet, signet, regtest
HOST=127.0.0.1
PORT=5222

# Server
MAX_PEERS=10000
MESSAGE_RATE_LIMIT=100
LOG_LEVEL=INFO
```

Heartbeat liveness settings (section `[directory_server]` in `config.toml`):

- `heartbeat_sweep_interval` (default `60.0`)
- `heartbeat_idle_threshold` (default `600.0`)
- `heartbeat_hard_evict` (default `1500.0`)
- `heartbeat_pong_wait` (default `30.0`)

Behavior summary:

- Idle peers are probed with PING/PONG when they advertise `"ping": true` in handshake features
- Legacy/non-ping makers receive `!orderbook` as a compatibility liveness probe
- Peers idle beyond hard eviction threshold are disconnected

## Optional

### Vanity Onion Address

Generate a vanity `.onion` address using
[mkp224o](https://github.com/cathugger/mkp224o) before first start.
The tool creates one output directory per matching address and runs until killed.
Five-character prefixes are fast; six characters can take hours.

```bash
docker run --rm -it --network none -v $PWD:/keys \
  ghcr.io/cathugger/mkp224o:master -d /keys desired_prefix
```

Copy the generated keys into the Tor data directory:

```bash
mv desired_prefix*.onion/hs_ed25519_public_key \
   desired_prefix*.onion/hs_ed25519_secret_key \
   desired_prefix*.onion/hostname \
   tor/data/
chown -R 1000:1000 tor/data/
```

If the stack is already running, restart Tor to pick up the new keys:

```bash
docker compose restart tor
cat tor/data/hostname
```

## Health Check & Monitoring

The directory server provides comprehensive health check and monitoring capabilities.

### Health Check Endpoint

An HTTP server runs on port 8080 (configurable via `HEALTH_CHECK_HOST` and `HEALTH_CHECK_PORT`) providing:

**`GET /health`** - Basic health check
```bash
curl http://localhost:8080/health
# {"status": "healthy"}
```

**`GET /status`** - Detailed server statistics
```bash
curl http://localhost:8080/status
# {
#   "network": "mainnet",
#   "uptime_seconds": 3600,
#   "server_status": "running",
#   "max_peers": 1000,
#   "stats": {
#     "total_peers": 150,
#     "connected_peers": 150,
#     "passive_peers": 45,
#     "active_peers": 105
#   },
#   "connected_peers": {
#     "total": 150,
#     "nicks": ["maker1", "taker1", ...]
#   },
#   "passive_peers": {
#     "total": 45,
#     "nicks": ["taker1", "taker2", ...]
#   },
#   "active_peers": {
#     "total": 105,
#     "nicks": ["maker1", "maker2", ...]
#   },
#   "active_connections": 150
# }
```

### CLI Tool

Use `jm-directory-ctl` to query server status:

```bash
# Check server health
jm-directory-ctl health

# Get detailed status (human-readable)
jm-directory-ctl status

# Get status as JSON
jm-directory-ctl status --json

# Query remote server
jm-directory-ctl status --host 192.168.1.10 --port 8080
```

### Signal-based Status Logging

Send `SIGUSR1` signal to trigger detailed status logging to the server logs:

```bash
# Docker
docker kill -s SIGUSR1 joinmarket_directory_server

# Local process
kill -USR1 $(pgrep jm-directory-server)
```

This will log comprehensive status including:
- Network type and uptime
- Connected peers count and list
- Passive peers (orderbook watchers/takers - NOT-SERVING-ONION)
- Active peers (makers - serving onion address)
- Active connections

### Docker Health Check

The Docker image includes automatic health checks using the CLI command:

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["jm-directory-server", "health"]
```

Check container health status:
```bash
docker ps  # Shows (healthy) or (unhealthy)
docker inspect joinmarket_directory_server | grep -A 10 Health
```

## Protocol and Security Context

- Message envelope types and handshake flow: [Technical Protocol Notes](technical/protocol.md#transport-layer)
- Directory role in peer discovery and routing: [Technical Protocol Notes](technical/protocol.md#direct-vs-relay-connections)

## Command Reference

<!-- AUTO-GENERATED HELP START: jm-directory-ctl -->

<details>
<summary><code>jm-directory-ctl --help</code></summary>

```
usage: jm-directory-ctl [-h] [--host HOST] [--port PORT]
                        [--log-level LOG_LEVEL]
                        {status,health} ...

JoinMarket Directory Server CLI

positional arguments:
  {status,health}       Available commands
    status              Get server status
    health              Check server health

options:
  -h, --help            show this help message and exit
  --host HOST           Health check server host (default: 127.0.0.1)
  --port PORT           Health check server port (default: 8080)
  --log-level, -l LOG_LEVEL
                        Log level (default: INFO)
```

</details>

<details>
<summary><code>jm-directory-ctl status --help</code></summary>

```
usage: jm-directory-ctl status [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Output as JSON
```

</details>

<details>
<summary><code>jm-directory-ctl health --help</code></summary>

```
usage: jm-directory-ctl health [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Output as JSON
```

</details>


<!-- AUTO-GENERATED HELP END: jm-directory-ctl -->
