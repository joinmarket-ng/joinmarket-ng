"""
Tests for jmcore.paths module - nick state file management.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jmcore.paths import (
    get_all_nick_states,
    get_commitment_blacklist_path,
    get_default_data_dir,
    get_ignored_makers_path,
    get_nick_state_path,
    get_used_commitments_path,
    get_wallet_metadata_path,
    read_nick_state,
    remove_nick_state,
    write_nick_state,
)


class TestNickStateFiles:
    """Tests for nick state file management functions."""

    def test_get_nick_state_path(self, tmp_path: Path) -> None:
        """Test that nick state path is correctly constructed."""
        path = get_nick_state_path(tmp_path, "maker")
        assert path == tmp_path / "state" / "maker.nick"

    def test_write_and_read_nick_state(self, tmp_path: Path) -> None:
        """Test writing and reading a nick state file."""
        # Write nick
        write_nick_state(tmp_path, "maker", "J5ABCDEFGHI")

        # Verify file exists
        assert (tmp_path / "state" / "maker.nick").exists()

        # Read nick back
        nick = read_nick_state(tmp_path, "maker")
        assert nick == "J5ABCDEFGHI"

    def test_read_nonexistent_nick_state(self, tmp_path: Path) -> None:
        """Test reading a non-existent nick state file returns None."""
        nick = read_nick_state(tmp_path, "nonexistent")
        assert nick is None

    def test_remove_nick_state(self, tmp_path: Path) -> None:
        """Test removing a nick state file."""
        # Write nick
        write_nick_state(tmp_path, "maker", "J5ABCDEFGHI")
        assert (tmp_path / "state" / "maker.nick").exists()

        # Remove nick
        result = remove_nick_state(tmp_path, "maker")
        assert result is True
        assert not (tmp_path / "state" / "maker.nick").exists()

    def test_remove_nonexistent_nick_state(self, tmp_path: Path) -> None:
        """Test removing a non-existent nick state file returns False."""
        result = remove_nick_state(tmp_path, "nonexistent")
        assert result is False

    def test_get_all_nick_states_empty(self, tmp_path: Path) -> None:
        """Test getting all nick states when none exist."""
        states = get_all_nick_states(tmp_path)
        assert states == {}

    def test_get_all_nick_states_multiple(self, tmp_path: Path) -> None:
        """Test getting all nick states with multiple components."""
        # Write multiple nicks
        write_nick_state(tmp_path, "maker", "J5MAKERABC")
        write_nick_state(tmp_path, "taker", "J5TAKERXYZ")
        write_nick_state(tmp_path, "directory", "directory-mainnet")
        write_nick_state(tmp_path, "orderbook", "J5ORDERBOOK")

        # Get all states
        states = get_all_nick_states(tmp_path)

        assert len(states) == 4
        assert states["maker"] == "J5MAKERABC"
        assert states["taker"] == "J5TAKERXYZ"
        assert states["directory"] == "directory-mainnet"
        assert states["orderbook"] == "J5ORDERBOOK"

    def test_write_overwrites_existing(self, tmp_path: Path) -> None:
        """Test that writing a nick overwrites existing file."""
        write_nick_state(tmp_path, "maker", "J5OLDNICK")
        write_nick_state(tmp_path, "maker", "J5NEWNICK")

        nick = read_nick_state(tmp_path, "maker")
        assert nick == "J5NEWNICK"

    def test_write_creates_state_directory(self, tmp_path: Path) -> None:
        """Test that writing creates the state directory if needed."""
        # Ensure state directory doesn't exist
        state_dir = tmp_path / "state"
        assert not state_dir.exists()

        # Write nick (should create directory)
        write_nick_state(tmp_path, "maker", "J5ABCDEFGHI")

        # Verify directory was created
        assert state_dir.exists()
        assert state_dir.is_dir()

    def test_nick_state_is_private_despite_permissive_umask(self, tmp_path: Path) -> None:
        """New nick state and its application-owned directory are owner-only."""
        previous_umask = os.umask(0o022)
        try:
            path = write_nick_state(tmp_path, "maker", "J5ABCDEFGHI")
        finally:
            os.umask(previous_umask)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    def test_read_tightens_existing_nick_state_without_changing_content(
        self, tmp_path: Path
    ) -> None:
        """Legacy nick state permissions are upgraded when the file is read."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        path = state_dir / "maker.nick"
        original = b"J5ABCDEFGHI\n"
        path.write_bytes(original)
        path.chmod(0o644)

        assert read_nick_state(tmp_path, "maker") == "J5ABCDEFGHI"
        assert path.read_bytes() == original
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_write_does_not_tighten_custom_data_parent(self, tmp_path: Path) -> None:
        """Only the application-owned state directory is tightened."""
        data_dir = tmp_path / "shared-data"
        data_dir.mkdir()
        data_dir.chmod(0o755)

        write_nick_state(data_dir, "maker", "J5ABCDEFGHI")

        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755

    def test_state_directory_alias_preserves_target_mode(self, tmp_path: Path) -> None:
        """A configured state directory alias remains intact while its nick is updated."""
        state_target = tmp_path / "shared-state"
        state_target.mkdir()
        state_target.chmod(0o755)
        state_alias = tmp_path / "state"
        state_alias.symlink_to(state_target, target_is_directory=True)

        path = write_nick_state(tmp_path, "maker", "J5ABCDEFGHI")

        assert path == state_alias / "maker.nick"
        assert state_alias.is_symlink()
        assert (state_target / "maker.nick").read_text() == "J5ABCDEFGHI\n"
        assert stat.S_IMODE(state_target.stat().st_mode) == 0o755

    def test_nick_alias_is_read_and_updated_without_replacement(self, tmp_path: Path) -> None:
        """A configured nick alias is read and atomically updated at its target."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = tmp_path / "maker.nick.target"
        target.write_text("J5OLDNICK\n")
        nick_alias = state_dir / "maker.nick"
        nick_alias.symlink_to(target)

        assert read_nick_state(tmp_path, "maker") == "J5OLDNICK"
        write_nick_state(tmp_path, "maker", "J5NEWNICK")

        assert nick_alias.is_symlink()
        assert target.read_text() == "J5NEWNICK\n"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_read_strips_whitespace(self, tmp_path: Path) -> None:
        """Test that reading strips whitespace from nick."""
        # Manually create file with extra whitespace
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        nick_file = state_dir / "maker.nick"
        nick_file.write_text("  J5ABCDEFGHI  \n\n")

        nick = read_nick_state(tmp_path, "maker")
        assert nick == "J5ABCDEFGHI"

    def test_get_all_nick_states_ignores_empty_files(self, tmp_path: Path) -> None:
        """Test that empty nick files are ignored."""
        # Create state directory
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        # Create an empty nick file
        (state_dir / "empty.nick").write_text("")

        # Create a valid nick file
        (state_dir / "maker.nick").write_text("J5ABCDEFGHI\n")

        states = get_all_nick_states(tmp_path)

        # Only the valid file should be included
        assert len(states) == 1
        assert "maker" in states
        assert "empty" not in states

    def test_get_all_nick_states_ignores_non_nick_files(self, tmp_path: Path) -> None:
        """Test that non-.nick files are ignored."""
        # Create state directory with mixed files
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        (state_dir / "maker.nick").write_text("J5MAKERABC\n")
        (state_dir / "other.txt").write_text("some other file")
        (state_dir / "taker.nick").write_text("J5TAKERXYZ\n")

        states = get_all_nick_states(tmp_path)

        assert len(states) == 2
        assert "maker" in states
        assert "taker" in states
        assert "other" not in states


class TestNickStateDefaultDataDir:
    """Tests for nick state functions with default data directory."""

    def test_write_with_none_data_dir(self, tmp_path: Path) -> None:
        """Test that None data_dir uses default."""
        from unittest.mock import patch

        # Mock get_default_data_dir to use tmp_path instead of ~/.joinmarket-ng
        with patch("jmcore.paths.get_default_data_dir", return_value=tmp_path):
            path = write_nick_state(None, "maker", "J5testNick")
            assert path.exists()
            assert path.read_text() == "J5testNick\n"  # write_nick_state adds newline
            assert path == tmp_path / "state" / "maker.nick"

    def test_get_nick_state_path_with_none(self, tmp_path: Path) -> None:
        """Test that None data_dir returns path under default data dir."""
        from unittest.mock import patch

        with patch("jmcore.paths.get_default_data_dir", return_value=tmp_path):
            path = get_nick_state_path(None, "maker")
            # Should be under mocked data dir/state/maker.nick
            assert path.name == "maker.nick"
            assert path.parent.name == "state"
            assert path == tmp_path / "state" / "maker.nick"


class TestPathUtilities:
    """Tests for path utility functions."""

    def test_get_default_data_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test get_default_data_dir with JOINMARKET_DATA_DIR env var."""
        data_dir = tmp_path / "jm-test-data"
        data_dir.mkdir()
        data_dir.chmod(0o755)
        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(data_dir))

        result = get_default_data_dir()

        assert result == data_dir
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755

    def test_get_default_data_dir_no_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test get_default_data_dir falls back to home directory."""
        monkeypatch.delenv("JOINMARKET_DATA_DIR", raising=False)
        from unittest.mock import patch

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = get_default_data_dir()
            assert result == tmp_path / ".joinmarket-ng"
            assert stat.S_IMODE(result.stat().st_mode) == 0o700

    def test_get_default_data_dir_preserves_existing_alias(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An existing default data-directory alias remains usable and unchanged."""
        monkeypatch.delenv("JOINMARKET_DATA_DIR", raising=False)
        target = tmp_path / "shared-data"
        target.mkdir()
        target.chmod(0o755)
        alias = tmp_path / ".joinmarket-ng"
        alias.symlink_to(target, target_is_directory=True)
        from unittest.mock import patch

        with patch("pathlib.Path.home", return_value=tmp_path):
            assert get_default_data_dir() == alias

        assert alias.is_symlink()
        assert stat.S_IMODE(target.stat().st_mode) == 0o755

    def test_get_commitment_blacklist_path(self, tmp_path: Path) -> None:
        """Test get_commitment_blacklist_path with explicit data_dir."""
        result = get_commitment_blacklist_path(tmp_path)
        assert result == tmp_path / "cmtdata" / "commitmentlist"
        assert result.parent.exists()

    def test_get_commitment_blacklist_path_none(self, tmp_path: Path) -> None:
        """Test get_commitment_blacklist_path with None data_dir."""
        from unittest.mock import patch

        with patch("jmcore.paths.get_default_data_dir", return_value=tmp_path):
            result = get_commitment_blacklist_path(None)
            assert result == tmp_path / "cmtdata" / "commitmentlist"

    def test_get_used_commitments_path(self, tmp_path: Path) -> None:
        """Test get_used_commitments_path."""
        result = get_used_commitments_path(tmp_path)
        assert result == tmp_path / "cmtdata" / "commitments.json"
        assert result.parent.exists()

    def test_get_used_commitments_path_none(self, tmp_path: Path) -> None:
        """Test get_used_commitments_path with None uses default."""
        from unittest.mock import patch

        with patch("jmcore.paths.get_default_data_dir", return_value=tmp_path):
            result = get_used_commitments_path(None)
            assert result == tmp_path / "cmtdata" / "commitments.json"

    def test_get_ignored_makers_path(self, tmp_path: Path) -> None:
        """Test get_ignored_makers_path."""
        result = get_ignored_makers_path(tmp_path)
        assert result == tmp_path / "ignored_makers.txt"

    def test_get_ignored_makers_path_none(self, tmp_path: Path) -> None:
        """Test get_ignored_makers_path with None uses default."""
        from unittest.mock import patch

        with patch("jmcore.paths.get_default_data_dir", return_value=tmp_path):
            result = get_ignored_makers_path(None)
            assert result == tmp_path / "ignored_makers.txt"

    def test_get_wallet_metadata_path(self, tmp_path: Path) -> None:
        """Test get_wallet_metadata_path."""
        result = get_wallet_metadata_path(tmp_path)
        assert result == tmp_path / "wallet_metadata.jsonl"

    def test_get_wallet_metadata_path_none(self, tmp_path: Path) -> None:
        """Test get_wallet_metadata_path with None uses default."""
        from unittest.mock import patch

        with patch("jmcore.paths.get_default_data_dir", return_value=tmp_path):
            result = get_wallet_metadata_path(None)
            assert result == tmp_path / "wallet_metadata.jsonl"

    def test_get_wallet_metadata_path_with_fingerprint(self, tmp_path: Path) -> None:
        """Per-wallet partitioning yields wallet_metadata_<fp>.jsonl."""
        result = get_wallet_metadata_path(tmp_path, fingerprint="aabbccdd")
        assert result == tmp_path / "wallet_metadata_aabbccdd.jsonl"

    def test_get_wallet_metadata_path_fingerprint_lowercased(self, tmp_path: Path) -> None:
        """Fingerprint is normalized to lowercase to keep filename stable."""
        result = get_wallet_metadata_path(tmp_path, fingerprint="AABBCCDD")
        assert result == tmp_path / "wallet_metadata_aabbccdd.jsonl"

    def test_get_wallet_metadata_path_rejects_unsafe_fingerprint(self, tmp_path: Path) -> None:
        """Non-hex fingerprints fall back to the shared path, never compose
        an unsafe filename (slashes, dots, etc.).
        """
        for bad in ["../etc", "ab/cd", "a.b", "abXY", "", "   "]:
            result = get_wallet_metadata_path(tmp_path, fingerprint=bad)
            assert result == tmp_path / "wallet_metadata.jsonl", (
                f"unsafe fingerprint {bad!r} should fall back to shared path"
            )


class TestNickStateStringDataDir:
    """Tests for nick state functions with string data_dir."""

    def test_get_nick_state_path_string(self, tmp_path: Path) -> None:
        """get_nick_state_path with str data_dir should work."""
        path = get_nick_state_path(str(tmp_path), "maker")
        assert path == tmp_path / "state" / "maker.nick"

    def test_write_and_read_string_data_dir(self, tmp_path: Path) -> None:
        """write/read_nick_state should work with str data_dir."""
        write_nick_state(str(tmp_path), "maker", "J5TESTSTR")
        nick = read_nick_state(str(tmp_path), "maker")
        assert nick == "J5TESTSTR"

    def test_remove_nick_state_string_data_dir(self, tmp_path: Path) -> None:
        """remove_nick_state should work with str data_dir."""
        write_nick_state(str(tmp_path), "maker", "J5TESTSTR")
        result = remove_nick_state(str(tmp_path), "maker")
        assert result is True
        assert read_nick_state(str(tmp_path), "maker") is None

    def test_get_all_nick_states_string_data_dir(self, tmp_path: Path) -> None:
        """get_all_nick_states with str data_dir."""
        write_nick_state(tmp_path, "maker", "J5MAKERABC")
        states = get_all_nick_states(str(tmp_path))
        assert states == {"maker": "J5MAKERABC"}

    def test_read_nick_state_oserror(self, tmp_path: Path) -> None:
        """read_nick_state should return None on OSError."""
        # Create state dir and a directory where a file would be (causes OSError on read)
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        nick_path = state_dir / "maker.nick"
        nick_path.mkdir()  # Make it a directory instead of file

        nick = read_nick_state(tmp_path, "maker")
        assert nick is None

    def test_remove_nick_state_oserror(self, tmp_path: Path) -> None:
        """remove_nick_state should return False on OSError."""
        # Create state dir and make the nick file a non-empty directory
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        nick_path = state_dir / "maker.nick"
        nick_path.mkdir()
        (nick_path / "child").write_text("block removal")

        result = remove_nick_state(tmp_path, "maker")
        assert result is False

    def test_get_all_nick_states_oserror(self, tmp_path: Path) -> None:
        """get_all_nick_states should skip files with OSError."""
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)

        # Create a valid nick file
        (state_dir / "maker.nick").write_text("J5GOOD\n")

        # Create a nick file that's actually a directory (triggers OSError)
        (state_dir / "broken.nick").mkdir()

        states = get_all_nick_states(tmp_path)
        assert "maker" in states
        assert "broken" not in states
