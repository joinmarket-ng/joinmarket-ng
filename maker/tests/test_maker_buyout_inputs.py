"""Maker-side funding of a CoinJoin with a prepared channel buyout.

Every test drives the real :class:`CoinJoinSession` message handlers. The
buyout runtime (a separate process in production) is a double, but the wallet
arithmetic, disclosure, verification and signing paths under test are the real
ones.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    scriptpubkey_to_address,
    serialize_transaction,
)
from jmcore.encryption import CryptoSession
from jmcore.models import NetworkType, Offer, OfferType
from jmwallet.backends.base import UTXO
from jmwallet.wallet.models import UTXOInfo

from maker.coinjoin import CoinJoinSession, CoinJoinState
from maker.offer_math import required_maker_input
from maker.tx_verification import verify_unsigned_transaction

WALLET_SCRIPT = b"\x51\x20" + bytes(CKey(b"\x11" * 32).xonly_pub)
CJ_SCRIPT = b"\x51\x20" + bytes(CKey(b"\x22" * 32).xonly_pub)
ESCROW_SCRIPT = b"\x51\x20" + bytes(CKey(b"\x33" * 32).xonly_pub)
TAKER_SCRIPT = b"\x51\x20" + bytes(CKey(b"\x44" * 32).xonly_pub)

WALLET_ADDRESS = scriptpubkey_to_address(WALLET_SCRIPT, "regtest")
CJ_ADDRESS = scriptpubkey_to_address(CJ_SCRIPT, "regtest")
ESCROW_ADDRESS = scriptpubkey_to_address(ESCROW_SCRIPT, "regtest")
TAKER_ADDRESS = scriptpubkey_to_address(TAKER_SCRIPT, "regtest")

WALLET_TXID = "55" * 32
CHANNEL_TXID = "33" * 32
TAKER_TXID = "77" * 32
PODLE_TXID = "88" * 32

FINGERPRINT = "a1b2c3d4"
BOUND_MIXDEPTH = 2
CHANNEL_VALUE = 1_000_000
CHANNEL_VOUT = 1
MINIMUM_CHANGE = 600_000
WALLET_VALUE = 200_000
CJ_AMOUNT = 500_000
CJFEE = 1000
CHAIN_TIP = 800
CHANNEL_HEIGHT = 700
CHANNEL_SIGNATURE = b"\x66" * 64
WALLET_SIGNATURE = b"\x77" * 64


class FakeBuyout:
    """The prepared buyout session, as the maker sees it across the boundary."""

    def __init__(
        self,
        *,
        mixdepth: int | str | bool | None = BOUND_MIXDEPTH,
        network: str = "regtest",
        fingerprint: str | None = FINGERPRINT,
        channel_value: int = CHANNEL_VALUE,
        binding: dict[str, Any] | None = None,
    ) -> None:
        if binding is None:
            binding = {"network": network, "lnd_identity": "02" * 33}
            if mixdepth is not None:
                binding["mixdepth"] = mixdepth
            if fingerprint is not None:
                binding["wallet_fingerprint"] = fingerprint
        self.buyer = SimpleNamespace(runtime_binding=binding)
        self.terms = SimpleNamespace(proposal=SimpleNamespace(network=network))
        self.change_address = ESCROW_ADDRESS
        self.total_value = channel_value
        self.minimum_change = MINIMUM_CHANGE
        self.inputs = [
            {
                "txid": CHANNEL_TXID,
                "vout": CHANNEL_VOUT,
                "value": channel_value,
                "scriptpubkey": ESCROW_SCRIPT.hex(),
            }
        ]
        self.rounds = 0
        self.validated: list[tuple[bytes, dict[tuple[str, int], tuple[int, bytes]], int]] = []
        self.signed: list[bytes] = []
        self.sign_error: Exception | None = None
        self.validate_error: Exception | None = None

    def wallet_funding_required(self, total_required: int) -> int:
        return max(1, total_required + self.minimum_change - self.total_value)

    def begin_round(self) -> None:
        if self.rounds:
            raise RuntimeError("buyout is already reserved or no longer accepted")
        self.rounds += 1

    def validate(
        self,
        raw: bytes,
        prevout_map: dict[tuple[str, int], tuple[int, bytes]],
        height: int,
    ) -> None:
        self.validated.append((raw, dict(prevout_map), height))
        if self.validate_error is not None:
            raise self.validate_error

    async def sign(
        self, raw: bytes, prevout_map: dict[tuple[str, int], tuple[int, bytes]]
    ) -> list[dict[str, Any]]:
        if self.sign_error is not None:
            raise self.sign_error
        self.signed.append(raw)
        return [
            {
                "txid": CHANNEL_TXID,
                "vout": CHANNEL_VOUT,
                "signature": CHANNEL_SIGNATURE.hex(),
                "pubkey": "",
                "witness": [CHANNEL_SIGNATURE.hex()],
            }
        ]


def _wallet_utxo(value: int = WALLET_VALUE, mixdepth: int = BOUND_MIXDEPTH) -> UTXOInfo:
    return UTXOInfo(
        txid=WALLET_TXID,
        vout=0,
        value=value,
        address=WALLET_ADDRESS,
        confirmations=10,
        scriptpubkey=WALLET_SCRIPT.hex(),
        path="m/86'/1'/0'/0/0",
        mixdepth=mixdepth,
        height=650,
    )


def _chain_utxo(
    txid: str,
    vout: int,
    value: int,
    script: bytes,
    address: str,
    *,
    confirmations: int = 10,
    height: int | None = CHANNEL_HEIGHT,
) -> UTXO:
    return UTXO(
        txid=txid,
        vout=vout,
        value=value,
        address=address,
        confirmations=confirmations,
        scriptpubkey=script.hex(),
        height=height,
    )


def _offer() -> Offer:
    return Offer(
        counterparty="J5maker",
        oid=0,
        ordertype=OfferType.TR0_ABSOLUTE,
        minsize=10_000,
        maxsize=10_000_000,
        txfee=0,
        cjfee=CJFEE,
    )


def _wallet(balances: dict[int, int] | None = None, *, address_type: str = "p2tr") -> MagicMock:
    wallet = MagicMock()
    wallet.network = "regtest"
    wallet.address_type = address_type
    wallet.wallet_fingerprint = FINGERPRINT
    wallet.mixdepth_count = 5
    wallet.get_locked_input_outpoints.return_value = set()
    wallet.get_maker_rotation_lineage_outpoints = AsyncMock(return_value=set())
    available = balances if balances is not None else {BOUND_MIXDEPTH: 5_000_000}

    async def balance(mixdepth: int, **_: Any) -> int:
        return available.get(mixdepth, 0)

    wallet.get_balance_for_offers = AsyncMock(side_effect=balance)
    wallet.select_utxos_with_merge.return_value = [_wallet_utxo()]
    wallet.reserve_coinjoin_inputs.return_value = True
    wallet.renew_coinjoin_inputs.return_value = True
    wallet.get_new_internal_address.return_value = CJ_ADDRESS
    key = MagicMock()
    key.get_public_key_bytes.return_value = b"\x02" + b"\x11" * 32
    key.get_private_key_bytes.return_value = b"\x11" * 32
    wallet.get_key_for_address.return_value = key
    wallet.sign_input.return_value = SimpleNamespace(
        signature=WALLET_SIGNATURE, pubkey=bytes(CKey(b"\x11" * 32).xonly_pub)
    )
    return wallet


def _backend(chain: dict[tuple[str, int], UTXO] | None = None) -> MagicMock:
    backend = MagicMock()
    backend.requires_neutrino_metadata.return_value = False
    backend.can_lookup_arbitrary_utxos.return_value = True
    backend.get_block_height = AsyncMock(return_value=CHAIN_TIP)
    entries = {
        (PODLE_TXID, 0): _chain_utxo(PODLE_TXID, 0, 200_000, TAKER_SCRIPT, TAKER_ADDRESS),
        (CHANNEL_TXID, CHANNEL_VOUT): _chain_utxo(
            CHANNEL_TXID, CHANNEL_VOUT, CHANNEL_VALUE, ESCROW_SCRIPT, ESCROW_ADDRESS
        ),
        (TAKER_TXID, 0): _chain_utxo(TAKER_TXID, 0, 600_000, TAKER_SCRIPT, TAKER_ADDRESS),
    }
    if chain is not None:
        entries.update(chain)

    async def get_utxo(txid: str, vout: int) -> UTXO | None:
        return entries.get((txid, vout))

    backend.get_utxo = AsyncMock(side_effect=get_utxo)
    backend.chain_entries = entries
    return backend


def _session(
    buyout: FakeBuyout | None = None,
    *,
    wallet: MagicMock | None = None,
    backend: MagicMock | None = None,
) -> CoinJoinSession:
    return CoinJoinSession(
        taker_nick="J5taker",
        offer=_offer(),
        wallet=wallet if wallet is not None else _wallet(),
        backend=backend if backend is not None else _backend(),
        buyout=buyout,
        restrict_md0=False,
    )


async def _negotiate(
    session: CoinJoinSession, *, extended: bool = False
) -> tuple[bool, dict[str, Any]]:
    """Run !fill and !auth with the PoDLE cryptography stubbed out."""
    commitment = "ab" * 32
    taker_pk = CryptoSession().get_pubkey_hex()
    ok, _ = await session.handle_fill(CJ_AMOUNT, commitment, taker_pk)
    assert ok

    revelation: dict[str, Any] = {
        "P": b"\x02" + b"\x01" * 32,
        "P2": b"\x02" + b"\x02" * 32,
        "sig": b"\x03" * 32,
        "e": b"\x04" * 32,
        "txid": PODLE_TXID,
        "vout": 0,
    }
    if extended:
        # A neutrino-capable taker; the maker answers in the same format.
        revelation["scriptpubkey"] = TAKER_SCRIPT.hex()
        revelation["blockheight"] = 640
    with (
        patch("maker.coinjoin.parse_podle_revelation", return_value=revelation),
        patch("maker.coinjoin.verify_podle", return_value=(True, "")),
        patch("maker.coinjoin.verify_podle_binding", return_value=(True, "")),
    ):
        return await session.handle_auth(commitment, dict(revelation), "")


def _coinjoin_tx(
    session: CoinJoinSession,
    *,
    change_value: int | None = None,
    include_channel: bool = True,
) -> str:
    """A well-formed CoinJoin paying the maker its CJ output and change."""
    wallet_total = sum(utxo.value for utxo in session.our_utxos.values())
    channel_total = CHANNEL_VALUE if session.buyout is not None else 0
    expected_change = wallet_total + channel_total - CJ_AMOUNT - session.offer.txfee + CJFEE
    inputs = [TxInput.from_hex(WALLET_TXID, 0), TxInput.from_hex(TAKER_TXID, 0)]
    if include_channel:
        inputs.insert(1, TxInput.from_hex(CHANNEL_TXID, CHANNEL_VOUT))
    outputs = [
        TxOutput(value=CJ_AMOUNT, script=CJ_SCRIPT),
        TxOutput(
            value=expected_change if change_value is None else change_value,
            script=ESCROW_SCRIPT if session.buyout is not None else WALLET_SCRIPT,
        ),
        TxOutput(value=CJ_AMOUNT, script=TAKER_SCRIPT),
    ]
    return serialize_transaction(2, inputs, outputs, 0).hex()


async def test_wallet_only_session_is_unchanged() -> None:
    wallet = _wallet({0: 5_000_000})
    wallet.select_utxos_with_merge.return_value = [_wallet_utxo(1_500_000, mixdepth=0)]
    wallet.get_new_internal_address.side_effect = [CJ_ADDRESS, WALLET_ADDRESS]
    session = _session(wallet=wallet)

    ok, response = await _negotiate(session)

    assert ok
    assert session.channel_prevouts == {}
    assert session.change_address == WALLET_ADDRESS
    assert session.wallet_change_address == WALLET_ADDRESS
    assert response["utxo_list"] == f"{WALLET_TXID}:0"
    # The unadjusted maker requirement still gates selection.
    assert wallet.select_utxos_with_merge.call_args.args[1] == required_maker_input(
        session.offer, CJ_AMOUNT
    )

    session.state = CoinJoinState.IOAUTH_SENT
    signed_ok, signed = await session.handle_tx(_coinjoin_tx(session, include_channel=False))

    assert signed_ok
    assert len(signed["signatures"]) == 1


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"network": "signet"}, "networks differ"),
        ({"fingerprint": "deadbeef"}, "not bound to this wallet"),
        ({"fingerprint": None}, "not bound to this wallet"),
        ({"mixdepth": None}, "not bound to a wallet mixdepth"),
        ({"mixdepth": True}, "not bound to a wallet mixdepth"),
        ({"mixdepth": "2"}, "not bound to a wallet mixdepth"),
        ({"mixdepth": 9}, "outside this wallet"),
        ({"binding": {}}, "networks differ"),
    ],
)
async def test_buyout_must_be_bound_to_this_wallet(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _session(FakeBuyout(**kwargs))


async def test_buyout_requires_taproot_pit_and_utxo_lookups() -> None:
    segwit_offer = _offer().model_copy(update={"ordertype": OfferType.SW0_ABSOLUTE})
    with pytest.raises(ValueError, match="Taproot wallet"):
        CoinJoinSession(
            taker_nick="J5taker",
            offer=segwit_offer,
            wallet=_wallet(address_type="p2wpkh"),
            backend=_backend(),
            buyout=FakeBuyout(),
            restrict_md0=False,
        )

    backend = _backend()
    backend.can_lookup_arbitrary_utxos.return_value = False
    with pytest.raises(ValueError, match="look up arbitrary UTXOs"):
        _session(FakeBuyout(), backend=backend)


async def test_buyout_funds_the_round_from_its_bound_mixdepth() -> None:
    wallet = _wallet({0: 5_000_000, BOUND_MIXDEPTH: 5_000_000})
    buyout = FakeBuyout()
    session = _session(buyout, wallet=wallet)

    ok, response = await _negotiate(session)

    assert ok
    assert wallet.select_utxos_with_merge.call_args.args[0] == BOUND_MIXDEPTH
    # Channel value covers the round except for the escrow reserve, but an
    # ordinary wallet input is still required.
    expected_required = buyout.wallet_funding_required(
        required_maker_input(session.offer, CJ_AMOUNT)
    )
    assert wallet.select_utxos_with_merge.call_args.args[1] == expected_required
    assert expected_required > 0
    assert session.our_utxos.keys() == {(WALLET_TXID, 0)}
    # A channel funding output can never be picked as a wallet input.
    assert (CHANNEL_TXID, CHANNEL_VOUT) in wallet.select_utxos_with_merge.call_args.kwargs[
        "exclude"
    ]
    assert session.channel_prevouts == {
        (CHANNEL_TXID, CHANNEL_VOUT): (CHANNEL_VALUE, ESCROW_SCRIPT)
    }
    # Escrow is the change destination, but never wallet address history.
    assert response["change_addr"] == ESCROW_ADDRESS
    assert session.wallet_change_address == ""
    # Channel outpoints are disclosed after the wallet ones.
    assert response["utxo_list"] == f"{WALLET_TXID}:0,{CHANNEL_TXID}:{CHANNEL_VOUT}"
    assert buyout.rounds == 1


async def test_buyout_cannot_fund_an_unbound_mixdepth() -> None:
    wallet = _wallet({0: 5_000_000})
    buyout = FakeBuyout()
    session = _session(buyout, wallet=wallet)

    ok, response = await _negotiate(session)

    assert not ok and response["error_code"] == "UTXO selection failed"
    wallet.select_utxos_with_merge.assert_not_called()
    assert buyout.rounds == 0


async def test_prepared_buyout_serves_only_one_round() -> None:
    buyout = FakeBuyout()
    first = _session(buyout)
    assert (await _negotiate(first))[0]

    second_wallet = _wallet()
    second = _session(buyout, wallet=second_wallet)
    ok, response = await _negotiate(second)

    assert not ok and response["error_code"] == "UTXO selection failed"
    assert buyout.rounds == 1
    # The durable reservation is never returned, but the wallet inputs of the
    # failed round are released.
    second_wallet.release_coinjoin_inputs.assert_called_once()
    assert second_wallet.release_coinjoin_inputs.call_args.args[0] == {(WALLET_TXID, 0)}


@pytest.mark.parametrize(
    "utxo,extended,error",
    [
        (None, False, "not found on the blockchain"),
        (
            _chain_utxo(CHANNEL_TXID, CHANNEL_VOUT, 999, ESCROW_SCRIPT, ESCROW_ADDRESS),
            False,
            "does not match the buyout terms",
        ),
        (
            _chain_utxo(CHANNEL_TXID, CHANNEL_VOUT, CHANNEL_VALUE, CJ_SCRIPT, CJ_ADDRESS),
            False,
            "does not match the buyout terms",
        ),
        (
            _chain_utxo(
                CHANNEL_TXID,
                CHANNEL_VOUT,
                CHANNEL_VALUE,
                ESCROW_SCRIPT,
                ESCROW_ADDRESS,
                confirmations=0,
            ),
            False,
            "too young",
        ),
        (
            _chain_utxo(
                CHANNEL_TXID,
                CHANNEL_VOUT,
                CHANNEL_VALUE,
                ESCROW_SCRIPT,
                ESCROW_ADDRESS,
                height=None,
            ),
            True,
            "no confirmed block height",
        ),
    ],
)
async def test_channel_input_is_verified_against_our_own_backend(
    utxo: UTXO | None, extended: bool, error: str
) -> None:
    backend = _backend()
    if utxo is None:
        del backend.chain_entries[(CHANNEL_TXID, CHANNEL_VOUT)]
    else:
        backend.chain_entries[(CHANNEL_TXID, CHANNEL_VOUT)] = utxo
    buyout = FakeBuyout()
    session = _session(buyout, backend=backend)

    ok, response = await _negotiate(session, extended=extended)

    assert not ok and error in response["error"]
    # An unusable channel never consumes the single-use reservation.
    assert buyout.rounds == 0


async def test_channel_value_is_required_input_and_expected_escrow_change() -> None:
    session = _session(FakeBuyout())
    assert (await _negotiate(session))[0]
    session.state = CoinJoinState.IOAUTH_SENT

    ok, response = await session.handle_tx(_coinjoin_tx(session, include_channel=False))
    assert not ok and "not included in transaction" in response["error"]

    session.state = CoinJoinState.IOAUTH_SENT
    short = _coinjoin_tx(session, change_value=MINIMUM_CHANGE - 1)
    ok, response = await session.handle_tx(short)
    assert not ok and "Change output value too low" in response["error"]


def test_external_input_can_never_also_be_a_wallet_utxo() -> None:
    """The verification guard itself, independent of how selection avoids it."""
    utxo = _wallet_utxo()
    tx_hex = serialize_transaction(
        2,
        [TxInput.from_hex(WALLET_TXID, 0)],
        [TxOutput(value=CJ_AMOUNT, script=CJ_SCRIPT)],
        0,
    ).hex()
    is_valid, error = verify_unsigned_transaction(
        tx_hex=tx_hex,
        our_utxos={(WALLET_TXID, 0): utxo},
        cj_address=CJ_ADDRESS,
        change_address=ESCROW_ADDRESS,
        amount=CJ_AMOUNT,
        cjfee=CJFEE,
        txfee=0,
        offer_type=OfferType.TR0_ABSOLUTE,
        network=NetworkType.REGTEST,
        external_prevouts={(WALLET_TXID, 0): (utxo.value, WALLET_SCRIPT)},
    )

    assert not is_valid and "overlap" in error


async def test_verified_parent_is_bound_before_any_signature() -> None:
    buyout = FakeBuyout()
    session = _session(buyout)
    assert (await _negotiate(session))[0]
    session.state = CoinJoinState.IOAUTH_SENT
    tx_hex = _coinjoin_tx(session)

    ok, response = await session.handle_tx(tx_hex)

    assert ok
    raw, prevouts, height = buyout.validated[0]
    assert raw == bytes.fromhex(tx_hex)
    assert height == CHAIN_TIP
    # Every prevout is chain-verified, including the other participants'.
    assert prevouts == {
        (WALLET_TXID, 0): (WALLET_VALUE, WALLET_SCRIPT),
        (CHANNEL_TXID, CHANNEL_VOUT): (CHANNEL_VALUE, ESCROW_SCRIPT),
        (TAKER_TXID, 0): (600_000, TAKER_SCRIPT),
    }
    assert buyout.signed == [bytes.fromhex(tx_hex)]

    wallet_sig, channel_sig = response["signatures"]
    assert base64.b64decode(wallet_sig)[1:65] == WALLET_SIGNATURE
    channel = base64.b64decode(channel_sig)
    assert channel == bytes([64]) + CHANNEL_SIGNATURE + bytes([32]) + ESCROW_SCRIPT[2:]
    # The wallet is never asked to sign a channel input.
    assert session.wallet.sign_input.call_count == 1
    assert session.wallet.sign_input.call_args.args[2].txid == WALLET_TXID


async def test_rejected_parent_is_never_signed() -> None:
    buyout = FakeBuyout()
    buyout.validate_error = ValueError("parent does not match the buyout terms")
    session = _session(buyout)
    assert (await _negotiate(session))[0]
    session.state = CoinJoinState.IOAUTH_SENT

    ok, response = await session.handle_tx(_coinjoin_tx(session))

    assert not ok and "parent validation failed" in response["error"]
    assert buyout.signed == []
    session.wallet.sign_input.assert_not_called()
    assert not session.signing_boundary_crossed


async def test_channel_signing_failure_returns_nothing_usable() -> None:
    buyout = FakeBuyout()
    buyout.sign_error = TimeoutError("uncertain peer authorization")
    session = _session(buyout)
    assert (await _negotiate(session))[0]
    session.state = CoinJoinState.IOAUTH_SENT

    ok, response = await session.handle_tx(_coinjoin_tx(session))

    assert not ok and "signatures" not in response
    # The irreversible boundary stays crossed: the channel signature may exist.
    assert session.signing_boundary_crossed
    session.wallet.sign_input.assert_not_called()
