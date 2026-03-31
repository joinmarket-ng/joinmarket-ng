"""
BIP32 HD key derivation for JoinMarket wallets.
Implements BIP84 (Native SegWit) derivation paths.
"""

from __future__ import annotations

import hashlib
import hmac

from coincurve import PrivateKey, PublicKey
from jmcore.constants import SECP256K1_N
from jmcore.crypto import base58check_encode as _base58check_encode
from jmcore.crypto import mnemonic_to_seed

# BIP32 version bytes for extended keys
# Note: For BIP84 (native segwit), we should use zpub/zprv but Bitcoin Core
# expects standard xpub format in descriptors - it infers the script type from
# the descriptor wrapper (wpkh, wsh, etc.)
XPUB_MAINNET = bytes.fromhex("0488B21E")  # xpub
XPRV_MAINNET = bytes.fromhex("0488ADE4")  # xprv
XPUB_TESTNET = bytes.fromhex("043587CF")  # tpub
XPRV_TESTNET = bytes.fromhex("04358394")  # tprv

# BIP84 version bytes for native segwit extended keys
ZPUB_MAINNET = bytes.fromhex("04B24746")  # zpub
ZPRV_MAINNET = bytes.fromhex("04B2430C")  # zprv
VPUB_TESTNET = bytes.fromhex("045F1CF6")  # vpub
VPRV_TESTNET = bytes.fromhex("045F18BC")  # vprv


class HDKey:
    """
    Hierarchical Deterministic Key for Bitcoin.
    Implements BIP32 derivation.
    """

    def __init__(
        self,
        private_key: PrivateKey,
        chain_code: bytes,
        depth: int = 0,
        parent_fingerprint: bytes = b"\x00\x00\x00\x00",
        child_number: int = 0,
    ):
        self._private_key = private_key
        self._public_key = private_key.public_key
        self.chain_code = chain_code
        self.depth = depth
        self.parent_fingerprint = parent_fingerprint
        self.child_number = child_number

    @property
    def private_key(self) -> PrivateKey:
        """Return the coincurve PrivateKey instance."""
        return self._private_key

    @property
    def public_key(self) -> PublicKey:
        """Return the coincurve PublicKey instance."""
        return self._public_key

    @property
    def fingerprint(self) -> bytes:
        """Get the fingerprint of this key (first 4 bytes of hash160 of public key)."""
        pubkey_bytes = self._public_key.format(compressed=True)
        sha256_hash = hashlib.sha256(pubkey_bytes).digest()
        ripemd160_hash = hashlib.new("ripemd160", sha256_hash).digest()
        return ripemd160_hash[:4]

    @classmethod
    def from_seed(cls, seed: bytes) -> HDKey:
        """Create master HD key from seed"""
        hmac_result = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
        key_bytes = hmac_result[:32]
        chain_code = hmac_result[32:]

        private_key = PrivateKey(key_bytes)

        return cls(private_key, chain_code, depth=0)

    def derive(self, path: str) -> HDKey:
        """
        Derive child key from path notation (e.g., "m/84'/0'/0'/0/0")
        ' indicates hardened derivation
        """
        if not path.startswith("m"):
            raise ValueError("Path must start with 'm'")

        parts = path.split("/")[1:]
        key = self

        for part in parts:
            if not part:
                continue

            hardened = part.endswith("'") or part.endswith("h")
            index_str = part.rstrip("'h")
            index = int(index_str)

            if hardened:
                index += 0x80000000

            key = key._derive_child(index)

        return key

    def _derive_child(self, index: int) -> HDKey:
        """Derive a child key at the given index"""
        hardened = index >= 0x80000000

        if hardened:
            priv_bytes = self._private_key.secret
            data = b"\x00" + priv_bytes + index.to_bytes(4, "big")
        else:
            pub_bytes = self._public_key.format(compressed=True)
            data = pub_bytes + index.to_bytes(4, "big")

        hmac_result = hmac.new(self.chain_code, data, hashlib.sha512).digest()
        key_offset = hmac_result[:32]
        child_chain = hmac_result[32:]

        parent_key_int = int.from_bytes(self._private_key.secret, "big")
        offset_int = int.from_bytes(key_offset, "big")

        child_key_int = (parent_key_int + offset_int) % SECP256K1_N

        if child_key_int == 0:
            raise ValueError("Invalid child key")

        child_key_bytes = child_key_int.to_bytes(32, "big")
        child_private_key = PrivateKey(child_key_bytes)

        return HDKey(
            child_private_key,
            child_chain,
            depth=self.depth + 1,
            parent_fingerprint=self.fingerprint,
            child_number=index,
        )

    def get_private_key_bytes(self) -> bytes:
        """Get private key as 32 bytes"""
        return self._private_key.secret

    def get_public_key_bytes(self, compressed: bool = True) -> bytes:
        """Get public key bytes"""
        return self._public_key.format(compressed=compressed)

    def get_address(self, network: str = "mainnet") -> str:
        """Get P2WPKH (Native SegWit) address for this key"""
        from jmwallet.wallet.address import pubkey_to_p2wpkh_address

        pubkey_hex = self.get_public_key_bytes(compressed=True).hex()
        return pubkey_to_p2wpkh_address(pubkey_hex, network)

    def get_p2tr_address(self, network: str = "mainnet") -> str:
        """Get P2TR (Taproot) address for this key"""
        from jmcore.bitcoin import pubkey_to_p2tr_address, taproot_tweak_pubkey

        pubkey_bytes = self.get_public_key_bytes(compressed=True)
        x_only_pubkey = pubkey_bytes[1:]

        _, tweaked_pubkey = taproot_tweak_pubkey(x_only_pubkey)

        return pubkey_to_p2tr_address(tweaked_pubkey, network)

    def sign(self, message: bytes) -> bytes:
        """Sign a message with this key (uses SHA256 hashing)."""
        return self._private_key.sign(message)

    def get_xpub(self, network: str = "mainnet") -> str:
        """
        Serialize the public key as an extended public key (xpub/tpub).

        This produces a standard BIP32 xpub that can be used in Bitcoin Core
        descriptors. The descriptor wrapper (wpkh, wsh, etc.) determines the
        actual address type.

        Args:
            network: "mainnet" for xpub, "testnet"/"regtest" for tpub

        Returns:
            Base58Check-encoded extended public key (xpub or tpub)
        """
        if network == "mainnet":
            version = XPUB_MAINNET
        else:
            version = XPUB_TESTNET

        # BIP32 serialization format:
        # 4 bytes: version
        # 1 byte: depth
        # 4 bytes: parent fingerprint
        # 4 bytes: child number
        # 32 bytes: chain code
        # 33 bytes: public key (compressed)
        depth_byte = min(self.depth, 255).to_bytes(1, "big")
        child_num_bytes = self.child_number.to_bytes(4, "big")
        pubkey_bytes = self._public_key.format(compressed=True)

        payload = (
            version
            + depth_byte
            + self.parent_fingerprint
            + child_num_bytes
            + self.chain_code
            + pubkey_bytes
        )

        return _base58check_encode(payload)

    def get_zpub(self, network: str = "mainnet") -> str:
        """
        Serialize the public key as a BIP84 extended public key (zpub/vpub).

        This produces a BIP84-compliant extended public key for native segwit wallets.
        zpub/vpub explicitly indicates the key is intended for P2WPKH addresses.

        Args:
            network: "mainnet" for zpub, "testnet"/"regtest" for vpub

        Returns:
            Base58Check-encoded extended public key (zpub or vpub)
        """
        if network == "mainnet":
            version = ZPUB_MAINNET
        else:
            version = VPUB_TESTNET

        # Same serialization format as xpub but with BIP84 version bytes
        depth_byte = min(self.depth, 255).to_bytes(1, "big")
        child_num_bytes = self.child_number.to_bytes(4, "big")
        pubkey_bytes = self._public_key.format(compressed=True)

        payload = (
            version
            + depth_byte
            + self.parent_fingerprint
            + child_num_bytes
            + self.chain_code
            + pubkey_bytes
        )

        return _base58check_encode(payload)

    def get_xprv(self, network: str = "mainnet") -> str:
        """
        Serialize the private key as an extended private key (xprv/tprv).

        Args:
            network: "mainnet" for xprv, "testnet"/"regtest" for tprv

        Returns:
            Base58Check-encoded extended private key
        """
        if network == "mainnet":
            version = XPRV_MAINNET
        else:
            version = XPRV_TESTNET

        depth_byte = min(self.depth, 255).to_bytes(1, "big")
        child_num_bytes = self.child_number.to_bytes(4, "big")
        # Private key is prefixed with 0x00 to make it 33 bytes
        privkey_bytes = b"\x00" + self._private_key.secret

        payload = (
            version
            + depth_byte
            + self.parent_fingerprint
            + child_num_bytes
            + self.chain_code
            + privkey_bytes
        )

        return _base58check_encode(payload)


# Re-export mnemonic_to_seed for backward compatibility - the canonical
# implementation now lives in jmcore.crypto
__all__ = ["HDKey", "mnemonic_to_seed"]
