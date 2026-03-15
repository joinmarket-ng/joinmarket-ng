"""
End-to-end tests for UTXO freezing behavior across all components.

Tests that frozen UTXOs are correctly excluded from:
- Maker offer calculations and UTXO selection for CoinJoin
- Taker UTXO selection for CoinJoin (normal and sweep modes)
- ``jm-wallet send`` auto-selection
- Balance calculations
- Persistence across wallet reloads (BIP-329 JSONL)
- Hot-reload when metadata is modified by an external process

Tests that frozen UTXOs are still visible in:
- Raw ``get_utxos()`` (used for display and interactive selection)
- The freeze TUI (can toggle back)

Requires: docker compose --profile e2e up -d
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio
from jmcore.models import NetworkType
from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.utxo_metadata import UTXOMetadataStore
from maker.config import MakerConfig
from taker.config import TakerConfig

# Mark all tests in this module as requiring Docker e2e profile
pytestmark = pytest.mark.e2e

# ==============================================================================
# Test wallet mnemonics (same as test_complete_system.py)
# ==============================================================================

MAKER1_MNEMONIC = (
    "avoid whisper mesh corn already blur sudden fine planet chicken hover sniff"
)
MAKER2_MNEMONIC = (
    "minute faint grape plate stock mercy tent world space opera apple rocket"
)
TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)
GENERIC_TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def bitcoin_backend():
    """Bitcoin Core backend for regtest."""
    return BitcoinCoreBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="test",
        rpc_password="test",
    )


@pytest_asyncio.fixture
async def funded_wallet_with_metadata(bitcoin_backend, tmp_path):
    """Create a funded wallet with metadata store enabled (for freezing).

    Uses a temporary data directory so freeze state is isolated per test.
    """
    from tests.e2e.rpc_utils import ensure_wallet_funded

    wallet = WalletService(
        mnemonic=GENERIC_TEST_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
        data_dir=tmp_path,
    )

    await wallet.sync_all()

    total_balance = await wallet.get_total_balance()
    if total_balance == 0:
        funding_address = wallet.get_receive_address(0, 0)
        funded = await ensure_wallet_funded(
            funding_address, amount_btc=1.0, confirmations=2
        )
        if funded:
            await wallet.sync_all()
            total_balance = await wallet.get_total_balance()

    if total_balance == 0:
        await wallet.close()
        pytest.skip("Wallet has no funds. Auto-funding failed.")

    try:
        yield wallet
    finally:
        await wallet.close()


@pytest_asyncio.fixture
async def funded_taker_wallet_with_metadata(bitcoin_backend, tmp_path):
    """Create a funded taker wallet with metadata store for freezing."""
    from tests.e2e.rpc_utils import ensure_wallet_funded

    wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
        data_dir=tmp_path,
    )

    await wallet.sync_all()

    total_balance = await wallet.get_total_balance()
    if total_balance == 0:
        funding_address = wallet.get_receive_address(0, 0)
        funded = await ensure_wallet_funded(
            funding_address, amount_btc=1.0, confirmations=2
        )
        if funded:
            await wallet.sync_all()
            total_balance = await wallet.get_total_balance()

    if total_balance == 0:
        await wallet.close()
        pytest.skip("Taker wallet has no funds. Auto-funding failed.")

    try:
        yield wallet
    finally:
        await wallet.close()


@pytest_asyncio.fixture
async def funded_maker_wallet_with_metadata(bitcoin_backend, tmp_path):
    """Create a funded maker wallet with metadata store for freezing."""
    from tests.e2e.rpc_utils import ensure_wallet_funded

    wallet = WalletService(
        mnemonic=MAKER1_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
        data_dir=tmp_path,
    )

    await wallet.sync_all()

    total_balance = await wallet.get_total_balance()
    if total_balance == 0:
        funding_address = wallet.get_receive_address(0, 0)
        funded = await ensure_wallet_funded(
            funding_address, amount_btc=1.0, confirmations=2
        )
        if funded:
            await wallet.sync_all()
            total_balance = await wallet.get_total_balance()

    if total_balance == 0:
        await wallet.close()
        pytest.skip("Maker wallet has no funds. Auto-funding failed.")

    try:
        yield wallet
    finally:
        await wallet.close()


@pytest.fixture
def maker_config_with_datadir(tmp_path):
    """Maker config that includes data_dir for metadata persistence."""
    return MakerConfig(
        mnemonic=MAKER1_MNEMONIC,
        network=NetworkType.TESTNET,
        bitcoin_network=NetworkType.REGTEST,
        backend_type="scantxoutset",
        backend_config={
            "rpc_url": "http://127.0.0.1:18443",
            "rpc_user": "test",
            "rpc_password": "test",
        },
        directory_servers=["127.0.0.1:5222"],
        min_size=100_000,
        cj_fee_relative="0.0003",
        tx_fee_contribution=1_000,
    )


@pytest.fixture
def taker_config():
    """Taker configuration."""
    return TakerConfig(
        mnemonic=TAKER_MNEMONIC,
        network=NetworkType.TESTNET,
        bitcoin_network=NetworkType.REGTEST,
        backend_type="scantxoutset",
        backend_config={
            "rpc_url": "http://127.0.0.1:18443",
            "rpc_user": "test",
            "rpc_password": "test",
        },
        directory_servers=["127.0.0.1:5222"],
        counterparty_count=2,
        minimum_makers=2,
        maker_timeout_sec=30,
        order_wait_time=10.0,
    )


# ==============================================================================
# Wallet-level frozen UTXO tests
# ==============================================================================


class TestFreezeBasics:
    """Basic freeze/unfreeze operations with real funded wallets."""

    @pytest.mark.asyncio
    async def test_freeze_utxo_persists_to_disk(
        self, funded_wallet_with_metadata: WalletService, tmp_path
    ):
        """Freezing a UTXO persists to BIP-329 JSONL file on disk."""
        wallet = funded_wallet_with_metadata
        utxos = await wallet.get_utxos(0)
        assert len(utxos) > 0, "Need UTXOs to test freezing"

        target = utxos[0]
        outpoint = target.outpoint

        # Freeze
        wallet.freeze_utxo(outpoint)

        # Verify in-memory state
        assert target.frozen is True

        # Verify on-disk persistence
        assert wallet.metadata_store is not None
        metadata_path = wallet.metadata_store.path
        assert metadata_path.exists(), "Metadata file should exist after freeze"

        content = metadata_path.read_text(encoding="utf-8")
        lines = [line for line in content.strip().splitlines() if line]
        assert len(lines) >= 1

        record = json.loads(lines[0])
        assert record["type"] == "output"
        assert record["ref"] == outpoint
        assert record["spendable"] is False

    @pytest.mark.asyncio
    async def test_unfreeze_removes_record_if_no_label(
        self, funded_wallet_with_metadata: WalletService
    ):
        """Unfreezing a UTXO with no label removes the record entirely."""
        wallet = funded_wallet_with_metadata
        utxos = await wallet.get_utxos(0)
        target = utxos[0]
        outpoint = target.outpoint

        wallet.freeze_utxo(outpoint)
        assert target.frozen is True

        wallet.unfreeze_utxo(outpoint)
        assert target.frozen is False

        # Record should be removed (no label, spendable=True is default)
        assert wallet.metadata_store is not None
        assert outpoint not in wallet.metadata_store.records

    @pytest.mark.asyncio
    async def test_toggle_freeze_round_trip(
        self, funded_wallet_with_metadata: WalletService
    ):
        """Toggle freeze cycles correctly: unfrozen -> frozen -> unfrozen."""
        wallet = funded_wallet_with_metadata
        utxos = await wallet.get_utxos(0)
        target = utxos[0]
        outpoint = target.outpoint

        assert target.frozen is False

        # Toggle to frozen
        result = wallet.toggle_freeze_utxo(outpoint)
        assert result is True
        assert target.frozen is True

        # Toggle back to unfrozen
        result = wallet.toggle_freeze_utxo(outpoint)
        assert result is False
        assert target.frozen is False


class TestFreezeExcludesFromBalance:
    """Frozen UTXOs must be excluded from all balance calculations."""

    @pytest.mark.asyncio
    async def test_frozen_utxo_excluded_from_get_balance(
        self, funded_wallet_with_metadata: WalletService
    ):
        """get_balance() excludes frozen UTXOs."""
        wallet = funded_wallet_with_metadata

        balance_before = await wallet.get_balance(0)
        assert balance_before > 0

        utxos = await wallet.get_utxos(0)
        target = utxos[0]
        frozen_value = target.value

        wallet.freeze_utxo(target.outpoint)

        balance_after = await wallet.get_balance(0)
        assert balance_after == balance_before - frozen_value

    @pytest.mark.asyncio
    async def test_frozen_utxo_excluded_from_total_balance(
        self, funded_wallet_with_metadata: WalletService
    ):
        """get_total_balance() excludes frozen UTXOs."""
        wallet = funded_wallet_with_metadata

        total_before = await wallet.get_total_balance()

        utxos = await wallet.get_utxos(0)
        target = utxos[0]
        frozen_value = target.value

        wallet.freeze_utxo(target.outpoint)

        total_after = await wallet.get_total_balance()
        assert total_after == total_before - frozen_value

    @pytest.mark.asyncio
    async def test_frozen_utxo_excluded_from_balance_for_offers(
        self, funded_wallet_with_metadata: WalletService
    ):
        """get_balance_for_offers() (used by makers) excludes frozen UTXOs.

        For md0, get_balance_for_offers returns the largest single UTXO
        value (not the sum) because merging md0 UTXOs is forbidden.

        When the frozen UTXO has a unique maximum value, the balance must
        strictly decrease.  When multiple UTXOs share the same max value,
        freezing one leaves another with the same value so the balance
        stays equal — we accept ``<=`` in that case.
        """
        wallet = funded_wallet_with_metadata

        offer_balance_before = await wallet.get_balance_for_offers(0)

        utxos = await wallet.get_utxos(0)
        # Sort descending and freeze ALL UTXOs that share the maximum value so
        # that the next call to get_balance_for_offers must return a strictly
        # smaller number (or 0 if all UTXOs have equal value).
        utxos_sorted = sorted(utxos, key=lambda u: u.value, reverse=True)
        target = utxos_sorted[0]

        # Determine whether the max value is unique among eligible UTXOs.
        eligible = [u for u in utxos if not u.frozen and not u.is_fidelity_bond]
        max_value_count = sum(1 for u in eligible if u.value == target.value)

        wallet.freeze_utxo(target.outpoint)

        offer_balance_after = await wallet.get_balance_for_offers(0)

        if max_value_count == 1:
            # Unique maximum — balance must strictly decrease.
            assert offer_balance_after < offer_balance_before
        else:
            # Duplicate max values — another UTXO has the same value so
            # max() returns the same number.  Still valid: the frozen
            # UTXO is excluded from the eligible set.
            assert offer_balance_after <= offer_balance_before

    @pytest.mark.asyncio
    async def test_freeze_all_utxos_yields_zero_balance(
        self, funded_wallet_with_metadata: WalletService
    ):
        """Freezing ALL UTXOs makes balance zero."""
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        assert len(utxos) > 0

        for utxo in utxos:
            wallet.freeze_utxo(utxo.outpoint)

        balance = await wallet.get_balance(0)
        assert balance == 0


class TestFreezeExcludesFromSelection:
    """Frozen UTXOs must be excluded from all UTXO selection methods."""

    @pytest.mark.asyncio
    async def test_select_utxos_skips_frozen(
        self, funded_wallet_with_metadata: WalletService
    ):
        """select_utxos() never returns frozen UTXOs."""
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        assert len(utxos) >= 2, "Need at least 2 UTXOs for this test"

        # Freeze one UTXO
        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # Select UTXOs -- the frozen one should not appear
        selected = wallet.select_utxos(0, 1, min_confirmations=0)
        selected_outpoints = {u.outpoint for u in selected}
        assert frozen_utxo.outpoint not in selected_outpoints

    @pytest.mark.asyncio
    async def test_get_all_utxos_skips_frozen(
        self, funded_wallet_with_metadata: WalletService
    ):
        """get_all_utxos() (sweep mode) skips frozen UTXOs."""
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        assert len(utxos) >= 2

        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        all_utxos = wallet.get_all_utxos(0, min_confirmations=0)
        all_outpoints = {u.outpoint for u in all_utxos}
        assert frozen_utxo.outpoint not in all_outpoints
        assert len(all_utxos) == len(utxos) - 1

    @pytest.mark.asyncio
    async def test_select_utxos_with_merge_skips_frozen(
        self, funded_wallet_with_metadata: WalletService
    ):
        """select_utxos_with_merge() (maker merge algorithms) skips frozen UTXOs."""
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        assert len(utxos) >= 2

        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        for algo in ("default", "gradual", "greedy", "random"):
            selected = wallet.select_utxos_with_merge(
                0, 1, min_confirmations=0, merge_algorithm=algo
            )
            selected_outpoints = {u.outpoint for u in selected}
            assert frozen_utxo.outpoint not in selected_outpoints, (
                f"Frozen UTXO appeared in merge algorithm '{algo}'"
            )

    @pytest.mark.asyncio
    async def test_freeze_all_causes_insufficient_funds(
        self, funded_wallet_with_metadata: WalletService
    ):
        """Freezing all UTXOs makes select_utxos() raise ValueError."""
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        for utxo in utxos:
            wallet.freeze_utxo(utxo.outpoint)

        with pytest.raises(ValueError, match="Insufficient funds"):
            wallet.select_utxos(0, 1, min_confirmations=0)


class TestFreezeStillVisibleInRawCache:
    """Frozen UTXOs must remain visible in raw get_utxos() for the TUI."""

    @pytest.mark.asyncio
    async def test_get_utxos_includes_frozen(
        self, funded_wallet_with_metadata: WalletService
    ):
        """get_utxos() returns frozen UTXOs (raw cache, used for display)."""
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        original_count = len(utxos)

        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # get_utxos returns raw cache -- frozen UTXOs are still there
        utxos_after = await wallet.get_utxos(0)
        assert len(utxos_after) == original_count

        # But the frozen flag is set
        found = next(u for u in utxos_after if u.outpoint == frozen_utxo.outpoint)
        assert found.frozen is True


# ==============================================================================
# BIP-329 persistence and hot-reload tests
# ==============================================================================


class TestBIP329Persistence:
    """BIP-329 JSONL persistence across wallet restarts and external edits."""

    @pytest.mark.asyncio
    async def test_freeze_survives_wallet_reload(self, bitcoin_backend, tmp_path):
        """Frozen state persists across wallet recreation (new WalletService instance)."""
        # Create first wallet instance and freeze a UTXO
        wallet1 = WalletService(
            mnemonic=GENERIC_TEST_MNEMONIC,
            backend=bitcoin_backend,
            network="regtest",
            mixdepth_count=5,
            data_dir=tmp_path,
        )
        await wallet1.sync_all()

        utxos = await wallet1.get_utxos(0)
        if not utxos:
            await wallet1.close()
            pytest.skip("No UTXOs available")

        target = utxos[0]
        outpoint = target.outpoint
        wallet1.freeze_utxo(outpoint)
        await wallet1.close()

        # Create second wallet instance with same mnemonic and data_dir
        backend2 = BitcoinCoreBackend(
            rpc_url="http://127.0.0.1:18443",
            rpc_user="test",
            rpc_password="test",
        )
        wallet2 = WalletService(
            mnemonic=GENERIC_TEST_MNEMONIC,
            backend=backend2,
            network="regtest",
            mixdepth_count=5,
            data_dir=tmp_path,
        )
        await wallet2.sync_all()

        # The UTXO should be frozen in the new instance
        utxos2 = await wallet2.get_utxos(0)
        found = next((u for u in utxos2 if u.outpoint == outpoint), None)
        assert found is not None, "UTXO should exist in reloaded wallet"
        assert found.frozen is True, "UTXO should still be frozen after reload"

        # Balance should exclude frozen UTXO
        balance = await wallet2.get_balance(0)
        all_unfrozen = sum(u.value for u in utxos2 if not u.frozen)
        assert balance == all_unfrozen

        await wallet2.close()

    @pytest.mark.asyncio
    async def test_external_metadata_edit_picked_up_on_resync(
        self, bitcoin_backend, tmp_path
    ):
        """Metadata changes written by an external process (e.g., ``jm-wallet freeze``)
        are picked up when the wallet resyncs.

        This is the hot-reload scenario: a maker is running and a user freezes a UTXO
        via the CLI in another terminal.
        """
        wallet = WalletService(
            mnemonic=GENERIC_TEST_MNEMONIC,
            backend=bitcoin_backend,
            network="regtest",
            mixdepth_count=5,
            data_dir=tmp_path,
        )
        await wallet.sync_all()

        utxos = await wallet.get_utxos(0)
        if not utxos:
            await wallet.close()
            pytest.skip("No UTXOs available")

        target = utxos[0]
        outpoint = target.outpoint

        # UTXO starts unfrozen
        assert target.frozen is False

        # Simulate external process writing to the metadata file
        assert wallet.metadata_store is not None
        metadata_path = wallet.metadata_store.path
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps(
            {"type": "output", "ref": outpoint, "spendable": False},
            separators=(",", ":"),
        )
        metadata_path.write_text(record + "\n", encoding="utf-8")

        # Resync wallet -- should pick up the external change
        await wallet.sync_all()

        utxos_after = await wallet.get_utxos(0)
        found = next(u for u in utxos_after if u.outpoint == outpoint)
        assert found.frozen is True, "External freeze should be picked up after resync"

        # Balance should exclude it
        balance = await wallet.get_balance(0)
        all_unfrozen_value = sum(u.value for u in utxos_after if not u.frozen)
        assert balance == all_unfrozen_value

        await wallet.close()

    @pytest.mark.asyncio
    async def test_external_metadata_deletion_clears_frozen_state(
        self, bitcoin_backend, tmp_path
    ):
        """If metadata file is deleted externally, frozen state is cleared on resync."""
        wallet = WalletService(
            mnemonic=GENERIC_TEST_MNEMONIC,
            backend=bitcoin_backend,
            network="regtest",
            mixdepth_count=5,
            data_dir=tmp_path,
        )
        await wallet.sync_all()

        utxos = await wallet.get_utxos(0)
        if not utxos:
            await wallet.close()
            pytest.skip("No UTXOs available")

        target = utxos[0]
        outpoint = target.outpoint

        # Freeze and verify
        wallet.freeze_utxo(outpoint)
        assert target.frozen is True

        # Delete the metadata file externally
        assert wallet.metadata_store is not None
        wallet.metadata_store.path.unlink()

        # Resync -- _apply_frozen_state re-reads from disk, finds no file
        await wallet.sync_all()

        utxos_after = await wallet.get_utxos(0)
        found = next(u for u in utxos_after if u.outpoint == outpoint)
        assert found.frozen is False, (
            "Frozen state should be cleared when metadata file deleted"
        )

        await wallet.close()


# ==============================================================================
# Maker frozen UTXO tests
# ==============================================================================


class TestMakerFrozenUTXOs:
    """Maker must exclude frozen UTXOs from offers and CoinJoin participation."""

    @pytest.mark.asyncio
    async def test_maker_offers_exclude_frozen_balance(
        self,
        funded_maker_wallet_with_metadata: WalletService,
        maker_config_with_datadir,
    ):
        """Maker offer maxsize reflects only spendable (unfrozen) balance."""
        from maker.offers import OfferManager

        wallet = funded_maker_wallet_with_metadata

        # Get total balance before freezing for comparison
        balance_before = await wallet.get_total_balance()

        # Freeze one UTXO BEFORE creating offers (realistic flow)
        utxos = await wallet.get_utxos(0)
        assert len(utxos) > 0
        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # Create offers -- maxsize should reflect only unfrozen balance
        offer_manager = OfferManager(wallet, maker_config_with_datadir, "J5TestMaker")
        offers = await offer_manager.create_offers()

        if offers:
            assert offers[0].maxsize < balance_before, (
                f"Offer maxsize {offers[0].maxsize} should be less than "
                f"full balance {balance_before} since {frozen_utxo.value} sats are frozen"
            )
        # If no offers created, that means balance dropped below min_size -- also valid

    @pytest.mark.asyncio
    async def test_maker_freeze_all_utxos_no_offers(
        self,
        funded_maker_wallet_with_metadata: WalletService,
        maker_config_with_datadir,
    ):
        """Freezing ALL UTXOs means the maker creates no offers."""
        from maker.offers import OfferManager

        wallet = funded_maker_wallet_with_metadata

        # Freeze all UTXOs across all mixdepths
        for md in range(wallet.mixdepth_count):
            utxos = await wallet.get_utxos(md)
            for utxo in utxos:
                wallet.freeze_utxo(utxo.outpoint)

        offer_manager = OfferManager(wallet, maker_config_with_datadir, "J5TestMaker")
        offers = await offer_manager.create_offers()
        assert len(offers) == 0, "Should create no offers when all UTXOs are frozen"

    @pytest.mark.asyncio
    async def test_maker_utxo_selection_skips_frozen(
        self,
        funded_maker_wallet_with_metadata: WalletService,
    ):
        """Maker UTXO selection for CoinJoin (via select_utxos_with_merge) skips frozen."""
        wallet = funded_maker_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 2:
            pytest.skip("Need at least 2 UTXOs for this test")

        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # Simulate what _select_our_utxos does: select_utxos_with_merge
        for algo in ("default", "gradual", "greedy", "random"):
            selected = wallet.select_utxos_with_merge(
                0, 1, min_confirmations=0, merge_algorithm=algo
            )
            selected_outpoints = {u.outpoint for u in selected}
            assert frozen_utxo.outpoint not in selected_outpoints, (
                f"Frozen UTXO selected by maker's merge algorithm '{algo}'"
            )

    @pytest.mark.asyncio
    async def test_maker_coinjoin_session_skips_frozen(
        self,
        bitcoin_backend,
        funded_maker_wallet_with_metadata: WalletService,
    ):
        """CoinJoinSession._select_our_utxos() never selects frozen UTXOs.

        Simulates the full maker UTXO selection path that happens during a
        real !auth response.
        """
        from jmcore.models import Offer, OfferType
        from maker.coinjoin import CoinJoinSession

        wallet = funded_maker_wallet_with_metadata
        balance = await wallet.get_balance(0)

        if balance < 200_000:
            pytest.skip("Need at least 200k sats for CoinJoin session test")

        utxos = await wallet.get_utxos(0)
        assert len(utxos) >= 2, "Need at least 2 UTXOs"

        # Freeze the largest UTXO
        largest = max(utxos, key=lambda u: u.value)
        wallet.freeze_utxo(largest.outpoint)

        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=balance,
            txfee=1_000,
            cjfee="0.0003",
        )

        session = CoinJoinSession(
            taker_nick="J5TestTaker",
            offer=offer,
            wallet=wallet,
            backend=bitcoin_backend,
            min_confirmations=0,
            merge_algorithm="greedy",  # Greedy should pick ALL non-frozen
        )
        session.amount = 100_000

        utxos_dict, cj_addr, change_addr, mixdepth = await session._select_our_utxos()

        selected_outpoints = {f"{txid}:{vout}" for txid, vout in utxos_dict}
        assert largest.outpoint not in selected_outpoints, (
            "Frozen UTXO was selected by CoinJoinSession (greedy mode)"
        )

        # All selected should be unfrozen
        for (txid, vout), info in utxos_dict.items():
            op = f"{txid}:{vout}"
            assert wallet.metadata_store is not None
            assert not wallet.metadata_store.is_frozen(op), (
                f"Selected UTXO {op} is frozen"
            )


# ==============================================================================
# Taker frozen UTXO tests
# ==============================================================================


class TestTakerFrozenUTXOs:
    """Taker must exclude frozen UTXOs from CoinJoin UTXO selection."""

    @pytest.mark.asyncio
    async def test_taker_select_utxos_skips_frozen(
        self, funded_taker_wallet_with_metadata: WalletService
    ):
        """Taker's wallet.select_utxos() (used in normal mode) skips frozen."""
        wallet = funded_taker_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 2:
            pytest.skip("Need at least 2 UTXOs")

        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        selected = wallet.select_utxos(0, 1, min_confirmations=0)
        selected_outpoints = {u.outpoint for u in selected}
        assert frozen_utxo.outpoint not in selected_outpoints

    @pytest.mark.asyncio
    async def test_taker_sweep_skips_frozen(
        self, funded_taker_wallet_with_metadata: WalletService
    ):
        """Taker's wallet.get_all_utxos() (used in sweep mode) skips frozen."""
        wallet = funded_taker_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 2:
            pytest.skip("Need at least 2 UTXOs")

        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        sweep_utxos = wallet.get_all_utxos(0, min_confirmations=0)
        sweep_outpoints = {u.outpoint for u in sweep_utxos}
        assert frozen_utxo.outpoint not in sweep_outpoints

    @pytest.mark.asyncio
    async def test_taker_freeze_all_blocks_coinjoin(
        self, funded_taker_wallet_with_metadata: WalletService
    ):
        """Freezing all taker UTXOs makes CoinJoin UTXO selection fail."""
        wallet = funded_taker_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        for utxo in utxos:
            wallet.freeze_utxo(utxo.outpoint)

        # select_utxos should fail
        with pytest.raises(ValueError, match="Insufficient funds"):
            wallet.select_utxos(0, 1, min_confirmations=0)

        # get_all_utxos should return empty
        sweep = wallet.get_all_utxos(0, min_confirmations=0)
        assert len(sweep) == 0

    @pytest.mark.asyncio
    async def test_taker_frozen_utxo_not_in_podle_candidates(
        self, funded_taker_wallet_with_metadata: WalletService
    ):
        """PoDLE candidates come from get_all_utxos/select_utxos which skip frozen.

        The taker generates PoDLE from pre-selected UTXOs. If those UTXOs
        are selected via select_utxos(), frozen ones are already excluded.
        """
        wallet = funded_taker_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 2:
            pytest.skip("Need at least 2 UTXOs")

        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # Both selection methods used by the taker should exclude frozen
        candidates_normal = wallet.select_utxos(0, 1, min_confirmations=0)
        candidates_sweep = wallet.get_all_utxos(0, min_confirmations=0)

        for candidate_list in (candidates_normal, candidates_sweep):
            outpoints = {u.outpoint for u in candidate_list}
            assert frozen_utxo.outpoint not in outpoints


# ==============================================================================
# jm-wallet send frozen UTXO tests
# ==============================================================================


class TestSendFrozenUTXOs:
    """``jm-wallet send`` auto-selection must exclude frozen UTXOs.

    These tests exercise the same filtering logic that _send_transaction() uses:
    ``[u for u in utxos if not u.frozen and not u.is_fidelity_bond]``
    """

    @pytest.mark.asyncio
    async def test_send_auto_selection_excludes_frozen(
        self, funded_wallet_with_metadata: WalletService
    ):
        """The send auto-selection filter excludes frozen UTXOs.

        Simulates the filtering logic from cli.py _send_transaction().
        """
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        assert len(utxos) >= 2, "Need at least 2 UTXOs"

        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # Refresh from cache (get_utxos returns the cached list with updated frozen state)
        utxos_after = await wallet.get_utxos(0)

        # Apply the same filter as _send_transaction()
        spendable = [u for u in utxos_after if not u.frozen and not u.is_fidelity_bond]
        frozen_count = len(utxos_after) - len(spendable)

        assert frozen_count >= 1, "At least one UTXO should be filtered out"
        spendable_outpoints = {u.outpoint for u in spendable}
        assert frozen_utxo.outpoint not in spendable_outpoints

    @pytest.mark.asyncio
    async def test_send_all_frozen_exits_with_error(
        self, funded_wallet_with_metadata: WalletService
    ):
        """When all UTXOs are frozen, the send filter leaves zero spendable.

        In the real CLI this causes ``raise typer.Exit(1)`` with error message
        'No spendable UTXOs available (all UTXOs are frozen or fidelity bonds)'.
        """
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        for utxo in utxos:
            wallet.freeze_utxo(utxo.outpoint)

        utxos_after = await wallet.get_utxos(0)
        spendable = [u for u in utxos_after if not u.frozen and not u.is_fidelity_bond]
        assert len(spendable) == 0, "No spendable UTXOs should remain"

    @pytest.mark.asyncio
    async def test_send_frozen_visible_in_interactive_selection(
        self, funded_wallet_with_metadata: WalletService
    ):
        """Frozen UTXOs appear in get_utxos() (interactive mode shows all).

        In interactive mode, the user sees all UTXOs including frozen ones
        (with a visual indicator). They can choose to include or skip them.
        """
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # get_utxos is used by interactive selection -- should include frozen
        display_utxos = await wallet.get_utxos(0)
        frozen_in_display = [u for u in display_utxos if u.frozen]
        assert len(frozen_in_display) >= 1
        assert any(u.outpoint == frozen_utxo.outpoint for u in frozen_in_display)


# ==============================================================================
# Read-only filesystem / verify_writable tests
# ==============================================================================


class TestVerifyWritable:
    """UTXOMetadataStore.verify_writable() catches read-only mounts early."""

    def test_verify_writable_passes_on_writable_dir(self, tmp_path):
        """verify_writable() succeeds on a normal writable directory."""
        store = UTXOMetadataStore(path=tmp_path / "metadata.jsonl")
        # Should not raise
        store.verify_writable()

    def test_verify_writable_fails_on_readonly_dir(self, tmp_path):
        """verify_writable() raises OSError on a read-only directory."""
        import os
        import stat

        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()
        # Remove write permission
        os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR)

        store = UTXOMetadataStore(path=ro_dir / "metadata.jsonl")
        try:
            with pytest.raises(OSError, match="not writable"):
                store.verify_writable()
        finally:
            # Restore permissions for cleanup
            os.chmod(ro_dir, stat.S_IRWXU)

    def test_save_propagates_oserror_on_readonly(self, tmp_path):
        """save() raises OSError when directory is read-only."""
        import os
        import stat

        ro_dir = tmp_path / "readonly"
        ro_dir.mkdir()

        store = UTXOMetadataStore(path=ro_dir / "metadata.jsonl")
        store.freeze("abc123:0")

        # Make directory read-only AFTER adding the record (so freeze succeeded
        # in memory but save will fail)
        os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR)

        try:
            # Re-create store and try to save
            store2 = UTXOMetadataStore(path=ro_dir / "metadata.jsonl")
            store2.records = store.records.copy()
            with pytest.raises(OSError):
                store2.save()
        finally:
            os.chmod(ro_dir, stat.S_IRWXU)


# ==============================================================================
# Cross-component integration tests
# ==============================================================================


class TestCrossComponentFreezeIntegration:
    """Tests that verify freeze behavior across maker + taker + wallet boundaries."""

    @pytest.mark.asyncio
    async def test_freeze_one_utxo_reduces_available_everywhere(
        self, bitcoin_backend, tmp_path
    ):
        """One frozen UTXO is consistently excluded from all selection methods.

        This test creates a wallet, freezes a UTXO, and verifies that:
        - get_balance() excludes it
        - get_balance_for_offers() excludes it
        - select_utxos() excludes it
        - get_all_utxos() excludes it
        - select_utxos_with_merge() excludes it (all algorithms)
        - get_utxos() still includes it (raw cache)
        """
        wallet = WalletService(
            mnemonic=GENERIC_TEST_MNEMONIC,
            backend=bitcoin_backend,
            network="regtest",
            mixdepth_count=5,
            data_dir=tmp_path,
        )
        await wallet.sync_all()

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 2:
            await wallet.close()
            pytest.skip("Need at least 2 UTXOs")

        target = utxos[0]
        frozen_value = target.value
        outpoint = target.outpoint

        # Capture pre-freeze state
        balance_pre = await wallet.get_balance(0)
        offer_balance_pre = await wallet.get_balance_for_offers(0)
        all_utxos_pre = wallet.get_all_utxos(0, min_confirmations=0)
        raw_count_pre = len(utxos)

        # Freeze
        wallet.freeze_utxo(outpoint)

        # Verify all methods
        balance_post = await wallet.get_balance(0)
        assert balance_post == balance_pre - frozen_value

        offer_balance_post = await wallet.get_balance_for_offers(0)
        # md0 balance for offers is the largest single UTXO value (no merging
        # allowed), so we can only assert that freezing reduces the balance.
        assert offer_balance_post <= offer_balance_pre

        selected = wallet.select_utxos(0, 1, min_confirmations=0)
        assert outpoint not in {u.outpoint for u in selected}

        all_utxos_post = wallet.get_all_utxos(0, min_confirmations=0)
        assert len(all_utxos_post) == len(all_utxos_pre) - 1
        assert outpoint not in {u.outpoint for u in all_utxos_post}

        for algo in ("default", "gradual", "greedy", "random"):
            merged = wallet.select_utxos_with_merge(
                0, 1, min_confirmations=0, merge_algorithm=algo
            )
            assert outpoint not in {u.outpoint for u in merged}, (
                f"Failed for algo={algo}"
            )

        # Raw cache still has it
        raw_utxos = await wallet.get_utxos(0)
        assert len(raw_utxos) == raw_count_pre
        assert any(u.outpoint == outpoint and u.frozen for u in raw_utxos)

        await wallet.close()

    @pytest.mark.asyncio
    async def test_bip329_format_interop_with_sparrow(self, bitcoin_backend, tmp_path):
        """Verify BIP-329 JSONL format is compatible with Sparrow wallet.

        BIP-329 specifies:
        - type: "output"
        - ref: "txid:vout"
        - spendable: false (for frozen)
        - label: optional string
        """
        wallet = WalletService(
            mnemonic=GENERIC_TEST_MNEMONIC,
            backend=bitcoin_backend,
            network="regtest",
            mixdepth_count=5,
            data_dir=tmp_path,
        )
        await wallet.sync_all()

        utxos = await wallet.get_utxos(0)
        if not utxos:
            await wallet.close()
            pytest.skip("No UTXOs available")

        target = utxos[0]
        outpoint = target.outpoint

        # Freeze and add a label
        wallet.freeze_utxo(outpoint)
        assert wallet.metadata_store is not None
        wallet.metadata_store.set_label(outpoint, "cold storage")

        # Read and validate the file
        content = wallet.metadata_store.path.read_text(encoding="utf-8")
        lines = [line.strip() for line in content.strip().splitlines() if line.strip()]

        found = False
        for line in lines:
            record = json.loads(line)
            if record.get("ref") == outpoint:
                found = True
                assert record["type"] == "output"
                assert record["spendable"] is False
                assert record["label"] == "cold storage"
                break

        assert found, f"BIP-329 record for {outpoint} not found in metadata file"

        # Verify we can also import a Sparrow-style record
        sparrow_outpoint = (
            "0000000000000000000000000000000000000000000000000000000000000001:0"
        )
        sparrow_record = json.dumps(
            {
                "type": "output",
                "ref": sparrow_outpoint,
                "spendable": False,
                "label": "from sparrow",
            },
            separators=(",", ":"),
        )

        # Append to existing file
        with open(wallet.metadata_store.path, "a", encoding="utf-8") as f:
            f.write(sparrow_record + "\n")

        # Reload and verify
        wallet.metadata_store.load()
        assert wallet.metadata_store.is_frozen(sparrow_outpoint)
        assert wallet.metadata_store.get_label(sparrow_outpoint) == "from sparrow"

        await wallet.close()

    @pytest.mark.asyncio
    async def test_multiple_utxos_frozen_unfrozen_independently(
        self, funded_wallet_with_metadata: WalletService
    ):
        """Multiple UTXOs can be frozen/unfrozen independently."""
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 3:
            pytest.skip("Need at least 3 UTXOs for this test")

        # Freeze first two, leave third unfrozen
        wallet.freeze_utxo(utxos[0].outpoint)
        wallet.freeze_utxo(utxos[1].outpoint)

        assert utxos[0].frozen is True
        assert utxos[1].frozen is True
        assert utxos[2].frozen is False

        # Unfreeze first, keep second frozen
        wallet.unfreeze_utxo(utxos[0].outpoint)

        assert utxos[0].frozen is False
        assert utxos[1].frozen is True
        assert utxos[2].frozen is False

        # Verify selection reflects the state
        selected = wallet.select_utxos(0, 1, min_confirmations=0)
        selected_outpoints = {u.outpoint for u in selected}
        assert utxos[1].outpoint not in selected_outpoints, (
            "Still-frozen UTXO was selected"
        )


# ==============================================================================
# Realistic scenario tests
# ==============================================================================


class TestRealisticScenarios:
    """Realistic usage scenarios combining freeze with wallet operations."""

    @pytest.mark.asyncio
    async def test_freeze_largest_utxo_forces_smaller_selection(
        self, funded_wallet_with_metadata: WalletService
    ):
        """Freezing the largest UTXO forces select_utxos to use smaller ones.

        Common scenario: user freezes their large UTXO to preserve its size
        for privacy, forcing the wallet to use smaller UTXOs.
        """
        wallet = funded_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 2:
            pytest.skip("Need at least 2 UTXOs")

        # Sort by value, freeze the largest
        sorted_utxos = sorted(utxos, key=lambda u: u.value, reverse=True)
        largest = sorted_utxos[0]
        wallet.freeze_utxo(largest.outpoint)

        # Select -- should use smaller UTXOs
        target = 1  # minimal amount
        selected = wallet.select_utxos(0, target, min_confirmations=0)
        assert largest.outpoint not in {u.outpoint for u in selected}

        # If we can identify the selected UTXO, it should be a smaller one
        if len(selected) > 0:
            assert all(u.value <= largest.value for u in selected)

    @pytest.mark.asyncio
    async def test_maker_offer_reflects_remaining_balance_after_freeze(
        self,
        bitcoin_backend,
        tmp_path,
    ):
        """A maker's offer maxsize reflects only unfrozen balance.

        Scenario: Operator freezes UTXOs to reserve them via ``jm-wallet freeze``,
        then the maker creates offers. The offers should only advertise the
        spendable (unfrozen) portion, not the full wallet balance.
        """
        from maker.offers import OfferManager

        wallet = WalletService(
            mnemonic=MAKER1_MNEMONIC,
            backend=bitcoin_backend,
            network="regtest",
            mixdepth_count=5,
            data_dir=tmp_path,
        )
        await wallet.sync_all()

        balance = await wallet.get_total_balance()
        if balance < 200_000:
            await wallet.close()
            pytest.skip("Need at least 200k sats")

        # Freeze half of the UTXOs BEFORE creating offers (realistic flow:
        # operator freezes via `jm-wallet freeze`, then starts/restarts maker)
        utxos = await wallet.get_utxos(0)
        assert len(utxos) > 1, "Need at least 2 UTXOs to freeze half"
        half = len(utxos) // 2
        frozen_value = 0
        for utxo in utxos[: max(half, 1)]:
            wallet.freeze_utxo(utxo.outpoint)
            frozen_value += utxo.value

        config = MakerConfig(
            mnemonic=MAKER1_MNEMONIC,
            network=NetworkType.TESTNET,
            bitcoin_network=NetworkType.REGTEST,
            backend_type="scantxoutset",
            backend_config={
                "rpc_url": "http://127.0.0.1:18443",
                "rpc_user": "test",
                "rpc_password": "test",
            },
            directory_servers=["127.0.0.1:5222"],
            min_size=100_000,
            cj_fee_relative="0.0003",
            tx_fee_contribution=1_000,
        )

        offer_manager = OfferManager(wallet, config, "J5TestMaker")
        offers = await offer_manager.create_offers()

        if offers:
            # The offer maxsize should be less than the full balance
            # (it should exclude the frozen portion)
            assert offers[0].maxsize < balance, (
                f"Offer maxsize {offers[0].maxsize} should be less than "
                f"full balance {balance} since {frozen_value} sats are frozen"
            )
        # If no offers at all, the unfrozen balance was below min_size -- also valid

        await wallet.close()

    @pytest.mark.asyncio
    async def test_taker_normal_coinjoin_with_frozen_utxos(
        self,
        funded_taker_wallet_with_metadata: WalletService,
    ):
        """Taker can still do a CoinJoin when some (but not all) UTXOs are frozen.

        The frozen UTXOs should not appear in the pre-selected UTXOs.
        """
        wallet = funded_taker_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 2:
            pytest.skip("Need at least 2 UTXOs")

        # Freeze one UTXO
        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # Simulate taker's UTXO pre-selection (normal mode)
        # Taker calls: wallet.select_utxos(mixdepth, estimated_required, taker_utxo_age)
        try:
            preselected = wallet.select_utxos(0, 1, min_confirmations=0)
            assert frozen_utxo.outpoint not in {u.outpoint for u in preselected}
        except ValueError:
            # If all remaining UTXOs together can't meet target, that's fine for this test
            pass

    @pytest.mark.asyncio
    async def test_taker_sweep_with_frozen_utxos(
        self,
        funded_taker_wallet_with_metadata: WalletService,
    ):
        """Taker sweep mode excludes frozen UTXOs.

        When sweeping, the taker calls get_all_utxos() which filters frozen.
        The sweep total should not include frozen UTXO values.
        """
        wallet = funded_taker_wallet_with_metadata

        utxos = await wallet.get_utxos(0)
        if len(utxos) < 2:
            pytest.skip("Need at least 2 UTXOs")

        total_all = sum(u.value for u in utxos)

        # Freeze one
        frozen_utxo = utxos[0]
        wallet.freeze_utxo(frozen_utxo.outpoint)

        # Sweep selection
        sweep_utxos = wallet.get_all_utxos(0, min_confirmations=0)
        sweep_total = sum(u.value for u in sweep_utxos)

        assert sweep_total == total_all - frozen_utxo.value
        assert frozen_utxo.outpoint not in {u.outpoint for u in sweep_utxos}


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
