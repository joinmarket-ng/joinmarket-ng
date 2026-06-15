#!/bin/sh
# Fund maker wallets for E2E testing
#
# Uses a test-funder wallet to send a single large UTXO to each maker/taker
# address instead of mining coinbases directly to those addresses.
#
# Mining many coinbases to the same fixed address triggers the wallet's
# auto-freeze-on-reuse feature (jm:autofrozen:reuse), which freezes ALL those
# UTXOs and leaves the maker with zero available balance.  Sending a single
# transaction per wallet avoids address reuse entirely.

set -e

RPC_HOST="${RPC_HOST:-jm-bitcoin}"
RPC_PORT="${RPC_PORT:-18443}"
RPC_USER="${RPC_USER:-test}"
RPC_PASSWORD="${RPC_PASSWORD:-test}"

CLI="bitcoin-cli -chain=regtest -rpcconnect=$RPC_HOST -rpcport=$RPC_PORT -rpcuser=$RPC_USER -rpcpassword=$RPC_PASSWORD"

echo "Waiting for Bitcoin Core to be ready..."
until $CLI getblockchaininfo > /dev/null 2>&1; do
    sleep 2
done
echo "Bitcoin Core is ready"

# Known wallet addresses derived from the test mnemonics:
# These are the first receive addresses (m/84'/1'/0'/0/0) for each wallet
# BIP84 native segwit path uses coin type 1 for testnet/regtest
#
# Maker1: "avoid whisper mesh corn already blur sudden fine planet chicken hover sniff"
#   Address: bcrt1q6x4xurtda3szpc54knp6qpuh0jxgcjajmnmy89
#
# Maker2: "minute faint grape plate stock mercy tent world space opera apple rocket"
#   Address: bcrt1qfuzpvnf2lgg8z54p3xcjp8xf8x5ydla63tgud2
#
# Maker3: "echo rural present blue chapter game keen keen keen keen keen keen"
#   Address: bcrt1qf5gztst2rddqv4hw2jh4m52ahrrvjrz4zescgw
#
# Maker4: "tower fence frozen amazing mosquito hint pause sausage door enrich gentle pulp"
#   Address: bcrt1qky5mftk8zj07ewcru27zngg2ersz4mpxkmvclm
#
# Maker5: "lemon orchard violet bargain travel orange brown dolphin hour ribbon canyon coral"
#   Address: bcrt1qed048vcfagng5k3s257rzx2dr4ckga0fhr5edt
#
# Maker-Neutrino: "ice index boss season jealous supreme nephew kit cool lock caught enter"
#   Address: bcrt1q6mse43hzgfdqh7fyg05lmd4x2ufhlunn3gw5j3
#
# Taker: "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
#   Address: bcrt1q84l5vscg3pvjn6se8jp4ruymtyh393ed5v2d9e
#
# These addresses are derived using BIP84 (native segwit) path for regtest/testnet

# Get current block height
blockcount=$($CLI getblockcount 2>/dev/null || echo "0")
echo "Current block height: $blockcount"

MAKER1_ADDR="bcrt1q6x4xurtda3szpc54knp6qpuh0jxgcjajmnmy89"
MAKER2_ADDR="bcrt1qfuzpvnf2lgg8z54p3xcjp8xf8x5ydla63tgud2"
MAKER3_ADDR="bcrt1qe4hmtjq53u7l5vr9uw6sjr9c75ulmklg8jgsj0"
MAKER4_ADDR="bcrt1qky5mftk8zj07ewcru27zngg2ersz4mpxkmvclm"
MAKER5_ADDR="bcrt1qed048vcfagng5k3s257rzx2dr4ckga0fhr5edt"
TAKER_ADDR="bcrt1q84l5vscg3pvjn6se8jp4ruymtyh393ed5v2d9e"
MAKER_NEUTRINO_ADDR="bcrt1q6mse43hzgfdqh7fyg05lmd4x2ufhlunn3gw5j3"

# Fidelity bond P2WSH address for Maker1
# Path: m/84'/1'/0'/2/0 with locktime 4099766400 (Dec 1, 2099)
MAKER1_FIDELITY_BOND_ADDR="bcrt1q7yv9xfz7vt5nn3nmpnrh899sxs5s9jnlqe94e8xx4jxc55xhtxcq0dgjy6"

# =============================================================================
# Phase 1: Build a large test-funder balance by mining early when subsidy is high
# =============================================================================
echo "Setting up test-funder wallet..."
$CLI createwallet "test-funder" false false "" false true true || true
TEST_FUNDER_ADDR=$($CLI -rpcwallet=test-funder getnewaddress "" "bech32")

# Mine 200 blocks to test-funder at early heights (50 BTC/block) = ~10,000 BTC
$CLI generatetoaddress 200 "$TEST_FUNDER_ADDR"

# Mine 110 maturity blocks so test-funder coinbases are spendable (need 100 conf)
# Mine these to test-funder too -- a fresh address each time avoids reuse
$CLI generatetoaddress 110 "$TEST_FUNDER_ADDR"

TEST_FUNDER_BALANCE=$($CLI -rpcwallet=test-funder getbalance)
echo "Test-funder wallet funded: $TEST_FUNDER_BALANCE BTC spendable"

# =============================================================================
# Phase 2: Fund each maker/taker with multiple sendtoaddress transactions
#
# Each wallet receives several independent UTXOs at the same address.
# Using sendtoaddress (rather than direct mining to maker addresses) avoids
# triggering the wallet's auto-freeze-on-reuse detector: that feature only
# fires on UTXOs that arrive AFTER the address was EMPTIED (spent-zero).
# As long as the address still holds funds from an earlier send, subsequent
# sends are not considered forced reuse.
#
# Multiple UTXOs allow makers to participate in several consecutive CoinJoins
# within a single test session without running out of funds.
# =============================================================================
echo "Funding maker and taker wallets via sendtoaddress..."
echo "  Maker1: $MAKER1_ADDR"
echo "  Maker2: $MAKER2_ADDR"
echo "  Maker3: $MAKER3_ADDR"
echo "  Maker4: $MAKER4_ADDR"
echo "  Maker5: $MAKER5_ADDR"
echo "  Maker-Neutrino: $MAKER_NEUTRINO_ADDR"
echo "  Taker:  $TAKER_ADDR"

# Send 10 transactions of 100 BTC each (1,000 BTC total per wallet).
# Each send creates one UTXO, giving the maker 10 independent UTXOs.
# The address is re-used across sends but is never emptied in between,
# so the auto-freeze-on-reuse feature does not trigger.
AMOUNT_PER_SEND="100.0"
SENDS_PER_WALLET=10

for wallet_addr in "$MAKER1_ADDR" "$MAKER2_ADDR" "$MAKER3_ADDR" \
                   "$MAKER4_ADDR" "$MAKER5_ADDR" "$TAKER_ADDR" \
                   "$MAKER_NEUTRINO_ADDR"; do
    for i in $(seq 1 $SENDS_PER_WALLET); do
        $CLI -rpcwallet=test-funder sendtoaddress "$wallet_addr" $AMOUNT_PER_SEND > /dev/null
    done
    echo "Sent ${SENDS_PER_WALLET}x${AMOUNT_PER_SEND} BTC to $wallet_addr"
done

# Confirm the sendtoaddress transactions with 110 blocks so every maker/taker
# UTXO reaches >= 100 confirmations. Some tests (e.g. the neutrino maker
# CoinJoin) require taker_utxo_age=100, which a shallow 6-block confirmation
# would not satisfy. Mine to test-funder so no extra UTXOs land on maker/taker
# addresses (which would trigger address-reuse auto-freeze).
$CLI generatetoaddress 110 "$TEST_FUNDER_ADDR"
echo "Mined 110 confirmation blocks (maker/taker UTXOs now have >= 100 confirmations)"

# =============================================================================
# Phase 3: Set up fidelity bond wallet and create fidelity bond for Maker1
# =============================================================================
echo "Setting up fidelity_funder wallet..."
$CLI createwallet "fidelity_funder" false false "" false true true || true
FIDELITY_FUNDER_ADDR=$($CLI -rpcwallet=fidelity_funder getnewaddress "" "bech32")
echo "Sending 5 BTC from test-funder to fidelity_funder..."
$CLI -rpcwallet=test-funder sendtoaddress "$FIDELITY_FUNDER_ADDR" 5.0
$CLI generatetoaddress 6 "$TEST_FUNDER_ADDR"

echo "Creating fidelity bond transaction..."
BALANCE=$($CLI -rpcwallet=fidelity_funder getbalance)
echo "Funder wallet balance: $BALANCE BTC"

echo "Sending 1 BTC to fidelity bond address..."
$CLI -rpcwallet=fidelity_funder sendtoaddress "$MAKER1_FIDELITY_BOND_ADDR" 1.0

# Mine 6 confirmation blocks to test-funder (not to maker addresses)
$CLI generatetoaddress 6 "$TEST_FUNDER_ADDR"

TEST_FUNDER_REMAINING=$($CLI -rpcwallet=test-funder getbalance)
echo "Wallet funding complete!"
echo "Each maker/taker has ${SENDS_PER_WALLET}x${AMOUNT_PER_SEND} BTC = 10 UTXOs"
echo "Maker1 also has 1 BTC in fidelity bond (timelocked P2WSH)"
echo "Test-funder remaining: $TEST_FUNDER_REMAINING BTC"

# Show final blockchain state
finalcount=$($CLI getblockcount 2>/dev/null)
echo "Final block height: $finalcount"
