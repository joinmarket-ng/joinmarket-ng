"""Live exact-parent restart recovery against two patched LND nodes."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from jmswap.lnd import LndBackend, LndEscrowStage, WitnessUtxo

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
        if all(
            not state[key]
            for state in (opener, fundee)
            for key in (
                "pending_open_channels",
                "pending_closing_channels",
                "pending_force_closing_channels",
                "waiting_close_channels",
            )
        ):
            return
        await asyncio.sleep(0.5)
    raise RuntimeError("confirmed exact parent did not resolve the channel")


async def test_exact_parent_restart_recovery_through_python_client() -> None:
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
    restarted_opener: LndBackend | None = None
    try:
        wallet = await core_wallet()
        await mine(wallet, 101)
        await asyncio.gather(wait_for_node(opener), wait_for_node(fundee))

        opener_address, fundee_address = await asyncio.gather(
            lncli("lnd-opener", "newaddress", "p2tr"),
            lncli("lnd-fundee", "newaddress", "p2tr"),
        )
        await bitcoin_rpc(
            "sendtoaddress", [opener_address["address"], 0.02], wallet=wallet
        )
        await bitcoin_rpc(
            "sendtoaddress", [fundee_address["address"], 0.02], wallet=wallet
        )
        await mine(wallet, 6)
        await asyncio.gather(wait_for_node(opener), wait_for_node(fundee))

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

        session_id = "joinmarket-python-client-regtest-restart-recovery"
        opener_snapshot = await opener.freeze_channel_input(session_id, channel_point)
        fundee_snapshot = await fundee.freeze_channel_input(session_id, channel_point)
        assert opener_snapshot.funding_outpoint == fundee_snapshot.funding_outpoint
        assert (
            opener_snapshot.funding_scriptpubkey == fundee_snapshot.funding_scriptpubkey
        )
        assert opener_snapshot.local_claim_sat == fundee_snapshot.remote_claim_sat
        assert opener_snapshot.remote_claim_sat == fundee_snapshot.local_claim_sat

        destination = await bitcoin_rpc("getnewaddress", ["", "bech32m"], wallet=wallet)
        parent_hex = await bitcoin_rpc(
            "createrawtransaction",
            [
                [
                    {
                        "txid": opener_snapshot.funding_outpoint.txid,
                        "vout": opener_snapshot.funding_outpoint.vout,
                    }
                ],
                [{destination: 0.01499}],
            ],
        )
        prevouts = [
            WitnessUtxo(
                value_sat=opener_snapshot.capacity_sat,
                script_pubkey=opener_snapshot.funding_scriptpubkey,
            )
        ]
        opener_txid, fundee_txid = await asyncio.gather(
            opener.prepare_channel_input_parent(
                session_id, opener_snapshot.funding_outpoint, parent_hex, prevouts, 0
            ),
            fundee.prepare_channel_input_parent(
                session_id, fundee_snapshot.funding_outpoint, parent_hex, prevouts, 0
            ),
        )
        assert opener_txid == fundee_txid
        assert opener_txid not in await bitcoin_rpc("getrawmempool")

        opener_attempt, fundee_attempt = await asyncio.gather(
            opener.begin_channel_input_signing(
                session_id, opener_snapshot.funding_outpoint
            ),
            fundee.begin_channel_input_signing(
                session_id, fundee_snapshot.funding_outpoint
            ),
        )
        opener_partial, fundee_partial = await asyncio.gather(
            opener.sign_channel_input_parent(
                session_id,
                opener_snapshot.funding_outpoint,
                opener_attempt,
                fundee_attempt.public_nonce,
            ),
            fundee.sign_channel_input_parent(
                session_id,
                fundee_snapshot.funding_outpoint,
                fundee_attempt,
                opener_attempt.public_nonce,
            ),
        )
        assert opener_txid not in await bitcoin_rpc("getrawmempool")

        await opener.close()
        await compose("restart", "lnd-opener")
        restarted_opener = escrow_backend("opener", OPENER_GRPC)
        await wait_for_node(restarted_opener)
        recovered = await restarted_opener.channel_input_escrow_status(
            session_id, opener_snapshot.funding_outpoint
        )
        assert recovered.stage is LndEscrowStage.PARENT_SIGNED
        assert recovered.parent_txid == opener_txid
        assert recovered.durable_local_public_nonce == opener_attempt.public_nonce
        assert recovered.durable_remote_public_nonce == fundee_attempt.public_nonce
        assert recovered.durable_local_partial_signature == opener_partial

        opener_final, fundee_final = await asyncio.gather(
            restarted_opener.recover_channel_input_parent(
                session_id, opener_snapshot.funding_outpoint, fundee_partial
            ),
            fundee.finalize_channel_input_parent(
                session_id,
                fundee_snapshot.funding_outpoint,
                fundee_attempt,
                opener_partial,
            ),
        )
        assert opener_final == fundee_final
        assert opener_final.txid == opener_txid
        assert len(opener_final.channel_input_witness) == 1
        assert opener_txid not in await bitcoin_rpc("getrawmempool")

        broadcast_txid = await bitcoin_rpc("sendrawtransaction", [opener_final.tx_hex])
        assert broadcast_txid == opener_txid
        await mine(wallet, 3)
        assert (await bitcoin_rpc("getrawtransaction", [broadcast_txid, True]))[
            "confirmations"
        ] == 3
        assert not (await lncli("lnd-opener", "listchannels"))["channels"]
        assert not (await lncli("lnd-fundee", "listchannels"))["channels"]
        await wait_for_resolved_channel()
    finally:
        await asyncio.gather(
            *(
                client.close()
                for client in (opener, fundee, restarted_opener)
                if client is not None
            ),
            return_exceptions=True,
        )
