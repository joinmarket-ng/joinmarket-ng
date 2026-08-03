"""
Shared test helpers for taker tests.

Constants and factory functions used across taker test files.
Separated from conftest.py to avoid import collisions when running
tests from the monorepo root.
"""

from __future__ import annotations

from jmcore.crypto import NickIdentity
from jmcore.encryption import CryptoSession
from jmcore.models import NetworkType
from jmwallet.wallet.models import UTXOInfo

from taker.config import TakerConfig
from taker.taker import MultiDirectoryClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAMPLE_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)
TEST_SCRIPTPUBKEY = "001400" * 10
TEST_DIRECTORY_SERVERS = ["localhost:5222"]
TEST_NICK_AUTH_DIRECTORY_IDS = {"localhost:5222": "test:taker-directory"}


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def make_utxo(
    *,
    txid_char: str = "a",
    vout: int = 0,
    value: int = 25_000_000,
    address: str = "bcrt1qtest1",
    confirmations: int = 10,
    scriptpubkey: str = TEST_SCRIPTPUBKEY,
    path: str = "m/84'/1'/0'/0/0",
    mixdepth: int = 0,
) -> UTXOInfo:
    """Create a UTXOInfo with sensible defaults for testing."""
    return UTXOInfo(
        txid=txid_char * 64,
        vout=vout,
        value=value,
        address=address,
        confirmations=confirmations,
        scriptpubkey=scriptpubkey,
        path=path,
        mixdepth=mixdepth,
    )


def make_crypto_pair() -> tuple[CryptoSession, CryptoSession]:
    """Create a paired taker/maker CryptoSession with encryption set up."""
    taker_crypto = CryptoSession()
    maker_crypto = CryptoSession()
    taker_crypto.setup_encryption(maker_crypto.get_pubkey_hex())
    maker_crypto.setup_encryption(taker_crypto.get_pubkey_hex())
    return taker_crypto, maker_crypto


def make_taker_config(**overrides: object) -> TakerConfig:
    """Create a TakerConfig with standard test defaults.

    Any keyword argument overrides the default value.
    """
    defaults: dict[str, object] = {
        "mnemonic": SAMPLE_MNEMONIC,
        "network": NetworkType.REGTEST,
        "directory_servers": TEST_DIRECTORY_SERVERS,
        "nick_auth_directory_ids": TEST_NICK_AUTH_DIRECTORY_IDS,
    }
    defaults.update(overrides)
    return TakerConfig(**defaults)  # type: ignore[arg-type]


def make_directory_client(**overrides: object) -> MultiDirectoryClient:
    """Create a MultiDirectoryClient with standard test defaults."""
    nick_identity = NickIdentity(5)
    defaults: dict[str, object] = {
        "directory_servers": TEST_DIRECTORY_SERVERS,
        "nick_auth_directory_ids": TEST_NICK_AUTH_DIRECTORY_IDS,
        "network": "regtest",
        "nick_identity": nick_identity,
    }
    defaults.update(overrides)
    return MultiDirectoryClient(**defaults)  # type: ignore[arg-type]
