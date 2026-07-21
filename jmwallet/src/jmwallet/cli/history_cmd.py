"""
History command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from jmcore.cli_common import ResolvedBackendSettings, setup_cli
from loguru import logger

from jmwallet.cli import app
from jmwallet.cli._wallet_selection import resolve_wallet_fingerprint


@app.command()
def history(
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Max entries to show")] = None,
    role: Annotated[
        str | None,
        typer.Option("--role", "-r", help="Filter by role (maker/taker/send/deposit)"),
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
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
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
        HistoryRole,
        count_other_wallet_entries,
        get_history_stats,
        list_history_fingerprints,
        read_history,
    )

    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    role_filter: HistoryRole | None = None
    if role:
        if role.lower() not in ("maker", "taker", "send", "deposit"):
            logger.error("Role must be 'maker', 'taker', 'send', or 'deposit'")
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
            # Scope to the configured active wallet so a freshly created
            # wallet does not show another wallet's CoinJoins (issue #523).
            # The companion .meta fingerprint keeps this passwordless; only a
            # legacy wallet without it triggers a one-time decrypt. The
            # hidden-rows notice below keeps the scoping explicit, and
            # --all-wallets remains the escape hatch.
            fall_back_to_configured_mnemonic=True,
        )

    if stats:
        stats_data = get_history_stats(data_dir, wallet_fingerprint=wallet_fp)
        includes_reconstructed = any(
            entry.source == "onchain"
            for entry in read_history(data_dir, wallet_fingerprint=wallet_fp)
        )

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
        if includes_reconstructed:
            print("* Statistics include reconstructed role and fee estimates.\n")
        return

    entries = read_history(data_dir, limit, role_filter, wallet_fingerprint=wallet_fp)

    # Keep per-wallet scoping explicit: tell the user when rows from other
    # wallets (or legacy untagged rows) were excluded (issue #523).
    hidden = count_other_wallet_entries(
        data_dir, wallet_fingerprint=wallet_fp, role_filter=role_filter
    )

    if not entries:
        print("\nNo CoinJoin history found.")
        if hidden:
            print(
                f"({hidden} entries from other wallets are hidden; "
                "pass --all-wallets to show them.)"
            )
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
            "source",
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
                    "source": entry.source,
                }
            )
    else:
        if wallet_fp is not None:
            print(f"\nCoinJoin History for wallet {wallet_fp} ({len(entries)} entries):")
        else:
            print(f"\nCoinJoin History ({len(entries)} entries):")
        print("=" * 140)
        header = f"{'Timestamp':<20} {'Role':<8} {'Amount':>12} {'Peers':>6}"
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
            # Mark rows reconstructed from chain data (best-effort guesses).
            role_str = f"{entry.role}*" if entry.source == "onchain" else entry.role

            print(
                f"{entry.timestamp[:19]:<20} {role_str:<8} {entry.cj_amount:>12,} "
                f"{peer_str:>6} {fee_str:>12} {txid_full:<64}{status}"
            )

        print("=" * 140)
        if any(e.source == "onchain" for e in entries):
            print("* reconstructed from on-chain data (role and fees are best-effort guesses)")
        if hidden:
            print(
                f"{hidden} entries from other wallets are hidden; pass --all-wallets to show them."
            )


@app.command("reconstruct-history")
def reconstruct_history(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    max_transactions: Annotated[
        int,
        typer.Option(
            "--max-transactions",
            help="Safety cap on transactions classified in one pass",
            min=1,
        ),
    ] = 1000,
    keep_existing: Annotated[
        bool,
        typer.Option(
            "--keep-existing/--purge-existing",
            help=(
                "Keep previously reconstructed (on-chain) rows instead of "
                "purging and rebuilding them. Protocol-recorded rows are "
                "always kept either way."
            ),
        ),
    ] = False,
    network: Annotated[str | None, typer.Option("--network", "-n", help="Bitcoin network")] = None,
    backend_type: Annotated[
        str | None,
        typer.Option("--backend", "-b", help="Backend: descriptor_wallet | neutrino"),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    neutrino_url: Annotated[
        str | None, typer.Option("--neutrino-url", envvar="NEUTRINO_URL")
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Rebuild guessed CoinJoin/send/deposit history from on-chain data.

    Enumerates the wallet's confirmed transactions, classifies each with the
    equal-output CoinJoin heuristic (guessing role, fees, and peer count),
    and stores the result as history rows tagged ``source="onchain"``. Rows
    recorded at protocol time are never modified; transactions they already
    cover are skipped. By default previously reconstructed rows are purged
    first so the guessed portion is rebuilt from scratch.
    """
    import asyncio

    from jmcore.cli_common import resolve_backend_settings, resolve_mnemonic

    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    backend_settings = resolve_backend_settings(
        settings,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        neutrino_url=neutrino_url,
        data_dir=data_dir,
    )

    asyncio.run(
        _reconstruct_history(
            resolved.mnemonic,
            resolved.bip39_passphrase,
            backend_settings,
            creation_height=resolved.creation_height,
            max_transactions=max_transactions,
            keep_existing=keep_existing,
            gap_limit=settings.wallet.gap_limit,
            scan_range=settings.wallet.scan_range,
            mixdepth_count=settings.wallet.mixdepth_count,
        )
    )


async def _wait_for_complete_core_history(backend: Any) -> None:
    """Wait for complete Core wallet coverage before a destructive rebuild."""
    status = await backend.get_rescan_status()
    if not isinstance(status, dict):
        logger.error(
            "Could not determine Bitcoin Core rescan status; refusing to "
            "purge or reconstruct history from potentially partial data."
        )
        raise typer.Exit(1)
    if status.get("in_progress"):
        print("\nWaiting for the active Bitcoin Core rescan to complete...")
        if not await backend.wait_for_rescan_complete():
            logger.error("Bitcoin Core rescan did not complete; existing history was not changed.")
            raise typer.Exit(1)


def _require_neutrino_history_support(backend: Any) -> None:
    """Reject manual reconstruction when confirmed tx history is unavailable."""
    capabilities = backend.server_capabilities
    if capabilities.detected and capabilities.has_tx_enumeration:
        return
    logger.error(
        "The connected neutrino server does not provide confirmed transaction "
        "history. Upgrade to neutrino-api 1.4.0+ and enable tx history; "
        "existing history was not changed."
    )
    raise typer.Exit(1)


async def _reconstruct_history(
    mnemonic: str,
    bip39_passphrase: str,
    backend_settings: ResolvedBackendSettings,
    *,
    creation_height: int | None,
    max_transactions: int,
    keep_existing: bool,
    gap_limit: int,
    scan_range: int,
    mixdepth_count: int,
) -> None:
    """Implementation of ``jm-wallet reconstruct-history``."""
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.history import purge_reconstructed_entries
    from jmwallet.wallet.service import WalletService

    wallet_fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase)

    backend: DescriptorWalletBackend | NeutrinoBackend
    if backend_settings.backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=backend_settings.neutrino_url,
            network=backend_settings.network,
            scan_start_height=backend_settings.scan_start_height,
            add_peers=backend_settings.neutrino_add_peers,
            tls_cert_path=backend_settings.neutrino_tls_cert,
            auth_token=backend_settings.neutrino_auth_token,
        )
        logger.info("Waiting for neutrino to sync...")
        if not await backend.wait_for_sync(timeout=300.0):
            logger.error("Neutrino sync timeout")
            raise typer.Exit(1)
    elif backend_settings.backend_type == "descriptor_wallet":
        wallet_name = generate_wallet_name(wallet_fingerprint, backend_settings.network)
        backend = DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
        )
    else:
        raise ValueError(f"Unknown backend type: {backend_settings.backend_type}")

    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    if not getattr(backend, "supports_tx_enumeration", False):
        logger.error(
            "The configured backend cannot enumerate wallet transactions; "
            "history reconstruction requires Bitcoin Core (descriptor_wallet) "
            "or neutrino-api 1.4.0+."
        )
        raise typer.Exit(1)

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=backend_settings.network,
        mixdepth_count=mixdepth_count,
        gap_limit=gap_limit,
        scan_range=scan_range,
        passphrase=bip39_passphrase,
        data_dir=backend_settings.data_dir,
        # The command controls purge/rebuild ordering explicitly below.
        reconstruct_history=False,
    )

    try:
        # Sync first so the address cache covers the full descriptor range
        # (the reconstruction recognizes our coins via that cache).
        await wallet.sync_with_registered_bonds()

        if isinstance(backend, DescriptorWalletBackend):
            # First-time descriptor setup uses a recent smart scan and starts a
            # full rescan in the background. A forced reconstruction would
            # otherwise bypass the automatic deferral guard, purge the old
            # guessed rows, and persist only the recent partial history.
            await _wait_for_complete_core_history(backend)
        else:
            _require_neutrino_history_support(backend)

        if not keep_existing:
            purged = purge_reconstructed_entries(
                backend_settings.data_dir, wallet_fingerprint=wallet.wallet_fingerprint
            )
            if purged:
                print(f"Purged {purged} previously reconstructed entries.")

        created = await wallet.reconstruct_imported_history(
            force=True, max_transactions=max_transactions
        )
        print(f"\nReconstructed {created} history entries for wallet {wallet.wallet_fingerprint}.")
        if created:
            print("View them with: jm-wallet history (reconstructed rows are marked with *)")
    finally:
        await backend.close()
