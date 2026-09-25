"""Fresh isolated regtest CoinJoin with three maker channels and no taker LND."""

from __future__ import annotations

import asyncio
import os

import pytest
from jmcore.bitcoin import address_to_scriptpubkey_for_network, parse_transaction
from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    get_mnemonic_fingerprint,
)
from jmwallet.wallet.service import WalletService

from taker.taker import Taker
from tests.e2e.test_cofunded_ring_e2e import (
    DATA_DIR,
    RPC_URL,
    TAKER_MNEMONIC,
    bitcoin_rpc,
    compose,
    lncli,
    mine_and_sync,
    taker_config,
)

pytestmark = [pytest.mark.docker, pytest.mark.ring_e2e]


@pytest.mark.asyncio
@pytest.mark.timeout(1800)
async def test_three_maker_ring_without_taker_lnd() -> None:
    if os.getenv("RING_E2E") != "1":
        pytest.skip("requires the fresh ring-no-taker Compose project")

    data_dir = DATA_DIR / "host-taker"
    backend = DescriptorWalletBackend(
        rpc_url=RPC_URL,
        rpc_user="test",
        rpc_password="test",
        wallet_name=f"jm_{get_mnemonic_fingerprint(TAKER_MNEMONIC)}_ring_e2e_regtest",
    )
    wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=backend,
        network="regtest",
        mixdepth_count=5,
        address_type="p2tr",
        data_dir=data_dir,
    )
    # Keep coordinator signing evidence inside the retained fixture, never in
    # pytest's disposable tmp_path if the round fails after authorization.
    config = taker_config(data_dir, with_taker_node=False)
    assert not config.channel_ring.taker_joins
    assert not compose("ps", "--all", "-q", "lnd-taker").stdout.strip()
    taker = Taker(wallet, backend, config)
    try:
        await taker.start()
        destination = wallet.get_receive_address(1, 0)
        txid = await taker.do_coinjoin(
            amount=500_000, destination=destination, mixdepth=0, counterparty_count=3
        )
        assert txid is not None, taker.last_failure_reason
        coordinator = taker._session.ring_coordinator
        assert coordinator is not None
        record = coordinator._record()
        assert record.manifest is not None and record.final_tx is not None
        assert record.manifest.unsigned_txid == txid
        assert len(record.participants) == len(record.manifest.edges) == 3
        assert len(record.manifest.equal_output_indices) == 4
        assert len(taker._session.ring_maker_sessions) == 3
        assert not taker._session.ordinary_maker_sessions

        raw_tx = str(await bitcoin_rpc("getrawtransaction", [txid]))
        parsed = parse_transaction(raw_tx)
        assert len(parsed.outputs) == 8
        assert all(output.script.startswith(b"\x51\x20") for output in parsed.outputs)
        ordinary_indices = (
            set(range(len(parsed.outputs)))
            - set(record.manifest.equal_output_indices)
            - {edge.output_index for edge in record.manifest.edges}
        )
        assert len(ordinary_indices) == 1
        taker_change = taker._session.taker_change_address
        assert taker_change is not None
        assert parsed.outputs[
            ordinary_indices.pop()
        ].script == address_to_scriptpubkey_for_network(taker_change, "regtest")

        await mine_and_sync(
            3, destination, services=("lnd-maker1", "lnd-maker2", "lnd-maker3")
        )
        expected_points = {
            f"{txid}:{edge.output_index}" for edge in record.manifest.edges
        }
        deadline = asyncio.get_running_loop().time() + 120
        while True:
            channels = {
                service: (await asyncio.to_thread(lncli, service, "listchannels"))[
                    "channels"
                ]
                for service in ("lnd-maker1", "lnd-maker2", "lnd-maker3")
            }
            if all(
                len(items) == 2 and all(item["active"] for item in items)
                for items in channels.values()
            ):
                break
            if asyncio.get_running_loop().time() > deadline:
                raise RuntimeError(
                    f"maker-only ring channels did not activate: {channels}"
                )
            await asyncio.sleep(2)
        assert {
            str(item["channel_point"]) for items in channels.values() for item in items
        } == (expected_points)
        assert all(item["private"] for items in channels.values() for item in items)
    finally:
        await taker.stop()
