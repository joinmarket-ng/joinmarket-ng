"""Focused protocol and CLI coverage for scripts.diagnose_maker."""

from __future__ import annotations

import asyncio
import base64
import json
import struct
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from jmcore import directory_client, network
from jmcore.crypto import NickIdentity
from jmcore.directory_client import PeerlistSnapshot
from jmcore.models import Offer, OfferType
from jmcore.network import ONION_HOSTID
from jmcore.nick_auth import (
    NickAuthChallenge,
    NickAuthMode,
    NickAuthProof,
    NickAuthResult,
    verify_nick_auth_proof,
)
from jmcore.protocol import (
    FEATURE_NICK_AUTH,
    FEATURE_PEERLIST_FEATURES,
    FeatureSet,
    JM_VERSION,
    MessageType,
)
from scripts import diagnose_maker as tool


DIRECTORY_HOST = "a" * 56 + ".onion"
DIRECTORY_PORT = 17777
DIRECTORY_ENDPOINT = f"{DIRECTORY_HOST}:{DIRECTORY_PORT}"
MAKER_ADDRESS = f"{'b' * 56}.onion:18888"
VALID_USER_NICK = "J59ezUFDWMUnzdbV"
TAKER_KEY = b"\x01" * 32
MAKER_KEY = b"\x02" * 32
CERT_KEY = b"\x03" * 32
UTXO_KEY = b"\x04" * 32
CHALLENGE = "22" * 32


def _envelope(message_type: MessageType, line: str) -> bytes:
    return json.dumps({"type": message_type.value, "line": line}).encode("utf-8")


def _handshake_response(*, nick_auth: bool = False) -> bytes:
    features = {FEATURE_PEERLIST_FEATURES: True}
    if nick_auth:
        features[FEATURE_NICK_AUTH] = True
    return _envelope(
        MessageType.DN_HANDSHAKE,
        json.dumps(
            {
                "accepted": True,
                "proto-ver-min": JM_VERSION,
                "proto-ver-max": JM_VERSION,
                "features": features,
                "nick": "directory",
            }
        ),
    )


def _offer_line(oid: int = 0) -> str:
    return f"sw0reloffer {oid} 10000 20000 0 0.001"


def _signed_response(
    maker: NickIdentity, recipient: str, commands: str
) -> dict[str, Any]:
    command, data = commands.split(" ", 1)
    signed = maker.sign_message(data, ONION_HOSTID)
    return {
        "type": MessageType.PRIVMSG.value,
        "line": f"{maker.nick}!{recipient}!{command} {signed}",
    }


def _bond_proof(maker_nick: str, taker_nick: str) -> str:
    """Build a real, reference-shaped fidelity-bond proof with fixed keys."""
    cert = NickIdentity(private_key_bytes=CERT_KEY)
    utxo = NickIdentity(private_key_bytes=UTXO_KEY)
    cert_pub = bytes.fromhex(cert.public_key_hex)
    utxo_pub = bytes.fromhex(utxo.public_key_hex)
    cert_expiry = 52
    nick_signature = base64.b64decode(
        cert.sign_bytes(f"{taker_nick}|{maker_nick}".encode())
    )
    cert_message = b"fidelity-bond-cert|" + cert_pub + b"|" + str(cert_expiry).encode()
    cert_signature = base64.b64decode(utxo.sign_bytes(cert_message))
    proof = struct.pack(
        "<72s72s33sH33s32sII",
        nick_signature.rjust(72, b"\xff"),
        cert_signature.rjust(72, b"\xff"),
        cert_pub,
        cert_expiry,
        utxo_pub,
        b"\x11" * 32,
        2,
        900_000,
    )
    return base64.b64encode(proof).decode("ascii")


def _invalid_bond_proof(proof_b64: str) -> str:
    proof = bytearray(base64.b64decode(proof_b64))
    proof[71] ^= 1
    return base64.b64encode(proof).decode("ascii")


def _route(name: str, *, offer: bool = True) -> tool.RouteResult:
    offers = (
        [
            Offer(
                counterparty=VALID_USER_NICK,
                ordertype=OfferType.SW0_RELATIVE,
                oid=0,
                minsize=10_000,
                maxsize=20_000,
                txfee=0,
                cjfee="0.001",
            )
        ]
        if offer
        else []
    )
    return tool.RouteResult(
        name=name,
        connected=True,
        requested=True,
        offers=offers,
        offer_lines=[_offer_line()] if offer else [],
    )


class _QueuedTransport:
    """A fake TCP transport that makes an empty receive queue block until cancelled."""

    def __init__(self, responses: list[bytes | BaseException]) -> None:
        self.responses = deque(responses)
        self.send = AsyncMock()
        self.close = AsyncMock()

    async def receive(self) -> bytes:
        if self.responses:
            response = self.responses.popleft()
            if isinstance(response, BaseException):
                raise response
            return response
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    def is_connected(self) -> bool:
        return True


class _FakePeer:
    def __init__(
        self,
        *,
        nick: str,
        location: str,
        nick_identity: NickIdentity,
        timeout: float,
        on_message: Callable[[str, bytes], Awaitable[None]],
        on_disconnect: Callable[[str], Awaitable[None]],
    ) -> None:
        self.nick = nick
        self.location = location
        self.nick_identity = nick_identity
        self.timeout = timeout
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.peer_features: dict[str, bool] = {"direct": True}
        self.connect_result = True
        self.send_result = True
        self.before_connect: bytes | None = None
        self.after_send: Callable[[_FakePeer], Awaitable[None]] | None = None
        self.sent: list[bytes] = []
        self.disconnected = False
        self.connect_calls: list[tuple[str, str, str]] = []

    async def connect(self, our_nick: str, our_location: str, network: str) -> bool:
        self.connect_calls.append((our_nick, our_location, network))
        if self.before_connect is not None:
            await self.on_message(self.nick, self.before_connect)
        return self.connect_result

    async def send(self, data: bytes) -> bool:
        self.sent.append(data)
        if self.after_send is not None:
            await self.after_send(self)
        return self.send_result

    async def disconnect(self) -> None:
        self.disconnected = True


def _peer_factory(
    configure: Callable[[_FakePeer], None],
) -> tuple[Callable[..., _FakePeer], list[_FakePeer]]:
    peers: list[_FakePeer] = []

    def factory(**kwargs: Any) -> _FakePeer:
        peer = _FakePeer(**kwargs)
        configure(peer)
        peers.append(peer)
        return peer

    return factory, peers


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (DIRECTORY_HOST, f"{DIRECTORY_HOST}:5222"),
        (DIRECTORY_ENDPOINT, DIRECTORY_ENDPOINT),
    ],
)
def test_directory_endpoint_accepts_bare_legacy_default_and_non_default_port(
    value: str, expected: str
) -> None:
    assert tool.directory_endpoint(value) == expected


def test_parser_accepts_confirmed_user_nick_and_cli_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = f"{'c' * 56}.onion:19999"
    report = tmp_path / "diagnosis.json"
    result = tool.DiagnosisResult(
        maker_nick=VALID_USER_NICK,
        taker_nick="J5aaaaaaaaaaaaaa",
        network="signet",
        directory=directory,
        directory_result=_route("directory"),
        direct_result=_route("direct"),
    )
    diagnose = AsyncMock(return_value=result)
    monkeypatch.setattr(tool, "diagnose_maker", diagnose)

    assert (
        tool.main(
            [
                VALID_USER_NICK,
                "--directory",
                directory,
                "--network",
                "signet",
                "--nick-auth-mode",
                NickAuthMode.REQUIRE_VERIFIED.value,
                "--timeout",
                "0.1",
                "--maker-address",
                MAKER_ADDRESS,
                "--output",
                str(report),
            ]
        )
        == 0
    )
    assert diagnose.await_args is not None
    assert diagnose.await_args.kwargs == {
        "directory": directory,
        "network": "signet",
        "timeout": 0.1,
        "nick_auth_mode": NickAuthMode.REQUIRE_VERIFIED,
        "maker_address": MAKER_ADDRESS,
    }
    assert json.loads(report.read_text()) == result.model_dump(mode="json")
    assert tool.main(["invalid-nick"]) == 2


@pytest.mark.asyncio
async def test_diagnose_uses_authenticated_signet_directory_then_direct_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    maker = NickIdentity(private_key_bytes=MAKER_KEY)
    directory_offer = _signed_response(maker, taker.nick, _offer_line(0))
    direct_offer = _signed_response(maker, taker.nick, _offer_line(1))
    challenge = NickAuthChallenge(challenge=CHALLENGE, directory_id=DIRECTORY_ENDPOINT)
    transport = _QueuedTransport(
        [
            _handshake_response(nick_auth=True),
            _envelope(MessageType.NICK_AUTH_CHALLENGE, challenge.to_json()),
            _envelope(
                MessageType.NICK_AUTH_RESULT,
                NickAuthResult(code="ok", verified=True).to_json(),
            ),
            _envelope(
                MessageType.PEERLIST,
                f"{maker.nick};{MAKER_ADDRESS};F:direct,neutrino_compat",
            ),
            TimeoutError(),
            _envelope(MessageType.PRIVMSG, directory_offer["line"]),
            TimeoutError(),
        ]
    )
    connect_via_tor = AsyncMock(return_value=transport)
    monkeypatch.setattr(directory_client, "connect_via_tor", connect_via_tor)
    monkeypatch.setattr(tool, "NickIdentity", lambda: taker)

    def configure(peer: _FakePeer) -> None:
        async def reply_and_disconnect(current: _FakePeer) -> None:
            assert transport.close.await_count == 1
            await current.on_message(
                maker.nick, _envelope(MessageType.PRIVMSG, direct_offer["line"])
            )
            await current.on_disconnect(maker.nick)

        peer.after_send = reply_and_disconnect

    peer_factory, peers = _peer_factory(configure)
    monkeypatch.setattr(tool, "OnionPeer", peer_factory)

    result = await tool.diagnose_maker(
        maker.nick,
        directory=DIRECTORY_ENDPOINT,
        network="signet",
        timeout=0.01,
        nick_auth_mode=NickAuthMode.REQUIRE_VERIFIED,
    )

    assert result.successful
    assert result.directory_nick_authenticated
    assert result.maker_address == MAKER_ADDRESS
    assert result.discovery == f"discovered {MAKER_ADDRESS}"
    assert [offer.oid for offer in result.directory_result.offers] == [0]
    assert [offer.oid for offer in result.direct_result.offers] == [1]
    connect_via_tor.assert_awaited_once_with(
        DIRECTORY_HOST,
        DIRECTORY_PORT,
        "127.0.0.1",
        9050,
        2097152,
        0.01,
        socks_username=None,
        socks_password=None,
    )
    sent = [json.loads(call.args[0]) for call in transport.send.await_args_list]
    handshake = sent[0]
    assert json.loads(handshake["line"])["network"] == "signet"
    proof = NickAuthProof.parse(sent[1]["line"])
    assert verify_nick_auth_proof(
        proof, CHALLENGE, DIRECTORY_ENDPOINT, handshake["line"], taker.nick, JM_VERSION
    )
    assert sent[-1] == {
        "type": MessageType.PUBMSG.value,
        "line": f"{taker.nick}!PUBLIC!orderbook",
    }
    assert peers[0].connect_calls == [(taker.nick, "NOT-SERVING-ONION", "signet")]
    assert json.loads(peers[0].sent[0]) == {
        "type": MessageType.PUBMSG.value,
        "line": f"{taker.nick}!PUBLIC!orderbook",
    }
    assert peers[0].disconnected


@pytest.mark.asyncio
async def test_discovery_uses_complete_peerlist_target_and_features(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = MagicMock()
    monkeypatch.setattr(tool.logger, "info", info)
    client = MagicMock()
    client.get_authoritative_peerlist_snapshot = AsyncMock(
        return_value=PeerlistSnapshot(
            peers=(
                (VALID_USER_NICK, MAKER_ADDRESS, FeatureSet(features={"direct", "x"})),
            )
        )
    )

    discovery, endpoint = await tool._discover_maker_endpoint(
        client, VALID_USER_NICK, 0.1
    )

    assert (discovery, endpoint) == (f"discovered {MAKER_ADDRESS}", MAKER_ADDRESS)
    info.assert_any_call("Discovered maker endpoint: {}", MAKER_ADDRESS)
    info.assert_any_call("Discovered maker features: {}", "direct, x")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "expected_discovery"),
    [
        (None, "unknown (directory did not provide a complete peerlist snapshot)"),
        (
            PeerlistSnapshot(
                peers=(("J5aaaaaaaaaaaaaa", MAKER_ADDRESS, FeatureSet()),)
            ),
            "complete peerlist snapshot does not contain the maker",
        ),
        (
            PeerlistSnapshot(
                peers=((VALID_USER_NICK, "NOT-SERVING-ONION", FeatureSet()),)
            ),
            "maker is not serving an onion endpoint",
        ),
    ],
)
async def test_discovery_treats_absent_or_unavailable_peerlist_as_non_authoritative(
    snapshot: PeerlistSnapshot | None, expected_discovery: str
) -> None:
    client = MagicMock()
    client.get_authoritative_peerlist_snapshot = AsyncMock(return_value=snapshot)

    discovery, endpoint = await tool._discover_maker_endpoint(
        client, VALID_USER_NICK, 0.1
    )

    assert discovery == expected_discovery
    assert endpoint is None


@pytest.mark.asyncio
async def test_record_response_collects_bundled_offers_and_real_bond() -> None:
    maker = NickIdentity(private_key_bytes=MAKER_KEY)
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    proof = _bond_proof(maker.nick, taker.nick)
    result = tool.RouteResult(name="directory", connected=True, requested=True)

    stop = tool.record_response(
        _signed_response(
            maker, taker.nick, f"{_offer_line(0)}!tbond {proof}!{_offer_line(1)}"
        ),
        maker.nick,
        taker.nick,
        result,
    )

    assert not stop
    assert [offer.oid for offer in result.offers] == [0, 1]
    analysis = result.bonds[proof]
    assert analysis["nick_signature"]["valid"] is True
    assert analysis["cert_signature"]["valid"] is True
    assert analysis["cert_signature"]["message_format"] == "binary"
    assert result.successful
    assert tool._route_bonds_are_valid(result)


def test_bond_analysis_marks_invalid_signatures_incomplete_but_no_bond_succeeds() -> (
    None
):
    maker = NickIdentity(private_key_bytes=MAKER_KEY)
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    no_bond = tool.RouteResult(name="directory", connected=True, requested=True)
    invalid_bond = tool.RouteResult(name="direct", connected=True, requested=True)
    missing_bond_proof = tool.RouteResult(
        name="missing-bond", connected=True, requested=True
    )
    assert not tool.record_response(
        _signed_response(maker, taker.nick, _offer_line()),
        maker.nick,
        taker.nick,
        no_bond,
    )
    proof = _invalid_bond_proof(_bond_proof(maker.nick, taker.nick))
    assert not tool.record_response(
        _signed_response(maker, taker.nick, f"{_offer_line()}!tbond {proof}"),
        maker.nick,
        taker.nick,
        invalid_bond,
    )
    assert not tool.record_response(
        _signed_response(maker, taker.nick, f"{_offer_line()}!tbond"),
        maker.nick,
        taker.nick,
        missing_bond_proof,
    )

    assert no_bond.successful
    assert tool._route_bonds_are_valid(no_bond)
    assert invalid_bond.successful
    assert not tool._route_bonds_are_valid(invalid_bond)
    assert not missing_bond_proof.successful
    diagnosis = tool.DiagnosisResult(
        maker_nick=maker.nick,
        taker_nick=taker.nick,
        network="signet",
        directory=DIRECTORY_ENDPOINT,
        directory_result=no_bond,
        direct_result=invalid_bond,
    )
    assert not diagnosis.successful


def test_record_response_requires_exact_signed_sender_and_recipient() -> None:
    maker = NickIdentity(private_key_bytes=MAKER_KEY)
    other = NickIdentity(private_key_bytes=b"\x05" * 32)
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    result = tool.RouteResult(name="directory")
    valid = _signed_response(maker, taker.nick, _offer_line())
    wrong_recipient = dict(valid)
    wrong_recipient["line"] = wrong_recipient["line"].replace(taker.nick, other.nick, 1)
    invalid_signature = dict(valid)
    invalid_signature["line"] = invalid_signature["line"].rsplit(" ", 1)[0] + " broken"

    assert not tool.record_response(
        _signed_response(other, taker.nick, _offer_line()),
        maker.nick,
        taker.nick,
        result,
    )
    assert not tool.record_response(wrong_recipient, maker.nick, taker.nick, result)
    assert not tool.record_response(invalid_signature, maker.nick, taker.nick, result)
    assert result.offers == []
    assert result.errors == ["Maker response has an invalid message signature"]


def test_malformed_unsigned_direct_offer_explains_both_defects() -> None:
    maker = NickIdentity(private_key_bytes=MAKER_KEY)
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    route = tool.RouteResult(name="direct", connected=True, requested=True)
    content = "sw0absoffer0 10000 20000 0 0"
    logs: list[str] = []
    sink = tool.logger.add(logs.append, level="DEBUG", format="{message}")
    try:
        tool.record_response(
            {
                "type": MessageType.PRIVMSG.value,
                "line": f"{maker.nick}!{taker.nick}!{content}",
            },
            maker.nick,
            taker.nick,
            route,
        )
    finally:
        tool.logger.remove(sink)

    assert not route.successful
    assert not route.offers
    assert any("missing space after sw0absoffer" in error for error in route.errors)
    assert any("unsigned" in error for error in route.errors)
    assert any(content in message for message in logs)


def test_log_level_defaults_to_debug_and_can_be_reduced() -> None:
    parser = tool.build_parser()
    assert parser.parse_args([VALID_USER_NICK]).log_level == "DEBUG"
    assert (
        parser.parse_args([VALID_USER_NICK, "--log-level", "INFO"]).log_level == "INFO"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("peerlist_state", "expected_discovery"),
    [
        ("unknown", "unknown (directory did not provide a complete peerlist snapshot)"),
        ("absent", "complete peerlist snapshot does not contain the maker"),
        ("not-serving", "maker is not serving an onion endpoint"),
    ],
)
async def test_unknown_or_unavailable_peerlist_is_not_offline_and_directory_offer_remains(
    monkeypatch: pytest.MonkeyPatch,
    peerlist_state: str,
    expected_discovery: str,
) -> None:
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    maker = NickIdentity(private_key_bytes=MAKER_KEY)
    offer = _signed_response(maker, taker.nick, _offer_line())
    snapshot = (
        None
        if peerlist_state == "unknown"
        else PeerlistSnapshot(
            peers=(
                (
                    "J5aaaaaaaaaaaaaa" if peerlist_state == "absent" else maker.nick,
                    MAKER_ADDRESS
                    if peerlist_state == "absent"
                    else "NOT-SERVING-ONION",
                    FeatureSet(),
                ),
            )
        )
    )

    class DirectoryWithoutPeerlist:
        directory_nick_authenticated = False

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.delivered = False

        async def connect(self) -> None:
            pass

        async def get_authoritative_peerlist_snapshot(self) -> PeerlistSnapshot | None:
            return snapshot

        async def listen_for_messages(self, duration: float) -> list[dict[str, Any]]:
            if duration == 0:
                return []
            if not self.delivered:
                self.delivered = True
                return [offer]
            await asyncio.sleep(duration)
            return []

        async def send_public_message(self, _message: str) -> None:
            pass

        async def close(self) -> None:
            pass

    monkeypatch.setattr(tool, "DirectoryClient", DirectoryWithoutPeerlist)
    monkeypatch.setattr(tool, "NickIdentity", lambda: taker)

    result = await tool.diagnose_maker(
        maker.nick, directory=DIRECTORY_ENDPOINT, timeout=0.001
    )

    assert result.directory_result.offers[0].counterparty == maker.nick
    assert result.discovery == expected_discovery
    assert "offline" not in result.discovery
    assert result.direct_result.connected is False


@pytest.mark.asyncio
async def test_override_runs_direct_route_after_directory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    maker = NickIdentity(private_key_bytes=MAKER_KEY)
    direct_offer = _signed_response(maker, taker.nick, _offer_line())

    class FailingDirectory:
        directory_nick_authenticated = False

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.closed = False

        async def connect(self) -> None:
            raise RuntimeError("connection refused")

        async def close(self) -> None:
            self.closed = True

    def configure(peer: _FakePeer) -> None:
        async def reply_and_disconnect(current: _FakePeer) -> None:
            await current.on_message(
                maker.nick, _envelope(MessageType.PRIVMSG, direct_offer["line"])
            )
            await current.on_disconnect(maker.nick)

        peer.after_send = reply_and_disconnect

    peer_factory, peers = _peer_factory(configure)
    monkeypatch.setattr(tool, "DirectoryClient", FailingDirectory)
    monkeypatch.setattr(tool, "OnionPeer", peer_factory)
    monkeypatch.setattr(tool, "NickIdentity", lambda: taker)

    result = await tool.diagnose_maker(
        maker.nick,
        directory=DIRECTORY_ENDPOINT,
        maker_address=MAKER_ADDRESS,
        timeout=0.01,
    )

    assert result.maker_address == MAKER_ADDRESS
    assert result.directory_result.errors
    assert result.direct_result.connected
    assert result.direct_result.offers[0].oid == 0
    assert peers[0].disconnected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connect_result", "send_result"), [(False, True), (True, False)]
)
async def test_direct_route_failures_disconnect_cleanly(
    monkeypatch: pytest.MonkeyPatch, connect_result: bool, send_result: bool
) -> None:
    identity = NickIdentity(private_key_bytes=TAKER_KEY)
    result = tool.RouteResult(name="direct")

    def configure(peer: _FakePeer) -> None:
        peer.connect_result = connect_result
        peer.send_result = send_result

    peer_factory, peers = _peer_factory(configure)
    monkeypatch.setattr(tool, "OnionPeer", peer_factory)

    await tool._run_direct_phase(
        VALID_USER_NICK, identity, MAKER_ADDRESS, "signet", 0.01, result
    )

    assert peers[0].disconnected
    assert result.errors
    if connect_result:
        assert result.connected and result.requested
    else:
        assert not result.connected and not result.requested


@pytest.mark.asyncio
async def test_direct_callback_before_request_is_ignored_and_disconnect_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = NickIdentity(private_key_bytes=TAKER_KEY)
    result = tool.RouteResult(name="direct")
    premature = _envelope(MessageType.PRIVMSG, "not-json-before-request")

    def configure(peer: _FakePeer) -> None:
        peer.before_connect = premature

        async def disconnect_without_offer(current: _FakePeer) -> None:
            await current.on_disconnect(VALID_USER_NICK)

        peer.after_send = disconnect_without_offer

    peer_factory, peers = _peer_factory(configure)
    monkeypatch.setattr(tool, "OnionPeer", peer_factory)

    await tool._run_direct_phase(
        VALID_USER_NICK, identity, MAKER_ADDRESS, "mainnet", 0.01, result
    )

    assert result.messages_received == 0
    assert any("disconnected before a valid offer" in error for error in result.errors)
    assert peers[0].disconnected


@pytest.mark.asyncio
async def test_direct_timeout_disconnects_without_leaking_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = NickIdentity(private_key_bytes=TAKER_KEY)
    result = tool.RouteResult(name="direct")
    peer_factory, peers = _peer_factory(lambda _peer: None)
    monkeypatch.setattr(tool, "OnionPeer", peer_factory)

    await tool._run_direct_phase(
        VALID_USER_NICK, identity, MAKER_ADDRESS, "mainnet", 0.001, result
    )

    assert result.connected and result.requested
    assert not result.errors
    assert peers[0].disconnected


@pytest.mark.asyncio
@pytest.mark.parametrize("include_requested_maker", [False, True])
async def test_direct_route_warns_once_for_verified_alternate_nick(
    monkeypatch: pytest.MonkeyPatch,
    include_requested_maker: bool,
) -> None:
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    requested_maker = NickIdentity(private_key_bytes=MAKER_KEY)
    alternate_maker = NickIdentity(private_key_bytes=b"\x05" * 32)
    response = _signed_response(alternate_maker, taker.nick, _offer_line())
    result = tool.RouteResult(name="direct")

    def configure(peer: _FakePeer) -> None:
        async def reply(current: _FakePeer) -> None:
            for _ in range(2):
                await current.on_message(
                    alternate_maker.nick, json.dumps(response).encode()
                )
            if include_requested_maker:
                expected = _signed_response(requested_maker, taker.nick, _offer_line(1))
                await current.on_message(
                    requested_maker.nick, json.dumps(expected).encode()
                )
            await current.on_disconnect(requested_maker.nick)

        peer.after_send = reply

    factory, peers = _peer_factory(configure)
    monkeypatch.setattr(tool, "OnionPeer", factory)
    logs: list[str] = []
    sink = tool.logger.add(logs.append, level="WARNING", format="{message}")
    try:
        await tool._run_direct_phase(
            requested_maker.nick,
            taker,
            MAKER_ADDRESS,
            "signet",
            0.01,
            result,
        )
    finally:
        tool.logger.remove(sink)

    assert len(result.warnings) == 1
    assert requested_maker.nick in result.warnings[0]
    assert f"retry with {alternate_maker.nick}" in result.warnings[0]
    assert sum("valid signed response" in line for line in logs) == 1
    assert [offer.oid for offer in result.offers] == (
        [1] if include_requested_maker else []
    )
    assert result.successful is include_requested_maker
    assert result.model_dump()["warnings"] == result.warnings
    assert peers[0].disconnected


@pytest.mark.parametrize("invalid_case", ["signature", "recipient", "public"])
def test_untrusted_or_unrelated_message_does_not_trigger_stale_nick_warning(
    invalid_case: str,
) -> None:
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    sender = NickIdentity(private_key_bytes=MAKER_KEY)
    result = tool.RouteResult(name="direct")
    message = _signed_response(sender, taker.nick, _offer_line())
    if invalid_case == "signature":
        message["line"] = message["line"].rsplit(" ", 1)[0] + " invalid"
    elif invalid_case == "recipient":
        message["line"] = message["line"].replace(taker.nick, VALID_USER_NICK, 1)
    else:
        message["type"] = MessageType.PUBMSG.value

    tool._warn_unexpected_direct_nick(message, VALID_USER_NICK, taker.nick, result)

    assert result.warnings == []
    assert result.offers == []


@pytest.mark.asyncio
async def test_real_direct_client_receives_signed_offer_and_logs_wire_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(private_key_bytes=TAKER_KEY)
    maker = NickIdentity(private_key_bytes=MAKER_KEY)
    response = json.dumps(_signed_response(maker, taker.nick, _offer_line())).encode()
    transport = _QueuedTransport(
        [
            _envelope(
                MessageType.HANDSHAKE,
                json.dumps(
                    {
                        "app-name": "joinmarket",
                        "directory": False,
                        "proto-ver": JM_VERSION,
                        "network": "signet",
                        "nick": maker.nick,
                    }
                ),
            ),
        ]
    )

    async def send(data: bytes) -> None:
        if json.loads(data)["type"] == MessageType.PUBMSG.value:
            transport.responses.append(response)

    transport.send = AsyncMock(side_effect=send)
    connector = AsyncMock(return_value=transport)
    monkeypatch.setattr(network, "connect_via_tor", connector)
    peers: list[network.OnionPeer] = []

    def peer_factory(**kwargs: Any) -> network.OnionPeer:
        peer = network.OnionPeer(**kwargs)
        peers.append(peer)
        return peer

    monkeypatch.setattr(tool, "OnionPeer", peer_factory)
    result = tool.RouteResult(name="direct")
    logs: list[str] = []
    sink = tool.logger.add(logs.append, level="DEBUG", format="{message}")
    try:
        await tool._run_direct_phase(
            maker.nick, taker, MAKER_ADDRESS, "signet", 0.01, result
        )
    finally:
        tool.logger.remove(sink)

    assert result.successful
    assert result.offers[0].oid == 0
    assert transport.send.await_count == 2
    request = transport.send.await_args_list[1].args[0]
    assert json.loads(request) == {
        "type": MessageType.PUBMSG.value,
        "line": f"{taker.nick}!PUBLIC!orderbook",
    }
    assert any(f"[direct] SEND {request!r}" in message for message in logs)
    assert any(f"[direct] RECV {response!r}" in message for message in logs)
    assert not peers[0].is_connected()
    assert peers[0]._receive_task is None
    transport.close.assert_awaited_once()


def test_json_output_uses_exit_status_for_incomplete_diagnosis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "incomplete.json"
    result = tool.DiagnosisResult(
        maker_nick=VALID_USER_NICK,
        taker_nick="J5aaaaaaaaaaaaaa",
        network="mainnet",
        directory=DIRECTORY_ENDPOINT,
        directory_result=tool.RouteResult(name="directory"),
        direct_result=tool.RouteResult(name="direct"),
    )
    monkeypatch.setattr(tool, "diagnose_maker", AsyncMock(return_value=result))

    assert tool.main([VALID_USER_NICK, "--output", str(report)]) == 1
    saved = json.loads(report.read_text())
    assert saved["maker_nick"] == VALID_USER_NICK
    assert saved["directory_result"]["connected"] is False
