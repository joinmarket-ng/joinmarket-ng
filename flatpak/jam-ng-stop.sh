#!/bin/bash
# Gracefully stop a running jam-ng instance by sending SIGTERM to its
# entrypoint process.  The entrypoint already traps SIGTERM and calls
# cleanup(), which kills all child services (Tor, neutrino, jmwalletd, …)
# before exiting.

PIDFILE_DIR="${HOME}/.joinmarket-ng/run"
ENTRYPOINT_PIDFILE="${PIDFILE_DIR}/jam-ng.pid"

stop_via_pidfile() {
    if [ ! -f "${ENTRYPOINT_PIDFILE}" ]; then
        return 1
    fi
    local pid
    pid=$(cat "${ENTRYPOINT_PIDFILE}")
    if kill -0 "${pid}" 2>/dev/null; then
        echo "[jam-ng-stop] Sending SIGTERM to jam-ng (PID ${pid})..."
        kill -TERM "${pid}"
        # Wait up to 10 s for it to exit
        local i=0
        while kill -0 "${pid}" 2>/dev/null && [ "$i" -lt 10 ]; do
            sleep 1
            i=$((i + 1))
        done
        if kill -0 "${pid}" 2>/dev/null; then
            echo "[jam-ng-stop] Process did not exit; sending SIGKILL."
            kill -9 "${pid}" 2>/dev/null || true
        fi
        echo "[jam-ng-stop] Done."
        return 0
    fi
    return 1
}

stop_via_pgrep() {
    local pid
    pid=$(pgrep -f 'bash.*jam-ng-entrypoint' | head -n1)
    if [ -z "${pid}" ]; then
        return 1
    fi
    echo "[jam-ng-stop] Sending SIGTERM to jam-ng (PID ${pid})..."
    kill -TERM "${pid}"
    echo "[jam-ng-stop] Done."
    return 0
}

if stop_via_pidfile; then
    exit 0
elif stop_via_pgrep; then
    exit 0
else
    echo "[jam-ng-stop] No running jam-ng instance found."
    exit 1
fi
