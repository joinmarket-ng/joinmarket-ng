"""Tests for typed Taproot maker signature handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import TxInput, TxOutput, serialize_transaction, taproot_tweak_privkey
from jmcore.models import Offer, OfferType
from jmcore.taproot import TaprootTx, TxIn, TxOut, sign_taproot_keypath, taproot_output_script
from jmwallet.wallet.models import UTXOInfo

from taker.coinjoin_session import CoinJoinSession
from taker.models import MakerSession
from taker.signatures import (
    P2TRSignatureFormatError,
    parse_p2tr_maker_signature,
    verify_p2tr_keypath_signature,
)


def _p2tr_transaction() -> tuple[bytes, list[int], list[bytes], bytes]:
    maker_key = PrivateKey(b"\x01" * 32)
    maker_xonly = maker_key.public_key.format(compressed=True)[1:]
    maker_script = taproot_output_script(maker_xonly)
    taker_script = bytes.fromhex("0014" + "02" * 20)
    inputs = [
        TxInput.from_hex("11" * 32, 1, value=11_000, scriptpubkey=taker_script.hex()),
        TxInput.from_hex("22" * 32, 2, value=22_000, scriptpubkey=maker_script.hex()),
    ]
    outputs = [TxOutput(value=32_000, script=bytes.fromhex("0014" + "03" * 20))]
    unsigned_tx = serialize_transaction(2, inputs, outputs, 0, witnesses=None)
    values = [11_000, 22_000]
    scripts = [taker_script, maker_script]
    taproot_tx = TaprootTx(
        version=2,
        inputs=[
            TxIn(txid=input_.txid, vout=input_.vout, sequence=input_.sequence) for input_ in inputs
        ],
        outputs=[TxOut(value=output.value, scriptpubkey=output.script) for output in outputs],
        locktime=0,
    )
    signature = sign_taproot_keypath(
        taproot_tx,
        1,
        values,
        scripts,
        taproot_tweak_privkey(maker_key.secret),
    )
    return unsigned_tx, values, scripts, signature


def test_parse_p2tr_maker_signature() -> None:
    signature = "ab" * 64
    parsed = parse_p2tr_maker_signature(f"p2tr:{'01' * 32}:4294967295:{signature}")

    assert parsed.txid == "01" * 32
    assert parsed.vout == 0xFFFFFFFF
    assert parsed.signature.hex() == signature


@pytest.mark.parametrize(
    "payload",
    [
        "p2tr:" + "AA" * 32 + ":0:" + "ab" * 64,
        "p2tr:" + "01" * 32 + ":00:" + "ab" * 64,
        "p2tr:" + "01" * 32 + ":-1:" + "ab" * 64,
        "p2tr:" + "01" * 32 + ":4294967296:" + "ab" * 64,
        "p2tr:" + "01" * 32 + ":0:" + "ab" * 63,
        "p2tr:" + "01" * 31 + ":0:" + "ab" * 64,
        "p2tr:" + "01" * 32 + ":0:" + "ab" * 64 + ":extra",
    ],
)
def test_parse_p2tr_maker_signature_rejects_malformed_payloads(payload: str) -> None:
    with pytest.raises(P2TRSignatureFormatError, match="invalid P2TR maker signature payload"):
        parse_p2tr_maker_signature(payload)


def test_verify_p2tr_keypath_signature_commits_to_all_prevouts() -> None:
    unsigned_tx, values, scripts, signature = _p2tr_transaction()

    assert verify_p2tr_keypath_signature(unsigned_tx, 1, values, scripts, signature)
    assert not verify_p2tr_keypath_signature(unsigned_tx, 0, values, scripts, signature)
    assert not verify_p2tr_keypath_signature(unsigned_tx, 1, values[:1], scripts, signature)
    assert not verify_p2tr_keypath_signature(unsigned_tx, 1, values, scripts[:1], signature)
    assert not verify_p2tr_keypath_signature(unsigned_tx, 1, [11_001, 22_000], scripts, signature)
    assert not verify_p2tr_keypath_signature(unsigned_tx, 1, [11_000, 22_001], scripts, signature)
    assert not verify_p2tr_keypath_signature(
        unsigned_tx, 1, values, [bytes.fromhex("0014" + "04" * 20), scripts[1]], signature
    )
    assert not verify_p2tr_keypath_signature(
        unsigned_tx, 1, values, [scripts[0], bytes.fromhex("0014" + "05" * 20)], signature
    )
    assert not verify_p2tr_keypath_signature(unsigned_tx, 1, values, scripts, b"\x00" * 64)

    wrong_txid = bytearray(unsigned_tx)
    wrong_txid[5] ^= 1
    assert not verify_p2tr_keypath_signature(bytes(wrong_txid), 1, values, scripts, signature)

    wrong_vout = bytearray(unsigned_tx)
    second_input_vout_offset = 5 + 41 + 32
    wrong_vout[second_input_vout_offset] ^= 1
    assert not verify_p2tr_keypath_signature(bytes(wrong_vout), 1, values, scripts, signature)


@pytest.mark.asyncio
async def test_collect_typed_p2tr_signature_uses_one_item_witness(tmp_path: Path) -> None:
    unsigned_tx, values, scripts, signature = _p2tr_transaction()
    offer = Offer(
        counterparty="maker",
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=1,
        maxsize=100_000,
        txfee=0,
        cjfee="0",
    )
    maker_session = MakerSession(
        nick="maker",
        offer=offer,
        utxos=[
            {
                "txid": "22" * 32,
                "vout": 2,
                "value": values[1],
                "scriptpubkey": scripts[1].hex(),
            }
        ],
    )
    crypto = MagicMock()
    crypto.encrypt.return_value = "encrypted"
    crypto.decrypt.return_value = f"p2tr:{'22' * 32}:2:{signature.hex()}"
    object.__setattr__(maker_session, "crypto", crypto)

    session = CoinJoinSession()
    taker = MagicMock()
    taker.config.network.value = "regtest"
    taker.config.tx_broadcast.value = "self"
    taker.config.maker_timeout_sec = 1
    taker.config.data_dir = tmp_path
    taker.wallet.wallet_fingerprint = "deadbeef"
    taker.directory_client.upgrade_channel_prefer_direct.return_value = ""
    taker.directory_client.send_privmsg = AsyncMock()
    taker.directory_client.wait_for_responses = AsyncMock(
        return_value={"maker": {"data": ["reply"]}}
    )
    session.attach(taker)
    session.unsigned_tx = unsigned_tx
    session.tx_metadata = {"input_owners": ["taker", "maker"], "source_mixdepth": 0}
    session.cj_amount = 1
    session.cj_destination = "destination"
    session.selected_utxos = [
        UTXOInfo(
            txid="11" * 32,
            vout=1,
            value=values[0],
            address="source",
            confirmations=1,
            scriptpubkey=scripts[0].hex(),
            path="m/84'/1'/0'/0/0",
            mixdepth=0,
        )
    ]
    session.maker_sessions = {"maker": maker_session}
    session._sign_our_inputs = AsyncMock(
        return_value=[
            {
                "txid": "11" * 32,
                "vout": 1,
                "witness": ["30" * 71, "02" + "06" * 32],
            }
        ]
    )

    with patch("taker.coinjoin_session.append_history_entry"):
        assert await session._phase_collect_signatures()

    from jmcore.bitcoin import parse_transaction_bytes

    parsed = parse_transaction_bytes(session.final_tx)
    assert parsed.witnesses[1] == [signature]
