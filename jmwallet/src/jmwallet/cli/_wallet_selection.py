"""
Helpers for resolving a wallet identity (8-char BIP32 fingerprint) for
CLI commands that read unencrypted, per-wallet on-disk data such as
``jm-wallet history``, ``jm-wallet list-bonds`` and
``jm-wallet registry-show``.

These commands originally required the wallet mnemonic (and, when a
BIP39 passphrase was set, the passphrase too) just to recompute the
fingerprint that keys the data directory. That hurts usability:

* the user already typed the passphrase elsewhere (``info``, ``send``);
* knowing the fingerprint at all should be enough to read public data;
* when only one wallet has ever written to the directory, asking for
  the mnemonic again is needless friction.

This module centralizes the lookup order so every command that needs a
wallet fingerprint behaves the same:

1. ``--wallet-fingerprint <fp>`` if given (validated).
2. Derived from ``--mnemonic-file`` (+ BIP39 passphrase resolution).
3. Auto-detected when exactly one wallet identity is present on disk.
4. Otherwise: a clear error listing the known fingerprints and the
   ways to disambiguate (``--mnemonic-file [-passphrase]`` /
   ``--wallet-fingerprint`` / ``--all-wallets`` where applicable).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer
from jmcore.cli_common import resolve_mnemonic
from jmcore.settings import JoinMarketSettings
from loguru import logger


def validate_fingerprint(fingerprint: str) -> str:
    """Normalize and validate an 8-char hex wallet fingerprint.

    Raises ``typer.Exit(1)`` with a friendly message on invalid input.
    Returns the lowercased fingerprint on success.
    """
    fp = fingerprint.strip().lower()
    if len(fp) != 8:
        logger.error(f"--wallet-fingerprint must be exactly 8 hex chars, got {len(fp)}: {fp!r}")
        raise typer.Exit(1)
    try:
        bytes.fromhex(fp)
    except ValueError:
        logger.error(f"--wallet-fingerprint must be valid hex, got {fp!r}")
        raise typer.Exit(1)
    return fp


def resolve_wallet_fingerprint(
    settings: JoinMarketSettings,
    *,
    mnemonic_file: Path | None,
    wallet_fingerprint: str | None,
    prompt_bip39_passphrase: bool,
    list_known_fingerprints: Callable[[], list[str]],
    command_label: str,
    allow_all_wallets: bool = False,
) -> str | None:
    """Resolve the active wallet fingerprint for an offline read command.

    Args:
        settings: CLI settings (used to read configured mnemonic / passphrase).
        mnemonic_file: Optional ``--mnemonic-file`` value.
        wallet_fingerprint: Optional ``--wallet-fingerprint`` value (8 hex chars).
        prompt_bip39_passphrase: Whether to prompt for a BIP39 passphrase
            when the mnemonic is supplied and no passphrase is otherwise
            configured.
        list_known_fingerprints: Callable returning the fingerprints
            already present on disk for this command's data source
            (e.g. ``history.csv`` rows or ``fidelity_bonds_*.json``
            files). Used to auto-detect a single wallet and to render a
            helpful error listing the alternatives.
        command_label: Human-readable label used in error messages,
            e.g. ``"jm-wallet history"``.
        allow_all_wallets: When ``True``, the error message for the
            ambiguous case mentions ``--all-wallets`` as an alternative.

    Returns:
        The resolved 8-char fingerprint, or raises ``typer.Exit(1)``
        when the input is ambiguous or invalid. When no wallet identity
        is determinable at all and the data source is empty, returns
        ``None`` so the caller can fall through to its own
        "nothing to show" path.
    """
    # 1) Explicit --wallet-fingerprint short-circuits everything else.
    if wallet_fingerprint:
        return validate_fingerprint(wallet_fingerprint)

    # 2) --mnemonic-file derives the fingerprint. Note: we deliberately
    # do NOT fall through to settings-configured mnemonics here. Auto-
    # filtering an offline read command by whichever mnemonic happens to
    # be configured in ``config.toml`` would silently hide history rows
    # written by other wallets (and legacy untagged rows). Users who
    # want the configured mnemonic to drive selection must still pass
    # ``--mnemonic-file`` explicitly.
    if mnemonic_file is not None:
        try:
            resolved = resolve_mnemonic(
                settings,
                mnemonic_file=mnemonic_file,
                prompt_bip39_passphrase=prompt_bip39_passphrase,
            )
        except (FileNotFoundError, ValueError) as e:
            logger.error(str(e))
            raise typer.Exit(1)
        if resolved is None:
            logger.error("No mnemonic available; cannot derive wallet fingerprint.")
            raise typer.Exit(1)
        from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

        return get_mnemonic_fingerprint(resolved.mnemonic, resolved.bip39_passphrase or "")

    # 3) Auto-detect when exactly one wallet has ever written here.
    known = list_known_fingerprints()
    if len(known) == 1:
        fp = known[0]
        logger.info(
            f"Using the only wallet present in the data directory (fingerprint: {fp}). "
            "Pass --wallet-fingerprint or --mnemonic-file to be explicit."
        )
        return fp

    # 4) Ambiguous or empty: error with actionable guidance.
    if len(known) > 1:
        logger.error(
            f"{command_label} found multiple wallets in this data directory; please "
            "pick one explicitly."
        )
        logger.info("Known wallet fingerprints:")
        for fp in known:
            logger.info(f"  - {fp}")
        hints = [
            "--mnemonic-file <file> [--prompt-bip39-passphrase] to derive the active wallet",
            "--wallet-fingerprint <fp> if you already know it (see 'jm-wallet info')",
        ]
        if allow_all_wallets:
            hints.append("--all-wallets to include entries from every wallet")
        logger.info("Options:")
        for hint in hints:
            logger.info(f"  - {hint}")
        raise typer.Exit(1)

    return None
