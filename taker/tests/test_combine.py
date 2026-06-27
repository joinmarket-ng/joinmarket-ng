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
