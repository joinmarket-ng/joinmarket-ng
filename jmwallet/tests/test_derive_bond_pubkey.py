"""Tests for the standalone fidelity bond pubkey derivation script.

This test file verifies the BIP32 derivation logic used to extract fidelity bond
public keys from the reference JoinMarket implementation's xpub.  Tests use
deterministic test vectors derived from the well-known BIP39 test mnemonic
("abandon abandon ... about") at path m/84'/0'/0'.

The script under test is self-contained (only depends on coincurve).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add scripts directory to path for importing the derivation script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "scripts"))

from derive_bond_pubkey import (
    _base58_decode,
    _point_add,
    _scalar_to_pubkey,
    derive_bond_pubkey,
    derive_child_pubkey,
    locktime_to_timenumber,
    parse_xpub,
    timenumber_to_locktime,
    timenumber_to_timestamp,
)

# ---------------------------------------------------------------------------
# Test data -- deterministic vectors from the BIP39 "abandon...about" mnemonic
# (the standard test mnemonic from the BIP39 specification, no passphrase)
# Derivation: m/84'/0'/0' on mainnet (xpub version 0x0488b21e)
# ---------------------------------------------------------------------------

# Account xpub at m/84'/0'/0' (from "fbonds-mpk-" line in wallet-tool.py display)
ACCOUNT_XPUB = "xpub6CatWdiZiodmUeTDp8LT5or8nmbKNcuyvz7WyksVFkKB4RHwCD3XyuvPEbvqAQY3rAPshWcMLoP2fMFMKHPJ4ZeZXYVUhLv1VMrjPC7PW6V"  # noqa: E501

# Branch xpub at m/84'/0'/0'/2 (from the /2 sub-header in wallet-tool.py display)
BRANCH_XPUB = "xpub6FPnz8nd9KHwwz8jUeGM5uHg3vLvDrsoHpcciPDGmW37H8LguaZvpgRd4DLfvSzcAEdMxAo6yi1SjBGwoSvMfX4fcPbWSPiz18f9sKnfs47"  # noqa: E501

# Account xpub public key (at m/84'/0'/0')
ACCOUNT_PUBKEY_HEX = "02707a62fdacc26ea9b63b1c197906f56ee0180d0bcf1966e1a2da34f5f3a09a9b"

# Branch /2 public key (at m/84'/0'/0'/2)
BRANCH_PUBKEY_HEX = "02499f5d0718c971ac125f944ee0990964910f369f7e080c8478a0aa09d155fe97"

# Derived pubkey for locktime 2026-02 (timenumber 73) at m/84'/0'/0'/2/73
# Both account xpub and branch xpub must produce this same key.
BOND_PUBKEY_2026_02 = "03a30ac2cbcd6cafae59a6077893fe1aad0605efa7b98cd9c68cff754a13fe4d48"


# ---------------------------------------------------------------------------
# Tests: timenumber calculations
# ---------------------------------------------------------------------------


class TestTimenumberCalculation:
    def test_epoch_start(self) -> None:
        """January 2020 is timenumber 0."""
        assert locktime_to_timenumber(2020, 1) == 0

    def test_epoch_end(self) -> None:
        """December 2099 is timenumber 959."""
        assert locktime_to_timenumber(2099, 12) == 959

    def test_2026_02(self) -> None:
        """February 2026 is timenumber 73."""
        assert locktime_to_timenumber(2026, 2) == 73

    def test_2025_01(self) -> None:
        """January 2025 is timenumber 60."""
        assert locktime_to_timenumber(2025, 1) == 60

    def test_2020_12(self) -> None:
        """December 2020 is timenumber 11."""
        assert locktime_to_timenumber(2020, 12) == 11

    def test_before_epoch_raises(self) -> None:
        with pytest.raises(ValueError, match="outside the valid range"):
            locktime_to_timenumber(2019, 12)

    def test_after_epoch_raises(self) -> None:
        with pytest.raises(ValueError, match="outside the valid range"):
            locktime_to_timenumber(2100, 1)

    def test_invalid_month_low(self) -> None:
        with pytest.raises(ValueError, match="Month must be 1-12"):
            locktime_to_timenumber(2025, 0)

    def test_invalid_month_high(self) -> None:
        with pytest.raises(ValueError, match="Month must be 1-12"):
            locktime_to_timenumber(2025, 13)


class TestTimenumberRoundtrip:
    """Verify timenumber <-> (year, month) round-trips."""

    def test_roundtrip_epoch_start(self) -> None:
        year, month = timenumber_to_locktime(0)
        assert (year, month) == (2020, 1)
        assert locktime_to_timenumber(year, month) == 0

    def test_roundtrip_epoch_end(self) -> None:
        year, month = timenumber_to_locktime(959)
        assert (year, month) == (2099, 12)
        assert locktime_to_timenumber(year, month) == 959

    def test_roundtrip_2026_02(self) -> None:
        year, month = timenumber_to_locktime(73)
        assert (year, month) == (2026, 2)
        assert locktime_to_timenumber(year, month) == 73

    @pytest.mark.parametrize("tn", [0, 1, 11, 12, 59, 60, 73, 100, 500, 959])
    def test_roundtrip_parametric(self, tn: int) -> None:
        year, month = timenumber_to_locktime(tn)
        assert locktime_to_timenumber(year, month) == tn


class TestTimenumberTimestamp:
    def test_2020_01_timestamp(self) -> None:
        """January 2020 = Unix timestamp 1577836800."""
        assert timenumber_to_timestamp(0) == 1577836800

    def test_2026_02_timestamp(self) -> None:
        """February 2026 = Unix timestamp 1769904000."""
        assert timenumber_to_timestamp(73) == 1769904000

    def test_timestamp_monotonic(self) -> None:
        """Timestamps must be strictly increasing."""
        prev = timenumber_to_timestamp(0)
        for tn in range(1, 960):
            curr = timenumber_to_timestamp(tn)
            assert curr > prev, f"timenumber {tn} timestamp not > previous"
            prev = curr


# ---------------------------------------------------------------------------
# Tests: Base58 / xpub parsing
# ---------------------------------------------------------------------------


class TestBase58Decode:
    def test_invalid_checksum(self) -> None:
        """Corrupted xpub should fail checksum verification."""
        # Change one character in a valid xpub
        corrupted = ACCOUNT_XPUB[:-1] + ("Y" if ACCOUNT_XPUB[-1] != "Y" else "Z")
        with pytest.raises(ValueError, match="checksum"):
            _base58_decode(corrupted)


class TestParseXpub:
    def test_account_xpub_depth_and_key(self) -> None:
        """Account xpub at m/84'/0'/0' should be depth 3."""
        pubkey, chain_code, depth, child_number = parse_xpub(ACCOUNT_XPUB)
        assert depth == 3
        # child_number for hardened index 0 = 0x80000000
        assert child_number == 0x80000000
        assert pubkey.hex() == ACCOUNT_PUBKEY_HEX
        assert len(chain_code) == 32

    def test_branch_xpub_depth_and_key(self) -> None:
        """Branch xpub at m/84'/0'/0'/2 should be depth 4, child 2."""
        pubkey, chain_code, depth, child_number = parse_xpub(BRANCH_XPUB)
        assert depth == 4
        assert child_number == 2
        assert pubkey.hex() == BRANCH_PUBKEY_HEX
        assert len(chain_code) == 32

    def test_invalid_xpub_string(self) -> None:
        with pytest.raises((ValueError, IndexError)):
            parse_xpub("not-an-xpub")

    def test_compressed_pubkey_prefix(self) -> None:
        """Public key should start with 0x02 or 0x03."""
        pubkey, _, _, _ = parse_xpub(ACCOUNT_XPUB)
        assert pubkey[0] in (0x02, 0x03)


# ---------------------------------------------------------------------------
# Tests: BIP32 child derivation
# ---------------------------------------------------------------------------


class TestChildDerivation:
    def test_derive_branch_from_account(self) -> None:
        """Deriving child 2 from account xpub should match the branch xpub."""
        acct_pub, acct_cc, _, _ = parse_xpub(ACCOUNT_XPUB)
        br_pub, br_cc, _, _ = parse_xpub(BRANCH_XPUB)

        derived_pub, derived_cc = derive_child_pubkey(acct_pub, acct_cc, 2)

        assert derived_pub == br_pub
        assert derived_cc == br_cc

    def test_hardened_derivation_raises(self) -> None:
        """Cannot derive hardened child from xpub."""
        pubkey, chain_code, _, _ = parse_xpub(ACCOUNT_XPUB)
        with pytest.raises(ValueError, match="hardened"):
            derive_child_pubkey(pubkey, chain_code, 0x80000000)

    def test_different_indices_different_keys(self) -> None:
        """Different child indices must produce different keys."""
        pubkey, chain_code, _, _ = parse_xpub(BRANCH_XPUB)
        child_0, _ = derive_child_pubkey(pubkey, chain_code, 0)
        child_1, _ = derive_child_pubkey(pubkey, chain_code, 1)
        child_73, _ = derive_child_pubkey(pubkey, chain_code, 73)

        assert child_0 != child_1
        assert child_0 != child_73
        assert child_1 != child_73

    def test_derived_key_is_compressed(self) -> None:
        """Derived public key should be 33 bytes with 0x02 or 0x03 prefix."""
        pubkey, chain_code, _, _ = parse_xpub(BRANCH_XPUB)
        child_pub, child_cc = derive_child_pubkey(pubkey, chain_code, 73)

        assert len(child_pub) == 33
        assert child_pub[0] in (0x02, 0x03)
        assert len(child_cc) == 32


# ---------------------------------------------------------------------------
# Tests: EC point operations
# ---------------------------------------------------------------------------


class TestECOperations:
    def test_scalar_to_pubkey_produces_compressed(self) -> None:
        """A random 32-byte scalar should produce a 33-byte compressed key."""
        # Use SHA256("test") as a deterministic scalar
        import hashlib

        scalar = hashlib.sha256(b"test").digest()
        pubkey = _scalar_to_pubkey(scalar)
        assert len(pubkey) == 33
        assert pubkey[0] in (0x02, 0x03)

    def test_point_add_commutative(self) -> None:
        """Point addition should be commutative."""
        import hashlib

        s1 = hashlib.sha256(b"key1").digest()
        s2 = hashlib.sha256(b"key2").digest()
        p1 = _scalar_to_pubkey(s1)
        p2 = _scalar_to_pubkey(s2)

        sum1 = _point_add(p1, p2)
        sum2 = _point_add(p2, p1)
        assert sum1 == sum2


# ---------------------------------------------------------------------------
# Tests: derive_bond_pubkey (end-to-end)
# ---------------------------------------------------------------------------


class TestDeriveBondPubkey:
    def test_from_account_xpub(self) -> None:
        """Account xpub -> derive /2 -> derive /73 -> bond pubkey."""
        result = derive_bond_pubkey(ACCOUNT_XPUB, 2026, 2, branch_xpub=False)
        assert result == BOND_PUBKEY_2026_02

    def test_from_branch_xpub(self) -> None:
        """Branch xpub -> derive /73 -> bond pubkey (same result)."""
        result = derive_bond_pubkey(BRANCH_XPUB, 2026, 2, branch_xpub=True)
        assert result == BOND_PUBKEY_2026_02

    def test_account_and_branch_match(self) -> None:
        """Both paths must produce identical pubkeys."""
        from_account = derive_bond_pubkey(ACCOUNT_XPUB, 2026, 2, branch_xpub=False)
        from_branch = derive_bond_pubkey(BRANCH_XPUB, 2026, 2, branch_xpub=True)
        assert from_account == from_branch

    def test_different_locktimes_different_keys(self) -> None:
        """Different locktimes produce different bond pubkeys."""
        key_2026_01 = derive_bond_pubkey(ACCOUNT_XPUB, 2026, 1, branch_xpub=False)
        key_2026_02 = derive_bond_pubkey(ACCOUNT_XPUB, 2026, 2, branch_xpub=False)
        key_2027_01 = derive_bond_pubkey(ACCOUNT_XPUB, 2027, 1, branch_xpub=False)

        assert key_2026_01 != key_2026_02
        assert key_2026_01 != key_2027_01
        assert key_2026_02 != key_2027_01

    def test_pubkey_is_valid_hex(self) -> None:
        """Output should be 66-char hex (33 bytes compressed)."""
        result = derive_bond_pubkey(ACCOUNT_XPUB, 2026, 2, branch_xpub=False)
        assert len(result) == 66
        # Should be valid hex
        key_bytes = bytes.fromhex(result)
        assert len(key_bytes) == 33
        assert key_bytes[0] in (0x02, 0x03)

    def test_invalid_locktime_raises(self) -> None:
        with pytest.raises(ValueError, match="outside the valid range"):
            derive_bond_pubkey(ACCOUNT_XPUB, 2019, 12, branch_xpub=False)

    def test_epoch_start_locktime(self) -> None:
        """January 2020 (timenumber 0) should work."""
        result = derive_bond_pubkey(ACCOUNT_XPUB, 2020, 1, branch_xpub=False)
        assert len(result) == 66

    def test_epoch_end_locktime(self) -> None:
        """December 2099 (timenumber 959) should work."""
        result = derive_bond_pubkey(ACCOUNT_XPUB, 2099, 12, branch_xpub=False)
        assert len(result) == 66


# ---------------------------------------------------------------------------
# Tests: CLI main() function
# ---------------------------------------------------------------------------


class TestMainCLI:
    def test_info_mode(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--info mode should print timenumber info without needing an xpub."""
        from derive_bond_pubkey import main

        sys.argv = ["derive_bond_pubkey.py", "--locktime", "2026-02", "--info"]
        main()
        captured = capsys.readouterr()
        assert "Timenumber:      73" in captured.out
        assert "1769904000" in captured.out
        assert "m/84'/0'/0'/2/73" in captured.out

    def test_full_derivation_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Full derivation should print the pubkey and usage hint."""
        from derive_bond_pubkey import main

        sys.argv = ["derive_bond_pubkey.py", "--xpub", ACCOUNT_XPUB, "--locktime", "2026-02"]
        main()
        captured = capsys.readouterr()
        assert BOND_PUBKEY_2026_02 in captured.out
        assert "jm-wallet create-bond-address" in captured.out
        assert "--locktime-date 2026-02" in captured.out
        assert "account xpub" in captured.out

    def test_branch_xpub_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--branch-xpub flag should be reflected in output."""
        from derive_bond_pubkey import main

        sys.argv = [
            "derive_bond_pubkey.py",
            "--xpub",
            BRANCH_XPUB,
            "--locktime",
            "2026-02",
            "--branch-xpub",
        ]
        main()
        captured = capsys.readouterr()
        assert BOND_PUBKEY_2026_02 in captured.out
        assert "branch /2 xpub" in captured.out

    def test_missing_xpub_exits(self) -> None:
        """Without --info, --xpub is required."""
        from derive_bond_pubkey import main

        sys.argv = ["derive_bond_pubkey.py", "--locktime", "2026-02"]
        with pytest.raises(SystemExit):
            main()

    def test_invalid_locktime_format_exits(self) -> None:
        """Bad locktime format should exit with error."""
        from derive_bond_pubkey import main

        sys.argv = ["derive_bond_pubkey.py", "--xpub", ACCOUNT_XPUB, "--locktime", "2026"]
        with pytest.raises(SystemExit):
            main()

    def test_invalid_locktime_range_exits(self) -> None:
        """Out-of-range locktime should exit with error."""
        from derive_bond_pubkey import main

        sys.argv = ["derive_bond_pubkey.py", "--xpub", ACCOUNT_XPUB, "--locktime", "2100-01"]
        with pytest.raises(SystemExit):
            main()
