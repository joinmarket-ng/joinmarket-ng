"""Tests for the true-pooling (ring) knapsack planner.

The ring turns each channel party's change into contributions to two equal-valued
shared outputs, so no output equals any single party's change and every output is
co-funded by two parties (non-zero on both sides).
"""

from __future__ import annotations

from jmcore.bitcoin import (
    create_p2tr_scriptpubkey,
    parse_transaction,
    scriptpubkey_to_address,
)

from taker.knapsack import (
    PooledPlan,
    plan_pooled_knapsack,
)
from taker.tx_builder import build_coinjoin_tx


def _addr(byte: int) -> str:
    return scriptpubkey_to_address(create_p2tr_scriptpubkey(bytes([byte]) * 32), "regtest")


def _check_invariants(plan: PooledPlan, parties: dict[str, int], *, min_channel: int) -> None:
    # Every channel output is co-funded by two distinct parties, both strictly positive,
    # and the value is exactly the sum of contributions.
    for ch in plan.channels:
        assert ch.party_a != ch.party_b
        assert ch.contrib_a > 0 and ch.contrib_b > 0
        assert ch.value == ch.contrib_a + ch.contrib_b
    # A ring (3+ parties) makes every output equal within one sat, so no single output
    # is distinguishable as anyone's change (a 2-party pool is a single output).
    if len(plan.channels) >= 2:
        values = [ch.value for ch in plan.channels]
        assert max(values) - min(values) <= 1
    # Each channel party's contributions sum to exactly its change.
    contribs = plan.contributions_by_party()
    for nick, total in contribs.items():
        assert total == parties[nick], (nick, total, parties[nick])
    # Plain parties keep their change out of channels; channel parties never appear plain.
    assert set(contribs).isdisjoint(plan.plain)
    assert set(contribs) | set(plan.plain) == set(parties)


def test_three_balanced_parties_form_equal_ring() -> None:
    parties = {"A": 500_000, "B": 520_000, "C": 480_000}
    plan = plan_pooled_knapsack(list(parties.items()), min_channel=200_000)
    assert len(plan.channels) == 3
    assert not plan.plain
    values = {ch.value for ch in plan.channels}
    # All outputs equal within one sat (S=1_500_000, N=3 -> exactly 500_000 each).
    assert max(values) - min(values) <= 1
    _check_invariants(plan, parties, min_channel=200_000)


def test_taker_change_folded_into_ring() -> None:
    parties = {"A": 400_000, "B": 450_000, "C": 410_000, "TAKER": 440_000}
    plan = plan_pooled_knapsack(list(parties.items()), min_channel=200_000)
    assert len(plan.channels) == 4
    # The taker co-funds channels like any other party (no standalone taker change).
    assert "TAKER" in plan.contributions_by_party()
    assert "TAKER" not in plan.plain
    _check_invariants(plan, parties, min_channel=200_000)


def test_small_party_stays_plain() -> None:
    parties = {"A": 500_000, "B": 520_000, "C": 480_000, "D": 50_000}
    plan = plan_pooled_knapsack(list(parties.items()), min_channel=200_000)
    assert "D" in plan.plain
    assert len(plan.channels) == 3
    _check_invariants(plan, parties, min_channel=200_000)


def test_outlier_excluded_to_keep_equal_outputs() -> None:
    # E dwarfs the others; the ring cannot stay equal-valued with it, so it is dropped.
    parties = {"A": 300_000, "B": 320_000, "C": 310_000, "E": 5_000_000}
    plan = plan_pooled_knapsack(list(parties.items()), min_channel=200_000)
    assert "E" in plan.plain
    assert {ch.party_a for ch in plan.channels} | {ch.party_b for ch in plan.channels} == {
        "A",
        "B",
        "C",
    }
    _check_invariants(plan, parties, min_channel=200_000)


def test_two_parties_degenerate_to_single_pool() -> None:
    parties = {"A": 500_000, "B": 700_000}
    plan = plan_pooled_knapsack(list(parties.items()), min_channel=200_000)
    assert len(plan.channels) == 1
    ch = plan.channels[0]
    assert ch.value == 1_200_000
    assert {ch.party_a, ch.party_b} == {"A", "B"}
    _check_invariants(plan, parties, min_channel=200_000)


def test_all_small_all_plain() -> None:
    parties = {"A": 10_000, "B": 12_000, "C": 9_000}
    plan = plan_pooled_knapsack(list(parties.items()), min_channel=200_000)
    assert not plan.channels
    assert set(plan.plain) == set(parties)


def test_deterministic() -> None:
    parties = [("C", 480_000), ("A", 500_000), ("B", 520_000)]
    p1 = plan_pooled_knapsack(parties, min_channel=200_000)
    p2 = plan_pooled_knapsack(list(reversed(parties)), min_channel=200_000)
    assert p1 == p2


def test_ring_values_sum_to_total_change() -> None:
    parties = {"A": 333_333, "B": 451_111, "C": 502_222, "D": 388_888}
    plan = plan_pooled_knapsack(list(parties.items()), min_channel=200_000)
    channel_total = sum(parties[n] for n in plan.contributions_by_party())
    assert sum(ch.value for ch in plan.channels) == channel_total
    _check_invariants(plan, parties, min_channel=200_000)


def test_ring_merges_contributions_into_shared_outputs_in_build() -> None:
    """Each party supplies its per-channel contributions as change pieces; the builder
    merges same-address pieces into one shared 2-of-2 output worth the channel value."""
    # changes: A=501000, B=401000, C=451000 (input - 1_000_000 cj + 1000 cjfee).
    maker_data = {
        "A": {
            "utxos": [{"txid": "a" * 64, "vout": 0, "value": 1_500_000}],
            "cj_addr": _addr(0x03),
            "change_addr": _addr(0x04),
            "cjfee": 1000,
            "txfee": 0,
        },
        "B": {
            "utxos": [{"txid": "b" * 64, "vout": 0, "value": 1_400_000}],
            "cj_addr": _addr(0x05),
            "change_addr": _addr(0x06),
            "cjfee": 1000,
            "txfee": 0,
        },
        "C": {
            "utxos": [{"txid": "c" * 64, "vout": 0, "value": 1_450_000}],
            "cj_addr": _addr(0x07),
            "change_addr": _addr(0x08),
            "cjfee": 1000,
            "txfee": 0,
        },
    }
    plan = plan_pooled_knapsack(
        [("A", 501_000), ("B", 401_000), ("C", 451_000)], min_channel=200_000
    )
    assert len(plan.channels) == 3

    chan_addr = {i: _addr(0x20 + i) for i in range(len(plan.channels))}
    per_party: dict[str, list[tuple[str, int]]] = {}
    for i, ch in enumerate(plan.channels):
        per_party.setdefault(ch.party_a, []).append((chan_addr[i], ch.contrib_a))
        per_party.setdefault(ch.party_b, []).append((chan_addr[i], ch.contrib_b))
    for nick, pieces in per_party.items():
        maker_data[nick]["change_outputs"] = pieces

    tx_bytes, _ = build_coinjoin_tx(
        [{"txid": "d" * 64, "vout": 0, "value": 5_000_000}],
        _addr(0x01),
        _addr(0x02),
        5_000_000,
        maker_data,
        1_000_000,
        2000,
        "regtest",
        merge_change_by_address=True,
    )
    tx = parse_transaction(tx_bytes.hex())
    from jmcore.bitcoin import address_to_scriptpubkey

    for i, ch in enumerate(plan.channels):
        spk = address_to_scriptpubkey(chan_addr[i])
        vals = [o.value for o in tx.outputs if o.script == spk]
        # exactly one merged output per channel, worth the full channel value
        assert vals == [ch.value], (i, vals, ch.value)
