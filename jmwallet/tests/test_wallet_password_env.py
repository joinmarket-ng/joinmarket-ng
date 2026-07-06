"""Tests for MNEMONIC_PASSWORD env-var support in `jm-wallet generate` / `import`.

Regression coverage for issue #462: the TUI collects the encryption
password via whiptail and exports MNEMONIC_PASSWORD before invoking
jm-wallet so the user is not asked for the password a second time.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jmwallet.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _offline_creation_height_fetch(monkeypatch):
    """Keep tests hermetic: ``generate`` performs a best-effort chain tip
    lookup to record the wallet creation height (issue #472); simulate an
    unreachable backend so no RPC call is ever attempted."""

    async def _unreachable(*args, **kwargs):
        raise ConnectionError("test: backend unreachable")

    monkeypatch.setattr("jmwallet.cli.wallet._fetch_current_block_height", _unreachable)


class TestGenerateRespectsMnemonicPasswordEnv:
    def test_generate_uses_env_password_without_prompting(self, monkeypatch) -> None:
        """When MNEMONIC_PASSWORD is set, ``generate`` must use it directly
        and NOT drop into an interactive password prompt."""
        monkeypatch.setenv("MNEMONIC_PASSWORD", "hunter2_env_pwd")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "wallet.mnemonic"

            result = runner.invoke(
                app,
                ["generate", "--words", "12", "--output", str(output_file)],
                # No stdin input: if the code ever tries to prompt we fail
                # (CliRunner would error on unexpected interactive input).
                input="",
            )

            assert result.exit_code == 0, f"generate failed: {result.stdout}"
            assert output_file.exists()
            assert "File is encrypted" in result.stdout

            # Confirm the file is encrypted with the exact env password by
            # decrypting it.
            from jmwallet.cli.mnemonic import load_mnemonic_file

            mnemonic = load_mnemonic_file(output_file, "hunter2_env_pwd")
            assert mnemonic, "decrypted mnemonic is empty"

    def test_generate_without_env_still_falls_back_to_prompt_flag(self, monkeypatch) -> None:
        """Without MNEMONIC_PASSWORD the existing --no-prompt-password
        behaviour is unchanged: no password, no encryption."""
        monkeypatch.delenv("MNEMONIC_PASSWORD", raising=False)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "wallet.mnemonic"
            result = runner.invoke(
                app,
                [
                    "generate",
                    "--words",
                    "12",
                    "--output",
                    str(output_file),
                    "--no-prompt-password",
                ],
            )
            assert result.exit_code == 0, f"generate failed: {result.stdout}"
            assert "File is NOT encrypted" in result.stdout

    def test_generate_empty_env_password_means_unencrypted(self, monkeypatch) -> None:
        """An empty MNEMONIC_PASSWORD must behave like "no password" and
        must NOT attempt interactive prompting."""
        monkeypatch.setenv("MNEMONIC_PASSWORD", "")

        with tempfile.TemporaryDirectory() as tmpdir:
            output_file = Path(tmpdir) / "wallet.mnemonic"
            result = runner.invoke(
                app,
                [
                    "generate",
                    "--words",
                    "12",
                    "--output",
                    str(output_file),
                    "--no-prompt-password",
                ],
                input="",
            )
            assert result.exit_code == 0, f"generate failed: {result.stdout}"
            assert "File is NOT encrypted" in result.stdout
