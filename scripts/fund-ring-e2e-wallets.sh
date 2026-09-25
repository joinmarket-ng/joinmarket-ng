#!/bin/sh
set -eu

cli="bitcoin-cli -chain=regtest -rpcconnect=ring-bitcoin -rpcport=18443 -rpcuser=test -rpcpassword=test"
until $cli getblockchaininfo >/dev/null 2>&1; do
    sleep 1
done

$cli createwallet ring-funder false false "" false true true >/dev/null 2>&1 || true
address=$($cli -rpcwallet=ring-funder getnewaddress "" bech32m)
$cli generatetoaddress 110 "$address" >/dev/null

for destination in \
    bcrt1pkczcqwgewgz70peqjy2he3xz3396emq4w5vkr5au0v2an37a4gkq5dx68f \
    bcrt1pyv6lcd8l4e373xt2yn29k9tgmvvm3xcryl32t32azadp3meqwsaqgvn6jn \
    bcrt1ptdgs9m3y53qdaccjstj8jn6srhv7ecjrld2kzd79wdmy3pyqam9qgg0dh7 \
    bcrt1p8wpt9v4frpf3tkn0srd97pksgsxc5hs52lafxwru9kgeephvs7rqjeprhg \
    bcrt1psldw7rs3gy4jv25nzjdvemez8zah6mcuxtpesr7lr0s028macnvsqpg9n7
do
    $cli -rpcwallet=ring-funder sendtoaddress "$destination" 0.1 >/dev/null
done

$cli generatetoaddress 6 "$address" >/dev/null
