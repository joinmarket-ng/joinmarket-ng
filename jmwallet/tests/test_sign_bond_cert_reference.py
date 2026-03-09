"""Tests for the standalone fidelity bond certificate signing script.

This test file verifies the certificate signing logic used for migration from the
reference JoinMarket implementation.  Tests verify:
- Timenumber calculation
- Bitcoin message hashing
- BIP32 derivation path construction
- Certificate signing in Electrum recoverable format
- End-to-end: signature can be verified by ``_verify_recoverable_signature``
- CLI argument parsing and output format

The script under test is self-contained (only depends on coincurve).
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from coincurve import PrivateKey

# Add scripts directory to path for importing the signing script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from sign_bond_cert_reference import (
    _bitcoin_message_hash,
    _derive_key_from_mnemonic,
    _make_bond_path,
    _path_to_string,
    locktime_to_timenumber,
    sign_certificate,
)

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

# Well-known BIP39 test mnemonic (from BIP39 spec)
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)

# Cert pubkey and expiry used in tests (arbitrary but valid)
TEST_CERT_PRIVKEY = PrivateKey(b"\x01" * 32)
TEST_CERT_PUBKEY_HEX = TEST_CERT_PRIVKEY.public_key.format(compressed=True).hex()
TEST_CERT_EXPIRY = 518


class TestTimenumber:
    """Timenumber calculation tests (subset -- full tests in test_derive_bond_pubkey)."""

    def test_jan_2020(self) -> None:
        assert locktime_to_timenumber(2020, 1) == 0

    def test_feb_2026(self) -> None:
        assert locktime_to_timenumber(2026, 2) == 73

    def test_dec_2099(self) -> None:
        assert locktime_to_timenumber(2099, 12) == 959

    def test_out_of_range_raises(self) -> None:
        with pytest.raises(ValueError, match="outside the valid range"):
            locktime_to_timenumber(2100, 1)

    def test_invalid_month_raises(self) -> None:
        with pytest.raises(ValueError, match="Month must be 1-12"):
            locktime_to_timenumber(2026, 13)


class TestBitcoinMessageHash:
    """Bitcoin message hash tests."""

    def test_empty_message(self) -> None:
        h = _bitcoin_message_hash("")
        assert len(h) == 32

    def test_matches_jmcore(self) -> None:
        """Our inline hash must match jmcore's implementation."""
        from jmcore.crypto import bitcoin_message_hash

        msg = "fidelity-bond-cert|03abcdef1234567890|518"
        assert _bitcoin_message_hash(msg) == bitcoin_message_hash(msg)

    def test_cert_message_matches_bytes_variant(self) -> None:
        """String hash must match bytes hash for ASCII messages."""
        from jmcore.crypto import bitcoin_message_hash_bytes

        msg = f"fidelity-bond-cert|{TEST_CERT_PUBKEY_HEX}|{TEST_CERT_EXPIRY}"
        assert _bitcoin_message_hash(msg) == bitcoin_message_hash_bytes(msg.encode("utf-8"))


class TestBondPath:
    """BIP32 path construction tests."""

    def test_path_indices(self) -> None:
        indices = _make_bond_path(73)
        assert indices == [
            84 + 0x80000000,
            0 + 0x80000000,
            0 + 0x80000000,
            2,
            73,
        ]

    def test_path_string(self) -> None:
        indices = _make_bond_path(73)
        assert _path_to_string(indices) == "m/84'/0'/0'/2/73"

    def test_timenumber_0(self) -> None:
        indices = _make_bond_path(0)
        assert _path_to_string(indices) == "m/84'/0'/0'/2/0"

    def test_timenumber_959(self) -> None:
        indices = _make_bond_path(959)
        assert _path_to_string(indices) == "m/84'/0'/0'/2/959"


class TestKeyDerivation:
    """BIP32 key derivation from mnemonic."""

    def test_deterministic(self) -> None:
        """Same mnemonic + path must produce the same key."""
        path = _make_bond_path(73)
        priv1, pub1 = _derive_key_from_mnemonic(TEST_MNEMONIC, path)
        priv2, pub2 = _derive_key_from_mnemonic(TEST_MNEMONIC, path)
        assert priv1 == priv2
        assert pub1 == pub2

    def test_different_timenumber_different_key(self) -> None:
        """Different timenumbers must produce different keys."""
        path_73 = _make_bond_path(73)
        path_74 = _make_bond_path(74)
        _, pub73 = _derive_key_from_mnemonic(TEST_MNEMONIC, path_73)
        _, pub74 = _derive_key_from_mnemonic(TEST_MNEMONIC, path_74)
        assert pub73 != pub74

    def test_passphrase_changes_key(self) -> None:
        """BIP39 passphrase must produce a different key."""
        path = _make_bond_path(73)
        _, pub_no_pass = _derive_key_from_mnemonic(TEST_MNEMONIC, path)
        _, pub_with_pass = _derive_key_from_mnemonic(TEST_MNEMONIC, path, "mypassphrase")
        assert pub_no_pass != pub_with_pass

    def test_pubkey_is_compressed(self) -> None:
        """Derived pubkey must be 33 bytes and start with 02 or 03."""
        path = _make_bond_path(73)
        _, pub = _derive_key_from_mnemonic(TEST_MNEMONIC, path)
        assert len(pub) == 33
        assert pub[0] in (0x02, 0x03)

    def test_privkey_is_32_bytes(self) -> None:
        path = _make_bond_path(73)
        priv, _ = _derive_key_from_mnemonic(TEST_MNEMONIC, path)
        assert len(priv) == 32

    def test_privkey_corresponds_to_pubkey(self) -> None:
        """Private key must generate the returned public key."""
        path = _make_bond_path(73)
        priv, pub = _derive_key_from_mnemonic(TEST_MNEMONIC, path)
        assert PrivateKey(priv).public_key.format(compressed=True) == pub

    def test_matches_sign_bond_mnemonic(self) -> None:
        """Our derivation must match sign_bond_mnemonic.py's derivation."""
        from sign_bond_mnemonic import derive_key_from_mnemonic

        path = _make_bond_path(73)
        our_priv, our_pub = _derive_key_from_mnemonic(TEST_MNEMONIC, path)
        ref_priv, ref_pub = derive_key_from_mnemonic(TEST_MNEMONIC, path)
        assert our_priv == ref_priv
        assert our_pub == ref_pub


class TestSignCertificate:
    """Certificate signing tests."""

    def test_returns_base64(self) -> None:
        """Signature must be valid base64."""
        priv, _ = _derive_key_from_mnemonic(TEST_MNEMONIC, _make_bond_path(73))
        sig_b64 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        sig_bytes = base64.b64decode(sig_b64)
        assert len(sig_bytes) == 65

    def test_electrum_header_byte(self) -> None:
        """Header byte must be in compressed P2PKH range (31-34)."""
        priv, _ = _derive_key_from_mnemonic(TEST_MNEMONIC, _make_bond_path(73))
        sig_b64 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        sig_bytes = base64.b64decode(sig_b64)
        header = sig_bytes[0]
        assert 31 <= header <= 34, f"Header byte {header} not in range 31-34"

    def test_deterministic(self) -> None:
        """Same inputs must produce the same signature."""
        priv, _ = _derive_key_from_mnemonic(TEST_MNEMONIC, _make_bond_path(73))
        sig1 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        sig2 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        assert sig1 == sig2

    def test_different_expiry_different_sig(self) -> None:
        """Different cert expiry must produce different signatures."""
        priv, _ = _derive_key_from_mnemonic(TEST_MNEMONIC, _make_bond_path(73))
        sig1 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, 518)
        sig2 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, 519)
        assert sig1 != sig2

    def test_different_cert_pubkey_different_sig(self) -> None:
        """Different cert pubkey must produce different signatures."""
        priv, _ = _derive_key_from_mnemonic(TEST_MNEMONIC, _make_bond_path(73))
        other_pubkey = PrivateKey(b"\x02" * 32).public_key.format(compressed=True).hex()
        sig1 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        sig2 = sign_certificate(priv, other_pubkey, TEST_CERT_EXPIRY)
        assert sig1 != sig2


class TestVerifyWithImportCertificate:
    """End-to-end: signature must be accepted by import-certificate's verifier."""

    def test_verify_recoverable(self) -> None:
        """Signature from sign_certificate must be verified by _verify_recoverable_signature."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        # Derive the bond key
        path = _make_bond_path(73)
        priv, bond_pubkey = _derive_key_from_mnemonic(TEST_MNEMONIC, path)

        # Sign
        sig_b64 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        sig_bytes = base64.b64decode(sig_b64)

        # Verify
        assert _verify_recoverable_signature(
            sig_bytes, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY, bond_pubkey
        )

    def test_wrong_bond_pubkey_fails(self) -> None:
        """Verification must fail if the expected pubkey doesn't match."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        path = _make_bond_path(73)
        priv, _bond_pubkey = _derive_key_from_mnemonic(TEST_MNEMONIC, path)

        sig_b64 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        sig_bytes = base64.b64decode(sig_b64)

        # Use a different pubkey as "expected"
        wrong_pubkey = PrivateKey(b"\x05" * 32).public_key.format(compressed=True)
        assert not _verify_recoverable_signature(
            sig_bytes, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY, wrong_pubkey
        )

    def test_wrong_cert_expiry_fails(self) -> None:
        """Verification must fail if the cert expiry doesn't match."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        path = _make_bond_path(73)
        priv, bond_pubkey = _derive_key_from_mnemonic(TEST_MNEMONIC, path)

        sig_b64 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        sig_bytes = base64.b64decode(sig_b64)

        # Verify with wrong expiry
        assert not _verify_recoverable_signature(sig_bytes, TEST_CERT_PUBKEY_HEX, 999, bond_pubkey)

    def test_multiple_timenumbers(self) -> None:
        """Signatures for different timenumbers must all verify independently."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        for tn in [0, 1, 73, 144, 500, 959]:
            path = _make_bond_path(tn)
            priv, bond_pubkey = _derive_key_from_mnemonic(TEST_MNEMONIC, path)
            sig_b64 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
            sig_bytes = base64.b64decode(sig_b64)
            assert _verify_recoverable_signature(
                sig_bytes, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY, bond_pubkey
            ), f"Verification failed for timenumber {tn}"

    def test_with_passphrase(self) -> None:
        """Signature from passphrase-derived key must verify with the matching pubkey."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        path = _make_bond_path(73)
        priv, bond_pubkey = _derive_key_from_mnemonic(TEST_MNEMONIC, path, "bond-passphrase")
        sig_b64 = sign_certificate(priv, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY)
        sig_bytes = base64.b64decode(sig_b64)
        assert _verify_recoverable_signature(
            sig_bytes, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY, bond_pubkey
        )


class TestMainCLI:
    """CLI entry point tests."""

    def _run_main(
        self,
        args: list[str],
        mnemonic: str = TEST_MNEMONIC,
        passphrase: str = "",
    ) -> tuple[str, str]:
        """Run main() with mocked stdin and capture output."""
        from sign_bond_cert_reference import main

        sys.argv = ["sign_bond_cert_reference.py"] + args

        inputs = iter([mnemonic] + ([passphrase] if "--passphrase" in args else []))

        with patch("sign_bond_cert_reference.getpass.getpass", side_effect=inputs):
            import io

            old_stdout = sys.stdout
            old_stderr = sys.stderr
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            try:
                main()
                stdout = sys.stdout.getvalue()
                stderr = sys.stderr.getvalue()
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr

        return stdout, stderr

    def test_basic_invocation(self) -> None:
        """Basic invocation should produce a base64 signature on stdout."""
        stdout, stderr = self._run_main(
            [
                "--locktime",
                "2026-02",
                "--cert-pubkey",
                TEST_CERT_PUBKEY_HEX,
                "--cert-expiry",
                str(TEST_CERT_EXPIRY),
            ]
        )
        sig_b64 = stdout.strip()
        sig_bytes = base64.b64decode(sig_b64)
        assert len(sig_bytes) == 65

    def test_output_shows_path(self) -> None:
        """Stderr should show the derivation path."""
        _, stderr = self._run_main(
            [
                "--locktime",
                "2026-02",
                "--cert-pubkey",
                TEST_CERT_PUBKEY_HEX,
                "--cert-expiry",
                str(TEST_CERT_EXPIRY),
            ]
        )
        assert "m/84'/0'/0'/2/73" in stderr

    def test_output_shows_import_command(self) -> None:
        """Stderr should show the import-certificate command."""
        _, stderr = self._run_main(
            [
                "--locktime",
                "2026-02",
                "--cert-pubkey",
                TEST_CERT_PUBKEY_HEX,
                "--cert-expiry",
                str(TEST_CERT_EXPIRY),
            ]
        )
        assert "jm-wallet import-certificate" in stderr
        assert "--cert-signature" in stderr
        assert f"--cert-expiry {TEST_CERT_EXPIRY}" in stderr

    def test_output_shows_timenumber(self) -> None:
        """Stderr should show the timenumber."""
        _, stderr = self._run_main(
            [
                "--locktime",
                "2026-02",
                "--cert-pubkey",
                TEST_CERT_PUBKEY_HEX,
                "--cert-expiry",
                str(TEST_CERT_EXPIRY),
            ]
        )
        assert "Timenumber:      73" in stderr

    def test_output_shows_bond_pubkey(self) -> None:
        """Stderr should show the derived bond pubkey."""
        _, stderr = self._run_main(
            [
                "--locktime",
                "2026-02",
                "--cert-pubkey",
                TEST_CERT_PUBKEY_HEX,
                "--cert-expiry",
                str(TEST_CERT_EXPIRY),
            ]
        )
        assert "Bond pubkey:" in stderr

    def test_with_passphrase_flag(self) -> None:
        """--passphrase should prompt and produce a different signature."""
        stdout_no_pass, _ = self._run_main(
            [
                "--locktime",
                "2026-02",
                "--cert-pubkey",
                TEST_CERT_PUBKEY_HEX,
                "--cert-expiry",
                str(TEST_CERT_EXPIRY),
            ]
        )
        stdout_with_pass, _ = self._run_main(
            [
                "--locktime",
                "2026-02",
                "--cert-pubkey",
                TEST_CERT_PUBKEY_HEX,
                "--cert-expiry",
                str(TEST_CERT_EXPIRY),
                "--passphrase",
            ],
            passphrase="bond-pass",
        )
        assert stdout_no_pass.strip() != stdout_with_pass.strip()

    def test_invalid_locktime_exits(self) -> None:
        """Invalid locktime should exit with error."""
        from sign_bond_cert_reference import main

        sys.argv = [
            "sign_bond_cert_reference.py",
            "--locktime",
            "2026",
            "--cert-pubkey",
            TEST_CERT_PUBKEY_HEX,
            "--cert-expiry",
            str(TEST_CERT_EXPIRY),
        ]
        with pytest.raises(SystemExit):
            main()

    def test_invalid_pubkey_exits(self) -> None:
        """Invalid cert pubkey should exit with error."""
        from sign_bond_cert_reference import main

        sys.argv = [
            "sign_bond_cert_reference.py",
            "--locktime",
            "2026-02",
            "--cert-pubkey",
            "not_a_valid_hex",
            "--cert-expiry",
            str(TEST_CERT_EXPIRY),
        ]
        with pytest.raises(SystemExit):
            main()

    def test_short_pubkey_exits(self) -> None:
        """Too-short cert pubkey should exit with error."""
        from sign_bond_cert_reference import main

        sys.argv = [
            "sign_bond_cert_reference.py",
            "--locktime",
            "2026-02",
            "--cert-pubkey",
            "03abcd",
            "--cert-expiry",
            str(TEST_CERT_EXPIRY),
        ]
        with pytest.raises(SystemExit):
            main()

    def test_signature_verifies_end_to_end(self) -> None:
        """The full CLI output signature must verify with import-certificate's verifier."""
        from jmwallet.cli.cold_wallet import _verify_recoverable_signature

        stdout, _ = self._run_main(
            [
                "--locktime",
                "2026-02",
                "--cert-pubkey",
                TEST_CERT_PUBKEY_HEX,
                "--cert-expiry",
                str(TEST_CERT_EXPIRY),
            ]
        )
        sig_b64 = stdout.strip()
        sig_bytes = base64.b64decode(sig_b64)

        # Derive the same bond pubkey
        path = _make_bond_path(73)
        _, bond_pubkey = _derive_key_from_mnemonic(TEST_MNEMONIC, path)

        assert _verify_recoverable_signature(
            sig_bytes, TEST_CERT_PUBKEY_HEX, TEST_CERT_EXPIRY, bond_pubkey
        )
