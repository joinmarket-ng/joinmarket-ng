from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jmcore.channel_input_config import ChannelInputConfig

from jmswap.channel_input import initialize_channel_input_backend
from jmswap.lnd import (
    ChannelInputCandidate,
    EscrowOutpoint,
    LndBackend,
    LndCapabilityError,
    LndNodeInfo,
)

NODE_ID = "02" + "22" * 32
FIRST_OUTPOINT = EscrowOutpoint(txid="11" * 32, vout=2)
SECOND_OUTPOINT = EscrowOutpoint(txid="22" * 32, vout=3)


def config(tmp_path: Path, **changes: Any) -> ChannelInputConfig:
    cert = tmp_path / "tls.cert"
    admin = tmp_path / "admin.macaroon"
    escrow = tmp_path / "escrow.macaroon"
    cert.write_bytes(b"cert")
    admin.write_bytes(b"admin")
    escrow.write_bytes(b"escrow")
    values: dict[str, Any] = {
        "enabled": True,
        "lnd_grpc_url": "https://127.0.0.1:10009",
        "lnd_tls_cert_path": cert,
        "lnd_macaroon_path": admin,
        "lnd_escrow_macaroon_path": escrow,
        "allowed_channel_points": (f"{FIRST_OUTPOINT.txid}:{FIRST_OUTPOINT.vout}",),
        "min_channel_capacity_sat": 100_000,
        "max_channel_capacity_sat": 1_000_000,
        "phase_timeout_seconds": 12.0,
    }
    values.update(changes)
    return ChannelInputConfig(**values)


def candidate(outpoint: EscrowOutpoint, *, capacity_sat: int = 500_000) -> ChannelInputCandidate:
    return ChannelInputCandidate(
        channel_id=outpoint.vout,
        funding_outpoint=outpoint,
        peer_node_pubkey=NODE_ID,
        capacity_sat=capacity_sat,
        local_balance_sat=capacity_sat // 2,
        remote_balance_sat=capacity_sat // 2,
        private=True,
        active=True,
        default_channel_state=True,
        pending_htlc_count=0,
        commitment_type=7,
    )


class FakeBackend:
    def __init__(
        self,
        candidates: tuple[ChannelInputCandidate, ...],
        *,
        node_error: Exception | None = None,
        candidate_error: Exception | None = None,
        probe_error: Exception | None = None,
    ) -> None:
        self.candidates = candidates
        self.node_error = node_error
        self.candidate_error = candidate_error
        self.probe_error = probe_error
        self.closed = False
        self.node_timeout: float | None = None
        self.candidate_filters: dict[str, Any] | None = None
        self.probe_call: tuple[EscrowOutpoint, float] | None = None

    async def node_info(self, *, timeout_seconds: float) -> LndNodeInfo:
        self.node_timeout = timeout_seconds
        if self.node_error is not None:
            raise self.node_error
        return LndNodeInfo(
            identity_pubkey=NODE_ID,
            version="0.21.1-beta",
            network="regtest",
            synced_to_chain=True,
            wallet_synced=True,
            feature_bits=frozenset({81}),
            advertised_uris=(),
        )

    async def list_channel_input_candidates(
        self, **kwargs: Any
    ) -> tuple[ChannelInputCandidate, ...]:
        self.candidate_filters = kwargs
        if self.candidate_error is not None:
            raise self.candidate_error
        return self.candidates

    async def probe_channel_input_escrow(
        self, funding_outpoint: EscrowOutpoint, *, timeout_seconds: float
    ) -> None:
        self.probe_call = (funding_outpoint, timeout_seconds)
        if self.probe_error is not None:
            raise self.probe_error

    async def close(self) -> None:
        self.closed = True


async def test_initialize_validates_policy_and_probes_deterministic_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend(
        (
            candidate(SECOND_OUTPOINT),
            candidate(FIRST_OUTPOINT),
            candidate(FIRST_OUTPOINT, capacity_sat=1_000_001),
        )
    )
    captured: dict[str, Any] = {}

    def from_paths(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return fake

    monkeypatch.setattr(LndBackend, "from_paths", from_paths)
    initialized = await initialize_channel_input_backend(
        config(
            tmp_path,
            allowed_channel_points=(
                f"{FIRST_OUTPOINT.txid}:{FIRST_OUTPOINT.vout}",
                f"{SECOND_OUTPOINT.txid}:{SECOND_OUTPOINT.vout}",
            ),
        ),
        network="regtest",
        offer_type="tr0absoffer",
    )

    assert captured["args"] == (
        "127.0.0.1:10009",
        tmp_path / "tls.cert",
        tmp_path / "admin.macaroon",
    )
    assert captured["kwargs"] == {
        "network": "regtest",
        "escrow_macaroon_path": tmp_path / "escrow.macaroon",
    }
    assert fake.node_timeout == 12.0
    assert fake.candidate_filters == {
        "allowed_outpoints": (FIRST_OUTPOINT, SECOND_OUTPOINT),
        "allowed_peer_node_pubkeys": None,
        "timeout_seconds": 12.0,
    }
    assert initialized.candidates == (candidate(FIRST_OUTPOINT), candidate(SECOND_OUTPOINT))
    assert fake.probe_call == (FIRST_OUTPOINT, 12.0)
    assert not fake.closed


@pytest.mark.parametrize(
    "fake",
    [
        FakeBackend(
            (),
        ),
        FakeBackend((candidate(SECOND_OUTPOINT),)),
        FakeBackend((candidate(FIRST_OUTPOINT),), node_error=LndCapabilityError("node failed")),
        FakeBackend(
            (candidate(FIRST_OUTPOINT),), candidate_error=LndCapabilityError("list failed")
        ),
        FakeBackend((candidate(FIRST_OUTPOINT),), probe_error=LndCapabilityError("probe failed")),
    ],
)
async def test_initialize_closes_backend_for_all_validation_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeBackend
) -> None:
    monkeypatch.setattr(LndBackend, "from_paths", lambda *args, **kwargs: fake)

    with pytest.raises(LndCapabilityError):
        await initialize_channel_input_backend(
            config(tmp_path), network="regtest", offer_type="tr0absoffer"
        )

    assert fake.closed


@pytest.mark.parametrize(
    ("configured", "network", "offer_type", "message"),
    [
        (ChannelInputConfig(), "regtest", "tr0absoffer", "disabled"),
        (None, "unsupported", "tr0absoffer", "unsupported"),
        (None, "regtest", "sw0absoffer", "tr0"),
    ],
)
async def test_invalid_policy_never_builds_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: ChannelInputConfig | None,
    network: str,
    offer_type: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        LndBackend,
        "from_paths",
        lambda *args, **kwargs: pytest.fail("backend must not be built"),
    )

    with pytest.raises(ValueError, match=message):
        await initialize_channel_input_backend(
            configured or config(tmp_path), network=network, offer_type=offer_type
        )
