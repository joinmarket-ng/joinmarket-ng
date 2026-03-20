#!/bin/bash
# =============================================================================
# JAM-NG Flatpak Entrypoint
#
# Manages the lifecycle of all services:
#   1. Tor daemon (SOCKS proxy + control port)
#   2. neutrino-api (Bitcoin light client, if configured)
#   3. jmwalletd (API daemon + JAM frontend)
#   4. orderbook_watcher (optional, background)
#
# Usage:
#   jam-ng                         Start services + GUI (default)
#   jam-ng --no-gui                Start services, open browser, no GUI
#   jam-ng --network signet        Use signet instead of mainnet
#   jam-ng cli jm-wallet info ...  Run a CLI tool against the running services
#   jam-ng cli jm-taker coinjoin   Run taker coinjoin
#
# All state is stored in ~/.joinmarket-ng/
# Config is at ~/.joinmarket-ng/config.toml
# =============================================================================
set -uo pipefail

DATA_DIR="${HOME}/.joinmarket-ng"
# Network-specific dirs are set after argument parsing in setup_network_dirs().
# Tor is shared across networks (network-agnostic).
TOR_DIR="${DATA_DIR}/tor"
TOR_DATA_DIR="${TOR_DIR}/data"
# These are set dynamically per network:
NET_DATA_DIR=""
NEUTRINO_DATA_DIR=""
LOG_DIR=""
CONFIG_FILE=""
PIDFILE_DIR=""
TORRC="${TOR_DIR}/torrc"

# All ports are allocated dynamically so the Flatpak never conflicts with
# Docker-compose test infrastructure or other local services.
# JMWALLETD_PORT and OBWATCHER_PORT are set in main() after argument parsing.
JMWALLETD_PORT=28183  # default; overridden in main() if already in use
OBWATCHER_PORT=8000   # default; overridden in main() if already in use

find_free_port() {
    python3 -c "
import socket
s = socket.socket()
s.bind(('127.0.0.1', 0))
print(s.getsockname()[1])
s.close()
"
}

# Try preferred_port first; if it's occupied, fall back to a random free port.
find_port_prefer() {
    local preferred="$1"
    python3 -c "
import socket, sys
preferred = int('${preferred}')
s = socket.socket()
try:
    s.bind(('127.0.0.1', preferred))
    print(preferred)
    s.close()
except OSError:
    s.close()
    s2 = socket.socket()
    s2.bind(('127.0.0.1', 0))
    print(s2.getsockname()[1])
    s2.close()
"
}

# ---- Parse arguments --------------------------------------------------------

NETWORK="mainnet"
USE_GUI=true

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --network)
                shift
                NETWORK="${1:-mainnet}"
                case "${NETWORK}" in
                    mainnet|signet|regtest) ;;
                    *)
                        echo "ERROR: unknown network '${NETWORK}'. Use mainnet, signet, or regtest."
                        exit 1
                        ;;
                esac
                ;;
            --no-gui)
                USE_GUI=false
                ;;
            cli)
                shift
                exec_cli "$@"
                ;;
            --help|-h)
                show_help
                exit 0
                ;;
            *)
                echo "Unknown argument: $1"
                show_help
                exit 1
                ;;
        esac
        shift
    done
}

show_help() {
    cat <<'HELP'
JAM-NG -- Decentralized CoinJoin for Bitcoin privacy

Usage:
  jam-ng [OPTIONS]                     Start all services + control panel
  jam-ng cli <command> [args...]       Run a CLI tool in the Flatpak sandbox

Options:
  --network <net>   Bitcoin network: mainnet (default), signet, regtest
  --no-gui          Start services without the GUI (opens browser instead)
  -h, --help        Show this help message

CLI commands (pass-through to installed tools):
  jam-ng cli jm-wallet info <wallet>          Show wallet info
  jam-ng cli jm-wallet balance <wallet>       Show wallet balance
  jam-ng cli jm-taker coinjoin [args...]      Run a CoinJoin as taker
  jam-ng cli jm-maker [args...]               Run the yield generator
  jam-ng cli orderbook-watcher                Start the orderbook watcher
  jam-ng cli jmwalletd [args...]              Start the wallet daemon

Environment variables set for CLI commands:
  JOINMARKET_DATA_DIR, TOR__SOCKS_PORT, TOR__CONTROL_PORT, etc.
  are read from the running instance's pidfile directory.

Examples:
  flatpak run org.joinmarketng.JamNG
  flatpak run org.joinmarketng.JamNG --network signet
  flatpak run org.joinmarketng.JamNG cli jm-wallet info wallet.jmdat
HELP
}

exec_cli() {
    # Run a CLI command with the same environment as the running services.
    # Load connection details from the running instance if available.
    if [ $# -eq 0 ]; then
        echo "ERROR: no CLI command specified. Run 'jam-ng cli --help' or 'jam-ng --help'."
        exit 1
    fi

    # Set up network-specific data dir
    setup_network_dirs
    export JOINMARKET_DATA_DIR="${NET_DATA_DIR}"
    export NETWORK_CONFIG__NETWORK="${NETWORK}"
    export NETWORK_CONFIG__BITCOIN_NETWORK="${NETWORK}"

    # Read port info from the running instance
    local envfile="${PIDFILE_DIR}/env"
    if [ -f "${envfile}" ]; then
        # shellcheck source=/dev/null
        source "${envfile}"
    else
        echo "WARNING: no running jam-ng instance found (${envfile} missing)."
        echo "         Tor and neutrino connection settings may not be available."
    fi

    exec "$@"
}

# PIDs of managed processes
PIDS=()
# PIDs that are critical (if they exit, shut down everything)
CRITICAL_PIDS=()

# ---- Helpers ----------------------------------------------------------------

log() {
    echo "[jam-ng] $(date '+%H:%M:%S') $*"
}

cleanup() {
    log "Shutting down..."
    local pid
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    # Give child services (especially neutrino + Tor) enough time to flush state
    # and exit cleanly before forcing termination.
    local grace_seconds=15
    local waited=0
    local all_stopped=false
    while [ "$waited" -lt "$grace_seconds" ]; do
        all_stopped=true
        for pid in "${PIDS[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                all_stopped=false
                break
            fi
        done
        if [ "$all_stopped" = true ]; then
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    # Force-kill anything still alive after grace period.
    for pid in "${PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            log "Process ${pid} did not exit after ${grace_seconds}s; sending SIGKILL"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done

    # Check if the GUI requested a network switch restart.
    # If so, re-exec the entrypoint with the new network instead of exiting.
    local restart_file="${PIDFILE_DIR}/restart_network"
    local new_network=""
    if [ -f "${restart_file}" ]; then
        new_network=$(cat "${restart_file}" 2>/dev/null || true)
        rm -f "${restart_file}"
    fi

    rm -rf "${PIDFILE_DIR}"
    log "All services stopped."

    if [ -n "${new_network}" ]; then
        log "Re-launching with --network ${new_network}..."
        # Reset trap to avoid recursive cleanup on exec
        trap - SIGINT SIGTERM EXIT
        exec "$0" --network "${new_network}"
    fi

    exit 0
}

trap cleanup SIGINT SIGTERM EXIT

wait_for_port() {
    local host="$1" port="$2" name="$3" max_attempts="${4:-60}"
    local attempt=0
    log "Waiting for ${name} on ${host}:${port}..."
    while [ "$attempt" -lt "$max_attempts" ]; do
        if python3 -c "
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(1)
try:
    s.connect(('${host}', ${port}))
    s.close()
    sys.exit(0)
except (ConnectionRefusedError, TimeoutError, OSError):
    sys.exit(1)
" 2>/dev/null; then
            log "${name} is ready."
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    log "ERROR: ${name} failed to start after ${max_attempts}s"
    return 1
}

read_config_value() {
    # Simple TOML value reader: read_config_value "section" "key" "default"
    local section="$1" key="$2" default="${3:-}"
    python3 -c "
import sys
try:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    with open('${CONFIG_FILE}', 'rb') as f:
        config = tomllib.load(f)
    val = config.get('${section}', {}).get('${key}', '${default}')
    print(val)
except Exception:
    print('${default}')
" 2>/dev/null
}

# ---- First-run setup --------------------------------------------------------

setup_network_dirs() {
    # Mainnet uses the base dir directly (backwards compatible).
    # Non-mainnet networks get a subdirectory.
    if [ "${NETWORK}" = "mainnet" ]; then
        NET_DATA_DIR="${DATA_DIR}"
    else
        NET_DATA_DIR="${DATA_DIR}/${NETWORK}"
    fi
    NEUTRINO_DATA_DIR="${NET_DATA_DIR}/neutrino"
    LOG_DIR="${NET_DATA_DIR}/logs"
    CONFIG_FILE="${NET_DATA_DIR}/config.toml"
    PIDFILE_DIR="${NET_DATA_DIR}/run"
}

stop_stale_instance() {
    # If a previous instance left a PID file, stop it before starting.
    local old_pid_file="${PIDFILE_DIR}/jam-ng.pid"
    if [ -f "${old_pid_file}" ]; then
        local old_pid
        old_pid=$(cat "${old_pid_file}" 2>/dev/null || true)
        if [ -n "${old_pid}" ] && [ "${old_pid}" != "$$" ] && kill -0 "${old_pid}" 2>/dev/null; then
            log "Stopping previous instance (PID ${old_pid})..."
            kill "${old_pid}" 2>/dev/null || true
            # Wait up to 5s for graceful shutdown
            local i=0
            while [ "$i" -lt 10 ] && kill -0 "${old_pid}" 2>/dev/null; do
                sleep 0.5
                i=$((i + 1))
            done
            if kill -0 "${old_pid}" 2>/dev/null; then
                kill -9 "${old_pid}" 2>/dev/null || true
                sleep 0.5
            fi
        fi
        rm -f "${old_pid_file}"
    fi
}

setup_data_dir() {
    mkdir -p "${DATA_DIR}" "${TOR_DIR}" "${TOR_DATA_DIR}" \
             "${NET_DATA_DIR}" "${NEUTRINO_DATA_DIR}" \
             "${LOG_DIR}" "${PIDFILE_DIR}"
    chmod 700 "${TOR_DATA_DIR}"

    # Stop any stale instance before writing our PID
    stop_stale_instance

    # Record the entrypoint PID so jam-ng-stop can find it
    echo "$$" > "${PIDFILE_DIR}/jam-ng.pid"

    # Copy config template on first run
    if [ ! -f "${CONFIG_FILE}" ]; then
        log "First run: creating config from template..."
        cp /app/share/joinmarket-ng/config.toml.template "${CONFIG_FILE}"
        log "Config created at ${CONFIG_FILE}"
        log "Edit this file to customize your setup."
    fi

    # Regenerate torrc every launch so dynamic ports are always up to date
    sed -e "s|__DATA_DIR__|${DATA_DIR}|g" \
        -e "s|__TOR_SOCKS_PORT__|${TOR_SOCKS_PORT}|g" \
        -e "s|__TOR_CONTROL_PORT__|${TOR_CONTROL_PORT}|g" \
        /app/share/joinmarket-ng/torrc > "${TORRC}"
}

save_env() {
    # Persist port assignments and network so CLI commands can connect
    cat > "${PIDFILE_DIR}/env" <<EOF
export TOR__SOCKS_HOST="127.0.0.1"
export TOR__SOCKS_PORT="${TOR_SOCKS_PORT}"
export TOR__CONTROL_HOST="127.0.0.1"
export TOR__CONTROL_PORT="${TOR_CONTROL_PORT}"
export TOR__COOKIE_PATH="${TOR_DATA_DIR}/control_auth_cookie"
export BITCOIN__BACKEND_TYPE="${BITCOIN__BACKEND_TYPE:-neutrino}"
export BITCOIN__NEUTRINO_URL="http://127.0.0.1:${NEUTRINO_PORT}"
export JMWALLETD_NO_TLS="${JMWALLETD_NO_TLS:-false}"
export NETWORK_CONFIG__NETWORK="${NETWORK}"
export NETWORK_CONFIG__BITCOIN_NETWORK="${NETWORK}"
export JMWALLETD_PORT="${JMWALLETD_PORT}"
export JMWALLETD_HOST="127.0.0.1"
export ORDERBOOK_WATCHER__HTTP_PORT="${OBWATCHER_PORT}"
export OBWATCH_URL="http://127.0.0.1:${OBWATCHER_PORT}"
EOF
}

# ---- Service launchers ------------------------------------------------------

start_tor() {
    local use_bundled
    use_bundled=$(read_config_value "flatpak" "bundled_tor" "true")
    if [ "${use_bundled}" = "false" ]; then
        log "Bundled Tor disabled, using system Tor."
        return 0
    fi

    log "Starting Tor daemon..."
    tor -f "${TORRC}" > "${LOG_DIR}/tor.log" 2>&1 &
    local pid=$!
    PIDS+=("$pid")
    CRITICAL_PIDS+=("$pid")
    echo "$pid" > "${PIDFILE_DIR}/tor.pid"

    wait_for_port "127.0.0.1" "${TOR_SOCKS_PORT}" "Tor SOCKS" 60
}

start_neutrino() {
    local backend_type
    backend_type=$(read_config_value "bitcoin" "backend_type" "neutrino")
    if [ "${backend_type}" != "neutrino" ]; then
        log "Backend type is '${backend_type}', skipping neutrino-api."
        return 0
    fi

    # Check if using external neutrino
    local neutrino_url
    neutrino_url=$(read_config_value "bitcoin" "neutrino_url" "http://127.0.0.1:${NEUTRINO_PORT}")
    if [ "${neutrino_url}" != "http://127.0.0.1:${NEUTRINO_PORT}" ]; then
        log "Using external neutrino-api at ${neutrino_url}"
        return 0
    fi

    if ! command -v neutrinod >/dev/null 2>&1; then
        log "WARNING: neutrinod binary not found. Install neutrino-api or configure an external URL."
        return 0
    fi

    # Read optional add-peers list (preferred peers while still allowing discovery).
    local add_peers
    add_peers=$(python3 -c "
import sys
try:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib
    with open('${CONFIG_FILE}', 'rb') as f:
        config = tomllib.load(f)
    peers = config.get('bitcoin', {}).get('neutrino_add_peers', [])
    if isinstance(peers, list):
        print(','.join(peers))
    elif peers:
        print(peers)
except Exception:
    pass
" 2>/dev/null || true)

    log "Starting neutrino-api light client..."
    NETWORK="${NETWORK}" \
    LISTEN_ADDR="127.0.0.1:${NEUTRINO_PORT}" \
    DATA_DIR="${NEUTRINO_DATA_DIR}" \
    LOG_LEVEL="${NEUTRINO_LOG_LEVEL:-info}" \
    TOR_PROXY="127.0.0.1:${TOR_SOCKS_PORT}" \
    ADD_PEERS="${add_peers}" \
    neutrinod > "${LOG_DIR}/neutrino.log" 2>&1 &
    local pid=$!
    PIDS+=("$pid")
    echo "$pid" > "${PIDFILE_DIR}/neutrino.pid"

    if ! wait_for_port "127.0.0.1" "${NEUTRINO_PORT}" "neutrino-api" 30; then
        log "WARNING: neutrino-api did not start in time; continuing without it."
    fi
}

start_obwatcher() {
    log "Starting orderbook watcher..."
    JOINMARKET_DATA_DIR="${NET_DATA_DIR}" \
    NETWORK_CONFIG__NETWORK="${NETWORK}" \
    NETWORK_CONFIG__BITCOIN_NETWORK="${NETWORK}" \
    ORDERBOOK_WATCHER__HTTP_PORT="${OBWATCHER_PORT}" \
    python3 -m orderbook_watcher.main > "${LOG_DIR}/obwatcher.log" 2>&1 &
    local pid=$!
    PIDS+=("$pid")
    echo "$pid" > "${PIDFILE_DIR}/obwatcher.pid"
    # Non-critical: obwatcher failure does not take down jmwalletd
}

start_jmwalletd() {
    log "Starting jmwalletd API daemon..."
    # Set environment for jmwalletd
    export JOINMARKET_DATA_DIR="${NET_DATA_DIR}"
    export JMWALLETD_HOST="127.0.0.1"
    export TOR__SOCKS_HOST="127.0.0.1"
    export TOR__SOCKS_PORT="${TOR_SOCKS_PORT}"
    export TOR__CONTROL_HOST="127.0.0.1"
    export TOR__CONTROL_PORT="${TOR_CONTROL_PORT}"
    export TOR__COOKIE_PATH="${TOR_DATA_DIR}/control_auth_cookie"
    export OBWATCH_URL="http://127.0.0.1:${OBWATCHER_PORT}"
    export NETWORK_CONFIG__NETWORK="${NETWORK}"
    export NETWORK_CONFIG__BITCOIN_NETWORK="${NETWORK}"
    # Default to neutrino backend (bundled neutrinod)
    export BITCOIN__BACKEND_TYPE="${BITCOIN__BACKEND_TYPE:-neutrino}"
    export BITCOIN__NEUTRINO_URL="${BITCOIN__NEUTRINO_URL:-http://127.0.0.1:${NEUTRINO_PORT}}"

    if [ "${JMWALLETD_NO_TLS:-false}" = "true" ]; then
        jmwalletd --no-tls --port "${JMWALLETD_PORT}" > "${LOG_DIR}/jmwalletd.log" 2>&1 &
    else
        jmwalletd --port "${JMWALLETD_PORT}" > "${LOG_DIR}/jmwalletd.log" 2>&1 &
    fi
    local pid=$!
    PIDS+=("$pid")
    CRITICAL_PIDS+=("$pid")
    echo "$pid" > "${PIDFILE_DIR}/jmwalletd.pid"

    wait_for_port "127.0.0.1" "${JMWALLETD_PORT}" "jmwalletd" 30
}

open_browser() {
    local scheme="https"
    if [ "${JMWALLETD_NO_TLS:-false}" = "true" ]; then
        scheme="http"
    fi
    local url="${scheme}://127.0.0.1:${JMWALLETD_PORT}"
    log "Opening JAM web UI at ${url}"
    # Use xdg-open via the Flatpak portal
    xdg-open "${url}" 2>/dev/null || log "Could not open browser. Navigate to ${url} manually."
}

start_gui() {
    # GTK3 (via PyGObject) supports both X11 and Wayland natively.
    # If neither display variable is set we are in a headless session.
    if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        log "No display server detected. Falling back to browser."
        open_browser
        return 0
    fi

    log "Starting control panel..."
    export JOINMARKET_DATA_DIR="${NET_DATA_DIR}"
    export JMWALLETD_PORT
    export NETWORK
    python3 /app/share/joinmarket-ng/jam-ng-gui.py 2>"${LOG_DIR}/gui.log" &
    local pid=$!
    PIDS+=("$pid")
    echo "$pid" > "${PIDFILE_DIR}/gui.pid"

    # Brief health check: if the GUI exits immediately, fall back to browser.
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
        log "GUI failed to start (see ${LOG_DIR}/gui.log). Falling back to browser."
        open_browser
    fi
}

# ---- Monitor ----------------------------------------------------------------

monitor_processes() {
    # Wait for any critical child to exit; if it does, shut down everything.
    # Non-critical processes (neutrino, obwatcher, gui) are only logged when they exit.
    while true; do
        for pid in "${CRITICAL_PIDS[@]}"; do
            if ! kill -0 "$pid" 2>/dev/null; then
                log "Critical process ${pid} exited unexpectedly. Shutting down."
                cleanup
            fi
        done
        sleep 5
    done
}

# ---- Main -------------------------------------------------------------------

main() {
    parse_args "$@"

    # Set up network-specific directory layout before anything else
    setup_network_dirs

    log "JAM-NG starting (network=${NETWORK})..."
    log "Data directory: ${NET_DATA_DIR}"

    # Allocate dynamic ports after parsing args (not at script top level)
    # so that 'cli' subcommand can exec early without binding ports.
    # jmwalletd prefers 28183 (memorable default) but falls back to a random
    # free port if something else (e.g. docker-compose) is already using it.
    # orderbook_watcher likewise prefers 8000 for compatibility with JAM,
    # but falls back to a random free port when 8000 is occupied.
    TOR_SOCKS_PORT=$(find_free_port)
    TOR_CONTROL_PORT=$(find_free_port)
    NEUTRINO_PORT=$(find_free_port)
    JMWALLETD_PORT=$(find_port_prefer 28183)
    OBWATCHER_PORT=$(find_port_prefer 8000)

    setup_data_dir
    save_env

    # Start services in dependency order
    start_tor
    start_neutrino
    start_obwatcher
    start_jmwalletd

    if [ "${USE_GUI}" = "true" ]; then
        start_gui
    else
        open_browser
    fi

    local ui_scheme="https"
    if [ "${JMWALLETD_NO_TLS:-false}" = "true" ]; then
        ui_scheme="http"
    fi
    log "All services running. JAM UI: ${ui_scheme}://127.0.0.1:${JMWALLETD_PORT}"
    log "Logs: ${LOG_DIR}/"
    log "Config: ${CONFIG_FILE}"

    monitor_processes
}

main "$@"
