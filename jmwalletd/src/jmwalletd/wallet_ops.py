"""Wallet operations bridge.

Thin adapter layer between the HTTP API and our ``jmwallet.WalletService``.
These functions handle wallet creation, opening, and recovery, returning
the initialised WalletService instances that the daemon state holds.

The actual wallet implementation lives in the ``jmwallet`` package; this
module only wires things together in the way the HTTP daemon needs.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

from loguru import logger


def _is_descriptor_backend(backend: Any) -> bool:
    """Return True if *backend* supports descriptor wallet operations."""
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

    return isinstance(backend, DescriptorWalletBackend)


def _get_network() -> str:
    """Return the configured wallet address network (e.g. ``"regtest"``).

    Uses ``network_config.bitcoin_network`` when set, otherwise falls back to
    ``network_config.network``. This allows protocol network (directory
    handshakes) to differ from Bitcoin address network in regtest-compatible
    mixed deployments.
    """
    from jmcore.settings import get_settings

    network_config = get_settings().network_config
    if network_config.bitcoin_network is not None:
        return network_config.bitcoin_network.value
    return network_config.network.value


async def create_wallet(
    *,
    wallet_path: Path,
    password: str,
    wallet_type: str,
    data_dir: Path,
) -> tuple[Any, str]:
    """Create a new wallet and return ``(wallet_service, seedphrase)``.

    Args:
        wallet_path: Full path for the new .jmdat wallet file.
        password: Encryption password.
        wallet_type: One of ``"sw"``, ``"sw-legacy"``, ``"sw-fb"``.
        data_dir: Application data directory.

    Returns:
        Tuple of (WalletService, seed_phrase_string).

    Raises:
        FileExistsError: If the wallet file already exists.
        ValueError: If the wallet type is invalid.
    """
    from jmwallet.wallet.service import WalletService
    from jmwalletd._backend import get_backend

    if wallet_path.exists():
        raise FileExistsError(f"Wallet file already exists: {wallet_path}")

    valid_types = {"sw", "sw-legacy", "sw-fb"}
    if wallet_type not in valid_types:
        msg = f"Invalid wallet type: {wallet_type}. Must be one of {valid_types}"
        raise ValueError(msg)

    from mnemonic import Mnemonic

    mnemo = Mnemonic("english")
    seedphrase = mnemo.generate(strength=128)

    backend = await get_backend(
        data_dir=data_dir,
        mnemonic=seedphrase,
        network=_get_network(),
    )

    # Record current block height as the wallet birthday.  Since this is a
    # brand-new wallet, it cannot have received any funds before this point,
    # so future rescans can safely start from this height.
    creation_height: int | None = None
    try:
        creation_height = await backend.get_block_height()
        logger.info(f"Recording wallet creation height: {creation_height}")
    except Exception as exc:
        logger.warning(f"Could not fetch block height for wallet birthday: {exc}")

    ws = WalletService(
        mnemonic=seedphrase,
        backend=backend,
        data_dir=data_dir,
        network=_get_network(),
    )

    # Persist the wallet file (encrypted with the password).
    _save_wallet_file(
        wallet_path=wallet_path,
        mnemonic=seedphrase,
        password=password,
        wallet_type=wallet_type,
        creation_height=creation_height,
    )

    # Ensure the watch-only descriptor wallet is loaded in Bitcoin Core
    # and import HD descriptors.  No rescan needed for a brand-new wallet.
    # Skipped for non-descriptor backends (e.g. neutrino).
    if _is_descriptor_backend(backend):
        await ws.setup_descriptor_wallet(rescan=False)

    # Initial sync to populate caches.
    await ws.sync()

    logger.info("Created wallet: {}", wallet_path.name)
    return ws, seedphrase


async def recover_wallet(
    *,
    wallet_path: Path,
    password: str,
    wallet_type: str,
    seedphrase: str,
    data_dir: Path,
) -> Any:
    """Recover a wallet from a BIP39 seed phrase.

    Returns:
        WalletService instance.

    Raises:
        FileExistsError: If the wallet file already exists.
        ValueError: If the seed phrase or wallet type is invalid.
    """
    from mnemonic import Mnemonic

    from jmwallet.wallet.service import WalletService
    from jmwalletd._backend import get_backend

    if wallet_path.exists():
        raise FileExistsError(f"Wallet file already exists: {wallet_path}")

    mnemo = Mnemonic("english")
    if not mnemo.check(seedphrase):
        msg = "Invalid BIP39 mnemonic seed phrase."
        raise ValueError(msg)

    valid_types = {"sw", "sw-legacy", "sw-fb"}
    if wallet_type not in valid_types:
        msg = f"Invalid wallet type: {wallet_type}. Must be one of {valid_types}"
        raise ValueError(msg)

    backend = await get_backend(
        data_dir=data_dir,
        mnemonic=seedphrase,
        network=_get_network(),
    )

    ws = WalletService(
        mnemonic=seedphrase,
        backend=backend,
        data_dir=data_dir,
        network=_get_network(),
    )

    _save_wallet_file(
        wallet_path=wallet_path,
        mnemonic=seedphrase,
        password=password,
        wallet_type=wallet_type,
    )

    # Ensure the watch-only descriptor wallet is loaded in Bitcoin Core
    # and import HD descriptors so sync can find existing UTXOs.
    # Skipped for non-descriptor backends (e.g. neutrino).
    if _is_descriptor_backend(backend):
        await ws.setup_descriptor_wallet()

    await ws.sync()

    logger.info("Recovered wallet: {}", wallet_path.name)
    return ws


async def open_wallet_with_mnemonic(
    *,
    wallet_path: Path,
    password: str,
    data_dir: Path,
    sync_on_open: bool = True,
) -> tuple[Any, str]:
    """Open (unlock) an existing wallet file and return mnemonic.

    Returns:
        Tuple of (WalletService, seedphrase).

    Raises:
        FileNotFoundError: If the wallet file doesn't exist.
        ValueError: If the password is wrong.
    """
    from jmwallet.wallet.service import WalletService
    from jmwalletd._backend import get_backend

    if not wallet_path.exists():
        raise FileNotFoundError(f"Wallet file not found: {wallet_path}")

    seedphrase, creation_height = _load_wallet_file(wallet_path=wallet_path, password=password)

    backend = await get_backend(
        data_dir=data_dir,
        mnemonic=seedphrase,
        network=_get_network(),
    )

    # Propagate wallet creation height hint to the backend.  Passing None
    # clears any stale hint from a previously opened wallet when backend
    # instances are reused.
    backend.set_wallet_creation_height(creation_height)

    ws = WalletService(
        mnemonic=seedphrase,
        backend=backend,
        data_dir=data_dir,
        network=_get_network(),
    )

    # Ensure the watch-only descriptor wallet is loaded in Bitcoin Core
    # and import HD descriptors.  Idempotent — skips if already set up.
    # Skipped for non-descriptor backends (e.g. neutrino).
    if _is_descriptor_backend(backend):
        await ws.setup_descriptor_wallet()

    if sync_on_open:
        await ws.sync()

    logger.info("Opened wallet: {}", wallet_path.name)
    return ws, seedphrase


async def open_wallet(
    *,
    wallet_path: Path,
    password: str,
    data_dir: Path,
    sync_on_open: bool = True,
) -> Any:
    """Open (unlock) an existing wallet file.

    Returns:
        WalletService instance.

    Raises:
        FileNotFoundError: If the wallet file doesn't exist.
        ValueError: If the password is wrong.
    """
    ws, _ = await open_wallet_with_mnemonic(
        wallet_path=wallet_path,
        password=password,
        data_dir=data_dir,
        sync_on_open=sync_on_open,
    )
    return ws


def _save_wallet_file(
    *,
    wallet_path: Path,
    mnemonic: str,
    password: str,
    wallet_type: str,
    creation_height: int | None = None,
) -> None:
    """Persist an encrypted wallet file.

    Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256, from the
    ``cryptography`` library) with a key derived from the password via
    Argon2id. Argon2id is memory-hard and significantly more resistant to
    offline GPU/ASIC cracking than the legacy PBKDF2-HMAC-SHA256 format.

    The on-disk layout is:

    ``[ magic 'JMNG' 4B ][ ver 1B ][ kdf_id 1B ][ m_cost u32 BE ]``
    ``[ t_cost u32 BE ][ p_cost u8 ][ salt 16B ][ Fernet token ... ]``

    Older PBKDF2 wallet files (which have no magic and start with a raw
    16-byte salt) are still readable via :func:`_load_wallet_file`. New
    saves always use the Argon2id format.
    """
    import json

    from cryptography.fernet import Fernet

    salt = os.urandom(_SALT_LEN)
    key = base64.urlsafe_b64encode(
        _derive_key_argon2id(
            password=password,
            salt=salt,
            memory_cost=_ARGON2ID_MEMORY_COST,
            time_cost=_ARGON2ID_TIME_COST,
            parallelism=_ARGON2ID_PARALLELISM,
        )
    )
    fernet = Fernet(key)

    wallet_data_dict: dict[str, str | int] = {
        "mnemonic": mnemonic,
        "wallet_type": wallet_type,
    }
    if creation_height is not None:
        wallet_data_dict["creation_height"] = creation_height

    wallet_data = json.dumps(wallet_data_dict).encode()

    encrypted = fernet.encrypt(wallet_data)

    header = _pack_argon2id_header(
        memory_cost=_ARGON2ID_MEMORY_COST,
        time_cost=_ARGON2ID_TIME_COST,
        parallelism=_ARGON2ID_PARALLELISM,
        salt=salt,
    )

    wallet_path.parent.mkdir(parents=True, exist_ok=True)
    wallet_path.write_bytes(header + encrypted)

    logger.debug("Saved wallet file (argon2id): {}", wallet_path)


def _load_wallet_file(*, wallet_path: Path, password: str) -> tuple[str, int | None]:
    """Load and decrypt a wallet file, returning the mnemonic and creation height.

    Auto-detects the on-disk format:

    - New format: ``JMNG`` magic + version + KDF parameters + salt + ciphertext,
      decrypted with Argon2id.
    - Legacy format: 16-byte salt + Fernet ciphertext, decrypted with PBKDF2.
      Kept for backward compatibility with wallet files written before
      the Argon2id migration. The file is *not* silently re-encrypted on
      load -- migration requires the caller to re-save through
      :func:`_save_wallet_file` (for example, on the next password change).

    Returns:
        Tuple of (mnemonic, creation_height).  ``creation_height`` is ``None``
        for wallet files created before that feature was added.

    Raises:
        ValueError: If the password is incorrect or the file is corrupted.
    """
    import json

    from cryptography.fernet import Fernet, InvalidToken

    raw = wallet_path.read_bytes()

    if raw.startswith(_WALLET_MAGIC):
        key, encrypted = _derive_key_from_header(raw, password=password)
    else:
        key, encrypted = _derive_key_legacy_pbkdf2(raw, password=password)

    fernet = Fernet(base64.urlsafe_b64encode(key))

    try:
        decrypted = fernet.decrypt(encrypted)
    except InvalidToken as exc:
        raise ValueError("Wrong password or corrupted wallet file.") from exc

    data = json.loads(decrypted)
    mnemonic: str = data["mnemonic"]

    raw_creation_height = data.get("creation_height")
    creation_height: int | None
    if isinstance(raw_creation_height, int) and not isinstance(raw_creation_height, bool):
        creation_height = raw_creation_height if raw_creation_height >= 0 else None
    else:
        creation_height = None

    return mnemonic, creation_height


# ---------------------------------------------------------------------------
# Wallet file encryption format helpers
# ---------------------------------------------------------------------------

# Magic prefix marking a versioned wallet file. Legacy PBKDF2 files have no
# magic and start with a raw 16-byte random salt; that prefix can never
# coincide with this ASCII tag.
_WALLET_MAGIC = b"JMNG"
_WALLET_FORMAT_VERSION = 1
_KDF_ID_ARGON2ID = 1

_SALT_LEN = 16
_KEY_LEN = 32

# OWASP 2024 baseline for Argon2id (interactive-resistant settings tuned for
# file decryption rather than per-request login): m=19 MiB, t=2, p=1.
# Parameters are stored in the header so they can be raised later without
# breaking older files.
_ARGON2ID_MEMORY_COST = 19_456  # KiB (~19 MiB)
_ARGON2ID_TIME_COST = 2
_ARGON2ID_PARALLELISM = 1

# Legacy PBKDF2 parameters. Kept fixed; we only need to *read* these files.
_LEGACY_PBKDF2_ITERATIONS = 600_000


def _derive_key_argon2id(
    *,
    password: str,
    salt: bytes,
    memory_cost: int,
    time_cost: int,
    parallelism: int,
) -> bytes:
    """Derive a 32-byte key from *password* using Argon2id."""
    from argon2.low_level import Type, hash_secret_raw

    return hash_secret_raw(
        secret=password.encode(),
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=_KEY_LEN,
        type=Type.ID,
    )


def _pack_argon2id_header(
    *,
    memory_cost: int,
    time_cost: int,
    parallelism: int,
    salt: bytes,
) -> bytes:
    """Build the binary header for an Argon2id-encrypted wallet file."""
    if len(salt) != _SALT_LEN:
        msg = f"salt must be {_SALT_LEN} bytes, got {len(salt)}"
        raise ValueError(msg)
    return (
        _WALLET_MAGIC
        + bytes([_WALLET_FORMAT_VERSION, _KDF_ID_ARGON2ID])
        + memory_cost.to_bytes(4, "big")
        + time_cost.to_bytes(4, "big")
        + bytes([parallelism])
        + salt
    )


def _derive_key_from_header(raw: bytes, *, password: str) -> tuple[bytes, bytes]:
    """Parse a new-format wallet file header and return ``(key, ciphertext)``."""
    # Layout: magic(4) | version(1) | kdf_id(1) | m_cost(4) | t_cost(4) | p_cost(1) | salt(16)
    header_len = 4 + 1 + 1 + 4 + 4 + 1 + _SALT_LEN
    if len(raw) < header_len:
        raise ValueError("Wallet file is truncated (header too short).")

    version = raw[4]
    kdf_id = raw[5]
    if version != _WALLET_FORMAT_VERSION:
        msg = f"Unsupported wallet file version: {version}"
        raise ValueError(msg)
    if kdf_id != _KDF_ID_ARGON2ID:
        msg = f"Unsupported wallet KDF id: {kdf_id}"
        raise ValueError(msg)

    memory_cost = int.from_bytes(raw[6:10], "big")
    time_cost = int.from_bytes(raw[10:14], "big")
    parallelism = raw[14]
    salt = raw[15 : 15 + _SALT_LEN]
    ciphertext = raw[header_len:]

    key = _derive_key_argon2id(
        password=password,
        salt=salt,
        memory_cost=memory_cost,
        time_cost=time_cost,
        parallelism=parallelism,
    )
    return key, ciphertext


def _derive_key_legacy_pbkdf2(raw: bytes, *, password: str) -> tuple[bytes, bytes]:
    """Parse a legacy PBKDF2 wallet file and return ``(key, ciphertext)``."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    if len(raw) < _SALT_LEN:
        raise ValueError("Wallet file is truncated (legacy salt missing).")

    salt = raw[:_SALT_LEN]
    ciphertext = raw[_SALT_LEN:]

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LEN,
        salt=salt,
        iterations=_LEGACY_PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode()), ciphertext
