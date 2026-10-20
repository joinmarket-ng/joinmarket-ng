"""A maker CoinJoin output tops up at most one managed node's Lightning reserve."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest
from jmcore.models import Offer, OfferType
from jmwallet.wallet.models import UTXOInfo

from maker.coinjoin import CoinJoinSession

AMOUNT = 500_000
INPUT_VALUE = 700_000
CJFEE = 500


class Funder:
    """ReserveFunder.claim double: the mixdepths below their minimum."""

    def __init__(self, low: set[int]) -> None:
        self.low = low
        self.claims: list[tuple[int, int]] = []

    def claim(self, mixdepth: int, amount: int) -> str | None:
        self.claims.append((mixdepth, amount))
        return f"node-{mixdepth}" if mixdepth in self.low else None


def _session(
    funder: Funder, *, ordertype: OfferType = OfferType.TR0_ABSOLUTE, txfee: int = 0
) -> CoinJoinSession:
    taproot = ordertype in (OfferType.TR0_ABSOLUTE, OfferType.TR0_RELATIVE)
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    wallet.address_type = "p2tr" if taproot else "p2wpkh"
    wallet.get_locked_input_outpoints.return_value = set()
    wallet.get_maker_rotation_lineage_outpoints = AsyncMock(return_value=set())
    wallet.get_balance_for_offers = AsyncMock(return_value=5_000_000)
    wallet.reserve_coinjoin_inputs.return_value = True
    wallet.get_new_internal_address.side_effect = ["equal-output", "change-output"]
    wallet.select_utxos_with_merge.return_value = [
        UTXOInfo(
            txid="11" * 32,
            vout=0,
            value=INPUT_VALUE,
            address="bcrt1ptest",
            confirmations=6,
            scriptpubkey=("5120" + "22" * 32) if taproot else ("0014" + "22" * 20),
            path="m/86'/1'/4'/0/0" if taproot else "m/84'/1'/4'/0/0",
            mixdepth=4,
        )
    ]
    backend = MagicMock()
    backend.can_resolve_foreign_prevouts.return_value = True
    backend.requires_neutrino_metadata.return_value = False
    session = CoinJoinSession(
        taker_nick="J5Taker",
        wallet=wallet,
        backend=backend,
        offer=Offer(
            counterparty="J5Maker",
            oid=0,
            ordertype=ordertype,
            minsize=10_000,
            maxsize=10_000_000,
            txfee=txfee,
            cjfee=CJFEE if taproot else "0.001",
        ),
        allowed_mixdepths=frozenset({4}),
        reserve_claim=funder.claim,
    )
    session.amount = AMOUNT
    return session


async def test_the_equal_output_funds_the_next_mixdepths_node() -> None:
    funder = Funder(low={0, 4})
    session = _session(funder)
    _, equal, change, mixdepth = await session._select_our_utxos()

    # Mixdepth 4's equal output wraps to mixdepth 0; one node per round.
    assert (mixdepth, equal, change) == (4, "node-0", "equal-output")
    assert funder.claims == [(0, AMOUNT)]
    # No wallet address is derived for the redirected output.
    assert session.wallet.get_new_internal_address.call_args_list == [call(4)]


async def test_otherwise_the_baseline_change_funds_the_source_node() -> None:
    funder = Funder(low={4})
    session = _session(funder, txfee=300)
    _, equal, change, _ = await session._select_our_utxos()

    assert (equal, change) == ("equal-output", "node-4")
    # The verifier's minimum change: inputs - amount - txfee + cjfee.
    assert funder.claims == [(0, AMOUNT), (4, INPUT_VALUE - AMOUNT - 300 + CJFEE)]
    assert session.wallet.get_new_internal_address.call_args_list == [call(0)]


async def test_no_low_node_keeps_both_outputs_in_the_wallet() -> None:
    funder = Funder(low=set())
    session = _session(funder)
    _, equal, change, _ = await session._select_our_utxos()

    assert (equal, change) == ("equal-output", "change-output")
    assert session.wallet.get_new_internal_address.call_args_list == [call(0), call(4)]


@pytest.mark.parametrize("buyout", [False, True])
async def test_a_non_taproot_pit_or_a_buyout_funds_nothing(buyout: bool) -> None:
    funder = Funder(low={0, 4})
    if buyout:
        session = _session(funder)
        # A buyout round keeps its escrow change and funds no reserve.
        session.buyout = MagicMock(change_address="escrow-change")
        assert session._claim_reserve(4, 0, []) == (None, None)
    else:
        session = _session(funder, ordertype=OfferType.SW0_RELATIVE)
        assert session.reserve_claim is None
        _, equal, change, _ = await session._select_our_utxos()
        assert (equal, change) == ("equal-output", "change-output")
    assert funder.claims == []
