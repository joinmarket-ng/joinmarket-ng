"""
Live channel-as-change lifecycle against real bitcoind (docker ``swap`` profile).

Emits a Taproot channel-change funding output, funds it on-chain, scans/tracks
it, and cooperatively sweeps it back into the wallet. bitcoind accepting the
sweep proves the MuSig2 key-path cooperative close is consensus-correct on a real
node (the same close a used channel performs).

Run with::

    docker compose --profile swap up -d
    pytest -m swap tests/e2e/test_channel_change_live.py --fail-on-skip
"""

from __future__ import annotations

import pytest

from .conftest import is_port_open

pytestmark = [pytest.mark.swap, pytest.mark.docker]

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


@pytest.mark.timeout(120)
async def test_channel_change_track_and_cooperative_sweep() -> None:
    if not is_port_open("127.0.0.1", 18443):
        pytest.skip("bitcoind (swap profile) not running on :18443")

    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
    from jmwallet.wallet.service import WalletService

    from maker.channel_change import (
        ChannelChangeStore,
        channel_change_balance,
        derive_channel_change_address,
        emit_channel_change_address,
        scan_channel_change_utxos,
        sweep_channel_change,
    )
    from tests.e2e.test_swap_ldk_live import BitcoindRPC, MINER_WALLET

    rpc = BitcoindRPC()
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443", rpc_user="test", rpc_password="test"
    )
    wallet = WalletService(
        mnemonic=TEST_MNEMONIC, backend=backend, network="regtest", mixdepth_count=5
    )

    import tempfile

    store = ChannelChangeStore(tempfile.mkdtemp(prefix="cc-live-"))
    address = emit_channel_change_address(wallet, store)
    assert address.startswith("bcrt1p")
    # the derived address is recoverable from the record
    _, rec0 = derive_channel_change_address(wallet, 0)
    assert rec0.address == address

    # fund the channel-change output on-chain
    rpc.call("sendtoaddress", [address, 0.002], wallet=MINER_WALLET)
    rpc.mine(1)

    # track it
    utxos = await scan_channel_change_utxos(backend, store)
    assert len(utxos) == 1
    assert utxos[0].value == 200_000
    assert await channel_change_balance(backend, store) == 200_000

    # cooperatively sweep it back into the wallet; bitcoind validates the
    # MuSig2 key-path cooperative close.
    dest = rpc.call("getnewaddress", ["", "bech32"], wallet=MINER_WALLET)
    txids = await sweep_channel_change(wallet, backend, store, dest)
    assert len(txids) == 1
    rpc.mine(1)
    # the sweep confirmed and the channel-change balance is now zero
    assert await channel_change_balance(backend, store) == 0
    assert rpc.call("getrawtransaction", [txids[0]]) is not None
