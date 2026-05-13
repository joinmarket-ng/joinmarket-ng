"""
Wallet constants shared across wallet modules.
"""

from __future__ import annotations

# Fidelity bond constants
FIDELITY_BOND_BRANCH = 2  # Internal branch for fidelity bonds

# Default range for descriptor scans (Bitcoin Core default is 1000)
DEFAULT_SCAN_RANGE = 1000

# Upper bound for auto-expansion of the descriptor scan range during
# ``setup_descriptor_wallet``. Prevents pathological loops on wallets that
# claim arbitrarily high used indices.
MAX_AUTO_SCAN_RANGE = 100_000
