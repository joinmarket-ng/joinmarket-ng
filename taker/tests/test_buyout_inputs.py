from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import parse_transaction_bytes, scriptpubkey_to_address
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig
from jmcore.models import NetworkType, OfferType
from jmwallet.wallet.models import UTXOInfo

from taker.coinjoin_session import CoinJoinSession
from taker.config import TakerConfig
from taker.taker import RING_BUYOUT_CONFLICT, Taker, TakerState

SCRIPT = b"\x51\x20" + bytes(CKey(b"\x11" * 32).xonly_pub)
ESCROW = b"\x51\x20" + bytes(CKey(b"\x22" * 32).xonly_pub)
CHANNEL = "33" * 32
MNEMONIC = "abandon " * 11 + "about"


def _ring_config(tmp_path) -> ChannelRingConfig:
    """A minimal valid enabled ring policy (loopback lnd, v3 onion, own store)."""
    return ChannelRingConfig(
        enabled=True,
        nodes={
            "local": ChannelRingNodeConfig(
                lnd_grpc_url="https://127.0.0.1:10009",
                lnd_tls_cert_path=tmp_path / "tls.cert",
                lnd_macaroon_path=tmp_path / "admin.macaroon",
                onion_endpoint="a" * 56 + ".onion:9735",
            )
        },
        mixdepth_nodes={0: "local"},
        node_binding_directory=tmp_path / "node-bindings",
        persistence_directory=tmp_path / "rings",
    )


def _prepared_adapter(total_value: int, minimum_change: int) -> MagicMock:
    """A buyout adapter double with the funding arithmetic the real one owns."""
    adapter = MagicMock()
    adapter.total_value = total_value
    adapter.minimum_change = minimum_change
    adapter.wallet_funding_required.side_effect = lambda required: max(
        1, required + minimum_change - total_value
    )
    return adapter


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
    owner.wallet.sign_input.return_value = SimpleNamespace(
        signature=b"\x44" * 64, pubkey=b"", witness=[b"\x44" * 64]
    )
    result.attach(owner)
    result.cj_amount = 500_000
    result._fee_rate = result._randomized_fee_rate = result._minimum_fee_rate_sat_vb = 1.0
    utxo = UTXOInfo(
        txid="55" * 32,
        vout=0,
        value=200_000,
        confirmations=10,
        address=scriptpubkey_to_address(SCRIPT, "regtest"),
        scriptpubkey=SCRIPT.hex(),
        path="m/86'/1'/0'/0/0",
        mixdepth=0,
    )
    result.preselected_utxos = [utxo]
    result.reserved_inputs = {(utxo.txid, utxo.vout)}
    result.buyout = MagicMock()
    result.buyout.inputs = [
        {"txid": CHANNEL, "vout": 1, "value": 1_000_000, "scriptpubkey": ESCROW.hex()}
    ]
    result.buyout.total_value = 1_000_000
    result.buyout.minimum_change = 410_000
    result.buyout.change_address = scriptpubkey_to_address(ESCROW, "regtest")
    result.buyout.sign = AsyncMock(
        return_value=[
            {
                "txid": CHANNEL,
                "vout": 1,
                "witness": ["66" * 64],
                "signature": "66" * 64,
                "pubkey": "",
            }
        ]
    )
    return result


async def test_channel_value_funds_coinjoin_without_becoming_wallet_input(
    session: CoinJoinSession,
) -> None:
    assert await session._phase_build_tx(scriptpubkey_to_address(SCRIPT, "regtest"), 0)
    assert session.selected_utxos == session.preselected_utxos
    assert session.reserved_inputs == {("55" * 32, 0)}
    session.wallet.select_utxos.assert_not_called()
    session.wallet.get_new_internal_address.assert_not_called()
    parsed = parse_transaction_bytes(session.unsigned_tx)
    assert {(item.txid, item.vout) for item in parsed.inputs} == {("55" * 32, 0), (CHANNEL, 1)}
    assert sum(output.script == ESCROW for output in parsed.outputs) == 1
    assert session.wallet_change_address == ""
    assert session._verify_unsigned_minimum_miner_fee() is None
    assert session.wallet_funding_required(1_000_000) == 410_000


async def test_wallet_only_signs_ordinary_input_with_all_prevouts(session: CoinJoinSession) -> None:
    assert await session._phase_build_tx(scriptpubkey_to_address(SCRIPT, "regtest"), 0)
    signatures = await session._sign_our_inputs()
    assert len(signatures) == 2
    session.wallet.sign_input.assert_called_once()
    call = session.wallet.sign_input.call_args
    assert call.args[2] == session.preselected_utxos[0]
    assert sorted(call.kwargs["prevout_values"]) == [200_000, 1_000_000]
    assert set(call.kwargs["prevout_scripts"]) == {SCRIPT, ESCROW}
    assert session.signing_boundary_crossed


async def test_external_signing_failure_never_signs_wallet_inputs(session: CoinJoinSession) -> None:
    assert session.buyout is not None
    session.buyout.sign.side_effect = TimeoutError("uncertain peer authorization")
    assert await session._phase_build_tx(scriptpubkey_to_address(SCRIPT, "regtest"), 0)
    assert await session._sign_our_inputs() == []
    session.wallet.sign_input.assert_not_called()
    assert session.signing_boundary_crossed


async def test_channel_outpoint_cannot_also_be_wallet_owned(session: CoinJoinSession) -> None:
    assert session.buyout is not None
    ordinary = session.preselected_utxos[0]
    session.buyout.inputs = [
        {
            "txid": ordinary.txid,
            "vout": ordinary.vout,
            "value": ordinary.value,
            "scriptpubkey": SCRIPT.hex(),
        }
    ]
    assert not await session._phase_build_tx(scriptpubkey_to_address(SCRIPT, "regtest"), 0)
    session.wallet.sign_input.assert_not_called()


def _wallet_utxo(value: int, vout: int = 0) -> UTXOInfo:
    return UTXOInfo(
        txid="77" * 32,
        vout=vout,
        value=value,
        confirmations=10,
        address=scriptpubkey_to_address(SCRIPT, "regtest"),
        scriptpubkey=SCRIPT.hex(),
        path="m/86'/1'/0'/0/0",
        mixdepth=0,
    )


def _preflight_taker(utxos: list[UTXOInfo]) -> Taker:
    """A Taker with only what :meth:`check_utxo_eligibility` reads."""
    taker = Taker.__new__(Taker)
    taker.config = TakerConfig(
        mnemonic=MNEMONIC,
        network=NetworkType.REGTEST,
        address_type="p2tr",
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
    )
    taker._session = CoinJoinSession()
    taker.backend = MagicMock()
    taker.wallet = MagicMock()
    taker.wallet.mixdepth_count = 5
    taker.wallet.get_utxos = AsyncMock(return_value=utxos)
    taker.wallet.get_locked_input_outpoints.return_value = set()
    return taker


async def test_preflight_credits_channel_value_against_wallet_funding() -> None:
    taker = _preflight_taker([_wallet_utxo(250_000)])
    adapter = _prepared_adapter(total_value=1_000_000, minimum_change=410_000)

    assert await taker.check_utxo_eligibility(1_000_000, 0, buyout=adapter) is None

    # Only the escrow reserve minus the channel value has to come from the wallet.
    assert taker.wallet.select_utxos.call_args.args[1] == 410_000


async def test_preflight_keeps_podle_threshold_on_the_coinjoin_amount() -> None:
    taker = _preflight_taker([_wallet_utxo(150_000)])
    adapter = _prepared_adapter(total_value=1_000_000, minimum_change=100_000)

    reason = await taker.check_utxo_eligibility(1_000_000, 0, buyout=adapter)

    # The wallet covers the 100,000 sats of funding left after the channels,
    # but a commitment still needs 20% of the full CoinJoin amount.
    assert reason is not None and "PoDLE commitment" in reason
    taker.wallet.select_utxos.assert_not_called()


@pytest.mark.parametrize(
    "value,expected",
    [(250_000, "need at least 410,000 sats"), (450_000, None)],
)
async def test_preflight_explicit_inputs_use_the_same_funding_requirement(
    value: int, expected: str | None
) -> None:
    taker = _preflight_taker([])
    adapter = _prepared_adapter(total_value=1_000_000, minimum_change=410_000)
    selected = [_wallet_utxo(value)]
    with patch("taker.taker.resolve_input_utxos", AsyncMock(return_value=(selected, None))):
        reason = await taker.check_utxo_eligibility(
            1_000_000,
            0,
            input_utxos=[f"{selected[0].txid}:0"],
            buyout=adapter,
        )

    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


@pytest.mark.parametrize(
    ("bound_mixdepth", "fingerprint", "failure"),
    [
        (0, "f00d", None),
        (0, None, "wallet identity"),
        (0, "", "wallet identity"),
        (0, "different", "wallet identity"),
        (1, None, "source mixdepth"),
        (True, None, "source mixdepth"),
        ("0", None, "source mixdepth"),
        (None, None, "source mixdepth"),
    ],
)
async def test_buyout_binding_precedes_reservation_and_maker_work(
    bound_mixdepth: object,
    fingerprint: str | None,
    failure: str | None,
) -> None:
    taker = Taker.__new__(Taker)
    taker.config = TakerConfig(
        mnemonic=MNEMONIC,
        network=NetworkType.REGTEST,
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        address_type="p2tr",
    )
    taker.wallet = MagicMock()
    taker.wallet.mixdepth_count = 5
    taker.wallet.wallet_fingerprint = "f00d"
    taker.wallet.get_new_internal_address.return_value = scriptpubkey_to_address(SCRIPT, "regtest")
    taker.orderbook_manager = MagicMock(own_wallet_nicks=set())
    taker._maker_nick_component = "maker_taproot"
    taker.state = TakerState.IDLE
    taker._session = CoinJoinSession()
    taker._session.attach(taker)
    buyout = MagicMock()
    buyout.buyer.runtime_binding = {"mixdepth": bound_mixdepth}
    if fingerprint is not None:
        buyout.buyer.runtime_binding["wallet_fingerprint"] = fingerprint
    taker._session.buyout = buyout
    taker._begin_input_lock_round = MagicMock()
    taker.release_input_locks = MagicMock()
    taker._clear_coinjoin_log_context = MagicMock()
    taker._prepare_requested_input_selection = AsyncMock(return_value=([], [], 0))
    taker._session._resolve_fee_rate = AsyncMock(side_effect=ValueError("stop after binding"))

    with patch("taker.taker.read_nick_state", return_value=None):
        assert (
            await taker._do_coinjoin(500_000, "INTERNAL", mixdepth=0, counterparty_count=2) is None
        )

    if failure is None:
        buyout.begin_round.assert_called_once_with()
        assert taker.last_failure_reason == "stop after binding"
    else:
        buyout.begin_round.assert_not_called()
        assert taker.last_failure_reason is not None
        assert failure in taker.last_failure_reason
        taker.wallet.get_new_internal_address.assert_not_called()


@pytest.mark.parametrize(
    "amount,offer_type,address_type",
    [
        (0, OfferType.TR0_ABSOLUTE, "p2tr"),
        (500_000, OfferType.SW0_ABSOLUTE, "p2wpkh"),
    ],
)
async def test_buyout_rejects_sweep_and_non_taproot_pit_before_reservation(
    amount: int,
    offer_type: OfferType,
    address_type: str,
) -> None:
    taker = Taker.__new__(Taker)
    taker._round_lock = asyncio.Lock()
    taker.config = TakerConfig(
        mnemonic=MNEMONIC,
        network=NetworkType.REGTEST,
        preferred_offer_type=offer_type,
        address_type=address_type,
    )
    buyout = MagicMock()
    with pytest.raises(ValueError, match="non-sweep Taproot"):
        await taker.do_coinjoin(amount, "INTERNAL", buyout=buyout)
    buyout.begin_round.assert_not_called()


@pytest.mark.asyncio
async def test_buyout_and_channel_ring_cannot_share_one_round(tmp_path) -> None:
    """A ring spends every residual into channels, so no escrow change can exist."""
    taker = Taker.__new__(Taker)
    taker._round_lock = asyncio.Lock()
    taker.config = TakerConfig(
        mnemonic=MNEMONIC,
        network=NetworkType.REGTEST,
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        address_type="p2tr",
        channel_ring=_ring_config(tmp_path),
    )
    buyout = MagicMock()

    with pytest.raises(ValueError, match="cannot fund a private channel buyout"):
        await taker.do_coinjoin(500_000, "INTERNAL", buyout=buyout)

    buyout.begin_round.assert_not_called()


def test_ring_request_error_rejects_an_attached_buyout(tmp_path) -> None:
    """The in-round guard also refuses a buyout attached to the session."""
    taker = Taker.__new__(Taker)
    taker.config = TakerConfig(
        mnemonic=MNEMONIC,
        network=NetworkType.REGTEST,
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        address_type="p2tr",
        channel_ring=_ring_config(tmp_path),
    )
    taker._session = CoinJoinSession()
    taker._session.attach(taker)

    assert taker._ring_request_error(500_000, 5, None) is None
    assert taker._ring_request_error(500_000, 5, MagicMock()) == RING_BUYOUT_CONFLICT
    with pytest.raises(ValueError, match="cannot fund a private channel buyout"):
        taker._enforce_ring_request(500_000, 5, MagicMock())
