"""Live private-channel maintenance payment against isolated stock LND nodes."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import suppress
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from jmcore.channel_maintenance import ChannelMaintenanceConfig
from jmswap.lnd import (
    LndBackend,
    MaintenanceChannel,
    MaintenanceInvoice,
    MaintenancePayment,
    PeerRouteHop,
)
from jmswap.maintenance import MaintenanceService, enroll_channel, maintenance_status

from tests.e2e import test_cofunded_ring_e2e as ring


_LOCAL = "lnd-maker1"
_HELPER = "lnd-maker2"
_RETURN = "lnd-maker3"
_SERVICES = (_LOCAL, _HELPER, _RETURN)
_TRIANGLE = ((_LOCAL, _HELPER), (_HELPER, _RETURN), (_RETURN, _LOCAL))
_ALTERNATE_RETURN = (_LOCAL, _RETURN)
_OPEN_CHANNELS = (*_TRIANGLE, _ALTERNATE_RETURN)
_CHANNEL_CAPACITY_SAT = 2_000_000
_CHANNEL_PUSH_SAT = 1_000_000
_PAYMENT_AMOUNT_SAT = 10_000
_FUNDING_OUTPUT_BTC = 0.03


class _DispatchRecordingLndBackend(LndBackend):
    """Observe the durable payment authorization before using the real RouterRPC call."""

    _maintenance_journal_path: Path
    dispatch_intent_observed: bool
    invoice_error: str | None
    maintenance_payment_dispatches: int
    peer_requests: int
    verify_dispatch_intent: bool

    async def send_peer_message(
        self, peer_node_id: str, message_type: int, payload: bytes
    ) -> None:
        self.peer_requests += 1
        await super().send_peer_message(peer_node_id, message_type, payload)

    async def create_maintenance_invoice(
        self, amount_sat: int, hops: Sequence[PeerRouteHop], expiry_seconds: int
    ) -> MaintenanceInvoice:
        try:
            return await super().create_maintenance_invoice(
                amount_sat, hops, expiry_seconds
            )
        except Exception as exc:
            self.invoice_error = str(exc)
            raise

    async def send_maintenance_payment(
        self, payment_request: str, outgoing_channel_id: int, timeout_seconds: int
    ) -> MaintenancePayment:
        self.maintenance_payment_dispatches += 1
        if self.verify_dispatch_intent:
            document = json.loads(
                self._maintenance_journal_path.read_text(encoding="utf-8")
            )
            payment = document["payment"]
            assert isinstance(payment, dict)
            assert payment["phase"] == "dispatch_intent"
            self.dispatch_intent_observed = True
        return await super().send_maintenance_payment(
            payment_request, outgoing_channel_id, timeout_seconds
        )


class _TriangleRandom:
    """Fix the initiator's selected direction while retaining real service behavior."""

    def __init__(self, pair_index: int) -> None:
        self.pair_index = pair_index
        self.counter = 0

    def randint(self, minimum: int, maximum: int) -> int:
        if (minimum, maximum) == (0, 1):
            return self.pair_index
        return minimum

    def token_hex(self, nbytes: int) -> str:
        self.counter += 1
        return f"{self.counter:0{nbytes * 2}x}"


def _maintenance_config(interval_seconds: int) -> ChannelMaintenanceConfig:
    return ChannelMaintenanceConfig(
        interval_min_seconds=interval_seconds,
        interval_max_seconds=interval_seconds,
        amount_min_sat=_PAYMENT_AMOUNT_SAT,
        amount_max_sat=_PAYMENT_AMOUNT_SAT,
        liquidity_margin_sat=10_000,
    )


async def _list_channels() -> dict[str, list[dict[str, Any]]]:
    responses = await asyncio.gather(
        *(
            asyncio.to_thread(ring.lncli, service, "listchannels")
            for service in _SERVICES
        )
    )
    result: dict[str, list[dict[str, Any]]] = {}
    for service, response in zip(_SERVICES, responses, strict=True):
        channels = response["channels"]
        if not isinstance(channels, list):
            raise RuntimeError(f"{service} returned an invalid listchannels response")
        result[service] = channels
    return result


def _channel(
    channels: dict[str, list[dict[str, Any]]], service: str, channel_point: str
) -> dict[str, Any]:
    matching = [
        item
        for item in channels[service]
        if str(item["channel_point"]) == channel_point
    ]
    assert len(matching) == 1
    return matching[0]


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]], description: str, timeout: float = 90
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if await predicate():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"timed out waiting for {description}")
        await asyncio.sleep(0.5)


async def _maintenance_channel(
    backend: LndBackend, channel_point: str
) -> MaintenanceChannel:
    # Activation precedes the graph's receipt of both private forwarding policies.
    deadline = asyncio.get_running_loop().time() + 90
    while True:
        matching = [
            item
            for item in await backend.list_maintenance_channels()
            if item.channel_point == channel_point
        ]
        if matching:
            assert len(matching) == 1
            return matching[0]
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"private forwarding policies did not arrive for {channel_point}"
            )
        await asyncio.sleep(0.5)


async def _set_private_channel_forwarding_fee(
    backend: LndBackend, service: str, channel_point: str, base_fee_msat: int
) -> None:
    channel = await _maintenance_channel(backend, channel_point)
    await asyncio.to_thread(
        ring.lncli,
        service,
        "updatechanpolicy",
        f"--chan_point={channel_point}",
        f"--base_fee_msat={base_fee_msat}",
        "--fee_rate_ppm=0",
        f"--time_lock_delta={channel.local_cltv_delta}",
    )

    async def policy_updated() -> bool:
        updated = await _maintenance_channel(backend, channel_point)
        return (
            updated.local_base_fee_msat == base_fee_msat and updated.local_fee_ppm == 0
        )

    await _wait_until(
        policy_updated,
        f"{service} forwarding policy for {channel_point}",
    )


async def _original_return_hint_route(
    backends: dict[str, LndBackend],
    node_ids: dict[str, str],
    opened: dict[tuple[str, str], str],
) -> list[PeerRouteHop]:
    helper_channel = await _maintenance_channel(
        backends[_HELPER], opened[(_HELPER, _RETURN)]
    )
    return_channel = await _maintenance_channel(
        backends[_RETURN], opened[(_RETURN, _LOCAL)]
    )
    assert helper_channel.local_alias is not None
    assert return_channel.local_alias is not None
    return [
        PeerRouteHop(
            node_id=node_ids[_HELPER],
            channel_id=helper_channel.local_alias,
            base_fee_msat=0,
            fee_ppm=0,
            cltv_delta=helper_channel.local_cltv_delta,
        ),
        PeerRouteHop(
            node_id=node_ids[_RETURN],
            channel_id=return_channel.local_alias,
            base_fee_msat=0,
            fee_ppm=0,
            cltv_delta=return_channel.local_cltv_delta,
        ),
    ]


async def _wait_for_wallet_funding() -> str:
    addresses = await asyncio.gather(
        *(
            asyncio.to_thread(ring.lncli, service, "newaddress", "p2tr")
            for service in _SERVICES
        )
    )
    funding_addresses = [str(response["address"]) for response in addresses]
    assert all(address.startswith("bcrt1p") for address in funding_addresses)
    for address in funding_addresses:
        for _ in range(2):
            await ring.bitcoin_rpc(
                "sendtoaddress", [address, _FUNDING_OUTPUT_BTC], wallet="ring-funder"
            )
    mined = await ring.bitcoin_rpc("generatetoaddress", [6, funding_addresses[0]])
    assert len(mined) == 6

    async def funded() -> bool:
        balances = await asyncio.gather(
            *(
                asyncio.to_thread(ring.lncli, service, "walletbalance")
                for service in _SERVICES
            )
        )
        return all(
            int(balance["confirmed_balance"]) >= _CHANNEL_CAPACITY_SAT
            for balance in balances
        )

    await _wait_until(funded, "LND wallet funding")
    return funding_addresses[0]


async def _connect_private_peer(source: str, destination: str, node_id: str) -> None:
    endpoint = f"{node_id}@{ring.onion_endpoint(destination)}"
    last_error: subprocess.CalledProcessError | subprocess.TimeoutExpired | None = None
    for attempt in range(4):
        try:
            await asyncio.to_thread(
                ring.lncli,
                source,
                "connect",
                "--timeout=30s",
                endpoint,
            )
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            last_error = exc
            if "already connected" in str(exc.stderr).lower():
                return
            if attempt + 1 < 4:
                await asyncio.sleep(5)
    assert last_error is not None
    raise last_error


async def _open_channels(
    node_ids: dict[str, str], mining_address: str
) -> dict[tuple[str, str], str]:
    opened: dict[tuple[str, str], str] = {}
    for source, destination in _OPEN_CHANNELS:
        peers = await asyncio.to_thread(ring.lncli, source, "listpeers")
        if node_ids[destination] not in {peer["pub_key"] for peer in peers["peers"]}:
            await _connect_private_peer(source, destination, node_ids[destination])
        response = await asyncio.to_thread(
            ring.lncli,
            source,
            "openchannel",
            f"--node_key={node_ids[destination]}",
            f"--local_amt={_CHANNEL_CAPACITY_SAT}",
            f"--push_amt={_CHANNEL_PUSH_SAT}",
            "--private",
            "--scid_alias",
            "--channel_type=taproot",
            "--remote_csv_delay=144",
            "--max_local_csv=432",
            "--sat_per_vbyte=2",
        )
        funding_txid = str(response["funding_txid"])
        assert len(funding_txid) == 64
        pending = await asyncio.to_thread(ring.lncli, source, "pendingchannels")
        points = [
            str(item["channel"]["channel_point"])
            for item in pending["pending_open_channels"]
            if str(item["channel"]["channel_point"]).partition(":")[0] == funding_txid
        ]
        assert len(points) == 1
        opened[(source, destination)] = points[0]
        if (source, destination) == _TRIANGLE[-1]:
            # LND consumes all confirmed witness UTXOs for the first local open.
            # Confirm its change before the local node opens the alternate channel.
            mined = await ring.bitcoin_rpc("generatetoaddress", [6, mining_address])
            assert len(mined) == 6

            async def local_wallet_has_alternate_funding() -> bool:
                balance = await asyncio.to_thread(ring.lncli, _LOCAL, "walletbalance")
                return int(balance["confirmed_balance"]) >= _CHANNEL_CAPACITY_SAT

            await _wait_until(
                local_wallet_has_alternate_funding,
                "LND wallet funding for alternate return channel",
            )
            await _wait_for_active_channels(node_ids, opened)

    assert len(set(opened.values())) == len(_OPEN_CHANNELS)
    mined = await ring.bitcoin_rpc("generatetoaddress", [6, mining_address])
    assert len(mined) == 6
    return opened


async def _wait_for_active_channels(
    node_ids: dict[str, str], opened: dict[tuple[str, str], str]
) -> dict[str, list[dict[str, Any]]]:
    deadline = asyncio.get_running_loop().time() + 120
    last_channels: dict[str, list[dict[str, Any]]] = {}
    while True:
        channels = await _list_channels()
        last_channels = channels
        expected: list[dict[str, Any]] = []
        for (source, destination), channel_point in opened.items():
            for service, remote_node_id in (
                (source, node_ids[destination]),
                (destination, node_ids[source]),
            ):
                matches = [
                    item
                    for item in channels[service]
                    if str(item["channel_point"]) == channel_point
                ]
                if (
                    len(matches) == 1
                    and str(matches[0]["remote_pubkey"]) == remote_node_id
                ):
                    expected.append(matches[0])
        if len(expected) == 2 * len(opened) and all(
            item["active"]
            and item["private"]
            and item["alias_scids"]
            and item["commitment_type"] in {7, "7", "SIMPLE_TAPROOT", "TAPROOT"}
            for item in expected
        ):
            return channels
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"private alias Taproot channels did not activate: {last_channels}"
            )
        await asyncio.sleep(1)


def _enroll_triangle(
    directories: dict[str, Path],
    node_ids: dict[str, str],
    channels: dict[str, list[dict[str, Any]]],
    opened: dict[tuple[str, str], str],
) -> None:
    for source, destination in _TRIANGLE:
        channel_point = opened[(source, destination)]
        funding_txid, separator, _output_index = channel_point.partition(":")
        assert separator
        source_channel = _channel(channels, source, channel_point)
        destination_channel = _channel(channels, destination, channel_point)
        assert str(source_channel["remote_pubkey"]) == node_ids[destination]
        assert str(destination_channel["remote_pubkey"]) == node_ids[source]
        assert enroll_channel(
            directories[source],
            node_ids[source],
            node_ids[destination],
            channel_point,
            funding_txid,
        )
        assert enroll_channel(
            directories[destination],
            node_ids[destination],
            node_ids[source],
            channel_point,
            funding_txid,
        )


async def _wait_for_enrollment_and_zero_fees(
    services: dict[str, MaintenanceService],
    tasks: dict[str, asyncio.Task[None]],
    directories: dict[str, Path],
    node_ids: dict[str, str],
    channel_points: set[str],
) -> None:
    async def initialized() -> bool:
        for task in tasks.values():
            if task.done():
                task.result()
        statuses = [
            maintenance_status(directories[service], node_ids[service])
            for service in _SERVICES
        ]
        if not all(status["enrollments"] == {"enrolled": 2} for status in statuses):
            return False
        observations = await asyncio.gather(
            *(
                services[service].backend.list_maintenance_channels()
                for service in _SERVICES
            )
        )
        return all(
            len([item for item in items if item.channel_point in channel_points]) == 2
            and all(
                item.local_base_fee_msat == 0
                and item.local_fee_ppm == 0
                and item.remote_base_fee_msat == 0
                and item.remote_fee_ppm == 0
                for item in items
                if item.channel_point in channel_points
            )
            for items in observations
        )

    try:
        await _wait_until(initialized, "enrollment and zero forwarding fees")
    except RuntimeError as exc:
        statuses = {
            service: maintenance_status(directories[service], node_ids[service])
            for service in _SERVICES
        }
        raise RuntimeError(f"maintenance initialization failed: {statuses}") from exc


def _balance_snapshot(
    channels: dict[str, list[dict[str, Any]]], channel_points: set[str]
) -> dict[str, dict[str, int]]:
    return {
        service: {
            channel_point: int(
                _channel(channels, service, channel_point)["local_balance"]
            )
            for channel_point in channel_points
            if any(
                str(item["channel_point"]) == channel_point
                for item in channels[service]
            )
        }
        for service in _SERVICES
    }


async def _wait_for_balance_deltas(
    before: dict[str, dict[str, int]],
    expected: dict[str, dict[str, int]],
    channel_points: set[str],
) -> None:
    async def balances_match() -> bool:
        after = _balance_snapshot(await _list_channels(), channel_points)
        if {service: set(points) for service, points in after.items()} != {
            service: set(points) for service, points in before.items()
        }:
            return False
        observed = {
            service: {
                channel_point: after[service][channel_point]
                - before[service][channel_point]
                for channel_point in before[service]
            }
            for service in _SERVICES
        }
        return observed == expected

    await _wait_until(
        balances_match, "expected circular payment balance deltas", timeout=30
    )


async def _wait_for_succeeded_payment(
    baseline_hashes: set[str], expected_hop_node_ids: list[str]
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 60
    while True:
        response = await asyncio.to_thread(ring.lncli, _LOCAL, "listpayments")
        payments = response["payments"]
        if not isinstance(payments, list):
            raise RuntimeError("lnd-maker1 returned an invalid listpayments response")
        matches = [
            payment
            for payment in payments
            if str(payment["payment_hash"]) not in baseline_hashes
            and payment["status"] == "SUCCEEDED"
            and int(payment["value_sat"]) == _PAYMENT_AMOUNT_SAT
            and int(payment["fee_sat"]) == 0
            and int(payment["fee_msat"]) == 0
        ]
        if matches:
            assert len(matches) == 1
            payment = matches[0]
            attempts = [
                item for item in payment["htlcs"] if item["status"] == "SUCCEEDED"
            ]
            assert len(attempts) == 1
            hops = attempts[0]["route"]["hops"]
            assert len(hops) == 3
            assert [str(hop["pub_key"]) for hop in hops] == expected_hop_node_ids
            return cast(dict[str, Any], payment)
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                "maintenance service did not create a successful circular payment"
            )
        await asyncio.sleep(0.25)


async def _send_once_and_reconcile_failure(
    backend: LndBackend,
    invoice: MaintenanceInvoice,
    outgoing_channel_id: int,
    return_channel_id: int,
) -> MaintenancePayment:
    assert invoice.preimage is not None

    async def reconcile_invoice() -> None:
        assert invoice.preimage is not None
        while True:
            await backend.reconcile_maintenance_invoice(
                invoice.payment_hash, invoice.preimage, return_channel_id, 200_000
            )
            await asyncio.sleep(0.25)

    monitor = asyncio.create_task(reconcile_invoice())
    try:
        outcome = await backend.send_maintenance_payment(
            invoice.payment_request, outgoing_channel_id, timeout_seconds=10
        )
    finally:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
    deadline = asyncio.get_running_loop().time() + 30
    while outcome.status == "in_flight":
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("manual maintenance payment remained in flight")
        await asyncio.sleep(0.25)
        await backend.reconcile_maintenance_invoice(
            invoice.payment_hash, invoice.preimage, return_channel_id, 200_000
        )
        outcome = await backend.track_maintenance_payment(invoice.payment_hash)
    return outcome


async def _wait_for_failed_payment(payment_hash: str) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + 30
    while True:
        response = await asyncio.to_thread(
            ring.lncli, _LOCAL, "listpayments", "--include_incomplete"
        )
        payments = response["payments"]
        if not isinstance(payments, list):
            raise RuntimeError("lnd-maker1 returned an invalid listpayments response")
        matches = [
            payment
            for payment in payments
            if str(payment["payment_hash"]) == payment_hash
        ]
        if matches:
            assert len(matches) == 1
            payment = matches[0]
            if payment["status"] == "IN_FLIGHT":
                pass
            else:
                assert payment["status"] == "FAILED"
                attempts = payment["htlcs"]
                assert isinstance(attempts, list)
                assert len(attempts) == 1
                assert attempts[0]["status"] == "FAILED"
                return cast(dict[str, Any], payment)
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                "manual maintenance payment did not fail with one attempt"
            )
        await asyncio.sleep(0.25)


async def _wait_for_no_pending_htlcs(channel_points: set[str]) -> None:
    async def channels_settled() -> bool:
        channels = await _list_channels()
        return all(
            not _channel(channels, service, channel_point)["pending_htlcs"]
            for service in _SERVICES
            for channel_point in channel_points
            if any(
                str(item["channel_point"]) == channel_point
                for item in channels[service]
            )
        )

    await _wait_until(
        channels_settled, "maintenance payment HTLC resolution", timeout=30
    )


async def _cooperatively_close_channels(
    opened: dict[tuple[str, str], str], mining_address: str
) -> None:
    for (source, _destination), channel_point in opened.items():
        channels = await _list_channels()
        if not any(
            str(item["channel_point"]) == channel_point and item["active"]
            for item in channels[source]
        ):
            continue
        closing_txid = await asyncio.to_thread(
            ring.close_channel, source, channel_point
        )
        await ring.wait_for_mempool_transaction(closing_txid)
        mined = await ring.bitcoin_rpc("generatetoaddress", [1, mining_address])
        assert len(mined) == 1
        await ring.wait_for_closed_channel(channel_point)
    mined = await ring.bitcoin_rpc("generatetoaddress", [6, mining_address])
    assert len(mined) == 6


@pytest.mark.docker
@pytest.mark.ring_e2e
@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_private_coordination_maintenance_triangle(tmp_path: Path) -> None:
    if os.getenv("RING_E2E") != "1":
        pytest.skip("start the ring-e2e Compose profile and set RING_E2E=1")
    if os.getenv("RING_E2E_PRIVATE_COORDINATION") != "true":
        pytest.skip(
            "start the ring-e2e Compose profile with private coordination enabled"
        )

    credential_paths = [
        path
        for service in _SERVICES
        for path in (
            ring.lnd_path(service, "tls.cert"),
            ring.lnd_path(service, "admin.macaroon"),
        )
    ]
    await asyncio.gather(*(ring.wait_for_file(path) for path in credential_paths))

    backends: dict[str, LndBackend] = {}
    services: dict[str, MaintenanceService] = {}
    service_tasks: dict[str, asyncio.Task[None]] = {}
    opened: dict[tuple[str, str], str] = {}
    mining_address: str | None = None
    makers_stopped = False
    try:
        # Ring acceptors reject unrelated native opens. This explicitly enrolled
        # node-local fixture runs independently of the isolated ring producers.
        await asyncio.to_thread(
            ring.compose, "stop", "ring-maker1", "ring-maker2", "ring-maker3"
        )
        makers_stopped = True
        initiator_backend = cast(
            _DispatchRecordingLndBackend,
            _DispatchRecordingLndBackend.from_paths(
                f"127.0.0.1:{ring.LND_PORTS[_LOCAL]}",
                ring.lnd_path(_LOCAL, "tls.cert"),
                ring.lnd_path(_LOCAL, "admin.macaroon"),
                network="regtest",
            ),
        )
        backends[_LOCAL] = initiator_backend
        for service in (_HELPER, _RETURN):
            backends[service] = LndBackend.from_paths(
                f"127.0.0.1:{ring.LND_PORTS[service]}",
                ring.lnd_path(service, "tls.cert"),
                ring.lnd_path(service, "admin.macaroon"),
                network="regtest",
            )
        infos = await asyncio.gather(
            *(backends[service].node_info() for service in _SERVICES)
        )
        node_ids = {
            service: info.identity_pubkey
            for service, info in zip(_SERVICES, infos, strict=True)
        }
        assert all(info.network == "regtest" for info in infos)

        mining_address = await _wait_for_wallet_funding()
        opened = await _open_channels(node_ids, mining_address)
        channels = await _wait_for_active_channels(node_ids, opened)
        directories = {service: tmp_path / service for service in _SERVICES}
        _enroll_triangle(directories, node_ids, channels, opened)
        alternate_channel_point = opened[_ALTERNATE_RETURN]
        assert alternate_channel_point != opened[(_RETURN, _LOCAL)]
        await asyncio.gather(
            _set_private_channel_forwarding_fee(
                backends[_LOCAL], _LOCAL, alternate_channel_point, base_fee_msat=0
            ),
            _set_private_channel_forwarding_fee(
                backends[_RETURN], _RETURN, alternate_channel_point, base_fee_msat=0
            ),
        )
        for service in (_LOCAL, _RETURN):
            alternate_channel = await _maintenance_channel(
                backends[service], alternate_channel_point
            )
            assert alternate_channel.local_base_fee_msat == 0
            assert alternate_channel.local_fee_ppm == 0

        helper_config = _maintenance_config(900)
        services = {
            _LOCAL: MaintenanceService(
                initiator_backend,
                _maintenance_config(5),
                directories[_LOCAL],
                node_ids[_LOCAL],
                random_source=_TriangleRandom(
                    sorted(
                        (
                            opened[(_LOCAL, _HELPER)],
                            opened[(_RETURN, _LOCAL)],
                        )
                    ).index(opened[(_LOCAL, _HELPER)])
                ),
            ),
            _HELPER: MaintenanceService(
                backends[_HELPER],
                helper_config,
                directories[_HELPER],
                node_ids[_HELPER],
            ),
            _RETURN: MaintenanceService(
                backends[_RETURN],
                helper_config,
                directories[_RETURN],
                node_ids[_RETURN],
            ),
        }
        initiator_backend._maintenance_journal_path = (
            directories[_LOCAL] / "maintenance-v1.json"
        )
        initiator_backend.dispatch_intent_observed = False
        initiator_backend.invoice_error = None
        initiator_backend.maintenance_payment_dispatches = 0
        initiator_backend.peer_requests = 0
        initiator_backend.verify_dispatch_intent = True
        enrolled_channel_points = {opened[edge] for edge in _TRIANGLE}
        all_channel_points = set(opened.values())
        before = _balance_snapshot(await _list_channels(), all_channel_points)
        baseline = await asyncio.to_thread(ring.lncli, _LOCAL, "listpayments")
        baseline_hashes = {
            str(payment["payment_hash"]) for payment in baseline["payments"]
        }

        for service in _SERVICES:
            service_tasks[service] = asyncio.create_task(services[service].run())
        await _wait_for_enrollment_and_zero_fees(
            services, service_tasks, directories, node_ids, enrolled_channel_points
        )
        expected_enrollments = {
            service: {opened[edge] for edge in _TRIANGLE if service in edge}
            for service in _SERVICES
        }
        for service in _SERVICES:
            document = json.loads(
                (directories[service] / "maintenance-v1.json").read_text(
                    encoding="utf-8"
                )
            )
            enrollments = document["enrollments"]
            assert isinstance(enrollments, dict)
            assert set(enrollments) == expected_enrollments[service]
            assert alternate_channel_point not in enrollments
        try:
            payment = await _wait_for_succeeded_payment(
                baseline_hashes,
                [node_ids[_HELPER], node_ids[_RETURN], node_ids[_LOCAL]],
            )
        except RuntimeError as exc:
            exc.add_note(
                f"Peer requests: {initiator_backend.peer_requests}; invoice error: {initiator_backend.invoice_error}"
            )
            for name, backend in backends.items():
                exc.add_note(
                    f"{name} channels: {await backend.list_maintenance_channels()}"
                )
                exc.add_note(
                    f"{name} candidates: {await backend.list_channel_input_candidates()}"
                )
                exc.add_note(
                    f"{name} status: {maintenance_status(directories[name], node_ids[name])}"
                )
            raise
        # Exercise capability negotiation and advisory leases over real LND messages.
        # A new service conservatively quarantines remote pauses for a full minute.
        # Payment retries share the peer request budget with capability, pause, and
        # release messages. Start this phase with a fresh window on both peers.
        services[_LOCAL].config = services[_LOCAL].config.model_copy(
            update={"enabled": False}
        )
        initiator = services[_LOCAL]
        helper = services[_HELPER]
        pause_deadline = asyncio.get_running_loop().time() + 75
        while True:
            helper_state = helper._read_state()
            assert helper_state is not None
            last_sent = max(
                initiator._outgoing_request_times.get(node_ids[_HELPER], ()), default=0
            )
            last_received = max(
                helper._incoming_request_times.get(node_ids[_LOCAL], ()), default=0
            )
            if (
                helper._monotonic_now() >= helper_state.spend_pause_quarantine_until
                and initiator._now() >= last_sent + 60
                and helper._now() >= last_received + 60
            ):
                break
            if asyncio.get_running_loop().time() >= pause_deadline:
                raise RuntimeError(
                    "maintenance pause quarantine or request window did not expire"
                )
            await asyncio.sleep(1)
        pause_point = opened[(_LOCAL, _HELPER)]
        pause = await services[_LOCAL].request_spend_pause(
            node_ids[_HELPER], pause_point, 10
        )
        assert pause is not None
        assert all(
            channel.channel_point != pause_point
            for channel, _ in await helper._eligible_channels()
        )
        assert await services[_LOCAL].release_spend_pause(node_ids[_HELPER], pause)
        release_deadline = asyncio.get_running_loop().time() + 10
        while True:
            helper_state = helper._read_state()
            assert helper_state is not None
            if helper_state.remote_spend_pauses[pause.request_id].status == "released":
                break
            if asyncio.get_running_loop().time() >= release_deadline:
                raise RuntimeError(
                    "maintenance peer did not release the advisory pause"
                )
            await asyncio.sleep(0.1)
        services[_LOCAL].stop()
        await asyncio.wait_for(service_tasks[_LOCAL], timeout=10)

        assert initiator_backend.dispatch_intent_observed
        assert initiator_backend.maintenance_payment_dispatches == 1
        assert (
            maintenance_status(directories[_LOCAL], node_ids[_LOCAL])["payment_phase"]
            is None
        )
        local_outgoing = _channel(
            await _list_channels(), _LOCAL, opened[(_LOCAL, _HELPER)]
        )
        helper_outgoing = _channel(
            await _list_channels(), _HELPER, opened[(_HELPER, _RETURN)]
        )
        return_outgoing = _channel(
            await _list_channels(), _RETURN, opened[(_RETURN, _LOCAL)]
        )
        succeeded_hops = [
            item for item in payment["htlcs"] if item["status"] == "SUCCEEDED"
        ][0]["route"]["hops"]
        assert [str(hop["chan_id"]) for hop in succeeded_hops] == [
            str(local_outgoing["scid"]),
            str(helper_outgoing["alias_scids"][0]),
            str(return_outgoing["alias_scids"][0]),
        ]

        expected_deltas = {
            service: {channel_point: 0 for channel_point in before[service]}
            for service in _SERVICES
        }
        for source, destination in _TRIANGLE:
            channel_point = opened[(source, destination)]
            expected_deltas[source][channel_point] -= _PAYMENT_AMOUNT_SAT
            expected_deltas[destination][channel_point] += _PAYMENT_AMOUNT_SAT
        await _wait_for_balance_deltas(before, expected_deltas, all_channel_points)
        after = _balance_snapshot(await _list_channels(), all_channel_points)
        assert {
            service: sum(balances.values()) for service, balances in after.items()
        } == {service: sum(balances.values()) for service, balances in before.items()}

        drain_invoice = await asyncio.to_thread(
            ring.lncli, _LOCAL, "addinvoice", "--amt=900000"
        )
        drain_payment = await asyncio.to_thread(
            ring.lncli,
            _RETURN,
            "payinvoice",
            "--json",
            "--force",
            "--fee_limit=0",
            f"--outgoing_chan_id={return_outgoing['scid']}",
            str(drain_invoice["payment_request"]),
        )
        assert drain_payment["status"] == "SUCCEEDED"
        await _wait_for_no_pending_htlcs(all_channel_points)
        depleted = await _maintenance_channel(
            backends[_RETURN], opened[(_RETURN, _LOCAL)]
        )
        assert depleted.local_balance_sat < 200_000
        for service in (_LOCAL, _RETURN):
            alternate_channel = await _maintenance_channel(
                backends[service], alternate_channel_point
            )
            assert alternate_channel.local_base_fee_msat == 0
            assert alternate_channel.local_fee_ppm == 0
        manual_invoice = await initiator_backend.create_maintenance_invoice(
            200_000,
            await _original_return_hint_route(backends, node_ids, opened),
            expiry_seconds=60,
        )
        failed_before = _balance_snapshot(await _list_channels(), all_channel_points)
        initiator_backend.verify_dispatch_intent = False
        dispatches_before_manual_payment = (
            initiator_backend.maintenance_payment_dispatches
        )
        manual_outcome = await _send_once_and_reconcile_failure(
            initiator_backend,
            manual_invoice,
            int(local_outgoing["scid"]),
            depleted.channel_id,
        )
        assert (
            initiator_backend.maintenance_payment_dispatches
            == dispatches_before_manual_payment + 1
        )
        assert manual_outcome == MaintenancePayment(status="failed", fee_msat=None)
        await _wait_for_failed_payment(manual_invoice.payment_hash)
        await _wait_for_no_pending_htlcs(all_channel_points)
        no_balance_deltas = {
            service: {channel_point: 0 for channel_point in failed_before[service]}
            for service in _SERVICES
        }
        await _wait_for_balance_deltas(
            failed_before,
            no_balance_deltas,
            all_channel_points,
        )
        failed_after = _balance_snapshot(await _list_channels(), all_channel_points)
        for service in (_LOCAL, _RETURN):
            assert (
                failed_after[service][alternate_channel_point]
                == failed_before[service][alternate_channel_point]
            )
    finally:
        cleanup_failures: list[BaseException] = []
        for maintenance_service in services.values():
            maintenance_service.stop()
        task_results = await asyncio.gather(
            *service_tasks.values(), return_exceptions=True
        )
        cleanup_failures.extend(
            result for result in task_results if isinstance(result, BaseException)
        )
        if opened and mining_address is not None:
            try:
                await _cooperatively_close_channels(opened, mining_address)
            except BaseException as exc:
                cleanup_failures.append(exc)
        close_results = await asyncio.gather(
            *(backend.close() for backend in backends.values()), return_exceptions=True
        )
        cleanup_failures.extend(
            result for result in close_results if isinstance(result, BaseException)
        )
        if makers_stopped:
            try:
                await asyncio.to_thread(
                    ring.compose, "start", "ring-maker1", "ring-maker2", "ring-maker3"
                )
            except BaseException as exc:
                cleanup_failures.append(exc)
        if sys.exc_info()[0] is None and cleanup_failures:
            raise cleanup_failures[0]
