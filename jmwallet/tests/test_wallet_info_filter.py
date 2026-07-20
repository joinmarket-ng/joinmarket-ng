"""Tests for empty-address filtering in `jm-wallet info --extended`.

Issue: after running a wallet for a few months, the extended view becomes
unreadable because most addresses have zero balance. This test exercises
the `_print_branch_addresses` helper that drives the filtering.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from jmwallet.cli.wallet import _print_branch_addresses
from jmwallet.wallet.models import AddressInfo, UTXOInfo


def _mk_utxo(address: str, confirmations: int, value: int = 100_000) -> UTXOInfo:
    return UTXOInfo(
        txid="a" * 64,
        vout=0,
        value=value,
        address=address,
        confirmations=confirmations,
        scriptpubkey="0014" + "b" * 40,
        path="m/84'/0'/0'/0/0",
        mixdepth=0,
    )


def _mk(index: int, status: str, balance: int, branch: int = 0) -> AddressInfo:
    return AddressInfo(
        address=f"bc1qaddr{index:04d}",
        index=index,
        balance=balance,
        status=status,  # type: ignore[arg-type]
        path=f"m/84'/0'/0'/{branch}/{index}",
        is_external=(branch == 0),
    )


def _capture(addresses: list[AddressInfo], show_empty: bool) -> tuple[str, int, int]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        total, hidden = _print_branch_addresses(
            addresses,
            pending_addresses=set(),
            show_empty=show_empty,
        )
    return buf.getvalue(), total, hidden


class TestPrintBranchAddressesFiltering:
    def test_show_empty_true_prints_every_address(self) -> None:
        addrs = [
            _mk(0, "used-empty", 0),
            _mk(1, "deposit", 100_000),
            _mk(2, "new", 0),
            _mk(3, "new", 0),
        ]
        output, total, hidden = _capture(addrs, show_empty=True)

        # Every address must appear; nothing hidden.
        for a in addrs:
            assert a.address in output
        assert hidden == 0
        assert total == 100_000

    def test_show_empty_false_hides_zero_balance_entries(self) -> None:
        addrs = [
            _mk(0, "used-empty", 0),
            _mk(1, "deposit", 100_000),
            _mk(2, "used-empty", 0),
            _mk(3, "new", 0),  # kept: first "new" address
            _mk(4, "new", 0),  # kept: within new_address_limit (default 6)
        ]
        output, total, hidden = _capture(addrs, show_empty=False)

        # Non-empty address is always shown.
        assert "bc1qaddr0001" in output
        # Both "new" receive addresses are surfaced up to the default
        # limit of 6 so users can pick multiple fresh deposit addresses
        # without having to drop to --show-empty (issue #463).
        assert "bc1qaddr0003" in output
        assert "bc1qaddr0004" in output
        # Zero-balance used-empty/flagged lines are dropped.
        assert "bc1qaddr0000" not in output
        assert "bc1qaddr0002" not in output

        # 2 entries hidden (used-empty at 0 and 2); balance still totals everything.
        assert hidden == 2
        assert total == 100_000

    def test_show_empty_false_with_no_new_address_still_shows_funded_only(self) -> None:
        """If no 'new' address exists (all used), don't invent one; only print funded ones."""
        addrs = [
            _mk(0, "used-empty", 0),
            _mk(1, "deposit", 42),
            _mk(2, "used-empty", 0),
        ]
        output, total, hidden = _capture(addrs, show_empty=False)

        assert "bc1qaddr0001" in output
        assert "bc1qaddr0000" not in output
        assert "bc1qaddr0002" not in output
        assert hidden == 2
        assert total == 42

    def test_balance_accounts_for_hidden_addresses(self) -> None:
        """Total balance must include addresses even if we skipped printing them."""
        # Corner case: a funded address still gets printed; the balance
        # sum must be correct regardless of show_empty.
        addrs = [
            _mk(0, "used-empty", 0),
            _mk(1, "deposit", 10),
            _mk(2, "cj-out", 20),
            _mk(3, "new", 0),
        ]
        _, total_shown, _ = _capture(addrs, show_empty=True)
        _, total_hidden, _ = _capture(addrs, show_empty=False)
        assert total_shown == total_hidden == 30

    def test_multiple_new_addresses_shown_up_to_default_limit(self) -> None:
        """Issue #463: show up to 6 empty 'new' addresses so users can send
        multiple deposits without enabling --show-empty (which would also
        surface confusing used-empty/flagged lines)."""
        addrs = [_mk(i, "new", 0) for i in range(10)]
        output, _, hidden = _capture(addrs, show_empty=False)

        # First 6 "new" addresses are shown, the rest are hidden.
        for i in range(6):
            assert f"bc1qaddr{i:04d}" in output
        for i in range(6, 10):
            assert f"bc1qaddr{i:04d}" not in output
        assert hidden == 4

    def test_used_empty_and_flagged_are_always_hidden_in_default_view(self) -> None:
        """Issue #463: used-empty and flagged addresses (both unsafe to
        reuse) must never appear in the default view -- not even as the
        leading placeholder -- so the output stays actionable."""
        addrs = [
            _mk(0, "used-empty", 0),
            _mk(1, "flagged", 0),
            _mk(2, "deposit", 500),
            _mk(3, "new", 0),
        ]
        output, total, hidden = _capture(addrs, show_empty=False)

        assert "bc1qaddr0000" not in output  # used-empty suppressed
        assert "bc1qaddr0001" not in output  # flagged suppressed
        assert "bc1qaddr0002" in output  # funded deposit is shown
        assert "bc1qaddr0003" in output  # fresh "new" is shown
        # Both unsafe-to-reuse entries counted as hidden.
        assert hidden == 2
        assert total == 500

    def test_show_empty_true_still_prints_used_empty_and_flagged(self) -> None:
        """Power users running `jm-wallet info --extended --show-empty`
        must still see the full picture (issue #463)."""
        addrs = [
            _mk(0, "used-empty", 0),
            _mk(1, "flagged", 0),
            _mk(2, "new", 0),
        ]
        output, _, hidden = _capture(addrs, show_empty=True)

        assert "bc1qaddr0000" in output
        assert "bc1qaddr0001" in output
        assert "bc1qaddr0002" in output
        assert hidden == 0


class TestPrintBranchAddressesConfirmations:
    """Confirmation count is shown for funded addresses."""

    def test_exact_conf_count_shown_below_5(self) -> None:
        addr = "bc1qtest0001"
        utxo = _mk_utxo(addr, confirmations=3)
        ai = AddressInfo(
            address=addr,
            index=0,
            balance=100_000,
            status="deposit",
            path="m/84'/0'/0'/0/0",
            is_external=True,
            utxos=[utxo],
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_branch_addresses([ai], pending_addresses=set())
        assert "3 conf" in buf.getvalue()

    def test_capped_at_5_plus(self) -> None:
        addr = "bc1qtest0002"
        utxo = _mk_utxo(addr, confirmations=10)
        ai = AddressInfo(
            address=addr,
            index=0,
            balance=100_000,
            status="deposit",
            path="m/84'/0'/0'/0/0",
            is_external=True,
            utxos=[utxo],
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_branch_addresses([ai], pending_addresses=set())
        assert "5+ conf" in buf.getvalue()
        assert "10 conf" not in buf.getvalue()

    def test_per_utxo_conf_shown_for_multiple_utxos(self) -> None:
        """When an address has multiple UTXOs, each UTXO's confirmation count is shown."""
        addr = "bc1qtest0003"
        utxos = [_mk_utxo(addr, confirmations=2), _mk_utxo(addr, confirmations=7)]
        ai = AddressInfo(
            address=addr,
            index=0,
            balance=200_000,
            status="deposit",
            path="m/84'/0'/0'/0/0",
            is_external=True,
            utxos=utxos,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_branch_addresses([ai], pending_addresses=set())
        output = buf.getvalue()
        # Each UTXO is listed individually with its own confirmation count.
        assert "2 conf" in output
        assert "5+ conf" in output

    def test_no_conf_shown_for_empty_address(self) -> None:
        ai = _mk(0, "new", 0)
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_branch_addresses([ai], pending_addresses=set(), show_empty=True)
        assert "conf" not in buf.getvalue()


class TestPrintBranchAddressesReused:
    """Reused addresses keep the underlying UTXO label visible (issue #564)."""

    def test_reused_shows_base_status_on_every_utxo_line(self) -> None:
        addr = "bc1qtest0004"
        utxos = [_mk_utxo(addr, confirmations=6), _mk_utxo(addr, confirmations=6)]
        ai = AddressInfo(
            address=addr,
            index=0,
            balance=200_000,
            status="reused",
            path="m/84'/0'/3'/1/1",
            is_external=False,
            utxos=utxos,
            base_status="non-cj-change",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_branch_addresses([ai], pending_addresses=set())
        output = buf.getvalue()
        # Both UTXO lines carry the combined label, not a bare "reused".
        assert output.count("non-cj-change (reused)") == 2

    def test_reused_single_utxo_shows_base_status(self) -> None:
        """Single auto-frozen UTXO on a reused deposit address."""
        addr = "bc1qtest0005"
        utxo = _mk_utxo(addr, confirmations=2)
        ai = AddressInfo(
            address=addr,
            index=0,
            balance=100_000,
            status="reused",
            path="m/84'/0'/0'/0/1",
            is_external=True,
            utxos=[utxo],
            base_status="deposit",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_branch_addresses([ai], pending_addresses=set())
        assert "deposit (reused)" in buf.getvalue()

    def test_reused_without_base_status_falls_back_to_plain_label(self) -> None:
        """Callers that do not populate base_status still get the old label."""
        addr = "bc1qtest0006"
        utxos = [_mk_utxo(addr, confirmations=6), _mk_utxo(addr, confirmations=6)]
        ai = AddressInfo(
            address=addr,
            index=0,
            balance=200_000,
            status="reused",
            path="m/84'/0'/0'/0/2",
            is_external=True,
            utxos=utxos,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            _print_branch_addresses([ai], pending_addresses=set())
        output = buf.getvalue()
        assert "reused" in output
        assert "(reused)" not in output
