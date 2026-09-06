"""Focused channel-input behavior at the CoinJoin session boundary."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.bitcoin import TxInput, TxOutput, serialize_transaction
from jmcore.models import NetworkType, Offer, OfferType
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.signing import deserialize_transaction

from taker.channel_input_parent import OrdinaryWitness
from taker.coinjoin_session import CoinJoinSession
from taker.tx_builder import (
    ChannelEndpointSettlement,
    ChannelTransactionContribution,
    CoinJoinTxBuilder,
)

_TAKER_ADDRESS = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
_TAKER_CHANGE_ADDRESS = "bcrt1qqgpqyqszqgpqyqszqgpqyqszqgpqyqszazmwwa"
_MAKER_ADDRESS = "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry"
_MAKER_CJ_ADDRESS = "bcrt1qqyqszqgpqyqszqgpqyqszqgpqyqszqgpvxat9t"
_P2WPKH = "0014" + "11" * 20
_P2TR = "5120" + "22" * 32


def _contribution() -> ChannelTransactionContribution:
    return ChannelTransactionContribution(
        channel_id="channel-1",
        funding_input=TxInput.from_hex("c" * 64, 3, value=50_000, scriptpubkey=_P2TR),
        first_endpoint=ChannelEndpointSettlement(
            endpoint_id="maker",
            participant_key="11" * 32,
            output=TxOutput(26_000, bytes.fromhex("5120" + "33" * 32)),
        ),
        second_endpoint=ChannelEndpointSettlement(
            endpoint_id="taker",
            participant_key="22" * 32,
            output=TxOutput(24_000, bytes.fromhex("5120" + "44" * 32)),
        ),
    )


def _offer() -> Offer:
    return Offer(
        counterparty="maker",
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=10_000,
        maxsize=1_000_000,
        txfee=200,
        cjfee="5000",
        fidelity_bond_value=0,
    )


def _build_session() -> CoinJoinSession:
    wallet = MagicMock()
    wallet.get_new_internal_address.return_value = _TAKER_CHANGE_ADDRESS
    config = SimpleNamespace(
        network=NetworkType.REGTEST,
        preferred_offer_type=OfferType.SW0_ABSOLUTE,
        round_up_cj_fees=False,
        equalize_cj_fees=False,
    )
    session = CoinJoinSession()
    session.attach(
        SimpleNamespace(
            wallet=wallet,
            config=config,
            backend=SimpleNamespace(get_block_height=AsyncMock(return_value=100)),
        )
    )
    taker_utxo = UTXOInfo(
        txid="a" * 64,
        vout=0,
        value=200_000,
        address=_TAKER_ADDRESS,
        confirmations=1,
        scriptpubkey=_P2WPKH,
        path="m/84'/1'/0'/0/0",
        mixdepth=0,
    )
    session.preselected_utxos = [taker_utxo]
    session.reserved_inputs = {(taker_utxo.txid, taker_utxo.vout)}
    session.cj_amount = 100_000
    session._fee_rate = 1.0
    session.maker_sessions = {
        "maker": SimpleNamespace(
            offer=_offer(),
            utxos=[
                {
                    "txid": "b" * 64,
                    "vout": 1,
                    "value": 300_000,
                    "scriptpubkey": _P2WPKH,
                }
            ],
            cj_address=_MAKER_CJ_ADDRESS,
            change_address=_MAKER_ADDRESS,
        )
    }
    return session


@pytest.mark.asyncio
async def test_channel_build_sizes_p2tr_shape_and_excludes_advertised_cjfee() -> None:
    session = _build_session()
    contribution = _contribution()
    estimate = MagicMock(return_value=1_000)
    session._estimate_tx_fee = estimate

    assert await session._phase_build_tx(
        _TAKER_ADDRESS,
        0,
        channel_contributions=(contribution,),
    )

    args, kwargs = estimate.call_args
    assert args[:2] == (3, 6)
    assert kwargs["input_types"] == ["p2wpkh", "p2wpkh", "p2tr"]
    assert kwargs["output_types"] == ["p2wpkh", "p2wpkh", "p2wpkh", "p2wpkh", "p2tr", "p2tr"]
    assert session.channel_contributions == (contribution,)
    assert session.tx_metadata["channel_input_ids"].count("channel-1") == 1
    assert sum(item is not None for item in session.tx_metadata["channel_endpoint_outputs"]) == 2
    assert session.tx_metadata["actual_fee"] == 1_200

    tx = deserialize_transaction(session.unsigned_tx)
    output_values = {
        owner: tx.outputs[index].value
        for index, owner in enumerate(session.tx_metadata["output_owners"])
    }
    assert output_values[("taker", "change")] == 99_000
    assert output_values[("maker", "change")] == 199_800


@pytest.mark.asyncio
async def test_channel_build_accepts_valid_parent_at_minimum_fee_floor() -> None:
    session = _build_session()
    session._minimum_fee_rate_sat_vb = 1.0

    assert await session._phase_build_tx(
        _TAKER_ADDRESS,
        0,
        channel_contributions=(_contribution(),),
    )
    assert session.tx_metadata["actual_fee"] > 0
    assert session._verify_unsigned_minimum_miner_fee() is None


def _replace_channel_input(session: CoinJoinSession, replacement: TxInput) -> None:
    tx = deserialize_transaction(session.unsigned_tx)
    channel_index = session.tx_metadata["channel_input_ids"].index("channel-1")
    inputs = list(tx.inputs)
    inputs[channel_index] = replacement
    session.unsigned_tx = serialize_transaction(tx.version, inputs, tx.outputs, tx.locktime, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("replacement", "expected_error"),
    [
        (TxInput.from_hex("f" * 64, 9), "Missing prevout"),
        (TxInput.from_hex("a" * 64, 0), "missing or substituted"),
    ],
)
async def test_channel_fee_floor_rejects_missing_or_substituted_prevout(
    replacement: TxInput, expected_error: str
) -> None:
    session = _build_session()
    assert await session._phase_build_tx(
        _TAKER_ADDRESS,
        0,
        channel_contributions=(_contribution(),),
    )
    session._minimum_fee_rate_sat_vb = 1.0
    _replace_channel_input(session, replacement)

    error = session._verify_unsigned_minimum_miner_fee()

    assert error is not None
    assert expected_error in error


@pytest.mark.asyncio
async def test_channel_build_failure_clears_transient_state() -> None:
    session = _build_session()
    invalid = ChannelTransactionContribution(
        channel_id="not-p2tr",
        funding_input=TxInput.from_hex("d" * 64, 0, value=50_000, scriptpubkey=_P2WPKH),
        first_endpoint=_contribution().first_endpoint,
        second_endpoint=_contribution().second_endpoint,
    )
    session.channel_contributions = (_contribution(),)
    session.ordinary_witnesses = (OrdinaryWitness(input_index=0, witness=("aa",)),)

    assert not await session._phase_build_tx(_TAKER_ADDRESS, 0, channel_contributions=(invalid,))
    assert session.channel_contributions == ()
    assert session.ordinary_witnesses == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ("sweep", "ring"))
async def test_channel_build_rejects_sweep_and_ring_sessions(mode: str) -> None:
    session = _build_session()
    if mode == "sweep":
        session.is_sweep = True
    else:
        session.ring_coordinator = object()

    assert not await session._phase_build_tx(
        _TAKER_ADDRESS,
        0,
        channel_contributions=(_contribution(),),
    )


def test_channel_prevout_map_includes_exact_funding_prevout() -> None:
    session = _build_session()
    contribution = _contribution()
    session.selected_utxos = session.preselected_utxos
    session.channel_contributions = (contribution,)

    prevouts = session._build_prevout_map()

    assert prevouts[(contribution.funding_input.txid, contribution.funding_input.vout)] == (
        50_000,
        bytes.fromhex(_P2TR),
    )


@pytest.mark.asyncio
async def test_channel_revalidation_includes_exact_funding_input() -> None:
    session = _build_session()
    contribution = _contribution()
    session.selected_utxos = session.preselected_utxos
    session.channel_contributions = (contribution,)
    session._taker.backend = SimpleNamespace(
        requires_neutrino_metadata=lambda: False,
        get_utxo=AsyncMock(return_value=SimpleNamespace(confirmations=1)),
    )

    assert await session._revalidate_inputs_before_broadcast() == (True, "")

    queried = {(call.args[0], call.args[1]) for call in session.backend.get_utxo.await_args_list}
    assert queried == {
        ("a" * 64, 0),
        ("b" * 64, 1),
        (contribution.funding_input.txid, contribution.funding_input.vout),
    }


def _signing_session(*, include_channel: bool, unknown_owner: bool = False) -> CoinJoinSession:
    taker_input = TxInput.from_hex("a" * 64, 0, value=200_000, scriptpubkey=_P2WPKH)
    maker_input = TxInput.from_hex("b" * 64, 1, value=300_000, scriptpubkey=_P2WPKH)
    contribution = _contribution()
    inputs = [taker_input]
    if include_channel:
        inputs.append(contribution.funding_input)
    inputs.append(maker_input)
    unsigned = serialize_transaction(
        2,
        inputs,
        [TxOutput(100_000, bytes.fromhex(_P2WPKH))],
        0,
        None,
    )
    config = SimpleNamespace(
        network=SimpleNamespace(value="regtest"),
        bitcoin_network=None,
        tx_broadcast=SimpleNamespace(value="self"),
        maker_timeout_sec=1,
        data_dir="/tmp",
        round_up_cj_fees=False,
        equalize_cj_fees=False,
    )
    wallet = MagicMock(wallet_fingerprint="deadbeef")
    directory = MagicMock()
    directory.upgrade_channel_prefer_direct.return_value = "direct"
    directory.send_privmsg = AsyncMock()
    directory.wait_for_responses = AsyncMock()
    session = CoinJoinSession()
    session.attach(SimpleNamespace(wallet=wallet, config=config, directory_client=directory))
    session.unsigned_tx = unsigned
    session.tx_metadata = {
        "input_owners": [
            "taker",
            *(["__channel_input__:channel-1"] if include_channel else []),
            "maker",
        ],
        "channel_input_ids": [None, *(["channel-1"] if include_channel else []), None],
        "source_mixdepth": 0,
        "output_owners": [("taker", "cj")],
    }
    if unknown_owner:
        session.tx_metadata["input_owners"][-1] = "unknown-owner"
    session.selected_utxos = [
        SimpleNamespace(
            txid=taker_input.txid,
            vout=taker_input.vout,
            value=taker_input.value,
            scriptpubkey=_P2WPKH,
            address=_TAKER_ADDRESS,
        )
    ]
    session.reserved_inputs = {(taker_input.txid, taker_input.vout)}
    session.channel_contributions = (contribution,) if include_channel else ()
    session.cj_amount = 100_000
    session.cj_destination = _TAKER_ADDRESS
    maker_crypto = MagicMock()
    maker_crypto.encrypt.return_value = "encrypted"
    maker_crypto.decrypt.return_value = base64.b64encode(b"\x01\x01\x01\x02").decode()
    session.maker_sessions = {
        "maker": SimpleNamespace(
            offer=_offer(),
            utxos=[
                {
                    "txid": maker_input.txid,
                    "vout": maker_input.vout,
                    "value": maker_input.value,
                    "scriptpubkey": _P2WPKH,
                }
            ],
            crypto=maker_crypto,
            comm_channel="direct",
        )
    }
    maker_index = len(inputs) - 1
    session._match_maker_input_signature = MagicMock(return_value=(maker_index, False))
    session._sign_our_inputs = AsyncMock(
        return_value=[
            {
                "txid": taker_input.txid,
                "vout": taker_input.vout,
                "witness": ["aa", "bb"],
            }
        ]
    )
    directory.wait_for_responses.return_value = {"maker": {"data": ["encrypted"]}}
    session.renew_input_locks = MagicMock(return_value=True)
    return session


@pytest.mark.asyncio
async def test_channel_signature_collection_retains_only_ordered_ordinary_witnesses() -> None:
    session = _signing_session(include_channel=True)

    with (
        patch("taker.coinjoin_session.create_taker_history_entry"),
        patch("taker.coinjoin_session.append_history_entry"),
        patch.object(
            CoinJoinTxBuilder,
            "add_signatures",
            side_effect=AssertionError("must not assemble"),
        ),
    ):
        assert await session._phase_collect_signatures()

    assert session.final_tx == b""
    assert session.ordinary_witnesses == (
        OrdinaryWitness(input_index=0, witness=("aa", "bb")),
        OrdinaryWitness(input_index=2, witness=("01", "02")),
    )


@pytest.mark.asyncio
async def test_ordinary_signature_collection_still_assembles_final_transaction() -> None:
    session = _signing_session(include_channel=False)

    with (
        patch("taker.coinjoin_session.create_taker_history_entry"),
        patch("taker.coinjoin_session.append_history_entry"),
        patch.object(CoinJoinTxBuilder, "add_signatures", return_value=b"final") as add_signatures,
    ):
        assert await session._phase_collect_signatures()

    add_signatures.assert_called_once()
    assert session.final_tx == b"final"


@pytest.mark.asyncio
async def test_channel_collection_rejects_unknown_ordinary_owner() -> None:
    session = _signing_session(include_channel=True, unknown_owner=True)

    with (
        patch("taker.coinjoin_session.create_taker_history_entry"),
        patch("taker.coinjoin_session.append_history_entry"),
    ):
        assert not await session._phase_collect_signatures()

    session._sign_our_inputs.assert_not_awaited()


@pytest.mark.parametrize(
    ("signatures", "expected"),
    [
        ({"taker": [{"txid": "a" * 64, "vout": 0, "witness": ["aa"]}]}, None),
        (
            {
                "taker": [
                    {"txid": "a" * 64, "vout": 0, "witness": ["aa"]},
                    {"txid": "a" * 64, "vout": 0, "witness": ["bb"]},
                ],
                "maker": [{"txid": "b" * 64, "vout": 1, "witness": ["cc"]}],
            },
            None,
        ),
        (
            {
                "taker": [{"txid": "a" * 64, "vout": 0, "witness": "not-a-list"}],
                "maker": [{"txid": "b" * 64, "vout": 1, "witness": ["cc"]}],
            },
            None,
        ),
        (
            {
                "taker": [{"txid": "c" * 64, "vout": 3, "witness": ["aa"]}],
                "maker": [{"txid": "b" * 64, "vout": 1, "witness": ["cc"]}],
            },
            None,
        ),
        (
            {
                "taker": [{"txid": "f" * 64, "vout": 9, "witness": ["aa"]}],
                "maker": [{"txid": "b" * 64, "vout": 1, "witness": ["cc"]}],
            },
            None,
        ),
    ],
)
def test_collect_ordinary_witnesses_rejects_incomplete_or_substituted_records(
    signatures: dict[str, list[dict[str, object]]],
    expected: None,
) -> None:
    session = _signing_session(include_channel=True)
    tx = deserialize_transaction(session.unsigned_tx)

    assert session._collect_ordinary_witnesses(tx, signatures) is expected
