"""
JoinMarket Wallet CLI package.

This package provides the CLI commands for managing JoinMarket wallets.
Commands are organized into submodules and registered via ``@app.command()``
decorators that reference the ``app`` Typer instance defined here.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="jm-wallet",
    help="JoinMarket Wallet Management",
    no_args_is_help=True,
)


def main() -> None:
    """Entry point for the ``jm-wallet`` console script."""
    app()


# ---------------------------------------------------------------------------
# Backwards-compatible re-exports from jmwallet.cli.mnemonic
# Tests and external code import these directly from ``jmwallet.cli``.
# ---------------------------------------------------------------------------
from jmwallet.cli.mnemonic import (  # noqa: E402, F401, I001
    _interactive_word_input,
    _read_char,
    _supports_raw_terminal,
    decrypt_mnemonic,
    encrypt_mnemonic,
    format_word_suggestions,
    generate_mnemonic_secure,
    get_bip39_wordlist,
    get_word_completions,
    interactive_mnemonic_input,
    load_mnemonic_file,
    prompt_password_with_confirmation,
    save_mnemonic_file,
    validate_mnemonic,
)

# ---------------------------------------------------------------------------
# Import submodules to register their ``@app.command()`` decorated functions.
# These imports MUST come after ``app`` is defined above.
# ---------------------------------------------------------------------------
from jmwallet.cli import (  # noqa: E402, F401
    address,
    bonds,
    cold_wallet,
    debug_info,
    freeze,
    history_cmd,
    registry,
    send,
    wallet,
)

if __name__ == "__main__":
    main()
