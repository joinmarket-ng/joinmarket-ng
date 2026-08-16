"""Tests for pre-escrow maker ring authorization persistence."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from pydantic import ValidationError

from jmswap.errors import ChannelInputEscrowError
from jmswap.ring_authorization import (
    RingChannelPoint,
    RingEdgeAuthorization,
    RingEdgeAuthorizationRecord,
    RingEdgeAuthorizationStore,
    RingEdgeFunding,
    RingEdgeRole,
)


def _authorization(**overrides: object) -> RingEdgeAuthorization:
    values: dict[str, object] = {
        "edge_id": "11" * 32,
        "coinjoin_context": "22" * 32,
        "taker_nick": "taker",
        "taker_session_pubkey": "33" * 32,
        "funder_nick": "funder",
        "receiver_nick": "receiver",
        "funder_session_pubkey": "44" * 32,
        "receiver_session_pubkey": "55" * 32,
        "funder_ln_node": "02" + "66" * 32 + "@funder.example:9735",
        "receiver_ln_node": "03" + "77" * 32 + "@receiver.example:9735",
        "coinjoin_amount_sats": 100_000,
        "capacity_sats": 150_000,
        "funder_contribution_sats": 80_000,
        "receiver_contribution_sats": 70_000,
        "expires_at_unix": 2_000_000_000,
    }
    values.update(overrides)
    return RingEdgeAuthorization.model_validate(values)


def _channel_point(txid: str = "88" * 32) -> RingChannelPoint:
    return RingChannelPoint(txid=txid, vout=1)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"edge_id": "11" * 31}, "edge_id"),
        ({"funder_nick": "receiver"}, "nicks must be distinct"),
        ({"receiver_session_pubkey": "44" * 32}, "session keys must be distinct"),
        ({"receiver_ln_node": "02" + "66" * 32 + "@receiver.example:9735"}, "LN node"),
        ({"funder_contribution_sats": 79_999}, "conserve capacity"),
    ],
)
def test_authorization_rejects_invalid_ring_invariants(
    overrides: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        _authorization(**overrides)


def test_authorization_hash_helpers_and_transport_are_deterministic() -> None:
    authorization = _authorization()
    reversed_values = dict(reversed(list(authorization.model_dump().items())))
    rebuilt = RingEdgeAuthorization.model_validate(reversed_values)

    assert authorization.authorization_id == rebuilt.authorization_id
    assert authorization.role_for("funder") is RingEdgeRole.FUNDER
    assert authorization.local_contribution_sats("receiver") == 70_000
    assert authorization.peer_nick("funder") == "receiver"
    assert authorization.peer_session_pubkey("receiver") == "44" * 32
    assert authorization.peer_ln_node("funder") == authorization.receiver_ln_node

    transport = authorization.to_transport()
    assert "=" not in transport
    assert RingEdgeAuthorization.from_transport(transport) == authorization


@pytest.mark.parametrize(
    "transport",
    [
        "not valid!",
        "abc=",
        base64.urlsafe_b64encode(b"not-json").rstrip(b"=").decode("ascii"),
        base64.urlsafe_b64encode(b'{"version":2}').rstrip(b"=").decode("ascii"),
    ],
)
def test_authorization_transport_rejects_malformed_values(transport: str) -> None:
    with pytest.raises(ChannelInputEscrowError, match="transport"):
        RingEdgeAuthorization.from_transport(transport)


def test_funding_relay_transport_round_trip() -> None:
    relay = RingEdgeFunding(
        edge_id="11" * 32,
        authorization_id="22" * 32,
        funding_address="bcrt1pfunding",
        contribution_sats=70_000,
    )

    assert RingEdgeFunding.from_transport(relay.to_transport()) == relay
    with pytest.raises(ChannelInputEscrowError, match="funding relay"):
        RingEdgeFunding.from_transport("incomplete")


def test_record_requires_valid_local_endpoint_and_complete_funding_binding() -> None:
    authorization = _authorization()
    with pytest.raises(ValidationError, match="endpoint"):
        RingEdgeAuthorizationRecord(authorization=authorization, local_nick="other")
    with pytest.raises(ValidationError, match="bound together"):
        RingEdgeAuthorizationRecord(
            authorization=authorization,
            local_nick="funder",
            funding_address="bcrt1qfunding",
        )
    with pytest.raises(ValidationError, match="requires a funding"):
        RingEdgeAuthorizationRecord(
            authorization=authorization,
            local_nick="funder",
            channel_point=_channel_point(),
        )


def test_store_rejects_expired_authorization(tmp_path: Path) -> None:
    store = RingEdgeAuthorizationStore(tmp_path)
    with pytest.raises(ChannelInputEscrowError, match="expired"):
        store.authorize(_authorization(expires_at_unix=9), local_nick="funder", now_unix=10)


def test_store_authorization_is_idempotent_and_rejects_conflicting_replay(tmp_path: Path) -> None:
    authorization = _authorization()
    first_store = RingEdgeAuthorizationStore(tmp_path)
    record = first_store.authorize(authorization, local_nick="funder", now_unix=1)

    second_store = RingEdgeAuthorizationStore(tmp_path)
    assert second_store.authorize(authorization, local_nick="funder", now_unix=1) == record
    conflicting = _authorization(coinjoin_context="99" * 32)
    with pytest.raises(ChannelInputEscrowError, match="replay conflicts"):
        second_store.authorize(conflicting, local_nick="funder", now_unix=1)


def test_store_funding_binding_checks_hash_and_is_immutable(tmp_path: Path) -> None:
    authorization = _authorization()
    store = RingEdgeAuthorizationStore(tmp_path)
    store.authorize(authorization, local_nick="funder", now_unix=1)

    with pytest.raises(ChannelInputEscrowError, match="hash does not match"):
        store.bind_funding(authorization.edge_id, "00" * 32, "bcrt1qfunding", "5120" + "aa" * 32)

    bound = store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        "bcrt1qfunding",
        "5120" + "aa" * 32,
    )
    assert bound.revision == 1
    assert (
        store.bind_funding(
            authorization.edge_id,
            authorization.authorization_id,
            "bcrt1qfunding",
            "5120" + "aa" * 32,
        )
        == bound
    )
    with pytest.raises(ChannelInputEscrowError, match="immutable"):
        store.bind_funding(
            authorization.edge_id,
            authorization.authorization_id,
            "bcrt1qother",
            "5120" + "aa" * 32,
        )


def test_store_channel_point_requires_funding_and_is_immutable(tmp_path: Path) -> None:
    authorization = _authorization()
    store = RingEdgeAuthorizationStore(tmp_path)
    store.authorize(authorization, local_nick="receiver", now_unix=1)

    with pytest.raises(ChannelInputEscrowError, match="requires a funding"):
        store.bind_channel_point(
            authorization.edge_id, authorization.authorization_id, _channel_point()
        )

    store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        "bcrt1qfunding",
        "5120" + "aa" * 32,
    )
    bound = store.bind_channel_point(
        authorization.edge_id, authorization.authorization_id, _channel_point()
    )
    assert bound.revision == 2
    assert (
        store.bind_channel_point(
            authorization.edge_id, authorization.authorization_id, _channel_point()
        )
        == bound
    )
    with pytest.raises(ChannelInputEscrowError, match="immutable"):
        store.bind_channel_point(
            authorization.edge_id,
            authorization.authorization_id,
            _channel_point("99" * 32),
        )


def test_store_list_skips_and_preserves_corrupt_files(tmp_path: Path) -> None:
    authorization = _authorization()
    store = RingEdgeAuthorizationStore(tmp_path)
    persisted = store.authorize(authorization, local_nick="funder", now_unix=1)
    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("not-json")

    assert store.load(authorization.edge_id) == persisted
    assert store.list() == [persisted]
    assert corrupt_path.read_text() == "not-json"
