"""Tests for taker-side combined channel-as-change coordination."""

from __future__ import annotations

from jmcore.bitcoin import (
    address_to_scriptpubkey,
    create_p2tr_scriptpubkey,
    parse_transaction,
    scriptpubkey_to_address,
)

from taker.combine import (
    CombineMaker,
    combine_groups_from_pairs,
    pair_combine_makers,
)
from taker.tx_builder import build_coinjoin_tx


def _addr(byte: int) -> str:
    return scriptpubkey_to_address(create_p2tr_scriptpubkey(bytes([byte]) * 32), "regtest")


# valid, distinct regtest addresses
TAKER_CJ = _addr(0x01)
TAKER_CHG = _addr(0x02)
A_CJ = _addr(0x03)
A_CHG = _addr(0x04)
B_CJ = _addr(0x05)
B_CHG = _addr(0x06)


def test_pair_combine_makers_pairs_eligible_and_leaves_odd_out() -> None:
    makers = [
        CombineMaker("m1", 500_000, True, "02aa@h:9735"),
        CombineMaker("m2", 400_000, True, "02bb@h:9735"),
        CombineMaker("m3", 300_000, True, "02cc@h:9735"),  # odd one out
        CombineMaker("m4", 200_000, False, None),  # not capable
    ]
    pairs = pair_combine_makers(makers)
    assert len(pairs) == 1
    pair = pairs[0]
    assert (pair.funder, pair.folder) == ("m1", "m2")
    assert pair.pooled_total == 900_000
    assert pair.push_to_folder_msat == 400_000 * 1000
    assert combine_groups_from_pairs(pairs) == {"m2": "m1"}


def test_pair_combine_requires_capability_and_ln_node() -> None:
    makers = [
        CombineMaker("m1", 500_000, True, None),  # capable but no LN node
        CombineMaker("m2", 400_000, False, "02bb@h:9735"),  # LN node but not capable
    ]
    assert pair_combine_makers(makers) == []


def _maker_data():
    return {
        "A": {
            "utxos": [{"txid": "a" * 64, "vout": 0, "value": 1_500_000}],
            "cj_addr": A_CJ,
            "change_addr": A_CHG,
            "cjfee": 1000,
            "txfee": 0,
        },
        "B": {
            "utxos": [{"txid": "b" * 64, "vout": 0, "value": 1_400_000}],
            "cj_addr": B_CJ,
            "change_addr": B_CHG,
            "cjfee": 1000,
            "txfee": 0,
        },
    }


def _count_outputs_to(tx_hex: str, address: str) -> list[int]:
    spk = address_to_scriptpubkey(address)
    tx = parse_transaction(tx_hex)
    return [o.value for o in tx.outputs if o.script == spk]


def test_build_coinjoin_combined_pools_change_into_one_output() -> None:
    taker_utxos = [{"txid": "c" * 64, "vout": 0, "value": 5_000_000}]
    cj_amount = 1_000_000

    # Without combining: each maker gets its own change output.
    tx_plain, _ = build_coinjoin_tx(
        taker_utxos, TAKER_CJ, TAKER_CHG, 5_000_000, _maker_data(), cj_amount, 2000, "regtest"
    )
    plain_hex = tx_plain.hex()
    assert _count_outputs_to(plain_hex, A_CHG) == [501_000]  # 1.5M - 1M - 0 + 1000
    assert _count_outputs_to(plain_hex, B_CHG) == [401_000]

    # Combined: B's change folds into A's single shared funding output.
    tx_combined, _ = build_coinjoin_tx(
        taker_utxos,
        TAKER_CJ,
        TAKER_CHG,
        5_000_000,
        _maker_data(),
        cj_amount,
        2000,
        "regtest",
        combine_groups={"B": "A"},
    )
    combined_hex = tx_combined.hex()
    assert _count_outputs_to(combined_hex, A_CHG) == [501_000 + 401_000]  # pooled
    assert _count_outputs_to(combined_hex, B_CHG) == []  # folder has no change output


def test_build_coinjoin_split_change_into_several_outputs() -> None:
    """A maker can split its change across several funding outputs (subset-sum)."""
    taker_utxos = [{"txid": "c" * 64, "vout": 0, "value": 5_000_000}]
    cj_amount = 1_000_000
    split_a = _addr(0x11)
    split_b = _addr(0x12)
    data = _maker_data()
    # A's change is 501_000; split it into two funding outputs that sum to it.
    data["A"]["change_outputs"] = [(split_a, 250_000), (split_b, 251_000)]

    tx, _ = build_coinjoin_tx(
        taker_utxos, TAKER_CJ, TAKER_CHG, 5_000_000, data, cj_amount, 2000, "regtest"
    )
    tx_hex = tx.hex()
    assert _count_outputs_to(tx_hex, split_a) == [250_000]
    assert _count_outputs_to(tx_hex, split_b) == [251_000]
    # no single output equals A's exact residual (501_000)
    assert _count_outputs_to(tx_hex, A_CHG) == []


def test_split_change_must_sum_to_change() -> None:
    import pytest

    taker_utxos = [{"txid": "c" * 64, "vout": 0, "value": 5_000_000}]
    data = _maker_data()
    data["A"]["change_outputs"] = [(_addr(0x11), 100_000), (_addr(0x12), 100_000)]  # wrong sum
    with pytest.raises(ValueError, match="!= computed change"):
        build_coinjoin_tx(
            taker_utxos, TAKER_CJ, TAKER_CHG, 5_000_000, data, 1_000_000, 2000, "regtest"
        )


def test_plan_combine_session_pools_into_real_shared_2of2() -> None:
    """The taker plan + builder emit ONE maker-to-maker shared 2-of-2 output."""
    from coincurve import PrivateKey
    from jmswap.channel import combined_funding_address

    from taker.combine import plan_combine_session

    a_pub = PrivateKey().public_key.format()
    b_pub = PrivateKey().public_key.format()
    # A change = 1.5M - 1M - 0 + 1000 = 501_000 ; B change = 401_000 ; pooled = 902_000
    plan = plan_combine_session(
        [("A", a_pub.hex(), 501_000), ("B", b_pub.hex(), 401_000)], "regtest"
    )
    shared = combined_funding_address([a_pub, b_pub], "regtest")
    assert plan.combine_groups == {"B": "A"}
    assert plan.change_outputs["A"] == [(shared, 902_000)]
    assert plan.relay["A"].partner_change == 401_000
    assert plan.relay["B"].shared_address == shared

    data = _maker_data()
    data["A"]["change_outputs"] = plan.change_outputs["A"]
    tx, _ = build_coinjoin_tx(
        [{"txid": "c" * 64, "vout": 0, "value": 5_000_000}],
        TAKER_CJ,
        TAKER_CHG,
        5_000_000,
        data,
        1_000_000,
        2000,
        "regtest",
        combine_groups=plan.combine_groups,
    )
    tx_hex = tx.hex()
    # exactly one output to the shared 2-of-2 with the pooled amount, none to plain changes
    assert _count_outputs_to(tx_hex, shared) == [902_000]
    assert _count_outputs_to(tx_hex, A_CHG) == []
    assert _count_outputs_to(tx_hex, B_CHG) == []


def test_plan_knapsack_session_splits_into_per_maker_2of2_grid_pieces() -> None:
    """Knapsack: each maker's change becomes grid pieces at its own 2-of-2."""
    from coincurve import PrivateKey
    from jmswap.channel import combined_funding_address

    from taker.knapsack import plan_knapsack_session

    a, b = PrivateKey().public_key.format(), PrivateKey().public_key.format()
    # A change 501_000 (5 grid + 1_000), B change 401_000 (4 grid + 1_000); grid 100_000
    plan = plan_knapsack_session(
        [("A", a.hex(), 501_000), ("B", b.hex(), 401_000)],
        denomination=1_000_000,
        network="regtest",
        grid_div=10,
        max_pieces=12,
    )
    assert plan.combine_groups == {}  # no folding; each keeps its own pieces
    ab = combined_funding_address([a, b], "regtest")
    # A and B both pair with each other (round-robin of two), same 2-of-2 address
    assert plan.relay["A"].shared_address == ab and plan.relay["B"].shared_address == ab
    assert plan.relay["A"].partner_change == 0  # each 2-of-2 holds only its owner's change
    a_vals = [v for _, v in plan.change_outputs["A"]]
    assert a_vals == [100_000, 100_000, 100_000, 100_000, 100_000, 1_000]
    assert sum(a_vals) == 501_000

    data = _maker_data()
    data["A"]["change_outputs"] = plan.change_outputs["A"]
    data["B"]["change_outputs"] = plan.change_outputs["B"]
    tx, _ = build_coinjoin_tx(
        [{"txid": "c" * 64, "vout": 0, "value": 5_000_000}],
        TAKER_CJ,
        TAKER_CHG,
        5_000_000,
        data,
        1_000_000,
        2000,
        "regtest",
    )
    # equal 100_000 grid pieces from both makers land at the shared 2-of-2 (5 + 4 = 9)
    assert _count_outputs_to(tx.hex(), ab).count(100_000) == 9
