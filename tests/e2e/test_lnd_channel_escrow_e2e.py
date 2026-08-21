"""Live exact-parent restart recovery against two patched LND nodes."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    address_to_scriptpubkey,
    analyze_coinjoin_outputs,
    estimate_vsize,
    get_txid,
    parse_transaction,
    scriptpubkey_to_address,
    serialize_transaction,
    taproot_tweak_privkey,
)
from jmcore.channel_input import (
    ChannelDescriptor,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputKeyPair,
    ChannelInputPreparePayload,
    ChannelInputTranscript,
    EndpointTerms,
    InputPrevout,
    Outpoint,
    PolicyCeilings,
    SettlementOutput as AuthorizedSettlementOutput,
    approval_set_hash,
    endpoint_terms_hash,
    sign_payload,
    transcript_hash,
)
from jmswap.lnd import (
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndBackend,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
    WitnessUtxo,
)
from jmwallet.wallet.signing import sign_p2tr_input, verify_p2tr_signature
from taker.channel_input_parent import (
    ChannelContribution,
    ChannelInputEndpoint,
    ChannelInputParentCoordinator,
    ChannelInputParentError,
    ChannelInputParentPlan,
    ChannelInputParentState,
    ChannelInputAuthorization,
    ChainTransaction,
    EndpointContribution,
    OrdinaryWitness,
    ParentPrevout,
    SettlementOutput,
)

from tests.e2e.test_lnd_external_channel_e2e import (
    DATA_DIR,
    FUNDEE_GRPC,
    OPENER_GRPC,
    bitcoin_rpc,
    core_wallet,
    wait_for_file,
)

pytestmark = [pytest.mark.docker, pytest.mark.lnd_channel_escrow]

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "jmswap/docker-compose.lnd-external.yml"
FIRST_PARTICIPANT_SECRET = b"\x41" * 32
SECOND_PARTICIPANT_SECRET = b"\x42" * 32


def _participant_key(secret: bytes) -> str:
    return ChannelInputKeyPair.from_secret(secret).public_key


def _authorization(
    *,
    session_id: str,
    unsigned_parent_hex: str,
    opener_snapshot: FrozenChannelSnapshot,
    fundee_snapshot: FrozenChannelSnapshot,
    ordinary_value: int,
    ordinary_script: bytes,
    ordinary_outpoint: Outpoint,
    outputs: list[TxOutput],
) -> ChannelInputAuthorization:
    first_participant_key = _participant_key(FIRST_PARTICIPANT_SECRET)
    second_participant_key = _participant_key(SECOND_PARTICIPANT_SECRET)
    ceilings = PolicyCeilings(
        max_miner_fee_share_sat=500,
        max_claim_adjustment_sat=0,
        max_settlement_outputs=2,
    )
    first_terms = EndpointTerms(
        session_id=session_id,
        attempt=0,
        funding_outpoint=Outpoint(
            txid=opener_snapshot.funding_outpoint.txid,
            vout=opener_snapshot.funding_outpoint.vout,
        ),
        input_index=0,
        participant_key=first_participant_key,
        role="first",
        local_node_key=fundee_snapshot.peer_node_pubkey.hex(),
        peer_node_key=opener_snapshot.peer_node_pubkey.hex(),
        capacity_sat=opener_snapshot.capacity_sat,
        frozen_local_claim_sat=opener_snapshot.local_claim_sat,
        frozen_remote_claim_sat=opener_snapshot.remote_claim_sat,
        settlement_outputs=tuple(
            AuthorizedSettlementOutput(
                output_index=index,
                amount_sat=outputs[index].value,
                script_pubkey=outputs[index].script.hex(),
            )
            for index in (0, 3)
        ),
        miner_fee_share_sat=500,
        claim_adjustment_sat=0,
        policy_ceilings=ceilings,
    )
    second_terms = EndpointTerms(
        session_id=session_id,
        attempt=0,
        funding_outpoint=Outpoint(
            txid=fundee_snapshot.funding_outpoint.txid,
            vout=fundee_snapshot.funding_outpoint.vout,
        ),
        input_index=0,
        participant_key=second_participant_key,
        role="second",
        local_node_key=opener_snapshot.peer_node_pubkey.hex(),
        peer_node_key=fundee_snapshot.peer_node_pubkey.hex(),
        capacity_sat=fundee_snapshot.capacity_sat,
        frozen_local_claim_sat=fundee_snapshot.local_claim_sat,
        frozen_remote_claim_sat=fundee_snapshot.remote_claim_sat,
        settlement_outputs=tuple(
            AuthorizedSettlementOutput(
                output_index=index,
                amount_sat=outputs[index].value,
                script_pubkey=outputs[index].script.hex(),
            )
            for index in (1, 4)
        ),
        miner_fee_share_sat=500,
        claim_adjustment_sat=0,
        policy_ceilings=ceilings,
    )
    fee_sat = (
        opener_snapshot.capacity_sat
        + ordinary_value
        - sum(output.value for output in outputs)
    )
    transcript = ChannelInputTranscript(
        network="regtest",
        session_id=session_id,
        attempt=0,
        unsigned_parent_tx=unsigned_parent_hex,
        input_prevouts=(
            InputPrevout(
                outpoint=first_terms.funding_outpoint,
                value_sat=opener_snapshot.capacity_sat,
                script_pubkey=opener_snapshot.funding_scriptpubkey.hex(),
            ),
            InputPrevout(
                outpoint=ordinary_outpoint,
                value_sat=ordinary_value,
                script_pubkey=ordinary_script.hex(),
            ),
        ),
        total_fee_sat=fee_sat,
        fee_rate_sat_vb=(fee_sat + estimate_vsize(["p2tr", "p2tr"], ["p2tr"] * 6) - 1)
        // estimate_vsize(["p2tr", "p2tr"], ["p2tr"] * 6),
        expiry=1_900_000_000,
        recovery_height=999_999,
        participant_keys=(first_participant_key, second_participant_key),
        coordinator_key=first_participant_key,
        channels=(
            ChannelDescriptor(
                funding_outpoint=first_terms.funding_outpoint,
                input_index=0,
                capacity_sat=opener_snapshot.capacity_sat,
                first_participant_key=first_participant_key,
                second_participant_key=second_participant_key,
                first_terms_hash=endpoint_terms_hash(first_terms).hex(),
                second_terms_hash=endpoint_terms_hash(second_terms).hex(),
            ),
        ),
    )
    participants = (
        (first_participant_key, FIRST_PARTICIPANT_SECRET),
        (second_participant_key, SECOND_PARTICIPANT_SECRET),
    )
    approvals = tuple(
        cast(
            ChannelInputApprovalPayload,
            sign_payload(
                ChannelInputApprovalPayload(
                    session_id=session_id,
                    attempt=0,
                    signer_key=participant_key,
                    transcript_hash=transcript_hash(transcript).hex(),
                    terms_hashes=tuple(
                        sorted(transcript.terms_hashes_for(participant_key))
                    ),
                ),
                secret,
                aux_randomness=secret,
            ),
        )
        for participant_key, secret in participants
    )
    approval_set = cast(
        ChannelInputApprovalSetPayload,
        sign_payload(
            ChannelInputApprovalSetPayload(
                session_id=session_id,
                attempt=0,
                signer_key=first_participant_key,
                transcript=transcript,
                approvals=approvals,
            ),
            FIRST_PARTICIPANT_SECRET,
            aux_randomness=b"\x43" * 32,
        ),
    )
    acks = tuple(
        cast(
            ChannelInputApprovalSetAckPayload,
            sign_payload(
                ChannelInputApprovalSetAckPayload(
                    session_id=session_id,
                    attempt=0,
                    signer_key=participant_key,
                    transcript_hash=transcript_hash(transcript).hex(),
                    approval_set_hash=approval_set_hash(approval_set).hex(),
                ),
                secret,
                aux_randomness=bytes([index]) * 32,
            ),
        )
        for index, (participant_key, secret) in enumerate(participants, start=0x44)
    )
    prepare = cast(
        ChannelInputPreparePayload,
        sign_payload(
            ChannelInputPreparePayload(
                session_id=session_id,
                attempt=0,
                signer_key=first_participant_key,
                transcript_hash=transcript_hash(transcript).hex(),
                approval_set_hash=approval_set_hash(approval_set).hex(),
                approval_set=approval_set,
                approval_set_acks=acks,
            ),
            FIRST_PARTICIPANT_SECRET,
            aux_randomness=b"\x46" * 32,
        ),
    )
    return ChannelInputAuthorization(
        prepare=prepare, endpoint_terms=(first_terms, second_terms)
    )


async def compose(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "--profile",
        "lnd_external",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.decode().strip())
    return stdout.decode().strip()


async def lncli(service: str, *args: str) -> dict[str, Any]:
    output = await compose("exec", "-T", service, "lncli", "--network=regtest", *args)
    result = json.loads(output)
    if not isinstance(result, dict):
        raise RuntimeError(f"unexpected lncli response: {result!r}")
    return result


def escrow_backend(node: str, host: str) -> LndBackend:
    chain_dir = DATA_DIR / node / "data/chain/bitcoin/regtest"
    return LndBackend.from_paths(
        host,
        DATA_DIR / node / "tls.cert",
        chain_dir / "admin.macaroon",
        escrow_macaroon_path=chain_dir / "channelescrow.macaroon",
    )


async def wait_for_node(backend: LndBackend, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = await backend.node_info(timeout_seconds=2)
            if info.synced_to_chain and info.wallet_synced:
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError("LND did not become synchronized")


async def mine(wallet: str, blocks: int) -> list[str]:
    address = await bitcoin_rpc("getnewaddress", ["", "bech32m"], wallet=wallet)
    result = await bitcoin_rpc("generatetoaddress", [blocks, address])
    return [str(block_hash) for block_hash in result]


async def wait_for_active_channel(timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        channels = (await lncli("lnd-opener", "listchannels"))["channels"]
        if channels and channels[0]["active"]:
            return dict(channels[0])
        await asyncio.sleep(0.5)
    raise RuntimeError("final-Taproot channel did not become active")


async def wait_for_resolved_channel(timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        opener, fundee = await asyncio.gather(
            lncli("lnd-opener", "pendingchannels"),
            lncli("lnd-fundee", "pendingchannels"),
        )
        if (
            not (await lncli("lnd-opener", "listchannels"))["channels"]
            and not (await lncli("lnd-fundee", "listchannels"))["channels"]
            and all(
                not state[key]
                for state in (opener, fundee)
                for key in (
                    "pending_open_channels",
                    "pending_closing_channels",
                    "pending_force_closing_channels",
                    "waiting_close_channels",
                )
            )
        ):
            return
        await asyncio.sleep(0.5)
    raise RuntimeError("confirmed exact parent did not resolve the channel")


@dataclass(frozen=True)
class CoreTransaction(ChainTransaction):
    txid: str
    raw: str
    confirmations: int


class CoreChain:
    async def get_transaction(self, txid: str) -> ChainTransaction | None:
        try:
            transaction = await bitcoin_rpc("getrawtransaction", [txid, True])
        except RuntimeError as exc:
            message = str(exc)
            if (
                "-5" in message
                or "No such mempool or blockchain transaction" in message
            ):
                return None
            raise
        return CoreTransaction(
            txid=str(transaction["txid"]),
            raw=str(transaction["hex"]),
            confirmations=int(transaction.get("confirmations", 0)),
        )

    async def broadcast_transaction(self, tx_hex: str) -> str:
        return str(await bitcoin_rpc("sendrawtransaction", [tx_hex]))


class SimulatedCoordinatorInterruption(ChannelInputParentError):
    """Raised after LND has durably signed but before the coordinator saves it."""


class RestartingOpenerEndpoint:
    def __init__(self, backend: LndBackend) -> None:
        self.backend = backend
        self.restarted_backend: LndBackend | None = None
        self.begin_calls = 0
        self.sign_calls = 0
        self._interrupted = False

    async def freeze_channel_input(
        self, session_id: str, channel_id: str
    ) -> FrozenChannelSnapshot:
        return await self.backend.freeze_channel_input(session_id, channel_id)

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo],
        channel_input_index: int,
    ) -> str:
        return await self.backend.prepare_channel_input_parent(
            session_id,
            funding_outpoint,
            parent_tx_hex,
            input_utxos,
            channel_input_index,
        )

    async def begin_channel_input_signing(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        *,
        restart_attempt: bool = False,
    ) -> LndEscrowSigningAttempt:
        self.begin_calls += 1
        return await self.backend.begin_channel_input_signing(
            session_id, funding_outpoint, restart_attempt=restart_attempt
        )

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes:
        self.sign_calls += 1
        partial = await self.backend.sign_channel_input_parent(
            session_id, funding_outpoint, attempt, remote_public_nonce
        )
        if not self._interrupted:
            self._interrupted = True
            await self.backend.close()
            await compose("restart", "lnd-opener")
            self.restarted_backend = escrow_backend("opener", OPENER_GRPC)
            self.backend = self.restarted_backend
            await wait_for_node(self.backend)
            raise SimulatedCoordinatorInterruption(
                "simulated interruption after durable opener partial signature"
            )
        return partial

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        return await self.backend.finalize_channel_input_parent(
            session_id, funding_outpoint, attempt, remote_partial_signature
        )

    async def recover_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        return await self.backend.recover_channel_input_parent(
            session_id, funding_outpoint, remote_partial_signature
        )

    async def channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus:
        return await self.backend.channel_input_escrow_status(
            session_id, funding_outpoint
        )

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool:
        return await self.backend.cancel_channel_input_escrow(
            session_id, funding_outpoint
        )


def p2tr_script(key_material: bytes) -> tuple[PrivateKey, bytes]:
    key = PrivateKey(taproot_tweak_privkey(key_material))
    return key, b"\x51\x20" + key.public_key_xonly.format()


async def test_exact_parent_restart_recovery_through_python_client(
    tmp_path: Path,
) -> None:
    if os.getenv("LND_CHANNEL_ESCROW_E2E") != "1":
        pytest.skip(
            "start the lnd_external profile with patched LND and set "
            "LND_CHANNEL_ESCROW_E2E=1"
        )

    credential_paths = [
        DATA_DIR / node / relative
        for node in ("opener", "fundee")
        for relative in (
            "tls.cert",
            "data/chain/bitcoin/regtest/admin.macaroon",
            "data/chain/bitcoin/regtest/channelescrow.macaroon",
        )
    ]
    await asyncio.gather(*(wait_for_file(path) for path in credential_paths))
    opener = escrow_backend("opener", OPENER_GRPC)
    fundee = escrow_backend("fundee", FUNDEE_GRPC)
    restarting_opener = RestartingOpenerEndpoint(opener)
    try:
        wallet = await core_wallet()
        await mine(wallet, 101)
        await asyncio.gather(wait_for_node(opener), wait_for_node(fundee))

        opener_address, fundee_address = await asyncio.gather(
            lncli("lnd-opener", "newaddress", "p2tr"),
            lncli("lnd-fundee", "newaddress", "p2tr"),
        )
        ordinary_key, ordinary_script = p2tr_script(b"\x11" * 32)
        ordinary_address = scriptpubkey_to_address(ordinary_script, "regtest")
        ordinary_funding_txid = str(
            await bitcoin_rpc("sendtoaddress", [ordinary_address, 0.005], wallet=wallet)
        )
        await bitcoin_rpc(
            "sendtoaddress", [opener_address["address"], 0.02], wallet=wallet
        )
        await bitcoin_rpc(
            "sendtoaddress", [fundee_address["address"], 0.02], wallet=wallet
        )
        await mine(wallet, 6)
        await asyncio.gather(wait_for_node(opener), wait_for_node(fundee))

        ordinary_funding = await bitcoin_rpc(
            "getrawtransaction", [ordinary_funding_txid, True]
        )
        ordinary_vouts = [
            output
            for output in ordinary_funding["vout"]
            if int(Decimal(str(output["value"])) * 100_000_000) == 500_000
            and bytes.fromhex(str(output["scriptPubKey"]["hex"])) == ordinary_script
        ]
        assert len(ordinary_vouts) == 1
        ordinary_vout = ordinary_vouts[0]
        ordinary_value = int(Decimal(str(ordinary_vout["value"])) * 100_000_000)
        assert ordinary_value == 500_000

        fundee_info = await fundee.node_info()
        await lncli(
            "lnd-opener",
            "connect",
            f"{fundee_info.identity_pubkey}@lnd-fundee:9735",
        )
        opened = await lncli(
            "lnd-opener",
            "openchannel",
            f"--node_key={fundee_info.identity_pubkey}",
            "--local_amt=1500000",
            "--push_amt=600000",
            "--private",
            "--channel_type=taproot",
            "--sat_per_vbyte=2",
        )
        await mine(wallet, 6)
        channel = await wait_for_active_channel()
        assert channel["commitment_type"] == "TAPROOT"
        channel_point = channel["channel_point"]
        assert channel_point.startswith(f"{opened['funding_txid']}:")

        session_id = "11" * 32
        opener_snapshot = await restarting_opener.freeze_channel_input(
            session_id, channel_point
        )
        fundee_snapshot = await fundee.freeze_channel_input(session_id, channel_point)
        assert opener_snapshot.funding_outpoint == fundee_snapshot.funding_outpoint
        assert (
            opener_snapshot.funding_scriptpubkey == fundee_snapshot.funding_scriptpubkey
        )
        assert opener_snapshot.local_claim_sat == fundee_snapshot.remote_claim_sat
        assert opener_snapshot.remote_claim_sat == fundee_snapshot.local_claim_sat

        (
            opener_equal_address,
            opener_change_address,
            fundee_equal_address,
            fundee_change_address,
        ) = await asyncio.gather(
            lncli("lnd-opener", "newaddress", "p2tr"),
            lncli("lnd-opener", "newaddress", "p2tr"),
            lncli("lnd-fundee", "newaddress", "p2tr"),
            lncli("lnd-fundee", "newaddress", "p2tr"),
        )
        _, ordinary_equal_script = p2tr_script(b"\x12" * 32)
        _, ordinary_change_script = p2tr_script(b"\x13" * 32)
        opener_equal_script = address_to_scriptpubkey(
            str(opener_equal_address["address"])
        )
        opener_change_script = address_to_scriptpubkey(
            str(opener_change_address["address"])
        )
        fundee_equal_script = address_to_scriptpubkey(
            str(fundee_equal_address["address"])
        )
        fundee_change_script = address_to_scriptpubkey(
            str(fundee_change_address["address"])
        )
        output_scripts = (
            opener_equal_script,
            fundee_equal_script,
            ordinary_equal_script,
            opener_change_script,
            fundee_change_script,
            ordinary_change_script,
        )
        assert len(set(output_scripts)) == 6
        assert all(script.startswith(b"\x51\x20") for script in output_scripts)

        opener_change = opener_snapshot.local_claim_sat - 300_000 - 500
        fundee_change = fundee_snapshot.local_claim_sat - 300_000 - 500
        ordinary_change = ordinary_value - 300_000 - 1_000
        changes = (opener_change, fundee_change, ordinary_change)
        assert all(change > 0 and change != 300_000 for change in changes)
        assert len(set(changes)) == 3
        inputs = [
            TxInput.from_hex(
                opener_snapshot.funding_outpoint.txid,
                opener_snapshot.funding_outpoint.vout,
            ),
            TxInput.from_hex(ordinary_funding_txid, int(ordinary_vout["n"])),
        ]
        outputs = [
            TxOutput(value=300_000, script=opener_equal_script),
            TxOutput(value=300_000, script=fundee_equal_script),
            TxOutput(value=300_000, script=ordinary_equal_script),
            TxOutput(value=opener_change, script=opener_change_script),
            TxOutput(value=fundee_change, script=fundee_change_script),
            TxOutput(value=ordinary_change, script=ordinary_change_script),
        ]
        assert (
            opener_snapshot.capacity_sat
            + ordinary_value
            - sum(output.value for output in outputs)
            == 2_000
        )
        analysis = analyze_coinjoin_outputs(outputs)
        assert analysis.is_coinjoin
        assert analysis.cj_amount == 300_000
        assert analysis.cj_count == 3
        unsigned_parent_hex = serialize_transaction(2, inputs, outputs, 0).hex()
        unsigned_parent = parse_transaction(unsigned_parent_hex)
        prevout_values = [opener_snapshot.capacity_sat, ordinary_value]
        prevout_scripts = [opener_snapshot.funding_scriptpubkey, ordinary_script]
        ordinary_signature = sign_p2tr_input(
            unsigned_parent, 1, prevout_values, prevout_scripts, ordinary_key
        )
        plan = ChannelInputParentPlan(
            session_id=session_id,
            unsigned_parent_hex=unsigned_parent_hex,
            prevouts=(
                ParentPrevout(
                    value_sat=opener_snapshot.capacity_sat,
                    script_pubkey=opener_snapshot.funding_scriptpubkey.hex(),
                ),
                ParentPrevout(
                    value_sat=ordinary_value, script_pubkey=ordinary_script.hex()
                ),
            ),
            channels=(
                ChannelContribution(
                    channel_id=channel_point,
                    channel_input_index=0,
                    first=EndpointContribution(
                        endpoint_id="opener",
                        participant_key=_participant_key(FIRST_PARTICIPANT_SECRET),
                        settlement_outputs=(
                            SettlementOutput(
                                output_index=0,
                                value_sat=300_000,
                                script_pubkey=opener_equal_script.hex(),
                            ),
                            SettlementOutput(
                                output_index=3,
                                value_sat=opener_change,
                                script_pubkey=opener_change_script.hex(),
                            ),
                        ),
                        miner_fee_share_sat=500,
                    ),
                    second=EndpointContribution(
                        endpoint_id="fundee",
                        participant_key=_participant_key(SECOND_PARTICIPANT_SECRET),
                        settlement_outputs=(
                            SettlementOutput(
                                output_index=1,
                                value_sat=300_000,
                                script_pubkey=fundee_equal_script.hex(),
                            ),
                            SettlementOutput(
                                output_index=4,
                                value_sat=fundee_change,
                                script_pubkey=fundee_change_script.hex(),
                            ),
                        ),
                        miner_fee_share_sat=500,
                    ),
                ),
            ),
            ordinary_witnesses=(
                OrdinaryWitness(input_index=1, witness=(ordinary_signature.hex(),)),
            ),
            authorization=_authorization(
                session_id=session_id,
                unsigned_parent_hex=unsigned_parent_hex,
                opener_snapshot=opener_snapshot,
                fundee_snapshot=fundee_snapshot,
                ordinary_value=ordinary_value,
                ordinary_script=ordinary_script,
                ordinary_outpoint=Outpoint(
                    txid=ordinary_funding_txid, vout=int(ordinary_vout["n"])
                ),
                outputs=outputs,
            ),
        )
        coordinator = ChannelInputParentCoordinator(tmp_path)
        endpoints: dict[str, ChannelInputEndpoint] = {
            "opener": restarting_opener,
            "fundee": fundee,
        }
        expected_txid = get_txid(unsigned_parent_hex)
        with pytest.raises(SimulatedCoordinatorInterruption):
            await coordinator.execute(plan, endpoints)
        assert restarting_opener.sign_calls == 1
        assert restarting_opener.begin_calls == 1
        assert restarting_opener.restarted_backend is not None
        restarted_status = (
            await restarting_opener.restarted_backend.channel_input_escrow_status(
                session_id, opener_snapshot.funding_outpoint
            )
        )
        assert restarted_status.stage in {
            LndEscrowStage.PARENT_SIGNED,
            LndEscrowStage.FINALIZED,
        }
        assert restarted_status.parent_txid == expected_txid
        assert restarted_status.durable_local_public_nonce is not None
        assert restarted_status.durable_remote_public_nonce is not None
        assert restarted_status.durable_local_partial_signature is not None

        resumed = await ChannelInputParentCoordinator(tmp_path).resume(
            session_id, endpoints
        )
        assert resumed.state is ChannelInputParentState.FINAL_TX_PERSISTED
        assert resumed.final_txid == expected_txid
        assert resumed.final_tx_hex is not None
        assert restarting_opener.sign_calls == 1
        assert restarting_opener.begin_calls == 1
        final_parent = parse_transaction(resumed.final_tx_hex)
        assert len(final_parent.inputs) == 2
        assert len(final_parent.outputs) == 6
        assert all(len(witness) == 1 for witness in final_parent.witnesses)
        assert verify_p2tr_signature(
            final_parent,
            0,
            prevout_values,
            prevout_scripts,
            final_parent.witnesses[0][0],
            opener_snapshot.funding_scriptpubkey[2:],
        )
        assert verify_p2tr_signature(
            final_parent,
            1,
            prevout_values,
            prevout_scripts,
            final_parent.witnesses[1][0],
            ordinary_script[2:],
        )
        assert (
            serialize_transaction(
                final_parent.version,
                final_parent.inputs,
                final_parent.outputs,
                final_parent.locktime,
            ).hex()
            == unsigned_parent_hex
        )
        final_analysis = analyze_coinjoin_outputs(final_parent.outputs)
        assert final_analysis.is_coinjoin
        assert final_analysis.cj_amount == 300_000
        assert final_analysis.cj_count == 3

        post_resume_status = (
            await restarting_opener.backend.channel_input_escrow_status(
                session_id, opener_snapshot.funding_outpoint
            )
        )
        assert post_resume_status.stage in {
            LndEscrowStage.PARENT_SIGNED,
            LndEscrowStage.FINALIZED,
        }
        assert post_resume_status.parent_txid == expected_txid
        assert post_resume_status.durable_local_partial_signature is not None
        assert expected_txid not in await bitcoin_rpc("getrawmempool")
        chain = CoreChain()
        published = await coordinator.publish(session_id, chain)
        assert published.final_txid == expected_txid
        assert expected_txid in await bitcoin_rpc("getrawmempool")
        await mine(wallet, 3)
        observed = await coordinator.observe(session_id, chain)
        assert observed.state is ChannelInputParentState.CONFIRMED
        assert observed.observation is not None
        assert observed.observation.confirmations == 3
        assert (await bitcoin_rpc("getrawtransaction", [expected_txid, True]))[
            "confirmations"
        ] == 3
        await wait_for_resolved_channel()
        assert not (await lncli("lnd-opener", "listchannels"))["channels"]
        assert not (await lncli("lnd-fundee", "listchannels"))["channels"]
    finally:
        await asyncio.gather(
            *(
                client.close()
                for client in (opener, fundee, restarting_opener.restarted_backend)
                if client is not None
            ),
            return_exceptions=True,
        )
