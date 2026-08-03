from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest
from jmcore.crypto import NickIdentity
from jmcore.models import MessageEnvelope, NetworkType, PeerInfo, PeerStatus
from jmcore.nick_auth import (
    NickAuthChallenge,
    NickAuthMode,
    NickAuthProof,
    NickAuthResult,
    create_nick_auth_proof,
)
from jmcore.protocol import FEATURE_NICK_AUTH, JM_VERSION, MessageType
from jmcore.settings import DirectoryServerSettings

from directory_server.handshake_handler import HandshakeHandler
from directory_server.server import DirectoryServer

DIRECTORY_ID = "test:directory"


class ScriptedConnection:
    def __init__(
        self,
        received: list[bytes],
        proof_factory: Callable[[NickAuthChallenge], NickAuthProof] | None = None,
    ) -> None:
        self.received = received
        self.proof_factory = proof_factory
        self.sent: list[bytes] = []
        self.closed = False

    async def receive(self) -> bytes:
        return self.received.pop(0)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)
        envelope = MessageEnvelope.from_bytes(data)
        if envelope.message_type == MessageType.NICK_AUTH and self.proof_factory is not None:
            challenge = NickAuthChallenge.parse(envelope.payload)
            proof = self.proof_factory(challenge)
            self.received.append(
                MessageEnvelope(
                    message_type=MessageType.NICK_AUTH,
                    payload=proof.to_json(),
                ).to_bytes()
            )

    async def close(self) -> None:
        self.closed = True

    def is_connected(self) -> bool:
        return not self.closed


class BlockingConnection(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__([])
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send(self, data: bytes) -> None:
        self.send_started.set()
        await self.release_send.wait()
        self.sent.append(data)


class ResultFailingConnection(ScriptedConnection):
    async def send(self, data: bytes) -> None:
        envelope = MessageEnvelope.from_bytes(data)
        if envelope.message_type == MessageType.NICK_AUTH_RESULT:
            raise ConnectionError("result send failed")
        await super().send(data)


def _server(mode: NickAuthMode = NickAuthMode.PREFER_VERIFIED) -> DirectoryServer:
    settings = DirectoryServerSettings(host="127.0.0.1", port=0, max_peers=10)
    server = DirectoryServer(settings, NetworkType.MAINNET, "J5DirectoryOOOO")
    server.handshake_handler = HandshakeHandler(
        network=NetworkType.MAINNET,
        server_nick=server.server_nick,
        motd="test",
        nick_auth_mode=mode,
        nick_auth_directory_id=DIRECTORY_ID,
    )
    server.nick_auth_timeout = 1.0
    return server


def _handshake_line(
    identity: NickIdentity,
    *,
    advertise_auth: bool,
    location: str = "NOT-SERVING-ONION",
) -> str:
    return json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": location,
            "proto-ver": JM_VERSION,
            "features": {FEATURE_NICK_AUTH: True} if advertise_auth else {},
            "nick": identity.nick,
            "network": "mainnet",
        }
    )


def _handshake_envelope(line: str) -> bytes:
    return MessageEnvelope(message_type=MessageType.HANDSHAKE, payload=line).to_bytes()


def _sent_envelopes(connection: ScriptedConnection) -> list[MessageEnvelope]:
    return [MessageEnvelope.from_bytes(data) for data in connection.sent]


@pytest.mark.anyio
async def test_legacy_handshake_reserves_before_acceptance() -> None:
    server = _server()
    identity = NickIdentity()
    line = _handshake_line(identity, advertise_auth=False)
    connection = ScriptedConnection([_handshake_envelope(line)])

    peer_key = await server._perform_handshake(connection, "legacy-connection")  # type: ignore[arg-type]

    assert peer_key == identity.nick
    response = json.loads(_sent_envelopes(connection)[0].payload)
    assert response["accepted"] is True
    assert server.peer_registry.get_connection_id(peer_key) == "legacy-connection"
    assert server.peer_registry.get_by_key(peer_key).status is PeerStatus.HANDSHAKED


@pytest.mark.anyio
async def test_legacy_duplicate_gets_ordinary_rejection() -> None:
    server = _server()
    identity = NickIdentity()
    line = _handshake_line(identity, advertise_auth=False)
    existing = PeerInfo(
        nick=identity.nick,
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    server.peer_registry.register(existing, "first")
    connection = ScriptedConnection([_handshake_envelope(line)])

    peer_key = await server._perform_handshake(connection, "second")  # type: ignore[arg-type]

    assert peer_key is None
    response = json.loads(_sent_envelopes(connection)[0].payload)
    assert response["accepted"] is False
    assert server.peer_registry.get_connection_id(identity.nick) == "first"


@pytest.mark.anyio
async def test_valid_nick_auth_replaces_legacy_owner_and_clears_offers() -> None:
    server = _server()
    identity = NickIdentity()
    line = _handshake_line(identity, advertise_auth=True)

    def create_proof(challenge: NickAuthChallenge) -> NickAuthProof:
        return create_nick_auth_proof(identity, challenge.challenge, DIRECTORY_ID, line)

    old_peer = PeerInfo(
        nick=identity.nick,
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    peer_key = server.peer_registry.register(old_peer, "old").peer_key
    old_connection = ScriptedConnection([])
    server.connections.add("old", old_connection)  # type: ignore[arg-type]
    server.peer_key_to_conn_id[peer_key] = "old"
    server.message_router._peer_offers[(peer_key, "old")] = {"0"}
    server.heartbeat._pong_pending.add((peer_key, "old"))
    observer = PeerInfo(
        nick="observer",
        onion_address="a" * 56 + ".onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    observer_key = server.peer_registry.register(observer, "observer").peer_key
    observer_connection = ScriptedConnection([])
    server.connections.add("observer", observer_connection)  # type: ignore[arg-type]
    server.peer_key_to_conn_id[observer_key] = "observer"
    connection = ScriptedConnection([_handshake_envelope(line)], create_proof)

    result_key = await server._perform_handshake(connection, "verified")  # type: ignore[arg-type]

    envelopes = _sent_envelopes(connection)
    assert [envelope.message_type for envelope in envelopes] == [
        MessageType.DN_HANDSHAKE,
        MessageType.NICK_AUTH,
        MessageType.NICK_AUTH_RESULT,
    ]
    result = NickAuthResult.parse(envelopes[-1].payload)
    assert result.verified is True
    assert result_key == peer_key
    assert server.peer_registry.get_connection_id(peer_key) == "verified"
    assert server.peer_registry.get_owner(peer_key).verified_pubkey == identity.public_key_hex
    assert server.peer_registry.get_by_key(peer_key).status is PeerStatus.HANDSHAKED
    assert old_connection.closed is True
    assert server.message_router.get_offer_stats()["total_offers"] == 0
    assert (peer_key, "old") not in server.heartbeat.pong_pending
    disconnects = [MessageEnvelope.from_bytes(data) for data in observer_connection.sent]
    assert len(disconnects) == 1
    assert disconnects[0].message_type == MessageType.PEERLIST
    assert disconnects[0].payload == f"{identity.nick};NOT-SERVING-ONION;D"


@pytest.mark.anyio
async def test_verified_maker_is_not_blocked_by_legacy_location_squatter() -> None:
    server = _server()
    mallory = NickIdentity()
    bob = NickIdentity()
    bob_location = f"{'b' * 56}.onion:5222"
    mallory_line = _handshake_line(
        mallory,
        advertise_auth=False,
        location=bob_location,
    )
    bob_line = _handshake_line(
        bob,
        advertise_auth=True,
        location=bob_location,
    )

    def create_bob_proof(challenge: NickAuthChallenge) -> NickAuthProof:
        return create_nick_auth_proof(bob, challenge.challenge, DIRECTORY_ID, bob_line)

    mallory_connection = ScriptedConnection([_handshake_envelope(mallory_line)])
    bob_connection = ScriptedConnection([_handshake_envelope(bob_line)], create_bob_proof)

    mallory_key = await server._perform_handshake(  # type: ignore[arg-type]
        mallory_connection,
        "mallory-connection",
    )
    bob_key = await server._perform_handshake(  # type: ignore[arg-type]
        bob_connection,
        "bob-connection",
    )

    assert mallory_key == mallory.nick
    assert bob_key == bob.nick
    assert server.peer_registry.count() == 2
    assert server.peer_registry.get_connection_id(mallory.nick) == "mallory-connection"
    assert server.peer_registry.get_connection_id(bob.nick) == "bob-connection"
    mallory_peer = server.peer_registry.get_by_nick(mallory.nick)
    bob_peer = server.peer_registry.get_by_nick(bob.nick)
    assert mallory_peer is not None
    assert bob_peer is not None
    assert mallory_peer.location_string == bob_location
    assert bob_peer.location_string == bob_location
    result = NickAuthResult.parse(_sent_envelopes(bob_connection)[-1].payload)
    assert result.code == "ok"
    assert result.verified is True


@pytest.mark.anyio
async def test_invalid_nick_auth_proof_has_no_legacy_fallback() -> None:
    server = _server()
    identity = NickIdentity()
    line = _handshake_line(identity, advertise_auth=True)

    def create_invalid_proof(challenge: NickAuthChallenge) -> NickAuthProof:
        return create_nick_auth_proof(
            identity,
            challenge.challenge,
            DIRECTORY_ID,
            f"{line} ",
        )

    connection = ScriptedConnection([_handshake_envelope(line)], create_invalid_proof)

    peer_key = await server._perform_handshake(connection, "invalid")  # type: ignore[arg-type]

    envelopes = _sent_envelopes(connection)
    result = NickAuthResult.parse(envelopes[-1].payload)
    assert peer_key is None
    assert result.code == "invalid"
    assert result.verified is False
    assert server.peer_registry.get_by_nick(identity.nick) is None


@pytest.mark.anyio
async def test_duplicate_outer_proof_keys_are_malformed() -> None:
    server = _server()
    identity = NickIdentity()
    line = _handshake_line(identity, advertise_auth=True)

    def create_proof(challenge: NickAuthChallenge) -> NickAuthProof:
        return create_nick_auth_proof(identity, challenge.challenge, DIRECTORY_ID, line)

    connection = ScriptedConnection([_handshake_envelope(line)], create_proof)
    original_send = connection.send

    async def duplicate_proof_send(data: bytes) -> None:
        await original_send(data)
        envelope = MessageEnvelope.from_bytes(data)
        if envelope.message_type == MessageType.NICK_AUTH:
            proof_envelope = MessageEnvelope.from_bytes(connection.received.pop())
            connection.received.append(
                (
                    '{"type":803,"type":803,"line":' + json.dumps(proof_envelope.payload) + "}"
                ).encode()
            )

    connection.send = duplicate_proof_send  # type: ignore[method-assign]

    peer_key = await server._perform_handshake(connection, "duplicate-proof")  # type: ignore[arg-type]

    result = NickAuthResult.parse(_sent_envelopes(connection)[-1].payload)
    assert peer_key is None
    assert result.code == "malformed"
    assert server.peer_registry.get_by_nick(identity.nick) is None


@pytest.mark.anyio
async def test_failed_success_result_rolls_back_pending_registration() -> None:
    server = _server()
    identity = NickIdentity()
    line = _handshake_line(identity, advertise_auth=True)

    def create_proof(challenge: NickAuthChallenge) -> NickAuthProof:
        return create_nick_auth_proof(identity, challenge.challenge, DIRECTORY_ID, line)

    connection = ResultFailingConnection([_handshake_envelope(line)], create_proof)

    peer_key = await server._perform_handshake(connection, "failed-result")  # type: ignore[arg-type]

    assert peer_key is None
    assert server.peer_registry.get_by_nick(identity.nick) is None
    assert identity.nick not in server.peer_key_to_conn_id


@pytest.mark.anyio
async def test_replacement_waits_for_inflight_write_to_old_owner() -> None:
    server = _server()
    identity = NickIdentity()
    old_peer = PeerInfo(
        nick=identity.nick,
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    peer_key = server.peer_registry.register(
        old_peer,
        "old",
        verified_pubkey=identity.public_key_hex,
    ).peer_key
    old_connection = BlockingConnection()
    server.connections.add("old", old_connection)  # type: ignore[arg-type]
    server.peer_key_to_conn_id[peer_key] = "old"

    send_task = asyncio.create_task(server._send_to_peer(peer_key, b"message", "old"))
    await old_connection.send_started.wait()
    replacement = old_peer.model_copy(deep=True)
    registration_task = asyncio.create_task(
        server._register_peer(
            replacement,
            "new",
            verified_pubkey=identity.public_key_hex,
        )
    )
    await asyncio.sleep(0)

    assert not registration_task.done()
    assert server.peer_registry.get_connection_id(peer_key) == "old"

    old_connection.release_send.set()
    await send_task
    await registration_task

    assert old_connection.sent == [b"message"]
    assert server.peer_registry.get_connection_id(peer_key) == "new"


@pytest.mark.anyio
async def test_require_mode_rejects_nonadvertising_client() -> None:
    server = _server(NickAuthMode.REQUIRE_VERIFIED)
    identity = NickIdentity()
    line = _handshake_line(identity, advertise_auth=False)
    connection = ScriptedConnection([_handshake_envelope(line)])

    peer_key = await server._perform_handshake(connection, "legacy")  # type: ignore[arg-type]

    envelopes = _sent_envelopes(connection)
    response = json.loads(envelopes[0].payload)
    assert peer_key is None
    assert len(envelopes) == 1
    assert envelopes[0].message_type == MessageType.DN_HANDSHAKE
    assert response["accepted"] is False
    assert server.peer_registry.count() == 0
