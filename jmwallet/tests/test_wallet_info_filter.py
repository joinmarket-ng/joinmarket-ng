"""Tests for empty-address filtering in `jm-wallet info --extended`.

Issue: after running a wallet for a few months, the extended view becomes
unreadable because most addresses have zero balance. This test exercises
the `_print_branch_addresses` helper that drives the filtering.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

from jmwallet.cli.wallet import _print_branch_addresses
from jmwallet.wallet.models import AddressInfo


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
            frozen_addresses=set(),
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
            _mk(4, "new", 0),  # hidden
        ]
        output, total, hidden = _capture(addrs, show_empty=False)

        # Non-empty address is always shown.
        assert "bc1qaddr0001" in output
        # First "new" receive address is surfaced so user sees a receive addr.
        assert "bc1qaddr0003" in output
        # Zero-balance used-empty/flagged lines are dropped.
        assert "bc1qaddr0000" not in output
        assert "bc1qaddr0002" not in output
        assert "bc1qaddr0004" not in output

        # 3 entries hidden (index 0, 2, 4); balance still totals everything.
        assert hidden == 3
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

    def test_only_first_new_address_kept_when_many_empty_new(self) -> None:
        """Multiple consecutive 'new' empty addresses: only the first is shown."""
        addrs = [_mk(i, "new", 0) for i in range(5)]
        output, _, hidden = _capture(addrs, show_empty=False)

        assert "bc1qaddr0000" in output
        for i in range(1, 5):
            assert f"bc1qaddr{i:04d}" not in output
        assert hidden == 4
