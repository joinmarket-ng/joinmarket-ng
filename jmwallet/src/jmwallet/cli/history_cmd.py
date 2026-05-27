"""
History command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import typer
from jmcore.cli_common import setup_cli
from loguru import logger

from jmwallet.cli import app
from jmwallet.cli._wallet_selection import resolve_wallet_fingerprint


@app.command()
def history(
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Max entries to show")] = None,
    role: Annotated[
        str | None, typer.Option("--role", "-r", help="Filter by role (maker/taker)")
    ] = None,
    stats: Annotated[bool, typer.Option("--stats", "-s", help="Show statistics only")] = False,
    csv_output: Annotated[bool, typer.Option("--csv", help="Output as CSV")] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    mnemonic_file: Annotated[
        Path | None,
        typer.Option(
            "--mnemonic-file",
            "-f",
            help=(
                "Path to mnemonic file. When provided, the history is filtered "
                "to entries belonging to this wallet (matched by BIP32 master "
                "fingerprint). Required when multiple wallets share the same "
                "data directory (issue #473) unless --wallet-fingerprint is "
                "passed instead."
            ),
            envvar="MNEMONIC_FILE",
        ),
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--prompt-bip39-passphrase",
            help=(
                "Prompt for the BIP39 passphrase when deriving the wallet "
                "fingerprint from --mnemonic-file. Required when the wallet "
                "was created with a BIP39 passphrase, otherwise the derived "
                "fingerprint will not match any recorded history."
            ),
        ),
    ] = False,
    wallet_fingerprint: Annotated[
        str | None,
        typer.Option(
            "--wallet-fingerprint",
            help=(
                "Filter history to this 8-char hex BIP32 master fingerprint. "
                "Use this instead of --mnemonic-file when you already know the "
                "fingerprint (e.g. printed by 'jm-wallet info'). When neither "
                "this flag nor --mnemonic-file is given and history contains "
                "exactly one wallet, that wallet is selected automatically."
            ),
        ),
    ] = None,
    all_wallets: Annotated[
        bool,
        typer.Option(
            "--all-wallets",
            help=(
                "Show entries from all wallets that have ever written to this "
                "data directory, including legacy rows without a fingerprint."
            ),
        ),
    ] = False,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """View CoinJoin transaction history.

    By default the active wallet's entries are shown. The wallet is
    selected (in priority order) from ``--wallet-fingerprint``,
    ``--mnemonic-file`` (with optional ``--prompt-bip39-passphrase``),
    or auto-detected when ``history.csv`` contains exactly one wallet.
    Pass ``--all-wallets`` to disable per-wallet filtering entirely.
    """
    from jmwallet.history import (
        get_history_stats,
        list_history_fingerprints,
        read_history,
    )

    settings = setup_cli(log_level)

    role_filter: Literal["maker", "taker"] | None = None
    if role:
        if role.lower() not in ("maker", "taker"):
            logger.error("Role must be 'maker' or 'taker'")
            raise typer.Exit(1)
        role_filter = role.lower()  # type: ignore[assignment]

    # Resolve the wallet fingerprint to scope the history to (issue #473).
    wallet_fp: str | None = None
    if not all_wallets:
        resolved_data_dir = data_dir if data_dir else settings.get_data_dir()
        wallet_fp = resolve_wallet_fingerprint(
            settings,
            mnemonic_file=mnemonic_file,
            wallet_fingerprint=wallet_fingerprint,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
            list_known_fingerprints=lambda: list_history_fingerprints(resolved_data_dir),
            command_label="jm-wallet history",
            allow_all_wallets=True,
        )

    if stats:
        stats_data = get_history_stats(data_dir, wallet_fingerprint=wallet_fp)

        print("\n" + "=" * 60)
        print("COINJOIN HISTORY STATISTICS")
        if wallet_fp is not None:
            print(f"Wallet: {wallet_fp}")
        print("=" * 60)
        print(f"Total CoinJoins:      {stats_data['total_coinjoins']}")
        print(f"  As Maker:           {stats_data['maker_coinjoins']}")
        print(f"  As Taker:           {stats_data['taker_coinjoins']}")
        print(f"Success Rate:         {stats_data['success_rate']:.1f}%")
        print(f"Successful Volume:    {stats_data['successful_volume']:,} sats")
        print(f"Total Volume:         {stats_data['total_volume']:,} sats")
        print(f"Total Fees Earned:    {stats_data['total_fees_earned']:,} sats")
        print(f"Total Fees Paid:      {stats_data['total_fees_paid']:,} sats")
        print(f"UTXOs Disclosed:      {stats_data['utxos_disclosed']}")
        print("=" * 60 + "\n")
        return

    entries = read_history(data_dir, limit, role_filter, wallet_fingerprint=wallet_fp)

    if not entries:
        print("\nNo CoinJoin history found.")
        return

    if csv_output:
        import csv as csv_module
        import sys

        fieldnames = [
            "timestamp",
            "role",
            "txid",
            "cj_amount",
            "peer_count",
            "net_fee",
            "success",
        ]
        writer = csv_module.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {
                    "timestamp": entry.timestamp,
                    "role": entry.role,
                    "txid": entry.txid,
                    "cj_amount": entry.cj_amount,
                    "peer_count": entry.peer_count if entry.peer_count is not None else "",
                    "net_fee": entry.net_fee,
                    "success": entry.success,
                }
            )
    else:
        if wallet_fp is not None:
            print(f"\nCoinJoin History for wallet {wallet_fp} ({len(entries)} entries):")
        else:
            print(f"\nCoinJoin History ({len(entries)} entries):")
        print("=" * 140)
        header = f"{'Timestamp':<20} {'Role':<7} {'Amount':>12} {'Peers':>6}"
        header += f" {'Net Fee':>12} {'TXID':<64}"
        print(header)
        print("-" * 140)

        # Display in chronological order (oldest at top, most recent at
        # bottom) so a terminal scrolling downward shows the latest entry
        # last -- matching the natural reading order for a log. ``entries``
        # comes from ``read_history`` sorted newest-first (and already
        # truncated by ``--limit`` to the most recent N), so reverse here.
        for entry in reversed(entries):
            # Distinguish between pending, failed, and successful transactions
            if entry.success:
                status = ""
            elif entry.confirmations == 0 and entry.failure_reason == "Pending confirmation":
                status = " [PENDING]"
            else:
                status = " [FAILED]"
            txid_full = entry.txid if entry.txid else "N/A"
            fee_str = f"{entry.net_fee:+,}" if entry.net_fee != 0 else "0"
            peer_str = str(entry.peer_count) if entry.peer_count is not None else "?"

            print(
                f"{entry.timestamp[:19]:<20} {entry.role:<7} {entry.cj_amount:>12,} "
                f"{peer_str:>6} {fee_str:>12} {txid_full:<64}{status}"
            )

        print("=" * 140)
