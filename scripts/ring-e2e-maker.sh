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

if [ "${MAKER__SWAP_PROVIDER__ENABLED:-false}" = "true" ]; then
    nostr_onion_file=/tor/swap-nostr/hostname
    until [ -s "$nostr_onion_file" ]; do
        sleep 1
    done
    export MAKER__SWAP_PROVIDER__NOSTR_RELAYS="[\"ws://$(tr -d '\r\n' < "$nostr_onion_file")\"]"
fi

exec jm-maker start
