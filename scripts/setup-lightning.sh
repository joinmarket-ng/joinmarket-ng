#!/bin/sh
# setup-lightning.sh
#
# Initialize a cross-implementation Lightning channel between:
#   - Electrum swap server (built-in LN node)
#   - LND taker node
#
# Run as a one-shot init container after both electrum-swap-server and
# lnd-taker are healthy.
#
# This script:
# 1. Waits for LND-taker to sync to chain
# 2. Reads Electrum swap server info from /shared/electrum/
# 3. Generates addresses and funds both Lightning nodes
# 4. Mines blocks for maturity
# 5. Connects LND-taker to Electrum's LN node
# 6. Opens a channel from LND-taker to Electrum with push_amt
# 7. Mines blocks to confirm the channel
# 8. Exports connection info to /shared for other containers
#
# Expected volumes:
#   /lnd-taker     -> lnd-taker's .lnd directory (read-only)
#   /shared        -> shared directory for artifacts (read-write)

set -e

LNCLI_TAKER="lncli --network=regtest --rpcserver=jm-lnd-taker:10009 --tlscertpath=/lnd-taker/tls.cert --macaroonpath=/lnd-taker/data/chain/bitcoin/regtest/admin.macaroon"
BTC_RPC_URL="http://jm-bitcoin:18443/wallet/miner"
BTC_RPC_AUTH="test:test"

# Call Bitcoin Core JSON-RPC against the miner wallet.
# Usage: btc_rpc <method> [json-array-of-params]
btc_rpc() {
    method="$1"
    params="${2:-[]}"
    curl -sf --user "$BTC_RPC_AUTH" \
        --data-binary "{\"jsonrpc\":\"1.0\",\"id\":\"lnd-setup\",\"method\":\"${method}\",\"params\":${params}}" \
        -H "content-type: text/plain;" \
        "$BTC_RPC_URL"
}

# Channel parameters
CHANNEL_SIZE=5000000       # 5M sats = 0.05 BTC
PUSH_AMT=2500000           # Push half to Electrum side for balanced liquidity
FUNDING_AMOUNT_BTC="1.0"   # Amount to send to each LN node for funding
MAX_RETRIES=60
RETRY_INTERVAL=2

log() {
    echo "[lnd-setup] $(date '+%H:%M:%S') $*"
}

wait_for_sync() {
    node_name="$1"
    lncli_cmd="$2"
    log "Waiting for $node_name to sync to chain..."
    retries=0
    while [ $retries -lt $MAX_RETRIES ]; do
        synced=$($lncli_cmd getinfo 2>/dev/null | grep -o '"synced_to_chain": *[a-z]*' | tr -d ' ' | cut -d: -f2)
        if [ "$synced" = "true" ]; then
            log "$node_name synced to chain"
            return 0
        fi
        retries=$((retries + 1))
        sleep $RETRY_INTERVAL
    done
    log "ERROR: $node_name did not sync after $MAX_RETRIES retries"
    return 1
}

# Check if channel setup has already been done (idempotency)
if [ -f /shared/lightning-setup-done ]; then
    log "Lightning setup already completed (found /shared/lightning-setup-done). Checking channel..."
    # Quick check: verify channel exists
    channels=$($LNCLI_TAKER listchannels 2>/dev/null | grep -c '"active": *true' || true)
    if [ "$channels" -gt 0 ]; then
        log "Active channel found. Skipping setup."
        exit 0
    fi
    log "No active channel found despite marker. Re-running setup..."
    rm -f /shared/lightning-setup-done
fi

# Step 1: Wait for LND-taker to sync
wait_for_sync "lnd-taker" "$LNCLI_TAKER"

# Step 2: Read Electrum swap server info
log "Reading Electrum swap server info..."
ELECTRUM_INFO="/shared/electrum/swap-server-info.json"
retries=0
while [ $retries -lt 30 ]; do
    if [ -f "$ELECTRUM_INFO" ]; then
        ELECTRUM_LN_PUBKEY=$(cat "$ELECTRUM_INFO" | grep '"ln_pubkey"' | cut -d'"' -f4)
        ELECTRUM_LN_HOST=$(cat "$ELECTRUM_INFO" | grep '"ln_host"' | cut -d'"' -f4)
        ELECTRUM_LN_PORT=$(cat "$ELECTRUM_INFO" | grep '"ln_port"' | grep -o '[0-9]*')
        if [ -n "$ELECTRUM_LN_PUBKEY" ]; then
            break
        fi
    fi
    retries=$((retries + 1))
    sleep 2
done

if [ -z "$ELECTRUM_LN_PUBKEY" ]; then
    log "ERROR: Could not read Electrum LN pubkey from $ELECTRUM_INFO"
    exit 1
fi

# Step 3: Get node identities
TAKER_PUBKEY=$($LNCLI_TAKER getinfo | grep '"identity_pubkey"' | cut -d'"' -f4)
log "Electrum LN pubkey: $ELECTRUM_LN_PUBKEY"
log "Taker LND pubkey:   $TAKER_PUBKEY"

if [ -z "$TAKER_PUBKEY" ]; then
    log "ERROR: Failed to get taker node pubkey"
    exit 1
fi

# Step 4: Fund LND-taker (Electrum is funded via electrs blockchain indexing)
TAKER_ADDR=$($LNCLI_TAKER newaddress p2wkh | grep '"address"' | cut -d'"' -f4)
log "Taker address: $TAKER_ADDR"

# Also generate an address for the Electrum node — we'll send BTC via Bitcoin Core
# The Electrum node picks up UTXOs via electrs indexing
# We use a placeholder address here; the Electrum node should already have an address
# from its wallet creation. We'll fund via a general regtest address.
# For the channel, LND-taker will be the opener (since we have lncli).

# Fund LND-taker via Bitcoin Core (which has coinbase funds from auto-mining)
log "Funding LND taker via Bitcoin Core..."
if ! btc_rpc sendtoaddress "[\"$TAKER_ADDR\", $FUNDING_AMOUNT_BTC]" > /dev/null 2>&1; then
    log "sendtoaddress for taker failed, mining blocks first..."
    MINING_ADDR=$(btc_rpc getnewaddress '["", "bech32"]' | grep -o '"result":"[^"]*"' | cut -d'"' -f4)
    btc_rpc generatetoaddress "[110, \"$MINING_ADDR\"]" > /dev/null 2>&1
    btc_rpc sendtoaddress "[\"$TAKER_ADDR\", $FUNDING_AMOUNT_BTC]" > /dev/null
fi

# Mine blocks for confirmation
log "Mining blocks for funding confirmation..."
MINING_ADDR=$(btc_rpc getnewaddress '["", "bech32"]' | grep -o '"result":"[^"]*"' | cut -d'"' -f4)
btc_rpc generatetoaddress "[6, \"$MINING_ADDR\"]" > /dev/null 2>&1

# Wait for LND-taker to see the funds
log "Waiting for LND-taker to see confirmed funds..."
retries=0
while [ $retries -lt 30 ]; do
    taker_balance=$($LNCLI_TAKER walletbalance 2>/dev/null | grep '"confirmed_balance"' | cut -d'"' -f4)
    if [ -n "$taker_balance" ] && [ "$taker_balance" != "0" ]; then
        log "Taker confirmed balance: $taker_balance sats"
        break
    fi
    retries=$((retries + 1))
    sleep 2
done

# Step 5: Connect LND-taker to Electrum's LN node
# This is a cross-implementation connection: LND -> Electrum built-in LN
log "Connecting lnd-taker to Electrum LN node at ${ELECTRUM_LN_HOST}:${ELECTRUM_LN_PORT}..."
$LNCLI_TAKER connect "${ELECTRUM_LN_PUBKEY}@${ELECTRUM_LN_HOST}:${ELECTRUM_LN_PORT}" 2>/dev/null || {
    log "Already connected or connection in progress"
}

# Verify connection
sleep 3
peers=$($LNCLI_TAKER listpeers | grep -c '"pub_key"' || true)
log "Taker has $peers peer(s)"

if [ "$peers" -eq 0 ]; then
    log "WARNING: No peers connected. Retrying connection..."
    sleep 5
    $LNCLI_TAKER connect "${ELECTRUM_LN_PUBKEY}@${ELECTRUM_LN_HOST}:${ELECTRUM_LN_PORT}" 2>/dev/null || true
    sleep 3
    peers=$($LNCLI_TAKER listpeers | grep -c '"pub_key"' || true)
    log "Taker now has $peers peer(s)"
fi

# Step 6: Open channel from LND-taker to Electrum with push_amt
# push_amt gives the Electrum side some initial balance (for reverse swaps)
log "Opening channel: size=${CHANNEL_SIZE} sats, push_amt=${PUSH_AMT} sats..."
FUNDING_TXID=$($LNCLI_TAKER openchannel \
    --node_key="$ELECTRUM_LN_PUBKEY" \
    --local_amt="$CHANNEL_SIZE" \
    --push_amt="$PUSH_AMT" \
    --min_confs=1 2>&1 | grep '"funding_txid"' | cut -d'"' -f4)

if [ -z "$FUNDING_TXID" ]; then
    log "WARNING: Could not parse funding txid. Channel may still be opening..."
    # Try to get it from pending channels
    sleep 2
    FUNDING_TXID=$($LNCLI_TAKER pendingchannels 2>/dev/null | grep '"channel_point"' | head -1 | cut -d'"' -f4 | cut -d: -f1)
fi

log "Channel funding txid: $FUNDING_TXID"

# Mine blocks to confirm the channel
log "Mining blocks to confirm channel..."
btc_rpc generatetoaddress "[10, \"$MINING_ADDR\"]" > /dev/null 2>&1

# Wait for channel to become active; mine extra blocks every 10s if needed
log "Waiting for channel to become active..."
retries=0
active=0
while [ $retries -lt $MAX_RETRIES ]; do
    active=$($LNCLI_TAKER listchannels 2>/dev/null | grep -c '"active": *true' || true)
    if [ "$active" -gt 0 ]; then
        log "Channel is active!"
        break
    fi
    retries=$((retries + 1))
    # Mine an extra block every 5 retries (~10s) in case confirmations are needed
    if [ $((retries % 5)) -eq 0 ]; then
        log "Mining extra block to help channel confirm (retry $retries)..."
        btc_rpc generatetoaddress "[1, \"$MINING_ADDR\"]" > /dev/null 2>&1
    fi
    sleep $RETRY_INTERVAL
done

if [ "$active" -eq 0 ]; then
    log "ERROR: Channel did not become active after $MAX_RETRIES retries"
    $LNCLI_TAKER pendingchannels
    exit 1
fi

# Show channel details
log "=== Channel Details ==="
$LNCLI_TAKER listchannels | grep -E '"remote_pubkey"|"capacity"|"local_balance"|"remote_balance"' | head -8
log "======================="

# Step 7: Fund the Electrum swap server wallet with on-chain BTC.
# The swap server needs on-chain funds to create lockup transactions for
# reverse submarine swaps (client pays LN, server locks up BTC on-chain).
log "Funding Electrum swap server on-chain wallet..."
ELECTRUM_ONCHAIN_ADDR=$(cat "$ELECTRUM_INFO" | grep '"onchain_address"' | cut -d'"' -f4)

if [ -n "$ELECTRUM_ONCHAIN_ADDR" ] && [ "$ELECTRUM_ONCHAIN_ADDR" != "null" ]; then
    log "Electrum on-chain address: $ELECTRUM_ONCHAIN_ADDR"
    # Send 1 BTC to the Electrum wallet for swap lockup operations
    btc_rpc sendtoaddress "[\"$ELECTRUM_ONCHAIN_ADDR\", 1.0]" > /dev/null 2>&1 || {
        log "WARNING: Failed to fund Electrum wallet. Reverse swaps may not work."
    }
    # Mine to confirm the funding
    btc_rpc generatetoaddress "[6, \"$MINING_ADDR\"]" > /dev/null 2>&1
    log "Electrum wallet funded with 1.0 BTC"
else
    log "WARNING: No on-chain address found in $ELECTRUM_INFO. Reverse swaps may not work."
fi

# Step 8: Export connection info for other containers
log "Exporting LND connection info to /shared..."
mkdir -p /shared/lnd

# Copy taker TLS cert and macaroon (for taker containers to connect)
cp /lnd-taker/tls.cert /shared/lnd/taker-tls.cert 2>/dev/null || true
cp /lnd-taker/data/chain/bitcoin/regtest/admin.macaroon /shared/lnd/taker-admin.macaroon 2>/dev/null || true
chmod 644 /shared/lnd/taker-admin.macaroon 2>/dev/null || true

# Write connection metadata
cat > /shared/lnd/connection-info.json << CONNEOF
{
    "electrum_swap_server": {
        "ln_pubkey": "$ELECTRUM_LN_PUBKEY",
        "ln_host": "$ELECTRUM_LN_HOST",
        "ln_port": $ELECTRUM_LN_PORT
    },
    "taker": {
        "pubkey": "$TAKER_PUBKEY",
        "rest_url": "https://jm-lnd-taker:8080",
        "grpc_host": "jm-lnd-taker:10009",
        "cert_path": "/shared/lnd/taker-tls.cert",
        "macaroon_path": "/shared/lnd/taker-admin.macaroon"
    },
    "channel": {
        "funding_txid": "$FUNDING_TXID",
        "capacity": $CHANNEL_SIZE,
        "push_amt": $PUSH_AMT
    }
}
CONNEOF

# Mark setup as done
touch /shared/lightning-setup-done

log "Lightning Network setup complete!"
log "  Electrum LN: $ELECTRUM_LN_PUBKEY @ ${ELECTRUM_LN_HOST}:${ELECTRUM_LN_PORT}"
log "  LND Taker:   $TAKER_PUBKEY"
log "  Channel:     ${CHANNEL_SIZE} sats (${PUSH_AMT} pushed to Electrum)"
