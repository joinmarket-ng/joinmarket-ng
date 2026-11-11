"""A maker must not disclose inputs based only on Neutrino discovery state."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from jmcore.bitcoin import address_to_scriptpubkey
from jmcore.models import Offer, OfferType
from jmwallet.backends.base import BlockchainBackend, UTXOVerificationResult
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService

from maker.coinjoin import OWN_INPUT_VERIFICATION_TIMEOUT_SEC, CoinJoinSession


@pytest.fixture
def session(tmp_path: Path) -> CoinJoinSession:
    backend = Mock(spec=BlockchainBackend)
    backend.requires_neutrino_metadata.return_value = True
    wallet = WalletService(
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about",
        backend,
        network="regtest",
        data_dir=tmp_path,
    )
    coins = []
    for index, value in enumerate((10_000_000, 25_000_000)):
        address = wallet.get_address(1, 0, index)
        coins.append(
            UTXOInfo(
                txid="ab"[index] * 64,
                vout=0,
                value=value,
                address=address,
                scriptpubkey=address_to_scriptpubkey(address).hex(),
                height=100,
                confirmations=100,
                path=f"m/84'/1'/1'/0/{index}",
                mixdepth=1,
            )
        )
    wallet.utxo_cache = {md: coins if md == 1 else [] for md in range(wallet.mixdepth_count)}

    async def verify(txid: str, **kwargs: object) -> UTXOVerificationResult:
        if txid == coins[0].txid:
            return UTXOVerificationResult(valid=False, error="UTXO not found")
        return UTXOVerificationResult(
            valid=True, value=coins[1].value, confirmations=10, block_height=190
        )

    backend.verify_wallet_utxo_with_metadata = AsyncMock(side_effect=verify)
    result = CoinJoinSession(
        taker_nick="J5ReorgTaker",
        offer=Offer(
            counterparty="J5ReorgMaker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=1_000,
            maxsize=100_000_000,
            txfee=0,
            cjfee=500,
        ),
        wallet=wallet,
        backend=backend,
        merge_algorithm="greedy",
        restrict_md0=False,
    )
    result.amount = 5_000_000
    return result


@pytest.mark.asyncio
async def test_maker_reselects_live_inputs_before_disclosure(session: CoinJoinSession) -> None:
    ghost, live = session.wallet.utxo_cache[1]
    selected, _, _, mixdepth = await session._select_our_utxos()

    assert mixdepth == 1
    assert set(selected) == {(live.txid, live.vout)}
    assert session.wallet.get_locked_input_outpoints() == {(live.txid, live.vout)}
    assert ghost in session.wallet.utxo_cache[1]
    session.wallet.release_coinjoin_inputs(set(selected), owner=session.input_lock_owner)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["unavailable", "timeout", "cancel", "expired", "late-result", "late-rejected"]
)
async def test_unfinished_verification_releases_inputs_without_exclusion(
    session: CoinJoinSession, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    entered = asyncio.Event()
    active = True

    async def verify(**kwargs: object) -> UTXOVerificationResult:
        nonlocal active
        entered.set()
        if failure in ("timeout", "cancel"):
            await asyncio.Event().wait()
        if failure in ("late-result", "late-rejected"):
            time.sleep(0.02)
            return UTXOVerificationResult(
                valid=failure == "late-result", value=10_000_000, confirmations=10
            )
        if failure == "expired":
            active = False
            return UTXOVerificationResult(valid=True, value=10_000_000, confirmations=10)
        return UTXOVerificationResult(
            valid=False, conclusive=False, error="temporarily unavailable"
        )

    session.backend.verify_wallet_utxo_with_metadata.side_effect = verify
    if failure in ("timeout", "late-result", "late-rejected"):
        monkeypatch.setattr("maker.coinjoin.OWN_INPUT_VERIFICATION_TIMEOUT_SEC", 0.01)
    selection = asyncio.create_task(session._select_our_utxos(active_check=lambda: active))
    await entered.wait()
    if failure == "cancel":
        selection.cancel()
        with pytest.raises(asyncio.CancelledError):
            await selection
    else:
        assert await selection == ({}, "", "", -1)

    assert session.wallet.get_locked_input_outpoints() == set()
    assert len(session.wallet.utxo_cache[1]) == 2
    assert session.our_utxos == {}
    if failure == "late-rejected":
        session.backend.verify_wallet_utxo_with_metadata.assert_awaited_once()

    # No failure permanently quarantines a coin. A later attempt can use it.
    monkeypatch.setattr(
        "maker.coinjoin.OWN_INPUT_VERIFICATION_TIMEOUT_SEC", OWN_INPUT_VERIFICATION_TIMEOUT_SEC
    )
    session.backend.verify_wallet_utxo_with_metadata.side_effect = None
    session.backend.verify_wallet_utxo_with_metadata.return_value = UTXOVerificationResult(
        valid=True, value=10_000_000, confirmations=10
    )
    selected, _, _, _ = await session._select_our_utxos()
    ghost = session.wallet.utxo_cache[1][0]
    assert set(selected) == {(ghost.txid, ghost.vout)}
    session.wallet.release_coinjoin_inputs(set(selected), owner=session.input_lock_owner)
