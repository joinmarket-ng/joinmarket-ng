"""Fidelity bond utilities for maker bot."""

from __future__ import annotations

import base64
import os
import struct

from coincurve import PrivateKey
from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.crypto import bitcoin_message_hash_bytes
from jmwallet.wallet.service import WalletService
from loguru import logger
from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

# Fidelity bonds are stored in mixdepth 0, internal branch 2
# Path format: m/84'/coin'/0'/2/index:locktime
FIDELITY_BOND_MIXDEPTH = 0
FIDELITY_BOND_INTERNAL_BRANCH = 2

# Certificate expiry parameters (matching reference implementation)
RETARGET_INTERVAL = 2016  # Bitcoin difficulty retarget interval
BLOCK_COUNT_SAFETY = 2  # Safety margin to reduce chances of proof expiring before verification
CERT_MAX_VALIDITY_TIME = 1  # Validity time in retarget periods (1 = ~2 weeks)

# DEPRECATED: For backwards compatibility with tests only
# The actual expiry is calculated dynamically based on current block height
CERT_EXPIRY_BLOCKS = 2016 * 52  # ~1 year in blocks (DEPRECATED)


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class FidelityBondInfo:
    txid: str
    vout: int
    value: int
    locktime: int
    confirmation_time: int
    bond_value: int
    pubkey: bytes | None = None
    private_key: PrivateKey | None = None
    # Certificate fields (for cold wallet support)
    # When set, the bond proof uses the certificate chain instead of self-signing
    cert_pubkey: bytes | None = None  # Hot wallet certificate public key
    cert_privkey: PrivateKey | None = None  # Hot wallet private key for signing nicks
    cert_signature: bytes | None = None  # Certificate signature by UTXO key
    cert_expiry: int | None = None  # Certificate expiry in 2016-block periods


def _parse_locktime_from_path(path: str) -> int | None:
    """
    Extract locktime from a fidelity bond path.

    Fidelity bond paths have format: m/84'/coin'/0'/2/index:locktime
    where locktime is Unix timestamp.

    Args:
        path: BIP32 derivation path

    Returns:
        Locktime as Unix timestamp, or None if not a fidelity bond path
    """
    if ":" not in path:
        return None

    try:
        # Split on colon to get locktime
        locktime_str = path.split(":")[-1]
        return int(locktime_str)
    except (ValueError, IndexError):
        return None


async def find_fidelity_bonds(
    wallet: WalletService, mixdepth: int = FIDELITY_BOND_MIXDEPTH
) -> list[FidelityBondInfo]:
    """
    Find fidelity bonds in the wallet.

    Fidelity bonds are timelocked UTXOs in mixdepth 0, internal branch 2.
    Path format: m/84'/coin'/0'/2/index:locktime
    They use a CLTV script: <locktime> OP_CLTV OP_DROP <pubkey> OP_CHECKSIG

    This function also loads certificate information from the bond registry if available,
    allowing for cold wallet support where the bond UTXO private key is kept offline.

    Args:
        wallet: WalletService instance
        mixdepth: Mixdepth to search for bonds (default 0)

    Returns:
        List of FidelityBondInfo for each bond found
    """
    bonds: list[FidelityBondInfo] = []

    # Try to load bond registry for certificate information
    from jmwallet.wallet.bond_registry import BondRegistry

    registry: BondRegistry | None = None
    try:
        from jmwallet.wallet.bond_registry import load_registry

        if wallet.data_dir is not None:
            registry = load_registry(wallet.data_dir, wallet.wallet_fingerprint)
            logger.debug(f"Loaded bond registry with {len(registry.bonds)} bonds")
    except Exception as e:
        logger.debug(f"Could not load bond registry: {e}")

    utxos = wallet.utxo_cache.get(mixdepth, [])
    if not utxos:
        return bonds

    for utxo_info in utxos:
        # Fidelity bonds are on internal branch 2 with locktime in path
        # Path format: m/84'/coin'/0'/2/index:locktime
        path_parts = utxo_info.path.split("/")
        if len(path_parts) < 5:
            continue

        # Check if this is internal branch 2 (fidelity bond branch)
        # path_parts[-2] is the branch (0=external, 1=internal change, 2=fidelity bonds)
        branch_part = path_parts[-2]
        if branch_part != str(FIDELITY_BOND_INTERNAL_BRANCH):
            continue

        # Extract locktime from path (format: index:locktime)
        locktime = _parse_locktime_from_path(utxo_info.path)
        if locktime is None:
            # Not a timelocked UTXO
            continue

        # Check if this is an external bond (index=-1 indicates cold storage)
        # External bonds have path format: m/84'/0'/0'/2/-1:locktime
        index_locktime = path_parts[-1]  # e.g., "-1:1769904000" or "0:1768435200"
        index_str = index_locktime.split(":")[0]
        is_external_bond = index_str == "-1"

        pubkey: bytes | None = None
        private_key: PrivateKey | None = None

        # Check registry for bond info (needed for both external bonds and certificates)
        registry_bond = None
        if registry is not None:
            registry_bond = registry.get_bond_by_address(utxo_info.address)

        if is_external_bond:
            # External bond: get pubkey from registry, no private key available
            if registry_bond:
                try:
                    pubkey = bytes.fromhex(registry_bond.pubkey)
                    logger.debug(
                        f"External bond {utxo_info.address[:20]}... using pubkey from registry"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to get pubkey for external bond {utxo_info.address}: {e}"
                    )
            if pubkey is None:
                logger.warning(
                    f"External bond {utxo_info.address[:20]}... not found in registry, skipping"
                )
                continue
        else:
            # Hot wallet bond: derive key from wallet
            key = wallet.get_key_for_address(utxo_info.address)
            pubkey = key.get_public_key_bytes(compressed=True) if key else None
            private_key = key.private_key if key else None

        # Get confirmation_time (Unix timestamp) from block height
        # For unconfirmed UTXOs (height=None), we can't calculate bond value yet
        if utxo_info.height is None:
            logger.warning(f"Skipping unconfirmed bond UTXO {utxo_info.txid}:{utxo_info.vout}")
            continue

        confirmation_time = await wallet.backend.get_block_time(utxo_info.height)

        bond_value = calculate_timelocked_fidelity_bond_value(
            utxo_value=utxo_info.value,
            confirmation_time=confirmation_time,
            locktime=locktime,
        )

        # Check registry for certificate information (cold wallet support)
        cert_pubkey: bytes | None = None
        cert_privkey: PrivateKey | None = None
        cert_signature: bytes | None = None
        cert_expiry: int | None = None

        if registry_bond is not None and registry_bond.has_certificate:
            try:
                cert_pubkey = bytes.fromhex(registry_bond.cert_pubkey)  # type: ignore
                cert_privkey = PrivateKey(
                    bytes.fromhex(registry_bond.cert_privkey)  # type: ignore
                )
                cert_signature = bytes.fromhex(registry_bond.cert_signature)  # type: ignore
                cert_expiry = registry_bond.cert_expiry
                logger.debug(
                    f"Found certificate for bond {utxo_info.address[:20]}... "
                    f"(expiry: {cert_expiry} periods)"
                )
            except Exception as e:
                logger.warning(f"Failed to parse certificate for {utxo_info.address}: {e}")

        bonds.append(
            FidelityBondInfo(
                txid=utxo_info.txid,
                vout=utxo_info.vout,
                value=utxo_info.value,
                locktime=locktime,
                confirmation_time=confirmation_time,
                bond_value=bond_value,
                pubkey=pubkey,
                private_key=private_key,
                cert_pubkey=cert_pubkey,
                cert_privkey=cert_privkey,
                cert_signature=cert_signature,
                cert_expiry=cert_expiry,
            )
        )

    return bonds


def _pad_signature(sig_der: bytes, target_len: int = 72) -> bytes:
    """
    Pad DER signature to fixed length for wire format.

    Uses leading 0xff padding (rjust) to match the reference implementation.
    The verifier strips padding by finding the DER header (0x30).
    """
    if len(sig_der) > target_len:
        raise ValueError(f"Signature too long: {len(sig_der)} > {target_len}")
    return sig_der.rjust(target_len, b"\xff")


def _bitcoin_message_hash(message: bytes) -> bytes:
    """
    Hash a message using Bitcoin's message signing format.

    Format: SHA256(SHA256("\\x18Bitcoin Signed Message:\\n" + varint(len) + message))

    This delegates to jmcore.crypto.bitcoin_message_hash_bytes.
    Kept as a private alias for backward compatibility with tests.
    """
    return bitcoin_message_hash_bytes(message)


def _sign_message_bitcoin(private_key: PrivateKey, message: bytes) -> bytes:
    """
    Sign a message using Bitcoin message signing format.

    Args:
        private_key: coincurve PrivateKey
        message: Raw message bytes (NOT pre-hashed)

    Returns:
        DER-encoded signature
    """
    msg_hash = _bitcoin_message_hash(message)
    return private_key.sign(msg_hash, hasher=None)


def create_fidelity_bond_proof(
    bond: FidelityBondInfo,
    maker_nick: str,
    taker_nick: str,
    current_block_height: int,
) -> str | None:
    """
    Create a fidelity bond proof for broadcasting.

    The proof structure (252 bytes total):
    - 72 bytes: Nick signature (signs "taker_nick|maker_nick" with Bitcoin message format)
    - 72 bytes: Certificate signature (signs cert message with Bitcoin message format)
    - 33 bytes: Certificate public key (ephemeral random key for hot wallet, pre-signed for cold)
    - 2 bytes: Certificate expiry (retarget period number when cert becomes invalid)
    - 33 bytes: UTXO public key (the key that can spend the bond UTXO)
    - 32 bytes: TXID (little-endian)
    - 4 bytes: Vout (little-endian)
    - 4 bytes: Locktime (little-endian)

    Nick signature message format:
        (taker_nick + '|' + maker_nick).encode('ascii')

    Certificate signature message format (binary):
        b'fidelity-bond-cert|' + cert_pub + b'|' + str(cert_expiry_encoded).encode('ascii')

    Both signatures use Bitcoin message signing format (double SHA256 with prefix).

    This function supports two modes:
    1. **Hot wallet mode**: bond.private_key is available, generates ephemeral cert keypair
       (matching the reference implementation's delegated certificate approach)
    2. **Certificate mode** (cold wallet): bond.cert_* fields are set, uses pre-signed cert

    Args:
        bond: FidelityBondInfo with UTXO details and either private key or certificate
        maker_nick: Maker's JoinMarket nick
        taker_nick: Target taker's nick (for ownership proof)
        current_block_height: Current blockchain height (for calculating cert expiry)

    Returns:
        Base64-encoded proof string, or None if signing fails
    """
    if not bond.pubkey:
        logger.error("Bond missing pubkey")
        return None

    try:
        # Determine if we're using a certificate (cold wallet) or self-signing (hot wallet)
        use_certificate = (
            bond.cert_pubkey is not None
            and bond.cert_privkey is not None
            and bond.cert_signature is not None
            and bond.cert_expiry is not None
        )

        if use_certificate:
            # COLD WALLET MODE: Use pre-signed certificate
            # These assertions are safe because use_certificate already checks they're not None
            assert bond.cert_pubkey is not None
            assert bond.cert_signature is not None
            assert bond.cert_expiry is not None
            assert bond.cert_privkey is not None

            cert_pub = bond.cert_pubkey
            cert_sig = bond.cert_signature
            cert_expiry_encoded = bond.cert_expiry
            utxo_pub = bond.pubkey

            logger.debug(
                f"Using certificate mode for bond proof (cert_expiry={cert_expiry_encoded})"
            )

            # Sign nick message with hot wallet cert_privkey
            nick_msg = (taker_nick + "|" + maker_nick).encode("ascii")
            nick_sig = _sign_message_bitcoin(bond.cert_privkey, nick_msg)
            nick_sig_padded = _pad_signature(nick_sig, 72)

            # Use pre-signed certificate signature
            cert_sig_padded = _pad_signature(cert_sig, 72)

        else:
            # HOT WALLET MODE: delegated certificate with random cert keypair
            # Matches the reference implementation (yieldgenerator.py) which always
            # generates a random cert keypair, even for hot wallets. The UTXO key
            # signs the certificate (delegation), and the random cert key signs
            # nick messages.
            if not bond.private_key:
                logger.error("Bond missing private key (required for hot wallet mode)")
                return None

            # Generate ephemeral cert keypair (reference: yieldgenerator.py line 162)
            cert_priv = PrivateKey(os.urandom(32))
            cert_pub = cert_priv.public_key.format(compressed=True)
            utxo_pub = bond.pubkey

            # Calculate certificate expiry as retarget period number
            # Reference: yieldgenerator.py line 139
            # cert_expiry =
            # ((blocks + BLOCK_COUNT_SAFETY) // RETARGET_INTERVAL) + CERT_MAX_VALIDITY_TIME
            cert_expiry_encoded = (
                (current_block_height + BLOCK_COUNT_SAFETY) // RETARGET_INTERVAL
            ) + CERT_MAX_VALIDITY_TIME

            logger.debug(
                f"Using delegated cert mode for bond proof (cert_expiry={cert_expiry_encoded})"
            )

            # 1. Nick signature: signed with the cert key (proves maker holds delegated key)
            # Signs "(taker_nick|maker_nick)" using Bitcoin message format
            nick_msg = (taker_nick + "|" + maker_nick).encode("ascii")
            nick_sig = _sign_message_bitcoin(cert_priv, nick_msg)
            nick_sig_padded = _pad_signature(nick_sig, 72)

            # 2. Certificate signature: UTXO key signs the cert (delegation to cert_pub)
            # Signs "fidelity-bond-cert|<cert_pub>|<cert_expiry_encoded>"
            cert_msg = (
                b"fidelity-bond-cert|" + cert_pub + b"|" + str(cert_expiry_encoded).encode("ascii")
            )
            cert_sig = _sign_message_bitcoin(bond.private_key, cert_msg)
            cert_sig_padded = _pad_signature(cert_sig, 72)

        # 3. Pack the proof
        # TXID in display format (big-endian, human-readable) - same as how Bitcoin Core
        # returns txids and how the reference implementation stores them.
        # Reference: wallet.py line 754 uses tx.GetTxid()[::-1] which converts from
        # internal (little-endian) to display (big-endian) format.
        txid_bytes = bytes.fromhex(bond.txid)
        if len(txid_bytes) != 32:
            raise ValueError(f"Invalid txid length: {len(txid_bytes)}")

        proof_data = struct.pack(
            "<72s72s33sH33s32sII",
            nick_sig_padded,
            cert_sig_padded,
            cert_pub,
            cert_expiry_encoded,
            utxo_pub,
            txid_bytes,
            bond.vout,
            bond.locktime,
        )

        if len(proof_data) != 252:
            raise ValueError(f"Invalid proof length: {len(proof_data)}, expected 252")

        return base64.b64encode(proof_data).decode("ascii")

    except Exception as e:
        logger.error(f"Failed to create bond proof: {e}")
        return None


async def get_best_fidelity_bond(
    wallet: WalletService, mixdepth: int = FIDELITY_BOND_MIXDEPTH
) -> FidelityBondInfo | None:
    """
    Get the best (highest value) fidelity bond from the wallet.

    Args:
        wallet: WalletService instance
        mixdepth: Mixdepth to search

    Returns:
        Best FidelityBondInfo or None if no bonds found
    """
    bonds = await find_fidelity_bonds(wallet, mixdepth)
    if not bonds:
        return None

    return max(bonds, key=lambda b: b.bond_value)
