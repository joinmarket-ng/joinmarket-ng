"""Production composition and ring-bound plan selection for channel-input escrow."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from pathlib import Path

from jmcore.paths import get_default_data_dir
from jmswap.bitcoin_core_escrow import AiohttpBitcoinCoreRpc, BitcoinCoreEscrowChainAdapter
from jmswap.bitcoin_escrow import ChannelInputEscrowTree
from jmswap.channel_escrow import (
    ChannelInputEscrowRecord,
    ChannelInputEscrowStore,
    EscrowLifecycleState,
    PrepareTimeoutRequest,
)
from jmswap.channel_funding import REQUIRED_CHANNEL_INPUT_ESCROW_CAPABILITIES
from jmswap.escrow_coordinator import ChannelInputEscrowCoordinator
from jmswap.lightning import PaymentStatus
from jmswap.lnd import LndBackend
from jmswap.lnd_escrow_payment import LndEscrowPaymentAdapter
from jmswap.peer_transport import EscrowPeerBroker, EscrowPeerJournal, EscrowPeerTransport
from jmswap.ring_authorization import RingEdgeAuthorizationStore, RingEdgeRole
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService
from loguru import logger

from maker.channel_input_escrow import ChannelInputEscrowPlan
from maker.config import MakerConfig
from maker.escrow_keys import EscrowEndpointRole, WalletEscrowKeyDeriver
from maker.escrow_runtime import (
    ChannelInputEscrowRuntime,
    EndpointEscrowRuntimePolicy,
    EndpointEscrowSessionStore,
)
from maker.success_spend_material import MakerSuccessSpendMaterialResolver
from maker.swap_adapter import WalletChainBackend


class ChannelInputEscrowDeployment:
    """Own the live escrow services and admit only finalized authorized ring edges."""

    def __init__(
        self,
        *,
        runtime: ChannelInputEscrowRuntime,
        coordinator: ChannelInputEscrowCoordinator,
        authorization_store: RingEdgeAuthorizationStore,
        endpoint_store: EndpointEscrowSessionStore,
        coordinator_store: ChannelInputEscrowStore,
        lnd: LndBackend,
        rpc: AiohttpBitcoinCoreRpc,
        policy: EndpointEscrowRuntimePolicy,
        monitor_poll_interval_seconds: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if monitor_poll_interval_seconds <= 0:
            raise ValueError("monitor_poll_interval_seconds must be positive")
        self.runtime = runtime
        self.coordinator = coordinator
        self.authorization_store = authorization_store
        self.endpoint_store = endpoint_store
        self.coordinator_store = coordinator_store
        self.lnd = lnd
        self.rpc = rpc
        self._policy = policy
        self._monitor_poll_interval_seconds = monitor_poll_interval_seconds
        self._clock = clock
        self._start_lock = asyncio.Lock()
        self._selection_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._session_monitor_tasks: dict[str, asyncio.Task[None]] = {}
        self._monitor_failures: dict[str, int] = {}
        self._monitor_store_failed = False
        self._alerted_states: set[tuple[str, EscrowLifecycleState]] = set()
        self._started = False
        self._closed = False
        self._failed = False

    async def start(self) -> None:
        """Recover finalized ring edges before allowing any new economic session."""
        async with self._start_lock:
            if self._closed or self._failed:
                raise RuntimeError("channel-input escrow deployment is unavailable")
            if self._started:
                return
            try:
                await self._reconcile_pending_ring_opens()
            except BaseException:
                self._failed = True
                raise
            edge_ids = tuple(
                sorted(
                    record.authorization.edge_id
                    for record in self.authorization_store.list()
                    if record.channel_point is not None
                    and getattr(record, "funding_completed", True)
                )
            )
            try:
                self.runtime.recover_known_finalized_ring_edges(edge_ids)
                await self.coordinator.resume_durable_sessions()
            except BaseException:
                self._failed = True
                raise
            self._started = True
            self._monitor_task = asyncio.create_task(
                self._run_monitor(), name="channel-input-escrow-monitor"
            )

    async def _reconcile_pending_ring_opens(self) -> None:
        """Finish durable cancellation work before admitting finalized ring channels."""
        for record in self.authorization_store.list():
            pending_id = getattr(record, "pending_channel_id", None)
            if pending_id is None or getattr(record, "funding_completed", False):
                continue
            edge_id = record.authorization.edge_id
            authorization_id = record.authorization.authorization_id
            if (
                not getattr(record, "funding_canceled", False)
                and record.funding_address is not None
            ):
                continue
            if not getattr(record, "funding_canceled", False):
                self.authorization_store.cancel_funding(edge_id, authorization_id)
            if not getattr(record, "funding_cancel_completed", False):
                await self.lnd.cancel_external_channel_funding(pending_id)
                self.authorization_store.complete_funding_cancellation(edge_id, authorization_id)

    @staticmethod
    def _monitoring_complete(record: ChannelInputEscrowRecord) -> bool:
        observation = record.chain_observation
        if observation is None:
            return False
        margin = record.terms.reorg_margin_blocks
        if record.state is EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED:
            return (observation.success_confirmations or 0) >= (
                record.terms.success_confirmation_depth + margin
            )
        if record.state in {
            EscrowLifecycleState.TIMEOUT_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
        }:
            return (observation.timeout_confirmations or 0) >= 1 + margin
        return False

    @classmethod
    def _should_monitor(cls, record: ChannelInputEscrowRecord) -> bool:
        if record.state in {
            EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED,
            EscrowLifecycleState.COMMITMENT_CONFLICT,
            EscrowLifecycleState.CROSS_LAYER_LOSS,
            EscrowLifecycleState.ABORTED,
        }:
            return False
        if record.state is EscrowLifecycleState.NEGOTIATED:
            return record.negotiated_parent_evidence is not None
        return not cls._monitoring_complete(record)

    async def _advance_parent_confirmed(
        self, record: ChannelInputEscrowRecord
    ) -> ChannelInputEscrowRecord:
        record = await self.coordinator.observe_chain(record.session_id)
        if record.state is EscrowLifecycleState.PARENT_REORGED:
            return await self._advance_reorg(record)
        if record.state is not EscrowLifecycleState.PARENT_CONFIRMED:
            return record
        observation = record.chain_observation
        if observation is None:
            raise RuntimeError("confirmed escrow parent lacks chain evidence")
        if record.payment_request is not None:
            request = record.payment_request
            return await self.coordinator.start_payment(
                record.session_id,
                request.excluded_channel_ids,
                request.requested_at_unix,
            )
        if observation.observed_height > record.terms.timeout_height:
            return await self.coordinator.broadcast_timeout(record.session_id)
        requested_at_unix = int(self._clock())
        if requested_at_unix >= record.terms.invoice_expiry_unix:
            return record
        payment_deadline = min(
            record.terms.timeout_height - record.terms.safety_delta_blocks,
            record.terms.success_broadcast_deadline - record.terms.preimage_budget_blocks,
        )
        if (
            payment_deadline - observation.observed_height
            < record.terms.invoice_min_final_cltv_delta
        ):
            return record
        return await self.coordinator.start_payment(
            record.session_id,
            (record.terms.snapshot.channel_id,),
            requested_at_unix,
        )

    async def _advance_payment(self, record: ChannelInputEscrowRecord) -> ChannelInputEscrowRecord:
        record = await self.coordinator.observe_chain(record.session_id)
        if record.state is EscrowLifecycleState.PARENT_REORGED:
            return await self._advance_reorg(record)
        if record.state is not EscrowLifecycleState.PAYMENT_IN_FLIGHT:
            return record
        result = record.payment_result
        if result is None:
            raise RuntimeError("in-flight escrow lacks payment evidence")
        if result.status is PaymentStatus.PENDING:
            record = await self.coordinator.observe_payment(record.session_id)
            if record.state is not EscrowLifecycleState.PAYMENT_IN_FLIGHT:
                return record
            result = record.payment_result
        if result is not None and result.status is PaymentStatus.SUCCEEDED:
            return await self.coordinator.ensure_success_spend(record.session_id)
        observation = record.chain_observation
        if (
            result is not None
            and result.status is PaymentStatus.FAILED
            and observation is not None
            and observation.observed_height > record.terms.timeout_height
        ):
            return await self.coordinator.broadcast_timeout(record.session_id)
        return record

    async def _advance_success(self, record: ChannelInputEscrowRecord) -> ChannelInputEscrowRecord:
        record = await self.coordinator.observe_chain(record.session_id)
        if record.state is EscrowLifecycleState.PARENT_REORGED:
            return await self._advance_reorg(record)
        if record.state is not EscrowLifecycleState.SUCCESS_SPEND_PENDING:
            return record
        observation = record.chain_observation
        spend = record.success_spend
        if (
            observation is not None
            and spend is not None
            and observation.observed_height > spend.broadcast_height
            and observation.observed_height <= record.terms.success_broadcast_deadline
        ):
            return await self.coordinator.fee_bump_success(record.session_id)
        return record

    async def _advance_reorg(self, record: ChannelInputEscrowRecord) -> ChannelInputEscrowRecord:
        if (
            record.state_before_reorg is EscrowLifecycleState.PAYMENT_IN_FLIGHT
            and record.payment_result is not None
            and record.payment_result.status is PaymentStatus.PENDING
        ):
            record = await self.coordinator.observe_payment(record.session_id)
            if record.state is not EscrowLifecycleState.PARENT_REORGED:
                return record
        return await self.coordinator.recover_reorg(record.session_id)

    async def _monitor_session_once(self, record: ChannelInputEscrowRecord) -> None:
        session_id = record.session_id
        state = record.state
        if state is EscrowLifecycleState.NEGOTIATED:
            evidence = record.negotiated_parent_evidence
            if evidence is not None:
                await self.coordinator.prepare_timeout(
                    PrepareTimeoutRequest(terms=record.terms, parent_evidence=evidence)
                )
        elif state is EscrowLifecycleState.TIMEOUT_SIGNED:
            await self.coordinator.authorize_parent(session_id)
        elif state is EscrowLifecycleState.PARENT_SIGNED:
            await self.coordinator.broadcast_parent(session_id)
        elif state is EscrowLifecycleState.PARENT_PENDING:
            await self.coordinator.observe_chain(session_id)
        elif state is EscrowLifecycleState.PARENT_CONFIRMED:
            await self._advance_parent_confirmed(record)
        elif state is EscrowLifecycleState.PAYMENT_IN_FLIGHT:
            await self._advance_payment(record)
        elif state is EscrowLifecycleState.SUCCESS_SPEND_PENDING:
            await self._advance_success(record)
        elif state in {
            EscrowLifecycleState.TIMEOUT_PENDING,
            EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
            EscrowLifecycleState.TIMEOUT_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
        }:
            await self.coordinator.observe_chain(session_id)
        elif state is EscrowLifecycleState.PARENT_REORGED:
            await self._advance_reorg(record)

    def _reap_monitor_tasks(self) -> None:
        for session_id, task in tuple(self._session_monitor_tasks.items()):
            if not task.done():
                continue
            del self._session_monitor_tasks[session_id]
            if task.cancelled():
                continue
            error = task.exception()
            if error is None:
                if self._monitor_failures.pop(session_id, None) is not None:
                    logger.info("channel-input escrow monitor recovered for {}", session_id)
                continue
            failures = self._monitor_failures.get(session_id, 0) + 1
            self._monitor_failures[session_id] = failures
            if failures == 1 or failures & (failures - 1) == 0:
                logger.error(
                    "channel-input escrow monitor failure {} for {}: {}",
                    failures,
                    session_id,
                    error,
                )

    def _alert_record_state(self, record: ChannelInputEscrowRecord) -> None:
        alert_key = (record.session_id, record.state)
        if alert_key in self._alerted_states:
            return
        if record.state in {
            EscrowLifecycleState.COMMITMENT_CONFLICT,
            EscrowLifecycleState.CROSS_LAYER_LOSS,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
        }:
            logger.error(
                "channel-input escrow session {} entered {}",
                record.session_id,
                record.state,
            )
            self._alerted_states.add(alert_key)
        elif record.peer_settle_deadline_violated:
            logger.warning(
                "channel-input escrow peer settled after its deadline for {}",
                record.session_id,
            )
            self._alerted_states.add(alert_key)

    def _schedule_monitor_pass(self) -> None:
        self._reap_monitor_tasks()
        for record in sorted(self.coordinator_store.list(), key=lambda item: item.session_id):
            self._alert_record_state(record)
            if record.session_id in self._session_monitor_tasks or not self._should_monitor(record):
                continue
            self._session_monitor_tasks[record.session_id] = asyncio.create_task(
                self._monitor_session_once(record),
                name=f"channel-input-escrow-{record.session_id}",
            )

    async def _run_monitor(self) -> None:
        try:
            while True:
                try:
                    self._schedule_monitor_pass()
                except Exception as exc:  # noqa: BLE001 - keep recovery monitoring alive
                    if not self._monitor_store_failed:
                        logger.error("channel-input escrow store monitor failed: {}", exc)
                    self._monitor_store_failed = True
                else:
                    if self._monitor_store_failed:
                        logger.info("channel-input escrow store monitor recovered")
                    self._monitor_store_failed = False
                await asyncio.sleep(self._monitor_poll_interval_seconds)
        finally:
            tasks = tuple(self._session_monitor_tasks.values())
            self._session_monitor_tasks.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def plan_for_authenticated_fill(self) -> ChannelInputEscrowPlan | None:
        """Freeze and negotiate one deterministic, unused authorized funder edge."""
        async with self._selection_lock:
            if (
                not self._started
                or self._closed
                or self._failed
                or self._monitor_store_failed
                or self._monitor_failures
            ):
                return None
            records = sorted(
                self.authorization_store.list(), key=lambda item: item.authorization.edge_id
            )
            for record in records:
                authorization = record.authorization
                edge_id = authorization.edge_id
                if record.channel_point is None:
                    continue
                if not getattr(record, "funding_completed", True):
                    continue
                if authorization.role_for(record.local_nick) is not RingEdgeRole.FUNDER:
                    continue
                if authorization.capacity_sats < self._policy.escrow_value_sats:
                    continue
                if self._policy.escrow_value_sats <= (
                    authorization.receiver_contribution_sats
                    + self._policy.timeout_compensation_sats
                    + self._policy.timeout_fee_sats
                ):
                    continue
                if self.endpoint_store.load(edge_id) is not None:
                    continue
                if self.coordinator_store.load(edge_id) is not None:
                    continue

                channel_point = record.channel_point
                await self.runtime.initiate(edge_id, f"{channel_point.txid}:{channel_point.vout}")
                terms = await self.runtime.complete_maker_handshake(edge_id)
                endpoint = self.endpoint_store.load(edge_id)
                if (
                    endpoint is None
                    or endpoint.role is not EscrowEndpointRole.MAKER
                    or endpoint.remote_keys is None
                    or endpoint.terms != terms
                    or endpoint.terms.snapshot != endpoint.snapshot
                ):
                    raise RuntimeError("maker escrow handshake was not durably verified")
                if (
                    endpoint.snapshot.funding_outpoint.txid != channel_point.txid
                    or endpoint.snapshot.funding_outpoint.vout != channel_point.vout
                    or endpoint.snapshot.capacity_sats != authorization.capacity_sats
                ):
                    raise RuntimeError(
                        "maker escrow handshake conflicts with finalized ring channel"
                    )
                tree = ChannelInputEscrowTree(
                    maker_escrow_pubkey=bytes.fromhex(endpoint.local_keys.escrow_pubkey),
                    peer_escrow_pubkey=bytes.fromhex(endpoint.remote_keys.escrow_pubkey),
                    maker_claim_pubkey=bytes.fromhex(endpoint.local_keys.maker_claim_pubkey or ""),
                    payment_hash=bytes.fromhex(terms.payment_hash),
                )
                return ChannelInputEscrowPlan(
                    terms=terms,
                    escrow_scriptpubkey=tree.funding_output_script().hex(),
                    coordinator=self.coordinator,
                )
            return None

    async def close(self) -> None:
        """Stop peer delivery before closing the Lightning and RPC clients."""
        async with self._start_lock:
            if self._closed:
                return
            self._closed = True
            try:
                monitor = self._monitor_task
                self._monitor_task = None
                if monitor is not None:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                await self.runtime.close()
            finally:
                try:
                    await self.lnd.close()
                finally:
                    await self.rpc.close()


def _journal_directories(data_dir: Path) -> dict[str, Path]:
    root = data_dir / "channel_input_escrow"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    names = ("endpoint", "coordinator", "peer", "chain", "payment")
    directories = {name: root / name for name in names}
    for directory in directories.values():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    return directories


def _required_rpc_config(config: MakerConfig) -> tuple[str, str, str]:
    if config.backend_type.lower() != "descriptor_wallet":
        raise ValueError("channel-input escrow requires a descriptor wallet backend")
    values = tuple(
        config.backend_config.get(name) for name in ("rpc_url", "rpc_user", "rpc_password")
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError(
            "channel-input escrow requires backend_config rpc_url, rpc_user, and rpc_password"
        )
    return values  # type: ignore[return-value]


async def build_channel_input_escrow_deployment(
    wallet: WalletService,
    backend: BlockchainBackend,
    config: MakerConfig,
    ring_store: RingEdgeAuthorizationStore,
    lnd: LndBackend | None = None,
) -> ChannelInputEscrowDeployment:
    """Build a complete production escrow deployment, closing partial resources on failure."""
    if not config.channel_input_escrow.enabled:
        raise ValueError("channel-input escrow is not enabled")
    if not isinstance(ring_store, RingEdgeAuthorizationStore):
        raise TypeError("ring_store must be a RingEdgeAuthorizationStore")
    rpc_url, rpc_user, rpc_password = _required_rpc_config(config)
    swap = config.swap_provider
    lnd_grpc_url = swap.lnd_grpc_url
    lnd_tls_cert_path = swap.lnd_tls_cert_path
    lnd_macaroon_path = swap.lnd_macaroon_path
    lnd_escrow_macaroon_path = swap.lnd_escrow_macaroon_path
    required_lnd = (lnd_grpc_url, lnd_tls_cert_path, lnd_macaroon_path, lnd_escrow_macaroon_path)
    if not all(isinstance(value, str) and value for value in required_lnd):
        raise ValueError("channel-input escrow requires a fully configured LND backend")
    assert isinstance(lnd_grpc_url, str)
    assert isinstance(lnd_tls_cert_path, str)
    assert isinstance(lnd_macaroon_path, str)
    assert isinstance(lnd_escrow_macaroon_path, str)

    rpc = AiohttpBitcoinCoreRpc(rpc_url, rpc_user, rpc_password)
    active_lnd = lnd
    try:
        if active_lnd is None:
            active_lnd = LndBackend.from_paths(
                lnd_grpc_url,
                lnd_tls_cert_path,
                lnd_macaroon_path,
                network=str(config.network.value),
                escrow_macaroon_path=lnd_escrow_macaroon_path,
            )
        if not isinstance(active_lnd, LndBackend):
            raise TypeError("lnd must be an LndBackend")
        data_dir = Path(config.data_dir) if config.data_dir is not None else get_default_data_dir()
        directories = _journal_directories(data_dir)
        policy = EndpointEscrowRuntimePolicy(
            premium_sats=config.channel_input_escrow.premium_sats,
            timeout_compensation_sats=config.channel_input_escrow.timeout_compensation_sats,
            escrow_value_sats=config.channel_input_escrow.escrow_value_sats,
            timeout_fee_sats=config.channel_input_escrow.timeout_fee_sats,
            invoice_expiry_seconds=config.channel_input_escrow.invoice_expiry_seconds,
            invoice_min_final_cltv_delta=config.channel_input_escrow.invoice_min_final_cltv_delta,
            peer_settle_delta_blocks=config.channel_input_escrow.peer_settle_delta_blocks,
            success_broadcast_delta_blocks=config.channel_input_escrow.success_broadcast_delta_blocks,
            timeout_delta_blocks=config.channel_input_escrow.timeout_delta_blocks,
            parent_confirmation_depth=config.channel_input_escrow.parent_confirmation_depth,
            success_confirmation_depth=config.channel_input_escrow.success_confirmation_depth,
            success_confirmation_budget_blocks=config.channel_input_escrow.success_confirmation_budget_blocks,
            reorg_margin_blocks=config.channel_input_escrow.reorg_margin_blocks,
            safety_delta_blocks=config.channel_input_escrow.safety_delta_blocks,
            preimage_budget_blocks=config.channel_input_escrow.preimage_budget_blocks,
            peer_message_timeout_seconds=config.channel_input_escrow.peer_message_timeout_seconds,
        )
        endpoint_store = EndpointEscrowSessionStore(directories["endpoint"])
        coordinator_store = ChannelInputEscrowStore(directories["coordinator"])
        key_deriver = WalletEscrowKeyDeriver(wallet)
        height_source = WalletChainBackend(backend)
        chain = BitcoinCoreEscrowChainAdapter(
            rpc,
            MakerSuccessSpendMaterialResolver(endpoint_store, key_deriver),
            directories["chain"],
            fee_target_blocks=config.channel_input_escrow.fee_target_blocks,
            minimum_feerate_sat_vb=config.channel_input_escrow.minimum_feerate_sat_vb,
        )
        payment = LndEscrowPaymentAdapter(
            active_lnd,
            height_source,
            directories["payment"],
            fee_limit_msat=config.channel_input_escrow.payment_fee_limit_msat,
            timeout_seconds=config.channel_input_escrow.payment_timeout_seconds,
        )
        local_node_id = await active_lnd.node_id()
        transport = EscrowPeerTransport(
            active_lnd, ring_store, EscrowPeerJournal(directories["peer"]), local_node_id
        )
        runtime = ChannelInputEscrowRuntime(
            lnd=active_lnd,
            key_deriver=key_deriver,
            broker=EscrowPeerBroker(transport),
            chain=chain,
            payment=payment,
            height_source=height_source,
            policy=policy,
            endpoint_store=endpoint_store,
            coordinator_store=coordinator_store,
        )
        runtime.channel_input_escrow_capabilities = REQUIRED_CHANNEL_INPUT_ESCROW_CAPABILITIES
        coordinator = ChannelInputEscrowCoordinator(runtime, coordinator_store)
        return ChannelInputEscrowDeployment(
            runtime=runtime,
            coordinator=coordinator,
            authorization_store=ring_store,
            endpoint_store=endpoint_store,
            coordinator_store=coordinator_store,
            lnd=active_lnd,
            rpc=rpc,
            policy=policy,
            monitor_poll_interval_seconds=config.channel_input_escrow.monitor_poll_interval_seconds,
        )
    except BaseException:
        if active_lnd is not None:
            with contextlib.suppress(Exception):
                await active_lnd.close()
        with contextlib.suppress(Exception):
            await rpc.close()
        raise
