#!/bin/sh
# Fund maker wallets for E2E testing
# This script generates addresses from known mnemonics and mines BTC to them

set -e

RPC_HOST="${RPC_HOST:-jm-bitcoin}"
RPC_PORT="${RPC_PORT:-18443}"
RPC_USER="${RPC_USER:-test}"
RPC_PASSWORD="${RPC_PASSWORD:-test}"
BLOCKS_TO_MINE="${BLOCKS_TO_MINE:-112}"

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

# Maker1 address (derived from: avoid whisper mesh corn...)
MAKER1_ADDR="bcrt1q6x4xurtda3szpc54knp6qpuh0jxgcjajmnmy89"

# Maker2 address (derived from: minute faint grape...)
MAKER2_ADDR="bcrt1qfuzpvnf2lgg8z54p3xcjp8xf8x5ydla63tgud2"

# Maker3 address (derived from: echo rural present...)
MAKER3_ADDR="bcrt1qe4hmtjq53u7l5vr9uw6sjr9c75ulmklg8jgsj0"

# Maker4 address (derived from: tower fence frozen...)
MAKER4_ADDR="bcrt1qky5mftk8zj07ewcru27zngg2ersz4mpxkmvclm"

# Maker5 address (derived from: lemon orchard violet...)
MAKER5_ADDR="bcrt1qed048vcfagng5k3s257rzx2dr4ckga0fhr5edt"

# Taker address (derived from: burden notable love...)
TAKER_ADDR="bcrt1q84l5vscg3pvjn6se8jp4ruymtyh393ed5v2d9e"

# Maker-Neutrino address (derived from: ice index boss...)
MAKER_NEUTRINO_ADDR="bcrt1q6mse43hzgfdqh7fyg05lmd4x2ufhlunn3gw5j3"

# Fidelity bond P2WSH address for Maker1
# Path: m/84'/1'/0'/2/0 with locktime 4099766400 (Dec 1, 2099)
# Generated with: python -m jmwallet generate-bond-address --mnemonic "avoid whisper..." --locktime 4099766400 --network regtest
MAKER1_FIDELITY_BOND_ADDR="bcrt1q7yv9xfz7vt5nn3nmpnrh899sxs5s9jnlqe94e8xx4jxc55xhtxcq0dgjy6"

# Create a test-funder wallet with a large initial balance.
# Do this BEFORE funding the maker/taker wallets so the coinbase subsidy is
# still near its maximum.  The wallet is used by e2e test fixtures to send
# BTC to per-test wallets via ``sendtoaddress`` instead of mining coinbases.
# Mining coinbases to fund individual test wallets advances the chain height
# and eventually pushes the regtest subsidy to zero, breaking all fixtures
# that rely on coinbase rewards.
echo "Setting up test-funder wallet..."
$CLI createwallet "test-funder" false false "" false true true || true
TEST_FUNDER_ADDR=$($CLI -rpcwallet=test-funder getnewaddress "" "bech32")
# Mine 200 blocks to test-funder while the subsidy is still near its peak
# (heights 1-150: 50 BTC; 151-300: 25 BTC).  200 blocks ≈ 6 000-7 500 BTC –
# enough for hundreds of test fixtures even if each needs 10 BTC.
$CLI generatetoaddress 200 "$TEST_FUNDER_ADDR"
# Mine 100 maturity blocks to MAKER1_ADDR so the test-funder coinbases all
# have at least 100 confirmations and are immediately spendable.
$CLI generatetoaddress 100 "$MAKER1_ADDR"
TEST_FUNDER_BALANCE=$($CLI -rpcwallet=test-funder getbalance)
echo "Test-funder wallet funded: $TEST_FUNDER_BALANCE BTC spendable"

# Create and pre-fund the fidelity_funder wallet from test-funder.
# This avoids relying on late-stage coinbase rewards after many halvings.
echo "Setting up fidelity_funder wallet..."
$CLI createwallet "fidelity_funder" false false "" false true true || true
FIDELITY_FUNDER_ADDR=$($CLI -rpcwallet=fidelity_funder getnewaddress "" "bech32")
echo "Sending 5 BTC from test-funder to fidelity_funder..."
$CLI -rpcwallet=test-funder sendtoaddress "$FIDELITY_FUNDER_ADDR" 5.0
$CLI generatetoaddress 6 "$MAKER1_ADDR"

echo "Funding maker and taker wallets..."
echo "  Maker1: $MAKER1_ADDR"
echo "  Maker1 Fidelity Bond: $MAKER1_FIDELITY_BOND_ADDR"
echo "  Maker2: $MAKER2_ADDR"
echo "  Maker3: $MAKER3_ADDR"
echo "  Maker4: $MAKER4_ADDR"
echo "  Maker5: $MAKER5_ADDR"
echo "  Maker-Neutrino: $MAKER_NEUTRINO_ADDR"
echo "  Taker:  $TAKER_ADDR"

# Mine blocks to each address to fund them
# Each block gives 50 BTC on regtest
$CLI generatetoaddress $BLOCKS_TO_MINE "$MAKER1_ADDR"
echo "Mined $BLOCKS_TO_MINE blocks to Maker1"

$CLI generatetoaddress $BLOCKS_TO_MINE "$MAKER2_ADDR"
echo "Mined $BLOCKS_TO_MINE blocks to Maker2"

$CLI generatetoaddress $BLOCKS_TO_MINE "$MAKER3_ADDR"
echo "Mined $BLOCKS_TO_MINE blocks to Maker3"

$CLI generatetoaddress $BLOCKS_TO_MINE "$MAKER4_ADDR"
echo "Mined $BLOCKS_TO_MINE blocks to Maker4"

$CLI generatetoaddress $BLOCKS_TO_MINE "$MAKER5_ADDR"
echo "Mined $BLOCKS_TO_MINE blocks to Maker5"

$CLI generatetoaddress $BLOCKS_TO_MINE "$TAKER_ADDR"
echo "Mined $BLOCKS_TO_MINE blocks to Taker"

$CLI generatetoaddress $BLOCKS_TO_MINE "$MAKER_NEUTRINO_ADDR"
echo "Mined $BLOCKS_TO_MINE blocks to Maker-Neutrino"

# Mine some extra blocks for coinbase maturity
# After this, all wallets should have spendable funds
$CLI generatetoaddress 10 "$MAKER1_ADDR"

# Create a transaction from fidelity_funder to the fidelity bond address.
echo "Creating fidelity bond transaction..."

# Verify we have spendable funds
BALANCE=$($CLI -rpcwallet=fidelity_funder getbalance)
echo "Funder wallet balance: $BALANCE BTC"

# Send 1 BTC to the fidelity bond address
echo "Sending 1 BTC to fidelity bond address..."
$CLI -rpcwallet=fidelity_funder sendtoaddress "$MAKER1_FIDELITY_BOND_ADDR" 1.0

# Mine a block to confirm the fidelity bond transaction
$CLI generatetoaddress 6 "$MAKER1_ADDR"

echo "Wallet funding complete!"
echo "Each wallet should have ~5500 BTC from coinbase rewards"
echo "Maker1 also has 1 BTC in fidelity bond (timelocked P2WSH)"

# Show final blockchain state
finalcount=$($CLI getblockcount 2>/dev/null)
echo "Final block height: $finalcount"
