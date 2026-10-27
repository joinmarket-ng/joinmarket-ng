"""A taker CoinJoin tops up at most one managed node's Lightning reserve."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import parse_transaction_bytes, scriptpubkey_to_address
from jmcore.models import NetworkType, OfferType
from jmwallet.wallet.models import UTXOInfo

from taker.coinjoin_session import CoinJoinSession
from taker.config import TakerConfig
from taker.taker import Taker

SCRIPT = b"\x51\x20" + bytes(CKey(b"\x11" * 32).xonly_pub)
NODE_SCRIPT = b"\x51\x20" + bytes(CKey(b"\x22" * 32).xonly_pub)
WALLET_SCRIPT = b"\x51\x20" + bytes(CKey(b"\x33" * 32).xonly_pub)
NODE = scriptpubkey_to_address(NODE_SCRIPT, "regtest")
MNEMONIC = "abandon " * 11 + "about"


class Funder:
    """ReserveFunder double: records reads and which outputs claimed a node."""

    def __init__(self, low: set[int]) -> None:
        self.low = low
        self.refreshed: list[set[int]] = []
        self.claims: list[tuple[int, int]] = []

    async def refresh(self, mixdepths: set[int]) -> None:
        self.refreshed.append(set(mixdepths))

    def claim(self, mixdepth: int, amount: int) -> str | None:
        self.claims.append((mixdepth, amount))
        return f"node-{mixdepth}" if mixdepth in self.low else None


def _taker(funder: Funder | None, *, sweep: bool = False, buyout: bool = False) -> Any:
    session = SimpleNamespace(
        cj_amount=60_000,
        is_sweep=sweep,
        buyout=object() if buyout else None,
        reserve_change_claim=None,
    )
    return SimpleNamespace(
        _reserve_funder=funder, _session=session, wallet=SimpleNamespace(mixdepth_count=5)
    )


async def _plan(taker: Any, mixdepth: int = 0, *, internal: bool = True) -> str:
    return await Taker._plan_reserve_funding(
        taker, "wallet-address", mixdepth, destination_is_internal=internal
    )


async def test_an_internal_equal_output_funds_the_next_mixdepths_node() -> None:
    funder = Funder(low={1, 0})
    taker = _taker(funder)
    assert await _plan(taker) == "node-1"
    assert funder.refreshed == [{0, 1}]
    assert funder.claims == [(1, 60_000)]
    # One node per transaction: the change then stays in the wallet.
    assert taker._session.reserve_change_claim is None


@pytest.mark.parametrize("internal", [True, False])
async def test_otherwise_ordinary_change_may_fund_the_source_node(internal: bool) -> None:
    funder = Funder(low={4})
    taker = _taker(funder)
    # Mixdepth 4's internal destination wraps around to mixdepth 0.
    assert await _plan(taker, 4, internal=internal) == "wallet-address"
    assert funder.refreshed == [{4, 0} if internal else {4}]
    assert funder.claims == ([(0, 60_000)] if internal else [])
    assert taker._session.reserve_change_claim(12_345) == "node-4"
    assert funder.claims[-1] == (4, 12_345)


async def test_a_sweep_keeps_no_change_and_a_buyout_funds_nothing() -> None:
    funder = Funder(low={0, 1})
    sweep = _taker(funder, sweep=True)
    assert await _plan(sweep, internal=False) == "wallet-address"
    assert sweep._session.reserve_change_claim is None

    funder = Funder(low={0, 1})
    buyout = _taker(funder, buyout=True)
    assert await _plan(buyout) == "wallet-address"
    assert (funder.refreshed, funder.claims) == ([], [])
    assert buyout._session.reserve_change_claim is None


async def test_without_managed_nodes_nothing_changes() -> None:
    taker = _taker(None)
    assert await _plan(taker) == "wallet-address"
    assert taker._session.reserve_change_claim is None


@pytest.fixture
def session() -> CoinJoinSession:
    result = CoinJoinSession()
    owner = MagicMock()
    owner.config = TakerConfig(
        mnemonic=MNEMONIC,
        network=NetworkType.REGTEST,
        address_type="p2tr",
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
    )
    owner.backend.get_block_height = AsyncMock(return_value=200)
    owner.wallet.get_new_internal_address.return_value = scriptpubkey_to_address(
        WALLET_SCRIPT, "regtest"
    )
    result.attach(owner)
    result.cj_amount = 500_000
    result._fee_rate = result._randomized_fee_rate = result._minimum_fee_rate_sat_vb = 1.0
    utxo = UTXOInfo(
        txid="55" * 32,
        vout=0,
        value=700_000,
        confirmations=10,
        address=scriptpubkey_to_address(SCRIPT, "regtest"),
        scriptpubkey=SCRIPT.hex(),
        path="m/86'/1'/0'/0/0",
        mixdepth=0,
    )
    result.preselected_utxos = [utxo]
    result.reserved_inputs = {(utxo.txid, utxo.vout)}
    return result


async def test_claimed_change_pays_the_node_instead_of_the_wallet(
    session: CoinJoinSession,
) -> None:
    claimed: list[int] = []

    def claim(change: int) -> str:
        claimed.append(change)
        return NODE

    session.reserve_change_claim = claim
    assert await session._phase_build_tx(scriptpubkey_to_address(SCRIPT, "regtest"), 0)

    session.wallet.get_new_internal_address.assert_not_called()
    assert session.taker_change_address == NODE
    [change] = claimed
    outputs = parse_transaction_bytes(session.unsigned_tx).outputs
    assert [output.value for output in outputs if output.script == NODE_SCRIPT] == [change]


async def test_unclaimed_change_stays_in_the_source_mixdepth(session: CoinJoinSession) -> None:
    session.reserve_change_claim = lambda change: None
    assert await session._phase_build_tx(scriptpubkey_to_address(SCRIPT, "regtest"), 0)

    session.wallet.get_new_internal_address.assert_called_once_with(0)
    outputs = parse_transaction_bytes(session.unsigned_tx).outputs
    assert sum(output.script == WALLET_SCRIPT for output in outputs) == 1
    assert all(output.script != NODE_SCRIPT for output in outputs)
