"""E2E test for the forced-address-reuse auto-freeze (issue #529).

Per https://en.bitcoin.it/wiki/Privacy#Forced_address_reuse, a forced payment
to an already-used *empty* address must never be spent (we freeze it), whereas
coins arriving on an address that still holds funds should be fully spent
together (so we do not freeze those). This drives the real sync path against a
regtest bitcoind and asserts both behaviors, plus that an explicit unfreeze is
not overridden by a later sync.

Requires: ``docker compose --profile e2e up -d`` (or the default regtest
bitcoind). Run with: ``pytest tests/e2e/test_address_reuse_freeze_e2e.py -m e2e``.
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
from loguru import logger

from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    generate_wallet_name,
    get_mnemonic_fingerprint,
)
from jmwallet.cli.mnemonic import generate_mnemonic_secure
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.spend import direct_send
from jmwallet.wallet.utxo_metadata import AUTO_FREEZE_REUSE_LABEL

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
        "id": "jmng-529",
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
    only 1 confirmation block instead of 100+ coinbase blocks.  This avoids
    the cascading halving problem where repeated coinbase mining pushes the
    regtest subsidy to zero.  Falls back to coinbase mining when test-funder
    is not available.
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


async def _fund(
    cfg: dict[str, str], miner: str, miner_addr: str, address: str, btc: float
) -> None:
    await _rpc(cfg, "sendtoaddress", [address, btc], wallet=miner)
    await _rpc(cfg, "generatetoaddress", [1, miner_addr], wallet=miner)


@pytest.mark.asyncio
async def test_reuse_on_spent_empty_address_is_auto_frozen(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
    funded_miner: tuple[str, str],
) -> None:
    """A new payment to a used address that was emptied is auto-frozen."""
    miner, miner_addr = funded_miner
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
            max_sats_freeze_reuse=-1,
        )
        try:
            deposit = wallet.get_receive_address(mixdepth=0, index=0)
            await _fund(bitcoin_rpc_config, miner, miner_addr, deposit, 0.01)
            await wallet.setup_descriptor_wallet(scan_range=1000, rescan=True)
            await wallet.sync_with_descriptor_wallet()
            assert await wallet.get_balance(mixdepth=0) == 1_000_000

            # Spend everything out of mixdepth 0 to empty the deposit address.
            sweep_dest = wallet.get_receive_address(mixdepth=1, index=0)
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=0,  # sweep
                destination=sweep_dest,
                fee_rate=2.0,
            )
            await _rpc(
                bitcoin_rpc_config, "generatetoaddress", [1, miner_addr], wallet=miner
            )
            await wallet.sync_with_descriptor_wallet()
            # The deposit address is now used-but-empty.
            assert not any(
                u.address == deposit for u in wallet.utxo_cache.get(0, [])
            ), "deposit address should be spent empty"
            assert deposit in wallet.addresses_with_history

            # Forced reuse: pay the now-empty used deposit address again.
            await _fund(bitcoin_rpc_config, miner, miner_addr, deposit, 0.005)
            await wallet.sync_with_descriptor_wallet()

            reuse = [u for u in wallet.utxo_cache.get(0, []) if u.address == deposit]
            assert len(reuse) == 1, f"expected the reuse UTXO, got {reuse}"
            assert reuse[0].frozen is True, (
                "reuse on a spent-empty address must be auto-frozen"
            )

            assert wallet.metadata_store is not None
            assert wallet.metadata_store.is_frozen(reuse[0].outpoint)
            record = wallet.metadata_store.records[reuse[0].outpoint]
            assert record.label == AUTO_FREEZE_REUSE_LABEL

            # Frozen reuse UTXO excluded from spendable balance.
            assert await wallet.get_balance(mixdepth=0) == 0
            logger.info(f"Auto-froze spent-empty reuse UTXO {reuse[0].outpoint}")
        finally:
            await wallet.close()


@pytest.mark.asyncio
async def test_reuse_while_address_still_funded_is_not_frozen(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
    funded_miner: tuple[str, str],
) -> None:
    """Coins arriving on an address that still holds funds are not frozen.

    The privacy-correct action there is to fully spend the address together, so
    neither the original nor the new arrival is auto-frozen.
    """
    miner, miner_addr = funded_miner
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
            max_sats_freeze_reuse=-1,
        )
        try:
            deposit = wallet.get_receive_address(mixdepth=0, index=0)
            await _fund(bitcoin_rpc_config, miner, miner_addr, deposit, 0.01)
            await wallet.setup_descriptor_wallet(scan_range=1000, rescan=True)
            await wallet.sync_with_descriptor_wallet()

            # Pay the SAME address again without spending the first UTXO.
            await _fund(bitcoin_rpc_config, miner, miner_addr, deposit, 0.005)
            await wallet.sync_with_descriptor_wallet()

            utxos = [u for u in wallet.utxo_cache.get(0, []) if u.address == deposit]
            assert len(utxos) == 2, (
                f"expected two UTXOs on the address, got {len(utxos)}"
            )
            assert all(not u.frozen for u in utxos), (
                "reuse on an address that still holds funds must not be auto-frozen"
            )
            # Full balance remains spendable.
            assert await wallet.get_balance(mixdepth=0) == 1_500_000
        finally:
            await wallet.close()


@pytest.mark.asyncio
async def test_unfrozen_reuse_utxo_is_not_refrozen(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
    funded_miner: tuple[str, str],
) -> None:
    """An explicitly unfrozen reuse UTXO stays spendable across later syncs."""
    miner, miner_addr = funded_miner
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
            max_sats_freeze_reuse=-1,
        )
        try:
            deposit = wallet.get_receive_address(mixdepth=0, index=0)
            await _fund(bitcoin_rpc_config, miner, miner_addr, deposit, 0.01)
            await wallet.setup_descriptor_wallet(scan_range=1000, rescan=True)
            await wallet.sync_with_descriptor_wallet()

            sweep_dest = wallet.get_receive_address(mixdepth=1, index=0)
            await direct_send(
                wallet=wallet,
                backend=backend,
                mixdepth=0,
                amount_sats=0,
                destination=sweep_dest,
                fee_rate=2.0,
            )
            await _rpc(
                bitcoin_rpc_config, "generatetoaddress", [1, miner_addr], wallet=miner
            )
            await wallet.sync_with_descriptor_wallet()

            await _fund(bitcoin_rpc_config, miner, miner_addr, deposit, 0.005)
            await wallet.sync_with_descriptor_wallet()
            reuse = [u for u in wallet.utxo_cache.get(0, []) if u.address == deposit]
            assert len(reuse) == 1 and reuse[0].frozen is True
            frozen_outpoint = reuse[0].outpoint

            # User deliberately unfreezes it.
            wallet.unfreeze_utxo(frozen_outpoint)
            assert wallet.metadata_store is not None
            assert not wallet.metadata_store.is_frozen(frozen_outpoint)

            # A later sync must NOT re-freeze it.
            await wallet.sync_with_descriptor_wallet()
            again = [
                u for u in wallet.utxo_cache.get(0, []) if u.outpoint == frozen_outpoint
            ]
            assert len(again) == 1
            assert again[0].frozen is False, (
                "an explicitly unfrozen reuse UTXO must stay spendable"
            )
        finally:
            await wallet.close()
