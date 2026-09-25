#!/bin/sh
set -eu

onion_file="/tor/${LND_NODE}/hostname"
macaroon="/lnd/admin.macaroon"
until [ -s "$onion_file" ] && [ -s /lnd/tls.cert ] && [ -s "$macaroon" ]; do
    sleep 1
done

export MAKER__CHANNEL_RING__ONION_ENDPOINT="$(tr -d '\r\n' < "$onion_file"):9735"
export MAKER__CHANNEL_RING__LND_TLS_CERT_PATH=/lnd/tls.cert
export MAKER__CHANNEL_RING__LND_MACAROON_PATH="$macaroon"

exec jm-maker start
