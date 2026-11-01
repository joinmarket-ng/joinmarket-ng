"""Sweep makers whose inputs the fixed fee budget cannot fund are replaced, not fatal."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _taker_test_helpers import make_crypto_pair, make_taker_config
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    parse_transaction_bytes,
    pubkey_to_p2wpkh_script,
    scriptpubkey_to_address,
)
from jmcore.crypto import ecdsa_sign
from jmcore.fee_policy import estimate_p2wpkh_vsize
from jmcore.models import Offer, OfferType
from jmwallet.backends.base import UTXO
from jmwallet.wallet.models import UTXOInfo

from taker.coinjoin_session import CoinJoinSession
from taker.taker import MakerSession, Taker, _estimate_initial_tx_shape

NICKS = ("J5makerA", "J5makerB", "J5makerC", "J5makerD")
DESTINATION = "bcrt1qqvpsxqcrqvpsxqcrqvpsxqcrqvpsxqcruj60yu"
MAKER_UTXO_VALUE = 300_000


def _address(tag: int) -> str:
    return scriptpubkey_to_address(bytes([0x00, 0x14]) + bytes([tag]) * 20, "regtest")


def _offer(nick: str) -> Offer:
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=10_000,
        maxsize=1_000_000_000,
        txfee=0,
        cjfee=0,
    )


async def _sweep_taker(
    est3: float, est10: float, max_sweep_fee_change: float = 0.8, minimum_makers: int = 3
) -> Taker:
    """Return a taker whose sweep budget was fixed for four makers from real fee resolution."""
    with patch.object(Taker, "__init__", lambda self, *a, **k: None):
        taker = Taker.__new__(Taker)
    taker.config = make_taker_config(
        counterparty_count=4,
        minimum_makers=minimum_makers,
        taker_utxo_age=1,
        max_sweep_fee_change=max_sweep_fee_change,
    )
    taker.wallet = MagicMock()
    taker.backend = AsyncMock()
    taker.backend.requires_neutrino_metadata = MagicMock(return_value=False)
    taker.backend.can_estimate_fee = MagicMock(return_value=True)
    taker.backend.estimate_fee = AsyncMock(side_effect=lambda t: est3 if t == 3 else est10)
    taker.backend.get_mempool_min_fee = AsyncMock(return_value=0.1)
    taker.backend.get_block_height = AsyncMock(return_value=840_000)

    session = CoinJoinSession()
    session.attach(taker)
    taker._session = session
    session.is_sweep = True
    session.preselected_utxos = [
        UTXOInfo(
            txid=f"{i:064x}",
            vout=0,
            value=value,
            address=_address(1),
            confirmations=10,
            scriptpubkey="0014" + "00" * 20,
            path=f"m/84'/1'/0'/0/{i}",
            mixdepth=3,
        )
        for i, value in enumerate((68_874, 78_609), start=1)
    ]
    await session._resolve_fee_rate()
    inputs, outputs = _estimate_initial_tx_shape(
        len(session.preselected_utxos), 4, is_sweep=True, max_maker_utxos=15
    )
    session._sweep_tx_fee_budget = session._estimate_tx_fee(inputs, outputs, use_base_rate=True)
    session.cj_amount = (
        sum(u.value for u in session.preselected_utxos) - session._sweep_tx_fee_budget
    )
    return taker


def _add_ioauth_makers(taker: Taker, input_counts: tuple[int, ...]) -> None:
    """Fill makers that answer !auth with valid, signed !ioauth messages."""
    session = taker._session
    responses: dict[str, dict[str, str]] = {}
    scriptpubkeys: dict[str, str] = {}
    for index, count in enumerate(input_counts):
        nick = NICKS[index]
        taker_crypto, maker_crypto = make_crypto_pair()
        auth_key = CKey(bytes([index + 1]) * 32)
        auth_pub = bytes(auth_key.pub)
        txids = [f"{index + 0xA0:02x}{n:062x}" for n in range(count)]
        for txid in txids:
            scriptpubkeys[txid] = pubkey_to_p2wpkh_script(auth_pub).hex()
        btc_sig = ecdsa_sign(maker_crypto.get_pubkey_hex(), auth_key.secret_bytes)
        utxo_list = ",".join(f"{txid}:0" for txid in txids)
        ioauth = (
            f"{utxo_list} {auth_pub.hex()} {_address(10 + index)} {_address(50 + index)} {btc_sig}"
        )
        responses[nick] = {"data": maker_crypto.encrypt(ioauth)}
        maker = MakerSession(nick=nick, offer=_offer(nick), pubkey=maker_crypto.get_pubkey_hex())
        maker.responded_fill = True
        maker.crypto = taker_crypto
        session.maker_sessions[nick] = maker

    async def get_utxo(txid: str, vout: int) -> UTXO:
        return UTXO(
            txid=txid,
            vout=vout,
            value=MAKER_UTXO_VALUE,
            address="bcrt1qtest",
            confirmations=3,
            scriptpubkey=scriptpubkeys[txid],
        )

    cast(AsyncMock, taker.backend).get_utxo = AsyncMock(side_effect=get_utxo)
    directory_client = MagicMock()
    directory_client.send_privmsg = AsyncMock()
    directory_client.upgrade_channel_prefer_direct = MagicMock(side_effect=lambda n, ch: ch)
    directory_client.wait_for_responses = AsyncMock(return_value=responses)
    taker.directory_client = directory_client
    commitment = MagicMock()
    commitment.has_neutrino_metadata.return_value = False
    commitment.to_revelation.return_value = {
        "utxo": f"{1:064x}:0",
        "P": "00",
        "P2": "00",
        "sig": "00",
        "e": "00",
    }
    session.podle_commitment = commitment


def _mining_fee_rate(session: CoinJoinSession) -> float:
    tx = parse_transaction_bytes(session.unsigned_tx)
    total_input = sum(u.value for u in session.selected_utxos) + sum(
        u["value"] for maker in session.maker_sessions.values() for u in maker.utxos
    )
    fee = total_input - sum(o.value for o in tx.outputs)
    return float(fee / estimate_p2wpkh_vsize(len(tx.inputs), len(tx.outputs)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("est3", "est10", "tolerance", "input_counts", "dropped"),
    [
        # Estimates below the static floor pin the budget rate to the floor itself.
        (0.6, 0.6, 0.8, (1, 1, 1, 10), "J5makerD"),
        # The heaviest maker is dropped even when it does not answer last.
        (0.6, 0.6, 0.8, (10, 1, 1, 2), "J5makerA"),
        # A target well above the floor is still bounded by the sweep tolerance.
        (10.0, 1.0, 0.3, (3, 3, 3, 10), "J5makerD"),
    ],
)
async def test_auth_replaces_heaviest_over_budget_maker_instead_of_failing_build(
    est3: float,
    est10: float,
    tolerance: float,
    input_counts: tuple[int, ...],
    dropped: str,
) -> None:
    taker = await _sweep_taker(est3, est10, max_sweep_fee_change=tolerance)
    session = taker._session
    _add_ioauth_makers(taker, input_counts)

    result = await session._phase_auth()

    assert result.success is True
    assert result.over_budget_makers == [dropped]
    assert result.failed_makers == []
    assert dropped not in session.maker_sessions
    assert all(maker.responded_auth for maker in session.maker_sessions.values())
    assert await session._phase_build_tx(destination=DESTINATION, mixdepth=3) is True
    assert session._minimum_fee_rate_sat_vb is not None
    assert _mining_fee_rate(session) >= session._minimum_fee_rate_sat_vb


@pytest.mark.asyncio
@pytest.mark.parametrize("input_counts", [(1, 1, 1, 1), (3, 3, 3, 3)])
async def test_auth_keeps_sweep_makers_the_budget_funds(input_counts: tuple[int, ...]) -> None:
    taker = await _sweep_taker(0.6, 0.6)
    session = taker._session
    _add_ioauth_makers(taker, input_counts)

    result = await session._phase_auth()

    assert result.success is True
    assert result.over_budget_makers == []
    assert set(session.maker_sessions) == set(NICKS)
    assert await session._phase_build_tx(destination=DESTINATION, mixdepth=3) is True


@pytest.mark.asyncio
async def test_over_budget_drop_below_minimum_makers_is_replaceable() -> None:
    taker = await _sweep_taker(0.6, 0.6, minimum_makers=4)
    session = taker._session
    _add_ioauth_makers(taker, (1, 1, 1, 10))

    result = await session._phase_auth()

    assert result.success is False
    assert result.over_budget_makers == ["J5makerD"]
    assert result.needs_replacement is True


@pytest.mark.asyncio
async def test_build_reports_why_a_sweep_below_the_fee_floor_failed() -> None:
    taker = await _sweep_taker(0.6, 0.6)
    session = taker._session
    for index, count in enumerate((1, 1, 1, 10)):
        nick = NICKS[index]
        maker = MakerSession(nick=nick, offer=_offer(nick))
        maker.responded_auth = True
        maker.utxos = [
            {"txid": f"{index + 0xA0:02x}{n:062x}", "vout": 0, "value": MAKER_UTXO_VALUE}
            for n in range(count)
        ]
        maker.cj_address = _address(10 + index)
        maker.change_address = _address(50 + index)
        session.maker_sessions[nick] = maker

    assert await session._phase_build_tx(destination=DESTINATION, mixdepth=3) is False
    assert session.last_failure_reason is not None
    assert "below the required minimum" in session.last_failure_reason
