"""Tests for cold wallet fidelity bond certificate functionality."""

from __future__ import annotations

import time
from pathlib import Path

from coincurve import PrivateKey

from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    load_registry,
    save_registry,
)


class TestCertificateFields:
    """Tests for certificate fields in FidelityBondInfo."""

    def test_has_certificate_true(self) -> None:
        """Bond with all certificate fields should have certificate."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "11" * 32,
            cert_privkey="aa" * 32,
            cert_signature="bb" * 70,
            cert_expiry=52,
        )
        assert bond.has_certificate is True

    def test_has_certificate_false_missing_pubkey(self) -> None:
        """Bond missing cert_pubkey should not have certificate."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_privkey="aa" * 32,
            cert_signature="bb" * 70,
            cert_expiry=52,
        )
        assert bond.has_certificate is False

    def test_has_certificate_false_missing_privkey(self) -> None:
        """Bond missing cert_privkey should not have certificate."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "11" * 32,
            cert_signature="bb" * 70,
            cert_expiry=52,
        )
        assert bond.has_certificate is False

    def test_has_certificate_false_missing_signature(self) -> None:
        """Bond missing cert_signature should not have certificate."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "11" * 32,
            cert_privkey="aa" * 32,
            cert_expiry=52,
        )
        assert bond.has_certificate is False

    def test_has_certificate_false_missing_expiry(self) -> None:
        """Bond missing cert_expiry should not have certificate."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "11" * 32,
            cert_privkey="aa" * 32,
            cert_signature="bb" * 70,
        )
        assert bond.has_certificate is False

    def test_has_certificate_false_all_none(self) -> None:
        """Bond without any certificate fields should not have certificate."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )
        assert bond.has_certificate is False


class TestCertificateExpiry:
    """Tests for certificate expiry checking."""

    def test_is_certificate_expired_true_past_height(self) -> None:
        """Certificate should be expired if current block height is past expiry."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "11" * 32,
            cert_privkey="aa" * 32,
            cert_signature="bb" * 70,
            cert_expiry=100,  # Expires at block 100 * 2016 = 201600
        )
        # Current block height is past expiry
        assert bond.is_certificate_expired(300000) is True

    def test_is_certificate_expired_false_before_height(self) -> None:
        """Certificate should not be expired if current block height is before expiry."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "11" * 32,
            cert_privkey="aa" * 32,
            cert_signature="bb" * 70,
            cert_expiry=500,  # Expires at block 500 * 2016 = 1008000
        )
        # Current block height is before expiry
        assert bond.is_certificate_expired(800000) is False

    def test_is_certificate_expired_true_at_exact_height(self) -> None:
        """Certificate should be expired at exact expiry height."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "11" * 32,
            cert_privkey="aa" * 32,
            cert_signature="bb" * 70,
            cert_expiry=100,  # Expires at block 100 * 2016 = 201600
        )
        # Exactly at expiry height
        assert bond.is_certificate_expired(201600) is True

    def test_is_certificate_expired_no_certificate(self) -> None:
        """Bond without certificate should always report as expired."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )
        # No certificate = always expired
        assert bond.is_certificate_expired(100000) is True


class TestCertificatePersistence:
    """Tests for certificate persistence in bond registry."""

    def test_save_and_load_with_certificate(self, tmp_path: Path) -> None:
        """Test saving and loading a bond with certificate."""
        registry = BondRegistry()

        # Create bond with certificate
        bond = FidelityBondInfo(
            address="bc1qcert",
            locktime=1735689600,
            locktime_human="2025-01-01 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="abcd" * 10,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid="certifiedtx" + "00" * 21,
            vout=0,
            value=10000000,
            confirmations=100,
            cert_pubkey="03" + "ff" * 32,
            cert_privkey="ee" * 32,
            cert_signature="dd" * 70,
            cert_expiry=52,
        )
        registry.add_bond(bond)

        # Save and reload
        save_registry(registry, tmp_path)
        loaded = load_registry(tmp_path)

        assert len(loaded.bonds) == 1
        loaded_bond = loaded.bonds[0]
        assert loaded_bond.address == "bc1qcert"
        assert loaded_bond.has_certificate is True
        assert loaded_bond.cert_pubkey == "03" + "ff" * 32
        assert loaded_bond.cert_privkey == "ee" * 32
        assert loaded_bond.cert_signature == "dd" * 70
        assert loaded_bond.cert_expiry == 52

    def test_save_and_load_without_certificate(self, tmp_path: Path) -> None:
        """Test saving and loading a bond without certificate (backward compat)."""
        registry = BondRegistry()

        # Create bond without certificate
        bond = FidelityBondInfo(
            address="bc1qnocert",
            locktime=1735689600,
            locktime_human="2025-01-01 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="abcd" * 10,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid="normalftx" + "00" * 22,
            vout=0,
            value=5000000,
        )
        registry.add_bond(bond)

        # Save and reload
        save_registry(registry, tmp_path)
        loaded = load_registry(tmp_path)

        assert len(loaded.bonds) == 1
        loaded_bond = loaded.bonds[0]
        assert loaded_bond.address == "bc1qnocert"
        assert loaded_bond.has_certificate is False
        assert loaded_bond.cert_pubkey is None
        assert loaded_bond.cert_privkey is None
        assert loaded_bond.cert_signature is None
        assert loaded_bond.cert_expiry is None

    def test_mixed_bonds_with_and_without_certificates(self, tmp_path: Path) -> None:
        """Test registry with mixed bonds (some with certificates, some without)."""
        registry = BondRegistry()

        # Bond with certificate
        bond_with_cert = FidelityBondInfo(
            address="bc1qwithcert",
            locktime=1735689600,
            locktime_human="2025-01-01 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "aa" * 32,
            witness_script_hex="abcd" * 10,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "bb" * 32,
            cert_privkey="cc" * 32,
            cert_signature="dd" * 70,
            cert_expiry=52,
        )

        # Bond without certificate
        bond_without_cert = FidelityBondInfo(
            address="bc1qwithoutcert",
            locktime=1735689600,
            locktime_human="2025-01-01 00:00:00",
            index=1,
            path="m/84'/0'/0'/2/1",
            pubkey="02" + "11" * 32,
            witness_script_hex="efgh" * 10,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )

        registry.add_bond(bond_with_cert)
        registry.add_bond(bond_without_cert)

        # Save and reload
        save_registry(registry, tmp_path)
        loaded = load_registry(tmp_path)

        assert len(loaded.bonds) == 2

        cert_bond = loaded.get_bond_by_address("bc1qwithcert")
        assert cert_bond is not None
        assert cert_bond.has_certificate is True

        no_cert_bond = loaded.get_bond_by_address("bc1qwithoutcert")
        assert no_cert_bond is not None
        assert no_cert_bond.has_certificate is False


class TestBitcoinMessageHashBytes:
    """Tests for bitcoin_message_hash_bytes function."""

    def test_bitcoin_message_hash_bytes_basic(self) -> None:
        """Test basic message hashing."""
        from jmcore.crypto import bitcoin_message_hash_bytes

        # Simple test message
        message = b"test message"
        result = bitcoin_message_hash_bytes(message)

        # Should return 32 bytes (SHA256 output)
        assert len(result) == 32
        assert isinstance(result, bytes)

    def test_bitcoin_message_hash_bytes_consistent(self) -> None:
        """Test that hashing is consistent."""
        from jmcore.crypto import bitcoin_message_hash_bytes

        message = b"consistent test"
        result1 = bitcoin_message_hash_bytes(message)
        result2 = bitcoin_message_hash_bytes(message)

        assert result1 == result2

    def test_bitcoin_message_hash_bytes_different_messages(self) -> None:
        """Test that different messages produce different hashes."""
        from jmcore.crypto import bitcoin_message_hash_bytes

        message1 = b"message one"
        message2 = b"message two"

        result1 = bitcoin_message_hash_bytes(message1)
        result2 = bitcoin_message_hash_bytes(message2)

        assert result1 != result2

    def test_bitcoin_message_hash_bytes_certificate_format(self) -> None:
        """Test hashing a certificate message format."""
        from jmcore.crypto import bitcoin_message_hash_bytes

        # Simulate certificate message format
        cert_pubkey = bytes.fromhex("03" + "ff" * 32)
        cert_expiry = 52
        cert_msg = b"fidelity-bond-cert|" + cert_pubkey + b"|" + str(cert_expiry).encode("ascii")

        result = bitcoin_message_hash_bytes(cert_msg)
        assert len(result) == 32


class TestCertificateSignatureVerification:
    """Tests for certificate signature creation and verification."""

    def test_create_and_verify_certificate_signature(self) -> None:
        """Test creating and verifying a certificate signature."""
        from jmcore.crypto import bitcoin_message_hash_bytes, verify_raw_ecdsa

        # Generate UTXO keypair (cold wallet)
        utxo_privkey = PrivateKey()
        utxo_pubkey = utxo_privkey.public_key.format(compressed=True)

        # Generate certificate keypair (hot wallet)
        cert_privkey = PrivateKey()
        cert_pubkey = cert_privkey.public_key.format(compressed=True)

        # Create certificate message
        cert_expiry = 52
        cert_msg = b"fidelity-bond-cert|" + cert_pubkey + b"|" + str(cert_expiry).encode("ascii")

        # Sign with UTXO key (cold wallet)
        msg_hash = bitcoin_message_hash_bytes(cert_msg)
        signature = utxo_privkey.sign(msg_hash, hasher=None)

        # Verify with UTXO pubkey
        is_valid = verify_raw_ecdsa(msg_hash, signature, utxo_pubkey)
        assert is_valid is True

    def test_certificate_signature_wrong_key_fails(self) -> None:
        """Test that verification fails with wrong key."""
        from jmcore.crypto import bitcoin_message_hash_bytes, verify_raw_ecdsa

        # Generate UTXO keypair
        utxo_privkey = PrivateKey()

        # Generate certificate keypair
        cert_privkey = PrivateKey()
        cert_pubkey = cert_privkey.public_key.format(compressed=True)

        # Generate wrong key for verification
        wrong_privkey = PrivateKey()
        wrong_pubkey = wrong_privkey.public_key.format(compressed=True)

        # Create certificate message
        cert_msg = b"fidelity-bond-cert|" + cert_pubkey + b"|52"

        # Sign with UTXO key
        msg_hash = bitcoin_message_hash_bytes(cert_msg)
        signature = utxo_privkey.sign(msg_hash, hasher=None)

        # Verify with wrong pubkey should fail
        is_valid = verify_raw_ecdsa(msg_hash, signature, wrong_pubkey)
        assert is_valid is False


class TestRecoverableSignatureVerification:
    """Tests for _verify_recoverable_signature with various header byte formats."""

    @staticmethod
    def _make_recoverable_sig(
        privkey: PrivateKey,
        message: bytes,
        header_offset: int,
    ) -> bytes:
        """Create a recoverable signature with a specific header byte offset.

        Args:
            privkey: Key to sign with.
            message: Raw message bytes (will be hashed with Bitcoin message format).
            header_offset: Base header value (27=uncompressed, 31=compressed,
                35=P2SH-P2WPKH, 39=P2WPKH).

        Returns:
            65-byte signature: header(1) + R(32) + S(32)
        """
        from jmcore.crypto import bitcoin_message_hash_bytes

        msg_hash = bitcoin_message_hash_bytes(message)
        # coincurve recoverable sign returns 65 bytes: R(32) + S(32) + recid(1)
        sig = privkey.sign_recoverable(msg_hash, hasher=None)
        r = sig[0:32]
        s = sig[32:64]
        recovery_id = sig[64]
        header = header_offset + recovery_id
        return bytes([header]) + r + s

    def test_compressed_p2pkh_header(self) -> None:
        """Header bytes 31-34 (compressed P2PKH) should verify."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        utxo_privkey = PrivateKey()
        utxo_pubkey = utxo_privkey.public_key.format(compressed=True)
        cert_privkey = PrivateKey()
        cert_pubkey_hex = cert_privkey.public_key.format(compressed=True).hex()
        cert_expiry = 518

        msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
        sig = self._make_recoverable_sig(utxo_privkey, msg, header_offset=31)

        assert _verify_recoverable_signature(sig, cert_pubkey_hex, cert_expiry, utxo_pubkey)

    def test_p2wpkh_header(self) -> None:
        """Header bytes 39-42 (native segwit P2WPKH) should verify.

        This is the format Trezor and Sparrow produce when signing with a bc1q address.
        """
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        utxo_privkey = PrivateKey()
        utxo_pubkey = utxo_privkey.public_key.format(compressed=True)
        cert_privkey = PrivateKey()
        cert_pubkey_hex = cert_privkey.public_key.format(compressed=True).hex()
        cert_expiry = 518

        msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
        sig = self._make_recoverable_sig(utxo_privkey, msg, header_offset=39)

        assert _verify_recoverable_signature(sig, cert_pubkey_hex, cert_expiry, utxo_pubkey)

    def test_p2sh_p2wpkh_header(self) -> None:
        """Header bytes 35-38 (nested segwit P2SH-P2WPKH) should verify."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        utxo_privkey = PrivateKey()
        utxo_pubkey = utxo_privkey.public_key.format(compressed=True)
        cert_privkey = PrivateKey()
        cert_pubkey_hex = cert_privkey.public_key.format(compressed=True).hex()
        cert_expiry = 518

        msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
        sig = self._make_recoverable_sig(utxo_privkey, msg, header_offset=35)

        assert _verify_recoverable_signature(sig, cert_pubkey_hex, cert_expiry, utxo_pubkey)

    def test_uncompressed_header(self) -> None:
        """Header bytes 27-30 (uncompressed P2PKH) should verify."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        utxo_privkey = PrivateKey()
        utxo_pubkey = utxo_privkey.public_key.format(compressed=False)
        cert_privkey = PrivateKey()
        cert_pubkey_hex = cert_privkey.public_key.format(compressed=True).hex()
        cert_expiry = 518

        msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
        sig = self._make_recoverable_sig(utxo_privkey, msg, header_offset=27)

        assert _verify_recoverable_signature(sig, cert_pubkey_hex, cert_expiry, utxo_pubkey)

    def test_invalid_header_byte_rejected(self) -> None:
        """Header bytes outside 27-42 should be rejected."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        utxo_privkey = PrivateKey()
        utxo_pubkey = utxo_privkey.public_key.format(compressed=True)
        cert_privkey = PrivateKey()
        cert_pubkey_hex = cert_privkey.public_key.format(compressed=True).hex()
        cert_expiry = 518

        msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
        sig = self._make_recoverable_sig(utxo_privkey, msg, header_offset=31)

        # Replace header with invalid byte (outside all ranges)
        bad_sig = bytes([43]) + sig[1:]
        assert not _verify_recoverable_signature(bad_sig, cert_pubkey_hex, cert_expiry, utxo_pubkey)

    def test_wrong_key_fails(self) -> None:
        """Signature from wrong key should fail verification."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        utxo_privkey = PrivateKey()
        utxo_pubkey = utxo_privkey.public_key.format(compressed=True)
        wrong_privkey = PrivateKey()
        cert_privkey = PrivateKey()
        cert_pubkey_hex = cert_privkey.public_key.format(compressed=True).hex()
        cert_expiry = 518

        msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
        # Sign with wrong key
        sig = self._make_recoverable_sig(wrong_privkey, msg, header_offset=39)

        assert not _verify_recoverable_signature(sig, cert_pubkey_hex, cert_expiry, utxo_pubkey)
