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

## Installation

See [Installation](install.md) for general installation instructions.

**For directory server** (manual installation):

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

## Configuration

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

## Running

### Docker Compose (Recommended)

The recommended deployment uses Docker Compose with an isolated network where the directory server runs behind a Tor hidden service for privacy.

#### Initial Setup

**Important**: The Tor directories and configuration must be set up with proper permissions before starting Docker Compose. If not created manually, Docker will create them as root, causing permission errors.

```bash
# 1. Create directory structure with correct permissions
mkdir -p tor/conf tor/data tor/run
chmod 755 tor/conf tor/run
chmod 700 tor/data
chown -R 1000:1000 tor/

# 2. Create Tor configuration file
cat > tor/conf/torrc << 'EOF'
# JoinMarket Directory Server Hidden Service
HiddenServiceDir /var/lib/tor
HiddenServiceVersion 3
HiddenServicePort 5222 joinmarket_directory_server:5222
EOF

# 3. Start both directory server and Tor (uses pre-built image)
docker compose up -d

# 4. View logs
docker compose logs -f

# 5. Get your onion address (available after first tor startup)
cat tor/data/hostname

# Stop services
docker compose down
```

By default, docker-compose.yml uses the pre-built image `ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:main`. To build locally, uncomment the `build` section and comment out the `image` line in docker-compose.yml.

#### Debug Image

A debug variant is available with full Python debug symbols and debugging tools pre-installed:

- **pdbpp**: Enhanced Python debugger with syntax highlighting, tab completion, and sticky mode
- **memray**: Memory profiler for tracking allocations and finding memory leaks

```bash
# Pull the debug image
docker pull ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:main-debug

# Run with debug image
docker run -it --rm \
  -e LOG_LEVEL=DEBUG \
  ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:main-debug

# Profile memory usage with memray
docker run -it --rm \
  -v $(pwd)/memray-output:/app/memray-output \
  ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:main-debug \
  memray run -o /app/memray-output/profile.bin -m directory_server.main

# Attach debugger (requires adding breakpoint() in code)
docker run -it --rm \
  ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:main-debug
```

#### Live Profiling (Attach)

To attach memray to a running container, the `SYS_PTRACE` capability is required.

1. Add capability in `docker-compose.yml`:
```yaml
services:
  directory_server:
    image: ghcr.io/joinmarket-ng/joinmarket-ng/directory-server:main-debug
    cap_add:
      - SYS_PTRACE
```

2. Attach to the process:
```bash
docker exec -it jm_directory_server bash
# Inside container
python -m memray attach 1 --verbose
```

> **Tip**: If it does not work, trying `gdb -p 1` first can provide more details.

To build the debug image locally:
```bash
# Build debug target
docker build --target debug -t directory-server:debug -f directory_server/Dockerfile .

# Build production target (default)
docker build --target production -t directory-server:latest -f directory_server/Dockerfile .
```

#### Directory Structure After Setup

```
directory_server/
└── tor/
    ├── conf/
    │   └── torrc                    # Tor config (755, uid 1000)
    ├── data/                        # Hidden service keys (700, uid 1000)
    │   ├── hostname                 # Your .onion address (auto-generated)
    │   ├── hs_ed25519_public_key    # Public key (auto-generated)
    │   ├── hs_ed25519_secret_key    # Private key (auto-generated)
    │   └── authorized_clients/      # For client auth (optional)
    └── run/                         # Tor runtime files (755, uid 1000)
```

#### Vanity Onion Address (Optional)

To create a vanity onion address with a custom prefix:

```bash
# 1. Generate vanity address (can take hours/days depending on prefix length)
docker run --rm -it --network none -v $PWD:/keys \
  ghcr.io/cathugger/mkp224o:master -d /keys prefix

# 2. Move generated keys to tor data directory
# Note: mkp224o creates a directory named "prefix<randomchars>.onion"
mv prefix*.onion/hs_ed25519_public_key prefix*.onion/hs_ed25519_secret_key prefix*.onion/hostname tor/data/

# 3. Set correct ownership (uid 1000 required by tor container)
chown -R 1000:1000 tor/data/

# 4. Restart tor to use the new keys
docker compose restart tor

# 5. Verify your new vanity address
cat tor/data/hostname
```

**Note**: Longer prefixes take exponentially longer to generate. A 5-character prefix may take hours, 6+ characters may take days. The vanity generator will create `hs_ed25519_public_key` and `hs_ed25519_secret_key` files which replace the auto-generated ones.

#### Network Architecture & Security

The Docker Compose setup provides maximum security through network isolation:

- **directory_server**: Runs on isolated internal network (`joinmarket_directory_internal`) with **no external internet access**
  - Cannot make outbound connections to the internet
  - Cannot be reached directly from the internet
  - Only accessible through the Tor hidden service
- **tor**: Acts as a secure gateway
  - Connected to both internal network (`joinmarket_directory_internal`) and external network (`joinmarket_directory_external`)
  - Forwards hidden service traffic to directory_server on port 5222
  - Provides .onion address for privacy

This architecture ensures:
- The directory server cannot leak information or be exploited to make external connections
- All connections are anonymized through Tor
- Attack surface is minimized through network isolation
- Even if the directory server is compromised, it cannot access the internet directly

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
