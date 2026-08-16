"""Maker-side durable authorization and opening for true-pooling ring edges."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address
from jmcore.models import Offer, OfferType
from jmcore.protocol import FEATURE_CHANNEL_CHANGE, FEATURE_RING_EDGE_AUTH
from jmswap.ring_authorization import RingEdgeAuthorization, RingEdgeAuthorizationStore

from maker.coinjoin import CoinJoinSession as InnerCoinJoinSession
from maker.coinjoin import CoinJoinState
from maker.maker_session import MakerSession

FUNDER_NICK = "J5funder"
TAKER_NICK = "J5taker"
FUNDER_SESSION_KEY = "44" * 32
TAKER_SESSION_KEY = "33" * 32
FUNDER_NODE = "02" + "66" * 32 + "@funder.example:9735"
RECEIVER_NODE = "03" + "77" * 32 + "@receiver.example:9735"


def _funding_address() -> str:
    scriptpubkey = create_p2tr_scriptpubkey(b"\xaa" * 32)
    return scriptpubkey_to_address(scriptpubkey, "regtest")


def _authorization(**overrides: object) -> RingEdgeAuthorization:
    values: dict[str, object] = {
        "edge_id": "11" * 32,
        "coinjoin_context": "22" * 32,
        "taker_nick": TAKER_NICK,
        "taker_session_pubkey": TAKER_SESSION_KEY,
        "funder_nick": FUNDER_NICK,
        "receiver_nick": "J5receiver",
        "funder_session_pubkey": FUNDER_SESSION_KEY,
        "receiver_session_pubkey": "55" * 32,
        "funder_ln_node": FUNDER_NODE,
        "receiver_ln_node": RECEIVER_NODE,
        "coinjoin_amount_sats": 1_000_000,
        "capacity_sats": 500_000,
        "funder_contribution_sats": 325_000,
        "receiver_contribution_sats": 175_000,
        "expires_at_unix": int(time.time()) + 120,
    }
    values.update(overrides)
    return RingEdgeAuthorization.model_validate(values)


def _session(open_result: str | None) -> tuple[MakerSession, MagicMock]:
    inner = MagicMock()
    inner.taker_nick = TAKER_NICK
    inner.crypto.is_encrypted = True
    inner.crypto.encrypt.side_effect = lambda value: f"enc({value})"
    inner.crypto.get_pubkey_hex.return_value = FUNDER_SESSION_KEY
    inner.open_external_channel = AsyncMock(return_value=open_result)
    inner._ring_authorization_ids = {}
    inner._channel_edge_id = None
    inner._channel_authorization_id = None
    inner._channel_user_id = None
    inner._cancel_channel_change = AsyncMock()
    inner.ln_node = FUNDER_NODE
    inner.taker_nacl_pk = TAKER_SESSION_KEY
    inner.commitment = bytes.fromhex("22" * 32)
    inner.amount = 1_000_000
    inner.created_at = time.time()
    inner.session_timeout_sec = 300
    inner.state = CoinJoinState.IOAUTH_SENT
    return MakerSession(inner=inner), inner


def _bot(tmp_path: Path) -> tuple[MagicMock, AsyncMock]:
    client = MagicMock()
    client.send_private_message = AsyncMock()
    bot = MagicMock()
    bot.nick = FUNDER_NICK
    bot.directory_clients = {"d": client}
    bot.ring_edge_authorization_store = RingEdgeAuthorizationStore(tmp_path)
    return bot, client.send_private_message


async def test_ring_feature_requires_store_and_channel_backend(tmp_path: Path) -> None:
    offer = Offer(
        counterparty=FUNDER_NICK,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=1,
        maxsize=2_000_000,
        txfee=0,
        cjfee=0,
    )

    async def advertised_features(ldk: object | None) -> list[str]:
        inner = InnerCoinJoinSession(
            taker_nick=TAKER_NICK,
            offer=offer,
            wallet=MagicMock(),
            backend=MagicMock(),
            combine_capable=True,
            ln_node=FUNDER_NODE,
            ldk=ldk,
            ring_edge_authorization_store=RingEdgeAuthorizationStore(tmp_path / str(id(ldk))),
        )
        success, response = await inner.handle_fill(1_000_000, "11" * 32, "22" * 32)
        assert success is True
        return response["features"]

    unavailable = await advertised_features(None)
    assert FEATURE_RING_EDGE_AUTH not in unavailable
    available = await advertised_features(MagicMock())
    assert FEATURE_CHANNEL_CHANGE in available
    assert FEATURE_RING_EDGE_AUTH in available


async def test_chanauth_persists_matching_record_and_acknowledges(tmp_path: Path) -> None:
    session, inner = _session(_funding_address())
    authorization = _authorization()
    inner.crypto.decrypt.return_value = authorization.to_transport()
    bot, send = _bot(tmp_path)

    await session.on_chanauth(bot, "chanauth ENC", "directory")

    record = bot.ring_edge_authorization_store.load(authorization.edge_id)
    assert record is not None
    assert record.authorization == authorization
    assert inner._ring_authorization_ids == {authorization.edge_id: authorization.authorization_id}
    assert send.await_args is not None
    _, command, content = send.await_args.args
    assert command == "chanauthack"
    assert content == f"enc({authorization.authorization_id})"


async def test_chanauth_rejects_local_ln_identity_mismatch(tmp_path: Path) -> None:
    session, inner = _session(_funding_address())
    authorization = _authorization(funder_ln_node="02" + "99" * 32 + "@other.example:9735")
    inner.crypto.decrypt.return_value = authorization.to_transport()
    bot, send = _bot(tmp_path)

    await session.on_chanauth(bot, "chanauth ENC", "directory")

    assert bot.ring_edge_authorization_store.load(authorization.edge_id) is None
    assert send.await_args is not None
    _, command, content = send.await_args.args
    assert command == "chanauthack"
    assert content == "enc()"


async def test_chanreq_derives_channel_terms_and_replies_with_binding(tmp_path: Path) -> None:
    address = _funding_address()
    session, inner = _session(address)
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.open_external_channel.assert_awaited_once_with(
        RECEIVER_NODE,
        500_000,
        push_to_counterparty_msat=175_000_000,
    )
    record = bot.ring_edge_authorization_store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_address == address
    assert send.await_args is not None
    nick, command, content = send.await_args.args
    assert nick == TAKER_NICK
    assert command == "chanaddr"
    assert content == (f"enc(1 {authorization.edge_id} {authorization.authorization_id} {address})")


async def test_chanreq_open_failure_replies_empty_address(tmp_path: Path) -> None:
    session, inner = _session(None)
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    assert send.await_args is not None
    _, command, content = send.await_args.args
    assert command == "chanaddr"
    assert content == "enc()"


async def test_chancancel_cancels_only_the_authorized_pending_edge(tmp_path: Path) -> None:
    session, inner = _session(_funding_address())
    authorization = _authorization()
    bot, _send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner._channel_edge_id = authorization.edge_id
    inner._channel_authorization_id = authorization.authorization_id
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chancancel(bot, "chancancel ENC", "directory")

    inner._cancel_channel_change.assert_awaited_once_with()


async def test_chanreq_rejects_mutable_legacy_terms(tmp_path: Path) -> None:
    session, inner = _session(_funding_address())
    inner.crypto.decrypt.return_value = f"{RECEIVER_NODE} 500000 175000000"
    bot, send = _bot(tmp_path)

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.open_external_channel.assert_not_awaited()
    assert send.await_args is not None
    _, command, content = send.await_args.args
    assert command == "chanaddr"
    assert content == "enc()"
