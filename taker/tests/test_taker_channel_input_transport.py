"""Encrypted taker channel-input transport boundary tests."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from jmcore.channel_input import (
    MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputCancelAckPayload,
    ChannelInputCancelPayload,
    ChannelInputHelloPayload,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputPayload,
    ChannelInputPreparedPayload,
    ChannelInputPreparePayload,
    ChannelInputRecoveryAcceptPayload,
    ChannelInputRecoveryConfirmPayload,
    ChannelInputRecoveryInitPayload,
    ChannelInputTranscript,
    EndpointTerms,
    approval_set_hash,
    cancel_payload_hash,
    decode_channel_input_message,
    encode_channel_input_message,
    payload_hash,
    sign_payload,
    transcript_hash,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.encryption import CryptoSession
from jmswap.channel_input import InitializedChannelInputBackend
from test_channel_input_negotiation import (
    _MAKER_SECRETS,
    _approval,
    _candidate,
    _freeze_remote,
    _frozen_snapshot,
    _new,
    _node,
    _plan,
)
from test_taker_channel_input_registry import FakeBackend, _add_freeze, _config

from taker.channel_input_negotiation import (
    TakerChannelInputNegotiation,
    TakerChannelInputStage,
)
from taker.channel_input_runtime import TakerChannelInputRegistry
from taker.channel_input_store import TakerChannelInputStore
from taker.channel_input_transport import (
    TakerChannelInputTransport,
    TakerChannelInputTransportError,
)
from taker.coinjoin_session import CoinJoinSession
from taker.multi_directory import ReplyRoute


class WireCrypto:
    """Faithful reversible stand-in for a live authenticated crypto session."""

    is_encrypted = True

    def __init__(self) -> None:
        self.decrypted: list[str] = []

    def encrypt(self, plaintext: str) -> str:
        return base64.b64encode(plaintext.encode("ascii")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        self.decrypted.append(ciphertext)
        return base64.b64decode(ciphertext).decode("ascii")


class FakeDirectory:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []
        self.routed: list[tuple[str, str, str, ReplyRoute]] = []
        self.responses: dict[str, dict[str, object]] = {}
        self.response_batches: list[dict[str, dict[str, object]]] = []
        self.response_factories: list[Callable[[], dict[str, dict[str, object]]]] = []
        self.events: list[str] = []
        self.cryptos: dict[str, WireCrypto] = {}
        self.outbound_payloads: list[tuple[str, ChannelInputPayload]] = []
        self.wait_arguments: list[dict[str, object]] = []

    async def send_privmsg(
        self, recipient: str, command: str, data: str, *, force_channel: str
    ) -> str:
        self.sent.append((recipient, command, data, force_channel))
        self._record_payload(recipient, data)
        return force_channel

    async def send_privmsg_via_route(
        self, recipient: str, command: str, data: str, route: ReplyRoute
    ) -> str:
        self.routed.append((recipient, command, data, route))
        self._record_payload(recipient, data)
        return route.channel_id

    async def wait_for_responses(self, **_kwargs: object) -> dict[str, dict[str, object]]:
        self.wait_arguments.append(dict(_kwargs))
        if self.response_batches:
            return self.response_batches.pop(0)
        if self.response_factories:
            return self.response_factories.pop(0)()
        return self.responses

    def _record_payload(self, nick: str, ciphertext: str) -> None:
        crypto = self.cryptos.get(nick)
        if crypto is None:
            return
        payload = decode_channel_input_message(crypto.decrypt(ciphertext))
        self.outbound_payloads.append((nick, payload))
        self.events.append(f"send:{payload.type}:{nick}")


class FakeRegistry:
    def __init__(self, negotiation: object) -> None:
        self.negotiation = negotiation
        self.accepted: list[tuple[str, ChannelInputHelloPayload]] = []

    def get(self, _session_id: str, _attempt: int) -> object:
        return self.negotiation


class RecoveryDirectory:
    """Recovery peer that proves fresh NaCl ownership before returning a payload."""

    def __init__(self, maker_secret: bytes, route: ReplyRoute) -> None:
        self._maker_secret = maker_secret
        self._maker_key = ChannelInputKeyPair.from_secret(maker_secret)
        self._route = route
        self._crypto: CryptoSession | None = None
        self._accept: ChannelInputRecoveryAcceptPayload | None = None
        self._responses: list[dict[str, dict[str, object]]] = []
        self.commands: list[str] = []

    async def send_privmsg_via_route(
        self, recipient: str, command: str, data: str, route: ReplyRoute
    ) -> str:
        assert recipient == "maker-a"
        assert route == self._route
        self.commands.append(command)
        if self._accept is None:
            init = TakerChannelInputTransport._decode_recovery_token(data)
            assert isinstance(init, ChannelInputRecoveryInitPayload)
            crypto = CryptoSession()
            crypto.setup_encryption(init.ephemeral_pubkey)
            accept = sign_payload(
                ChannelInputRecoveryAcceptPayload(
                    session_id=init.session_id,
                    attempt=init.attempt,
                    signer_key=self._maker_key.public_key,
                    recovery_id=init.recovery_id,
                    init_hash=payload_hash(init).hex(),
                    ephemeral_pubkey=crypto.get_pubkey_hex(),
                ),
                self._maker_secret,
            )
            self._crypto = crypto
            self._accept = accept
            token = TakerChannelInputTransport._encode_recovery_token(accept)
            self._responses.append({"maker-a": {"data": f"{token} key sig", "route": route}})
            return route.channel_id
        assert self._crypto is not None
        recovery_id, ciphertext = data.split()
        inbound = decode_channel_input_message(self._crypto.decrypt(ciphertext))
        response: ChannelInputPayload
        if isinstance(inbound, ChannelInputRecoveryConfirmPayload):
            assert self._accept is not None
            assert inbound.accept_hash == payload_hash(self._accept).hex()
            response = self._accept
        else:
            response = sign_payload(
                ChannelInputPreparedPayload(
                    session_id=inbound.session_id,
                    attempt=inbound.attempt,
                    signer_key=self._maker_key.public_key,
                    transcript_hash="01" * 32,
                    funding_outpoint=_freeze_remote(0).funding_outpoint,
                    public_nonce="02" * 66,
                ),
                self._maker_secret,
            )
        encrypted = self._crypto.encrypt(encode_channel_input_message(response))
        self._responses.append(
            {"maker-a": {"data": f"{recovery_id} {encrypted} key sig", "route": route}}
        )
        return route.channel_id

    async def wait_for_responses(self, **_kwargs: object) -> dict[str, dict[str, object]]:
        return self._responses.pop(0)


def _transport(
    negotiation: object,
    maker_nicks: tuple[str, ...],
    *,
    registry: object | None = None,
    backend: object | None = None,
    initialized_backend: object | None = None,
    config: ChannelInputConfig | None = None,
    current_time: int = 1,
    current_height: int = 1,
) -> tuple[TakerChannelInputTransport, FakeDirectory, dict[str, WireCrypto]]:
    cryptos = {nick: WireCrypto() for nick in maker_nicks}
    directory = FakeDirectory()
    directory.cryptos = cryptos
    session = SimpleNamespace(
        maker_sessions={
            nick: SimpleNamespace(crypto=cryptos[nick], comm_channel=f"directory:{nick}")
            for nick in maker_nicks
        },
        directory_client=directory,
    )
    return (
        TakerChannelInputTransport(
            session=cast(CoinJoinSession, session),
            registry=cast(TakerChannelInputRegistry, registry or FakeRegistry(negotiation)),
            initialized_backend=cast(
                InitializedChannelInputBackend,
                initialized_backend or SimpleNamespace(backend=backend or FakeBackend()),
            ),
            config=config or ChannelInputConfig(),
            settlement_script_allocator=lambda _nick: "5120" + "01" * 32,
            current_time=current_time,
            current_height=current_height,
        ),
        directory,
        cryptos,
    )


def _response(
    nick: str,
    payload: ChannelInputPayload,
    crypto: WireCrypto,
    channel_id: str,
) -> dict[str, object]:
    return {
        "data": f"{crypto.encrypt(encode_channel_input_message(payload))} pubkey signature",
        "route": ReplyRoute(nick=nick, channel_id=channel_id),
    }


def _registry_with_frozen_channels(
    tmp_path: Path,
) -> tuple[TakerChannelInputRegistry, TakerChannelInputNegotiation]:
    negotiation = _new()
    for index, nick in enumerate(negotiation.selected_nicks):
        _add_freeze(negotiation, nick, index)
        negotiation.install_frozen_channel(nick, _frozen_snapshot(index))
    outpoints = tuple(intent.funding_outpoint for _, intent in negotiation.freeze_intents)
    store = TakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=4)
    store.save(negotiation.snapshot())
    registry = TakerChannelInputRegistry.load(
        store=store,
        config=_config(*outpoints),
        network="regtest",
        local_actor="taker",
    )
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None
    return registry, restored


def _prepare_peer_payloads(
    negotiation: TakerChannelInputNegotiation,
) -> tuple[
    ChannelInputTranscript,
    tuple[EndpointTerms, ...],
    tuple[tuple[str, ChannelInputPayload], ...],
]:
    transcript, terms = _plan(negotiation)
    peer = TakerChannelInputNegotiation.from_snapshot(negotiation.snapshot())
    proposals = peer.plan_proposals(transcript, terms, current_time=100, current_height=100)
    approvals: list[tuple[str, ChannelInputPayload]] = []
    for (nick, proposal), secret in zip(proposals, _MAKER_SECRETS, strict=True):
        approval = _approval(proposal, secret)
        peer.accept_approval(nick, approval)
        approvals.append((nick, approval))
    return transcript, terms, tuple(approvals)


def _ack_response_factory(
    directory: FakeDirectory,
    cryptos: dict[str, WireCrypto],
    proposals_source: TakerChannelInputNegotiation,
    transcript: ChannelInputTranscript,
    channel_prefix: str,
) -> Callable[[], dict[str, dict[str, object]]]:
    def responses() -> dict[str, dict[str, object]]:
        approval_set = next(
            payload
            for _nick, payload in reversed(directory.outbound_payloads)
            if isinstance(payload, ChannelInputApprovalSetPayload)
        )
        proposals = dict(proposals_source.proposals)
        result: dict[str, dict[str, object]] = {}
        for index, (nick, secret) in enumerate(
            zip(proposals_source.selected_nicks, _MAKER_SECRETS, strict=True)
        ):
            ack = sign_payload(
                ChannelInputApprovalSetAckPayload(
                    session_id=transcript.session_id,
                    attempt=transcript.attempt,
                    signer_key=proposals[nick].recipient_key,
                    transcript_hash=transcript_hash(transcript).hex(),
                    approval_set_hash=approval_set_hash(approval_set).hex(),
                ),
                secret,
                aux_randomness=b"\x03" * 32,
            )
            result[nick] = _response(nick, ack, cryptos[nick], f"{channel_prefix}:{index}")
        return result

    return responses


@pytest.mark.asyncio
async def test_encrypted_response_records_route_and_exact_route_is_used() -> None:
    negotiation = _new()
    negotiation.selected_nicks = ("maker-a",)  # type: ignore[misc]
    transport, directory, cryptos = _transport(negotiation, ("maker-a",))
    crypto = cryptos["maker-a"]
    transport._require_live_negotiation(negotiation.invite.session_id, negotiation.invite.attempt)

    await transport._send("maker-a", negotiation.invite)
    assert directory.sent[0][0:2] == ("maker-a", "channel_input")
    assert crypto.decrypt(directory.sent[0][2]) == encode_channel_input_message(negotiation.invite)

    remote = _freeze_remote(0)
    route = ReplyRoute(nick="maker-a", channel_id="directory:reply")
    directory.responses = {
        "maker-a": {
            "data": f"{crypto.encrypt(encode_channel_input_message(remote))} pubkey signature",
            "route": route,
        }
    }

    accepted: list[ChannelInputHelloPayload] = []

    async def accept(_nick: str, payload: ChannelInputHelloPayload) -> None:
        accepted.append(payload)

    await transport._receive_all(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        ChannelInputHelloPayload,
        accept,
    )
    assert accepted == [remote]
    assert transport.latest_reply_routes == (("maker-a", route),)

    await transport._send("maker-a", negotiation.invite, route=route)
    assert directory.routed[0][0:2] == ("maker-a", "channel_input")
    assert directory.routed[0][3] == route


@pytest.mark.asyncio
async def test_reply_route_is_retained_before_oversized_ciphertext_is_decrypted() -> None:
    negotiation = _new()
    negotiation.selected_nicks = ("maker-a",)  # type: ignore[misc]
    transport, directory, cryptos = _transport(negotiation, ("maker-a",))
    route = ReplyRoute(nick="maker-a", channel_id="direct")
    directory.responses = {
        "maker-a": {
            "data": f"{'x' * (MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES + 1)} pubkey signature",
            "route": route,
        }
    }

    with pytest.raises(Exception, match="exceeds size limit"):
        await transport._receive_all(
            negotiation.invite.session_id,
            negotiation.invite.attempt,
            ChannelInputHelloPayload,
            lambda _nick, _payload: None,
        )

    assert cryptos["maker-a"].decrypted == []
    assert transport.latest_reply_routes == (("maker-a", route),)


@pytest.mark.asyncio
async def test_malformed_peer_does_not_prevent_later_valid_response_persistence() -> None:
    negotiation = _new()
    negotiation.selected_nicks = ("maker-a", "maker-b")  # type: ignore[misc]
    transport, directory, cryptos = _transport(negotiation, ("maker-a", "maker-b"))
    valid = _freeze_remote(1)
    valid_ciphertext = cryptos["maker-b"].encrypt(encode_channel_input_message(valid))
    directory.responses = {
        "maker-a": {
            "data": "not-base64 pubkey signature",
            "route": ReplyRoute(nick="maker-a", channel_id="directory:bad"),
        },
        "maker-b": {
            "data": f"{valid_ciphertext} pubkey signature",
            "route": ReplyRoute(nick="maker-b", channel_id="direct"),
        },
    }
    accepted: list[tuple[str, ChannelInputHelloPayload]] = []

    async def accept(nick: str, payload: ChannelInputHelloPayload) -> None:
        accepted.append((nick, payload))

    with pytest.raises(Exception, match="invalid channel-input response"):
        await transport._receive_all(
            negotiation.invite.session_id,
            negotiation.invite.attempt,
            ChannelInputHelloPayload,
            accept,
        )

    assert accepted == [("maker-b", valid)]
    assert transport.latest_reply_routes == (
        ("maker-a", ReplyRoute(nick="maker-a", channel_id="directory:bad")),
        ("maker-b", ReplyRoute(nick="maker-b", channel_id="direct")),
    )


@pytest.mark.asyncio
async def test_proposals_use_latest_directory_and_direct_reply_routes() -> None:
    negotiation = _new()
    negotiation.selected_nicks = ("maker-a", "maker-b")  # type: ignore[misc]
    transport, directory, _cryptos = _transport(negotiation, ("maker-a", "maker-b"))
    directory_route = ReplyRoute(nick="maker-a", channel_id="directory:alpha")
    direct_route = ReplyRoute(nick="maker-b", channel_id="direct")
    transport._routes.update({"maker-a": directory_route, "maker-b": direct_route})

    await transport._send_mapping(
        negotiation.selected_nicks,
        {"maker-a": negotiation.invite, "maker-b": negotiation.invite},
    )

    assert [(nick, route) for nick, _command, _data, route in directory.routed] == [
        ("maker-a", directory_route),
        ("maker-b", direct_route),
    ]
    assert directory.sent == []


@pytest.mark.asyncio
async def test_hello_freeze_is_durable_before_exact_route_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lnd_candidate = _candidate(0)
    config = _config(lnd_candidate.funding_outpoint)
    store = TakerChannelInputStore(tmp_path / "hello", max_active_sessions=4)
    registry = TakerChannelInputRegistry.load(
        store=store,
        config=config,
        network="regtest",
        local_actor="taker",
    )
    negotiation = await registry.create(
        selected_nicks=("maker-a",),
        secret_key=(1).to_bytes(32, "big"),
        session_id="11" * 32,
        attempt=1,
        expiry=2_000,
        max_parent_fee_sat=3_000,
        max_fee_rate_sat_vb=100,
        min_recovery_delta=10,
        max_recovery_delta=50,
    )
    backend = FakeBackend()
    frozen = _frozen_snapshot(0)
    backend.frozen[frozen.channel_id] = frozen
    initialized = SimpleNamespace(
        backend=backend,
        node_info=SimpleNamespace(identity_pubkey=_node(10)),
        candidates=(lnd_candidate,),
    )
    transport, directory, cryptos = _transport(
        negotiation,
        negotiation.selected_nicks,
        registry=registry,
        initialized_backend=initialized,
        config=config,
    )
    route = ReplyRoute(nick="maker-a", channel_id="direct")
    directory.response_batches = [
        {"maker-a": _response("maker-a", _freeze_remote(0), cryptos["maker-a"], "direct")}
    ]
    original_save = registry._store.save

    def save(snapshot: object) -> None:
        directory.events.append(f"save:{getattr(snapshot, 'stage')}")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", save)
    await transport.exchange_hellos(negotiation.invite.session_id, negotiation.invite.attempt)

    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.stage is TakerChannelInputStage.HELLOS_READY
    local_hello_send = directory.events.index("send:channel_input_hello:maker-a")
    assert directory.events.index("save:hellos_ready") < local_hello_send
    assert directory.routed[-1][3] == route
    assert dict(restored.local_hellos)["maker-a"].settlement_script_pubkey == "5120" + "01" * 32
    assert dict(restored.local_hellos)["maker-a"].quoted_miner_fee_share_sat == 3_000


@pytest.mark.asyncio
async def test_failed_hello_uses_known_route_and_cancels_full_roster(
    tmp_path: Path,
) -> None:
    negotiation = _new()
    store = TakerChannelInputStore(tmp_path / "failed-hello", max_active_sessions=4)
    store.save(negotiation.snapshot())
    registry = TakerChannelInputRegistry.load(
        store=store,
        config=_config(),
        network="regtest",
        local_actor="taker",
    )
    backend = FakeBackend()
    transport, directory, cryptos = _transport(
        negotiation,
        negotiation.selected_nicks,
        registry=registry,
        backend=backend,
    )
    directory.response_batches = [
        {
            "maker-a": _response(
                "maker-a", negotiation.invite, cryptos["maker-a"], "authenticated-ingress"
            )
        }
    ]

    def cancel_responses() -> dict[str, dict[str, object]]:
        cancel = next(
            payload
            for _nick, payload in reversed(directory.outbound_payloads)
            if isinstance(payload, ChannelInputCancelPayload)
        )
        responses: dict[str, dict[str, object]] = {}
        for index, nick in enumerate(negotiation.selected_nicks):
            remote = _freeze_remote(index)
            ack = sign_payload(
                ChannelInputCancelAckPayload(
                    session_id=cancel.session_id,
                    attempt=cancel.attempt,
                    signer_key=remote.participant_key,
                    transcript_hash=cancel.transcript_hash,
                    cancel_hash=cancel_payload_hash(cancel).hex(),
                    resumed_outpoints=(remote.funding_outpoint,),
                ),
                _MAKER_SECRETS[index],
                aux_randomness=bytes([index + 20]) * 32,
            )
            responses[nick] = _response(nick, ack, cryptos[nick], f"cancel-ack:{index}")
        return responses

    directory.response_factories = [cancel_responses]
    with pytest.raises(Exception, match="unexpected channel-input payload"):
        await transport.exchange_hellos(negotiation.invite.session_id, negotiation.invite.attempt)

    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.stage is TakerChannelInputStage.CANCELED
    cancellation_recipients = {
        nick
        for nick, payload in directory.outbound_payloads
        if isinstance(payload, ChannelInputCancelPayload)
    }
    assert cancellation_recipients == set(negotiation.selected_nicks)
    assert any(
        nick == "maker-a" and route.channel_id == "authenticated-ingress"
        for nick, _command, _data, route in directory.routed
    )
    assert backend.events == []


@pytest.mark.asyncio
async def test_real_registry_prepare_is_durable_never_sent_and_replays_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, negotiation = _registry_with_frozen_channels(tmp_path)
    transcript, terms, approvals = _prepare_peer_payloads(negotiation)
    nicks = negotiation.selected_nicks
    transport, directory, cryptos = _transport(
        negotiation,
        nicks,
        registry=registry,
        backend=FakeBackend(),
        current_time=100,
        current_height=100,
    )
    transport._routes.update(
        {
            nick: ReplyRoute(nick=nick, channel_id="direct" if index == 1 else f"dir:{index}")
            for index, nick in enumerate(nicks)
        }
    )
    directory.response_batches = [
        {
            nick: _response(nick, payload, cryptos[nick], f"approval:{index}")
            for index, (nick, payload) in enumerate(approvals)
        }
    ]
    directory.response_factories = [
        _ack_response_factory(directory, cryptos, negotiation, transcript, "ack")
    ]
    original_save = registry._store.save

    def save(snapshot: object) -> None:
        stage = getattr(snapshot, "stage")
        directory.events.append(f"save:{stage}")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", save)
    result = await transport.build_prepare(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        transcript,
        terms,
    )

    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None
    assert restored.stage is TakerChannelInputStage.PREPARE_READY
    assert result.prepare == restored.prepare
    assert directory.events.index("save:proposals_ready") < next(
        index
        for index, event in enumerate(directory.events)
        if event.startswith("send:channel_input_proposal:")
    )
    assert directory.events.index("save:approval_set_ready") < next(
        index
        for index, event in enumerate(directory.events)
        if event.startswith("send:channel_input_approvals:")
    )
    assert directory.events[-1] == "save:prepare_ready"
    assert not any(event.startswith("send:channel_input_prepare:") for event in directory.events)

    persisted_prepare = result.prepare
    replayed_registry = TakerChannelInputRegistry.load(
        store=registry._store,
        config=registry._config,
        network="regtest",
        local_actor="taker",
    )
    replayed = replayed_registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert replayed is not None
    replay_transport, replay_directory, replay_cryptos = _transport(
        replayed,
        nicks,
        registry=replayed_registry,
        backend=FakeBackend(),
        current_time=100,
        current_height=100,
    )
    fresh_approval_routes = {
        nick: ReplyRoute(nick=nick, channel_id=f"fresh-approval:{index}")
        for index, nick in enumerate(nicks)
    }
    replay_directory.response_batches = [
        {
            nick: _response(
                nick, payload, replay_cryptos[nick], fresh_approval_routes[nick].channel_id
            )
            for nick, payload in approvals
        }
    ]
    replay_directory.response_factories = [
        _ack_response_factory(replay_directory, replay_cryptos, replayed, transcript, "fresh-ack")
    ]
    replay_result = await replay_transport.build_prepare(
        replayed.invite.session_id,
        replayed.invite.attempt,
        transcript,
        terms,
    )

    assert replay_result.prepare == persisted_prepare
    assert len(replay_directory.sent) == len(nicks)
    assert all(
        decode_channel_input_message(replay_cryptos[nick].decrypt(data)).type
        == "channel_input_proposal"
        for nick, _command, data, _channel in replay_directory.sent
    )
    assert {
        (nick, route.channel_id)
        for nick, _command, _data, route in replay_directory.routed[: len(nicks)]
    } == {(nick, route.channel_id) for nick, route in fresh_approval_routes.items()}
    assert not any(
        isinstance(
            decode_channel_input_message(replay_cryptos[nick].decrypt(data)),
            ChannelInputPreparePayload,
        )
        for nick, _command, data, _channel in replay_directory.sent
    )
    assert not any(
        isinstance(
            decode_channel_input_message(replay_cryptos[nick].decrypt(data)),
            ChannelInputPreparePayload,
        )
        for nick, _command, data, _route in replay_directory.routed
    )


@pytest.mark.asyncio
async def test_partial_cancel_acks_are_durable_and_fresh_replay_completes_without_escrows(
    tmp_path: Path,
) -> None:
    negotiation = _new()
    store = TakerChannelInputStore(tmp_path / "cancel", max_active_sessions=4)
    store.save(negotiation.snapshot())
    registry = TakerChannelInputRegistry.load(
        store=store,
        config=_config(),
        network="regtest",
        local_actor="taker",
    )
    cancel = await registry.plan_cancellation(
        negotiation.invite.session_id, negotiation.invite.attempt, "transport_failure"
    )
    cancel_acks = tuple(
        (
            nick,
            sign_payload(
                ChannelInputCancelAckPayload(
                    session_id=cancel.session_id,
                    attempt=cancel.attempt,
                    signer_key=_freeze_remote(index).participant_key,
                    transcript_hash=cancel.transcript_hash,
                    cancel_hash=cancel_payload_hash(cancel).hex(),
                    resumed_outpoints=(_freeze_remote(index).funding_outpoint,),
                ),
                _MAKER_SECRETS[index],
                aux_randomness=bytes([index + 20]) * 32,
            ),
        )
        for index, nick in enumerate(negotiation.selected_nicks)
    )
    transport, directory, cryptos = _transport(
        negotiation, negotiation.selected_nicks, registry=registry, backend=FakeBackend()
    )
    known_route = ReplyRoute(nick="maker-a", channel_id="direct")
    transport._routes["maker-a"] = known_route
    directory.response_batches = [
        {
            nick: _response(nick, payload, cryptos[nick], f"partial:{index}")
            for index, (nick, payload) in enumerate(cancel_acks[:2])
        }
    ]

    assert not await transport.cancel_or_replay(cancel.session_id, cancel.attempt)
    partial = registry.get(cancel.session_id, cancel.attempt)
    assert partial is not None
    assert len(partial.cancel_acks) == 2
    assert {nick for nick, _command, _data, _route in directory.routed} == {"maker-a"}
    assert {nick for nick, _command, _data, _channel in directory.sent} == {
        "maker-b",
        "maker-c",
    }

    restored_registry = TakerChannelInputRegistry.load(
        store=store,
        config=_config(),
        network="regtest",
        local_actor="taker",
    )
    restored = restored_registry.get(cancel.session_id, cancel.attempt)
    assert restored is not None
    replay, replay_directory, replay_cryptos = _transport(
        restored,
        restored.selected_nicks,
        registry=restored_registry,
        backend=FakeBackend(),
    )
    replay_directory.response_batches = [
        {
            nick: _response(nick, payload, replay_cryptos[nick], f"fresh:{index}")
            for index, (nick, payload) in enumerate(cancel_acks)
        }
    ]

    assert await replay.cancel_or_replay(cancel.session_id, cancel.attempt)
    completed = restored_registry.get(cancel.session_id, cancel.attempt)
    assert completed is not None
    assert completed.stage is TakerChannelInputStage.CANCELED
    assert completed.local_resumed_outpoints == ()
    assert (
        decode_channel_input_message(replay_cryptos["maker-a"].decrypt(replay_directory.sent[0][2]))
        == cancel
    )


@pytest.mark.asyncio
async def test_inner_validation_rejects_type_session_and_signature_but_uses_outer_nick() -> None:
    negotiation = _new()
    negotiation.selected_nicks = ("maker-a",)  # type: ignore[misc]
    transport, directory, cryptos = _transport(negotiation, ("maker-a",))
    crypto = cryptos["maker-a"]
    remote = _freeze_remote(1)
    accepted: list[tuple[str, str]] = []

    async def accept(nick: str, payload: ChannelInputHelloPayload) -> None:
        accepted.append((nick, payload.signer_key))

    directory.responses = {"maker-a": _response("maker-a", remote, crypto, "outer-route")}
    await transport._receive_all(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        ChannelInputHelloPayload,
        accept,
    )
    assert accepted == [("maker-a", remote.signer_key)]

    wrong_session = sign_payload(
        _freeze_remote(0).model_copy(update={"session_id": "22" * 32, "sig": "00" * 64}),
        _MAKER_SECRETS[0],
        aux_randomness=b"\x04" * 32,
    )
    invalid_payloads: tuple[ChannelInputPayload, ...] = (
        cast(ChannelInputInvitePayload, negotiation.invite),
        wrong_session,
        _freeze_remote(0).model_copy(update={"sig": "00" * 64}),
    )
    expected_errors = ("unexpected", "another channel-input attempt", "invalid BIP340")
    for payload, expected_error in zip(invalid_payloads, expected_errors, strict=True):
        directory.responses = {"maker-a": _response("maker-a", payload, crypto, "outer-route")}
        with pytest.raises(Exception, match=expected_error):
            await transport._receive_all(
                negotiation.invite.session_id,
                negotiation.invite.attempt,
                ChannelInputHelloPayload,
                accept,
            )


@pytest.mark.asyncio
async def test_single_maker_exchange_reuses_latest_route_and_refreshes_ingress() -> None:
    negotiation = _new()
    negotiation.selected_nicks = ("maker-a",)  # type: ignore[misc]
    transport, directory, cryptos = _transport(negotiation, ("maker-a",))
    old_route = ReplyRoute(nick="maker-a", channel_id="old-direct")
    new_route = ReplyRoute(nick="maker-a", channel_id="new-direct")
    transport._routes["maker-a"] = old_route
    response = sign_payload(
        ChannelInputPreparedPayload(
            session_id=negotiation.invite.session_id,
            attempt=negotiation.invite.attempt,
            signer_key=_freeze_remote(0).participant_key,
            transcript_hash="01" * 32,
            funding_outpoint=_freeze_remote(0).funding_outpoint,
            public_nonce="02" * 66,
        ),
        _MAKER_SECRETS[0],
        aux_randomness=b"\x08" * 32,
    )
    directory.responses = {
        "maker-a": _response("maker-a", response, cryptos["maker-a"], "new-direct")
    }

    received = await transport.exchange_one(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-a",
        negotiation.invite,
        ChannelInputPreparedPayload,
    )

    assert received == response
    assert directory.routed[-1][3] == old_route
    assert directory.wait_arguments[-1]["expected_nicks"] == ["maker-a"]
    assert directory.wait_arguments[-1]["expected_command"] == "!channel_input"
    assert transport.latest_reply_routes == (("maker-a", new_route),)


@pytest.mark.asyncio
async def test_recovery_establishes_fresh_crypto_without_live_maker_session() -> None:
    negotiation = _new()
    negotiation.selected_nicks = ("maker-a",)  # type: ignore[misc]
    _add_freeze(negotiation, "maker-a", 0)
    negotiation.install_frozen_channel("maker-a", _frozen_snapshot(0))
    route = ReplyRoute(nick="maker-a", channel_id="direct:fresh")
    directory = RecoveryDirectory(_MAKER_SECRETS[0], route)
    session = SimpleNamespace(maker_sessions={}, directory_client=directory)
    transport = TakerChannelInputTransport(
        session=cast(CoinJoinSession, session),
        registry=cast(TakerChannelInputRegistry, FakeRegistry(negotiation)),
        initialized_backend=cast(
            InitializedChannelInputBackend, SimpleNamespace(backend=FakeBackend())
        ),
        config=ChannelInputConfig(),
        settlement_script_allocator=lambda _nick: "5120" + "01" * 32,
        current_time=1,
        current_height=1,
    )

    assert (
        await transport.establish_recovery(
            negotiation.invite.session_id, negotiation.invite.attempt, "maker-a", route
        )
        == route
    )
    response = await transport.exchange_one(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-a",
        negotiation.invite,
        ChannelInputPreparedPayload,
    )

    assert isinstance(response, ChannelInputPreparedPayload)
    assert directory.commands == [
        "channel_input_recover",
        "channel_input_recover",
        "channel_input_recover",
    ]


def test_recovery_response_parsers_require_exact_verified_envelopes() -> None:
    negotiation = _new()
    maker_key = ChannelInputKeyPair.from_secret(_MAKER_SECRETS[0])
    crypto = CryptoSession()
    accept = sign_payload(
        ChannelInputRecoveryAcceptPayload(
            session_id=negotiation.invite.session_id,
            attempt=negotiation.invite.attempt,
            signer_key=maker_key.public_key,
            recovery_id="22" * 32,
            init_hash="33" * 32,
            ephemeral_pubkey=crypto.get_pubkey_hex(),
        ),
        _MAKER_SECRETS[0],
    )
    token = TakerChannelInputTransport._encode_recovery_token(accept)

    assert (
        TakerChannelInputTransport._decode_recovery_response_token(
            f"{token} outer-key outer-signature"
        )
        == accept
    )
    for malformed in (token, f"{token} outer-key outer-signature extra"):
        with pytest.raises(TakerChannelInputTransportError, match="malformed"):
            TakerChannelInputTransport._decode_recovery_response_token(malformed)

    wire = WireCrypto()
    ciphertext = wire.encrypt(encode_channel_input_message(accept))
    recovery_id = accept.recovery_id
    assert (
        TakerChannelInputTransport._decode_recovery_encrypted(
            f"{recovery_id} {ciphertext} outer-key outer-signature",
            recovery_id,
            wire,
        )
        == accept
    )
    for malformed in (
        f"{recovery_id} {ciphertext}",
        f"{recovery_id} {ciphertext} outer-key outer-signature extra",
    ):
        with pytest.raises(TakerChannelInputTransportError, match="active ID"):
            TakerChannelInputTransport._decode_recovery_encrypted(malformed, recovery_id, wire)
