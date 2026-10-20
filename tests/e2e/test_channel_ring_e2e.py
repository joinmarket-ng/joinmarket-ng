"""Ring-only integration, independent of credential trading and later buyouts."""

from __future__ import annotations

import os

import pytest
from jmcore.bitcoin import parse_transaction
from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    get_mnemonic_fingerprint,
)
from jmwallet.wallet.service import WalletService

from tests.e2e.ring_fixture import (
    DATA_DIR,
    RPC_URL,
    TAKER_MNEMONIC,
    bitcoin_rpc,
    make_taker,
    mine_and_sync,
    taker_config,
    wait_for_ring_channels,
)

pytestmark = [pytest.mark.docker, pytest.mark.ring_e2e]


@pytest.mark.asyncio
@pytest.mark.timeout(1800)
async def test_participating_taker_ring_with_an_ordinary_maker() -> None:
    if os.getenv("RING_E2E") != "1":
        pytest.skip("requires a fresh ring-core Compose project")
    data_dir = DATA_DIR / "host-taker"
    backend = DescriptorWalletBackend(
        rpc_url=RPC_URL,
        rpc_user="test",
        rpc_password="test",
        wallet_name=f"jm_{get_mnemonic_fingerprint(TAKER_MNEMONIC)}_ring_e2e_regtest",
    )
    wallet = WalletService(
        TAKER_MNEMONIC,
        backend,
        network="regtest",
        address_type="p2tr",
        data_dir=data_dir,
    )
    taker = await make_taker(wallet, backend, taker_config(data_dir))
    try:
        destination = wallet.get_receive_address(1, 0)
        txid = await taker.do_coinjoin(
            amount=500_000, destination=destination, mixdepth=0, counterparty_count=4
        )
        assert txid is not None, taker.last_failure_reason
        coordinator = taker._session.ring_coordinator
        assert coordinator is not None
        record = coordinator._record()
        assert record.manifest is not None and record.final_tx is not None
        assert record.manifest.unsigned_txid == txid
        assert len(record.coordinator_participants) == len(record.manifest.edges) == 4
        assert len(record.manifest.equal_output_indices) == 5
        assert len(taker._session.ring_maker_sessions) == 3
        assert len(taker._session.ordinary_maker_sessions) == 1

        parsed = parse_transaction(str(await bitcoin_rpc("getrawtransaction", [txid])))
        assert len(parsed.outputs) == 10
        assert all(output.script.startswith(b"\x51\x20") for output in parsed.outputs)
        await mine_and_sync(3, destination)
        channels = await wait_for_ring_channels()
        assert all(item["private"] for items in channels.values() for item in items)
        assert {
            str(item["channel_point"]) for items in channels.values() for item in items
        } == {f"{txid}:{edge.output_index}" for edge in record.manifest.edges}
    finally:
        await taker.stop()
        await wallet.close()
