#!/bin/sh
set -eu

onion_file="/tor/${LND_NODE}/hostname"
until [ -s "$onion_file" ]; do
    sleep 1
done
onion_host=$(tr -d '\r\n' < "$onion_file")

exec lnd \
    --noseedbackup \
    --bitcoin.active \
    --bitcoin.regtest \
    --bitcoin.node=bitcoind \
    --bitcoind.rpchost=ring-bitcoin:18443 \
    --bitcoind.rpcuser=test \
    --bitcoind.rpcpass=test \
    --bitcoind.zmqpubrawblock=tcp://ring-bitcoin:28332 \
    --bitcoind.zmqpubrawtx=tcp://ring-bitcoin:28333 \
    --listen=0.0.0.0:9735 \
    --rpclisten=0.0.0.0:10009 \
    --restlisten=0.0.0.0:8080 \
    --adminmacaroonpath=/root/.lnd/admin.macaroon \
    --tlsextraip=127.0.0.1 \
    --tlsextradomain=localhost \
    --tlsextradomain="${LND_NODE}" \
    --externalip="${onion_host}:9735" \
    --tor.active \
    --tor.socks=ring-tor:9050 \
    --tor.control=ring-tor:9051 \
    --tor.password=ring-e2e \
    --tor.skip-proxy-for-clearnet-targets \
    --protocol.simple-taproot-chans \
    --protocol.option-scid-alias \
    --acceptortimeout=120s \
    --nobootstrap
