"""
E2E test for the fidelity bond lifecycle: create, fund, sync, and spend.

Regression coverage for the JAM "move bond to jar" failure (expired bonds
could not be spent):

1. A bond created with an already-expired locktime must survive a plain
   ``sync()`` (the refresh jmwalletd performs right before a direct send)
   with its locktime attached. Previously ``_sync_all_with_descriptors``
   dropped the locktime, so the bond masqueraded as a regular UTXO and then
   failed to sign with "Cannot sign P2WSH UTXO ... locktime not available".
2. A sweep (``direct_send`` with ``amount_sats=0``) must include the expired
   bond, broadcast successfully, and actually move the coins. Previously
   ``select_spendable_utxos`` excluded all fidelity bonds, expired or not.
3. A bond whose timelock has NOT expired must never be swept.

Advertising (maker announces the bond, orderbook watcher receives and
validates it) is covered by ``test_fidelity_bonds_e2e.py`` and
``test_reference_fidelity_bonds_validation.py``.

Prerequisites:
- Docker and Docker Compose installed
- Run: docker compose --profile e2e up -d

Usage:
    pytest tests/e2e/test_fidelity_bond_lifecycle_e2e.py -v -s --timeout=300 -m e2e
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from jmcore.timenumber import get_nearest_valid_locktime, timestamp_to_timenumber
from loguru import logger

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.cli.mnemonic import generate_mnemonic_secure
from jmwallet.wallet.bond_registry import (
    create_bond_info,
    load_registry,
    save_registry,
)
from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.spend import direct_send

# Mark all tests in this module as requiring Docker e2e profile
pytestmark = pytest.mark.e2e

NETWORK = "regtest"
BOND_AMOUNT_BTC = 0.01
FEE_RATE_SAT_VB = 2.0
# Fallback miner address for confirmation blocks when the test-funder wallet
# is unavailable (never holds spendable funds we care about).
DUMMY_MINER_ADDR = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"


def _expired_locktime() -> tuple[int, int]:
    """Return ``(timenumber, locktime)`` for a locktime safely in the past."""
    locktime = get_nearest_valid_locktime(int(time.time()) - 90 * 86400, round_up=False)
    return timestamp_to_timenumber(locktime), locktime


def _locked_locktime() -> tuple[int, int]:
    """Return ``(timenumber, locktime)`` for a locktime safely in the future."""
    locktime = get_nearest_valid_locktime(int(time.time()) + 365 * 86400, round_up=True)
    return timestamp_to_timenumber(locktime), locktime


async def _make_wallet(
    bitcoin_rpc_config: dict[str, str], data_dir: Path
) -> WalletService:
    """Create a WalletService on a fresh random mnemonic (no cross-run state)."""
    mnemonic = generate_mnemonic_secure(12)
    backend = DescriptorWalletBackend(
        rpc_url=bitcoin_rpc_config["rpc_url"],
        rpc_user=bitcoin_rpc_config["rpc_user"],
        rpc_password=bitcoin_rpc_config["rpc_password"],
    )
    ws = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=NETWORK,
        mixdepth_count=5,
        data_dir=data_dir,
    )
    wallet_name = f"fb_lifecycle_{ws.wallet_fingerprint}"
    backend.wallet_name = wallet_name
    await backend.create_wallet()
    return ws


def _register_bond(ws: WalletService, timenumber: int, locktime: int) -> str:
    """Create a bond address and record it, as ``generate-bond-address`` does."""
    address = ws.get_fidelity_bond_address(timenumber, locktime)
    witness_script = ws.get_fidelity_bond_script(timenumber, locktime)
    key = ws.get_fidelity_bond_key(timenumber, locktime)
    coin_type = 0 if NETWORK == "mainnet" else 1
    path = f"m/84'/{coin_type}'/0'/{FIDELITY_BOND_BRANCH}/{timenumber}"

    assert ws.data_dir is not None
    registry = load_registry(
        ws.data_dir, ws.wallet_fingerprint, allow_legacy_fallback=False
    )
    registry.add_bond(
        create_bond_info(
            address=address,
            locktime=locktime,
            index=timenumber,
            path=path,
            pubkey_hex=key.get_public_key_bytes(compressed=True).hex(),
            witness_script=witness_script,
            network=NETWORK,
        )
    )
    save_registry(registry, ws.data_dir, ws.wallet_fingerprint)
    return address


async def _fund_address(address: str, amount_btc: float) -> None:
    from tests.e2e.rpc_utils import mine_blocks, send_from_test_funder

    funded = await send_from_test_funder(address, amount_btc, confirmations=1)
    if not funded:
        # Fallback: mine a coinbase to the address and mature it.
        await mine_blocks(1, address)
        await mine_blocks(110, DUMMY_MINER_ADDR)


def _bond_utxos(ws: WalletService, address: str) -> list:
    return [u for u in ws.utxo_cache.get(0, []) if u.address == address]


@pytest.mark.asyncio
async def test_expired_bond_lifecycle_create_sync_spend(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready,
    tmp_path: Path,
) -> None:
    """Full lifecycle of an expired bond: create, fund, sync, sweep out."""
    from tests.e2e.rpc_utils import mine_blocks

    ws = await _make_wallet(bitcoin_rpc_config, tmp_path)
    try:
        # 1) Create: derive + register a bond with an already-expired locktime
        #    (JAM allows creating these; the reported bug used one).
        timenumber, locktime = _expired_locktime()
        bond_address = _register_bond(ws, timenumber, locktime)
        logger.info(f"Bond address: {bond_address} (locktime={locktime})")

        # 2) Fund the bond on-chain.
        await _fund_address(bond_address, BOND_AMOUNT_BTC)

        # 3) Bond-aware sync (daemon wallet-open path): the bond UTXO must be
        #    visible in mixdepth 0 with its locktime.
        await ws.sync_with_registered_bonds()
        bonds = _bond_utxos(ws, bond_address)
        assert bonds, "Funded bond UTXO not found after sync_with_registered_bonds"
        assert all(u.locktime == locktime for u in bonds)
        assert all(u.is_fidelity_bond for u in bonds)
        bond_value = sum(u.value for u in bonds)
        assert bond_value > 0

        # 4) Plain sync, exactly what jmwalletd's direct-send handler runs
        #    before spending. Regression: this used to drop the locktime,
        #    making the bond unsignable (and unsafely auto-spendable).
        await ws.sync()
        bonds = _bond_utxos(ws, bond_address)
        assert bonds, "Bond UTXO disappeared after plain sync()"
        assert all(u.locktime == locktime for u in bonds), (
            "Plain sync() lost the bond locktime (signer would fail with "
            "'locktime not available')"
        )

        # 5) Spend: sweep mixdepth 0 to our own mixdepth-1 address (the JAM
        #    "move bond to jar" flow). Regression: expired bonds used to be
        #    excluded from sweeps entirely.
        destination = ws.get_receive_address(1, 0)
        result = await direct_send(
            wallet=ws,
            backend=ws.backend,
            mixdepth=0,
            amount_sats=0,
            destination=destination,
            fee_rate=FEE_RATE_SAT_VB,
        )
        assert result.txid, "Sweep of expired bond was not broadcast"
        assert result.num_inputs == len(bonds)
        assert result.send_amount == bond_value - result.fee
        logger.info(f"Swept expired bond in {result.txid} (fee={result.fee})")

        # 6) Confirm and verify the coins actually moved.
        await mine_blocks(1, DUMMY_MINER_ADDR)
        await ws.sync_with_registered_bonds()
        assert not _bond_utxos(ws, bond_address), "Bond UTXO still unspent after sweep"
        md1_values = [
            u.value for u in ws.utxo_cache.get(1, []) if u.address == destination
        ]
        assert md1_values == [result.send_amount], (
            f"Swept funds not found at destination: {md1_values}"
        )
    finally:
        await ws.close()


@pytest.mark.asyncio
async def test_locked_bond_is_visible_but_not_spendable(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready,
    tmp_path: Path,
) -> None:
    """A funded, still-locked bond must sync with its locktime and never sweep."""
    ws = await _make_wallet(bitcoin_rpc_config, tmp_path)
    try:
        timenumber, locktime = _locked_locktime()
        bond_address = _register_bond(ws, timenumber, locktime)
        logger.info(f"Locked bond address: {bond_address} (locktime={locktime})")

        await _fund_address(bond_address, BOND_AMOUNT_BTC)

        await ws.sync_with_registered_bonds()
        bonds = _bond_utxos(ws, bond_address)
        assert bonds, "Funded locked bond UTXO not found after sync"
        assert all(u.locktime == locktime and u.is_locked for u in bonds)

        # Plain sync must also keep the locktime; without it the locked bond
        # would be auto-spendable, which is the dangerous half of the bug.
        await ws.sync()
        bonds = _bond_utxos(ws, bond_address)
        assert bonds and all(u.locktime == locktime for u in bonds)

        # The only coin in mixdepth 0 is the locked bond: a sweep must refuse.
        destination = ws.get_receive_address(1, 0)
        with pytest.raises(ValueError, match="No spendable UTXOs"):
            await direct_send(
                wallet=ws,
                backend=ws.backend,
                mixdepth=0,
                amount_sats=0,
                destination=destination,
                fee_rate=FEE_RATE_SAT_VB,
            )

        # The bond is still there, untouched.
        await ws.sync_with_registered_bonds()
        assert _bond_utxos(ws, bond_address), "Locked bond UTXO went missing"
    finally:
        await ws.close()
