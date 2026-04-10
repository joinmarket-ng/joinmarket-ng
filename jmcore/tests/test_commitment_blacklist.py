"""
Tests for the commitment blacklist module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jmcore.commitment_blacklist import CommitmentBlacklist


class TestCommitmentBlacklist:
    """Tests for CommitmentBlacklist class."""

    def test_empty_blacklist(self, tmp_path: Path) -> None:
        """Test that a new blacklist is empty."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")
        assert len(blacklist) == 0

    def test_add_commitment(self, tmp_path: Path) -> None:
        """Test adding a commitment to the blacklist."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")

        commitment = "a" * 64
        result = blacklist.add(commitment)

        assert result is True
        assert len(blacklist) == 1
        assert commitment in blacklist

    def test_add_duplicate_commitment(self, tmp_path: Path) -> None:
        """Test that adding a duplicate commitment returns False."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")

        commitment = "b" * 64
        result1 = blacklist.add(commitment)
        result2 = blacklist.add(commitment)

        assert result1 is True
        assert result2 is False
        assert len(blacklist) == 1

    def test_is_blacklisted(self, tmp_path: Path) -> None:
        """Test checking if a commitment is blacklisted."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")

        commitment = "c" * 64
        assert blacklist.is_blacklisted(commitment) is False

        blacklist.add(commitment)
        assert blacklist.is_blacklisted(commitment) is True

    def test_check_and_add(self, tmp_path: Path) -> None:
        """Test the atomic check_and_add operation."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")

        commitment = "d" * 64

        # First call should return True (allowed, then added)
        result1 = blacklist.check_and_add(commitment)
        assert result1 is True
        assert commitment in blacklist

        # Second call should return False (already blacklisted)
        result2 = blacklist.check_and_add(commitment)
        assert result2 is False

    def test_persistence(self, tmp_path: Path) -> None:
        """Test that commitments are persisted to disk."""
        blacklist_path = tmp_path / "commitmentlist"

        # Add commitments and save
        blacklist1 = CommitmentBlacklist(blacklist_path)
        blacklist1.add("e" * 64)
        blacklist1.add("f" * 64)

        # Create new instance to load from disk
        blacklist2 = CommitmentBlacklist(blacklist_path)

        assert len(blacklist2) == 2
        assert "e" * 64 in blacklist2
        assert "f" * 64 in blacklist2

    def test_case_insensitivity(self, tmp_path: Path) -> None:
        """Test that commitments are case-insensitive."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")

        commitment_lower = "abcdef1234567890" + "0" * 48
        commitment_upper = "ABCDEF1234567890" + "0" * 48

        blacklist.add(commitment_lower)

        # Should find it regardless of case
        assert blacklist.is_blacklisted(commitment_upper) is True
        assert blacklist.is_blacklisted(commitment_lower) is True

    def test_whitespace_handling(self, tmp_path: Path) -> None:
        """Test that whitespace is stripped from commitments."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")

        commitment = "g" * 64
        commitment_with_space = f"  {commitment}  "

        blacklist.add(commitment_with_space)

        assert commitment in blacklist
        assert blacklist.is_blacklisted(commitment) is True

    def test_empty_commitment_rejected(self, tmp_path: Path) -> None:
        """Test that empty commitments are rejected."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")

        result = blacklist.add("")
        assert result is False
        assert len(blacklist) == 0

        result = blacklist.add("   ")
        assert result is False
        assert len(blacklist) == 0

    def test_multiple_commitments(self, tmp_path: Path) -> None:
        """Test adding multiple commitments."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")

        commitments = [f"{i:064x}" for i in range(10)]

        for c in commitments:
            blacklist.add(c)

        assert len(blacklist) == 10

        for c in commitments:
            assert c in blacklist

    def test_file_created_on_first_add(self, tmp_path: Path) -> None:
        """Test that the blacklist file is created on first add."""
        blacklist_path = tmp_path / "subdir" / "commitmentlist"

        # File and directory shouldn't exist yet
        assert not blacklist_path.exists()
        assert not blacklist_path.parent.exists()

        blacklist = CommitmentBlacklist(blacklist_path)
        blacklist.add("h" * 64)

        # Now the file should exist
        assert blacklist_path.exists()

    def test_load_corrupted_file(self, tmp_path: Path) -> None:
        """Test that corrupted files are handled gracefully."""
        blacklist_path = tmp_path / "commitmentlist"

        # Create a file with some valid and some empty lines
        blacklist_path.write_text("valid_commitment\n\n  \nanother_valid\n")

        blacklist = CommitmentBlacklist(blacklist_path)

        # Should load valid commitments only
        assert "valid_commitment" in blacklist
        assert "another_valid" in blacklist

    def test_load_mixed_case_from_disk(self, tmp_path: Path) -> None:
        """Test that commitments loaded from disk are normalized to lowercase.

        The reference implementation stores commitments without case
        normalization. When loading such a file, NG must lowercase entries
        so that is_blacklisted() (which also lowercases the query) can
        find them. Without this, a mixed-case commitment written by the
        reference impl could bypass the blacklist after a reload.
        """
        blacklist_path = tmp_path / "commitmentlist"

        # Simulate a file written by the reference implementation with
        # mixed-case hex commitments
        mixed_case = "ABCDEF1234567890" + "0" * 48
        blacklist_path.write_text(mixed_case + "\n")

        blacklist = CommitmentBlacklist(blacklist_path)

        # Must find it regardless of query case
        assert blacklist.is_blacklisted(mixed_case) is True
        assert blacklist.is_blacklisted(mixed_case.lower()) is True
        assert blacklist.is_blacklisted(mixed_case.upper()) is True

        # check_and_add must also detect it as already present
        assert blacklist.check_and_add(mixed_case) is False

    def test_case_insensitive_persistence_roundtrip(self, tmp_path: Path) -> None:
        """Test that case normalization survives a save-then-load cycle.

        Commitments added with any casing should be found after the
        blacklist is reloaded from disk.
        """
        blacklist_path = tmp_path / "commitmentlist"

        bl1 = CommitmentBlacklist(blacklist_path)
        upper_commitment = "AABBCCDD" + "0" * 56
        bl1.add(upper_commitment)

        # Reload from disk
        bl2 = CommitmentBlacklist(blacklist_path)

        assert bl2.is_blacklisted(upper_commitment) is True
        assert bl2.is_blacklisted(upper_commitment.lower()) is True
        assert bl2.is_blacklisted("AaBbCcDd" + "0" * 56) is True


class TestGlobalBlacklist:
    """Tests for global blacklist functions."""

    def test_global_functions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test the convenience global functions."""
        from jmcore import commitment_blacklist

        # Reset global state
        monkeypatch.setattr(commitment_blacklist, "_global_blacklist", None)

        # Set custom path
        commitment_blacklist.set_blacklist_path(tmp_path / "commitmentlist")

        commitment = "i" * 64

        # Initially allowed
        assert commitment_blacklist.check_commitment(commitment) is True

        # Add it
        result = commitment_blacklist.add_commitment(commitment)
        assert result is True

        # Now blacklisted
        assert commitment_blacklist.check_commitment(commitment) is False

        # Try check_and_add on a new commitment
        new_commitment = "j" * 64
        assert commitment_blacklist.check_and_add_commitment(new_commitment) is True
        assert commitment_blacklist.check_and_add_commitment(new_commitment) is False


class TestCommitmentBlacklistDataDir:
    """Tests for CommitmentBlacklist with data_dir parameter."""

    def test_init_with_data_dir(self, tmp_path: Path) -> None:
        """Test creating blacklist with data_dir instead of explicit path."""
        blacklist = CommitmentBlacklist(blacklist_path=None, data_dir=tmp_path)
        # Should use cmtdata/commitmentlist under data_dir
        assert "cmtdata" in str(blacklist.blacklist_path)
        assert blacklist.blacklist_path.name == "commitmentlist"

        # Should be functional
        blacklist.add("a" * 64)
        assert "a" * 64 in blacklist

    def test_check_and_add_empty_string(self, tmp_path: Path) -> None:
        """check_and_add with empty string should return False."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")
        result = blacklist.check_and_add("")
        assert result is False
        assert len(blacklist) == 0

    def test_check_and_add_whitespace_only(self, tmp_path: Path) -> None:
        """check_and_add with whitespace-only should return False."""
        blacklist = CommitmentBlacklist(tmp_path / "commitmentlist")
        result = blacklist.check_and_add("   ")
        assert result is False
        assert len(blacklist) == 0

    def test_get_blacklist_with_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test get_blacklist with data_dir parameter."""
        from jmcore import commitment_blacklist

        monkeypatch.setattr(commitment_blacklist, "_global_blacklist", None)

        bl = commitment_blacklist.get_blacklist(data_dir=tmp_path)
        assert bl is not None
        assert "cmtdata" in str(bl.blacklist_path)


class TestValidateCommitmentHex:
    """Tests for the validate_commitment_hex helper."""

    def test_valid_commitment(self) -> None:
        """A 64-char lowercase hex string is valid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("ab" * 32)
        assert valid is True
        assert error == ""

    def test_valid_uppercase(self) -> None:
        """Uppercase hex is also valid (normalization happens elsewhere)."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("AB" * 32)
        assert valid is True
        assert error == ""

    def test_valid_mixed_case(self) -> None:
        """Mixed-case hex is valid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("aB" * 32)
        assert valid is True
        assert error == ""

    def test_empty_string(self) -> None:
        """Empty string is invalid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("")
        assert valid is False
        assert "empty" in error

    def test_too_short(self) -> None:
        """Strings shorter than 64 chars are invalid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("ab" * 16)
        assert valid is False
        assert "length" in error

    def test_too_long(self) -> None:
        """Strings longer than 64 chars are invalid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("ab" * 33)
        assert valid is False
        assert "length" in error

    def test_non_hex_characters(self) -> None:
        """Strings with non-hex characters are invalid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        # 'g' is not a hex character
        valid, error = validate_commitment_hex("g" * 64)
        assert valid is False
        assert "non-hex" in error

    def test_with_spaces(self) -> None:
        """Strings containing spaces are invalid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("ab " + "cd" * 31)
        assert valid is False
        # Either length or non-hex depending on stripping
        assert valid is False

    def test_with_prefix_not_stripped(self) -> None:
        """The 'P' prefix should be stripped before calling validate."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        # "P" + 64 hex chars = 65 chars total, wrong length
        valid, error = validate_commitment_hex("P" + "ab" * 32)
        assert valid is False

    def test_all_zeros(self) -> None:
        """All-zeros commitment is syntactically valid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("0" * 64)
        assert valid is True

    def test_all_f(self) -> None:
        """All-f commitment is syntactically valid."""
        from jmcore.commitment_blacklist import validate_commitment_hex

        valid, error = validate_commitment_hex("f" * 64)
        assert valid is True
