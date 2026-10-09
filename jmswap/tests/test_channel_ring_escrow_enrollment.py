"""Ring channels enroll for escrow buyouts without ever blocking the ring."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from jmcore.channel_ring_store import RingLifecycleState

import jmswap.channel_ring as ring
from jmswap.buyout_messages import Outpoint
from jmswap.channel_ring import (
    EscrowEnrollment,
    RingAuthorizationError,
    authorize_recorded_ring_channels,
    enroll_confirmed_ring_channels,
    enroll_escrow_channels,
    wallet_escrow_authorizer_pubkey,
)
from jmswap.lnd import LndBackend
from jmswap.lnd_escrow import LndEscrowRpcError

KEY = b"\x07" * 32
NODE_ID = "02" + "11" * 32
TXID = "ab" * 32
WALLET = "cd" * 32


class FakeEscrow:
    """Records enrollments; ``errors`` maps a vout to the status it fails with."""

    instances: list[FakeEscrow] = []
    errors: dict[int, grpc.StatusCode] = {}
    connect_error: Exception | None = None

    def __init__(self, endpoint: str, cert: bytes, macaroon: bytes, timeout: float) -> None:
        self.args = (endpoint, cert, macaroon)
        self.calls: list[tuple[Outpoint, bytes]] = []
        FakeEscrow.instances.append(self)

    async def __aenter__(self) -> FakeEscrow:
        if FakeEscrow.connect_error is not None:
            raise FakeEscrow.connect_error
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def authorize_channel(self, point: Outpoint, key: bytes) -> None:
        self.calls.append((point, key))
        if point.vout in FakeEscrow.errors:
            raise LndEscrowRpcError("AuthorizeChannel", FakeEscrow.errors[point.vout])


@pytest.fixture(autouse=True)
def fake_escrow(monkeypatch: pytest.MonkeyPatch) -> type[FakeEscrow]:
    FakeEscrow.instances = []
    FakeEscrow.errors = {}
    FakeEscrow.connect_error = None
    monkeypatch.setattr(ring, "LndEscrowClient", FakeEscrow)
    return FakeEscrow


def _backend(host: str = "127.0.0.1:10009") -> LndBackend:
    return LndBackend(host=host, tls_cert=b"cert", macaroon_hex="beef")


async def test_enrolls_every_point_once_with_the_ring_credentials() -> None:
    outcomes = await enroll_escrow_channels(_backend(), [(TXID, 0), (TXID, 1), (TXID, 0)], KEY)
    assert outcomes == {(TXID, 0): EscrowEnrollment.ENROLLED, (TXID, 1): EscrowEnrollment.ENROLLED}
    [client] = FakeEscrow.instances
    assert client.args == ("127.0.0.1:10009", b"cert", b"\xbe\xef")
    assert [(point.vout, key) for point, key in client.calls] == [(0, KEY), (1, KEY)]


async def test_unpatched_node_is_reported_unsupported() -> None:
    FakeEscrow.errors = {0: grpc.StatusCode.UNIMPLEMENTED}
    outcomes = await enroll_escrow_channels(_backend(), [(TXID, 0)], KEY)
    assert outcomes == {(TXID, 0): EscrowEnrollment.UNSUPPORTED}


@pytest.mark.parametrize(
    "code", [grpc.StatusCode.FAILED_PRECONDITION, grpc.StatusCode.PERMISSION_DENIED]
)
async def test_any_other_failure_never_raises(code: grpc.StatusCode) -> None:
    FakeEscrow.errors = {0: code}
    outcomes = await enroll_escrow_channels(_backend(), [(TXID, 0), (TXID, 1)], KEY)
    assert outcomes == {(TXID, 0): EscrowEnrollment.FAILED, (TXID, 1): EscrowEnrollment.FAILED}


async def test_unreachable_node_never_raises() -> None:
    FakeEscrow.connect_error = OSError("connection refused")
    outcomes = await enroll_escrow_channels(_backend(), [(TXID, 0)], KEY)
    assert outcomes == {(TXID, 0): EscrowEnrollment.FAILED}


def _record(
    vouts: tuple[int | None, int | None],
    *,
    state: RingLifecycleState = RingLifecycleState.CONFIRMED_OPEN,
    node: str = "alpha",
    wallet: str = WALLET,
    network: str = "regtest",
    node_id: str = NODE_ID,
) -> Any:
    def edge(vout: int | None) -> Any:
        if vout is None:
            return None
        return SimpleNamespace(funding_outpoint=SimpleNamespace(txid=TXID, vout=vout))

    return SimpleNamespace(
        state=state,
        node_binding=SimpleNamespace(
            node_name=node, wallet_identity=wallet, network=network, local_node_id=node_id
        ),
        incoming_edge=edge(vouts[0]),
        outgoing_edge=edge(vouts[1]),
    )


async def test_confirmation_enrolls_both_edges_only_with_a_key() -> None:
    await enroll_confirmed_ring_channels(_backend(), _record((0, 1)), None)
    assert FakeEscrow.instances == []
    await enroll_confirmed_ring_channels(_backend(), _record((2, None)), KEY)
    assert [point.vout for point, _ in FakeEscrow.instances[0].calls] == [2]


@pytest.fixture
def node_info(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """What every configured endpoint reports from GetInfo; closes are counted."""
    reported: dict[str, Any] = {"identity": NODE_ID, "network": "regtest", "closed": 0}

    async def info(self: LndBackend, **_: object) -> Any:
        if isinstance(reported["identity"], Exception):
            raise reported["identity"]
        return SimpleNamespace(identity_pubkey=reported["identity"], network=reported["network"])

    async def close(self: LndBackend) -> None:
        reported["closed"] += 1

    monkeypatch.setattr(LndBackend, "node_info", info)
    monkeypatch.setattr(LndBackend, "close", close)
    return reported


def _config(tmp_path: Path, *names: str) -> Any:
    for name in ("tls.cert", "admin.macaroon"):
        (tmp_path / name).write_bytes(b"\x01")
    node = SimpleNamespace(
        lnd_grpc_url="https://lnd:10009",
        lnd_tls_cert_path=tmp_path / "tls.cert",
        lnd_macaroon_path=tmp_path / "admin.macaroon",
    )
    return SimpleNamespace(nodes=dict.fromkeys(names, node))


async def test_recorded_channels_enroll_on_their_bound_node(
    tmp_path: Path, node_info: dict[str, Any]
) -> None:
    records = [
        _record((0, 1)),
        _record((5, 6), state=RingLifecycleState.SIGNED),
        _record((None, 2)),
    ]
    outcomes = await authorize_recorded_ring_channels(
        _config(tmp_path, "alpha"),
        records,
        network="regtest",
        wallet_identity=WALLET,
        authorizer_pubkey=KEY,
    )
    assert sorted(vout for _, vout in outcomes) == [0, 1, 2]
    [client] = FakeEscrow.instances
    assert client.args[0] == "lnd:10009"
    assert node_info["closed"] == 1


@pytest.mark.parametrize(
    ("reported", "match"),
    [
        ({"identity": "03" + "22" * 32}, "another LND identity"),
        ({"network": "signet"}, "another LND identity"),
        ({"identity": OSError("refused")}, "could not be verified"),
    ],
)
async def test_endpoint_must_resolve_to_the_recorded_node_before_enrolling(
    tmp_path: Path, node_info: dict[str, Any], reported: dict[str, Any], match: str
) -> None:
    """A name repointed to the channel's other endpoint never enrolls this key."""
    node_info.update(reported)
    with pytest.raises(RingAuthorizationError, match=match):
        await authorize_recorded_ring_channels(
            _config(tmp_path, "alpha"),
            [_record((0, 1))],
            network="regtest",
            wallet_identity=WALLET,
            authorizer_pubkey=KEY,
        )
    assert FakeEscrow.instances == []
    assert node_info["closed"] == 1


@pytest.mark.parametrize(
    ("record", "match"),
    [
        (_record((0, 1), wallet="ef" * 32), "different wallet"),
        (_record((0, 1), network="signet"), "different wallet"),
        (_record((0, 1), node="beta"), "not configured"),
    ],
)
async def test_foreign_or_unconfigured_records_refuse_before_any_rpc(
    tmp_path: Path, record: Any, match: str
) -> None:
    with pytest.raises(RingAuthorizationError, match=match):
        await authorize_recorded_ring_channels(
            _config(tmp_path, "alpha"),
            [_record((3, 4)), record],
            network="regtest",
            wallet_identity=WALLET,
            authorizer_pubkey=KEY,
        )
    assert FakeEscrow.instances == []


def test_wallet_key_is_read_only_from_a_wallet_that_has_one() -> None:
    assert wallet_escrow_authorizer_pubkey(object()) is None
    wallet = SimpleNamespace(escrow_authorizer=SimpleNamespace(public_key=KEY))
    assert wallet_escrow_authorizer_pubkey(wallet) == KEY
