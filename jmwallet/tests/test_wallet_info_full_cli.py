"""Tests for the ``--view`` option of ``jm-wallet info --extended``.

Covers the CLI flag behavior (``--view`` requires ``--extended``, unknown
values are rejected) and the end-to-end orchestration of the ``full`` view:
the canonical row model fed from a (mocked) WalletService, rendered as
pipe-aligned tables. The legacy layout must stay the default.
"""

from __future__ import annotations

import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import MethodType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.cli import wallet as wallet_cli
from jmwallet.cli.info_render import BRANCH_NAME_WIDTH
from jmwallet.wallet.models import AddressInfo
from jmwallet.wallet.service import WalletService

runner = CliRunner()

_EXT_ADDR = "bc1qfullviewexternal0000000000000000000000"
_INT_ADDR = "bc1qfullviewinternal0000000000000000000000"
_BOND_ACTIVE_ADDR = "bc1pfullviewbondactive000000000000000000000"
_BOND_EXPIRED_ADDR = "bc1pfullviewbondexpired00000000000000000000"
_CHAIN_TIME = 1_800_000_000


def _make_utxo(
    txid: str,
    vout: int,
    value: int,
    address: str,
    confs: int,
    path: str,
    locktime: int | None = None,
):
    from jmwallet.wallet.models import UTXOInfo

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


@pytest.fixture
def info_chain_time(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    snapshot = AsyncMock(return_value=_CHAIN_TIME)
    monkeypatch.setattr(
        "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend.get_median_time_past",
        snapshot,
    )
    return snapshot


@pytest.fixture
def full_view_wallet(info_chain_time):
    """Wallet mock with a deposit address (two UTXOs) and a reg-change UTXO."""
    utxo1 = _make_utxo("a" * 64, 0, 100_000, _EXT_ADDR, 3, "m/84'/0'/0'/0/0")
    utxo2 = _make_utxo("b" * 64, 1, 150_000, _EXT_ADDR, 5, "m/84'/0'/0'/0/0")
    utxo3 = _make_utxo("c" * 64, 0, 42_000, _INT_ADDR, 1, "m/84'/0'/0'/1/0")

    mock_wallet = MagicMock()
    mock_wallet.wallet_fingerprint = "5525def6"
    mock_wallet.network = "mainnet"
    mock_wallet.root_path = "m/84'/0'"
    mock_wallet.mixdepth_count = 5
    mock_wallet.mnemonic_file = None
    mock_wallet.utxo_cache = {0: [utxo1, utxo2, utxo3]}
    mock_wallet.address_cache = {_EXT_ADDR: (0, 0, 0), _INT_ADDR: (0, 1, 0)}
    mock_wallet.addresses_with_history = set()

    mock_wallet.sync_with_registered_bonds = AsyncMock(return_value={})
    mock_wallet.close = AsyncMock()
    mock_wallet.get_total_balance = AsyncMock(return_value=292_000)
    mock_wallet.get_fidelity_bond_balance = AsyncMock(return_value=0)
    mock_wallet.get_balance = AsyncMock(return_value=292_000)
    mock_wallet.get_account_zpub = MagicMock(return_value="zpub" + "x" * 100)
    mock_wallet.get_fidelity_bond_addresses_info = MagicMock(return_value=[])

    def mock_get_address_info(md, change, gap, used_addrs=None, hist_addrs=None):
        if md == 0 and change == 0:
            return [
                AddressInfo(
                    address=_EXT_ADDR,
                    index=0,
                    balance=250_000,
                    status="deposit",
                    path="m/84'/0'/0'/0/0",
                    is_external=True,
                    has_unconfirmed=False,
                    utxos=[utxo1, utxo2],
                )
            ]
        if md == 0 and change == 1:
            return [
                AddressInfo(
                    address=_INT_ADDR,
                    index=0,
                    balance=42_000,
                    status="non-cj-change",
                    path="m/84'/0'/0'/1/0",
                    is_external=False,
                    has_unconfirmed=False,
                    utxos=[utxo3],
                )
            ]
        return []

    mock_wallet.get_address_info_for_mixdepth = mock_get_address_info
    return mock_wallet


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


class TestViewFlagValidation:
    """``--view`` only selects the *extended* layout, so it requires
    ``--extended`` and a known view name; both fail fast (exit 2) before any
    mnemonic prompt or backend connection."""

    def test_view_full_requires_extended(self) -> None:
        result = runner.invoke(app, ["info", "--view", "full"])
        assert result.exit_code == 2
        assert "--view requires --extended" in result.output

    def test_view_legacy_requires_extended_when_explicit(self) -> None:
        result = runner.invoke(app, ["info", "--view", "legacy"])
        assert result.exit_code == 2
        assert "--view requires --extended" in result.output

    def test_view_rejects_unknown_value(self) -> None:
        result = runner.invoke(app, ["info", "--extended", "--view", "bogus"])
        assert result.exit_code == 2
        assert "Invalid --view 'bogus'" in result.output


class TestFullViewCli:
    """End-to-end: ``--extended --view full`` renders the canonical row model
    as pipe-aligned tables; the default stays the legacy layout."""

    def test_full_view_renders_pipe_table(self, full_view_wallet) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _invoke_info(full_view_wallet, tmpdir, "--extended", "--view", "full")

        assert result.exit_code == 0, f"Command failed: {result.output}"
        out = result.output

        # Pipe-aligned table header (one table per branch): the derivation
        # index replaces the per-row path, the units live in the headers.
        assert "idx" in out and "| address" in out and "| outpoint" in out
        assert "| sats " in out and "| confs " in out
        # Hard rule: one line per UTXO, each with its exact confirmation
        # count as a bare cell (no "5+ conf" bucketing) and its copy-ready
        # outpoint. Data rows start with the /-prefixed derivation index.
        confs_cells = [
            line.split("|")[3].strip()
            for line in out.splitlines()
            if line.lstrip().startswith("/0 |")
        ]
        assert confs_cells == ["3", "5", "1"]
        assert "5+ conf" not in out
        assert f"{'a' * 64}:0" in out
        assert f"{'b' * 64}:1" in out
        assert f"{'c' * 64}:0" in out
        # Display-level short labels and explicit spendable state.
        assert "| chg " in out
        assert "non-cj-change" not in out
        assert "| spend " in out
        # Full-view legend (labels / states / flags vocabulary).
        assert "Address legend:" in out
        assert "Address status legend:" not in out
        # No tabs in the full view: fixed padding aligns the zpub column.
        assert "\t" not in out
        # Branch and mixdepth summaries (legacy wording).
        assert "Balance: 250,000 sats (spendable: 250,000 sats)" in out
        assert "Balance: 42,000 sats (spendable: 42,000 sats)" in out
        assert "Balance for mixdepth 0:" in out
        assert "292,000 sats (spendable: 292,000 sats)" in out

    @pytest.mark.parametrize("extra_args", [["--extended"], ["--extended", "--view", "legacy"]])
    def test_default_and_explicit_legacy_view_render_legacy_layout(
        self, full_view_wallet, extra_args: list[str]
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _invoke_info(full_view_wallet, tmpdir, *extra_args)

        assert result.exit_code == 0, f"Command failed: {result.output}"
        out = result.output

        # Legacy legend and legacy UTXO child rows; no full-view table.
        assert "Address status legend:" in out
        assert "Address legend:" not in out
        assert "| outpoint" not in out
        # Legacy buckets confirmations at 5+.
        assert "(5+ conf)" in out
        # Legacy keeps the historic label name.
        assert "non-cj-change" in out


@pytest.fixture
def full_view_wallet_with_bonds(full_view_wallet):
    """The base wallet plus one active and one expired fidelity bond (md 0)."""
    active_locktime = int(time.time()) + 10 * 365 * 86400
    expired_locktime = 1_000_000_000  # 2001-09-09, long past

    bond_active_utxo = _make_utxo(
        "d" * 64, 0, 60_000, _BOND_ACTIVE_ADDR, 100, "m/84'/0'/0'/2/0", locktime=active_locktime
    )
    bond_expired_utxo = _make_utxo(
        "e" * 64, 0, 40_000, _BOND_EXPIRED_ADDR, 3, "m/84'/0'/0'/2/1", locktime=expired_locktime
    )
    full_view_wallet.get_fidelity_bond_addresses_info = MagicMock(
        return_value=[
            AddressInfo(
                address=_BOND_ACTIVE_ADDR,
                index=0,
                balance=60_000,
                status="bond",
                path="m/84'/0'/0'/2/0",
                is_external=False,
                is_bond=True,
                locktime=active_locktime,
                utxos=[bond_active_utxo],
            ),
            AddressInfo(
                address=_BOND_EXPIRED_ADDR,
                index=1,
                balance=40_000,
                status="bond",
                path="m/84'/0'/0'/2/1",
                is_external=False,
                is_bond=True,
                locktime=expired_locktime,
                utxos=[bond_expired_utxo],
            ),
        ]
    )
    return full_view_wallet


@pytest.fixture
def chain_time_wallet(full_view_wallet: MagicMock, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Consistent cache and balance helpers, with host time ahead of chain time."""
    monkeypatch.setattr(time, "time", lambda: _CHAIN_TIME + 3_600)
    regular = full_view_wallet.utxo_cache[0]
    regular[0].frozen = True
    template = _make_utxo(
        "d" * 64,
        0,
        40_000,
        _BOND_EXPIRED_ADDR,
        3,
        "m/84'/0'/0'/2/0",
        locktime=_CHAIN_TIME - 1,
    )
    bonds = [
        template,
        replace(template, txid="e" * 64, value=10_000, frozen=True),
        replace(template, txid="f" * 64, value=60_000, locktime=_CHAIN_TIME),
        replace(template, txid="1" * 64, value=20_000, locktime=_CHAIN_TIME + 1, frozen=True),
    ]
    bonds = [
        replace(bond, address=f"bc1pchainviewbond{i}", path=f"m/84'/0'/0'/2/{i}")
        for i, bond in enumerate(bonds)
    ]
    full_view_wallet.utxo_cache = {md: [] for md in range(full_view_wallet.mixdepth_count)}
    full_view_wallet.utxo_cache[0] = regular + bonds
    full_view_wallet.get_fidelity_bond_addresses_info.return_value = [
        AddressInfo(
            address=bond.address,
            index=i,
            balance=bond.value,
            status="bond",
            path=bond.path,
            is_external=False,
            is_bond=True,
            locktime=bond.locktime,
            utxos=[bond],
        )
        for i, bond in enumerate(bonds)
    ]
    for name in ("get_balance", "get_total_balance", "get_fidelity_bond_balance"):
        setattr(full_view_wallet, name, MethodType(getattr(WalletService, name), full_view_wallet))
    return full_view_wallet


@pytest.mark.parametrize(
    "args",
    [("--show-utxos",), ("--extended", "--view", "legacy"), ("--extended", "--view", "full")],
)
@pytest.mark.parametrize("hidden", [False, True])
def test_info_uses_one_chain_time_for_bonds(
    chain_time_wallet: MagicMock,
    info_chain_time: AsyncMock,
    tmp_path: Path,
    args: tuple[str, ...],
    hidden: bool,
) -> None:
    if hidden:
        (tmp_path / "config.toml").write_text("[wallet]\ninfo_show_fidelity_bonds = false\n")
    chain_time_wallet.data_dir = tmp_path
    result = _invoke_info(chain_time_wallet, str(tmp_path), *args)
    assert result.exit_code == 0, result.output
    info_chain_time.assert_awaited_once()
    out = result.output
    for amount, label in (
        (422_000, "Total Wallet Balance"),
        (100_000, "Frozen UTXOs"),
        (80_000, "fidelity bonds time-locked"),
        (10_000, "fidelity bonds expired (frozen)"),
        (232_000, "Total Spendable Balance"),
        (192_000, "thereof auto-selectable"),
        (40_000, "thereof expired fidelity bonds (explicit selection or bond-only sweep)"),
    ):
        assert f"{amount:,} sats  {label}" in out
    if args == ("--show-utxos",):
        lines = out.splitlines()
        expired = next(line for line in lines if f"{'d' * 64}:0" in line)
        at_cutoff = next(line for line in lines if f"{'f' * 64}:0" in line)
        assert "redeemable" in expired
        assert "locked" in at_cutoff
    elif hidden:
        assert "hidden (2 active, 2 expired, 130,000 sats)" in out
    elif args[-1] == "full":
        assert "80,000 time-locked + 50,000 expired; spendable (explicit): 40,000 sats" in out
        at_cutoff = next(line for line in out.splitlines() if f"{'f' * 64}:0" in line)
        assert at_cutoff.split("|")[4].strip() == "fb-a"
        assert at_cutoff.split("|")[5].strip() == "lock"
    else:
        assert "80,000 time-locked + 50,000 expired" in out
        assert "[FB TIME-LOCKED]" in out


def test_info_skips_chain_time_without_funded_bonds(
    full_view_wallet: MagicMock, info_chain_time: AsyncMock, tmp_path: Path
) -> None:
    result = _invoke_info(full_view_wallet, str(tmp_path), "--extended", "--view", "full")
    assert result.exit_code == 0, result.output
    info_chain_time.assert_not_awaited()


def test_info_chain_time_failure_does_not_fall_back_to_host_time(
    chain_time_wallet: MagicMock, info_chain_time: AsyncMock, tmp_path: Path
) -> None:
    error = RuntimeError("chain time unavailable")
    info_chain_time.side_effect = error
    result = _invoke_info(chain_time_wallet, str(tmp_path), "--extended", "--view", "full")
    assert result.exit_code == 1
    assert result.exception is error
    assert "Total Spendable Balance" not in result.output
    chain_time_wallet.close.assert_awaited_once()


class TestFullViewFidelityBonds:
    """The fidelity bond branch of mixdepth 0 renders ahead of the regular
    branches with its own raster (fb-date, lock/spend); hiding it via
    ``wallet.info_show_fidelity_bonds = false`` leaves a one-line notice and
    drops the bonds from the totals."""

    def test_bond_branch_comes_first_in_mixdepth_zero(self, full_view_wallet_with_bonds) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _invoke_info(
                full_view_wallet_with_bonds, tmpdir, "--extended", "--view", "full"
            )

        assert result.exit_code == 0, f"Command failed: {result.output}"
        out = result.output

        # Exactly one bond table: mixdepth 0 only, ahead of the regular branches.
        assert out.count("fidelity bond addr.") == 1
        assert out.index("Mixdepth 0") < out.index("fidelity bond addr.")
        assert out.index("fidelity bond addr.") < out.index("external addresses")

        # Own raster: fb-date column, fb-a/fb-x tags, lock/spend states, and
        # exact confirmation counts.
        assert any(line.startswith("idx") and "fb-date" in line for line in out.splitlines())
        active_line = next(line for line in out.splitlines() if _BOND_ACTIVE_ADDR in line)
        expired_line = next(line for line in out.splitlines() if _BOND_EXPIRED_ADDR in line)
        assert active_line.split("|")[4].strip() == "fb-a"
        assert active_line.split("|")[5].strip() == "lock"
        assert active_line.split("|")[3].strip() == "100"
        assert expired_line.split("|")[4].strip() == "fb-x"
        assert expired_line.split("|")[5].strip() == "spend"
        assert expired_line.split("|")[3].strip() == "3"
        assert f"{'d' * 64}:0" in active_line

        # Bonds count toward the mixdepth total but are never auto-selected;
        # the branch summary splits time-locked from expired funds and shows
        # the explicitly spendable figure.
        assert "392,000 sats (spendable: 292,000 sats)" in out
        assert (
            "Balance: 100,000 sats (60,000 time-locked + 40,000 expired; "
            "spendable (explicit): 40,000 sats)" in out
        )

        # Every dashed line in the full-view section (page separators and
        # -+- table separators) shares one length.
        full_section = out.split("Address legend:", 1)[1]
        dashed = [line for line in full_section.splitlines() if line and set(line) <= {"-", "+"}]
        assert len(dashed) >= 4
        assert len({len(line) for line in dashed}) == 1

        # All branch header paths start in the same column (names padded to
        # BRANCH_NAME_WIDTH, fixed padding instead of tab stops).
        headers = [
            line
            for line in out.splitlines()
            if line.startswith(("fidelity bond addr.", "external addresses", "internal addresses"))
        ]
        assert len(headers) == 11  # 1 bond + 5 external + 5 internal
        assert all(line.index("m/84'") == BRANCH_NAME_WIDTH + 2 for line in headers)

    def test_full_view_preserves_frozen_bond_states(self, full_view_wallet_with_bonds) -> None:
        for addr in full_view_wallet_with_bonds.get_fidelity_bond_addresses_info.return_value:
            addr.utxos[0].frozen = True
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _invoke_info(
                full_view_wallet_with_bonds, tmpdir, "--extended", "--view", "full"
            )
        assert result.exit_code == 0, result.output
        for address, state in (
            (_BOND_ACTIVE_ADDR, "lock frozn"),
            (_BOND_EXPIRED_ADDR, "spend frozn"),
        ):
            line = next(line for line in result.output.splitlines() if address in line)
            assert line.split("|")[5].strip() == state

    def test_hidden_bonds_leave_notice_and_drop_totals(
        self, full_view_wallet_with_bonds, capsys, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr("jmwallet.cli.ansi.color_enabled", lambda: False)
        full_view_wallet_with_bonds.data_dir = tmp_path
        wallet_cli._show_full_wallet_info(
            full_view_wallet_with_bonds,
            used_addresses=set(),
            history_addresses={},
            gap_limit=20,
            show_fidelity_bonds=False,
        )
        out = capsys.readouterr().out

        # The one-line notice stands exactly where the table would be: after
        # the mixdepth header, before the regular branches. It carries the
        # hidden value, so the page's balances stay reconcilable.
        notice = (
            "fidelity bond addr.  m/84'/0'/0'/2  hidden (1 active, 1 expired, 100,000 sats) - "
            "set wallet.info_show_fidelity_bonds = true in config.toml to show"
        )
        assert notice in out
        assert out.index("Mixdepth 0") < out.index(notice)
        assert out.index(notice) < out.index("external addresses")

        # No bond table, and the legend loses the whole bond vocabulary.
        assert "fb-date" not in out
        assert "fb-a" not in out
        assert "fb-x" not in out
        assert "lock" not in out
        assert "redeem" not in out

        # Branch and mixdepth totals exclude the hidden bonds.
        assert "292,000 sats (spendable: 292,000 sats)" in out
        assert "392,000" not in out

    def test_no_notice_without_funded_bonds(
        self, full_view_wallet, capsys, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setattr("jmwallet.cli.ansi.color_enabled", lambda: False)
        full_view_wallet.data_dir = tmp_path
        wallet_cli._show_full_wallet_info(
            full_view_wallet,
            used_addresses=set(),
            history_addresses={},
            gap_limit=20,
            show_fidelity_bonds=False,
        )
        out = capsys.readouterr().out
        # Nothing funded is hidden, so no notice appears; the legend is
        # still filtered.
        assert "info_show_fidelity_bonds = true" not in out
        assert "fb-a" not in out


class TestFidelityBondHiddenNotice:
    """The notice standing in for the hidden bond table counts funded bonds
    only (split into active vs expired) and vanishes when nothing funded is
    hidden."""

    @staticmethod
    def _bond(balance: int, locktime: int | None) -> AddressInfo:
        return AddressInfo(
            address="bc1pbond",
            index=0,
            balance=balance,
            status="bond",
            path="m/84'/0'/0'/2/0",
            is_external=False,
            is_bond=True,
            locktime=locktime,
        )

    def test_unfunded_bonds_hide_nothing(self) -> None:
        assert wallet_cli._fidelity_bond_hidden_notice([], 1) is None
        # A fully redeemed (empty) bond address hides nothing either.
        assert wallet_cli._fidelity_bond_hidden_notice([self._bond(0, 1_000_000_000)], 1) is None

    def test_counts_active_and_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("jmwallet.cli.ansi.color_enabled", lambda: False)
        active = self._bond(60_000, int(time.time()) + 10 * 365 * 86400)
        expired = self._bond(40_000, 1_000_000_000)
        notice = wallet_cli._fidelity_bond_hidden_notice([active, expired], 1)
        assert notice == (
            "fidelity bond addr.  m/84'/1'/0'/2  hidden (1 active, 1 expired, 100,000 sats) - "
            "set wallet.info_show_fidelity_bonds = true in config.toml to show"
        )

    def test_missing_locktime_counts_as_expired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("jmwallet.cli.ansi.color_enabled", lambda: False)
        notice = wallet_cli._fidelity_bond_hidden_notice([self._bond(10_000, None)], 0)
        assert notice is not None
        assert "(1 expired, 10,000 sats)" in notice
        assert "active" not in notice
