"""
Blockchain backend implementations.

Available backends:
- BitcoinCoreBackend: Full node via Bitcoin Core RPC (no wallet, uses scantxoutset)
- DescriptorWalletBackend: Full node with descriptor wallet (uses importdescriptors + listunspent)
- NeutrinoBackend: Lightweight BIP157/BIP158 SPV client
- MempoolBackend: Mempool.space API (third-party, no setup required)

Backend Selection Guide:
- DescriptorWalletBackend (Recommended): Fastest for ongoing operations. Uses Bitcoin Core's
  descriptor wallet feature to track UTXOs automatically. Requires one-time descriptor import.
- BitcoinCoreBackend: No wallet setup needed, but slower due to full UTXO set scans via
  scantxoutset. Good for simple scripts or one-off operations.
- NeutrinoBackend: Lightweight client for limited storage environments.

Neutrino Compatibility:
All backends support verify_utxo_with_metadata() for Neutrino-compatible
UTXO verification. Check backend.requires_neutrino_metadata() to determine
if the backend needs scriptPubKey/blockheight hints from peers.
"""

from jmwallet.backends.base import (
    UTXO,
    BlockchainBackend,
    BondVerificationRequest,
    BondVerificationResult,
    Transaction,
    UTXOVerificationResult,
)
from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    generate_wallet_name,
    get_mnemonic_fingerprint,
)
from jmwallet.backends.neutrino import NeutrinoBackend, NeutrinoConfig

__all__ = [
    "BlockchainBackend",
    "BitcoinCoreBackend",
    "BondVerificationRequest",
    "BondVerificationResult",
    "DescriptorWalletBackend",
    "NeutrinoBackend",
    "NeutrinoConfig",
    "Transaction",
    "UTXO",
    "UTXOVerificationResult",
    "generate_wallet_name",
    "get_mnemonic_fingerprint",
]
