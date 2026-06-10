"""E2E test for the forced-address-reuse auto-freeze (issue #529).

Funds a wallet's deposit address, syncs (recording it as used), then funds the
*same* address again to simulate a forced address-reuse (dust) attack, and
asserts that the second arrival is automatically frozen after the next sync so
it is excluded from coin selection (and therefore never co-spent in a CoinJoin,
which would link the wallet's coins via the common-input-ownership heuristic).

Prerequisites:
- Docker and Docker Compose installed
- Run: docker compose --profile e2e up -d

Usage:
    pytest tests/e2e/test_address_reuse_freeze_e2e.py -v -s --timeout=120 -m e2e
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from loguru import logger

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.utxo_metadata import AUTO_FREEZE_REUSE_LABEL

pytestmark = pytest.mark.e2e

# Standard test mnemonic (12 words). Never a real-funds wallet.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


@pytest.fixture
def bitcoin_backend() -> DescriptorWalletBackend:
    return DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="test",
        rpc_password="test",
    )


@pytest_asyncio.fixture
async def reuse_wallet(bitcoin_backend, tmp_path: Path):
    """A wallet with auto-freeze-all-reuse enabled and a fresh Core wallet."""
    from jmwallet.backends.descriptor_wallet import (
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )

    from tests.e2e.rpc_utils import rpc_call

    fingerprint = get_mnemonic_fingerprint(TEST_MNEMONIC, "")
    wallet_name = generate_wallet_name(fingerprint, "regtest")
    # Start from a clean Core wallet so funding history is isolated per run.
    try:
        await rpc_call("unloadwallet", [wallet_name])
    except Exception:
        pass

    wallet = WalletService(
        mnemonic=TEST_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
        data_dir=tmp_path,
        max_sats_freeze_reuse=-1,  # freeze ALL reuse
    )
    try:
        yield wallet
    finally:
        await wallet.close()


@pytest.mark.asyncio
async def test_forced_address_reuse_utxo_is_auto_frozen(
    reuse_wallet: WalletService,
    ensure_blockchain_ready,
) -> None:
    """A second UTXO on an already-used deposit address is auto-frozen.

    Assertions are delta-based (not absolute counts) so the test is robust on a
    reused regtest node where the deterministic deposit address may already
    carry coinbase UTXOs from earlier runs.
    """
    from tests.e2e.rpc_utils import mine_blocks

    wallet = reuse_wallet

    # First-time descriptor setup (no rescan needed for a fresh wallet).
    await wallet.setup_descriptor_wallet(rescan=False, fidelity_bond_addresses=None)

    deposit_address = wallet.get_receive_address(0, 0)
    logger.info(f"Deposit address: {deposit_address}")

    # 1) Initial funding + sync. On a fresh node this is a single UTXO; on a
    #    reused node there may be several. None are frozen, because on the very
    #    first sight of the address it is recorded as used only AFTER scanning
    #    (so an address is "reuse" only on a later sync).
    await mine_blocks(1, deposit_address)
    await mine_blocks(110, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080")

    await wallet.sync_with_descriptor_wallet()
    before = [u for u in wallet.utxo_cache.get(0, []) if u.address == deposit_address]
    assert before, "expected at least one deposit UTXO after funding"
    assert all(not u.frozen for u in before), (
        "initial deposits on a freshly-recorded address must stay spendable"
    )
    spendable_before = {u.outpoint for u in before if not u.frozen}
    assert deposit_address in wallet.addresses_with_history

    # 2) Forced reuse: a second coinbase to the SAME address, then mature.
    await mine_blocks(1, deposit_address)
    await mine_blocks(110, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080")

    await wallet.sync_with_descriptor_wallet()
    after = [u for u in wallet.utxo_cache.get(0, []) if u.address == deposit_address]
    new_utxos = [u for u in after if u.outpoint not in spendable_before]
    assert len(new_utxos) == 1, (
        f"expected exactly one new reuse UTXO, got {len(new_utxos)}"
    )
    reuse_utxo = new_utxos[0]

    # The newly arrived reuse UTXO is auto-frozen; previously-spendable UTXOs
    # are untouched.
    assert reuse_utxo.frozen is True, "the reuse UTXO must be auto-frozen"
    still_spendable = {
        u.outpoint for u in after if not u.frozen and u.outpoint in spendable_before
    }
    assert still_spendable == spendable_before, (
        "previously-spendable UTXOs must remain spendable"
    )

    # The freeze is persisted and labeled as an automatic reuse freeze.
    assert wallet.metadata_store is not None
    assert wallet.metadata_store.is_frozen(reuse_utxo.outpoint)
    record = wallet.metadata_store.records[reuse_utxo.outpoint]
    assert record.label == AUTO_FREEZE_REUSE_LABEL

    # The frozen reuse UTXO is excluded from the spendable balance.
    spendable_balance = await wallet.get_balance(0)
    expected_spendable = sum(u.value for u in after if not u.frozen)
    assert spendable_balance == expected_spendable, (
        "frozen reuse UTXO must be excluded from the spendable balance"
    )

    logger.info(
        f"Auto-froze reuse UTXO {reuse_utxo.outpoint} ({reuse_utxo.value} sats); "
        f"{len(spendable_before)} earlier UTXO(s) stay spendable."
    )


@pytest.mark.asyncio
async def test_unfrozen_reuse_utxo_is_not_refrozen(
    reuse_wallet: WalletService,
    ensure_blockchain_ready,
) -> None:
    """An explicitly unfrozen reuse UTXO stays spendable across later syncs."""
    from tests.e2e.rpc_utils import mine_blocks

    wallet = reuse_wallet
    await wallet.setup_descriptor_wallet(rescan=False, fidelity_bond_addresses=None)
    deposit_address = wallet.get_receive_address(0, 0)

    await mine_blocks(1, deposit_address)
    await mine_blocks(110, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080")
    await wallet.sync_with_descriptor_wallet()
    before = {
        u.outpoint
        for u in wallet.utxo_cache.get(0, [])
        if u.address == deposit_address and not u.frozen
    }

    await mine_blocks(1, deposit_address)
    await mine_blocks(110, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080")
    await wallet.sync_with_descriptor_wallet()

    after = [u for u in wallet.utxo_cache.get(0, []) if u.address == deposit_address]
    new_frozen = [u for u in after if u.frozen and u.outpoint not in before]
    assert len(new_frozen) == 1, "exactly one new reuse UTXO must be auto-frozen"
    frozen_outpoint = new_frozen[0].outpoint

    # User deliberately unfreezes the reuse UTXO.
    wallet.unfreeze_utxo(frozen_outpoint)
    assert wallet.metadata_store is not None
    assert not wallet.metadata_store.is_frozen(frozen_outpoint)

    # A subsequent sync must NOT re-freeze it.
    await wallet.sync_with_descriptor_wallet()
    again = [u for u in wallet.utxo_cache.get(0, []) if u.outpoint == frozen_outpoint]
    assert len(again) == 1
    assert again[0].frozen is False, (
        "an explicitly unfrozen reuse UTXO must stay spendable"
    )
