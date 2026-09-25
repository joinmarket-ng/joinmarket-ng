"""Bitcoin and JoinMarket protocol constants.

The reference implementation uses separate thresholds for two CoinJoin
decisions:

- ``BITCOIN_DUST_THRESHOLD`` controls whether takers keep their own change.
- ``DUST_THRESHOLD`` controls whether maker change is accepted.
"""

from __future__ import annotations

# Network identity shared by wallet backends and scoped market key derivation.
GENESIS_BLOCK_HASHES: dict[str, str] = {
    "mainnet": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
    "testnet": "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943",
    "signet": "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6",
    "regtest": "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206",
}

# Bitcoin network dust limits
# Standard P2PKH dust limit in Bitcoin Core
STANDARD_DUST_LIMIT = 546  # satoshis

# Bitcoin amount conversion
SATS_PER_BTC = 100_000_000
MAX_MONEY = 21_000_000 * SATS_PER_BTC
BTC_PER_SAT = 1.0 / SATS_PER_BTC  # For display only, never for calculations

# Taker change discard threshold: 5x the standard P2PKH dust limit.
# This matches the reference implementation's btc.DUST_THRESHOLD and is an
# economic policy, rather than Bitcoin Core's script-specific relay dust limit.
BITCOIN_DUST_THRESHOLD = 5 * STANDARD_DUST_LIMIT  # 2730 satoshis

# Maker change coordination threshold. Takers reject makers whose inputs do not
# leave change at or above this amount, matching the reference JoinMarket policy.
DUST_THRESHOLD = 10 * BITCOIN_DUST_THRESHOLD  # 27300 satoshis

# Backward-compatible alias for callers that imported the old default.
DEFAULT_DUST_THRESHOLD = DUST_THRESHOLD  # 27300 satoshis (conservative default)

# secp256k1 elliptic curve constants
# Order of the generator point G (number of points on the curve)
SECP256K1_N = int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)
# Field prime (defines the finite field F_p)
SECP256K1_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
