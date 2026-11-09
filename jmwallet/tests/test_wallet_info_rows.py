"""
Tests for the canonical wallet-info row model (jmwallet.wallet.rows).

The row model is the single source of truth for all wallet info renderers;
these tests pin the AddressInfo -> row translation (labels, reused flag,
fidelity bond lock states), the visibility/hidden-count logic (mirroring the
legacy display), and the pipeline stages (filter/sort/group/summarize).
"""

from __future__ import annotations

from datetime import datetime

from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwallet.wallet.models import AddressInfo, AddressStatus, UTXOInfo
from jmwallet.wallet.rows import (
    AddressRow,
    BranchSummaryRow,
    HiddenRow,
    UtxoRow,
    base_order_key,
    build_branch_rows,
    derive_label,
    fb_date_str,
    filter_rows,
    group_rows,
    path_order_key,
    sort_rows,
    summarize_branch,
    summarize_mixdepth,
)

NOW = 1_800_000_000  # fixed reference time for locktime-dependent tests


def _utxo(
    txid: str = "ab" * 32,
    vout: int = 0,
    value: int = 100_000,
    address: str = "bc1qaddr",
    confirmations: int = 3,
    mixdepth: int = 0,
    frozen: bool = False,
    locktime: int | None = None,
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid,
        vout=vout,
        value=value,
        address=address,
        confirmations=confirmations,
        scriptpubkey="0014" + "00" * 20,
        path="m/84'/1'/0'/0/0",
        mixdepth=mixdepth,
        frozen=frozen,
        locktime=locktime,
    )


def _addr(
    address: str = "bc1qaddr",
    index: int = 0,
    balance: int = 0,
    status: AddressStatus = "new",
    is_external: bool = True,
    utxos: list[UTXOInfo] | None = None,
    base_status: AddressStatus | None = None,
    is_bond: bool = False,
    locktime: int | None = None,
    label: str = "",
) -> AddressInfo:
    return AddressInfo(
        address=address,
        index=index,
        balance=balance,
        status=status,
        path=f"m/84'/1'/0'/{0 if is_external else 1}/{index}",
        is_external=is_external,
        is_bond=is_bond,
        locktime=locktime,
        utxos=utxos or [],
        label=label,
        base_status=base_status,
    )


class TestDeriveLabel:
    def test_reused_keeps_base_status(self) -> None:
        """Issue #564: a reused address keeps its underlying funded label."""
        addr = _addr(status="reused", base_status="cj-out", balance=50_000)
        assert derive_label(addr, now=NOW) == ("cj-o", True)

    def test_reused_deposit_base(self) -> None:
        addr = _addr(status="reused", base_status="deposit", balance=50_000)
        assert derive_label(addr, now=NOW) == ("dep", True)

    def test_reused_without_base_status_falls_back_by_branch(self) -> None:
        """base_status is always set in practice; the fallback must still
        produce a valid label (never the pseudo-status "reused")."""
        ext = _addr(status="reused", base_status=None, balance=1, is_external=True)
        internal = _addr(status="reused", base_status=None, balance=1, is_external=False)
        assert derive_label(ext, now=NOW) == ("dep", True)
        assert derive_label(internal, now=NOW) == ("chg", True)

    def test_bond_active_while_locked(self) -> None:
        addr = _addr(
            status="bond", is_bond=True, locktime=NOW + 86_400, balance=10_000, is_external=False
        )
        assert derive_label(addr, now=NOW) == ("fb-a", False)

    def test_bond_expired_after_locktime(self) -> None:
        addr = _addr(
            status="bond", is_bond=True, locktime=NOW - 86_400, balance=10_000, is_external=False
        )
        assert derive_label(addr, now=NOW) == ("fb-x", False)

    def test_bond_without_locktime_is_expired(self) -> None:
        addr = _addr(status="bond", is_bond=True, locktime=None, is_external=False)
        assert derive_label(addr, now=NOW) == ("fb-x", False)

    def test_bond_expiry_requires_strictly_passing_chain_time(self) -> None:
        for offset, expected_label in ((-1, "fb-x"), (0, "fb-a"), (1, "fb-a")):
            locktime = NOW + offset
            addr = _addr(
                status="bond",
                is_bond=True,
                is_external=False,
                balance=10_000,
                locktime=locktime,
                utxos=[_utxo(value=10_000, locktime=locktime)],
            )
            rows = build_branch_rows([addr], mixdepth=0, branch=2, now=NOW)
            assert isinstance(rows[0], UtxoRow)
            assert rows[0].label == expected_label
            summary = summarize_branch(rows, mixdepth=0, branch=2, now=NOW)
            assert summary.expired_spendable == (10_000 if offset < 0 else 0)
            assert summary.locked_balance == (0 if offset < 0 else 10_000)

    def test_plain_statuses_map_to_short_labels(self) -> None:
        """The long AddressStatus names map onto short (max 4 chars) row labels."""
        expected: dict[AddressStatus, str] = {
            "new": "new",
            "deposit": "dep",
            "cj-out": "cj-o",
            "cj-change": "cj-c",
            "used-empty": "used",
            "flagged": "flag",
            "reserved": "rsvd",
        }
        for status, label in expected.items():
            addr = _addr(status=status)
            assert derive_label(addr, now=NOW) == (label, False)

    def test_non_cj_change_surfaced_as_chg(self) -> None:
        """Display-level rename: ``AddressStatus`` keeps the historic
        ``non-cj-change`` (persisted metadata); rows show ``chg``."""
        addr = _addr(status="non-cj-change", balance=1)
        assert derive_label(addr, now=NOW) == ("chg", False)


class TestBuildBranchRows:
    def test_one_row_per_utxo(self) -> None:
        """Hard rule: one line per UTXO, address-level data carried on each."""
        utxos = [
            _utxo(vout=0, value=30_000),
            _utxo(vout=1, value=20_000),
        ]
        addr = _addr(status="deposit", balance=50_000, utxos=utxos)
        rows = build_branch_rows([addr], mixdepth=0, branch=0, now=NOW)

        assert len(rows) == 2
        first, second = rows
        assert isinstance(first, UtxoRow) and isinstance(second, UtxoRow)
        assert (first.utxo.value, first.original_seq) == (30_000, 0)
        assert (second.utxo.value, second.original_seq) == (20_000, 1)
        assert first.address == second.address == addr.address
        assert first.label == second.label == "dep"
        assert first.path == addr.path

    def test_reused_address_rows_carry_flag_and_base_label(self) -> None:
        utxos = [_utxo(vout=0), _utxo(vout=1)]
        addr = _addr(status="reused", base_status="cj-out", balance=200_000, utxos=utxos)
        rows = build_branch_rows([addr], mixdepth=0, branch=1, now=NOW)
        utxo_rows = [r for r in rows if isinstance(r, UtxoRow)]
        assert [r.label for r in utxo_rows] == ["cj-o", "cj-o"]
        assert all(r.reused for r in utxo_rows)

    def test_empty_reused_address_keeps_reused_flag(self) -> None:
        """An empty-but-reused address (visible with show_empty) must keep the
        RU flag so renderers can still warn about it."""
        addr = _addr(status="reused", base_status="cj-out", balance=0)
        rows = build_branch_rows([addr], mixdepth=0, branch=0, show_empty=True, now=NOW)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, AddressRow)
        assert row.label == "cj-o"
        assert row.reused is True

    def test_empty_addresses_hidden_by_default(self) -> None:
        """reserved always shown; up to 6 new shown; used-empty/flagged hidden."""
        addresses = [_addr(address=f"bc1qnew{i}", index=i, status="new") for i in range(7)]
        addresses.append(_addr(address="bc1qused", index=7, status="used-empty"))
        addresses.append(_addr(address="bc1qflag", index=8, status="flagged"))
        addresses.append(_addr(address="bc1qres", index=9, status="reserved", label="donation"))

        rows = build_branch_rows(addresses, mixdepth=0, branch=0, now=NOW)

        visible = [r for r in rows if isinstance(r, AddressRow)]
        hidden = [r for r in rows if isinstance(r, HiddenRow)]
        assert len(visible) == 7  # 6 new + 1 reserved
        assert visible[-1].label == "rsvd"
        assert visible[-1].user_label == "donation"
        assert len(hidden) == 1
        assert hidden[0].hidden_count == 3  # 1 surplus new + used-empty + flagged
        assert hidden[0].mixdepth == 0 and hidden[0].branch == 0

    def test_show_empty_disables_hiding(self) -> None:
        addresses = [_addr(address=f"bc1qnew{i}", index=i, status="new") for i in range(8)]
        addresses.append(_addr(address="bc1qused", index=8, status="used-empty"))
        rows = build_branch_rows(addresses, mixdepth=0, branch=0, show_empty=True, now=NOW)
        assert len(rows) == 9
        assert not any(isinstance(r, HiddenRow) for r in rows)

    def test_pending_flag_set_from_pending_addresses(self) -> None:
        addr = _addr(address="bc1qpend", index=0, status="new")
        rows = build_branch_rows(
            [addr], mixdepth=0, branch=0, pending_addresses={"bc1qpend"}, now=NOW
        )
        assert isinstance(rows[0], AddressRow)
        assert rows[0].pending is True

    def test_fidelity_bond_branch_never_hides(self) -> None:
        """Redeemed (unfunded) bonds must stay visible; no HiddenRow on the
        fidelity bond branch even with empty addresses present."""
        funded = _addr(
            address="bc1pbond0",
            index=0,
            status="bond",
            is_bond=True,
            is_external=False,
            locktime=NOW + 86_400,
            balance=10_000,
            utxos=[_utxo(address="bc1pbond0", locktime=NOW + 86_400, value=10_000)],
        )
        redeemed = _addr(
            address="bc1pbond1",
            index=1,
            status="bond",
            is_bond=True,
            is_external=False,
            locktime=NOW - 86_400,
            balance=0,
        )
        rows = build_branch_rows(
            [funded, redeemed], mixdepth=0, branch=FIDELITY_BOND_BRANCH, now=NOW
        )

        assert not any(isinstance(r, HiddenRow) for r in rows)
        utxo_rows = [r for r in rows if isinstance(r, UtxoRow)]
        addr_rows = [r for r in rows if isinstance(r, AddressRow)]
        assert len(utxo_rows) == 1 and utxo_rows[0].label == "fb-a"
        assert utxo_rows[0].locktime == NOW + 86_400
        assert len(addr_rows) == 1 and addr_rows[0].label == "fb-x"
        assert addr_rows[0].locktime == NOW - 86_400


class TestPipelineStages:
    def test_filter_rows_identity_without_predicate(self) -> None:
        rows = build_branch_rows(
            [_addr(status="new", index=0), _addr(status="new", index=1)],
            mixdepth=0,
            branch=0,
            now=NOW,
        )
        assert filter_rows(rows) == rows
        assert filter_rows(rows) is not rows  # defensive copy

    def test_filter_rows_with_predicate(self) -> None:
        rows = build_branch_rows(
            [_addr(status="new", index=0), _addr(status="reserved", index=1, label="x")],
            mixdepth=0,
            branch=0,
            show_empty=True,
            now=NOW,
        )
        kept = filter_rows(rows, lambda r: isinstance(r, AddressRow) and r.label == "rsvd")
        assert len(kept) == 1
        assert isinstance(kept[0], AddressRow) and kept[0].label == "rsvd"

    def test_base_order_mixdepth_branch_index_seq(self) -> None:
        """base_order = (mixdepth, branch rank ext<int<fb, index, cache seq)."""
        bond_built = build_branch_rows(
            [
                _addr(
                    status="bond",
                    is_bond=True,
                    is_external=False,
                    locktime=NOW + 1,
                    balance=5,
                    utxos=[_utxo(value=5, locktime=NOW + 1)],
                )
            ],
            mixdepth=0,
            branch=FIDELITY_BOND_BRANCH,
            now=NOW,
        )
        int_built = build_branch_rows(
            [
                _addr(
                    status="cj-out",
                    index=3,
                    balance=7,
                    is_external=False,
                    utxos=[_utxo(value=7)],
                )
            ],
            mixdepth=0,
            branch=1,
            now=NOW,
        )
        ext_built = build_branch_rows(
            [_addr(status="deposit", index=1, balance=9, utxos=[_utxo(value=9)])],
            mixdepth=0,
            branch=0,
            now=NOW,
        )
        # Intentionally out of order: fb first, internal, then external.
        rows: list[UtxoRow | AddressRow] = [
            r
            for built in (bond_built, int_built, ext_built)
            for r in built
            if isinstance(r, (UtxoRow, AddressRow))
        ]

        ordered = sort_rows(rows)
        assert [base_order_key(r) for r in ordered] == [
            (0, 0, 1, 0),  # external, index 1
            (0, 1, 3, 0),  # internal, index 3
            (0, 2, 0, 0),  # fidelity bond branch sorts last
        ]

    def test_sort_rows_custom_key_falls_back_to_base_order(self) -> None:
        """Ties under a user key keep the stable base order."""
        utxos = [
            _utxo(vout=0, value=50_000),
            _utxo(vout=1, value=50_000),
        ]
        addr = _addr(status="deposit", balance=100_000, utxos=utxos)
        built = build_branch_rows([addr], mixdepth=0, branch=0, now=NOW)
        rows = [r for r in built if isinstance(r, UtxoRow)]

        ordered = sort_rows(list(reversed(rows)), key=lambda r: -r.utxo.value)
        assert [r.original_seq for r in ordered] == [0, 1]

    def test_group_rows_buckets_by_mixdepth_branch(self) -> None:
        ext = build_branch_rows([_addr(status="new", index=0)], mixdepth=0, branch=0, now=NOW)
        internal = build_branch_rows(
            [_addr(address="bc1qint", status="new", index=0, is_external=False)],
            mixdepth=0,
            branch=1,
            now=NOW,
        )
        groups = group_rows([*ext, *internal])
        assert set(groups) == {(0, 0), (0, 1)}
        assert len(groups[(0, 0)]) == 1
        assert len(groups[(0, 1)]) == 1


class TestPathOrderKey:
    def test_orders_by_mixdepth_branch_rank_then_index(self) -> None:
        paths = [(2, 0, 1), (0, 1, 5), (0, 0, 9), (1, 0, 0), (0, 0, 2)]
        ordered = sorted(paths, key=path_order_key)
        assert ordered == [(0, 0, 2), (0, 0, 9), (0, 1, 5), (1, 0, 0), (2, 0, 1)]

    def test_fidelity_bond_branch_sorts_after_external_and_internal(self) -> None:
        paths = [(0, FIDELITY_BOND_BRANCH, 0), (0, 1, 0), (0, 0, 0)]
        ordered = sorted(paths, key=path_order_key)
        assert ordered == [(0, 0, 0), (0, 1, 0), (0, FIDELITY_BOND_BRANCH, 0)]

    def test_unknown_branch_falls_back_to_raw_value(self) -> None:
        paths = [(0, 5, 0), (0, FIDELITY_BOND_BRANCH, 0)]
        ordered = sorted(paths, key=path_order_key)
        assert ordered == [(0, FIDELITY_BOND_BRANCH, 0), (0, 5, 0)]

    def test_unresolvable_path_sorts_last(self) -> None:
        paths: list[tuple[int, int, int] | None] = [None, (1, 0, 0), (0, 0, 0), None]
        ordered = sorted(paths, key=path_order_key)
        assert ordered == [(0, 0, 0), (1, 0, 0), None, None]


class TestSummaries:
    def test_summarize_branch_balance_frozen_spendable(self) -> None:
        utxos = [
            _utxo(vout=0, value=30_000),
            _utxo(vout=1, value=20_000, frozen=True),
        ]
        addr = _addr(status="deposit", balance=50_000, utxos=utxos)
        rows = build_branch_rows([addr], mixdepth=0, branch=0, now=NOW)

        summary = summarize_branch(rows, mixdepth=0, branch=0, now=NOW)
        assert summary.balance == 50_000
        assert summary.frozen_balance == 20_000
        assert summary.spendable == 30_000
        assert summary.locked_balance == 0
        assert summary.expired_balance == 0
        assert summary.expired_frozen_balance == 0

    def test_summarize_branch_ignores_non_utxo_rows(self) -> None:
        """Hidden/address rows contribute nothing to the balance."""
        addresses = [_addr(address=f"bc1qnew{i}", index=i, status="new") for i in range(8)]
        rows = build_branch_rows(addresses, mixdepth=0, branch=0, now=NOW)
        assert any(isinstance(r, HiddenRow) for r in rows)
        summary = summarize_branch(rows, mixdepth=0, branch=0, now=NOW)
        assert summary.balance == 0
        assert summary.spendable == 0

    def test_summarize_branch_fidelity_bond_split(self) -> None:
        """FB funds split into locked/expired; spendable is always 0."""
        active = _addr(
            address="bc1pbond0",
            index=0,
            status="bond",
            is_bond=True,
            is_external=False,
            locktime=NOW + 86_400,
            balance=60_000,
            utxos=[_utxo(address="bc1pbond0", value=60_000, locktime=NOW + 86_400)],
        )
        expired = _addr(
            address="bc1pbond1",
            index=1,
            status="bond",
            is_bond=True,
            is_external=False,
            locktime=NOW - 86_400,
            balance=40_000,
            utxos=[_utxo(address="bc1pbond1", value=40_000, locktime=NOW - 86_400)],
        )
        rows = build_branch_rows(
            [active, expired], mixdepth=0, branch=FIDELITY_BOND_BRANCH, now=NOW
        )

        summary = summarize_branch(rows, mixdepth=0, branch=FIDELITY_BOND_BRANCH, now=NOW)
        assert summary.balance == 100_000
        assert summary.locked_balance == 60_000
        assert summary.expired_balance == 40_000
        assert summary.expired_frozen_balance == 0
        assert summary.expired_spendable == 40_000
        assert summary.spendable == 0

    def test_summarize_branch_expired_frozen_bond(self) -> None:
        """Frozen expired bonds are excluded from expired_spendable.

        A frozen *active* bond counts toward frozen_balance but must not
        reduce expired_frozen_balance.
        """
        locked_frozen = _addr(
            address="bc1pbond0",
            index=0,
            status="bond",
            is_bond=True,
            is_external=False,
            locktime=NOW + 86_400,
            balance=60_000,
            utxos=[_utxo(address="bc1pbond0", value=60_000, locktime=NOW + 86_400, frozen=True)],
        )
        expired_frozen = _addr(
            address="bc1pbond1",
            index=1,
            status="bond",
            is_bond=True,
            is_external=False,
            locktime=NOW - 86_400,
            balance=25_000,
            utxos=[_utxo(address="bc1pbond1", value=25_000, locktime=NOW - 86_400, frozen=True)],
        )
        expired = _addr(
            address="bc1pbond2",
            index=2,
            status="bond",
            is_bond=True,
            is_external=False,
            locktime=NOW - 86_400,
            balance=40_000,
            utxos=[_utxo(address="bc1pbond2", value=40_000, locktime=NOW - 86_400)],
        )
        rows = build_branch_rows(
            [locked_frozen, expired_frozen, expired],
            mixdepth=0,
            branch=FIDELITY_BOND_BRANCH,
            now=NOW,
        )
        summary = summarize_branch(rows, mixdepth=0, branch=FIDELITY_BOND_BRANCH, now=NOW)
        assert summary.balance == 125_000
        assert summary.frozen_balance == 85_000
        assert summary.locked_balance == 60_000
        assert summary.expired_balance == 65_000
        assert summary.expired_frozen_balance == 25_000
        assert summary.expired_spendable == 40_000
        assert summary.spendable == 0

    def test_summarize_mixdepth_includes_bonds_in_total_not_spendable(self) -> None:
        ext = BranchSummaryRow(mixdepth=0, branch=0, balance=50_000, frozen_balance=20_000)
        internal = BranchSummaryRow(mixdepth=0, branch=1, balance=10_000)
        bonds = BranchSummaryRow(mixdepth=0, branch=FIDELITY_BOND_BRANCH, balance=100_000)

        total = summarize_mixdepth([ext, internal, bonds], mixdepth=0)
        assert total.total_balance == 160_000
        assert total.spendable_balance == 30_000 + 10_000  # bonds excluded

    def test_fb_date_str_format(self) -> None:
        """YY-MM-DD keeps the fb-date column narrow."""
        locktime = NOW + 86_400
        assert fb_date_str(locktime) == datetime.fromtimestamp(locktime).strftime("%y-%m-%d")
