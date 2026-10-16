"""Live final-Taproot external funding spike against two stock LND nodes."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from jmcore.bitcoin import get_txid, parse_transaction
from jmswap.lnd import (
    AcceptorBounds,
    EndpointRole,
    ExternalChannelRequest,
    FundingTransactionChainStatus,
    InboundChannelExpectation,
    LndAmbiguousShimError,
    LndBackend,
    LndCapabilityError,
    LndNodeInfo,
    LndRpcError,
    LndValidationError,
    PendingChannelExpectation,
    TransactionPresence,
    VerifiedChannelRetirementAuthorization,
    VerifiedChannelRetirementStatus,
    VerifiedFunding,
    WitnessUtxo,
    validate_endpoint_readiness,
)

pytestmark = [pytest.mark.docker, pytest.mark.lnd_external]

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("LND_EXTERNAL_DATA_DIR", ROOT / "tmp" / "lnd-external"))
RPC_URL = f"http://127.0.0.1:{os.getenv('LND_EXTERNAL_BITCOIN_PORT', '19443')}"
OPENER_GRPC = f"127.0.0.1:{os.getenv('LND_EXTERNAL_OPENER_GRPC_PORT', '11009')}"
FUNDEE_GRPC = f"127.0.0.1:{os.getenv('LND_EXTERNAL_FUNDEE_GRPC_PORT', '12009')}"


async def bitcoin_rpc(
    method: str, params: list[Any] | None = None, wallet: str | None = None
) -> Any:
    url = RPC_URL if wallet is None else f"{RPC_URL}/wallet/{wallet}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            auth=("test", "test"),
            json={
                "jsonrpc": "2.0",
                "id": "lnd-external",
                "method": method,
                "params": params or [],
            },
        )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload["result"]


async def wait_for_file(path: Path, timeout: float = 90) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(f"LND credential was not created: {path}")
        await asyncio.sleep(0.25)


async def wait_for_node(backend: LndBackend, timeout: float = 90) -> LndNodeInfo:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await backend.node_info()
        except LndCapabilityError:
            if asyncio.get_running_loop().time() >= deadline:
                raise
            await asyncio.sleep(0.5)


async def core_wallet() -> str:
    wallet = "lnd-external"
    try:
        await bitcoin_rpc("createwallet", [wallet, False, False, "", False, True])
    except RuntimeError as exc:
        if "already exists" not in str(exc) and "already loaded" not in str(exc):
            raise
        try:
            await bitcoin_rpc("loadwallet", [wallet])
        except RuntimeError as load_exc:
            if "already loaded" not in str(load_exc):
                raise
    return wallet


async def external_unsigned_transaction(
    wallet: str, funding_address: str, funding_script: bytes, capacity_sat: int
) -> tuple[str, int, list[WitnessUtxo]]:
    raw = await bitcoin_rpc(
        "createrawtransaction", [[], [{funding_address: capacity_sat / 100_000_000}]]
    )
    funded = await bitcoin_rpc(
        "fundrawtransaction", [raw, {"change_type": "bech32m"}], wallet=wallet
    )
    raw_funded = str(funded["hex"])
    parsed = parse_transaction(raw_funded)
    funding_vout = next(
        index
        for index, output in enumerate(parsed.outputs)
        if output.script == funding_script and output.value == capacity_sat
    )
    witness_utxos: list[WitnessUtxo] = []
    for tx_input in parsed.inputs:
        prevout = await bitcoin_rpc("gettxout", [tx_input.txid, tx_input.vout, True])
        witness_utxos.append(
            WitnessUtxo(
                value_sat=int(Decimal(str(prevout["value"])) * 100_000_000),
                script_pubkey=bytes.fromhex(prevout["scriptPubKey"]["hex"]),
            )
        )
    return raw_funded, funding_vout, witness_utxos


async def funding_chain_status(unsigned_txid: str) -> FundingTransactionChainStatus:
    mempool = await bitcoin_rpc("getrawmempool")
    mempool_presence = (
        TransactionPresence.PRESENT
        if unsigned_txid in mempool
        else TransactionPresence.ABSENT
    )
    try:
        await bitcoin_rpc("getrawtransaction", [unsigned_txid, True])
    except RuntimeError as exc:
        message = str(exc)
        if (
            "-5" not in message
            and "No such mempool or blockchain transaction" not in message
        ):
            raise
        chain_presence = TransactionPresence.ABSENT
    else:
        chain_presence = TransactionPresence.PRESENT
    return FundingTransactionChainStatus(
        unsigned_txid=unsigned_txid,
        mempool=mempool_presence,
        chain=chain_presence,
    )


def retirement_authorization(
    verified: VerifiedFunding, chain_status: FundingTransactionChainStatus
) -> VerifiedChannelRetirementAuthorization:
    return VerifiedChannelRetirementAuthorization(
        verified_funding=verified,
        channel_point=verified.channel_point,
        unsigned_txid=verified.funding_txid,
        chain_status=chain_status,
        local_coinjoin_input_signature_created=False,
        local_coinjoin_input_signature_sent=False,
        funding_transaction_broadcast=False,
        final_transaction_observed=False,
    )


async def open_and_verify_channel(
    opener: LndBackend,
    fundee: LndBackend,
    opener_info: LndNodeInfo,
    fundee_info: LndNodeInfo,
    wallet: str,
    chain_hash: bytes,
) -> VerifiedFunding:
    pending_id = secrets.token_bytes(32)
    capacity = 1_500_000
    push = 600_000
    opener_reserve = 10_000
    fundee_reserve = 15_000
    expected_inbound = InboundChannelExpectation(
        pending_channel_id=pending_id,
        opener_node_id=opener_info.identity_pubkey,
        chain_hash=chain_hash,
        capacity_sat=capacity,
        push_msat=push * 1000,
        opener_reserve_sat=opener_reserve,
        fundee_reserve_sat=fundee_reserve,
        opener_csv_delay=144,
        fundee_csv_delay=144,
        min_depth=3,
    )
    acceptor_ready = asyncio.Event()
    acceptor = asyncio.create_task(
        fundee.run_channel_acceptor(
            expected_inbound,
            AcceptorBounds(),
            timeout_seconds=60,
            ready=acceptor_ready,
        )
    )
    await acceptor_ready.wait()
    negotiation = await opener.start_external_channel(
        ExternalChannelRequest(
            pending_channel_id=pending_id,
            peer_node_id=fundee_info.identity_pubkey,
            peer_host="lnd-fundee:9735",
            capacity_sat=capacity,
            push_sat=push,
            opener_reserve_sat=opener_reserve,
            fundee_reserve_sat=fundee_reserve,
            opener_csv_delay=144,
            fundee_csv_delay=144,
            min_depth=3,
            timeout_seconds=60,
        )
    )
    accepted = await acceptor
    assert accepted.pending_channel_id == pending_id

    raw, funding_vout, witness_utxos = await external_unsigned_transaction(
        wallet,
        negotiation.funding_address,
        negotiation.funding_script_pubkey,
        capacity,
    )
    verified = await opener.verify_external_funding(
        negotiation,
        raw,
        get_txid(raw),
        funding_vout,
        witness_utxos,
        timeout_seconds=30,
    )
    expected_pending = PendingChannelExpectation(
        pending_channel_id=pending_id,
        channel_point=verified.channel_point,
        opener_node_id=opener_info.identity_pubkey,
        fundee_node_id=fundee_info.identity_pubkey,
        capacity_sat=capacity,
        opener_contribution_sat=capacity - push,
        fundee_contribution_sat=push,
        opener_reserve_sat=opener_reserve,
        fundee_reserve_sat=fundee_reserve,
    )
    opener_observation, fundee_observation = await asyncio.gather(
        opener.pending_channel_observation(expected_pending, EndpointRole.OPENER),
        fundee.pending_channel_observation(expected_pending, EndpointRole.FUNDEE),
    )
    readiness = validate_endpoint_readiness(
        opener_observation, fundee_observation, expected_pending
    )
    assert readiness.opener.commitment_type == 7
    assert readiness.fundee.commitment_type == 7
    assert (
        readiness.opener.local_balance_sat
        + readiness.opener.commit_fee_sat
        + readiness.opener.commitment_overhead_sat
        == capacity - push
    )
    assert readiness.fundee.local_balance_sat == push
    assert parse_transaction(raw).witnesses == []
    status = await funding_chain_status(verified.funding_txid)
    assert status.mempool is TransactionPresence.ABSENT
    assert status.chain is TransactionPresence.ABSENT
    return verified


async def retire_from_both_endpoints(
    opener: LndBackend, fundee: LndBackend, verified: VerifiedFunding
) -> None:
    status = await funding_chain_status(verified.funding_txid)
    authorization = retirement_authorization(verified, status)
    outcomes = await asyncio.gather(
        opener.retire_verified_external_channel(authorization, timeout_seconds=30),
        fundee.retire_verified_external_channel(authorization, timeout_seconds=30),
    )
    assert all(
        outcome.status is VerifiedChannelRetirementStatus.ABANDONED
        for outcome in outcomes
    )
    opener_present, fundee_present = await asyncio.gather(
        opener.pending_channel_present(verified.channel_point),
        fundee.pending_channel_present(verified.channel_point),
    )
    assert opener_present is False
    assert fundee_present is False
    final_status = await funding_chain_status(verified.funding_txid)
    assert final_status.mempool is TransactionPresence.ABSENT
    assert final_status.chain is TransactionPresence.ABSENT


def opener_credentials() -> tuple[Path, Path]:
    return (
        DATA_DIR / "opener" / "tls.cert",
        DATA_DIR / "opener" / "data/chain/bitcoin/regtest/admin.macaroon",
    )


def fundee_credentials() -> tuple[Path, Path]:
    return (
        DATA_DIR / "fundee" / "tls.cert",
        DATA_DIR / "fundee" / "data/chain/bitcoin/regtest/admin.macaroon",
    )


@asynccontextmanager
async def lnd_endpoints() -> AsyncIterator[
    tuple[LndBackend, LndBackend, LndNodeInfo, LndNodeInfo, str, bytes]
]:
    """Two funded, synced stock LND nodes plus the regtest wallet and chain hash."""
    credentials = (*opener_credentials(), *fundee_credentials())
    await asyncio.gather(*(wait_for_file(path) for path in credentials))
    opener = LndBackend.from_paths(OPENER_GRPC, *opener_credentials())
    fundee = LndBackend.from_paths(FUNDEE_GRPC, *fundee_credentials())
    try:
        wallet = await core_wallet()
        mining_address = await bitcoin_rpc(
            "getnewaddress", ["", "bech32m"], wallet=wallet
        )
        await bitcoin_rpc("generatetoaddress", [101, mining_address])
        opener_info, fundee_info = await asyncio.gather(
            wait_for_node(opener), wait_for_node(fundee)
        )
        genesis = str(await bitcoin_rpc("getblockhash", [0]))
        yield (
            opener,
            fundee,
            opener_info,
            fundee_info,
            wallet,
            bytes.fromhex(genesis)[::-1],
        )
    finally:
        await asyncio.gather(opener.close(), fundee.close(), return_exceptions=True)


def inbound_expectation(
    pending_id: bytes,
    opener_info: LndNodeInfo,
    chain_hash: bytes,
    capacity: int,
    push: int,
) -> InboundChannelExpectation:
    return InboundChannelExpectation(
        pending_channel_id=pending_id,
        opener_node_id=opener_info.identity_pubkey,
        chain_hash=chain_hash,
        capacity_sat=capacity,
        push_msat=push * 1000,
        opener_reserve_sat=10_000,
        fundee_reserve_sat=15_000,
        opener_csv_delay=144,
        fundee_csv_delay=144,
        min_depth=3,
    )


def open_request(
    pending_id: bytes, fundee_info: LndNodeInfo, capacity: int, push: int
) -> ExternalChannelRequest:
    return ExternalChannelRequest(
        pending_channel_id=pending_id,
        peer_node_id=fundee_info.identity_pubkey,
        peer_host="lnd-fundee:9735",
        capacity_sat=capacity,
        push_sat=push,
        opener_reserve_sat=10_000,
        fundee_reserve_sat=15_000,
        opener_csv_delay=144,
        fundee_csv_delay=144,
        min_depth=3,
        timeout_seconds=60,
    )


async def arm_acceptor(
    fundee: LndBackend, expected: InboundChannelExpectation
) -> asyncio.Task[Any]:
    ready = asyncio.Event()
    acceptor = asyncio.create_task(
        fundee.run_channel_acceptor(
            expected, AcceptorBounds(), timeout_seconds=60, ready=ready
        )
    )
    await ready.wait()
    return acceptor


@pytest.mark.asyncio
async def test_acceptor_rejects_an_open_that_breaks_the_negotiated_contract() -> None:
    if os.getenv("LND_EXTERNAL_E2E") != "1":
        pytest.skip("start the lnd_external Compose profile and set LND_EXTERNAL_E2E=1")

    async with lnd_endpoints() as (
        opener,
        fundee,
        opener_info,
        fundee_info,
        _,
        chain_hash,
    ):
        pending_id = secrets.token_bytes(32)
        acceptor = await arm_acceptor(
            fundee,
            inbound_expectation(
                pending_id, opener_info, chain_hash, 1_500_000, 600_000
            ),
        )

        # The opener asks for a capacity the fundee never agreed to fund. LND relays
        # the acceptor's own rejection reason back to the opener, which surfaces as a
        # backend error carrying that reason.
        with pytest.raises(LndRpcError, match="co-funded channel rejected: capacity"):
            await opener.start_external_channel(
                open_request(pending_id, fundee_info, 1_400_000, 600_000)
            )
        with pytest.raises(LndValidationError, match="capacity"):
            await acceptor

        # A contract breach must leave nothing behind on either node.
        assert await opener._list_pending_channel_points(timeout_seconds=10) == set()
        assert await fundee._list_pending_channel_points(timeout_seconds=10) == set()


@pytest.mark.asyncio
async def test_cancel_before_verification_releases_the_funding_shim() -> None:
    if os.getenv("LND_EXTERNAL_E2E") != "1":
        pytest.skip("start the lnd_external Compose profile and set LND_EXTERNAL_E2E=1")

    async with lnd_endpoints() as (
        opener,
        fundee,
        opener_info,
        fundee_info,
        wallet,
        chain_hash,
    ):
        pending_id = secrets.token_bytes(32)
        acceptor = await arm_acceptor(
            fundee,
            inbound_expectation(
                pending_id, opener_info, chain_hash, 1_500_000, 600_000
            ),
        )
        await opener.start_external_channel(
            open_request(pending_id, fundee_info, 1_500_000, 600_000)
        )
        assert (await acceptor).pending_channel_id == pending_id

        await opener.cancel_external_channel(pending_id, input_signatures_added=False)
        assert await opener._list_pending_channel_points(timeout_seconds=10) == set()

        # The endpoints stay usable for a fresh ring revision afterwards.
        verified = await open_and_verify_channel(
            opener, fundee, opener_info, fundee_info, wallet, chain_hash
        )
        await retire_from_both_endpoints(opener, fundee, verified)


@pytest.mark.asyncio
async def test_restart_cannot_report_a_consumed_shim_as_canceled() -> None:
    if os.getenv("LND_EXTERNAL_E2E") != "1":
        pytest.skip("start the lnd_external Compose profile and set LND_EXTERNAL_E2E=1")

    async with lnd_endpoints() as (
        opener,
        fundee,
        opener_info,
        fundee_info,
        wallet,
        chain_hash,
    ):
        verified = await open_and_verify_channel(
            opener, fundee, opener_info, fundee_info, wallet, chain_hash
        )
        negotiation = opener._pending[bytes(verified.pending_channel_id)].negotiation
        await opener.close()

        # Restart with no durable evidence: LND answers a consumed shim exactly as it
        # answers an absent one, so cancellation must never be reported as success.
        blind = LndBackend.from_paths(OPENER_GRPC, *opener_credentials())
        blind.resume_external_channel(negotiation)
        with pytest.raises(LndAmbiguousShimError):
            await blind.cancel_external_channel(
                verified.pending_channel_id, input_signatures_added=False
            )
        assert await blind.pending_channel_present(verified.channel_point) is True
        await blind.close()

        # Restart with the durable evidence the participant persisted before it
        # attested readiness: the endpoint stays eligible for guarded retirement.
        restarted = LndBackend.from_paths(OPENER_GRPC, *opener_credentials())
        restarted.resume_external_channel(
            negotiation,
            verified=verified,
            observed_channel_point=verified.channel_point,
        )
        try:
            await retire_from_both_endpoints(restarted, fundee, verified)
        finally:
            await restarted.close()


@pytest.mark.asyncio
async def test_verified_external_channels_can_be_retired_and_reopened() -> None:
    if os.getenv("LND_EXTERNAL_E2E") != "1":
        pytest.skip("start the lnd_external Compose profile and set LND_EXTERNAL_E2E=1")

    async with lnd_endpoints() as (
        opener,
        fundee,
        opener_info,
        fundee_info,
        wallet,
        chain_hash,
    ):
        mempool_before = await bitcoin_rpc("getrawmempool")
        first = await open_and_verify_channel(
            opener, fundee, opener_info, fundee_info, wallet, chain_hash
        )
        await retire_from_both_endpoints(opener, fundee, first)

        second = await open_and_verify_channel(
            opener, fundee, opener_info, fundee_info, wallet, chain_hash
        )
        assert second.pending_channel_id != first.pending_channel_id
        assert second.channel_point != first.channel_point
        await retire_from_both_endpoints(opener, fundee, second)
        assert await bitcoin_rpc("getrawmempool") == mempool_before
