"""Coordinator-only rounds keep taker change ordinary and no taker LND endpoint."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from jmcore.bitcoin import address_to_scriptpubkey
from jmcore.cofunded_ring import (
    PreparedIncomingState,
    PreparedOutgoingState,
    RingCancelPayload,
    RingHelloPayload,
    RingOpenPayload,
    RingPayloadType,
    RingPreparedPayload,
    RingReadyPayload,
    RingUnsignedPayload,
    sign_payload,
)
from jmwallet.backends.base import Transaction
from test_channel_ring_coordinator import (
    MockCoordinator,
    _address,
    _funding_script,
    _harness,
    _ring_key,
    _secret,
)

from taker.channel_ring import TakerRingError
from taker.channel_ring_coordinator_store import CoordinatorPhase, CoordinatorStore
from taker.channel_ring_maker_only import (
    MakerOnlyRingCoordinator,
    reconcile_maker_only_ring_records,
)


class MockMakerOnly(MakerOnlyRingCoordinator):
    def __init__(
        self,
        *args: Any,
        malicious_ready: bool = False,
        missing_cancellation: bool = False,
        advertise_one_pending: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.peer_secrets = {f"maker-{index}": _secret(index) for index in range(1, 6)}
        self.sent_payloads: list[RingPayloadType] = []
        self.exchange_recipients: list[frozenset[str]] = []
        self.malicious_ready = malicious_ready
        self.missing_cancellation = missing_cancellation
        self.advertise_one_pending = advertise_one_pending
        self.missing_sign_ack = False

    def _peer_sign(self, nick: str, payload: RingPayloadType) -> RingPayloadType:
        return sign_payload(payload, self.peer_secrets[nick])

    def _prepared(self, key: str) -> RingPreparedPayload:
        record = self._record()
        plan = record.plans[key]
        outgoing, incoming = plan.outgoing_edge, plan.incoming_edge
        return RingPreparedPayload(
            round_nonce=record.round_nonce,
            revision=record.revision,
            signer_key=key,
            outgoing=PreparedOutgoingState(
                pending_channel_id=outgoing.pending_channel_id,
                opener_key=outgoing.opener_key,
                acceptor_key=outgoing.acceptor_key,
                script_pubkey=_funding_script(outgoing.pending_channel_id),
                capacity=outgoing.capacity,
                push_amount=outgoing.push_amount,
                policy=outgoing.policy,
            ),
            incoming=PreparedIncomingState(
                pending_channel_id=incoming.pending_channel_id,
                opener_key=incoming.opener_key,
                acceptor_key=incoming.acceptor_key,
                opener_node_id=plan.predecessor.node_id,
                capacity=incoming.capacity,
                push_amount=incoming.push_amount,
                policy=incoming.policy,
            ),
        )

    def _ready(self, nick: str, unsigned: RingUnsignedPayload) -> list[RingPayloadType]:
        # Reuse only the signed maker readiness fixture; all validation remains
        # in the real coordinator and the maker-only manifest has three edges.
        record = self._record()
        fake_record = SimpleNamespace(
            coordinator_plans=record.plans,
            round_nonce=record.round_nonce,
            revision=record.revision,
        )
        with patch.object(self, "_record", return_value=fake_record):
            return MockCoordinator._ready(self, nick, unsigned)

    async def _exchange(
        self,
        payloads: Mapping[str, RingPayloadType],
        expected_type: type[Any],
        *,
        expected_counts: int = 1,
        timeout: float | None = None,
    ) -> dict[str, list[RingPayloadType]]:
        self.exchange_recipients.append(frozenset(payloads))
        if expected_type is RingPreparedPayload:
            return {
                nick: [self._peer_sign(nick, self._prepared(key))]
                for key, nick in self._peer_by_key.items()
            }
        if expected_type is RingReadyPayload:
            return {
                nick: self._ready(nick, cast(RingUnsignedPayload, unsigned))
                for nick, unsigned in payloads.items()
            }
        if expected_type is RingOpenPayload:
            raise AssertionError("coordinator expected prepared, not open responses")
        if expected_type is RingCancelPayload:
            record = self._record()
            return {
                nick: [
                    self._peer_sign(
                        nick,
                        cast(RingCancelPayload, cancel).model_copy(
                            update={
                                "signer_key": key,
                                "canceled_pending_ids": (
                                    [record.plans[key].outgoing_edge.pending_channel_id]
                                    if key in record.prepared and not self.missing_cancellation
                                    else []
                                ),
                            }
                        ),
                    )
                ]
                for nick, cancel in payloads.items()
                for key in [
                    next(
                        (key for key, peer in self._peer_by_key.items() if peer == nick),
                        _ring_key(int(nick[-1])),
                    )
                ]
            }
        result = await MockCoordinator._exchange(
            self, payloads, expected_type, expected_counts=expected_counts, timeout=timeout
        )
        if expected_type is RingHelloPayload and self.advertise_one_pending:
            nick = next(iter(result))
            hello = cast(RingHelloPayload, result[nick][0])
            constrained = hello.participant.model_copy(
                update={
                    "backend_limits": hello.participant.backend_limits.model_copy(
                        update={"max_pending_channels": 1}
                    )
                }
            )
            result[nick] = [
                self._peer_sign(nick, hello.model_copy(update={"participant": constrained}))
            ]
        return result


def _coordinator(
    tmp_path: Path,
    *,
    mixed: bool = False,
    malicious_ready: bool = False,
    missing_cancellation: bool = False,
    advertise_one_pending: bool = False,
) -> MockMakerOnly:
    participant = _harness(tmp_path, ring_makers=3, mixed=mixed)
    session = participant.session
    session.wallet.get_new_internal_address = lambda mixdepth: _address(777 + mixdepth)
    config = participant.config.model_copy(update={"taker_participates": False})
    store = CoordinatorStore(
        participant.store.directory, network="regtest", wallet_identity="00" * 32
    )
    return MockMakerOnly(
        session,
        config=config,
        store=store,
        chain_backend=participant.chain_backend,
        session_identity="maker-only-round",
        malicious_ready=malicious_ready,
        missing_cancellation=missing_cancellation,
        advertise_one_pending=advertise_one_pending,
    )


def test_maker_only_ring_never_expands_explicit_inputs_after_fees(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator.session.strict_input_selection = True
    coordinator.session.cj_amount = coordinator.session.preselected_utxos[0].value
    coordinator._preconditions()

    with patch.object(coordinator.session.wallet, "select_utxos") as select:
        with pytest.raises(TakerRingError, match="Explicit input UTXOs are insufficient"):
            coordinator._finalize_fees_and_inputs(0)
        select.assert_not_called()
    assert not coordinator.has_durable_record
    assert coordinator.session.selected_utxos == []


@pytest.mark.parametrize("mixed", [False, True])
async def test_three_makers_open_without_taker_lnd_and_sign(mixed: bool, tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path, mixed=mixed)
    assert await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert record.phase is CoordinatorPhase.SIGNING
    assert record.sign_authorization_sent
    assert record.manifest is not None
    assert len(record.manifest.participant_keys) == 3
    assert len(record.manifest.edges) == 3
    assert len(record.manifest.outputs) == 2 * (len(coordinator.session.maker_sessions) + 1)
    assert record.signer_key not in record.manifest.participant_keys
    assert coordinator.session.taker_change_address == _address(777)
    assert address_to_scriptpubkey(_address(777)).hex() in {
        output.script_pubkey for output in record.manifest.outputs
    }
    assert len(record.readiness_set) == 6
    assert set(record.sign_ack_keys) == set(record.participant_nicks)
    assert not hasattr(coordinator, "lnd")
    if mixed:
        assert coordinator.exchange_recipients
        assert all(
            recipients == frozenset({"maker-1", "maker-2", "maker-3"})
            for recipients in coordinator.exchange_recipients
        )


async def test_invalid_remote_readiness_fails_before_sign_authorization(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path, malicious_ready=True)
    assert not await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert record.phase is CoordinatorPhase.RETIRED
    assert not record.sign_authorization_sent
    assert record.unsigned_tx is not None


async def test_empty_cancellation_after_preparation_retains_input_evidence(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path, malicious_ready=True, missing_cancellation=True)
    assert not await coordinator.prepare(_address(300), 0)
    assert coordinator._record().phase is CoordinatorPhase.RECOVERY_REQUIRED


async def test_maker_with_one_pending_slot_is_rejected_before_planning(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path, advertise_one_pending=True)
    assert not await coordinator.prepare(_address(300), 0)
    assert coordinator._record().plans == {}
    assert not coordinator._record().sign_authorization_sent


async def test_only_exact_durable_final_tx_is_rebroadcast_after_restart(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    assert await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert record.manifest is not None and record.unsigned_tx is not None
    chain = coordinator.chain_backend
    # A fully authorized but incomplete signature-collection round must not
    # broadcast solely because its journal contains the unsigned candidate.
    await reconcile_maker_only_ring_records(
        coordinator.store,
        chain,
        active_session_identities=frozenset({coordinator.session_identity}),
    )
    assert chain.broadcasts == []
    assert coordinator._record().phase is CoordinatorPhase.SIGNING

    coordinator.mark_local_signature_creation()
    coordinator.mark_local_signatures([{"txid": "01" * 32, "vout": 0, "witness": ["01"]}])
    await reconcile_maker_only_ring_records(
        coordinator.store,
        chain,
        active_session_identities=frozenset({coordinator.session_identity}),
    )
    assert chain.broadcasts == []
    coordinator.persist_final_transaction(record.unsigned_tx)
    await reconcile_maker_only_ring_records(coordinator.store, chain)
    assert chain.broadcasts == [record.unsigned_tx]

    chain.transactions[record.manifest.unsigned_txid] = Transaction(
        txid=record.manifest.unsigned_txid,
        raw=record.unsigned_tx,
        confirmations=1,
    )
    await reconcile_maker_only_ring_records(coordinator.store, chain)
    assert coordinator._record().phase is CoordinatorPhase.CONFIRMED
    assert chain.broadcasts == [record.unsigned_tx]


async def test_restart_before_unsigned_funding_escalates_without_contacting_makers(
    tmp_path: Path,
) -> None:
    coordinator = _coordinator(tmp_path)
    coordinator._preconditions()
    tx_fee, residuals = coordinator._finalize_fees_and_inputs(0)
    await coordinator._invite(tx_fee, residuals, 0)
    record = coordinator._record()
    assert record.phase is CoordinatorPhase.PLANNING
    sent_before_restart = tuple(coordinator.sent_payloads)

    await reconcile_maker_only_ring_records(coordinator.store, coordinator.chain_backend)
    stranded = coordinator._record()
    assert stranded.phase is CoordinatorPhase.RECOVERY_REQUIRED
    assert stranded.active and stranded.input_outpoints == record.input_outpoints
    assert stranded.input_lock_owner == record.input_lock_owner
    assert stranded.manifest is None and not stranded.input_signature_creation_intent
    assert coordinator.chain_backend.broadcasts == []
    assert tuple(coordinator.sent_payloads) == sent_before_restart


async def test_confirmed_exact_tx_resolves_signed_round_without_final_hex(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    assert await coordinator.prepare(_address(300), 0)
    coordinator.mark_local_signature_creation()
    coordinator.mark_local_signatures([{"txid": "01" * 32, "vout": 0, "witness": ["01"]}])
    record = coordinator._record()
    assert record.manifest is not None and record.final_tx is None
    chain = coordinator.chain_backend

    await reconcile_maker_only_ring_records(coordinator.store, chain)
    stranded = coordinator._record()
    assert stranded.phase is CoordinatorPhase.RECOVERY_REQUIRED
    assert stranded.input_signature_sent and stranded.active
    assert chain.broadcasts == []

    chain.transactions[record.manifest.unsigned_txid] = Transaction(
        txid=record.manifest.unsigned_txid,
        raw=record.unsigned_tx or "",
        confirmations=1,
    )
    await reconcile_maker_only_ring_records(coordinator.store, chain)
    recovered = coordinator._record()
    assert recovered.phase is CoordinatorPhase.CONFIRMED
    assert not recovered.active
    assert recovered.chain_status.exact_txid == record.manifest.unsigned_txid
    assert chain.broadcasts == []


async def test_preexisting_confirmation_resolves_signed_round_in_one_pass(tmp_path: Path) -> None:
    coordinator = _coordinator(tmp_path)
    assert await coordinator.prepare(_address(300), 0)
    coordinator.mark_local_signature_creation()
    coordinator.mark_local_signatures([{"txid": "01" * 32, "vout": 0, "witness": ["01"]}])
    record = coordinator._record()
    assert record.phase is CoordinatorPhase.SIGNED and record.manifest is not None
    chain = coordinator.chain_backend
    chain.transactions[record.manifest.unsigned_txid] = Transaction(
        txid=record.manifest.unsigned_txid,
        raw=record.unsigned_tx or "",
        confirmations=1,
    )

    await reconcile_maker_only_ring_records(coordinator.store, chain)
    recovered = coordinator._record()
    assert recovered.phase is CoordinatorPhase.CONFIRMED
    assert recovered.input_lock_owner == record.input_lock_owner
    assert recovered.input_outpoints == record.input_outpoints
    assert chain.broadcasts == []
