"""Bitcoin Core consensus checks for channel-input escrow transactions."""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address
from jmcore.musig2 import nonce_agg, sign_partial

from jmswap.bitcoin_escrow import (
    ChannelInputEscrowTree,
    build_success_spend,
    build_timeout_spend,
    finalize_timeout_spend,
    timeout_nonce,
    timeout_session,
)
from jmswap.spend import SwapOutpoint

from .rpc_utils import BitcoinRPCError, rpc_call

pytestmark = [pytest.mark.docker, pytest.mark.swap]

_ESCROW_VALUE = 100_000


async def _fund_escrow(
    wallet: str, tree: ChannelInputEscrowTree, miner_address: str
) -> SwapOutpoint:
    address = scriptpubkey_to_address(tree.funding_output_script(), "regtest")
    txid = await rpc_call(
        "sendtoaddress", [address, _ESCROW_VALUE / 100_000_000], wallet=wallet
    )
    await rpc_call("generatetoaddress", [1, miner_address])
    decoded: dict[str, Any] = await rpc_call("getrawtransaction", [txid, True])
    matches = [
        output
        for output in decoded["vout"]
        if output["scriptPubKey"]["hex"] == tree.funding_output_script().hex()
        and round(float(output["value"]) * 100_000_000) == _ESCROW_VALUE
    ]
    assert len(matches) == 1
    return SwapOutpoint(
        txid=txid,
        vout=int(matches[0]["n"]),
        value=_ESCROW_VALUE,
        scriptpubkey=tree.funding_output_script(),
    )


@pytest.mark.timeout(180)
async def test_bitcoin_core_accepts_success_and_timeout_spends() -> None:
    try:
        await rpc_call("getblockchaininfo")
    except Exception as exc:
        pytest.skip(f"Bitcoin Core regtest RPC is unavailable: {exc}")

    wallet = f"escrow-{secrets.token_hex(6)}"
    await rpc_call("createwallet", [wallet])
    try:
        miner_address = await rpc_call("getnewaddress", wallet=wallet)
        await rpc_call("generatetoaddress", [101, miner_address])

        maker_escrow = PrivateKey((1).to_bytes(32, "big"))
        peer_escrow = PrivateKey((2).to_bytes(32, "big"))
        maker_claim = PrivateKey((3).to_bytes(32, "big"))
        preimage = bytes.fromhex("11" * 32)
        tree = ChannelInputEscrowTree(
            maker_escrow_pubkey=maker_escrow.public_key.format(compressed=True),
            peer_escrow_pubkey=peer_escrow.public_key.format(compressed=True),
            maker_claim_pubkey=maker_claim.public_key.format(compressed=True),
            payment_hash=hashlib.sha256(preimage).digest(),
        )
        maker_output_script = create_p2tr_scriptpubkey(bytes.fromhex("44" * 32))
        peer_output_script = create_p2tr_scriptpubkey(bytes.fromhex("55" * 32))

        success_outpoint = await _fund_escrow(wallet, tree, miner_address)
        success = build_success_spend(
            tree,
            success_outpoint,
            preimage,
            maker_claim.secret,
            maker_output_script,
            fee_sats=300,
        )
        success_txid = await rpc_call("sendrawtransaction", [success.serialize().hex()])
        assert success_txid == success.txid()
        await rpc_call("generatetoaddress", [1, miner_address])
        assert await rpc_call("getrawtransaction", [success_txid, True])

        timeout_outpoint = await _fund_escrow(wallet, tree, miner_address)
        timeout_height = int(await rpc_call("getblockcount")) + 1
        timeout = build_timeout_spend(
            tree,
            timeout_outpoint,
            maker_output_script,
            peer_output_script,
            peer_claim_sats=30_000,
            timeout_compensation_sats=250,
            fee_sats=300,
            timeout_height=timeout_height,
        )
        maker_nonce, maker_pubnonce = timeout_nonce(
            tree.maker_escrow_pubkey, b"\x01" * 32
        )
        peer_nonce, peer_pubnonce = timeout_nonce(tree.peer_escrow_pubkey, b"\x02" * 32)
        session = timeout_session(
            tree,
            nonce_agg([maker_pubnonce, peer_pubnonce]),
            timeout.sighash,
        )
        signed_timeout = finalize_timeout_spend(
            tree,
            timeout,
            maker_pubnonce,
            peer_pubnonce,
            sign_partial(maker_nonce, maker_escrow.secret, session),
            sign_partial(peer_nonce, peer_escrow.secret, session),
        )

        await rpc_call("generatetoaddress", [1, miner_address])
        timeout_txid = await rpc_call(
            "sendrawtransaction", [signed_timeout.serialize().hex()]
        )
        assert timeout_txid == signed_timeout.txid()
        await rpc_call("generatetoaddress", [1, miner_address])
        assert await rpc_call("getrawtransaction", [timeout_txid, True])
    finally:
        try:
            await rpc_call("unloadwallet", [wallet])
        except BitcoinRPCError:
            pass
