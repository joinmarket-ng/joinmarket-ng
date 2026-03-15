#!/usr/bin/env python3
"""JAM-NG Flatpak control panel — GTK 3.

Lightweight GTK3 GUI launched by the entrypoint after all services are up.
Uses PyGObject (gi) which supports both X11 and Wayland natively, unlike
tkinter (Tk 8.6) which is X11-only.

Features:
  - Open Web UI button (xdg-open)
  - Log viewer with ANSI colour rendering and auto-refresh tailing
  - Network switcher (writes restart request + sends SIGTERM to entrypoint)
  - Quit button (sends SIGTERM to parent entrypoint)
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

import gi  # type: ignore[import-not-found]

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore[import-not-found]  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("JOINMARKET_DATA_DIR", Path.home() / ".joinmarket-ng"))
LOG_DIR = DATA_DIR / "logs"
JMWALLETD_PORT = os.environ.get("JMWALLETD_PORT", "28183")
NETWORK = os.environ.get("NETWORK", "mainnet")
PIDFILE_DIR = DATA_DIR / "run"

ENTRYPOINT_PID = os.getppid()

NETWORKS = ("mainnet", "signet", "regtest")

LOG_SOURCES: dict[str, str] = {
    "jmwalletd": "jmwalletd.log",
    "Tor": "tor.log",
    "neutrino": "neutrino.log",
    "obwatcher": "obwatcher.log",
}

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

# Base palette — dark, minimal
BG = "#0f0f0f"
BG_SURFACE = "#1a1a1a"
BG_LOG = "#111111"
FG = "#c8c8c8"
FG_DIM = "#707070"
BORDER = "#2a2a2a"
ACCENT = "#4d9fff"
ACCENT_HOVER = "#6db3ff"
RED = "#e05555"
BITCOIN_ORANGE = "#f7931a"

NETWORK_COLORS: dict[str, str] = {
    "mainnet": BITCOIN_ORANGE,
    "signet": "#a366e0",
    "regtest": "#e05555",
}

# ANSI SGR code → GTK text-tag colour mapping (basic 8 colours + bright)
_ANSI_COLOR_MAP: dict[int, str] = {
    30: "#555555",  # black (dark grey)
    31: "#e05555",  # red
    32: "#50c878",  # green
    33: "#e0c050",  # yellow
    34: "#5588dd",  # blue
    35: "#c070d0",  # magenta
    36: "#40b8b8",  # cyan
    37: "#c8c8c8",  # white
    90: "#707070",  # bright black
    91: "#ff6b6b",  # bright red
    92: "#70e898",  # bright green
    93: "#ffe070",  # bright yellow
    94: "#70a0ff",  # bright blue
    95: "#e090f0",  # bright magenta
    96: "#60d8d8",  # bright cyan
    97: "#ffffff",  # bright white
}

# Regex that captures ANSI SGR sequences: ESC[...m
_ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")

TAIL_INTERVAL_MS = 2000
TAIL_MAX_BYTES = 200 * 1024

# ---------------------------------------------------------------------------
# CSS — minimal dark theme
# ---------------------------------------------------------------------------

CSS = f"""
* {{
    font-family: "Inter", "Cantarell", "Helvetica Neue", sans-serif;
}}

window {{
    background-color: {BG};
}}

.header-bar {{
    background-color: {BG_SURFACE};
    border-bottom: 1px solid {BORDER};
    padding: 10px 16px;
}}

.title {{
    color: {FG};
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}

.subtitle {{
    color: {FG_DIM};
    font-size: 11px;
}}

.network-badge {{
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.3px;
}}

.toolbar {{
    background-color: {BG};
    padding: 8px 16px;
}}

.btn-primary {{
    background: {ACCENT};
    color: white;
    border: none;
    border-radius: 4px;
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 500;
}}

.btn-primary:hover {{
    background: {ACCENT_HOVER};
}}

.btn-danger {{
    background: transparent;
    color: {RED};
    border: 1px solid {RED};
    border-radius: 4px;
    padding: 5px 14px;
    font-size: 12px;
    font-weight: 500;
}}

.btn-danger:hover {{
    background: {RED};
    color: white;
}}

.btn-flat {{
    background: transparent;
    color: {FG_DIM};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 10px;
    font-size: 11px;
}}

.btn-flat:hover {{
    color: {FG};
    border-color: {FG_DIM};
}}

.log-container {{
    background-color: {BG_LOG};
    border-top: 1px solid {BORDER};
}}

.log-toolbar {{
    background-color: {BG_SURFACE};
    padding: 6px 16px;
    border-bottom: 1px solid {BORDER};
}}

.log-toolbar label {{
    color: {FG_DIM};
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.3px;
}}

textview {{
    background-color: {BG_LOG};
    color: {FG};
    font-family: "JetBrains Mono", "Fira Code", "Source Code Pro", monospace;
    font-size: 10px;
}}

textview text {{
    background-color: {BG_LOG};
    color: {FG};
}}

.status-bar {{
    background-color: {BG_SURFACE};
    border-top: 1px solid {BORDER};
    padding: 4px 16px;
}}

.status-bar label {{
    color: {FG_DIM};
    font-size: 10px;
}}

combobox button {{
    background-color: {BG_SURFACE};
    color: {FG};
    border: 1px solid {BORDER};
    border-radius: 3px;
    padding: 2px 8px;
    font-size: 11px;
    min-height: 0;
}}

combobox button:hover {{
    border-color: {FG_DIM};
}}

label {{
    color: {FG};
}}

scrolledwindow {{
    background-color: {BG_LOG};
}}
""".strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS.encode("utf-8"))
    screen = Gdk.Screen.get_default()
    if screen is not None:
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def _tail_file(path: Path, max_bytes: int = TAIL_MAX_BYTES) -> str:
    """Read the last *max_bytes* of a file (raw, with ANSI codes intact)."""
    size = path.stat().st_size
    with open(path, errors="replace") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()  # skip partial first line
        return fh.read()


def _insert_ansi_text(buf: Any, text: str, tag_table: Any) -> None:
    """Insert text into a GtkTextBuffer, converting ANSI SGR codes to GTK tags."""
    # Track current style state
    current_fg: str | None = None
    bold = False

    pos = 0
    for m in _ANSI_RE.finditer(text):
        # Insert plain text before this escape sequence
        plain = text[pos : m.start()]
        if plain:
            _insert_styled(buf, plain, current_fg, bold, tag_table)
        pos = m.end()

        # Parse SGR parameters
        params_str = m.group(1)
        if not params_str:
            codes = [0]
        else:
            codes = [int(c) for c in params_str.split(";") if c.isdigit()]

        for code in codes:
            if code == 0:  # reset
                current_fg = None
                bold = False
            elif code == 1:  # bold
                bold = True
            elif code in _ANSI_COLOR_MAP:
                current_fg = _ANSI_COLOR_MAP[code]

    # Insert remaining text
    remaining = text[pos:]
    if remaining:
        _insert_styled(buf, remaining, current_fg, bold, tag_table)


def _insert_styled(
    buf: Any, text: str, fg: str | None, bold: bool, tag_table: Any
) -> None:
    """Insert *text* at the end of *buf* with optional colour/bold tags."""
    end_iter = buf.get_end_iter()
    if not fg and not bold:
        buf.insert(end_iter, text)
        return

    # Build a tag name like "fg_#e05555_bold" for caching
    tag_name = "ansi"
    if fg:
        tag_name += f"_{fg}"
    if bold:
        tag_name += "_b"

    tag = tag_table.lookup(tag_name)
    if tag is None:
        kwargs: dict[str, Any] = {}
        if fg:
            kwargs["foreground"] = fg
        if bold:
            from gi.repository import Pango  # type: ignore[import-not-found]

            kwargs["weight"] = Pango.Weight.BOLD
        tag = buf.create_tag(tag_name, **kwargs)

    buf.insert_with_tags(end_iter, text, tag)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class JamNGPanel(Gtk.Window):
    """Minimalist control panel."""

    def __init__(self) -> None:
        super().__init__(title="JAM-NG")
        self.set_default_size(680, 500)
        self.set_size_request(480, 360)
        self.connect("destroy", self._on_quit)

        self._current_log: str | None = None
        self._tail_source_id: int = 0
        # Incremental log tracking: avoid full re-render on every refresh.
        self._last_file_size: int = 0
        self._last_file_offset: int = 0
        # Scroll-position tracking: only auto-scroll if the user is at the bottom.
        self._scroll_adj: Gtk.Adjustment | None = None

        self._build_ui()
        self._load_log()

    # ---- UI build -----------------------------------------------------------

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(root)

        # -- Header bar (custom, not CSD) --
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header.get_style_context().add_class("header-bar")

        # Left: title + network badge
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title = Gtk.Label(label="JAM-NG")
        title.get_style_context().add_class("title")
        left.pack_start(title, False, False, 0)

        badge = Gtk.Label(label=NETWORK.upper())
        badge.get_style_context().add_class("network-badge")
        badge_color = NETWORK_COLORS.get(NETWORK, ACCENT)
        badge_css = Gtk.CssProvider()
        badge_css.load_from_data(
            f"label {{ background-color: {badge_color}; color: white; }}".encode()
        )
        badge.get_style_context().add_provider(
            badge_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        left.pack_start(badge, False, False, 0)

        header.pack_start(left, False, False, 0)

        # Right: Open UI + Quit
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        open_btn = Gtk.Button(label="Open Web UI")
        open_btn.get_style_context().add_class("btn-primary")
        open_btn.connect("clicked", self._on_open_browser)
        right.pack_start(open_btn, False, False, 0)

        quit_btn = Gtk.Button(label="Quit")
        quit_btn.get_style_context().add_class("btn-danger")
        quit_btn.connect("clicked", self._on_quit)
        right.pack_start(quit_btn, False, False, 0)

        header.pack_end(right, False, False, 0)
        root.pack_start(header, False, False, 0)

        # -- Network switcher toolbar --
        net_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        net_bar.get_style_context().add_class("toolbar")

        net_label = Gtk.Label(label="Network:")
        net_label.set_opacity(0.6)
        net_bar.pack_start(net_label, False, False, 0)

        self._net_combo = Gtk.ComboBoxText()
        for n in NETWORKS:
            self._net_combo.append_text(n)
        self._net_combo.set_active(NETWORKS.index(NETWORK))
        net_bar.pack_start(self._net_combo, False, False, 0)

        restart_btn = Gtk.Button(label="Switch & Restart")
        restart_btn.get_style_context().add_class("btn-flat")
        restart_btn.connect("clicked", self._on_switch_network)
        net_bar.pack_start(restart_btn, False, False, 0)

        # URL on the right
        url_label = Gtk.Label(label=f"http://127.0.0.1:{JMWALLETD_PORT}")
        url_label.set_opacity(0.4)
        net_bar.pack_end(url_label, False, False, 0)

        root.pack_start(net_bar, False, False, 0)

        # -- Log viewer --
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        log_box.get_style_context().add_class("log-container")

        # Log toolbar
        log_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        log_toolbar.get_style_context().add_class("log-toolbar")

        log_label = Gtk.Label(label="LOGS")
        log_toolbar.pack_start(log_label, False, False, 0)

        self._log_combo = Gtk.ComboBoxText()
        for name in LOG_SOURCES:
            self._log_combo.append_text(name)
        self._log_combo.set_active(0)
        self._log_combo.connect("changed", self._on_log_changed)
        log_toolbar.pack_start(self._log_combo, False, False, 0)

        log_box.pack_start(log_toolbar, False, False, 0)

        # Log text
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        self._log_view = Gtk.TextView()
        self._log_view.set_editable(False)
        self._log_view.set_cursor_visible(False)
        self._log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._log_view.set_left_margin(16)
        self._log_view.set_right_margin(16)
        self._log_view.set_top_margin(8)
        self._log_view.set_bottom_margin(8)
        scroll.add(self._log_view)
        self._scroll_adj = scroll.get_vadjustment()

        log_box.pack_start(scroll, True, True, 0)
        root.pack_start(log_box, True, True, 0)

        # -- Status bar --
        status = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        status.get_style_context().add_class("status-bar")
        self._status_label = Gtk.Label(
            label=f"Data: {DATA_DIR}  |  Logs: {LOG_DIR}", xalign=0.0
        )
        status.pack_start(self._status_label, True, True, 0)
        root.pack_start(status, False, False, 0)

    # ---- Actions ------------------------------------------------------------

    def _on_open_browser(self, _w: Any = None) -> None:
        url = f"http://127.0.0.1:{JMWALLETD_PORT}"
        try:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            self._status_label.set_text(f"Could not open browser. Visit {url}")

    def _on_quit(self, _w: Any = None) -> None:
        if self._tail_source_id:
            GLib.source_remove(self._tail_source_id)
            self._tail_source_id = 0
        try:
            os.kill(ENTRYPOINT_PID, signal.SIGTERM)
        except ProcessLookupError:
            pass
        Gtk.main_quit()

    def _on_switch_network(self, _w: Any = None) -> None:
        chosen = self._net_combo.get_active_text()
        if not chosen or chosen == NETWORK:
            return
        # Write the desired network so the entrypoint can re-exec with it
        restart_file = PIDFILE_DIR / "restart_network"
        try:
            restart_file.write_text(chosen)
        except OSError:
            self._status_label.set_text("Failed to write restart request")
            return
        self._status_label.set_text(f"Restarting with {chosen}...")
        # SIGTERM triggers cleanup → entrypoint checks restart file → re-execs
        if self._tail_source_id:
            GLib.source_remove(self._tail_source_id)
            self._tail_source_id = 0
        try:
            os.kill(ENTRYPOINT_PID, signal.SIGTERM)
        except ProcessLookupError:
            pass
        Gtk.main_quit()

    def _on_log_changed(self, _combo: Any = None) -> None:
        self._load_log()

    def _is_at_bottom(self) -> bool:
        """Check whether the log view is scrolled to (or near) the bottom."""
        if self._scroll_adj is None:
            return True
        adj = self._scroll_adj
        # "at bottom" means the current position + visible page is near the end.
        # Use a tolerance of 30px to account for rendering imprecision.
        return adj.get_value() + adj.get_page_size() >= adj.get_upper() - 30

    def _scroll_to_bottom(self) -> None:
        """Scroll the log view to the very bottom."""
        if self._scroll_adj is None:
            return
        adj = self._scroll_adj
        adj.set_value(adj.get_upper() - adj.get_page_size())

    def _load_log(self) -> None:
        name = self._log_combo.get_active_text()
        if not name:
            return
        filename = LOG_SOURCES.get(name)
        if not filename:
            return
        self._current_log = str(LOG_DIR / filename)
        # Reset incremental state — force a full reload on source switch.
        self._last_file_size = 0
        self._last_file_offset = 0
        self._refresh_log_text()

    def _refresh_log_text(self) -> bool:
        if self._current_log is None:
            return False

        path = Path(self._current_log)
        buf = self._log_view.get_buffer()
        tag_table = buf.get_tag_table()

        was_at_bottom = self._is_at_bottom()

        if not path.exists():
            buf.set_text("(log file not yet created)")
            self._last_file_size = 0
            self._last_file_offset = 0
        else:
            try:
                current_size = path.stat().st_size

                if current_size == self._last_file_size:
                    # File unchanged — nothing to do.
                    pass
                elif current_size < self._last_file_size or self._last_file_offset == 0:
                    # File was truncated/rotated OR this is the first load:
                    # do a full reload using the tail window.
                    buf.set_text("")
                    raw = _tail_file(path, max_bytes=TAIL_MAX_BYTES)
                    _insert_ansi_text(buf, raw, tag_table)
                    self._last_file_size = current_size
                    # The offset for next incremental read is the end of file.
                    self._last_file_offset = current_size
                    was_at_bottom = True  # pin to bottom on full reload
                else:
                    # File grew — append only the new bytes.
                    with open(path, errors="replace") as fh:
                        fh.seek(self._last_file_offset)
                        new_text = fh.read()
                    if new_text:
                        _insert_ansi_text(buf, new_text, tag_table)
                    self._last_file_size = current_size
                    self._last_file_offset = current_size

                    # Trim the buffer if it exceeds the tail window, to avoid
                    # unbounded memory growth during long sessions.
                    buf_text = buf.get_text(
                        buf.get_start_iter(), buf.get_end_iter(), True
                    )
                    if len(buf_text) > TAIL_MAX_BYTES * 2:
                        excess = len(buf_text) - TAIL_MAX_BYTES
                        # Find the first newline after the cut point for a clean trim.
                        nl = buf_text.find("\n", excess)
                        if nl == -1:
                            nl = excess
                        else:
                            nl += 1  # include the newline itself
                        start_iter = buf.get_start_iter()
                        cut_iter = buf.get_iter_at_offset(nl)
                        buf.delete(start_iter, cut_iter)

            except OSError:
                buf.set_text("(could not read log file)")
                self._last_file_size = 0
                self._last_file_offset = 0

        # Only auto-scroll to bottom if the user was already there.
        if was_at_bottom:
            # Defer the scroll slightly so the text view has laid out the new content.
            GLib.idle_add(self._scroll_to_bottom)

        # Schedule next refresh
        if self._tail_source_id:
            GLib.source_remove(self._tail_source_id)
        self._tail_source_id = GLib.timeout_add(
            TAIL_INTERVAL_MS, self._refresh_log_text
        )
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    _apply_css()
    win = JamNGPanel()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
