"""Ordinary CoinJoin authorization backed by durable channel-input negotiation state."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    estimate_vsize,
    scriptpubkey_to_address,
    serialize_transaction,
)
from jmcore.channel_input import (
    ChannelDescriptor,
    ChannelInputKeyPair,
    ChannelInputProposalPayload,
    ChannelInputTranscript,
    ChannelInputValidationError,
    EndpointTerms,
    InputPrevout,
    Outpoint,
    PolicyCeilings,
    SettlementOutput,
    endpoint_terms_hash,
    sign_payload,
    transcript_hash,
)
from jmcore.models import NetworkType, Offer, OfferType, calculate_cj_fee
from jmwallet.wallet.models import UTXOInfo
from test_maker_channel_input_negotiation import (
    COORDINATOR_SECRET,
    PARTICIPANT_SECRET,
    _approval_set,
    _candidate,
    _negotiation,
    _plan_freeze,
)

from maker.channel_input import MakerChannelInputNegotiation, MakerChannelInputSnapshot
from maker.channel_input_runtime import MakerChannelInputRegistry
from maker.channel_input_store import MakerChannelInputStore
from maker.coinjoin import CoinJoinSession, CoinJoinState
from maker.tx_verification import verify_unsigned_transaction

_BINDING = "ab" * 32
_WRONG_BINDING = "cd" * 32
_ORDINARY_INPUT_VALUE = 200_000
_AMOUNT = 100_000
_TXFEE = 200
_WALLET_SCRIPT = "0014" + "41" * 20
_CJ_SCRIPT = "0014" + "42" * 20
_CHANGE_SCRIPT = "0014" + "43" * 20


def _address(script: str) -> str:
    return scriptpubkey_to_address(bytes.fromhex(script), "regtest")


def _wallet_utxo(*, value: int = _ORDINARY_INPUT_VALUE, script: str = _WALLET_SCRIPT) -> UTXOInfo:
    return UTXOInfo(
        txid="90" * 32,
        vout=1,
        value=value,
        address=_address(script),
        confirmations=10,
        scriptpubkey=script,
        path="m/84'/1'/0'/0/0",
        mixdepth=0,
    )


def _approved_negotiation(
    *,
    binding: str = _BINDING,
    channels: int = 1,
    duplicate_settlement_index: bool = False,
    txfee: int = _TXFEE,
) -> tuple[MakerChannelInputNegotiation, ChannelInputProposalPayload, str]:
    base, frozen = _negotiation(channels=channels)
    negotiation = MakerChannelInputNegotiation.from_invite(
        local_actor=base.local_actor,
        coordinator_actor=base.coordinator_actor,
        invite=base.invite,
        config=base.config,
        network=base.network,
        local_node_key=base.local_node_key,
        key_pair=ChannelInputKeyPair.from_secret(PARTICIPANT_SECRET),
        ordinary_session_binding=binding,
    )
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)

    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET).public_key
    ordinary_utxo = _wallet_utxo()
    inputs = [
        TxInput.from_hex(hello.funding_outpoint.txid, hello.funding_outpoint.vout)
        for hello in negotiation.hellos
    ] + [TxInput.from_hex(ordinary_utxo.txid, ordinary_utxo.vout)]
    prevouts = [
        InputPrevout(
            outpoint=hello.funding_outpoint,
            value_sat=hello.capacity_sat,
            script_pubkey=hello.funding_script_pubkey,
        )
        for hello in negotiation.hellos
    ] + [
        InputPrevout(
            outpoint=Outpoint(txid=ordinary_utxo.txid, vout=ordinary_utxo.vout),
            value_sat=ordinary_utxo.value,
            script_pubkey=ordinary_utxo.scriptpubkey,
        )
    ]
    outputs: list[TxOutput] = []
    local_outputs: list[SettlementOutput] = []
    coordinator_outputs: list[SettlementOutput] = []
    if duplicate_settlement_index:
        outputs.append(TxOutput.from_hex(negotiation.hellos[0].settlement_script_pubkey, 50_900))
        local_outputs = [
            SettlementOutput(
                output_index=0,
                amount_sat=50_900,
                script_pubkey=hello.settlement_script_pubkey,
            )
            for hello in negotiation.hellos
        ]
        for index, hello in enumerate(negotiation.hellos, start=1):
            output = SettlementOutput(
                output_index=index,
                amount_sat=49_000,
                script_pubkey="5120" + f"{index + 70:064x}",
            )
            coordinator_outputs.append(output)
            outputs.append(TxOutput.from_hex(output.script_pubkey, output.amount_sat))
        outputs.append(TxOutput.from_hex("5120" + "ff" * 32, 50_900))
    else:
        for index, hello in enumerate(negotiation.hellos):
            local = SettlementOutput(
                output_index=len(outputs),
                amount_sat=50_900,
                script_pubkey=hello.settlement_script_pubkey,
            )
            outputs.append(TxOutput.from_hex(local.script_pubkey, local.amount_sat))
            remote = SettlementOutput(
                output_index=len(outputs),
                amount_sat=49_000,
                script_pubkey="5120" + f"{index + 70:064x}",
            )
            outputs.append(TxOutput.from_hex(remote.script_pubkey, remote.amount_sat))
            local_outputs.append(local)
            coordinator_outputs.append(remote)
    outputs.extend(
        (
            TxOutput.from_hex(_CJ_SCRIPT, _AMOUNT),
            TxOutput.from_hex(_CHANGE_SCRIPT, _ORDINARY_INPUT_VALUE - _AMOUNT - txfee),
        )
    )

    maker_terms: list[EndpointTerms] = []
    coordinator_terms: list[EndpointTerms] = []
    for index, hello in enumerate(negotiation.hellos):
        maker_terms.append(
            EndpointTerms(
                session_id=negotiation.invite.session_id,
                attempt=negotiation.invite.attempt,
                funding_outpoint=hello.funding_outpoint,
                input_index=index,
                participant_key=negotiation.participant_key,
                role="first",
                local_node_key=hello.node_key,
                peer_node_key=hello.peer_node_key,
                capacity_sat=hello.capacity_sat,
                frozen_local_claim_sat=hello.frozen_local_claim_sat,
                frozen_remote_claim_sat=hello.frozen_remote_claim_sat,
                settlement_outputs=(local_outputs[index],),
                miner_fee_share_sat=100,
                claim_adjustment_sat=1_000,
                policy_ceilings=hello.policy_ceilings,
            )
        )
        coordinator_terms.append(
            EndpointTerms(
                session_id=negotiation.invite.session_id,
                attempt=negotiation.invite.attempt,
                funding_outpoint=hello.funding_outpoint,
                input_index=index,
                participant_key=coordinator,
                role="second",
                local_node_key=hello.peer_node_key,
                peer_node_key=hello.node_key,
                capacity_sat=hello.capacity_sat,
                frozen_local_claim_sat=hello.frozen_remote_claim_sat,
                frozen_remote_claim_sat=hello.frozen_local_claim_sat,
                settlement_outputs=(coordinator_outputs[index],),
                miner_fee_share_sat=0,
                claim_adjustment_sat=-1_000,
                policy_ceilings=PolicyCeilings(
                    max_miner_fee_share_sat=1_000,
                    max_claim_adjustment_sat=1_000,
                    max_settlement_outputs=1,
                ),
            )
        )
    descriptors = tuple(
        ChannelDescriptor(
            funding_outpoint=hello.funding_outpoint,
            input_index=index,
            capacity_sat=hello.capacity_sat,
            first_participant_key=negotiation.participant_key,
            second_participant_key=coordinator,
            first_terms_hash=endpoint_terms_hash(maker_terms[index]).hex(),
            second_terms_hash=endpoint_terms_hash(coordinator_terms[index]).hex(),
        )
        for index, hello in enumerate(negotiation.hellos)
    )
    unsigned = serialize_transaction(2, inputs, outputs, 0).hex()
    total_fee = sum(item.value_sat for item in prevouts) - sum(output.value for output in outputs)
    transcript = ChannelInputTranscript(
        network="regtest",
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        unsigned_parent_tx=unsigned,
        input_prevouts=tuple(prevouts),
        total_fee_sat=total_fee,
        fee_rate_sat_vb=(
            total_fee
            + estimate_vsize(
                ["p2tr"] * channels + ["p2wpkh"],
                ["p2tr"] * (len(outputs) - 2) + ["p2wpkh", "p2wpkh"],
            )
            - 1
        )
        // estimate_vsize(
            ["p2tr"] * channels + ["p2wpkh"],
            ["p2tr"] * (len(outputs) - 2) + ["p2wpkh", "p2wpkh"],
        ),
        expiry=1_900,
        recovery_height=120,
        participant_keys=(coordinator, negotiation.participant_key),
        coordinator_key=coordinator,
        channels=descriptors,
    )
    proposal = cast(
        ChannelInputProposalPayload,
        sign_payload(
            ChannelInputProposalPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=coordinator,
                recipient_key=negotiation.participant_key,
                transcript=transcript,
                transcript_hash=transcript_hash(transcript).hex(),
                recipient_terms=tuple(maker_terms),
            ),
            COORDINATOR_SECRET,
            aux_randomness=b"\x07" * 32,
        ),
    )
    approval = negotiation.accept_proposal(
        "coordinator", proposal, current_time=1_000, current_height=100
    )
    negotiation.accept_approval_set("coordinator", _approval_set(proposal, approval))
    return negotiation, proposal, unsigned


def _session(
    *,
    cjfee: str | int = 800,
    offer_type: OfferType = OfferType.SW0_ABSOLUTE,
    txfee: int = _TXFEE,
    utxo: UTXOInfo | None = None,
) -> CoinJoinSession:
    wallet = MagicMock()
    wallet.network = "regtest"
    wallet.address_type = "p2wpkh"
    wallet.renew_coinjoin_inputs.return_value = True
    backend = MagicMock()
    backend.requires_neutrino_metadata.return_value = False
    backend.get_block_height = AsyncMock(return_value=100)
    session = CoinJoinSession(
        taker_nick="coordinator",
        offer=Offer(
            counterparty="maker",
            ordertype=offer_type,
            oid=0,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=txfee,
            cjfee=cjfee,
        ),
        wallet=wallet,
        backend=backend,
    )
    maker_utxo = utxo or _wallet_utxo()
    session.amount = _AMOUNT
    session.our_utxos = {(maker_utxo.txid, maker_utxo.vout): maker_utxo}
    session.cj_address = _address(_CJ_SCRIPT)
    session.change_address = _address(_CHANGE_SCRIPT)
    session.state = CoinJoinState.IOAUTH_SENT
    session._sign_transaction = AsyncMock(return_value=["signature"])
    return session


@pytest.mark.asyncio
async def test_channel_parent_authorizes_only_the_advertised_fee_credit() -> None:
    negotiation, _, tx_hex = _approved_negotiation()
    session = _session()

    success, response = await session.handle_tx(
        tx_hex,
        channel_input=negotiation,
        channel_input_binding=_BINDING,
    )

    assert success, response
    session._sign_transaction.assert_awaited_once_with(tx_hex)
    assert session.backend.get_block_height.await_count == 1


def test_ordinary_verification_keeps_the_default_fee_and_rejects_excess_credit() -> None:
    utxo = _wallet_utxo()
    tx_hex = serialize_transaction(
        2,
        [TxInput.from_hex(utxo.txid, utxo.vout)],
        [
            TxOutput.from_hex(_CJ_SCRIPT, _AMOUNT),
            TxOutput.from_hex(_CHANGE_SCRIPT, _ORDINARY_INPUT_VALUE - _AMOUNT - _TXFEE + 1_000),
        ],
        0,
    ).hex()
    kwargs = {
        "tx_hex": tx_hex,
        "our_utxos": {(utxo.txid, utxo.vout): utxo},
        "cj_address": _address(_CJ_SCRIPT),
        "change_address": _address(_CHANGE_SCRIPT),
        "amount": _AMOUNT,
        "cjfee": 1_000,
        "txfee": _TXFEE,
        "offer_type": OfferType.SW0_ABSOLUTE,
        "network": NetworkType.REGTEST,
    }

    assert verify_unsigned_transaction(**kwargs) == (True, "")
    valid, error = verify_unsigned_transaction(**kwargs, channel_fee_credit_sat=1_001)
    assert not valid
    assert "outside" in error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ("wrong_binding", "canceled", "parent_mutation", "wallet_prevout"),
)
async def test_channel_authorization_rejects_before_any_backend_call(failure: str) -> None:
    negotiation, proposal, tx_hex = _approved_negotiation()
    binding = _BINDING
    session = _session()
    if failure == "wrong_binding":
        binding = _WRONG_BINDING
    elif failure == "canceled":
        from test_maker_channel_input_negotiation import _cancel

        assert negotiation.accept_cancel("coordinator", _cancel(proposal)) is None
    elif failure == "parent_mutation":
        tx_hex = tx_hex[:-2] + ("00" if tx_hex[-2:] != "00" else "01")
    else:
        session = _session(utxo=_wallet_utxo(value=_ORDINARY_INPUT_VALUE + 1))

    success, response = await session.handle_tx(
        tx_hex,
        channel_input=negotiation,
        channel_input_binding=binding,
    )

    assert not success
    assert response["error"]
    session.backend.get_block_height.assert_not_awaited()
    session._sign_transaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_mode_allows_zero_and_rounded_advertised_fees() -> None:
    negotiation, _, tx_hex = _approved_negotiation(txfee=0)
    zero_fee = _session(cjfee=0, txfee=0)
    rounded_fee = _session(cjfee="0.000015", offer_type=OfferType.SW0_RELATIVE, txfee=0)

    zero_result = await zero_fee.handle_tx(
        tx_hex,
        channel_input=negotiation,
        channel_input_binding=_BINDING,
    )
    rounded_result = await rounded_fee.handle_tx(
        tx_hex,
        channel_input=negotiation,
        channel_input_binding=_BINDING,
    )

    assert zero_result[0], zero_result[1]
    assert rounded_result[0], rounded_result[1]
    assert calculate_cj_fee(OfferType.SW0_RELATIVE, "0.000015", _AMOUNT) == 2


@pytest.mark.asyncio
async def test_channel_settlement_cannot_be_counted_twice_for_fee_credit() -> None:
    negotiation, _, tx_hex = _approved_negotiation(channels=2, duplicate_settlement_index=True)
    session = _session(cjfee=1_600)

    success, response = await session.handle_tx(
        tx_hex,
        channel_input=negotiation,
        channel_input_binding=_BINDING,
    )

    assert not success
    assert "counted more than once" in response["error"]
    session.backend.get_block_height.assert_not_awaited()


class _BindingFreezeBackend:
    def __init__(self, store: MakerChannelInputStore, binding: str, snapshot: object) -> None:
        self.store = store
        self.binding = binding
        self.snapshot = snapshot
        self.freeze_called = False

    async def find_channel_input_escrow_status(
        self, session_id: str, funding_outpoint: object
    ) -> None:
        return None

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> object:
        persisted = self.store.load(session_id, 1)
        assert persisted is not None and persisted.ordinary_session_binding == self.binding
        self.freeze_called = True
        return self.snapshot


@pytest.mark.asyncio
async def test_binding_round_trips_and_is_persisted_before_freeze_rpc(tmp_path: Path) -> None:
    negotiation, frozen = _negotiation(channels=1)
    bound = MakerChannelInputNegotiation.from_invite(
        local_actor=negotiation.local_actor,
        coordinator_actor=negotiation.coordinator_actor,
        invite=negotiation.invite,
        config=negotiation.config,
        network=negotiation.network,
        local_node_key=negotiation.local_node_key,
        key_pair=ChannelInputKeyPair.from_secret(PARTICIPANT_SECRET),
        ordinary_session_binding=_BINDING,
    )
    snapshot = MakerChannelInputSnapshot.model_validate_json(bound.snapshot().model_dump_json())
    assert MakerChannelInputNegotiation.from_snapshot(snapshot).ordinary_session_binding == _BINDING
    legacy_data = snapshot.model_dump(mode="python")
    legacy_data.pop("ordinary_session_binding")
    assert MakerChannelInputSnapshot.model_validate(legacy_data).ordinary_session_binding is None

    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    registry = MakerChannelInputRegistry.load(
        store=store, config=negotiation.config, network=negotiation.network
    )
    backend = _BindingFreezeBackend(store, _BINDING, frozen[0].snapshot)
    response = await registry.handle_payload(
        negotiation.invite,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,  # type: ignore[arg-type]
        candidates=(_candidate(frozen[0]),),
        settlement_script_for=lambda candidate: frozen[0].settlement_script_pubkey,
        current_time=1_000,
        current_height=100,
        ordinary_session_binding=_BINDING,
    )

    assert response
    assert backend.freeze_called
    with pytest.raises(ChannelInputValidationError, match="ordinary session binding"):
        await registry.handle_payload(
            negotiation.invite,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,  # type: ignore[arg-type]
            candidates=(),
            settlement_script_for=lambda candidate: "",
            current_time=1_000,
            current_height=100,
            ordinary_session_binding=_WRONG_BINDING,
        )
