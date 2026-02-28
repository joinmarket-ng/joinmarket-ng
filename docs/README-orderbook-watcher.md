# JoinMarket Orderbook Watcher

A clean, performant, and secure orderbook watcher for JoinMarket that aggregates offers from multiple directory nodes via Tor.

## Features

-  **Tor Integration**: Connects to directory nodes via Tor for privacy
-  **Multi-Directory Aggregation**: Fetches and combines orderbooks from multiple directory nodes
-  **Web Interface**: Clean, modern UI with real-time updates
-  **Advanced Filtering**: Filter by offer type, directory node, and counterparty
-  **Directory Statistics**: See offer counts per directory node
-  **Mempool.space Integration**: Validates fidelity bonds using mempool.space API
-  **Docker Support**: Easy deployment with Docker Compose

## Architecture

The orderbook watcher follows the clean architecture principles of this repository:

- **jmcore/models.py**: Core data models (Offer, FidelityBond, OrderBook)
- **jmcore/network.py**: Tor connection support
- **jmcore/mempool_api.py**: Mempool.space API client
- **orderbook_watcher/**: Application-specific code
  - **directory_client.py**: Connects to directory nodes
  - **aggregator.py**: Aggregates orderbooks from multiple nodes
  - **server.py**: HTTP server for API and static files

## Quick Start

### Using Docker Compose (Recommended)

1. Copy the environment file:
```bash
cd orderbook_watcher
cp .env.example .env
```

2. Edit `.env` and configure your directory nodes:
```bash
DIRECTORY_NODES=nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion:5222
```

3. Start the services:
```bash
docker-compose up -d
```

4. Access the web interface at http://localhost:8000

> **Note**: The `tor/conf/torrc` file must be manually created with the following content:
> ```
> SocksPort 0.0.0.0:9050
> ControlPort 0.0.0.0:9051
> CookieAuthentication 1
> DataDirectory /var/lib/tor
> Log notice stdout
> ```

### Manual Installation

See [Installation](install.md) for general installation instructions and Tor setup.

**For orderbook watcher** (manual installation):

1. Install dependencies:
```bash
cd joinmarket-ng
source jmvenv/bin/activate  # If you used install.sh
# OR create venv: python3 -m venv jmvenv && source jmvenv/bin/activate

cd jmcore
pip install -e .

cd ../orderbook_watcher
pip install -r requirements.txt
```

2. Make sure Tor is running on port 9050 (see [Installation - Tor Setup](install.md#tor-setup))

3. Set environment variables:
```bash
export NETWORK__NETWORK=mainnet
export NETWORK__DIRECTORY_SERVERS='["node1.onion:5222", "node2.onion:5222"]'
export TOR__SOCKS_HOST=127.0.0.1
export TOR__SOCKS_PORT=9050
export ORDERBOOK_WATCHER__MEMPOOL_API_URL=https://mempool.sgn.space/api
export ORDERBOOK_WATCHER__HTTP_HOST=0.0.0.0
export ORDERBOOK_WATCHER__HTTP_PORT=8000
```

4. Run the watcher:
```bash
python -m orderbook_watcher.main
```

## Configuration

All configuration is done via environment variables or config file (`~/.joinmarket-ng/config.toml`):

| Variable | Description | Default |
|----------|-------------|---------|
| `NETWORK__NETWORK` | Bitcoin network (mainnet/testnet/signet/regtest) | mainnet |
| `NETWORK__DIRECTORY_SERVERS` | JSON array of directory servers (e.g., `["host1:port1", "host2:port2"]`) | (required) |
| `TOR__SOCKS_HOST` | Tor SOCKS proxy host | 127.0.0.1 |
| `TOR__SOCKS_PORT` | Tor SOCKS proxy port | 9050 |
| `ORDERBOOK_WATCHER__MEMPOOL_API_URL` | Mempool.space API base URL | http://mempopwcaqoi7z5xj5zplfdwk5bgzyl3hemx725d4a3agado6xtk3kqd.onion/api |
| `ORDERBOOK_WATCHER__MEMPOOL_WEB_URL` | Base URL for transaction links (optional) | https://mempool.sgn.space |
| `ORDERBOOK_WATCHER__MEMPOOL_WEB_ONION_URL` | Onion base URL for transaction links (optional) | http://mempopwcaqoi7z5xj5zplfdwk5bgzyl3hemx725d4a3agado6xtk3kqd.onion |
| `ORDERBOOK_WATCHER__HTTP_HOST` | HTTP server bind address | 0.0.0.0 |
| `ORDERBOOK_WATCHER__HTTP_PORT` | HTTP server port | 8000 |
| `ORDERBOOK_WATCHER__UPDATE_INTERVAL` | Orderbook update interval in seconds | 60 |
| `LOGGING__LEVEL` | Logging level (TRACE/DEBUG/INFO/WARNING/ERROR) | INFO |
| `NETWORK__MAX_MESSAGE_SIZE` | Maximum message size in bytes | 2097152 |
| `NETWORK__CONNECTION_TIMEOUT` | Connection timeout in seconds | 30.0 |

## Exposing as a Tor Hidden Service

You can expose the orderbook watcher as a Tor hidden service using the existing Tor container.

1. Update your `tor/conf/torrc` file:

```conf
SocksPort 0.0.0.0:9050
ControlPort 0.0.0.0:9051
CookieAuthentication 1
DataDirectory /var/lib/tor
SafeLogging 0
Log notice stdout

# hidden service
HiddenServiceDir /var/lib/tor/hidden_service/
HiddenServiceVersion 3
HiddenServicePort 80 orderbook_watcher:8000
```

2. Restart the Tor container:
```bash
docker-compose restart tor
```

3. Get your onion address:
```bash
cat tor/data/hidden_service/hostname
```

4. (Optional) Configure onion-friendly links:
   If you want the web interface to use onion links for Mempool.space when visited via Tor, add this to your `.env`:
```bash
MEMPOOL_WEB_ONION_URL=http://mempopwcaqoi7z5xj5zplfdwk5bgzyl3hemx725d4a3agado6xtk3kqd.onion
```

## API Endpoints

### GET /
Web interface for viewing the orderbook

### GET /orderbook.json
Returns the aggregated orderbook in JSON format:

```json
{
  "timestamp": "2025-11-16T12:00:00.000000",
  "offers": [
    {
      "counterparty": "J5maker",
      "oid": 0,
      "ordertype": "sw0reloffer",
      "minsize": 100000,
      "maxsize": 10000000,
      "txfee": 1000,
      "cjfee": "0.0002",
      "fidelity_bond_value": 5000000,
      "directory_node": "node1.onion:5222"
    }
  ],
  "fidelitybonds": [...],
  "directory_nodes": ["node1.onion:5222", "node2.onion:5222"],
  "directory_stats": {
    "node1.onion:5222": {"offer_count": 10},
    "node2.onion:5222": {"offer_count": 8}
  }
}
```

### GET /health
Health check endpoint
