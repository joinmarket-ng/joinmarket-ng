"""
UTXO freeze/unfreeze commands.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from jmcore.cli_common import (
    ResolvedBackendSettings,
    resolve_backend_settings,
    resolve_mnemonic,
    setup_cli,
    suspend_stderr_logging,
)
from loguru import logger

from jmwallet.cli import app
from jmwallet.utxo_tui import (
    _clip,
    _draw_footer,
    _draw_frame_filler,
    _draw_help_overlay,
    _draw_hrule,
    _header_line,
    _help_overlay_rows,
    _is_address_reused,
    _minimum_table_width,
    _table_columns,
    _table_sep_width,
    adjust_scroll,
    align_control_columns,
    build_display_items,
    draw_resize_prompt,
    draw_status_block,
    enrich_utxo_labels,
    format_utxo_line,
    group_separator_parts,
    maker_bot_status,
    seek_selectable,
    status_header_lines,
    utxo_display_sort_key,
    utxo_status_text,
    wait_for_help_close,
    wrap_status_text,
)
from jmwallet.wallet.rows import FREEZE_STATE_TOKENS

if TYPE_CHECKING:
    import curses

    from jmwallet.wallet.models import UTXOInfo
    from jmwallet.wallet.rows import StateToken
    from jmwallet.wallet.service import WalletService


@app.command()
def freeze(
    mnemonic_file: Annotated[
        Path | None,
        typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file", envvar="MNEMONIC_FILE"),
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--prompt-bip39-passphrase",
            help="Prompt for BIP39 passphrase interactively",
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
    mixdepth: Annotated[
        int | None,
        typer.Option("--mixdepth", "-m", help="Filter to a specific configured mixdepth"),
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
    """Interactively freeze/unfreeze UTXOs to exclude them from coin selection.

    Opens a TUI where you can toggle the frozen state of individual UTXOs.
    Frozen UTXOs are persisted in BIP-329 format and excluded from all
    automatic coin selection (taker, maker, and sweep operations).
    Changes take effect immediately on each toggle.

    Fidelity bonds (locked or expired) can be frozen like any other UTXO:
    freezing a still-locked bond keeps it out of the sweep fallback and coin
    control once its timelock expires. Bonds are tagged fb-a/
    fb-x in the Label column; "freeze all"/"unfreeze all" include them.
    """
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
        resolved_mnemonic = resolved.mnemonic
        resolved_bip39_passphrase = resolved.bip39_passphrase
        resolved_creation_height = resolved.creation_height
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    backend = resolve_backend_settings(
        settings,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        neutrino_url=neutrino_url,
        data_dir=data_dir,
    )

    asyncio.run(
        _freeze_utxos(
            resolved_mnemonic,
            backend,
            resolved_bip39_passphrase,
            mixdepth_filter=mixdepth,
            creation_height=resolved_creation_height,
            mnemonic_file=resolved.mnemonic_file,
            mixdepth_count=settings.wallet.mixdepth_count,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
            reconstruct_history=settings.wallet.reconstruct_history,
        )
    )


async def _freeze_utxos(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
    mixdepth_filter: int | None = None,
    *,
    creation_height: int | None = None,
    mnemonic_file: Path | None = None,
    mixdepth_count: int = 5,
    max_sats_freeze_reuse: int = -1,
    reconstruct_history: bool = True,
) -> None:
    """Interactive UTXO freeze/unfreeze implementation."""
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.service import WalletService

    network = backend_settings.network
    backend_type = backend_settings.backend_type
    data_dir = backend_settings.data_dir

    # The wallet name is derived from the master fingerprint. Registered
    # fidelity bonds are loaded and imported by ``sync_with_registered_bonds``
    # below, so they do not need to be collected here.
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

    wallet_fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase or "")

    # Create backend
    backend: DescriptorWalletBackend | NeutrinoBackend
    if backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=backend_settings.neutrino_url,
            network=network,
            scan_start_height=backend_settings.scan_start_height,
            add_peers=backend_settings.neutrino_add_peers,
            tls_cert_path=backend_settings.neutrino_tls_cert,
            auth_token=backend_settings.neutrino_auth_token,
        )
        logger.info("Waiting for neutrino to sync...")
        synced = await backend.wait_for_sync(timeout=300.0)
        if not synced:
            logger.error("Neutrino sync timeout")
            raise typer.Exit(1)
    elif backend_type == "descriptor_wallet":
        from jmwallet.backends.descriptor_wallet import generate_wallet_name

        wallet_name = generate_wallet_name(wallet_fingerprint, network)
        backend = DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
            scan_start_height=backend_settings.scan_start_height,
            scan_lookback_blocks=backend_settings.scan_lookback_blocks,
        )
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")

    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=mixdepth_count,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
        reconstruct_history=reconstruct_history,
        mnemonic_file=mnemonic_file,
    )

    try:
        # Bond-aware sync (same path as the daemon and ``jm-wallet info``):
        # imports any registered fidelity bond's watch-only ``addr()``
        # descriptor into Bitcoin Core (and rescans) when missing, so a bond
        # funded after the base wallet was set up is visible (and freezable).
        # Detection is by the actual ``addr()`` descriptor set, not a
        # descriptor count. Non-descriptor backends (neutrino) scan the bond
        # addresses directly inside this call.
        await wallet.sync_with_registered_bonds()

        # Collect all UTXOs (including frozen ones) across requested mixdepths
        all_utxos: list[UTXOInfo] = []
        if mixdepth_filter is not None:
            if mixdepth_filter < 0 or mixdepth_filter >= wallet.mixdepth_count:
                print(f"Error: mixdepth must be 0-{wallet.mixdepth_count - 1}")
                raise typer.Exit(1)
            all_utxos = wallet.utxo_cache.get(mixdepth_filter, [])
        else:
            for md in range(wallet.mixdepth_count):
                all_utxos.extend(wallet.utxo_cache.get(md, []))

        # Same label enrichment the selector's caller (send.py) runs:
        # utxo_cache items carry no wallet labels, so without this the tag
        # column stays empty for every regular UTXO.
        enrich_utxo_labels(wallet.get_utxo_label_from_wallet, all_utxos)

        if not all_utxos:
            md_msg = f" in mixdepth {mixdepth_filter}" if mixdepth_filter is not None else ""
            print(f"No UTXOs found{md_msg}.")
            return

        # Sort by display order (mixdepth, branch, then derivation path with
        # numeric components so indices >= 10 order correctly), matching the
        # interactive UTXO selector; branch groups stay contiguous this way.
        all_utxos.sort(key=utxo_display_sort_key)

        # Build the display list grouped by mixdepth/branch with labelled
        # separators (external/internal/bond groups).
        display_items = build_display_items(all_utxos, branch_groups=True)

        # Check terminal
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            # Non-interactive: just show frozen status
            _show_freeze_status(all_utxos)
            return

        # Wallet identity for the status block (whiptail menu_header style):
        # the mnemonic file name is the wallet's user-visible name.
        wallet_display_name = (
            wallet.mnemonic_file.name if wallet.mnemonic_file is not None else "(no file)"
        )

        # Launch interactive TUI. Curses owns the terminal now: all log
        # output is suspended (see suspend_stderr_logging); freeze errors
        # are shown in the TUI's own footer note row.
        import curses

        with suspend_stderr_logging():
            curses.wrapper(_run_freeze_tui, display_items, wallet, wallet_display_name)

        # Show summary after TUI exit
        frozen_count = sum(1 for u in all_utxos if u.frozen)
        total = len(all_utxos)
        print(f"\n{frozen_count}/{total} UTXO(s) frozen.")

    finally:
        await wallet.close()


def _show_freeze_status(utxos: list[UTXOInfo]) -> None:
    """Show freeze status for non-interactive mode (no terminal)."""
    from jmcore.bitcoin import format_amount

    current_md = -1
    for utxo in utxos:
        if utxo.mixdepth != current_md:
            current_md = utxo.mixdepth
            print(f"\nMixdepth {current_md}:")

        frozen_tag = " [FROZEN]" if utxo.frozen else ""
        fb_tag = ""
        if utxo.is_fidelity_bond:
            fb_tag = " [FB-LOCKED]" if utxo.is_locked else " [FB]"

        print(
            f"  {utxo.txid[:12]}...:{utxo.vout:<3} "
            f"{format_amount(utxo.value):>18} "
            f"{utxo.confirmations:>6} conf"
            f"{fb_tag}{frozen_tag}"
        )


def _unfreeze_utxos(wallet: WalletService, utxos: list[UTXOInfo]) -> int:
    """Unfreeze every frozen UTXO, fidelity bonds included.

    A frozen still-locked bond is frozen deliberately: the flag keeps it out
    of the sweep fallback and coin control once its timelock expires, so
    "unfreeze all" clears it like any other frozen flag.

    Returns:
        Number of UTXOs unfrozen.
    """
    unfrozen_count = 0

    for utxo in utxos:
        if not utxo.frozen:
            continue
        wallet.toggle_freeze_utxo(utxo.outpoint)
        unfrozen_count += 1

    return unfrozen_count


# Building the display list is shared with the interactive UTXO selector;
# the alias is kept under the old name for tests (mixdepth-only grouping).
_build_display_items = build_display_items


def _seek_selectable(display_items: list[UTXOInfo | None], start: int, direction: int) -> int:
    """Return the nearest selectable index from ``start``.

    Skips only ``None`` separators: every UTXO (fidelity bonds included) is
    toggleable in the freeze manager.
    """
    return seek_selectable(display_items, start, direction, lambda _: True)


_FREEZE_CONTROLS_HINT = (
    "Space/Tab: toggle | j/k: navigate | a: freeze all | n: unfreeze all | ?: help | q/Esc: exit"
)


def _freeze_state_col(utxo: UTXOInfo) -> StateToken:
    """State-column value of a UTXO in the freeze manager.

    The freeze manager is a SUBSET of the shared catalog
    (:mod:`jmwallet.wallet.rows`): only ``frozn``/``lock``/``spend`` are
    reachable here. The selection-context tokens (``in-use``, ``no-cj``,
    ``immat``, ``oth-md``, ``unconf``, ``pend``) have no meaning in
    this TUI -- every UTXO is toggleable and no selection session exists.
    A frozen still-locked bond shows ``frozn`` (the user lock wins over
    the timelock); the timelock stays visible via the ``[!]`` mark and the
    ``fb-a`` tag.
    """
    if utxo.frozen:
        return "frozn"
    if utxo.is_fidelity_bond and utxo.is_locked:
        return "lock"
    return "spend"


_HELP_TITLE = " — UTXO Freeze Manager Help Screen —"


def _draw_header(
    stdscr: curses.window,
    width: int,
    columns: list[tuple[str, int]],
    status_lines: list[str],
) -> str:
    """Draw the title bar, status block and table header.

    The status block sits between the title rule and the header top edge
    (whiptail ``menu_header`` style: wallet name, fingerprint, maker bot
    status). ``status_lines`` is the prebuilt 1- or 2-line cascade from
    :func:`status_header_lines`; the returned header line sizes the row
    separators and right borders.
    """
    import curses

    title = " — UTXO Freeze Manager —"
    stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
    stdscr.addstr(1, 0, title.center(width)[:width])
    stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)

    header_line = _header_line(columns)
    # Geometry: rule(2), status rows(3..2+R), block bottom rule(3+R),
    # header(4+R), header bottom rule(5+R), list from 6+R.
    header_row = 4 + len(status_lines)
    stdscr.addstr(header_row, 0, header_line[:width])
    if "RU!" in header_line:
        # Overdraw the flush RU! header cell in the warning color.
        ru_col = header_line.index("RU!")
        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
        stdscr.addstr(header_row, ru_col, "RU!")
        stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)
    header_sep_width = min(len(header_line) - 1, width - 1)
    # The box's top edge: a rule between the title and the status block.
    _draw_hrule(stdscr, 2, header_sep_width)
    status_block_end = draw_status_block(stdscr, 3, status_lines, header_sep_width)
    # The header's own top edge below the status block.
    _draw_hrule(stdscr, 3 + status_block_end, header_sep_width)
    _draw_hrule(stdscr, header_row + 1, header_sep_width)
    return header_line


def _draw_utxo_rows(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    cursor_pos: int,
    scroll_offset: int,
    list_height: int,
    list_start: int,
    width: int,
    sep_width: int,
    address_utxo_counts: dict[str, int],
    columns: list[tuple[str, int]],
) -> None:
    """Draw the visible UTXO rows with bordered separators, freeze marks and colors.

    Marks: [F] frozen, [!] an unfrozen fidelity bond, [ ] anything else. A
    frozen bond shows [F]; its Label/State columns still describe the bond.
    Rows share the selector's layout and palette (yellow cursor, bright red
    frozen and still-timelocked bonds, magenta expired bonds, RU! badges
    behind the right border).
    """
    import curses

    for i, item in enumerate(display_items):
        if i < scroll_offset or i >= scroll_offset + list_height:
            continue
        display_row = list_start + (i - scroll_offset)

        # Two consecutive None entries encode a blank border row followed by
        # the labelled separator of a new mixdepth; a single None is a
        # branch-only separator.
        if item is None:
            stdscr.addstr(display_row, 0, "|", curses.A_NORMAL)
            inner_width = sep_width - 1
            if i + 1 < len(display_items) and display_items[i + 1] is None:
                # Blank border-only row above a mixdepth title.
                pass
            else:
                parts = group_separator_parts(display_items, i)
                if parts is not None:
                    md_part, dash_sep, branch_part, full_title = parts
                    col = 2
                    if not full_title:
                        # Branch change: dashes strike out the "Mixdepth N"
                        # part; the branch name keeps its column and color.
                        stdscr.addstr(display_row, col, "-" * len(md_part), curses.A_NORMAL)
                        col += len(md_part)
                    else:
                        stdscr.addstr(
                            display_row, col, md_part, curses.color_pair(3) | curses.A_BOLD
                        )
                        col += len(md_part)
                    stdscr.addstr(display_row, col, dash_sep, curses.color_pair(3))
                    col += len(dash_sep)
                    if col + len(branch_part) + 1 <= sep_width - 1:
                        stdscr.addstr(display_row, col, f"{branch_part} ", curses.color_pair(3))
                        col += len(branch_part) + 1
                    stdscr.addstr(display_row, col, "-" * (sep_width - 1 - col), curses.A_NORMAL)
                else:
                    stdscr.addstr(display_row, 1, "-" * inner_width, curses.A_NORMAL)
            stdscr.addstr(display_row, sep_width, "|", curses.A_NORMAL)
            continue

        if item.frozen:
            mark = "[F]"
        elif item.is_fidelity_bond:
            mark = "[!]"
        else:
            mark = "[ ]"
        line = f"| {mark} | " + format_utxo_line(
            item,
            width,
            None,
            width,
            columns=columns,
            mark_title="F",
            state_override=_freeze_state_col(item),
            reused=_is_address_reused(item, address_utxo_counts),
        )

        if i == cursor_pos:
            attr = curses.color_pair(2) | curses.A_REVERSE
        elif item.frozen or item.is_locked:
            # Frozen or still-timelocked bond: both are immutable from this
            # TUI, so both signal stop in bright red like the selector.
            attr = curses.color_pair(4) | curses.A_BOLD
        elif item.is_fidelity_bond:
            # Expired bond: spendable, but its address is published on the
            # network, so only magenta like the selector's unlocked bonds.
            attr = curses.color_pair(5)
        else:
            attr = curses.color_pair(1)  # Spendable UTXOs stay green

        stdscr.addstr(display_row, 0, "|", curses.A_NORMAL)
        stdscr.addstr(display_row, 1, line[1:-1], attr)
        stdscr.addstr(display_row, len(line) - 1, "|", curses.A_NORMAL)
        if _is_address_reused(item, address_utxo_counts):
            # Overdraw the flush RU! cell (inside the border) in red bold.
            ru_col = len(line) - 4
            stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
            stdscr.addstr(display_row, ru_col, "RU!")
            stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)


def _format_freeze_footer_stats(utxo_items: list[UTXOInfo]) -> str:
    """Build the footer statistics lines for the freeze TUI.

    Two lines, newline-separated; regular UTXOs and fidelity bonds are
    never mixed. Line 1 covers non-bond UTXOs (Total | Frozen | Spendable)
    and is labeled ``Total(w/o FBs)`` when bonds exist, plain ``Total``
    when they don't. Line 2 covers fidelity bonds and is omitted entirely
    when the wallet has none: Spendable counts expired, unfrozen bonds
    (the sweep fallback and coin control auto-select those), while
    Frozen/Locked collects every bond that is frozen or still timelocked.
    Each line's segments sum to that line's total.
    """

    def _utxo_word(count: int) -> str:
        return "UTXOs" if count != 1 else "UTXO"

    def _format_category(value: int, count: int) -> str:
        """Format category with value and optional UTXO count in parentheses."""
        if value == 0:
            return f"{value:,} sats"
        return f"{value:,} sats ({count} {_utxo_word(count)})"

    def _stats(us: list[UTXOInfo]) -> tuple[int, int]:
        return (sum(u.value for u in us), len(us))

    regular = [u for u in utxo_items if not u.is_fidelity_bond]
    bonds = [u for u in utxo_items if u.is_fidelity_bond]

    total_value, total_count = _stats(regular)
    frozen_value, frozen_count = _stats([u for u in regular if u.frozen])
    spendable_value, spendable_count = _stats([u for u in regular if not u.frozen])

    total_label = "Total(w/o FBs)" if bonds else "Total"
    lines = [
        f" {total_label}: {_format_category(total_value, total_count)} | "
        f"Frozen: {_format_category(frozen_value, frozen_count)} | "
        f"Spendable: {_format_category(spendable_value, spendable_count)}"
    ]

    if bonds:
        fb_total_value, fb_total_count = _stats(bonds)
        fb_unavailable_value, fb_unavailable_count = _stats(
            [u for u in bonds if u.frozen or u.is_locked]
        )
        fb_spendable_value, fb_spendable_count = _stats(
            [u for u in bonds if not u.frozen and not u.is_locked]
        )
        lines.append(
            f" Fidelity Bonds: {_format_category(fb_total_value, fb_total_count)} | "
            f"Frozen/Locked: {_format_category(fb_unavailable_value, fb_unavailable_count)} | "
            f"Spendable: {_format_category(fb_spendable_value, fb_spendable_count)}"
        )

    return "\n".join(lines)


def _freeze_footer_rows(
    sep_width: int,
    utxo_items: list[UTXOInfo],
    cursor_item: UTXOInfo | None,
    address_utxo_counts: dict[str, int],
    error_message: str | None,
    error_active: bool,
) -> list[tuple[str, int]]:
    """Lay out the footer sections before reserving space for the coin rows.

    Freeze Details holds the per-category statistics (wrapped between
    fields), UTXO Status describes the coin under the cursor (wrapped the
    same way), and the note row carries an active error or an RU! warning
    (reserved even when empty so cursor moves do not resize the list).
    ``sep_width`` is the table's separator span
    (:func:`jmwallet.utxo_tui._table_sep_width`): the footer box spans the
    table's full width -- framed separators and framed text rows, borders
    at columns 0 and ``sep_width``.
    """
    import curses

    content_width = max(1, sep_width - 4)
    separator = ("|" + "-" * (sep_width - 1) + "|", curses.A_NORMAL)
    title_attr = curses.color_pair(3) | curses.A_BOLD
    # The first separator closes the table's bottom edge (the frame filler
    # keeps the border columns connected above it).
    rows = [separator, ("Freeze Details", title_attr)]
    for stats_line in _format_freeze_footer_stats(utxo_items).split("\n"):
        rows.extend(
            (line, curses.A_BOLD) for line in wrap_status_text(stats_line.strip(), content_width)
        )
    rows.extend([separator, ("UTXO Status", title_attr)])
    status_text = ""
    if cursor_item is not None:
        # Shared payload: "Outpoint:" gets its own line (forced break, no
        # separator before the value); the value moves whole below the
        # label when the line is too wide (never broken mid-value).
        status_text = utxo_status_text(cursor_item)
    # The status block shows exactly the cursor coin's payload -- no
    # max-reserve padding over all coins: the padding rendered as a visible
    # empty line whenever the cursor coin wrapped shorter than the wallet
    # maximum (e.g. the one-column window where a 2-digit-vout outpoint
    # tips the wrap threshold), and it flipped around on resize
    # (user decision 2026-11-15). The footer height may breathe by one line
    # when coins of different status heights alternate under the cursor;
    # that cost is accepted (RU!/error note stays conditional, see below).
    status_lines = wrap_status_text(status_text, content_width)
    rows.extend((line, curses.A_BOLD) for line in status_lines)
    # The note row exists only when there is something to show (active error
    # or RU! warning); an always-reserved empty row wasted a list line in the
    # overwhelmingly common clean case (user decision 2026-11-15).
    note = ""
    if error_active and error_message:
        note = f"ERROR: {error_message}"
    elif cursor_item is not None and _is_address_reused(cursor_item, address_utxo_counts):
        note = "RU! Warning: Address reuse reduces privacy"
    if note:
        rows.append((_clip(note, content_width), curses.color_pair(4) | curses.A_BOLD))
    rows.extend([separator, ("Controls", title_attr)])
    rows.extend(
        (line, curses.A_NORMAL)
        for line in align_control_columns(_FREEZE_CONTROLS_HINT, content_width)
    )
    # The closing separator is the box's bottom edge; the render loop keeps
    # the terminal's last row empty below it (a one-cell black frame).
    rows.append(separator)
    return rows


def _render_freeze_screen(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    cursor_pos: int,
    scroll_offset: int,
    error_message: str | None,
    error_active: bool,
    wallet: WalletService,
    wallet_name: str,
) -> tuple[int, int, str]:
    """Render the whole freeze screen.

    Returns ``(scroll_offset, list_height, resize_reason)``. With too
    little room for the framed layout nothing is drawn, the list height
    is 0 and ``resize_reason`` names the dimension to enlarge
    (``"width"``/``"height"``); the event loop then shows the matching
    resize prompt.
    """
    stdscr.clear()
    height, width = stdscr.getmaxyx()
    utxo_items = [u for u in display_items if u is not None]
    columns = _table_columns(width, utxo_items, "F", state_tokens=FREEZE_STATE_TOKENS)

    # Status block above the column titles (whiptail menu_header style).
    # The maker status is process-derived (start-path independent); the
    # wallet identity comes precomputed from the TUI runner (name/fingerprint).
    sep_width = _table_sep_width(columns)
    status_lines = status_header_lines(
        sep_width,
        wallet_name,
        wallet.wallet_fingerprint,
        maker_bot_status(),
    )
    status_count = len(status_lines)

    # Address reuse detection: count UTXOs per address (shared RU! logic).
    address_utxo_counts: dict[str, int] = {}
    for item in utxo_items:
        address_utxo_counts[item.address] = address_utxo_counts.get(item.address, 0) + 1

    cursor_item = display_items[cursor_pos] if cursor_pos < len(display_items) else None
    footer_rows = _freeze_footer_rows(
        sep_width,
        utxo_items,
        cursor_item,
        address_utxo_counts,
        error_message,
        error_active,
    )

    list_start = 6 + status_count
    # Full layout widens the address column to fill the terminal exactly
    # (right border in the last usable column); the compact layout stays at
    # its own table width, so every border reads from the columns.
    # One blank row stays below the footer box (black frame at the
    # screen's bottom edge); it shrinks the scrollable region.
    list_height = height - list_start - len(footer_rows) - 1
    if width < max(80, _minimum_table_width(columns)):
        # The table itself cannot fit: the fix is horizontal space.
        return scroll_offset, 0, "width"
    if list_height < 1:
        # Width is fine but the vertical budget (title rules, status
        # block, footer, blank frame row) leaves no list row. First try
        # the one-line status block before blaming the terminal height:
        # a screen that rendered before the status block must keep
        # rendering with it.
        if status_count > 1:
            retry = status_header_lines(
                sep_width,
                wallet_name,
                wallet.wallet_fingerprint,
                maker_bot_status(),
                max_lines=1,
            )
            retry_height = height - (6 + len(retry)) - len(footer_rows) - 1
            if retry_height >= 1:
                status_lines = retry
                status_count = len(retry)
                list_start = 6 + status_count
                list_height = retry_height
            else:
                return scroll_offset, 0, "height"
        else:
            return scroll_offset, 0, "height"

    # Keep the cursor on screen.
    scroll_offset = adjust_scroll(cursor_pos, scroll_offset, list_height, len(display_items))

    _draw_header(stdscr, width, columns, status_lines)
    # The frame filler keeps the border columns connected across the spare
    # list rows (rows drawn later win over the filler borders); the footer's
    # own first separator row doubles as its top edge.
    # One blank row stays below the footer box (black frame at the screen's
    # bottom edge); it also keeps the terminal's unwritable bottom-right
    # corner out of the box entirely.
    footer_start = height - len(footer_rows) - 1
    _draw_frame_filler(stdscr, list_start, footer_start, sep_width)
    _draw_utxo_rows(
        stdscr,
        display_items,
        cursor_pos,
        scroll_offset,
        list_height,
        list_start,
        width,
        sep_width,
        address_utxo_counts,
        columns,
    )
    _draw_footer(stdscr, footer_start, footer_rows, sep_width)
    stdscr.refresh()
    return scroll_offset, list_height, ""


@dataclass
class _FreezeTUIState:
    """Mutable cursor/error state for the freeze TUI event loop."""

    cursor_pos: int = 0
    scroll_offset: int = 0
    error_message: str | None = None
    error_display_until: float = 0.0

    def set_error(self, message: str, duration: float = 5.0) -> None:
        import time

        self.error_message = message
        self.error_display_until = time.monotonic() + duration


def _toggle_selected_utxo(
    state: _FreezeTUIState, display_items: list[UTXOInfo | None], wallet: WalletService
) -> None:
    """Toggle freeze on the UTXO under the cursor and advance the cursor."""
    utxo = display_items[state.cursor_pos]
    if utxo is None:
        return

    # Fidelity bonds toggle like any other UTXO, including still-locked ones:
    # a frozen locked bond stays out of the sweep fallback and coin control
    # once its timelock expires.
    try:
        wallet.toggle_freeze_utxo(utxo.outpoint)
    except OSError as e:
        state.set_error(f"Failed to persist freeze state: {e}")

    # Move cursor down to the next selectable item after the action.
    if state.cursor_pos < len(display_items) - 1:
        state.cursor_pos = _seek_selectable(display_items, state.cursor_pos + 1, 1)


def _freeze_all_utxos(
    state: _FreezeTUIState, display_items: list[UTXOInfo | None], wallet: WalletService
) -> None:
    """Freeze every unfrozen UTXO, fidelity bonds included."""
    try:
        for item in display_items:
            if item is None or item.frozen:
                continue
            wallet.toggle_freeze_utxo(item.outpoint)
    except OSError as e:
        state.set_error(f"Failed to persist freeze state: {e}")


def _unfreeze_all_utxos(
    state: _FreezeTUIState, display_items: list[UTXOInfo | None], wallet: WalletService
) -> None:
    """Unfreeze every frozen UTXO, fidelity bonds included."""
    try:
        utxo_items = [u for u in display_items if u is not None]
        _unfreeze_utxos(wallet, utxo_items)
    except OSError as e:
        state.set_error(f"Failed to persist freeze state: {e}")


def _handle_freeze_key(
    key: int,
    state: _FreezeTUIState,
    display_items: list[UTXOInfo | None],
    wallet: WalletService,
    list_height: int,
) -> bool:
    """Apply a key press to ``state``. Return ``True`` to keep running.

    Returning ``False`` signals the caller to exit the event loop.
    """
    import curses

    last = len(display_items) - 1

    if key == ord("q") or key == 27:  # q or Escape
        return False
    elif key == ord(" ") or key == ord("\t"):  # toggle
        _toggle_selected_utxo(state, display_items, wallet)
    elif key == curses.KEY_UP or key == ord("k"):
        state.cursor_pos = _seek_selectable(display_items, state.cursor_pos - 1, -1)
    elif key == curses.KEY_DOWN or key == ord("j"):
        state.cursor_pos = _seek_selectable(display_items, state.cursor_pos + 1, 1)
    elif key == curses.KEY_PPAGE:  # Page Up
        state.cursor_pos = _seek_selectable(
            display_items, max(0, state.cursor_pos - list_height), -1
        )
    elif key == curses.KEY_NPAGE:  # Page Down
        state.cursor_pos = _seek_selectable(
            display_items, min(last, state.cursor_pos + list_height), 1
        )
    elif key == ord("g"):  # Go to top
        state.cursor_pos = _seek_selectable(display_items, 0, 1)
    elif key == ord("G"):  # Go to bottom
        state.cursor_pos = _seek_selectable(display_items, last, -1)
    elif key == ord("a"):  # Freeze all
        _freeze_all_utxos(state, display_items, wallet)
    elif key == ord("n"):  # Unfreeze all
        _unfreeze_all_utxos(state, display_items, wallet)

    return True


def _run_freeze_tui(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    wallet: WalletService,
    wallet_name: str,
) -> None:
    """Run the curses-based UTXO freeze/unfreeze TUI.

    Changes are persisted immediately on each toggle via wallet.toggle_freeze_utxo().

    Args:
        stdscr: The curses window.
        display_items: All UTXOs to display with labelled separators between
        mixdepth/branch groups.
        wallet: WalletService instance for persisting freeze state.
    """
    import curses
    import time

    curses.curs_set(0)
    curses.use_default_colors()

    # Palette aligned with the interactive UTXO selector; green stays the
    # "healthy" spendable color and red marks frozen coins, errors, RU!.
    curses.init_pair(1, curses.COLOR_GREEN, -1)  # Spendable UTXOs
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # Current cursor
    curses.init_pair(3, curses.COLOR_CYAN, -1)  # Title bar and group titles
    curses.init_pair(4, curses.COLOR_RED, -1)  # Frozen rows (bold), errors and RU! warnings
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # Unfrozen fidelity bonds

    state = _FreezeTUIState()
    # Land the cursor on the first selectable (toggleable, non-separator) item
    # so it never starts on a mixdepth separator or a non-toggleable fidelity
    # bond (e.g. when mixdepth 0 holds only a bond).
    state.cursor_pos = _seek_selectable(display_items, 0, 1)

    while True:
        error_active = bool(state.error_message) and time.monotonic() < state.error_display_until
        if not error_active:
            state.error_message = None

        try:
            state.scroll_offset, list_height, resize_reason = _render_freeze_screen(
                stdscr,
                display_items,
                state.cursor_pos,
                state.scroll_offset,
                state.error_message,
                error_active,
                wallet,
                wallet_name,
            )
        except curses.error:
            # Do not dispatch freeze actions after an interrupted repaint.
            # The cause of the interruption is not diagnosable here, so the
            # generic prompt text stays.
            draw_resize_prompt(stdscr, stdscr.getmaxyx()[1])
            if stdscr.getch() in (ord("q"), 27):
                return
            continue
        if resize_reason:
            # Too little room for the framed layout, with the failing
            # dimension named: show the shared resize prompt and ignore
            # all input except q/Escape.
            draw_resize_prompt(stdscr, stdscr.getmaxyx()[1], resize_reason)
            if stdscr.getch() in (ord("q"), 27):
                return
            continue

        key = stdscr.getch()
        if key == ord("?"):
            _, width = stdscr.getmaxyx()
            columns = _table_columns(
                width,
                [u for u in display_items if u is not None],
                "F",
                state_tokens=FREEZE_STATE_TOKENS,
            )
            sep_width = _table_sep_width(columns)
            _draw_help_overlay(
                stdscr, _help_overlay_rows(sep_width, FREEZE_STATE_TOKENS), sep_width, _HELP_TITLE
            )
            wait_for_help_close(stdscr)
            continue
        if not _handle_freeze_key(key, state, display_items, wallet, list_height):
            return
