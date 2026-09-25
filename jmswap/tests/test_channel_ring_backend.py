from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jmcore.channel_ring import ChannelRingConfig
from jmcore.cofunded_ring import RingKeyPair

from jmswap.channel_ring import initialize_channel_ring_backend
from jmswap.lnd import LndBackend, LndCapabilityError, LndNodeInfo

NODE_ID = "02" + "22" * 32
ENDPOINT = "a" * 56 + ".onion:9735"


def config(tmp_path: Path) -> ChannelRingConfig:
    cert = tmp_path / "tls.cert"
    macaroon = tmp_path / "admin.macaroon"
    cert.write_bytes(b"cert")
    macaroon.write_bytes(b"macaroon")
    return ChannelRingConfig(
        enabled=True,
        lnd_grpc_url="https://127.0.0.1:10009",
        lnd_tls_cert_path=cert,
        lnd_macaroon_path=macaroon,
        onion_endpoint=ENDPOINT,
    )


class FakeBackend:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False
        self.checked_endpoint: str | None = None

    async def check_production_cofunded_channel_ring_v1(
        self, endpoint: str, *, timeout_seconds: float
    ) -> LndNodeInfo:
        self.checked_endpoint = endpoint
        if self.error is not None:
            raise self.error
        return LndNodeInfo(
            identity_pubkey=NODE_ID,
            version="0.21.1-beta",
            network="regtest",
            synced_to_chain=True,
            wallet_synced=True,
            feature_bits=frozenset({81}),
            advertised_uris=(f"{NODE_ID}@{ENDPOINT}",),
        )

    async def close(self) -> None:
        self.closed = True


async def test_initialize_returns_private_participant_limits_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend()

    def from_paths(*args: Any, **kwargs: Any) -> Any:
        assert args[0] == "127.0.0.1:10009"
        assert kwargs["network"] == "regtest"
        return fake

    monkeypatch.setattr(LndBackend, "from_paths", from_paths)
    initialized = await initialize_channel_ring_backend(
        config(tmp_path), network="regtest", offer_type="tr0absoffer"
    )
    participant = initialized.private_participant(
        RingKeyPair.from_secret(bytes.fromhex("11" * 32)).public_key
    )
    assert fake.checked_endpoint == ENDPOINT
    assert participant.node_id == NODE_ID
    assert participant.onion_endpoint == ENDPOINT
    assert participant.backend_limits.taproot is True
    assert participant.backend_limits.max_pending_channels == 2


async def test_initialize_closes_backend_and_never_returns_capability_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend(error=LndCapabilityError("identity mismatch"))
    monkeypatch.setattr(LndBackend, "from_paths", lambda *args, **kwargs: fake)
    with pytest.raises(LndCapabilityError, match="identity mismatch"):
        await initialize_channel_ring_backend(
            config(tmp_path), network="regtest", offer_type="tr0absoffer"
        )
    assert fake.closed


async def test_disabled_backend_cannot_initialize(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="disabled"):
        await initialize_channel_ring_backend(
            ChannelRingConfig(), network="regtest", offer_type="tr0absoffer"
        )
