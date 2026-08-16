"""Maker-side durable authorization and opening for true-pooling ring edges."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address
from jmcore.models import Offer, OfferType
from jmcore.protocol import FEATURE_CHANNEL_CHANGE, FEATURE_RING_EDGE_AUTH
from jmswap.ring_authorization import (
    RingChannelPoint,
    RingEdgeAuthorization,
    RingEdgeAuthorizationStore,
)

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

    async def open_external_channel(*_args: object, **_kwargs: object) -> str | None:
        if open_result is not None:
            inner._channel_user_id = "pending-channel"
        return open_result

    inner.open_external_channel = AsyncMock(side_effect=open_external_channel)
    inner.restore_external_channel = AsyncMock()
    inner._ring_authorization_ids = {}
    inner._channel_edge_id = None
    inner._channel_authorization_id = None
    inner._channel_user_id = None
    inner._cancel_channel_change = AsyncMock()
    inner.new_reserved_external_funding_id.return_value = None
    inner.ldk = MagicMock()
    inner.ldk.supports_reserved_external_funding_id = True
    inner.ldk.supports_external_funding_cancellation = True
    inner.ldk.cancel_external_channel_funding = AsyncMock()
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
    incapable = MagicMock()
    incapable.supports_reserved_external_funding_id = False
    incapable.supports_external_funding_cancellation = False
    incapable_features = await advertised_features(incapable)
    assert FEATURE_CHANNEL_CHANGE in incapable_features
    assert FEATURE_RING_EDGE_AUTH not in incapable_features
    available = MagicMock()
    available.supports_reserved_external_funding_id = True
    available.supports_external_funding_cancellation = True
    available_features = await advertised_features(available)
    assert FEATURE_CHANNEL_CHANGE in available_features
    assert FEATURE_RING_EDGE_AUTH in available_features


async def test_chanreq_rejects_backend_without_durable_ring_support(tmp_path: Path) -> None:
    session, inner = _session(_funding_address())
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )
    inner.ldk.supports_reserved_external_funding_id = False

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.open_external_channel.assert_not_awaited()
    assert send.await_args is not None
    assert send.await_args.args[2] == "enc()"


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
        user_channel_id=None,
    )
    record = bot.ring_edge_authorization_store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_address == address
    assert record.pending_channel_id == "pending-channel"
    assert send.await_args is not None
    nick, command, content = send.await_args.args
    assert nick == TAKER_NICK
    assert command == "chanaddr"
    assert content == (f"enc(1 {authorization.edge_id} {authorization.authorization_id} {address})")


async def test_chanreq_reserves_lnd_pending_id_before_opening(tmp_path: Path) -> None:
    address = _funding_address()
    session, inner = _session(address)
    authorization = _authorization()
    bot, _send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )
    inner.new_reserved_external_funding_id.return_value = "ab" * 32

    async def open_after_reservation(*_args: object, **kwargs: object) -> str:
        record = bot.ring_edge_authorization_store.load(authorization.edge_id)
        assert record is not None
        assert record.pending_channel_id == "ab" * 32
        assert kwargs["user_channel_id"] == "ab" * 32
        inner._channel_user_id = "ab" * 32
        return address

    inner.open_external_channel = AsyncMock(side_effect=open_after_reservation)

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    record = bot.ring_edge_authorization_store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_address == address
    assert record.pending_channel_id == "ab" * 32


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


async def test_chanreq_failed_reserved_open_cancels_exact_intent(tmp_path: Path) -> None:
    session, inner = _session(None)
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )
    inner.new_reserved_external_funding_id.return_value = "ab" * 32

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.ldk.cancel_external_channel_funding.assert_awaited_once_with("ab" * 32)
    record = bot.ring_edge_authorization_store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_canceled is True
    assert record.funding_cancel_completed is True
    assert send.await_args is not None
    assert send.await_args.args[2] == "enc()"


async def test_chanreq_interrupted_reservation_cancels_without_reopening(tmp_path: Path) -> None:
    session, inner = _session(_funding_address())
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    store = bot.ring_edge_authorization_store
    store.authorize(authorization, local_nick=FUNDER_NICK)
    store.reserve_funding(authorization.edge_id, authorization.authorization_id, "ab" * 32)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.open_external_channel.assert_not_awaited()
    inner.ldk.cancel_external_channel_funding.assert_awaited_once_with("ab" * 32)
    record = store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_cancel_completed is True
    assert send.await_args is not None
    assert send.await_args.args[2] == "enc()"


async def test_chanreq_cleanup_failure_leaves_durable_retry_intent(tmp_path: Path) -> None:
    session, inner = _session(None)
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )
    inner.new_reserved_external_funding_id.return_value = "ab" * 32
    inner.ldk.cancel_external_channel_funding.side_effect = RuntimeError("LND unavailable")

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    record = bot.ring_edge_authorization_store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_canceled is True
    assert record.funding_cancel_completed is False
    assert send.await_args is not None
    assert send.await_args.args[2] == "enc()"


async def test_invalid_chanreq_does_not_cancel_unrelated_pending_channel(tmp_path: Path) -> None:
    session, inner = _session(_funding_address())
    bot, send = _bot(tmp_path)
    inner._channel_user_id = "unrelated-channel"
    inner._channel_edge_id = "ff" * 32
    inner._channel_authorization_id = "ee" * 32
    inner.crypto.decrypt.return_value = "malformed"

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner._cancel_channel_change.assert_not_awaited()
    inner.ldk.cancel_external_channel_funding.assert_not_awaited()
    assert send.await_args is not None
    assert send.await_args.args[2] == "enc()"


async def test_chancancel_cancels_only_the_authorized_pending_edge(tmp_path: Path) -> None:
    session, inner = _session(_funding_address())
    authorization = _authorization()
    bot, _send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner._channel_edge_id = authorization.edge_id
    inner._channel_authorization_id = authorization.authorization_id
    bot.ring_edge_authorization_store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        _funding_address(),
        "5120" + "aa" * 32,
        pending_channel_id="pending-channel",
    )
    bot.ring_edge_authorization_store.bind_channel_point(
        authorization.edge_id,
        authorization.authorization_id,
        RingChannelPoint(txid="98" * 32, vout=0),
    )
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chancancel(bot, "chancancel ENC", "directory")

    inner._cancel_channel_change.assert_awaited_once_with()
    record = bot.ring_edge_authorization_store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_canceled is True


async def test_chanreq_after_restart_reuses_the_durable_pending_open(tmp_path: Path) -> None:
    address = _funding_address()
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    bot.ring_edge_authorization_store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        address,
        "5120" + "aa" * 32,
        pending_channel_id="pending-channel",
    )
    bot.ring_edge_authorization_store.bind_channel_point(
        authorization.edge_id,
        authorization.authorization_id,
        RingChannelPoint(txid="98" * 32, vout=0),
    )
    session, inner = _session(_funding_address())
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.open_external_channel.assert_not_awaited()
    inner.restore_external_channel.assert_awaited_once_with(
        user_channel_id="pending-channel",
        funding_address=address,
        funding_scriptpubkey="5120" + "aa" * 32,
        capacity=500_000,
        push_to_counterparty_msat=175_000_000,
        edge_id=authorization.edge_id,
        authorization_id=authorization.authorization_id,
    )
    assert send.await_args is not None
    _, command, content = send.await_args.args
    assert command == "chanaddr"
    assert content == f"enc(1 {authorization.edge_id} {authorization.authorization_id} {address})"


async def test_chanreq_rejects_a_canceled_funding_binding(tmp_path: Path) -> None:
    address = _funding_address()
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    bot.ring_edge_authorization_store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        address,
        "5120" + "aa" * 32,
        pending_channel_id="pending-channel",
    )
    bot.ring_edge_authorization_store.cancel_funding(
        authorization.edge_id, authorization.authorization_id
    )
    session, inner = _session(_funding_address())
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.open_external_channel.assert_not_awaited()
    inner.restore_external_channel.assert_not_awaited()
    assert send.await_args is not None
    _, command, content = send.await_args.args
    assert command == "chanaddr"
    assert content == "enc()"


async def test_chanreq_replays_a_completed_binding_without_reopening(tmp_path: Path) -> None:
    address = _funding_address()
    authorization = _authorization()
    bot, send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    bot.ring_edge_authorization_store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        address,
        "5120" + "aa" * 32,
        pending_channel_id="pending-channel",
    )
    bot.ring_edge_authorization_store.bind_channel_point(
        authorization.edge_id,
        authorization.authorization_id,
        RingChannelPoint(txid="99" * 32, vout=0),
    )
    bot.ring_edge_authorization_store.complete_funding(
        authorization.edge_id,
        authorization.authorization_id,
        RingChannelPoint(txid="99" * 32, vout=0),
    )
    session, inner = _session(_funding_address())
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chanreq(bot, "chanreq ENC", "directory")

    inner.open_external_channel.assert_not_awaited()
    inner.restore_external_channel.assert_not_awaited()
    assert send.await_args is not None
    _, command, content = send.await_args.args
    assert command == "chanaddr"
    assert content == f"enc(1 {authorization.edge_id} {authorization.authorization_id} {address})"


async def test_chancancel_after_restart_marks_pending_funding_canceled(tmp_path: Path) -> None:
    address = _funding_address()
    authorization = _authorization()
    bot, _send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    bot.ring_edge_authorization_store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        address,
        "5120" + "aa" * 32,
        pending_channel_id="pending-channel",
    )
    bot.ring_edge_authorization_store.bind_channel_point(
        authorization.edge_id,
        authorization.authorization_id,
        RingChannelPoint(txid="98" * 32, vout=0),
    )
    session, inner = _session(None)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chancancel(bot, "chancancel ENC", "directory")

    inner.restore_external_channel.assert_awaited_once()
    inner._cancel_channel_change.assert_awaited_once_with()
    record = bot.ring_edge_authorization_store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_canceled is True
    assert record.funding_completed is False


async def test_chancancel_does_not_cancel_a_completed_ring_channel(tmp_path: Path) -> None:
    address = _funding_address()
    authorization = _authorization()
    bot, _send = _bot(tmp_path)
    bot.ring_edge_authorization_store.authorize(authorization, local_nick=FUNDER_NICK)
    bot.ring_edge_authorization_store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        address,
        "5120" + "aa" * 32,
        pending_channel_id="pending-channel",
    )
    bot.ring_edge_authorization_store.bind_channel_point(
        authorization.edge_id,
        authorization.authorization_id,
        RingChannelPoint(txid="99" * 32, vout=0),
    )
    bot.ring_edge_authorization_store.complete_funding(
        authorization.edge_id,
        authorization.authorization_id,
        RingChannelPoint(txid="99" * 32, vout=0),
    )
    session, inner = _session(None)
    inner._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    inner.crypto.decrypt.return_value = (
        f"1 {authorization.edge_id} {authorization.authorization_id}"
    )

    await session.on_chancancel(bot, "chancancel ENC", "directory")

    inner.restore_external_channel.assert_not_awaited()
    inner._cancel_channel_change.assert_not_awaited()


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
