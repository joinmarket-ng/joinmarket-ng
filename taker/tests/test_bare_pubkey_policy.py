"""Handling of key-only ``!pubkey`` replies.

Private message signatures cover the body and hostid, not the command. A maker
key-only body signed for some other command therefore verifies as that maker's
``!pubkey``, and the key it names is only proven by ``!ioauth``, after ``!auth``
has revealed our PoDLE commitment. These tests pin the strict reply grammar,
the routing policy applied before ``!auth``, and the direct-peer identity check.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from _taker_test_helpers import (
    OneShotDirectory,
    make_directory_client,
    register_direct_peer,
)
from jmcore.crypto import NickIdentity
from jmcore.encryption import CryptoSession
from jmcore.models import Offer, OfferType
from jmcore.network import ONION_HOSTID
from jmcore.protocol import NOT_SERVING_ONION_HOSTNAME, MessageType

from taker.coinjoin_session import CoinJoinSession, parse_pubkey_fields
from taker.config import BarePubkeyPolicy
from taker.models import MakerSession
from taker.taker import Taker

KEY = "ab" * 32
SUFFIX = "signpk sig"


def _offer(nick: str) -> Offer:
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=100_000,
        maxsize=10_000_000,
        txfee=0,
        cjfee="0.001",
        fidelity_bond_value=0,
    )


class TestParsePubkeyFields:
    def test_key_only(self) -> None:
        assert parse_pubkey_fields(f"{KEY} {SUFFIX}") == [KEY]

    def test_extension_fields(self) -> None:
        payload = f"{KEY} features=neutrino_compat b=00ff {SUFFIX}"
        assert parse_pubkey_fields(payload) == [KEY, "features=neutrino_compat", "b=00ff"]

    @pytest.mark.parametrize("separator", ["\t", "\x0b", "\x0c", "\n", "\r", "\u00a0", "\u2003"])
    def test_non_space_whitespace_is_rejected(self, separator: str) -> None:
        # Such a body is a single field to the signature verifier but would be
        # two fields to str.split(), so it must not count as extended.
        assert parse_pubkey_fields(f"{KEY}{separator}features=neutrino_compat {SUFFIX}") is None

    def test_empty_field_is_rejected(self) -> None:
        assert parse_pubkey_fields(f"{KEY}  features=neutrino_compat {SUFFIX}") is None

    @pytest.mark.parametrize(
        "key",
        ["ab" * 31 + "a", "ab" * 32 + "a", "zz" * 32, "Commitment", "", "0x" + "ab" * 31],
    )
    def test_key_must_be_64_hex(self, key: str) -> None:
        assert parse_pubkey_fields(f"{key} {SUFFIX}") is None

    def test_missing_signature_suffix_is_rejected(self) -> None:
        assert parse_pubkey_fields(KEY) is None


class TestRelabelledBodies:
    """Bodies a maker signed for another command, presented as ``!pubkey``."""

    @staticmethod
    async def _deliver(body: str) -> tuple[dict[str, Any], MakerSession, CoinJoinSession]:
        client = make_directory_client()
        maker = NickIdentity(5)
        line = f"{maker.nick}!{client.nick}!pubkey {maker.sign_message(body, ONION_HOSTID)}"
        client.clients = {"dir1": OneShotDirectory([line])}  # type: ignore[dict-item]
        responses = await client.wait_for_responses([maker.nick], "!pubkey", timeout=1.0)

        session = CoinJoinSession()
        session.crypto_session = CryptoSession()
        mk = MakerSession(nick=maker.nick, offer=_offer(maker.nick))
        session.maker_sessions = {maker.nick: mk}
        return responses[maker.nick], mk, session

    @pytest.mark.asyncio
    async def test_tab_joined_body_verifies_but_is_rejected(self) -> None:
        key = CryptoSession().get_pubkey_hex()
        response, mk, session = await self._deliver(f"{key}\tfeatures=neutrino_compat")

        # The signature does verify: the defense is the grammar, not the signature.
        assert "error" not in response
        assert session.process_pubkey_response(mk.nick, response["data"]) is False
        assert mk.supports_neutrino_compat is False
        assert mk.crypto is None

    @pytest.mark.asyncio
    async def test_key_only_body_is_marked_not_extended(self) -> None:
        key = CryptoSession().get_pubkey_hex()
        response, mk, session = await self._deliver(key)

        assert session.process_pubkey_response(mk.nick, response["data"]) is True
        assert mk.pubkey == key
        assert mk.pubkey_extended is False

    @pytest.mark.asyncio
    async def test_extended_body_is_marked_extended(self) -> None:
        key = CryptoSession().get_pubkey_hex()
        response, mk, session = await self._deliver(f"{key} features=neutrino_compat")

        assert session.process_pubkey_response(mk.nick, response["data"]) is True
        assert mk.pubkey_extended is True
        assert mk.supports_neutrino_compat is True


def _make_taker(
    policy: BarePubkeyPolicy, *, minimum_makers: int = 1, verified: bool = False
) -> Taker:
    with patch.object(Taker, "__init__", lambda self, *a, **k: None):
        taker = Taker.__new__(Taker)
    taker._session = CoinJoinSession()
    taker._session.attach(taker)
    taker.wallet = MagicMock()
    taker.backend = AsyncMock()
    taker.backend.requires_neutrino_metadata = MagicMock(return_value=False)
    taker.config = MagicMock()
    taker.config.minimum_makers = minimum_makers
    taker.config.maker_timeout_sec = 1
    taker.config.bare_pubkey_policy = policy

    dc = MagicMock()
    dc.send_privmsg = AsyncMock(side_effect=lambda nick, cmd, data, **kw: kw["force_channel"])
    dc.upgrade_channel_prefer_direct = MagicMock(side_effect=lambda n, ch: ch)
    dc.wait_for_responses = AsyncMock(return_value={})
    dc.connect_verified_direct = AsyncMock(return_value=verified)
    taker.directory_client = dc

    commitment = MagicMock()
    commitment.has_neutrino_metadata.return_value = False
    commitment.to_revelation.return_value = {
        "utxo": "a" * 64 + ":0",
        "P": "00",
        "P2": "00",
        "sig": "00",
        "e": "00",
    }
    taker._session.podle_commitment = commitment
    return taker


def _filled(nick: str, *, extended: bool) -> MakerSession:
    peer = CryptoSession()
    maker = MakerSession(nick=nick, offer=_offer(nick), pubkey_extended=extended)
    maker.pubkey = peer.get_pubkey_hex()
    maker.crypto = CryptoSession()
    maker.crypto.setup_encryption(peer.get_pubkey_hex())
    maker.comm_channel = "directory:dir1:5222"
    maker.responded_fill = True
    return maker


def _auth_channels(taker: Taker) -> dict[str, str]:
    dc = taker.directory_client
    return {
        call.args[0]: call.kwargs["force_channel"]
        for call in dc.send_privmsg.await_args_list  # type: ignore[attr-defined]
        if call.args[1] == "auth"
    }


class TestPhaseAuthPolicy:
    @pytest.mark.asyncio
    async def test_allow_sends_on_the_relayed_channel(self) -> None:
        taker = _make_taker(BarePubkeyPolicy.ALLOW)
        taker._session.maker_sessions = {"J5bare": _filled("J5bare", extended=False)}

        result = await taker._session._phase_auth()

        assert _auth_channels(taker) == {"J5bare": "directory:dir1:5222"}
        assert result.unroutable_makers == []
        taker.directory_client.connect_verified_direct.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_reject_skips_bare_makers_before_any_auth(self) -> None:
        taker = _make_taker(BarePubkeyPolicy.REJECT)
        taker._session.maker_sessions = {
            "J5bare": _filled("J5bare", extended=False),
            "J5ng": _filled("J5ng", extended=True),
        }

        result = await taker._session._phase_auth()

        assert _auth_channels(taker) == {"J5ng": "directory:dir1:5222"}
        assert result.unroutable_makers == ["J5bare"]
        assert "J5bare" not in result.failed_makers
        assert "J5bare" not in taker._session.maker_sessions
        taker.directory_client.connect_verified_direct.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_direct_pins_verified_bare_makers_to_direct(self) -> None:
        taker = _make_taker(BarePubkeyPolicy.DIRECT, verified=True)
        taker._session.maker_sessions = {
            "J5bare": _filled("J5bare", extended=False),
            "J5ng": _filled("J5ng", extended=True),
        }

        result = await taker._session._phase_auth()

        assert _auth_channels(taker) == {"J5bare": "direct", "J5ng": "directory:dir1:5222"}
        assert result.unroutable_makers == []
        # Only the key-only maker needs the route check.
        taker.directory_client.connect_verified_direct.assert_awaited_once_with(  # type: ignore[attr-defined]
            "J5bare", 1.0
        )

    @pytest.mark.asyncio
    async def test_direct_skips_bare_makers_without_verified_route(self) -> None:
        taker = _make_taker(BarePubkeyPolicy.DIRECT, verified=False)
        taker._session.maker_sessions = {
            "J5bare": _filled("J5bare", extended=False),
            "J5ng": _filled("J5ng", extended=True),
        }

        result = await taker._session._phase_auth()

        assert _auth_channels(taker) == {"J5ng": "directory:dir1:5222"}
        assert result.unroutable_makers == ["J5bare"]
        assert "J5bare" not in result.failed_makers

    @pytest.mark.asyncio
    async def test_direct_below_minimum_is_replaceable_without_revealing(self) -> None:
        taker = _make_taker(BarePubkeyPolicy.DIRECT, minimum_makers=2, verified=False)
        taker._session.maker_sessions = {
            "J5bare": _filled("J5bare", extended=False),
            "J5ng": _filled("J5ng", extended=True),
        }

        result = await taker._session._phase_auth()

        assert result.success is False
        assert result.needs_replacement is True
        assert result.podle_revealed is False
        assert result.unroutable_makers == ["J5bare"]
        taker.directory_client.send_privmsg.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_direct_send_failure_skips_only_that_maker(self) -> None:
        taker = _make_taker(BarePubkeyPolicy.DIRECT, verified=True)
        taker._session.maker_sessions = {
            "J5bare": _filled("J5bare", extended=False),
            "J5ng": _filled("J5ng", extended=True),
        }

        async def send(nick: str, cmd: str, data: str, **kw: Any) -> str:
            if kw["force_channel"] == "direct":
                raise RuntimeError("Forced to use direct channel but no connection")
            return kw["force_channel"]

        taker.directory_client.send_privmsg = AsyncMock(side_effect=send)  # type: ignore[method-assign]

        result = await taker._session._phase_auth()

        assert result.unroutable_makers == ["J5bare"]
        expected = taker.directory_client.wait_for_responses.await_args.kwargs["expected_nicks"]  # type: ignore[attr-defined]
        assert expected == ["J5ng"]
        # No retry over a directory.
        sent_to_bare = [
            c
            for c in taker.directory_client.send_privmsg.await_args_list  # type: ignore[attr-defined]
            if c.args[0] == "J5bare"
        ]
        assert [c.kwargs["force_channel"] for c in sent_to_bare] == ["direct"]


class TestConnectVerifiedDirect:
    LOCATION = "makerabc.onion:5222"

    def _client(self, listings: list[str | None], connected: bool = True) -> Any:
        client = make_directory_client()
        nick = "J5maker"
        dirs = {}
        for index, location in enumerate(listings):
            directory = MagicMock()
            directory._active_peers = {} if location is None else {nick: location}
            dirs[f"dir{index}:5222"] = directory
        client.clients = dirs  # type: ignore[assignment]
        if connected:
            peer = MagicMock()
            peer.location = self.LOCATION
            peer.is_connected.return_value = True
            client._peer_connections[nick] = peer
        return client

    @pytest.mark.asyncio
    async def test_single_agreeing_listing(self) -> None:
        client = self._client([self.LOCATION])
        assert await client.connect_verified_direct("J5maker", 0.1) is True

    @pytest.mark.asyncio
    async def test_non_listing_and_not_serving_directories_do_not_count(self) -> None:
        client = self._client([self.LOCATION, None, NOT_SERVING_ONION_HOSTNAME])
        assert await client.connect_verified_direct("J5maker", 0.1) is True

    @pytest.mark.asyncio
    async def test_disagreeing_directories(self) -> None:
        client = self._client([self.LOCATION, "other.onion:5222"])
        assert await client.connect_verified_direct("J5maker", 0.1) is False

    @pytest.mark.asyncio
    async def test_peer_no_longer_listed(self) -> None:
        client = self._client([None])
        assert await client.connect_verified_direct("J5maker", 0.1) is False

    @pytest.mark.asyncio
    async def test_direct_connections_disabled(self) -> None:
        client = self._client([self.LOCATION])
        client.prefer_direct_connections = False
        assert await client.connect_verified_direct("J5maker", 0.1) is False

    @pytest.mark.asyncio
    async def test_waits_for_pending_connection(self) -> None:
        client = self._client([self.LOCATION], connected=False)
        peer = MagicMock()
        peer.location = self.LOCATION
        peer.is_connected.return_value = False

        async def connect() -> bool:
            peer.is_connected.return_value = True
            return True

        client._try_direct_connect = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda nick: client._pending_connect_tasks.__setitem__(
                nick, asyncio.ensure_future(connect())
            )
        )
        client._peer_connections["J5maker"] = peer

        assert await client.connect_verified_direct("J5maker", 1.0) is True
        client._try_direct_connect.assert_called_once_with("J5maker")

    @pytest.mark.asyncio
    async def test_connection_timeout(self) -> None:
        client = self._client([self.LOCATION], connected=False)
        client._try_direct_connect = MagicMock()  # type: ignore[method-assign]
        assert await client.connect_verified_direct("J5maker", 0.01) is False


@pytest.mark.asyncio
async def test_direct_connection_only_speaks_for_the_dialed_nick() -> None:
    client = make_directory_client()
    dialed = NickIdentity(5)
    other = NickIdentity(5)
    register_direct_peer(client, dialed.nick)

    def frame(identity: NickIdentity, body: str) -> bytes:
        signed = identity.sign_message(body, ONION_HOSTID)
        line = f"{identity.nick}!{client.nick}!pubkey {signed}"
        return json.dumps({"type": MessageType.PRIVMSG.value, "line": line}).encode()

    # Validly signed by another maker, but arriving on the dialed connection.
    await client._on_peer_message(dialed.nick, frame(other, "OTHER_NACL"))
    await client._on_peer_message(dialed.nick, frame(dialed, "DIALED_NACL"))
    client.clients = {}

    responses = await client.wait_for_responses([dialed.nick, other.nick], "!pubkey", timeout=0.5)

    assert other.nick not in responses
    assert responses[dialed.nick]["data"].split()[0] == "DIALED_NACL"
