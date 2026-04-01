"""
Bitcoin and JoinMarket protocol constants.

Following the reference implementation's approach to dust thresholds:
- BITCOIN_DUST_THRESHOLD: 5x the standard P2PKH dust limit (546 sats)
- DUST_THRESHOLD: 10x BITCOIN_DUST_THRESHOLD for CoinJoin safety
"""

from __future__ import annotations

# Bitcoin network dust limits
# Standard P2PKH dust limit in Bitcoin Core
STANDARD_DUST_LIMIT = 546  # satoshis

# Bitcoin amount conversion
SATS_PER_BTC = 100_000_000
MAX_MONEY = 21_000_000 * SATS_PER_BTC
BTC_PER_SAT = 1.0 / SATS_PER_BTC  # For display only, never for calculations

# Bitcoin dust threshold: 5x the standard P2PKH dust limit
# This matches the reference implementation's btc.DUST_THRESHOLD
BITCOIN_DUST_THRESHOLD = 5 * STANDARD_DUST_LIMIT  # 2730 satoshis

# JoinMarket dust threshold for CoinJoin operations
# Set to 10x BITCOIN_DUST_THRESHOLD to provide safety margin for:
# 1. Fee estimation uncertainties in collaborative transactions
# 2. Ensuring outputs remain economically spendable
# 3. Avoiding rejection by peers due to changing network conditions
#
# This is a JoinMarket policy, not a Bitcoin protocol rule.
# Reference: JoinMarket sets this to 27300 sats (0.000273 BTC)
DUST_THRESHOLD = 10 * BITCOIN_DUST_THRESHOLD  # 27300 satoshis

# Default dust threshold for non-CoinJoin operations
# Can use the lower BITCOIN_DUST_THRESHOLD for direct payments
# This allows flexibility while maintaining safety for CoinJoin outputs
DEFAULT_DUST_THRESHOLD = DUST_THRESHOLD  # 27300 satoshis (conservative default)

# secp256k1 elliptic curve constants
# Order of the generator point G (number of points on the curve)
SECP256K1_N = int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)
# Field prime (defines the finite field F_p)
SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
