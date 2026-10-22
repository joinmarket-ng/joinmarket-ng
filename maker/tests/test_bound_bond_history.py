"""Bound histories serialize actual proof admission with lease designation."""

from __future__ import annotations

import asyncio
import os
import select
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from jmcore.bond_advertisements import (
    advertisement_record_path,
    initialize_fresh_advertisements,
    read_advertisements,
    record_advertisement,
)
from jmcore.market_store import MarketStore
from jmcore.paths import get_market_store_path, get_used_commitments_path
from test_bond_lease_guard import _bond, _bot

from maker import bond_leases
from maker.bond_leases import BondLeaseGuard, bond_references

WALLET = "a1" * 32


def _activate(directory: Path) -> MarketStore:
    initialize_fresh_advertisements(
        advertisement_record_path(directory), WALLET, "regtest", history_confirmed=True
    )
    store = MarketStore(get_market_store_path(directory), wallet_id=WALLET)
    store.activate_wallet(
        get_used_commitments_path(directory, create=False),
        history_confirmed=True,
        bound_advertisements_network="regtest",
    )
    return store


@pytest.mark.parametrize("replacement", ["missing", "legacy", "corrupt"])
async def test_cached_proof_and_absent_guard_never_bypass_required_history(
    tmp_path: Path,
    replacement: str,
) -> None:
    store = _activate(tmp_path)
    try:
        bond = _bond(0xAA)
        guard = BondLeaseGuard(store.path, "regtest", wallet_id=WALLET)
        await guard.refresh([bond], 1, advertising=bond)
        assert guard.allows(bond)
        path = advertisement_record_path(tmp_path)
        path.unlink()
        if replacement == "legacy":
            record_advertisement(path, 0)
        elif replacement == "corrupt":
            path.write_bytes(b'{"version":2,')
        assert not guard.allows(bond)
        await guard.refresh([bond], 1, advertising=bond)
        assert not guard.allows(bond)
        bot = _bot(tmp_path)
        bot.fidelity_bond = bond
        assert bot.bond_lease_guard is None and bot._advertised_bond() is None
    finally:
        store.close()


async def test_file_first_without_ready_ledger_never_publishes_a_proof(tmp_path: Path) -> None:
    initialize_fresh_advertisements(
        advertisement_record_path(tmp_path), WALLET, "regtest", history_confirmed=True
    )
    bond = _bond(0xAA)
    guard = BondLeaseGuard(get_market_store_path(tmp_path), "regtest", wallet_id=WALLET)
    await guard.refresh([bond], 1, advertising=bond)
    assert not guard.allows(bond)
    record = read_advertisements(guard.record_path, wallet_id=WALLET, network="regtest")
    assert record is not None and record.rentable((bond.txid, bond.vout), 0)


async def test_guided_fresh_setup_reaches_rental_admission_without_legacy_period_delay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from jmcore.settings import validate_config_document
    from jmwallet import setup_wallet
    from jmwallet.setup_market import prepare_seller_ledger
    from pydantic import SecretStr
    from test_market_sales import MNEMONIC, _owned_bond, _sales

    from maker import market_sales

    wallet_file = tmp_path / "wallets/default.mnemonic"
    monkeypatch.setattr(setup_wallet, "generate_mnemonic_secure", lambda _word_count: MNEMONIC)
    publication = setup_wallet.publish_setup_wallet(
        wallet_file,
        SecretStr("test-only-private-publication"),
        operation="generate",
    )
    settings = validate_config_document(
        {
            "data_dir": str(tmp_path),
            "network_config": {"network": "regtest"},
            "wallet": {"address_type": "p2tr"},
            "lightning": {"mode": "manual"},
        }
    )
    assert prepare_seller_ledger(
        settings,
        SecretStr(MNEMONIC),
        SecretStr(""),
        data_dir=tmp_path,
        wallet_file=wallet_file,
        publication=publication,
        activate=True,
        history_excluded_since_generation=True,
    )
    sales, bot, _, _ = _sales(tmp_path)
    sales._seller = None
    advertised, idle = _owned_bond(0xAA), _owned_bond(0xBB)
    bot.fidelity_bond = advertised
    monkeypatch.setattr(
        market_sales, "find_fidelity_bonds", AsyncMock(return_value=[advertised, idle])
    )
    try:
        assert await sales._rentals(1000) == [idle]
        guard = BondLeaseGuard(
            get_market_store_path(tmp_path), "regtest", wallet_id=bot.wallet.market_wallet_id
        )
        await guard.refresh([idle], 1000, advertising=idle)
        assert guard.allows(idle)
        assert await sales._rentals(1000) == []
    finally:
        await sales.stop()
        await bot.wallet.close()


_CHILD = """
import asyncio
import sys
from pathlib import Path
from jmcore.credential_market import BondReference
from jmcore.market_store import MarketStore, MarketStoreError
from maker.bond_leases import BondLeaseGuard
from maker.fidelity import FidelityBondInfo
mode, path, wallet, encoded = sys.argv[1:]
reference = BondReference.model_validate_json(encoded)
print('attempt', flush=True)
if mode == 'stock':
    with MarketStore(Path(path), wallet_id=wallet, create=False) as store:
        try:
            store.add_bond_inventory(reference, 0)
        except MarketStoreError as exc:
            print(type(exc).__name__, flush=True)
        else:
            print('stocked', flush=True)
else:
    bond = FidelityBondInfo(txid=reference.outpoint.txid, vout=reference.outpoint.vout,
        value=100000000, locktime=reference.locktime, confirmation_time=1, bond_value=1000,
        pubkey=bytes.fromhex(reference.pubkey))
    guard = BondLeaseGuard(Path(path), 'regtest', wallet_id=wallet)
    asyncio.run(guard.refresh([bond], 1, advertising=bond))
    print('allowed' if guard.allows(bond) else 'blocked', flush=True)
"""


def _child(mode: str, store: MarketStore) -> subprocess.Popen[str]:
    reference = bond_references([_bond(0xAA)], "regtest")[0]
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, mode, str(store.path), WALLET, reference.model_dump_json()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX pipe readiness and flock")
@pytest.mark.parametrize("first", ["proof", "stock"])
async def test_independent_process_proof_and_stock_share_the_decision_to_commit_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first: str,
) -> None:
    store = _activate(tmp_path)
    entered, release = threading.Event(), threading.Event()
    child: subprocess.Popen[str] | None = None
    task: asyncio.Task[Any] | None = None
    bond = _bond(0xAA)
    reference = bond_references([bond], "regtest")[0]
    guard = BondLeaseGuard(store.path, "regtest", wallet_id=WALLET)
    try:
        if first == "proof":
            scan = bond_leases.blocked_bond_outpoints

            def paused_scan(*args: Any, **kwargs: Any) -> Any:
                entered.set()
                assert release.wait(10), "proof admission was not released"
                return scan(*args, **kwargs)

            monkeypatch.setattr(bond_leases, "blocked_bond_outpoints", paused_scan)
            task = asyncio.create_task(guard.refresh([bond], 1, advertising=bond))
        else:
            insert = store._insert_inventory

            def paused_insert(*args: Any, **kwargs: Any) -> None:
                entered.set()
                assert release.wait(10), "stock admission was not released"
                insert(*args, **kwargs)

            monkeypatch.setattr(store, "_insert_inventory", paused_insert)
            task = asyncio.create_task(asyncio.to_thread(store.add_bond_inventory, reference, 0))
        assert await asyncio.to_thread(entered.wait, 5)
        child = _child("stock" if first == "proof" else "proof", store)
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 10)[0]
        assert child.stdout.readline() == "attempt\n"
        if first == "proof":
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            assert stdout == "MarketStoreUnavailableError\n"
        else:
            assert not select.select([child.stdout], [], [], 0.2)[0], (
                "proof escaped stock exclusion"
            )
        release.set()
        await task
        if first == "proof":
            assert guard.allows(bond)
            retry = _child("stock", store)
            try:
                stdout, stderr = retry.communicate(timeout=10)
                assert retry.returncode == 0, stderr
                assert stdout == "attempt\nMarketStoreConflictError\n"
            finally:
                if retry.poll() is None:
                    retry.kill()
                    retry.communicate(timeout=5)
        else:
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, stderr
            assert stdout == "blocked\n"
            record = read_advertisements(guard.record_path, wallet_id=WALLET, network="regtest")
            assert record is not None and record.rentable((bond.txid, bond.vout), 0)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
        store.close()
