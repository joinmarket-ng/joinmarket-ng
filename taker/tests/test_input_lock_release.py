"""Persisted input locks must be released when a coinjoin round fails.

Input locks are persisted to the wallet metadata file with a TTL of several
minutes. A failed ``do_coinjoin`` round that returns without releasing them
would keep the inputs "locked by another in-flight CoinJoin" for the whole
TTL, blocking retries even from fresh Taker instances (which discard the
in-memory reservation but not the on-disk lock). This is exactly what a
tumbler retry loop hits when a phase fails mid-negotiation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config, make_utxo
from jmcore.models import Offer, OfferType

from taker.taker import Taker, TakerState


def _make_wallet(utxos: list) -> AsyncMock:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.get_utxos = AsyncMock(return_value=utxos)
    wallet.get_locked_input_outpoints = Mock(return_value=set())
    wallet.select_utxos = Mock(return_value=list(utxos))
    wallet.reserve_coinjoin_inputs = Mock(return_value=True)
    wallet.release_coinjoin_inputs = Mock()
    return wallet


def _backend() -> AsyncMock:
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    return backend


def _offer(nick: str) -> Offer:
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=1_000,
        maxsize=100_000_000,
        txfee=0,
        cjfee=500,
    )


@pytest.mark.asyncio
async def test_failure_after_reservation_releases_persisted_locks() -> None:
    """A round that reserves inputs and then fails must release the locks."""
    utxo = make_utxo(txid_char="a", value=25_000_000, confirmations=10)
    wallet = _make_wallet([utxo])
    config = make_taker_config(
        counterparty_count=2,
        minimum_makers=2,
        taker_utxo_age=5,
        taker_utxo_amtpercent=20,
    )
    taker = Taker(wallet, _backend(), config)

    offers = [_offer("J5maker1"), _offer("J5maker2")]
    taker.directory_client.fetch_orderbook = AsyncMock(return_value=offers)
    taker._update_offers_with_bond_values = AsyncMock()  # type: ignore[method-assign]
    taker.orderbook_manager.update_offers = Mock()  # type: ignore[method-assign]
    taker.orderbook_manager.select_makers = Mock(  # type: ignore[method-assign]
        return_value=({o.counterparty: o for o in offers}, 1_000)
    )
    taker._session._fee_rate = 1.0
    # Fail the round right after the reservation: no PoDLE commitment.
    taker.podle_manager.generate_fresh_commitment = Mock(return_value=None)  # type: ignore[method-assign]

    result = await taker.do_coinjoin(
        amount=5_000_000,
        destination="bcrt1qxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        mixdepth=0,
    )

    assert result is None
    assert taker.state == TakerState.FAILED
    wallet.reserve_coinjoin_inputs.assert_called_once_with({(utxo.txid, utxo.vout)})
    wallet.release_coinjoin_inputs.assert_called_once_with({(utxo.txid, utxo.vout)})
    assert taker._session.reserved_inputs == set()


@pytest.mark.asyncio
async def test_early_failure_releases_leftover_locks() -> None:
    """Even a pre-flight failure must release any leftover reservation."""
    utxo = make_utxo(txid_char="a", value=25_000_000, confirmations=1)  # immature
    wallet = _make_wallet([utxo])
    wallet.select_utxos = Mock(side_effect=ValueError("Insufficient funds"))
    taker = Taker(wallet, _backend(), make_taker_config(taker_utxo_age=5))
    leftover = {("b" * 64, 1)}
    taker._session.reserved_inputs = set(leftover)

    result = await taker.do_coinjoin(amount=5_000_000, destination="INTERNAL", mixdepth=0)

    assert result is None
    wallet.release_coinjoin_inputs.assert_called_once_with(leftover)
    assert taker._session.reserved_inputs == set()
