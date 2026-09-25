"""Ring-capable makers can fill surplus slots without displacing ordinary makers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from jmcore.models import NetworkType, OfferType
from jmcore.protocol import FEATURE_PRIVATE_CHANNEL_RING

from taker.taker import Taker


class Chooser:
    def __init__(self, offers: list[Any]) -> None:
        self.offers = offers
        self.ignored_makers: set[str] = set()
        self.pools: list[tuple[int, list[str]]] = []

    def select_makers(
        self,
        *,
        n: int,
        hard_exclude_nicks: set[str],
        exclude_nicks: set[str] | None = None,
        **_: Any,
    ) -> tuple[dict[str, Any], int]:
        self.pools.append((n, [offer.counterparty for offer in self.offers]))
        eligible = [offer for offer in self.offers if offer.counterparty not in hard_exclude_nicks]
        soft = self.ignored_makers | set(exclude_nicks or ())
        fresh = [offer for offer in eligible if offer.counterparty not in soft]
        chosen = (fresh if len(fresh) >= n else eligible)[:n]
        return {offer.counterparty: offer for offer in chosen}, 0


def _taker(capabilities: list[bool], *, taker_joins: bool = True) -> tuple[Taker, Chooser]:
    offers = [
        SimpleNamespace(
            counterparty=f"maker-{index}",
            ordertype=OfferType.TR0_ABSOLUTE,
            features={FEATURE_PRIVATE_CHANNEL_RING: capable} if capable else {},
            fidelity_bond_verified=False,
        )
        for index, capable in enumerate(capabilities)
    ]
    taker = Taker.__new__(Taker)
    taker.config = cast(
        Any,
        SimpleNamespace(
            channel_ring=SimpleNamespace(enabled=True, minimum_makers=2, taker_joins=taker_joins),
            preferred_offer_type=OfferType.TR0_ABSOLUTE,
            bitcoin_network=None,
            network=NetworkType.REGTEST,
        ),
    )
    taker._session = cast(
        Any,
        SimpleNamespace(ring_maker_sessions={}, ordinary_maker_sessions={}, cj_amount=500_000),
    )
    chooser = Chooser(offers)
    taker.orderbook_manager = cast(Any, chooser)
    return taker, chooser


@pytest.mark.parametrize(
    "capabilities,expected_ring",
    [
        ([True, True, False, False], 2),
        ([True, True, True, False], 3),
        ([True, True, True, True], 4),
        ([True, True, False, False, True], 3),
    ],
)
def test_surplus_slots_use_both_offer_capabilities(
    capabilities: list[bool], expected_ring: int
) -> None:
    taker, chooser = _taker(capabilities)
    selected, _ = taker._select_channel_ring_makers(chooser.offers, 4, None)
    assert len(selected) == 4
    assert sum(FEATURE_PRIVATE_CHANNEL_RING in offer.features for offer in selected.values()) == (
        expected_ring
    )
    assert chooser.pools[0][0] == 4


def test_ring_makers_can_replace_ordinary_slots_before_roles_are_frozen() -> None:
    taker, chooser = _taker([True, True, False])
    selected, _ = taker._select_channel_ring_makers(
        chooser.offers, 1, None, ring_slots=0, hard_exclude_nicks={"maker-0"}
    )
    assert set(selected) == {"maker-1"}


def test_mixed_selection_rejects_less_than_required_capable_makers() -> None:
    taker, chooser = _taker([True, False, False, False])
    with pytest.raises(ValueError, match="Not enough private_channel_ring makers"):
        taker._select_channel_ring_makers(chooser.offers, 4, None)


def test_maker_only_selection_needs_three_capable_makers_even_with_ordinary_makers() -> None:
    taker, chooser = _taker([True, True, False, False], taker_joins=False)
    with pytest.raises(ValueError, match="Not enough private_channel_ring makers"):
        taker._select_channel_ring_makers(chooser.offers, 4, None)
    taker, chooser = _taker([True, True, True, False], taker_joins=False)
    selected, _ = taker._select_channel_ring_makers(chooser.offers, 4, None)
    assert set(selected) == {"maker-0", "maker-1", "maker-2", "maker-3"}
    assert taker._ring_maker_floor() == 3


def test_explicit_ring_maker_floor_is_not_incremented_in_maker_only_mode() -> None:
    taker, chooser = _taker([True, True, True, True, False], taker_joins=False)
    taker.config.channel_ring.minimum_makers = 4
    selected, _ = taker._select_channel_ring_makers(chooser.offers, 5, None)
    assert set(selected) == {f"maker-{index}" for index in range(5)}
    assert taker._ring_maker_floor() == 4


def test_remaining_slots_never_reuse_a_ring_makers_verified_bond() -> None:
    taker, chooser = _taker([True, True, False, False])
    shared_bond = {
        "utxo_txid": "a" * 64,
        "utxo_vout": 0,
        "utxo_pub": "02" + "b" * 64,
        "locktime": 2_000_000_000,
    }
    chooser.offers[0].fidelity_bond_verified = True
    chooser.offers[0].fidelity_bond_data = shared_bond
    chooser.offers[2].fidelity_bond_verified = True
    chooser.offers[2].fidelity_bond_data = shared_bond
    selected, _ = taker._select_channel_ring_makers(chooser.offers, 3, None)
    assert set(selected) == {"maker-0", "maker-1", "maker-3"}


def test_surplus_ignored_ring_maker_does_not_displace_ordinary_maker() -> None:
    taker, chooser = _taker([True, True, True, False, False])
    chooser.ignored_makers.add("maker-2")
    selected, _ = taker._select_channel_ring_makers(chooser.offers, 4, None)
    assert set(selected) == {"maker-0", "maker-1", "maker-3", "maker-4"}


def test_ring_floor_can_relax_soft_exclusion_when_needed() -> None:
    taker, chooser = _taker([True, True, False, False])
    chooser.ignored_makers.add("maker-1")
    selected, _ = taker._select_channel_ring_makers(chooser.offers, 4, None)
    assert set(selected) == {"maker-0", "maker-1", "maker-2", "maker-3"}
