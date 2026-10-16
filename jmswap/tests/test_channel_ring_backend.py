from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig
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
        nodes={
            "md0": ChannelRingNodeConfig(
                lnd_grpc_url="https://127.0.0.1:10009",
                lnd_tls_cert_path=cert,
                lnd_macaroon_path=macaroon,
                onion_endpoint=ENDPOINT,
            )
        },
        mixdepth_nodes={0: "md0"},
        node_binding_directory=tmp_path / "bindings",
    )


class FakeBackend:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        alias: bool = True,
        balance: int | Exception = 1_000_000,
    ) -> None:
        self.error = error
        self.alias = alias
        self.balance = balance
        self.closed = False
        self.checked_endpoint: str | None = None
        self.balance_timeout: float | None = None

    async def check_production_private_channel_ring(
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
            feature_bits=frozenset({81, 47} if self.alias else {81}),
            advertised_uris=(f"{NODE_ID}@{ENDPOINT}",),
        )

    async def confirmed_onchain_balance(self, *, timeout_seconds: float) -> int:
        self.balance_timeout = timeout_seconds
        if isinstance(self.balance, Exception):
            raise self.balance
        return self.balance

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
        config(tmp_path),
        node=config(tmp_path).nodes["md0"],
        network="regtest",
        offer_type="tr0absoffer",
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
            config(tmp_path),
            node=config(tmp_path).nodes["md0"],
            network="regtest",
            offer_type="tr0absoffer",
        )
    assert fake.closed


async def test_disabled_backend_cannot_initialize(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="disabled"):
        await initialize_channel_ring_backend(
            ChannelRingConfig(),
            node=config(tmp_path).nodes["md0"],
            network="regtest",
            offer_type="tr0absoffer",
        )


async def test_alias_capability_required_before_ring_backend_is_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeBackend(alias=False)
    monkeypatch.setattr(LndBackend, "from_paths", lambda *args, **kwargs: fake)
    ring = config(tmp_path)
    with pytest.raises(LndCapabilityError, match="SCID alias feature bit 47"):
        await initialize_channel_ring_backend(
            ring, node=ring.nodes["md0"], network="regtest", offer_type="tr0absoffer"
        )
    assert fake.closed


@pytest.mark.parametrize("balance", [0, RuntimeError("permission denied")])
async def test_initialize_warns_but_continues_without_an_onchain_reserve(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    balance: int | Exception,
) -> None:
    fake = FakeBackend(balance=balance)
    monkeypatch.setattr(LndBackend, "from_paths", lambda *args, **kwargs: fake)
    with caplog.at_level("WARNING", logger="jmswap.channel_ring"):
        initialized = await initialize_channel_ring_backend(
            config(tmp_path),
            node=config(tmp_path).nodes["md0"],
            network="regtest",
            offer_type="tr0absoffer",
        )
    assert initialized.node_info.identity_pubkey == NODE_ID
    assert not fake.closed
    assert "on-chain balance" in caplog.text or "advisory wallet target" in caplog.text
    assert fake.balance_timeout is not None and fake.balance_timeout <= 10.0


async def test_initialize_is_quiet_with_an_onchain_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(LndBackend, "from_paths", lambda *args, **kwargs: FakeBackend())
    with caplog.at_level("WARNING", logger="jmswap.channel_ring"):
        await initialize_channel_ring_backend(
            config(tmp_path),
            node=config(tmp_path).nodes["md0"],
            network="regtest",
            offer_type="tr0absoffer",
        )
    assert "fee-bump" not in caplog.text
