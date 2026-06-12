"""End-to-end regression test for issue #517.

Background
==========

Internal wallet transfers (a plain ``jm-wallet send`` between mixdepths,
without any CoinJoin) were misclassified by ``jm-wallet info --extended``:

  * the change output (internal md0 address) showed as ``cj-change``
  * the destination output (external md1 address) showed as ``cj-out``

Both labels imply CoinJoin participation and create false privacy
expectations. A plain send is recorded in ``history.csv`` with
``role="send"`` purely so its addresses are marked as used; the address
classifier (``get_address_history_types``) must not treat those rows as
CoinJoin outputs/change.

This test drives the real send path against a regtest bitcoind and asserts
the resulting address statuses are ``deposit`` / ``non-cj-change``.

Requires: ``docker compose up -d`` (the default regtest bitcoind).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncGenerator
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx
import pytest
import pytest_asyncio

from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    generate_wallet_name,
    get_mnemonic_fingerprint,
)
from jmwallet.cli.mnemonic import generate_mnemonic_secure
from jmwallet.history import (
    append_history_entry,
    create_send_history_entry,
    get_address_history_types,
)
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.spend import direct_send

pytestmark = pytest.mark.e2e

_RPC_TIMEOUT = 120.0


async def _rpc(
    cfg: dict[str, str],
    method: str,
    params: list[Any] | None = None,
    wallet: str | None = None,
) -> Any:
    url = cfg["rpc_url"].rstrip("/")
    if wallet:
        url = f"{url}/wallet/{wallet}"
    payload = {
        "jsonrpc": "1.0",
        "id": "jmng-517",
        "method": method,
        "params": params or [],
    }
    async with httpx.AsyncClient(timeout=_RPC_TIMEOUT) as client:
        response = await client.post(
            url, auth=(cfg["rpc_user"], cfg["rpc_password"]), json=payload
        )
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"{method} RPC error: {data['error']}")
    return data.get("result")


@pytest_asyncio.fixture
async def funded_miner(
    bitcoin_rpc_config: dict[str, str],
) -> AsyncGenerator[tuple[str, str], None]:
    """Per-test miner wallet on Core; returns ``(wallet_name, miner_addr)``.

    Funds the wallet from the pre-funded ``test-funder`` Core wallet (created
    by ``scripts/fund-test-wallets.sh``) via ``sendtoaddress``, which mines
    only 1 confirmation block instead of 100+ coinbase blocks.  Falls back to
    coinbase mining when test-funder is not available.
    """
    name = f"miner_{secrets.token_hex(4)}"
    await _rpc(bitcoin_rpc_config, "createwallet", [name])
    addr = await _rpc(bitcoin_rpc_config, "getnewaddress", ["", "bech32"], wallet=name)

    funded = False
    try:
        await _rpc(
            bitcoin_rpc_config, "sendtoaddress", [addr, 2.0], wallet="test-funder"
        )
        await _rpc(bitcoin_rpc_config, "generatetoaddress", [1, addr])
        funded = True
    except Exception:
        pass

    if not funded:
        import math

        target_btc = 1.0
        for _ in range(20):
            info = await _rpc(bitcoin_rpc_config, "getwalletinfo", wallet=name)
            if float(info.get("balance", 0)) >= target_btc:
                break
            chain = await _rpc(bitcoin_rpc_config, "getblockchaininfo")
            height = int(chain["blocks"])
            stats = await _rpc(
                bitcoin_rpc_config, "getblockstats", [height, ["subsidy"]]
            )
            subsidy_btc = float(stats["subsidy"]) / 1e8
            if subsidy_btc <= 0:
                raise RuntimeError(
                    f"Block subsidy is zero at height {height}; test-funder also "
                    "unavailable. Recreate the chain with `docker compose down -v`."
                )
            deficit = target_btc - float(info.get("balance", 0))
            needed_mature = max(1, math.ceil(deficit / subsidy_btc))
            await _rpc(
                bitcoin_rpc_config,
                "generatetoaddress",
                [needed_mature + 100, addr],
                wallet=name,
            )
    yield name, addr


@pytest.mark.asyncio
async def test_internal_transfer_not_labeled_as_coinjoin(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
    funded_miner: tuple[str, str],
) -> None:
    """A plain md0->md1 transfer must label change as non-cj-change and the
    destination as deposit (issue #517), not cj-change / cj-out."""
    miner_wallet, miner_addr = funded_miner
    mnemonic = generate_mnemonic_secure(word_count=12)
    fingerprint = get_mnemonic_fingerprint(mnemonic, "")
    wallet_name = generate_wallet_name(fingerprint, "regtest")

    with TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        backend = DescriptorWalletBackend(
            rpc_url=bitcoin_rpc_config["rpc_url"],
            rpc_user=bitcoin_rpc_config["rpc_user"],
            rpc_password=bitcoin_rpc_config["rpc_password"],
            wallet_name=wallet_name,
        )
        wallet = WalletService(
            mnemonic=mnemonic,
            backend=backend,
            network="regtest",
            mixdepth_count=5,
            scan_range=1000,
            data_dir=data_dir,
        )
        try:
            # Fund mixdepth 0.
            addr0 = wallet.get_receive_address(mixdepth=0, index=0)
            await _rpc(
                bitcoin_rpc_config, "sendtoaddress", [addr0, 0.01], wallet=miner_wallet
            )
            await _rpc(
                bitcoin_rpc_config,
                "generatetoaddress",
                [6, miner_addr],
                wallet=miner_wallet,
            )
            await wallet.setup_descriptor_wallet(scan_range=1000, rescan=True)
            await wallet.sync_with_descriptor_wallet()
            assert await wallet.get_balance(mixdepth=0) == 1_000_000

            # Plain internal transfer: md0 -> md1 external address (no CoinJoin).
            md1_dest = wallet.get_receive_address(mixdepth=1, index=0)
            change_index = wallet.get_next_address_index(0, 1)
            expected_change = wallet.get_change_address(0, change_index)
            result = await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=200_000,
                destination=md1_dest,
                fee_rate=2.0,
            )
            assert result.change_amount > 0, "expected a change output for this send"

            # Record the send exactly as ``jm-wallet send`` does.
            append_history_entry(
                create_send_history_entry(
                    destination=md1_dest,
                    change_address=expected_change,
                    amount=200_000,
                    mining_fee=result.fee,
                    source_mixdepth=0,
                    selected_utxos=[
                        (
                            str(i["outpoint"]).split(":")[0],
                            int(str(i["outpoint"]).split(":")[1]),
                        )
                        for i in result.inputs
                    ],
                    txid=result.txid,
                    success=True,
                    network="regtest",
                    wallet_fingerprint=fingerprint,
                ),
                data_dir,
            )

            await _rpc(
                bitcoin_rpc_config,
                "generatetoaddress",
                [6, miner_addr],
                wallet=miner_wallet,
            )
            await wallet.sync_with_descriptor_wallet()

            history_addresses = get_address_history_types(
                data_dir, wallet_fingerprint=fingerprint
            )

            ext_md1 = wallet.get_address_info_for_mixdepth(
                1, 0, history_addresses=history_addresses
            )
            int_md0 = wallet.get_address_info_for_mixdepth(
                0, 1, history_addresses=history_addresses
            )
            dest_status = next(
                (a.status for a in ext_md1 if a.address == md1_dest), None
            )
            change_status = next(
                (a.status for a in int_md0 if a.address == expected_change), None
            )

            assert dest_status == "deposit", (
                f"internal-transfer destination must be 'deposit', got {dest_status!r}"
            )
            assert change_status == "non-cj-change", (
                f"internal-transfer change must be 'non-cj-change', got {change_status!r}"
            )
        finally:
            await backend.close()
