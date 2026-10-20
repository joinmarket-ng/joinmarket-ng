#!/bin/sh
set -eu

onion_file="/tor/${LND_NODE}/hostname"
macaroon="/lnd/admin.macaroon"
until [ -s "$onion_file" ] && [ -s /lnd/tls.cert ] && [ -s "$macaroon" ]; do
    sleep 1
done

if [ "${MAKER__CHANNEL_RING__ENABLED:-false}" = true ]; then
    export MAKER__CHANNEL_RING__NODES="$(python -c '
import json, sys
print(json.dumps({"local": {
    "lnd_grpc_url": "https://127.0.0.1:10009",
    "lnd_tls_cert_path": "/lnd/tls.cert",
    "lnd_macaroon_path": "/lnd/admin.macaroon",
    "onion_endpoint": sys.argv[1] + ":9735",
}}))
' "$(tr -d '\r\n' < "$onion_file")")"
    export MAKER__CHANNEL_RING__MIXDEPTH_NODES='{"0":"local"}'
fi

# Only the disposable-stack runner requests enrollment, with a pinned pubkey.
# Ordinary restarts verify existing claims and never invent missing history.
if [ -n "${RING_NODE_ENROLLMENT_PUBKEY:-}" ]; then
    exec jm-maker enroll-ring-nodes --component maker \
        --expected-node "local=$RING_NODE_ENROLLMENT_PUBKEY" --acknowledge-prior-use
fi

exec jm-maker start
