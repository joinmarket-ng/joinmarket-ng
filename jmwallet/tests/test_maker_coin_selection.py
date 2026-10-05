"""Maker policy vectors, proportional consolidation, and disclosure invariants."""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import Mock

import pytest

from jmwallet.wallet.coin_selection import CoinSelectionMixin
from jmwallet.wallet.models import UTXOInfo

ALGORITHMS = ("default", "gradual", "greedy", "greediest", "random")


def _coins(values: Sequence[int], mixdepth: int = 1) -> list[UTXOInfo]:
    return [
        UTXOInfo(
            txid=f"{index:064x}",
            vout=0,
            value=value,
            address=f"bcrt1test{index}",
            confirmations=10,
            scriptpubkey="0014" + "01" * 20,
            path="",
            mixdepth=mixdepth,
        )
        for index, value in enumerate(values, start=1)
    ]


@pytest.fixture
def selector() -> CoinSelectionMixin:
    selector = CoinSelectionMixin()
    selector.utxo_cache = {}
    return selector


@pytest.fixture
def controlled_random(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable top-ups and ordering changes unless a test explicitly overrides them."""
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.randrange", lambda _: 0)
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.shuffle", lambda _: None)


@pytest.mark.usefixtures("controlled_random")
@pytest.mark.parametrize(
    ("algorithm", "values", "target", "expected"),
    [
        ("default", [100, 60, 30, 10], 50, [60]),
        ("default", [100, 60, 30, 10], 140, [100, 60]),
        ("gradual", [1, 1, 1, 6, 8, 20], 10, [6, 8]),
        ("greedy", [1, 1, 1, 6, 8, 20], 10, [8, 1, 1]),
        ("greediest", [1, 1, 1, 6, 8, 20], 10, [1, 1, 1, 6, 8]),
        ("gradual", [1, 2, 3, 8, 20], 10, [3, 8]),
        ("gradual", [1, 2, 11, 20], 10, [11]),
        ("greediest", [1, 2, 11, 20], 10, [11]),
        ("greedy", [1, 2, 11, 20], 10, [11]),
        ("greedy", [1, 2, 3, 8], 6, [1, 2, 3]),
        ("greedy", [2], 1, [2]),
        ("greedy", [2, 3], 1, [2]),
    ],
)
def test_reference_selection_vectors(
    selector: CoinSelectionMixin,
    algorithm: str,
    values: list[int],
    target: int,
    expected: list[int],
) -> None:
    """Positive-target vectors derived from clientserver's support.py selectors."""
    selector.utxo_cache[1] = _coins(values)
    selected = selector.select_utxos_with_merge(1, target, merge_algorithm=algorithm)
    assert [u.value for u in selected] == expected
    assert len({u.outpoint for u in selected}) == len(selected)


@pytest.mark.usefixtures("controlled_random")
@pytest.mark.parametrize("n", [1, 2, 3, 4, 10, 30])
def test_default_top_up_probability_is_exact(
    selector: CoinSelectionMixin, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    selector.utxo_cache[1] = _coins([100] * n)
    top_ups = 0
    for draw in range(n):
        rng = Mock(return_value=draw)
        monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.randrange", rng)
        selected = selector.select_utxos_with_merge(1, 80)
        if n > 2:
            rng.assert_called_once_with(n)
            assert len(selected) == (3 if draw >= 2 else 1)
        else:
            rng.assert_not_called()
            assert len(selected) == 1
        top_ups += len(selected) == 3
        assert len({u.outpoint for u in selected}) == len(selected)
    assert top_ups == max(0, n - 2)


@pytest.mark.usefixtures("controlled_random")
def test_default_top_up_randomizes_extra_identities(
    selector: CoinSelectionMixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    selector.utxo_cache[1] = _coins([100, 60, 30, 10])
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.randrange", lambda _: 2)
    sample = Mock(side_effect=lambda remaining, count: remaining[:count])
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.sample", sample)
    selected = selector.select_utxos_with_merge(1, 50)
    assert [u.value for u in selected] == [60, 100, 30]
    assert [u.value for u in sample.call_args.args[0]] == [100, 30, 10]
    assert sample.call_args.args[1] == 2


@pytest.mark.usefixtures("controlled_random")
@pytest.mark.parametrize(("target", "funding_count"), [(100, 2), (145, 3), (175, 4)])
def test_default_only_tops_up_to_three(
    selector: CoinSelectionMixin,
    monkeypatch: pytest.MonkeyPatch,
    target: int,
    funding_count: int,
) -> None:
    selector.utxo_cache[1] = _coins([60, 50, 40, 30, 20])
    rng = Mock(return_value=2)
    sample = Mock(side_effect=lambda remaining, count: remaining[:count])
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.randrange", rng)
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.sample", sample)
    selected = selector.select_utxos_with_merge(1, target)
    assert len(selected) == max(3, funding_count)
    assert sum(u.value for u in selected) >= target
    if funding_count < 3:
        rng.assert_called_once_with(5)
        assert sample.call_args.args[1] == 1
    else:
        rng.assert_not_called()
        sample.assert_not_called()


@pytest.mark.usefixtures("controlled_random")
def test_default_probability_excludes_unavailable_coins(
    selector: CoinSelectionMixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    coins = _coins([100] * 6)
    coins[1].frozen = True
    coins[2].confirmations = 0
    coins[3].locktime = 1893456000
    selector.utxo_cache[1] = coins
    rng = Mock()
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.randrange", rng)
    selected = selector.select_utxos_with_merge(1, 80, exclude={(coins[4].txid, 0)})
    assert selected == [coins[0]]
    rng.assert_not_called()  # Two eligible coins, not six cached coins.


@pytest.mark.usefixtures("controlled_random")
def test_default_md0_top_up_only_uses_authorized_lineage(
    selector: CoinSelectionMixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    coins = _coins([100] * 8, mixdepth=0)
    selector.utxo_cache[0] = coins
    authorized = {u.outpoint for u in coins[:3]}
    rng = Mock(return_value=2)
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.randrange", rng)
    selected = selector.select_utxos_with_merge(0, 80, md0_mergeable_outpoints=authorized)
    rng.assert_called_once_with(3)
    assert {u.outpoint for u in selected} == authorized


@pytest.mark.usefixtures("controlled_random")
@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_md0_single_fallback_chooses_smallest_sufficient(
    selector: CoinSelectionMixin, monkeypatch: pytest.MonkeyPatch, algorithm: str
) -> None:
    coins = _coins([10, 90, 50, 70], mixdepth=0)
    selector.utxo_cache[0] = coins
    rng = Mock()
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.randrange", rng)
    selected = selector.select_utxos_with_merge(
        0, 50, merge_algorithm=algorithm, md0_mergeable_outpoints={coins[0].outpoint}
    )
    assert selected == [coins[2]]
    rng.assert_not_called()


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_md0_never_combines_unrelated_coins(selector: CoinSelectionMixin, algorithm: str) -> None:
    selector.utxo_cache[0] = _coins([60] * 10, mixdepth=0)
    with pytest.raises(ValueError, match="Cannot merge non-CJ md0 UTXOs"):
        selector.select_utxos_with_merge(0, 100, merge_algorithm=algorithm)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_policy_eligibility_and_funding_errors(
    selector: CoinSelectionMixin, algorithm: str
) -> None:
    coins = _coins([10, 20, 100, 100, 100, 100])
    coins[2].frozen = True
    coins[3].confirmations = 0
    coins[4].locktime = 1893456000
    selector.utxo_cache[1] = coins
    excluded = {(coins[5].txid, 0)}
    selected = selector.select_utxos_with_merge(1, 30, merge_algorithm=algorithm, exclude=excluded)
    assert {u.outpoint for u in selected} == {u.outpoint for u in coins[:2]}
    with pytest.raises(ValueError, match="Insufficient confirmed funds"):
        selector.select_utxos_with_merge(1, 31, merge_algorithm=algorithm, exclude=excluded)
    coins[3].confirmations = 10
    coins[3].frozen = True
    with pytest.raises(ValueError, match="Insufficient funds"):
        selector.select_utxos_with_merge(1, 31, merge_algorithm=algorithm, exclude=excluded)


@pytest.mark.usefixtures("controlled_random")
@pytest.mark.parametrize("algorithm", ALGORITHMS)
@pytest.mark.parametrize("target", [0, -10])
def test_unrestricted_fee_covered_target_still_selects_an_auth_input(
    selector: CoinSelectionMixin, algorithm: str, target: int
) -> None:
    selector.utxo_cache[1] = _coins([6, 10])
    selected = selector.select_utxos_with_merge(1, target, merge_algorithm=algorithm)
    assert len(selected) == 1


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_empty_positive_target_fails(selector: CoinSelectionMixin, algorithm: str) -> None:
    with pytest.raises(ValueError, match="Insufficient funds"):
        selector.select_utxos_with_merge(1, 1, merge_algorithm=algorithm)


def test_random_prunes_dust_before_disclosure(
    selector: CoinSelectionMixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    coins = _coins([100, 1, 1, 1, 1, 1])
    selector.utxo_cache[1] = coins
    shuffle = Mock(side_effect=lambda items: items.sort(key=lambda u: u.value))
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.shuffle", shuffle)
    selected = selector.select_utxos_with_merge(1, 50, merge_algorithm="random")
    assert selected == [coins[0]]
    assert shuffle.call_count == 3


def test_random_can_choose_different_equal_value_identities(
    selector: CoinSelectionMixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    coins = _coins([100, 100, 100])
    selector.utxo_cache[1] = coins
    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.shuffle", lambda _: None)
    first = selector.select_utxos_with_merge(1, 80, merge_algorithm="random")
    monkeypatch.setattr(
        "jmwallet.wallet.coin_selection.secure_random.shuffle", lambda items: items.reverse()
    )
    second = selector.select_utxos_with_merge(1, 80, merge_algorithm="random")
    assert first == [coins[0]]
    assert second == [coins[2]]
    assert selector.utxo_cache[1] == coins


def test_random_is_inclusion_minimal_not_minimum_count(
    selector: CoinSelectionMixin, monkeypatch: pytest.MonkeyPatch
) -> None:
    coins = _coins([6, 6, 10])
    selector.utxo_cache[1] = coins
    calls = 0

    def shuffle(items: list[UTXOInfo]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            items.sort(key=lambda u: u.value)
        else:
            items.reverse()

    monkeypatch.setattr("jmwallet.wallet.coin_selection.secure_random.shuffle", shuffle)
    selected = selector.select_utxos_with_merge(1, 10, merge_algorithm="random")
    assert selected == [coins[1], coins[0]]  # Independent final order, not funding order.
    assert calls == 3
    assert all(sum(other.value for other in selected if other is not u) < 10 for u in selected)


@pytest.mark.usefixtures("controlled_random")
def test_random_does_not_impose_an_unfundable_five_input_cap(selector: CoinSelectionMixin) -> None:
    selector.utxo_cache[1] = _coins([10] * 6)
    selected = selector.select_utxos_with_merge(1, 55, merge_algorithm="random")
    assert len(selected) == 6
    assert sum(u.value for u in selected) == 60
