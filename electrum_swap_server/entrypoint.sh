#!/bin/sh
# entrypoint.sh — Electrum swap server for regtest e2e testing
#
# Starts an Electrum daemon with Lightning enabled and the swapserver plugin.
# All swap RPCs happen via Nostr DMs (kind 25582) — no HTTP ports are exposed.
#
# Runs initially as root to fix /shared permissions (rootless Docker bind-mount
# compatibility), then all Electrum commands are executed as the 'electrum' user.
#
# Environment variables (set in docker-compose.yml):
#   ELECTRS_HOST        — electrs hostname (default: jm-electrs)
#   ELECTRS_PORT        — electrs TCP port (default: 50001)
#   NOSTR_RELAY_URL     — Nostr relay WebSocket URL (default: ws://jm-nostr-relay:7000)
#   SWAPSERVER_PORT     — local HTTP port for plugin (default: 5455, localhost only)
#   SWAPSERVER_FEE      — fee in millionths (default: 5000 = 0.5%)
#   BITCOIN_NETWORK     — network (default: regtest)
#   WALLET_PATH         — wallet file path (default: /home/electrum/.electrum/regtest/wallets/swap_server)

ELECTRS_HOST="${ELECTRS_HOST:-jm-electrs}"
ELECTRS_PORT="${ELECTRS_PORT:-50001}"
NOSTR_RELAY_URL="${NOSTR_RELAY_URL:-ws://jm-nostr-relay:7000}"
SWAPSERVER_PORT="${SWAPSERVER_PORT:-5455}"
SWAPSERVER_FEE="${SWAPSERVER_FEE:-5000}"
BITCOIN_NETWORK="${BITCOIN_NETWORK:-regtest}"
WALLET_PATH="${WALLET_PATH:-/home/electrum/.electrum/regtest/wallets/swap_server}"
CONFIG_DIR="/home/electrum/.electrum/${BITCOIN_NETWORK}"
DAEMON_LOCKFILE="$CONFIG_DIR/daemon"

log() {
    echo "[electrum-swap] $(date '+%H:%M:%S') $*"
}

# Run a command as the electrum user
as_electrum() {
    su -s /bin/sh electrum -c "$*"
}

# Wait for a TCP service to be reachable
wait_for_tcp() {
    host="$1"
    port="$2"
    service_name="$3"
    max_retries="${4:-60}"

    log "Waiting for $service_name ($host:$port)..."
    retries=0
    while [ "$retries" -lt "$max_retries" ]; do
        if python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2)
try:
    s.connect(('$host', $port))
    s.close()
    sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            log "$service_name is ready"
            return 0
        fi
        retries=$((retries + 1))
        sleep 2
    done
    log "ERROR: $service_name not reachable after $max_retries retries"
    return 1
}

# Stop any running daemon and clean up stale lockfiles (runs as electrum user)
stop_daemon() {
    log "Stopping any running Electrum daemon..."
    as_electrum "electrum --$BITCOIN_NETWORK stop 2>/dev/null || true"
    sleep 2
    # Remove stale lockfile if daemon is no longer running
    if [ -f "$DAEMON_LOCKFILE" ]; then
        DAEMON_PID=$(python3 -c "
import sys, json
try:
    d = json.load(open('$DAEMON_LOCKFILE'))
    print(d.get('pid', ''))
except Exception:
    pass
" 2>/dev/null || true)
        if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
            log "Daemon PID $DAEMON_PID is still running, killing..."
            kill "$DAEMON_PID" 2>/dev/null || true
            sleep 2
        fi
        log "Removing stale lockfile $DAEMON_LOCKFILE"
        rm -f "$DAEMON_LOCKFILE"
    fi
}

# ---- Root-level setup ----
# Ensure /shared is writable by all container users.
# In rootless Docker, the 'electrum' user (uid=1000 in container) maps to a
# host subuid that differs from the bind-mount owner, so 'other' write permission
# is required. We set it here as root since the host cannot rely on git to
# preserve directory permissions (git does not track them).
log "Setting /shared permissions..."
chmod 777 /shared
mkdir -p /shared/electrum
chmod 777 /shared/electrum

# Remove stale artifacts from previous runs. The ./shared bind-mount persists
# across `docker compose down -v` (volumes flag only cleans Docker volumes, not
# bind-mounts). If these files exist from a prior run, dependent containers
# (e.g. lnd-setup) may read stale data before we overwrite it.
log "Cleaning stale shared artifacts..."
rm -f /shared/electrum-swap-ready
rm -f /shared/electrum/swap-server-info.json
rm -f /shared/lightning-setup-done

# Wait for dependencies
wait_for_tcp "$ELECTRS_HOST" "$ELECTRS_PORT" "electrs"
NOSTR_HOST=$(echo "$NOSTR_RELAY_URL" | sed 's|ws://||' | cut -d: -f1)
NOSTR_PORT=$(echo "$NOSTR_RELAY_URL" | sed 's|ws://||' | cut -d: -f2 | cut -d/ -f1)
wait_for_tcp "$NOSTR_HOST" "$NOSTR_PORT" "nostr-relay"

log "=== Configuring Electrum ==="

# Ensure data directories exist (owned by electrum)
as_electrum "mkdir -p '$(dirname "$WALLET_PATH")' '$CONFIG_DIR'"

# Write config directly to file to avoid daemon dependency during setup.
# electrum setconfig requires a running daemon in some versions, so we
# write the JSON config file directly.
#
# IMPORTANT: Electrum's SimpleConfig.get() splits dotted keys by '.' and
# traverses nested dicts, e.g. "plugins.swapserver.enabled" becomes
# user_config["plugins"]["swapserver"]["enabled"].  We must write nested
# JSON — flat dotted keys like {"plugins.swapserver.enabled": true} are
# silently ignored.
cat > "$CONFIG_DIR/config" << EOCFG
{
    "config_version": 3,
    "swapserver_pow_target": 0,
    "auto_connect": false,
    "oneserver": true,
    "server": "${ELECTRS_HOST}:${ELECTRS_PORT}:t",
    "lightning_listen": "0.0.0.0:9735",
    "use_gossip": true,
    "use_recoverable_channels": false,
    "nostr_relays": "${NOSTR_RELAY_URL}",
    "plugins": {
        "swapserver": {
            "enabled": true,
            "port": ${SWAPSERVER_PORT},
            "fee_millionths": ${SWAPSERVER_FEE}
        }
    }
}
EOCFG
chown electrum:electrum "$CONFIG_DIR/config"
log "Config written to $CONFIG_DIR/config"

# Create wallet if it doesn't exist
if [ ! -f "$WALLET_PATH" ]; then
    log "Creating new wallet at $WALLET_PATH"
    as_electrum "electrum --$BITCOIN_NETWORK --offline create -w '$WALLET_PATH'"
else
    log "Wallet already exists at $WALLET_PATH"
fi

log "=== Starting Electrum daemon ==="

# Clean up any stale daemon state from a previous container run
stop_daemon

# Start daemon in foreground (as electrum user), with verbose logging.
# Run in background so the entrypoint can continue with setup.
# Don't use --detach (-d) since it forks and loses stdout/stderr.
DAEMON_LOG="/home/electrum/.electrum/regtest/daemon.log"
log "Starting daemon with verbose logging (log: $DAEMON_LOG)..."
as_electrum "electrum --$BITCOIN_NETWORK -v daemon >> '$DAEMON_LOG' 2>&1 &"
sleep 2
log "Daemon start command issued"

# Wait for daemon to be responsive and connected to electrs.
# Note: Electrum's `is_connected` CLI command may not exist in all versions.
# We use `getinfo` which returns a JSON object with "connected": true/false.
log "Waiting for Electrum daemon to connect to electrs..."

retries=0
while [ "$retries" -lt 45 ]; do
    INFO=$(as_electrum "electrum --$BITCOIN_NETWORK getinfo 2>&1" || echo "")
    if echo "$INFO" | grep -q '"connected": true'; then
        log "Daemon is connected to electrs"
        break
    fi
    if [ $(( retries % 10 )) -eq 0 ]; then
        if echo "$INFO" | grep -q '"connected"'; then
            # Extract server and connected fields for debugging
            SERVER_LINE=$(echo "$INFO" | grep '"server"' || echo "unknown")
            CONNECTED_LINE=$(echo "$INFO" | grep '"connected"' || echo "unknown")
            log "Daemon RPC responsive but not connected (attempt $retries/45)"
            log "  server: $SERVER_LINE"
            log "  connected: $CONNECTED_LINE"
        else
            log "Daemon RPC not yet responsive (attempt $retries/45): $(echo "$INFO" | head -1)"
        fi
    fi
    retries=$((retries + 1))
    sleep 2
done

if [ "$retries" -ge 45 ]; then
    log "ERROR: Daemon failed to connect to electrs after 45 retries (90s)"
    log "Last getinfo response:"
    as_electrum "electrum --$BITCOIN_NETWORK getinfo 2>&1" || true
    log "Config 'server' as read by daemon:"
    as_electrum "electrum --$BITCOIN_NETWORK getconfig server 2>&1" || true
    log "Dumping config file:"
    as_electrum "cat '$CONFIG_DIR/config'" 2>/dev/null || true
    log "Electrum daemon logs (last 50 lines):"
    LOG_DIR="/home/electrum/.electrum/regtest/logs"
    if [ -d "$LOG_DIR" ]; then
        tail -50 "$LOG_DIR"/*.log 2>/dev/null || log "No log files found"
    else
        log "Log directory $LOG_DIR does not exist"
    fi
    log "Stopping daemon and exiting (container will restart)..."
    stop_daemon
    exit 1
fi

# Load the wallet (triggers swapserver plugin via daemon_wallet_loaded hook)
log "Loading wallet..."
as_electrum "electrum --$BITCOIN_NETWORK load_wallet -w '$WALLET_PATH'"

# Wait for Lightning to initialize
log "Waiting for Lightning node to initialize..."
sleep 5

# Export swap server info for other containers
log "Exporting swap server info to /shared..."

# Get the Lightning node pubkey
# When lightning_listen is configured, `nodeid` returns "pubkey@host:port".
# Strip the "@..." suffix to get the raw pubkey.
NOSTR_PUBKEY=""
retries=0
while [ "$retries" -lt 20 ]; do
    NODE_INFO=$(as_electrum "electrum --$BITCOIN_NETWORK nodeid 2>/dev/null" || echo "")
    if [ -n "$NODE_INFO" ] && [ "$NODE_INFO" != "null" ]; then
        NOSTR_PUBKEY=$(echo "$NODE_INFO" | cut -d'@' -f1)
        break
    fi
    retries=$((retries + 1))
    sleep 2
done

if [ -n "$NOSTR_PUBKEY" ]; then
    log "Electrum LN node pubkey: $NOSTR_PUBKEY"
else
    log "ERROR: Could not retrieve LN node pubkey after 20 retries (40s)"
    log "Lightning node failed to initialize. Stopping daemon and exiting..."
    stop_daemon
    exit 1
fi

LN_LISTEN_PORT=9735
SWAP_INFO_PATH="/shared/electrum/swap-server-info.json"

# Get an on-chain address so lnd-setup can fund the swap server wallet.
# The wallet needs on-chain BTC to create lockup transactions for reverse swaps.
ONCHAIN_ADDR=""
retries=0
while [ "$retries" -lt 10 ]; do
    ONCHAIN_ADDR=$(as_electrum "electrum --$BITCOIN_NETWORK getunusedaddress 2>/dev/null" || echo "")
    if [ -n "$ONCHAIN_ADDR" ] && [ "$ONCHAIN_ADDR" != "null" ]; then
        break
    fi
    retries=$((retries + 1))
    sleep 2
done

if [ -n "$ONCHAIN_ADDR" ]; then
    log "On-chain funding address: $ONCHAIN_ADDR"
else
    log "WARNING: Could not get on-chain address. Reverse swaps may not work."
fi

cat > "$SWAP_INFO_PATH" << EOF
{
    "nostr_pubkey": "$NOSTR_PUBKEY",
    "nostr_relay": "$NOSTR_RELAY_URL",
    "ln_pubkey": "$NOSTR_PUBKEY",
    "ln_host": "jm-electrum-swap",
    "ln_port": $LN_LISTEN_PORT,
    "onchain_address": "$ONCHAIN_ADDR",
    "network": "$BITCOIN_NETWORK",
    "swapserver_fee_millionths": $SWAPSERVER_FEE
}
EOF
log "Swap server info written to $SWAP_INFO_PATH"

# Signal that the swap server is ready
touch /shared/electrum-swap-ready
log "Ready signal written to /shared/electrum-swap-ready"

log "=== Electrum swap server is running ==="
log "  Network:       $BITCOIN_NETWORK"
log "  Electrs:       ${ELECTRS_HOST}:${ELECTRS_PORT}"
log "  Nostr relay:   $NOSTR_RELAY_URL"
log "  LN pubkey:     $NOSTR_PUBKEY"
log "  Swap fee:      ${SWAPSERVER_FEE} millionths"

# Keep the container alive by following the daemon log
if [ -f "$DAEMON_LOG" ]; then
    tail -f "$DAEMON_LOG" 2>/dev/null || true
else
    LOG_DIR="/home/electrum/.electrum/regtest/logs"
    if [ -d "$LOG_DIR" ]; then
        tail -f "$LOG_DIR"/*.log 2>/dev/null || true
    else
        while true; do sleep 60; done
    fi
fi
