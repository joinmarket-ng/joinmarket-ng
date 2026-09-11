from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import jmswap.maintenance_cli as maintenance_cli
from jmswap.lnd import LndBackend
from jmswap.maintenance import MaintenanceError

NODE_ID = "02" + "11" * 32
MAKER_ONION = "a" * 56 + ".onion:9735"
TAKER_ONION = "b" * 56 + ".onion:9735"
CHANNEL_POINT = "aa" * 32 + ":1"
PAYMENT_HASH = "bb" * 32


def write_config(
    path: Path,
    *,
    maker_enabled: bool = True,
    maker_private: bool = True,
    taker_enabled: bool = True,
    taker_private: bool = True,
) -> Path:
    data_dir = path.parent / "data"
    path.write_text(
        f'''data_dir = "{data_dir}"

[network_config]
network = "regtest"

[channel_maintenance]
interval_min_seconds = "61"
interval_max_seconds = "121"
amount_min_sat = "1200"
amount_max_sat = "2200"
liquidity_margin_sat = "100"
max_spendable_percent = "5"
payment_timeout_seconds = "10"
peer_query_timeout_seconds = "3"

[maker]
offer_type = "tr0absoffer"

[maker.channel_ring]
enabled = {str(maker_enabled).lower()}
private_coordination = {str(maker_private).lower()}
maintenance_directory = "{data_dir / "maker-maintenance"}"
lnd_grpc_url = "https://127.0.0.1:10009"
lnd_tls_cert_path = "{path.parent / "maker.cert"}"
lnd_macaroon_path = "{path.parent / "maker.macaroon"}"
onion_endpoint = "{MAKER_ONION}"

[taker]
preferred_offer_type = "tr0reloffer"

[taker.channel_ring]
enabled = {str(taker_enabled).lower()}
private_coordination = {str(taker_private).lower()}
maintenance_directory = "{data_dir / "taker-maintenance"}"
lnd_grpc_url = "https://127.0.0.1:10010"
lnd_tls_cert_path = "{path.parent / "taker.cert"}"
lnd_macaroon_path = "{path.parent / "taker.macaroon"}"
onion_endpoint = "{TAKER_ONION}"
'''
    )
    return data_dir


class FakeBackend:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class FakeService:
    created: list[FakeService] = []

    def __init__(self, backend: FakeBackend, config: Any, directory: Path, node_id: str) -> None:
        self.backend = backend
        self.config = config
        self.directory = directory
        self.node_id = node_id
        self.ran = False
        self.closed = False
        self.stopped = False
        self.created.append(self)

    async def run(self) -> None:
        self.ran = True

    def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        self.closed = True
        await self.backend.close()


@pytest.mark.parametrize(
    ("role", "expected_offer_type", "expected_onion"),
    [("maker", "tr0absoffer", MAKER_ONION), ("taker", "tr0reloffer", TAKER_ONION)],
)
def test_run_selects_exact_role_network_and_coerced_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    expected_offer_type: str,
    expected_onion: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    FakeService.created.clear()
    initialized_calls: list[tuple[Any, str, str]] = []
    backend = FakeBackend()

    async def initialize(ring: Any, *, network: str, offer_type: str) -> Any:
        initialized_calls.append((ring, network, offer_type))
        return SimpleNamespace(
            backend=backend,
            node_info=SimpleNamespace(identity_pubkey=NODE_ID),
        )

    monkeypatch.setattr(maintenance_cli, "initialize_channel_ring_backend", initialize)
    monkeypatch.setattr(maintenance_cli, "MaintenanceService", FakeService)
    monkeypatch.setenv("CHANNEL_MAINTENANCE__AMOUNT_MAX_SAT", "2300")

    assert maintenance_cli.main(["--config", str(config_path), "--role", role]) == 0

    assert len(initialized_calls) == 1
    ring, network, offer_type = initialized_calls[0]
    assert network == "regtest"
    assert offer_type == expected_offer_type
    assert ring.onion_endpoint == expected_onion
    service = FakeService.created[0]
    assert service.ran and service.closed and backend.closed
    assert service.config.interval_min_seconds == 61
    assert service.config.amount_min_sat == 1200
    assert service.config.amount_max_sat == 2300
    assert all(isinstance(value, int) for value in service.config.model_dump().values())


@pytest.mark.parametrize(
    ("maker_enabled", "maker_private", "message"),
    [(False, True, "enabled"), (True, False, "private_coordination")],
)
def test_run_rejects_disabled_or_legacy_role_without_initializing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    maker_enabled: bool,
    maker_private: bool,
    message: str,
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path, maker_enabled=maker_enabled, maker_private=maker_private)

    async def initialize(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("disabled or legacy configuration must not initialize LND")

    monkeypatch.setattr(maintenance_cli, "initialize_channel_ring_backend", initialize)

    with pytest.raises(SystemExit) as exc_info:
        maintenance_cli.main(["--config", str(config_path), "run"])

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err


def test_status_reads_identity_without_initializing_or_exposing_invoice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    backend = FakeBackend()
    calls: list[tuple[str, Any]] = []

    async def node_info(*, timeout_seconds: float) -> Any:
        calls.append(("node_info", timeout_seconds))
        return SimpleNamespace(
            identity_pubkey=NODE_ID,
            advertised_uris=(f"{NODE_ID}@{MAKER_ONION}",),
            feature_bits=frozenset({46}),
        )

    backend.node_info = node_info  # type: ignore[attr-defined]
    monkeypatch.setattr(LndBackend, "from_paths", lambda *args, **kwargs: backend)

    async def initialize(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("status must not initialize channel-ring maintenance")

    monkeypatch.setattr(maintenance_cli, "initialize_channel_ring_backend", initialize)

    assert maintenance_cli.main(["--config", str(config_path), "status"]) == 0

    status = json.loads(capsys.readouterr().out)
    assert calls == [("node_info", 60.0)]
    assert backend.closed
    assert "invoice" not in status
    assert status["automatic_initiation_blocked"] is False


async def test_cancellation_closes_initialized_backend_and_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    monkeypatch.setenv("JOINMARKET_CONFIG_FILE", str(config_path))
    settings = maintenance_cli.load_settings(config_path, None)
    command = maintenance_cli.resolve_command_config(settings, "maker")
    backend = FakeBackend()
    closed: list[str] = []

    class CancelledService(FakeService):
        async def run(self) -> None:
            raise asyncio.CancelledError

        async def close(self) -> None:
            closed.append("service")
            await super().close()

    async def initialize(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(
            backend=backend,
            node_info=SimpleNamespace(identity_pubkey=NODE_ID),
        )

    monkeypatch.setattr(maintenance_cli, "initialize_channel_ring_backend", initialize)
    monkeypatch.setattr(maintenance_cli, "MaintenanceService", CancelledService)

    with pytest.raises(asyncio.CancelledError):
        await maintenance_cli.run_maintenance(command)

    assert closed == ["service"]
    assert backend.closed


def test_reconcile_admission_uses_read_only_identity_and_exact_operator_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    data_dir = write_config(config_path)
    identity_reads: list[Any] = []
    reconciliation_calls: list[tuple[Path, str, list[str], bool]] = []

    async def read_node_info(config: Any) -> Any:
        identity_reads.append(config)
        return SimpleNamespace(identity_pubkey=NODE_ID)

    def reconcile(
        directory: Path, node_id: str, points: list[str], *, confirmed_idle: bool
    ) -> None:
        reconciliation_calls.append((directory, node_id, points, confirmed_idle))

    async def initialize(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("operator reconciliation must not initialize channel-ring maintenance")

    monkeypatch.setattr(maintenance_cli, "read_node_info", read_node_info)
    monkeypatch.setattr(maintenance_cli, "reconcile_maintenance_admission", reconcile)
    monkeypatch.setattr(maintenance_cli, "initialize_channel_ring_backend", initialize)

    assert (
        maintenance_cli.main(
            [
                "--config",
                str(config_path),
                "reconcile-admission",
                "--channel-point",
                CHANNEL_POINT,
                "--channel-point",
                "cc" * 32 + ":2",
                "--confirm-idle",
            ]
        )
        == 0
    )

    assert len(identity_reads) == 1
    assert reconciliation_calls == [
        (
            data_dir / "maker-maintenance",
            NODE_ID,
            [CHANNEL_POINT, "cc" * 32 + ":2"],
            True,
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"status": "admission_reconciled"}


def test_resolve_payment_uses_read_only_identity_and_exact_operator_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    data_dir = write_config(config_path)
    identity_reads: list[Any] = []
    resolution_calls: list[tuple[Path, str, str, str, bool, int, int]] = []

    async def read_node_info(config: Any) -> Any:
        identity_reads.append(config)
        return SimpleNamespace(identity_pubkey=NODE_ID)

    def resolve(
        directory: Path,
        node_id: str,
        payment_hash: str,
        terminal_status: str,
        *,
        confirmed_no_inflight: bool,
        recorded_at: int,
        next_due: int,
    ) -> None:
        resolution_calls.append(
            (
                directory,
                node_id,
                payment_hash,
                terminal_status,
                confirmed_no_inflight,
                recorded_at,
                next_due,
            )
        )

    async def initialize(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("operator reconciliation must not initialize channel-ring maintenance")

    monkeypatch.setattr(maintenance_cli, "read_node_info", read_node_info)
    monkeypatch.setattr(maintenance_cli, "resolve_maintenance_payment", resolve)
    monkeypatch.setattr(maintenance_cli, "initialize_channel_ring_backend", initialize)
    monkeypatch.setattr(maintenance_cli.time, "time", lambda: 100.9)

    assert (
        maintenance_cli.main(
            [
                "--config",
                str(config_path),
                "resolve-payment",
                "--payment-hash",
                PAYMENT_HASH,
                "--terminal-status",
                "failed",
                "--confirm-no-inflight",
            ]
        )
        == 0
    )

    assert len(identity_reads) == 1
    assert resolution_calls == [
        (
            data_dir / "maker-maintenance",
            NODE_ID,
            PAYMENT_HASH,
            "failed",
            True,
            100,
            161,
        )
    ]
    assert json.loads(capsys.readouterr().out) == {"status": "payment_resolved"}


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["reconcile-admission", "--confirm-idle"], "--channel-point"),
        (["reconcile-admission", "--channel-point", CHANNEL_POINT], "--confirm-idle"),
        (["resolve-payment", "--terminal-status", "succeeded"], "--payment-hash"),
        (["resolve-payment", "--payment-hash", PAYMENT_HASH], "--terminal-status"),
        (
            ["resolve-payment", "--payment-hash", PAYMENT_HASH, "--terminal-status", "succeeded"],
            "--confirm-no-inflight",
        ),
        (["status", "--confirm-idle"], "require reconcile-admission or resolve-payment"),
        (
            [
                "reconcile-admission",
                "--channel-point",
                CHANNEL_POINT,
                "--confirm-idle",
                "--payment-hash",
                PAYMENT_HASH,
            ],
            "cannot be used",
        ),
    ],
)
def test_operator_arguments_fail_before_settings_or_backend_reads(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    def load_settings(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "invalid operator arguments must fail before settings or backend reads"
        )

    monkeypatch.setattr(maintenance_cli, "load_settings", load_settings)

    with pytest.raises(SystemExit) as exc_info:
        maintenance_cli.main(argv)

    assert exc_info.value.code == 2
    assert message in capsys.readouterr().err


def test_maintenance_errors_are_reported_without_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    write_config(config_path)

    async def read_node_info(_config: Any) -> Any:
        return SimpleNamespace(identity_pubkey=NODE_ID)

    def reconcile(*args: Any, **kwargs: Any) -> None:
        raise MaintenanceError("stop the maintenance service before operator reconciliation")

    monkeypatch.setattr(maintenance_cli, "read_node_info", read_node_info)
    monkeypatch.setattr(maintenance_cli, "reconcile_maintenance_admission", reconcile)

    with pytest.raises(SystemExit) as exc_info:
        maintenance_cli.main(
            [
                "--config",
                str(config_path),
                "reconcile-admission",
                "--channel-point",
                CHANNEL_POINT,
                "--confirm-idle",
            ]
        )

    assert exc_info.value.code == 2
    error = capsys.readouterr().err
    assert "stop the maintenance service" in error
    assert "Traceback" not in error
