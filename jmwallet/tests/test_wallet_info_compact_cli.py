"""Tests for the ``compact`` extended view (``--view compact``).

The 80-column variant of the full view: two lines per UTXO with the full,
copyable outpoint on the detail line. The CLI flag behavior (``--view``
requires ``--extended``, unknown values are rejected) is covered by
:mod:`tests.test_wallet_info_full_cli`; this module covers the compact
layout: line-width invariant, full outpoints, exact confirmation counts,
elided bond addresses, grid-aligned empty rows, hidden-bond notices, and
the uniform dash lines.
"""

from __future__ import annotations

import re
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.cli import wallet as wallet_cli
from jmwallet.cli.info_compact import (
    MAX_LINE,
    _visible_len,
    render_compact_fb_summary,
    render_compact_legend,
    render_compact_page,
)
from jmwallet.cli.info_render import render_legend
from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwallet.wallet.models import AddressInfo, AddressStatus, UTXOInfo
from jmwallet.wallet.rows import BranchSummaryRow, fb_date_str

runner = CliRunner()

_EXT_ADDR = "bc1qcompactexternal00000000000000000000000"
_INT_ADDR = "bc1qcompactinternal00000000000000000000000"
_BOND_ACTIVE_ADDR = "bc1pcompactbondactive00000000000000000000000000000000000"
_BOND_EXPIRED_ADDR = "bc1pcompactbondexpired0000000000000000000000000000000000"

_NOW = int(time.time())


def _make_utxo(
    txid: str,
    vout: int,
    value: int,
    address: str,
    confs: int,
    path: str,
    locktime: int | None = None,
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid,
        vout=vout,
        value=value,
        address=address,
        confirmations=confs,
        scriptpubkey="0014" + "11" * 20,
        path=path,
        mixdepth=0,
        locktime=locktime,
    )


def _address_info(
    address: str,
    index: int,
    balance: int,
    status: AddressStatus,
    path: str,
    *,
    is_external: bool = True,
    is_bond: bool = False,
    base_status: AddressStatus | None = None,
    label: str = "",
    locktime: int | None = None,
    utxos: list[Any] | None = None,
) -> AddressInfo:
    return AddressInfo(
        address=address,
        index=index,
        balance=balance,
        status=status,
        base_status=base_status,
        path=path,
        is_external=is_external,
        is_bond=is_bond,
        label=label,
        has_unconfirmed=False,
        utxos=utxos or [],
        locktime=locktime,
    )


def _bond_utxo(
    address: str, value: int, *, confs: int = 60, locktime: int | None = None
) -> UTXOInfo:
    return _make_utxo("fb" + "e" * 62, 0, value, address, confs, "m/84'/0'/0'/80/0", locktime)


def _make_wallet(
    external: list[AddressInfo],
    internal: list[AddressInfo] | None = None,
    bonds: list[AddressInfo] | None = None,
    *,
    mixdepth_count: int = 1,
) -> MagicMock:
    mock_wallet = MagicMock()
    mock_wallet.wallet_fingerprint = "5525def6"
    mock_wallet.network = "mainnet"
    mock_wallet.root_path = "m/84'/0'"
    mock_wallet.mixdepth_count = mixdepth_count
    mock_wallet.mnemonic_file = None
    mock_wallet.utxo_cache = {}
    mock_wallet.address_cache = {}
    mock_wallet.addresses_with_history = set()
    mock_wallet.sync_with_registered_bonds = AsyncMock(return_value={})
    mock_wallet.close = AsyncMock()
    mock_wallet.get_total_balance = AsyncMock(return_value=0)
    mock_wallet.get_fidelity_bond_balance = AsyncMock(return_value=0)
    mock_wallet.get_balance = AsyncMock(return_value=0)
    mock_wallet.get_account_zpub = MagicMock(return_value="zpub" + "x" * 100)
    mock_wallet.get_fidelity_bond_addresses_info = MagicMock(return_value=bonds or [])

    def mock_get_address_info(md, change, gap, used_addrs=None, hist_addrs=None):
        if md == 0 and change == 0:
            return external
        if md == 0 and change == 1:
            return internal or []
        return []

    mock_wallet.get_address_info_for_mixdepth = mock_get_address_info
    return mock_wallet


def _show(mock_wallet, capsys, *args: Any, **kwargs: Any) -> str:
    """Render the compact view directly and return the plain-text output.

    Colors are off under pytest (stdout is piped), so the captured output
    is already ANSI-free; ``data_dir`` points at a throwaway directory so
    the pending-transaction lookup finds nothing.
    """
    with tempfile.TemporaryDirectory() as td:
        mock_wallet.data_dir = Path(td)
        wallet_cli._show_compact_wallet_info(mock_wallet, set(), {}, 6, *args, **kwargs)
    return capsys.readouterr().out


def _invoke_info(mock_wallet, tmpdir: str, *extra_args: str):
    """Run ``jm-wallet info`` against the mocked wallet service."""
    mnemonic_file = Path(tmpdir) / "test.mnemonic"
    mnemonic_file.write_text("abandon " * 11 + "about")
    with patch("jmwallet.wallet.service.WalletService", return_value=mock_wallet):
        return runner.invoke(
            app,
            [
                "info",
                "--mnemonic-file",
                str(mnemonic_file),
                "--data-dir",
                tmpdir,
                "--network",
                "mainnet",
                "--backend",
                "descriptor_wallet",
                *extra_args,
            ],
        )


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _glossary(lines: list[str]) -> dict[str, str]:
    """Token -> gloss text map parsed from a rendered legend.

    Entries start at four-space indent (``    token  - text``); deeper
    indented lines are wrapped continuations and join the entry text.
    ANSI codes are stripped first (the RU! token is colorized).
    """
    entries: dict[str, str] = {}
    token: str | None = None
    for raw in lines:
        line = _ANSI_RE.sub("", raw)
        m = re.match(r"^    (\S+)\s+- (.*)$", line)
        if m:
            token = m.group(1)
            entries[token] = m.group(2).strip()
        elif token is not None and line.startswith(" " * 13) and line.strip():
            entries[token] += " " + line.strip()
        elif not line.startswith("    "):
            token = None
    return entries


class TestCompactViewFlag:
    def test_view_compact_requires_extended(self) -> None:
        result = runner.invoke(app, ["info", "--view", "compact"])
        assert result.exit_code == 2
        assert "--view requires --extended" in result.output

    def test_view_compact_renders(self, tmp_path: Path) -> None:
        utxo = _make_utxo("a" * 64, 0, 100_000, _EXT_ADDR, 3, "m/84'/0'/0'/0/0")
        bond_utxo = _bond_utxo(_BOND_EXPIRED_ADDR, 50_000, locktime=1_000_000_000)
        wallet = _make_wallet(
            [_address_info(_EXT_ADDR, 0, 100_000, "deposit", "m/84'/0'/0'/0/0", utxos=[utxo])],
            bonds=[
                _address_info(
                    _BOND_EXPIRED_ADDR,
                    1,
                    50_000,
                    "bond",
                    "m/84'/0'/0'/80/1",
                    is_bond=True,
                    locktime=1_000_000_000,
                    utxos=[bond_utxo],
                )
            ],
        )
        result = _invoke_info(wallet, str(tmp_path), "--extended", "--view", "compact")
        assert result.exit_code == 0, result.output
        assert "Address legend:" in result.output
        # An expired bond exists, so the legend carries the full fb-x
        # glossary entry (wrapped) and the spend gloss's fb-x exception.
        assert "    fb-x   - fidelity bond expired: past its chain locktime;" in result.output
        assert "    spend  - spendable: usable in any transaction (except fidelity" in (
            result.output
        )
        assert "Mixdepth 0" in result.output


class TestLegendConsistency:
    def test_compact_legend_glossary_matches_the_full_view(self) -> None:
        # Both bond states exist -> same vocabulary as the full view with
        # bonds enabled: identical entries, identical texts; the compact
        # renderer only wraps them to the 80-column budget.
        full = _glossary(render_legend(show_fidelity_bonds=True))
        compact = _glossary(render_compact_legend(has_locked_bond=True, has_expired_bond=True))
        assert compact == full

    def test_compact_legend_glossary_without_bonds_matches(self) -> None:
        full = _glossary(render_legend(show_fidelity_bonds=False))
        compact = _glossary(render_compact_legend())
        assert compact == full

    def test_compact_legend_bond_vocabulary_follows_existence(self) -> None:
        locked_only = _glossary(render_compact_legend(has_locked_bond=True))
        assert "fb-a" in locked_only and "lock" in locked_only
        assert "fb-x" not in locked_only
        assert "except fidelity bonds" not in locked_only["spend"]
        expired_only = _glossary(render_compact_legend(has_expired_bond=True))
        assert "fb-x" in expired_only
        assert "fb-a" not in expired_only and "lock" not in expired_only
        assert expired_only["spend"].endswith("(except fidelity bonds: see fb-x)")


class TestCompactLayout:
    def test_two_lines_per_utxo_with_full_outpoint(self, capsys) -> None:
        txid = "a" * 64
        utxo = _make_utxo(txid, 7, 250_000, _EXT_ADDR, 3, "m/84'/0'/0'/0/0")
        out = _show(
            _make_wallet(
                [_address_info(_EXT_ADDR, 0, 250_000, "deposit", "m/84'/0'/0'/0/0", utxos=[utxo])]
            ),
            capsys,
        )
        # Line 1: the pipe row; confs live in the exact-count (left-aligned)
        # confs column.
        assert f"/0 | {_EXT_ADDR} | 250,000 | 3     | dep" in out
        # Line 2: state + full copyable outpoint, under the address column.
        assert f"      spend {txid}:7" in out

    def test_detail_line_starts_under_the_address_column(self, capsys) -> None:
        txid = "a" * 64
        utxo = _make_utxo(txid, 7, 250_000, _EXT_ADDR, 3, "m/84'/0'/0'/0/0")
        out = _show(
            _make_wallet(
                [_address_info(_EXT_ADDR, 0, 250_000, "deposit", "m/84'/0'/0'/0/0", utxos=[utxo])]
            ),
            capsys,
        )
        lines = out.splitlines()
        row_idx = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("/0 |"))
        row, detail = lines[row_idx], lines[row_idx + 1]
        addr_col = row.index(_EXT_ADDR)
        assert detail.startswith(" " * addr_col + "spend ")
        assert detail[addr_col:] == f"spend {txid}:7"

    def test_confs_rendered_exactly_without_bucketing(self, capsys) -> None:
        utxo = _make_utxo("b" * 64, 0, 10_000, _EXT_ADDR, 9, "m/84'/0'/0'/0/0")
        out = _show(
            _make_wallet(
                [_address_info(_EXT_ADDR, 0, 10_000, "deposit", "m/84'/0'/0'/0/0", utxos=[utxo])]
            ),
            capsys,
        )
        # The confs column shows the exact count (9, not the legacy "5+"
        # bucket); the detail line does not repeat the count.
        assert f"/0 | {_EXT_ADDR} | 10,000 | 9     | dep" in out
        assert "5+" not in out
        assert f"      spend {'b' * 64}:0" in out

    def test_empty_address_stays_on_the_grid(self, capsys) -> None:
        out = _show(
            _make_wallet([_address_info(_EXT_ADDR, 0, 0, "new", "m/84'/0'/0'/0/0")]),
            capsys,
            show_empty=True,
        )
        line = next(ln for ln in out.splitlines() if ln.lstrip().startswith("/0 |"))
        # No "0" amount cell, but the tag keeps its column: blank sats/cf
        # cells leave all four separators (and the address) in place.
        assert line.count("|") == 4
        assert line.endswith("| new")

    def test_bond_row_shows_state_date_and_outpoint_on_three_lines(self, capsys) -> None:
        locktime = _NOW + 30 * 86400
        utxo = _bond_utxo(_BOND_ACTIVE_ADDR, 1_000_000, locktime=locktime)
        wallet = _make_wallet(
            [],
            bonds=[
                _address_info(
                    _BOND_ACTIVE_ADDR,
                    0,
                    1_000_000,
                    "bond",
                    "m/84'/0'/0'/80/0",
                    is_bond=True,
                    locktime=locktime,
                    utxos=[utxo],
                )
            ],
        )
        out = _show(wallet, capsys)
        # The 62-char bech32 bond address is middle-elided into the column.
        assert _BOND_ACTIVE_ADDR not in out
        assert "…" in out
        # Funded bonds get a third line: state + fb-date on line 2 (the
        # full view's lock/spend vocabulary), the full copyable outpoint
        # on line 3 -- both indented to the same column, dash-closed.
        lines = out.splitlines()
        row_idx = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("/0 |"))
        row, state_line, outpoint_line = lines[row_idx : row_idx + 3]
        assert row.rstrip().endswith("| fb-a")
        assert state_line.strip() == f"lock {fb_date_str(locktime)}"
        assert outpoint_line.strip() == f"{'fb' + 'e' * 62}:0"
        assert len(state_line) - len(state_line.lstrip()) == len(outpoint_line) - len(
            outpoint_line.lstrip()
        )
        assert len(outpoint_line) <= MAX_LINE
        assert set(lines[row_idx + 3]) == {"-"}

    def test_frozen_expired_bond_keeps_frozn_in_the_state_line(self, capsys) -> None:
        locktime = 1_000_000_000  # long expired
        utxo = _bond_utxo(_BOND_EXPIRED_ADDR, 50_000, locktime=locktime)
        utxo.frozen = True
        wallet = _make_wallet(
            [],
            bonds=[
                _address_info(
                    _BOND_EXPIRED_ADDR,
                    0,
                    50_000,
                    "bond",
                    "m/84'/0'/0'/80/0",
                    is_bond=True,
                    locktime=locktime,
                    utxos=[utxo],
                )
            ],
        )
        out = _show(wallet, capsys)
        lines = out.splitlines()
        row_idx = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("/0 |"))
        assert lines[row_idx].rstrip().endswith("| fb-x")
        # Frozen is not tag-derivable (fb-x reads as spendable), so it must
        # stay visible per row, mirroring the full view's "spend frozn".
        assert lines[row_idx + 1].strip() == f"spend frozn {fb_date_str(locktime)}"
        assert lines[row_idx + 2].strip() == f"{'fb' + 'e' * 62}:0"

    def test_hidden_bonds_render_two_line_notice(self, capsys) -> None:
        utxo = _bond_utxo(_BOND_ACTIVE_ADDR, 100_000, locktime=_NOW + 30 * 86400)
        wallet = _make_wallet(
            [],
            bonds=[
                _address_info(
                    _BOND_ACTIVE_ADDR,
                    0,
                    100_000,
                    "bond",
                    "m/84'/0'/0'/80/0",
                    is_bond=True,
                    locktime=_NOW + 30 * 86400,
                    utxos=[utxo],
                )
            ],
        )
        out = _show(wallet, capsys, show_fidelity_bonds=False)
        assert "fidelity bond addr.  m/84'/0'/0'/2  hidden (1 active, 100,000 sats)" in out
        assert "  set wallet.info_show_fidelity_bonds = true in config.toml to show" in out
        # The legend keeps the fb-a/lock glossary entries: the hidden locked
        # bond is still referenced by the notice and the (unfiltered)
        # balance footer.
        assert "    fb-a   - fidelity bond active: time-locked until the fb-date" in out
        assert "    lock   - fidelity bond UTXO still time-locked (until fb-date)" in out

    def test_long_hidden_bond_notice_wraps_within_budget(self, capsys) -> None:
        # 1 active + 2 expired bonds with a nine-digit total push the
        # notice past 80 chars; it must wrap instead of stretching the
        # dash lines beyond the budget (regtest wallet shape).
        active = _bond_utxo("bcrt1q" + "a" * 38, 80_000_000, locktime=_NOW + 300 * 86400)
        expired1 = _bond_utxo("bcrt1q" + "b" * 38, 50_000_000, locktime=1_000_000_000)
        expired2 = _bond_utxo("bcrt1q" + "c" * 38, 40_000_000, locktime=1_100_000_000)
        wallet = _make_wallet(
            [],
            bonds=[
                _address_info(
                    "bcrt1q" + "a" * 38,
                    0,
                    80_000_000,
                    "bond",
                    "m/84'/0'/0'/80/0",
                    is_bond=True,
                    locktime=_NOW + 300 * 86400,
                    utxos=[active],
                ),
                _address_info(
                    "bcrt1q" + "b" * 38,
                    0,
                    50_000_000,
                    "bond",
                    "m/84'/0'/0'/80/1",
                    is_bond=True,
                    locktime=1_000_000_000,
                    utxos=[expired1],
                ),
                _address_info(
                    "bcrt1q" + "c" * 38,
                    0,
                    40_000_000,
                    "bond",
                    "m/84'/0'/0'/80/2",
                    is_bond=True,
                    locktime=1_100_000_000,
                    utxos=[expired2],
                ),
            ],
        )
        out = _show(wallet, capsys, show_fidelity_bonds=False)
        assert all(_visible_len(ln) <= MAX_LINE for ln in out.splitlines())
        # No content lost in the wrap: counts and total survive re-joining.
        notice = " ".join(ln.strip() for ln in out.splitlines() if "hidden" in ln or "sats)" in ln)
        assert "hidden (1 active, 2 expired, 170,000,000 sats)" in notice

    def test_label_truncated_but_ru_never_dropped(self, capsys) -> None:
        utxo = _make_utxo("c" * 64, 0, 50_000, _EXT_ADDR, 1, "m/84'/0'/0'/0/0")
        out = _show(
            _make_wallet(
                [
                    _address_info(
                        _EXT_ADDR,
                        0,
                        50_000,
                        "reused",
                        "m/84'/0'/0'/0/0",
                        base_status="deposit",
                        label="a very long label that cannot possibly fit",
                        utxos=[utxo],
                    )
                ]
            ),
            capsys,
        )
        # The index column is right-aligned against its "idx" header, so
        # single-digit indexes are space-padded on the left.
        data_lines = [ln for ln in out.splitlines() if ln.lstrip().startswith("/0 |")]
        assert len(data_lines) == 1
        assert data_lines[0].endswith("RU!")
        assert "…" in data_lines[0]

    def test_two_line_entries_close_with_a_dash_line(self, capsys) -> None:
        """Every multi-line entry is closed by a dash line: funded UTXOs
        (regular: 2 lines, funded bonds: 3) and unfunded bond rows with
        their fb-date line. Only single-line empty addresses get none."""
        utxo = _make_utxo("a" * 64, 7, 250_000, _EXT_ADDR, 3, "m/84'/0'/0'/0/0")
        bond = _bond_utxo(_BOND_ACTIVE_ADDR, 1_000_000, locktime=_NOW + 30 * 86400)
        wallet = _make_wallet(
            [
                _address_info(_EXT_ADDR, 0, 250_000, "deposit", "m/84'/0'/0'/0/0", utxos=[utxo]),
                _address_info(_INT_ADDR, 1, 0, "new", "m/84'/0'/0'/0/1"),
            ],
            bonds=[
                _address_info(
                    _BOND_ACTIVE_ADDR,
                    0,
                    1_000_000,
                    "bond",
                    "m/84'/0'/0'/80/0",
                    is_bond=True,
                    locktime=_NOW + 30 * 86400,
                    utxos=[bond],
                ),
                _address_info(
                    _BOND_EXPIRED_ADDR,
                    1,
                    0,
                    "bond",
                    "m/84'/0'/0'/80/1",
                    is_bond=True,
                    locktime=1_000_000_000,
                ),
            ],
        )
        lines = _show(wallet, capsys, show_empty=True).splitlines()

        def is_dash(line: str) -> bool:
            # Table separators carry "+" joints; only plain dash lines match.
            return bool(line) and set(line) == {"-"}

        # Funded bond (table order: bonds first): row, state + fb-date
        # line, outpoint line, closing dash.
        bond_idx = next(
            i for i, ln in enumerate(lines) if ln.lstrip().startswith("/0 |") and "fb-a" in ln
        )
        assert lines[bond_idx + 1].lstrip().startswith("lock ")
        assert lines[bond_idx + 2].strip().endswith(":0")
        assert is_dash(lines[bond_idx + 3])
        # Unfunded bond: two lines as well (row + fb-date), so it closes
        # with a dash, too; then the FB summary follows.
        empty_bond_idx = next(
            i for i, ln in enumerate(lines) if ln.lstrip().startswith("/1 |") and "fb-x" in ln
        )
        assert not is_dash(lines[empty_bond_idx + 1])  # the fb-date detail
        assert is_dash(lines[empty_bond_idx + 2])
        # Funded regular UTXO: row, detail, closing dash.
        ext_idx = next(
            i for i, ln in enumerate(lines) if ln.lstrip().startswith("/0 |") and "dep" in ln
        )
        assert lines[ext_idx + 1].lstrip().startswith("spend ")
        assert is_dash(lines[ext_idx + 2])
        # Empty address: no closing dash after its single line.
        empty_idx = next(
            i for i, ln in enumerate(lines) if ln.lstrip().startswith("/1 |") and "new" in ln
        )
        assert not is_dash(lines[empty_idx + 1])
        # Exactly one dash per two-line entry: 2 mixdepth frames + 3
        # entries (funded bond, funded regular UTXO, unfunded bond);
        # table separators carry "+" joints.
        assert sum(1 for ln in lines if is_dash(ln)) == 5


class TestCompactInvariants:
    """The width budget and the uniform dash lines are the compact view's
    defining invariants -- every scenario must satisfy them."""

    def _stress_wallet(self) -> MagicMock:
        """Width-stress wallet: RU! on a whale row (index /30) whose
        multi-comma amount fills the line to exactly 80 chars (no room
        left for its label), an active (0.01 BTC) and an expired bond. The
        active bond pushes the FB summary one-liner over the width budget,
        exercising the two-line fallback."""
        ext_utxo = _make_utxo("a" * 64, 0, 9_999_999_999, _EXT_ADDR, 3, "m/84'/0'/0'/0/30")
        int_utxo = _make_utxo("c" * 64, 0, 42_000, _INT_ADDR, 1, "m/84'/0'/0'/1/0")
        external = [
            _address_info(
                _EXT_ADDR,
                30,
                9_999_999_999,
                "reused",
                "m/84'/0'/0'/0/30",
                base_status="cj-out",
                label="a fairly long label that will need truncation",
                utxos=[ext_utxo],
            )
        ]
        internal = [
            _address_info(
                _INT_ADDR,
                0,
                42_000,
                "cj-out",
                "m/84'/0'/0'/1/0",
                is_external=False,
                utxos=[int_utxo],
            )
        ]
        bonds = [
            _address_info(
                _BOND_ACTIVE_ADDR,
                0,
                1_000_000,
                "bond",
                "m/84'/0'/0'/80/0",
                is_bond=True,
                locktime=_NOW + 30 * 86400,
                utxos=[_bond_utxo(_BOND_ACTIVE_ADDR, 1_000_000, locktime=_NOW + 30 * 86400)],
            ),
            _address_info(
                _BOND_EXPIRED_ADDR,
                1,
                15_000,
                "bond",
                "m/84'/0'/0'/80/1",
                is_bond=True,
                locktime=1_000_000_000,
                utxos=[
                    _make_utxo(
                        "fd" + "d" * 62,
                        1,
                        15_000,
                        _BOND_EXPIRED_ADDR,
                        60,
                        "m/84'/0'/0'/80/1",
                        1_000_000_000,
                    )
                ],
            ),
        ]
        return _make_wallet(external, internal, bonds)

    def test_no_line_exceeds_the_width_budget(self, capsys) -> None:
        out = _show(self._stress_wallet(), capsys, show_empty=True)
        assert out.strip(), "expected non-empty compact output"
        assert all(len(line) <= MAX_LINE for line in out.splitlines())

    def test_fb_summary_splits_into_two_lines_when_over_budget(self, capsys) -> None:
        """A 94-char FB one-liner splits: balance line + indented breakdown."""
        out = _show(self._stress_wallet(), capsys, show_empty=True)
        lines = out.splitlines()
        assert "Balance: 1,015,000 sats" in lines
        assert "  1,000,000 locked + 15,000 expired; spendable (explicit): 15,000 sats" in lines
        assert not any("1,015,000 sats (" in line for line in lines)

    def test_fb_summary_one_liner_when_it_fits(self) -> None:
        summary = BranchSummaryRow(
            mixdepth=0,
            branch=FIDELITY_BOND_BRANCH,
            balance=60,
            locked_balance=50,
            expired_balance=10,
        )
        assert render_compact_fb_summary(summary) == [
            "Balance: 60 sats (50 locked + 10 expired; spendable (explicit): 10 sats)"
        ]

    def test_fb_summary_spendable_explicit_excludes_frozen_expired(self) -> None:
        """Frozen expired bonds are excluded from the spendable figure.

        Unlike the full view, the compact summary does not annotate the
        frozen amount: it appears in the page footer and the suffix would
        break the 80-column budget; ``--view full`` carries the annotation.
        """
        summary = BranchSummaryRow(
            mixdepth=0,
            branch=FIDELITY_BOND_BRANCH,
            balance=1_500,
            expired_balance=1_500,
            expired_frozen_balance=500,
        )
        assert render_compact_fb_summary(summary) == [
            "Balance: 1,500 sats (1,500 expired; spendable (explicit): 1,000 sats)"
        ]

    def test_hidden_bond_layout_stays_within_budget(self, capsys) -> None:
        out = _show(self._stress_wallet(), capsys, show_empty=True, show_fidelity_bonds=False)
        assert out.strip(), "expected non-empty compact output"
        assert all(len(line) <= MAX_LINE for line in out.splitlines())

    def test_all_dash_lines_share_one_length(self, capsys) -> None:
        out = _show(self._stress_wallet(), capsys, show_empty=True)
        dash_lines = [ln for ln in out.splitlines() if ln.startswith("-")]
        assert dash_lines, "expected at least one dash line"
        lengths = {len(ln) for ln in dash_lines}
        assert len(lengths) == 1
        # ... and that length equals the widest visible content line.
        content = [ln for ln in out.splitlines() if not ln.startswith("-")]
        assert lengths.pop() == max(len(ln) for ln in content)

    def test_columns_align_across_all_tables(self, capsys) -> None:
        """Global grid: all tables of the page share one column geometry --
        sats, cf and tag line up across branches (and the FB table)."""
        out = _show(self._stress_wallet(), capsys, show_empty=True)
        lines = out.splitlines()
        headers = [ln for ln in lines if ln.startswith("idx")]
        separators = [ln for ln in lines if ln.startswith("----+")]
        assert len(headers) == 3  # external, internal, fidelity bonds
        assert len(set(headers)) == 1
        assert len(set(separators)) == 1
        # Amounts right-align to the same column width in every table.
        amount_cells = []
        for amount in ("9,999,999,999", "42,000", "1,000,000"):
            row = next(ln for ln in lines if amount in ln and "|" in ln)
            amount_cells.append(row.split("|")[2])
        assert len({len(cell) for cell in amount_cells}) == 1

    def test_whale_amount_shrinks_the_address_column_globally(self, capsys) -> None:
        """A whale amount anywhere widens the page-global sats column; the
        address column absorbs it (elides harder, in every table) so the
        widest RU! row still fits the budget."""
        whale_utxo = _make_utxo("a" * 64, 0, 24_750_000_000_000, _EXT_ADDR, 3, "m/84'/0'/0'/0/30")
        int_utxo = _make_utxo("c" * 64, 0, 42_000, _INT_ADDR, 1, "m/84'/0'/0'/1/0")
        wallet = _make_wallet(
            [
                _address_info(
                    _EXT_ADDR,
                    30,
                    24_750_000_000_000,
                    "reused",
                    "m/84'/0'/0'/0/30",
                    base_status="cj-out",
                    utxos=[whale_utxo],
                )
            ],
            [
                _address_info(
                    _INT_ADDR,
                    0,
                    42_000,
                    "cj-out",
                    "m/84'/0'/0'/1/0",
                    is_external=False,
                    utxos=[int_utxo],
                )
            ],
        )
        out = _show(wallet, capsys)
        lines = out.splitlines()
        assert all(len(ln) <= MAX_LINE for ln in lines)
        # The budget is fully used: the reused whale row sits exactly at 80.
        whale_row = next(ln for ln in lines if "24,750,000,000,000" in ln)
        assert whale_row.endswith("RU!")
        assert len(whale_row) == MAX_LINE
        # Both regular tables elide their (normally 42-char) addresses --
        # the shock absorber is global, not per-table.
        rows = [ln for ln in lines if ln.lstrip().startswith("/") and " | " in ln]
        assert len(rows) == 2
        assert all("…" in ln for ln in rows)
        # One geometry: identical table separators everywhere.
        separators = [ln for ln in lines if ln.startswith("----+")]
        assert len(set(separators)) == 1

    def test_visible_length_strips_ansi_codes(self) -> None:
        colored = "\x1b[1m\x1b[36mMixdepth 0\x1b[0m"
        assert _visible_len(colored) == len("Mixdepth 0")
        # Empty plans render just the legend and stay within budget.
        plain = render_compact_page([])
        assert all(_visible_len(line) <= MAX_LINE for line in plain)
