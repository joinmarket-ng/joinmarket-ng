"""
Tests for UTXO selection algorithms in WalletService.

Tests cover:
- Basic select_utxos() - minimum UTXOs needed
- get_all_utxos() - all UTXOs from a mixdepth (for sweep)
- select_utxos_with_merge() - maker merge algorithms
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService


@pytest.fixture
def mock_backend():
    """Create a mock blockchain backend."""
    backend = MagicMock()
    backend.get_utxos = AsyncMock(return_value=[])
    backend.close = AsyncMock()
    return backend


@pytest.fixture
def wallet_service(test_mnemonic: str, mock_backend) -> WalletService:
    """Create a WalletService with mock backend and pre-populated UTXO cache."""
    ws = WalletService(
        mnemonic=test_mnemonic,
        backend=mock_backend,
        network="regtest",
        mixdepth_count=5,
        gap_limit=20,
    )

    # Pre-populate UTXO cache for testing
    ws.utxo_cache = {
        0: [
            UTXOInfo(
                txid="a" * 64,
                vout=0,
                value=100_000,
                address="bcrt1test1",
                confirmations=10,
                scriptpubkey="0014" + "aa" * 20,
                path="m/84'/0'/0'/0/0",
                mixdepth=0,
            ),
            UTXOInfo(
                txid="b" * 64,
                vout=0,
                value=50_000,
                address="bcrt1test2",
                confirmations=5,
                scriptpubkey="0014" + "bb" * 20,
                path="m/84'/0'/0'/0/1",
                mixdepth=0,
            ),
            UTXOInfo(
                txid="c" * 64,
                vout=0,
                value=30_000,
                address="bcrt1test3",
                confirmations=3,
                scriptpubkey="0014" + "cc" * 20,
                path="m/84'/0'/0'/0/2",
                mixdepth=0,
            ),
        ],
        1: [
            UTXOInfo(
                txid="f" * 64,
                vout=0,
                value=100_000,
                address="bcrt1test1_md1",
                confirmations=10,
                scriptpubkey="0014" + "ff" * 20,
                path="m/84'/0'/1'/0/0",
                mixdepth=1,
            ),
            UTXOInfo(
                txid="g" * 64,
                vout=0,
                value=50_000,
                address="bcrt1test2_md1",
                confirmations=5,
                scriptpubkey="0014" + "gg" * 20,
                path="m/84'/0'/1'/0/1",
                mixdepth=1,
            ),
            UTXOInfo(
                txid="h" * 64,
                vout=0,
                value=30_000,
                address="bcrt1test3_md1",
                confirmations=3,
                scriptpubkey="0014" + "hh" * 20,
                path="m/84'/0'/1'/0/2",
                mixdepth=1,
            ),
            UTXOInfo(
                txid="i" * 64,
                vout=0,
                value=20_000,
                address="bcrt1test4_md1",
                confirmations=2,
                scriptpubkey="0014" + "ii" * 20,
                path="m/84'/0'/1'/0/3",
                mixdepth=1,
            ),
            UTXOInfo(
                txid="j" * 64,
                vout=0,
                value=10_000,
                address="bcrt1test5_md1",
                confirmations=1,
                scriptpubkey="0014" + "jj" * 20,
                path="m/84'/0'/1'/0/4",
                mixdepth=1,
            ),
        ],
    }

    return ws


class TestSelectUtxos:
    """Tests for basic select_utxos() - minimum UTXOs needed."""

    def test_select_single_utxo_sufficient(self, wallet_service: WalletService):
        """When one UTXO is enough, select only one."""
        selected = wallet_service.select_utxos(0, 80_000, min_confirmations=1)
        assert len(selected) == 1
        assert selected[0].value == 100_000  # Largest UTXO

    def test_select_multiple_utxos_needed(self, wallet_service: WalletService):
        """When multiple UTXOs needed, select minimum count (non-md0)."""
        selected = wallet_service.select_utxos(1, 140_000, min_confirmations=1)
        assert len(selected) == 2
        assert sum(u.value for u in selected) >= 140_000

    def test_select_respects_confirmations(self, wallet_service: WalletService):
        """UTXOs below min confirmations are excluded."""
        selected = wallet_service.select_utxos(0, 80_000, min_confirmations=6)
        # Only 100k UTXO has 10 confirms
        assert len(selected) == 1
        assert all(u.confirmations >= 6 for u in selected)

    def test_select_insufficient_funds_raises(self, wallet_service: WalletService):
        """Raises ValueError when insufficient funds."""
        with pytest.raises(ValueError, match="Insufficient funds"):
            wallet_service.select_utxos(0, 500_000, min_confirmations=1)

    def test_select_insufficient_confirmed_funds_mentions_unconfirmed(
        self, wallet_service: WalletService
    ):
        """When confirmed balance is 0 but unconfirmed funds exist, error explains it."""
        # min_confirmations=100 means none of the UTXOs qualify (all have <100 confirms)
        with pytest.raises(ValueError, match="unconfirmed") as exc_info:
            wallet_service.select_utxos(0, 80_000, min_confirmations=100)
        assert "confirmed" in str(exc_info.value)
        assert "confirmation" in str(exc_info.value)

    def test_select_with_include_utxos(self, wallet_service: WalletService):
        """Mandatory UTXOs are always included (non-md0)."""
        mandatory = [wallet_service.utxo_cache[1][2]]  # 30k UTXO from md1
        selected = wallet_service.select_utxos(
            1, 50_000, min_confirmations=1, include_utxos=mandatory
        )

        # Mandatory UTXO should be included
        assert mandatory[0] in selected
        assert sum(u.value for u in selected) >= 50_000

    def test_select_utxos_md0_returns_single_utxo(self, wallet_service: WalletService):
        """select_utxos() for md0 only returns the single largest UTXO."""
        selected = wallet_service.select_utxos(0, 80_000, min_confirmations=1)
        assert len(selected) == 1
        assert selected[0].value == 100_000

    def test_select_utxos_md0_refuses_merge(self, wallet_service: WalletService):
        """select_utxos() for md0 raises if no single UTXO covers the target."""
        # md0 has 100k + 50k + 30k, but no single UTXO covers 140k
        with pytest.raises(ValueError, match="Cannot merge non-CJ md0 UTXOs for privacy reasons"):
            wallet_service.select_utxos(0, 140_000, min_confirmations=1)

    def test_select_utxos_md0_with_include_utxos(self, wallet_service: WalletService):
        """select_utxos() for md0 honours mandatory UTXOs but adds at most 1 more."""
        mandatory = [wallet_service.utxo_cache[0][2]]  # 30k UTXO
        selected = wallet_service.select_utxos(
            0, 50_000, min_confirmations=1, include_utxos=mandatory
        )
        # 30k mandatory + 100k largest eligible = 2 UTXOs
        assert len(selected) == 2
        assert mandatory[0] in selected
        assert sum(u.value for u in selected) >= 50_000

    def test_select_utxos_md0_include_utxos_sufficient_alone(self, wallet_service: WalletService):
        """select_utxos() for md0 returns only mandatory UTXO if it covers target."""
        mandatory = [wallet_service.utxo_cache[0][0]]  # 100k UTXO
        selected = wallet_service.select_utxos(
            0, 80_000, min_confirmations=1, include_utxos=mandatory
        )
        assert len(selected) == 1
        assert selected[0].value == 100_000

    def test_select_utxos_md0_empty_raises(self, wallet_service: WalletService):
        """select_utxos() for md0 raises when no eligible UTXOs exist."""
        wallet_service.utxo_cache[0] = []
        with pytest.raises(ValueError, match="no eligible UTXOs in mixdepth 0"):
            wallet_service.select_utxos(0, 10_000, min_confirmations=1)


class TestGetAllUtxos:
    """Tests for get_all_utxos() - used for sweep operations."""

    def test_get_all_returns_all_eligible(self, wallet_service: WalletService):
        """Returns all UTXOs meeting confirmation requirement."""
        all_utxos = wallet_service.get_all_utxos(0, min_confirmations=1)
        assert len(all_utxos) == 3  # All 3 UTXOs in md0

    def test_get_all_respects_confirmations(self, wallet_service: WalletService):
        """Filters by confirmation requirement."""
        all_utxos = wallet_service.get_all_utxos(0, min_confirmations=5)
        # Only UTXOs with 5+ confirms: 100k (10), 50k (5)
        assert len(all_utxos) == 2

    def test_get_all_empty_mixdepth(self, wallet_service: WalletService):
        """Empty mixdepth returns empty list."""
        all_utxos = wallet_service.get_all_utxos(2, min_confirmations=1)
        assert len(all_utxos) == 0


# ---------------------------------------------------------------------------
# Mixdepth 0 CoinJoin output exemption tests
# ---------------------------------------------------------------------------


@pytest.fixture
def wallet_with_cj_labels(test_mnemonic: str, mock_backend) -> WalletService:
    """WalletService with md0 UTXOs that have CJ labels (cj-out, cj-change, deposit)."""
    ws = WalletService(
        mnemonic=test_mnemonic,
        backend=mock_backend,
        network="regtest",
        mixdepth_count=5,
        gap_limit=20,
    )

    ws.utxo_cache = {
        0: [
            # CJ output - can be merged
            UTXOInfo(
                txid="a" * 64,
                vout=0,
                value=60_000,
                address="bcrt1cjout1",
                confirmations=10,
                scriptpubkey="0014" + "aa" * 20,
                path="m/84'/0'/0'/0/0",
                mixdepth=0,
                label="cj-out",
            ),
            # CJ output - can be merged
            UTXOInfo(
                txid="b" * 64,
                vout=0,
                value=40_000,
                address="bcrt1cjout2",
                confirmations=8,
                scriptpubkey="0014" + "bb" * 20,
                path="m/84'/0'/0'/0/1",
                mixdepth=0,
                label="cj-out",
            ),
            # CJ change - restricted (same as deposit)
            UTXOInfo(
                txid="c" * 64,
                vout=0,
                value=70_000,
                address="bcrt1cjchange",
                confirmations=5,
                scriptpubkey="0014" + "cc" * 20,
                path="m/84'/0'/0'/0/2",
                mixdepth=0,
                label="cj-change",
            ),
            # Deposit - restricted
            UTXOInfo(
                txid="d" * 64,
                vout=0,
                value=50_000,
                address="bcrt1deposit",
                confirmations=3,
                scriptpubkey="0014" + "dd" * 20,
                path="m/84'/0'/0'/0/3",
                mixdepth=0,
                label="deposit",
            ),
            # No label - restricted
            UTXOInfo(
                txid="e" * 64,
                vout=0,
                value=30_000,
                address="bcrt1nolabel",
                confirmations=2,
                scriptpubkey="0014" + "ee" * 20,
                path="m/84'/0'/0'/0/4",
                mixdepth=0,
            ),
        ],
        1: [
            UTXOInfo(
                txid="f" * 64,
                vout=0,
                value=100_000,
                address="bcrt1md1",
                confirmations=10,
                scriptpubkey="0014" + "ff" * 20,
                path="m/84'/0'/1'/0/0",
                mixdepth=1,
            ),
        ],
    }

    return ws


class TestMd0CjOutputExemption:
    """Tests that CJ outputs (label='cj-out') are exempt from md0 merge restriction."""

    def test_select_utxos_md0_cj_outputs_can_merge(self, wallet_with_cj_labels: WalletService):
        """select_utxos() for md0 can merge multiple cj-out UTXOs."""
        # CJ pool: 60k + 40k = 100k, target is 90k -- needs both CJ outs
        selected = wallet_with_cj_labels.select_utxos(0, 90_000, min_confirmations=1)
        assert len(selected) == 2
        assert all(u.label == "cj-out" for u in selected)
        assert sum(u.value for u in selected) == 100_000

    def test_select_utxos_md0_single_cj_out_sufficient(self, wallet_with_cj_labels: WalletService):
        """When one cj-out is enough, select only that one."""
        selected = wallet_with_cj_labels.select_utxos(0, 55_000, min_confirmations=1)
        assert len(selected) == 1
        assert selected[0].value == 60_000
        assert selected[0].label == "cj-out"

    def test_select_utxos_md0_falls_back_to_largest_non_cj(
        self, wallet_with_cj_labels: WalletService
    ):
        """When CJ pool < target but a single non-CJ covers it, use that."""
        # CJ pool = 100k < 65k target, but cj-change 70k covers it
        # Actually CJ pool 100k >= 65k, so it prefers CJ pool
        # Try a target where CJ pool can't cover but single non-CJ can
        # Remove one CJ output to make pool smaller
        wallet_with_cj_labels.utxo_cache[0] = [
            u for u in wallet_with_cj_labels.utxo_cache[0] if u.txid != "b" * 64
        ]
        # Now CJ pool = 60k, non-CJ: cj-change=70k, deposit=50k, nolabel=30k
        selected = wallet_with_cj_labels.select_utxos(0, 65_000, min_confirmations=1)
        assert len(selected) == 1
        assert selected[0].value == 70_000  # cj-change (largest non-CJ)

    def test_select_utxos_md0_cj_change_not_merged(self, wallet_with_cj_labels: WalletService):
        """cj-change is NOT exempt from md0 restriction."""
        # Remove CJ outputs so only non-CJ UTXOs remain
        wallet_with_cj_labels.utxo_cache[0] = [
            u for u in wallet_with_cj_labels.utxo_cache[0] if u.label != "cj-out"
        ]
        # Non-CJ: cj-change=70k, deposit=50k, nolabel=30k
        # Cannot merge, largest is 70k
        with pytest.raises(ValueError, match="Cannot merge non-CJ md0 UTXOs"):
            wallet_with_cj_labels.select_utxos(0, 100_000, min_confirmations=1)

    def test_select_utxos_md0_restrict_false_allows_merge(
        self, wallet_with_cj_labels: WalletService
    ):
        """restrict_md0=False allows merging all md0 UTXOs."""
        selected = wallet_with_cj_labels.select_utxos(
            0, 200_000, min_confirmations=1, restrict_md0=False
        )
        assert sum(u.value for u in selected) >= 200_000
        assert len(selected) > 1

    def test_select_utxos_with_merge_md0_cj_outputs_merge(
        self, wallet_with_cj_labels: WalletService
    ):
        """select_utxos_with_merge() md0 can merge cj-out UTXOs with merge algo."""
        selected = wallet_with_cj_labels.select_utxos_with_merge(
            0, 55_000, min_confirmations=1, merge_algorithm="greedy"
        )
        # Greedy on CJ pool: selects both cj-out UTXOs
        assert all(u.label == "cj-out" for u in selected)
        assert len(selected) == 2

    def test_select_utxos_with_merge_md0_gradual_cj(self, wallet_with_cj_labels: WalletService):
        """Gradual merge adds one extra CJ output."""
        selected = wallet_with_cj_labels.select_utxos_with_merge(
            0, 55_000, min_confirmations=1, merge_algorithm="gradual"
        )
        # Minimum = 1 (60k covers 55k), gradual = +1 = 2 CJ outs
        assert len(selected) == 2
        assert all(u.label == "cj-out" for u in selected)

    def test_select_utxos_with_merge_md0_non_cj_single_only(
        self, wallet_with_cj_labels: WalletService
    ):
        """Non-CJ UTXOs in md0 still restricted to single UTXO even with greedy merge."""
        # Remove CJ outputs
        wallet_with_cj_labels.utxo_cache[0] = [
            u for u in wallet_with_cj_labels.utxo_cache[0] if u.label != "cj-out"
        ]
        # Largest non-CJ is 70k, target is 50k
        selected = wallet_with_cj_labels.select_utxos_with_merge(
            0, 50_000, min_confirmations=1, merge_algorithm="greedy"
        )
        assert len(selected) == 1
        assert selected[0].value == 70_000

    def test_select_utxos_with_merge_md0_restrict_false(self, wallet_with_cj_labels: WalletService):
        """restrict_md0=False allows full merge on md0."""
        selected = wallet_with_cj_labels.select_utxos_with_merge(
            0, 50_000, min_confirmations=1, merge_algorithm="greedy", restrict_md0=False
        )
        # All 5 UTXOs in md0 should be selected (greedy)
        assert len(selected) == 5
        assert sum(u.value for u in selected) == 250_000


class TestGetBalanceForOffersCjExemption:
    """Tests for get_balance_for_offers() with CJ output exemption."""

    @pytest.mark.asyncio
    async def test_md0_cj_pool_larger_than_single(self, wallet_with_cj_labels: WalletService):
        """When CJ pool > largest non-CJ, effective balance is CJ pool."""
        # CJ pool: 60k + 40k = 100k, largest non-CJ: 70k (cj-change)
        balance = await wallet_with_cj_labels.get_balance_for_offers(0, min_confirmations=1)
        assert balance == 100_000  # max(100k, 70k) = 100k

    @pytest.mark.asyncio
    async def test_md0_single_non_cj_larger_than_pool(self, wallet_with_cj_labels: WalletService):
        """When largest non-CJ > CJ pool, effective balance is largest non-CJ."""
        # Make CJ pool smaller by removing one CJ output
        wallet_with_cj_labels.utxo_cache[0] = [
            u for u in wallet_with_cj_labels.utxo_cache[0] if u.txid != "a" * 64
        ]
        # CJ pool: 40k, largest non-CJ: 70k
        balance = await wallet_with_cj_labels.get_balance_for_offers(0, min_confirmations=1)
        assert balance == 70_000  # max(40k, 70k) = 70k

    @pytest.mark.asyncio
    async def test_md0_restrict_false_returns_total(self, wallet_with_cj_labels: WalletService):
        """restrict_md0=False returns total balance like non-md0."""
        balance = await wallet_with_cj_labels.get_balance_for_offers(
            0, min_confirmations=1, restrict_md0=False
        )
        # Total: 60k + 40k + 70k + 50k + 30k = 250k
        assert balance == 250_000

    @pytest.mark.asyncio
    async def test_md0_only_cj_outputs(self, wallet_with_cj_labels: WalletService):
        """When md0 has only cj-out UTXOs, pool sum is the balance."""
        wallet_with_cj_labels.utxo_cache[0] = [
            u for u in wallet_with_cj_labels.utxo_cache[0] if u.label == "cj-out"
        ]
        balance = await wallet_with_cj_labels.get_balance_for_offers(0, min_confirmations=1)
        assert balance == 100_000  # max(100k, 0) = 100k

    @pytest.mark.asyncio
    async def test_md0_no_cj_outputs(self, wallet_with_cj_labels: WalletService):
        """When md0 has no cj-out UTXOs, balance is largest single UTXO."""
        wallet_with_cj_labels.utxo_cache[0] = [
            u for u in wallet_with_cj_labels.utxo_cache[0] if u.label != "cj-out"
        ]
        balance = await wallet_with_cj_labels.get_balance_for_offers(0, min_confirmations=1)
        # Largest non-CJ: cj-change=70k
        assert balance == 70_000


class TestGetBalanceForOffersMd0:
    """Tests that get_balance_for_offers() respects the md0 single-UTXO constraint."""

    @pytest.mark.asyncio
    async def test_md0_returns_largest_single_utxo(self, wallet_service: WalletService):
        """For md0, effective balance is the largest single UTXO, not the sum."""
        balance = await wallet_service.get_balance_for_offers(0, min_confirmations=1)
        # md0 has 100k + 50k + 30k, but only the largest single UTXO counts
        assert balance == 100_000

    @pytest.mark.asyncio
    async def test_md1_returns_total_sum(self, wallet_service: WalletService):
        """For non-md0, effective balance is the sum of all eligible UTXOs."""
        balance = await wallet_service.get_balance_for_offers(1, min_confirmations=1)
        # md1 has 100k + 50k + 30k + 20k + 10k = 210k
        assert balance == 210_000

    @pytest.mark.asyncio
    async def test_md0_empty_returns_zero(self, wallet_service: WalletService):
        """For md0 with no eligible UTXOs, returns 0."""
        wallet_service.utxo_cache[0] = []
        balance = await wallet_service.get_balance_for_offers(0, min_confirmations=1)
        assert balance == 0

    @pytest.mark.asyncio
    async def test_md0_respects_confirmations(self, wallet_service: WalletService):
        """For md0, confirmation filter applies before selecting largest."""
        # Only 100k has 10 confirms, 50k has 5 confirms
        balance = await wallet_service.get_balance_for_offers(0, min_confirmations=6)
        assert balance == 100_000

        # With min_confirmations=11, no UTXO qualifies
        balance = await wallet_service.get_balance_for_offers(0, min_confirmations=11)
        assert balance == 0


class TestSelectUtxosWithMerge:
    """Tests for select_utxos_with_merge() - maker merge algorithms."""

    def test_default_algorithm_minimum_utxos(self, wallet_service: WalletService):
        """Default algorithm selects minimum UTXOs needed."""
        selected = wallet_service.select_utxos_with_merge(
            0, 80_000, min_confirmations=1, merge_algorithm="default"
        )
        # Should select just 1 UTXO (100k is enough)
        assert len(selected) == 1
        assert selected[0].value == 100_000

    def test_gradual_algorithm_adds_one(self, wallet_service: WalletService):
        """Gradual algorithm adds exactly one extra UTXO."""
        selected = wallet_service.select_utxos_with_merge(
            1, 80_000, min_confirmations=1, merge_algorithm="gradual"
        )
        # Should select 1 (minimum) + 1 (gradual) = 2 UTXOs
        assert len(selected) == 2
        # Extra should be smallest remaining (10k)
        values = sorted([u.value for u in selected])
        assert 10_000 in values  # Smallest was added

    def test_greedy_algorithm_selects_all(self, wallet_service: WalletService):
        """Greedy algorithm selects all eligible UTXOs."""
        selected = wallet_service.select_utxos_with_merge(
            1, 80_000, min_confirmations=1, merge_algorithm="greedy"
        )
        # Should select all 5 UTXOs
        assert len(selected) == 5
        assert sum(u.value for u in selected) == 210_000  # Total of all

    def test_random_algorithm_adds_zero_to_two(self, wallet_service: WalletService):
        """Random algorithm adds 0-2 extra UTXOs."""
        # Run multiple times to check range
        counts = set()
        for _ in range(50):
            selected = wallet_service.select_utxos_with_merge(
                1, 80_000, min_confirmations=1, merge_algorithm="random"
            )
            counts.add(len(selected))

        # Should see counts between 1 (min) and 3 (min + 2 random)
        assert min(counts) >= 1
        assert max(counts) <= 3

    def test_greedy_respects_confirmations(self, wallet_service: WalletService):
        """Greedy algorithm still respects confirmation requirement."""
        selected = wallet_service.select_utxos_with_merge(
            1, 50_000, min_confirmations=5, merge_algorithm="greedy"
        )
        # Only 2 UTXOs have 5+ confirms
        assert len(selected) == 2
        assert all(u.confirmations >= 5 for u in selected)

    def test_merge_insufficient_funds_raises(self, wallet_service: WalletService):
        """Raises ValueError when insufficient funds."""
        with pytest.raises(ValueError, match="Insufficient funds"):
            wallet_service.select_utxos_with_merge(
                0, 500_000, min_confirmations=1, merge_algorithm="greedy"
            )

    def test_gradual_no_remaining_acts_like_default(self, wallet_service: WalletService):
        """Gradual with no remaining UTXOs doesn't add any."""
        # Request almost all funds - needs all UTXOs
        selected = wallet_service.select_utxos_with_merge(
            1, 200_000, min_confirmations=1, merge_algorithm="gradual"
        )
        # All 5 UTXOs needed to meet 200k (total is 210k)
        assert len(selected) == 5

    def test_mixdepth_0_returns_single_large_utxo(self, wallet_service: WalletService):
        """mixdepth 0 should strictly return 1 UTXO without merging."""
        selected = wallet_service.select_utxos_with_merge(
            0, 80_000, min_confirmations=1, merge_algorithm="greedy"
        )
        assert len(selected) == 1
        assert selected[0].value == 100_000

    def test_mixdepth_0_insufficient_single_utxo_raises(self, wallet_service: WalletService):
        """mixdepth 0 should raise ValueError if no SINGLE UTXO is large enough."""
        with pytest.raises(ValueError, match="Cannot merge non-CJ md0 UTXOs for privacy reasons"):
            wallet_service.select_utxos_with_merge(
                0, 140_000, min_confirmations=1, merge_algorithm="greedy"
            )

    def test_mixdepth_1_merges_utxos(self, wallet_service: WalletService):
        """Other mixdepths should still merge UTXOs as requested."""
        wallet_service.utxo_cache[1] = [
            UTXOInfo(
                txid="f" * 64,
                vout=0,
                value=100_000,
                address="md1_1",
                confirmations=10,
                scriptpubkey="",
                path="",
                mixdepth=1,
            ),
            UTXOInfo(
                txid="g" * 64,
                vout=0,
                value=50_000,
                address="md1_2",
                confirmations=10,
                scriptpubkey="",
                path="",
                mixdepth=1,
            ),
        ]
        selected = wallet_service.select_utxos_with_merge(
            1, 140_000, min_confirmations=1, merge_algorithm="greedy"
        )
        assert len(selected) == 2
        assert sum(u.value for u in selected) == 150_000


class TestFindUTXOByAddress:
    """Tests for find_utxo_by_address method."""

    def test_find_utxo_by_address_found(self, wallet_service: WalletService):
        """Test finding a UTXO by address."""
        # Use an address from the pre-populated cache
        utxo = wallet_service.find_utxo_by_address("bcrt1test1")

        assert utxo is not None
        assert utxo.address == "bcrt1test1"
        assert utxo.txid == "a" * 64
        assert utxo.vout == 0
        assert utxo.value == 100_000

    def test_find_utxo_by_address_not_found(self, wallet_service: WalletService):
        """Test finding a UTXO by address that doesn't exist."""
        utxo = wallet_service.find_utxo_by_address("bc1qnonexistent123456")

        assert utxo is None

    def test_find_utxo_by_address_across_mixdepths(self, wallet_service: WalletService):
        """Test that find_utxo_by_address searches all mixdepths."""
        # Add a UTXO in mixdepth 2 to test cross-mixdepth search
        wallet_service.utxo_cache[2] = [
            UTXOInfo(
                txid="f" * 64,
                vout=0,
                value=200_000,
                address="bcrt1test_md2",
                confirmations=5,
                scriptpubkey="0014" + "ff" * 20,
                path="m/84'/0'/2'/0/0",
                mixdepth=2,
            )
        ]

        # Find address from mixdepth 2
        utxo = wallet_service.find_utxo_by_address("bcrt1test_md2")

        assert utxo is not None
        assert utxo.address == "bcrt1test_md2"
        assert utxo.value == 200_000
        assert utxo.mixdepth == 2

    def test_find_utxo_by_address_returns_first_match(self, wallet_service: WalletService):
        """Test that find_utxo_by_address returns the first match if duplicates exist."""
        # Add a duplicate address to a different mixdepth (edge case)
        duplicate_utxo = UTXOInfo(
            txid="z" * 64,
            vout=5,
            value=999_999,
            address="bcrt1test1",  # Same address as mixdepth 0
            confirmations=20,
            scriptpubkey="0014" + "zz" * 20,
            path="m/84'/0'/3'/0/0",
            mixdepth=3,
        )
        wallet_service.utxo_cache[3] = [duplicate_utxo]

        # Should find the first one (from mixdepth 0)
        utxo = wallet_service.find_utxo_by_address("bcrt1test1")

        assert utxo is not None
        assert utxo.address == "bcrt1test1"
        # Should be the first match from mixdepth 0
        assert utxo.value == 100_000
        assert utxo.txid == "a" * 64
        assert utxo.mixdepth == 0


@pytest.fixture
def wallet_with_timelocked(test_mnemonic: str, mock_backend) -> WalletService:
    """Create a WalletService with timelocked (fidelity bond) UTXOs."""
    ws = WalletService(
        mnemonic=test_mnemonic,
        backend=mock_backend,
        network="regtest",
        mixdepth_count=5,
        gap_limit=20,
    )

    # Pre-populate UTXO cache with a mix of regular and timelocked UTXOs
    ws.utxo_cache = {
        0: [
            # Regular UTXO (P2WPKH)
            UTXOInfo(
                txid="a" * 64,
                vout=0,
                value=100_000,
                address="bcrt1test1",
                confirmations=10,
                scriptpubkey="0014" + "aa" * 20,  # P2WPKH
                path="m/84'/0'/0'/0/0",
                mixdepth=0,
            ),
            # Timelocked fidelity bond UTXO (P2WSH with locktime)
            UTXOInfo(
                txid="b" * 64,
                vout=0,
                value=500_000,  # Large timelocked bond
                address="bcrt1timelocked",
                confirmations=100,
                scriptpubkey="0020" + "bb" * 32,  # P2WSH
                path="m/84'/0'/0'/2/0:1893456000",  # Branch 2 with locktime
                mixdepth=0,
                locktime=1893456000,  # Future locktime
            ),
            # Regular UTXO (P2WPKH)
            UTXOInfo(
                txid="c" * 64,
                vout=0,
                value=50_000,
                address="bcrt1test3",
                confirmations=5,
                scriptpubkey="0014" + "cc" * 20,  # P2WPKH
                path="m/84'/0'/0'/0/2",
                mixdepth=0,
            ),
        ],
        1: [
            # Regular UTXO in mixdepth 1
            UTXOInfo(
                txid="d" * 64,
                vout=0,
                value=200_000,
                address="bcrt1test4",
                confirmations=10,
                scriptpubkey="0014" + "dd" * 20,  # P2WPKH
                path="m/84'/0'/1'/0/0",
                mixdepth=1,
            ),
            UTXOInfo(
                txid="e" * 64,
                vout=0,
                value=500_000,
                address="bcrt1timelocked_md1",
                confirmations=100,
                scriptpubkey="0020" + "ff" * 32,
                path="m/84'/0'/1'/2/0:1893456000",
                mixdepth=1,
                locktime=1893456000,
            ),
            UTXOInfo(
                txid="f" * 64,
                vout=0,
                value=50_000,
                address="bcrt1test5",
                confirmations=5,
                scriptpubkey="0014" + "ee" * 20,
                path="m/84'/0'/1'/0/1",
                mixdepth=1,
            ),
        ],
    }

    return ws


class TestFidelityBondUTXOFiltering:
    """Tests for fidelity bond UTXO filtering in balance and selection."""

    @pytest.mark.asyncio
    async def test_get_balance_includes_fidelity_bonds_by_default(
        self, wallet_with_timelocked: WalletService
    ):
        """get_balance() includes fidelity bond UTXOs by default."""
        balance = await wallet_with_timelocked.get_balance(0)
        # Should include all: 100k + 500k + 50k = 650k
        assert balance == 650_000

    @pytest.mark.asyncio
    async def test_get_balance_exclude_fidelity_bonds(self, wallet_with_timelocked: WalletService):
        """get_balance(include_fidelity_bonds=False) excludes fidelity bond UTXOs."""
        balance = await wallet_with_timelocked.get_balance(0, include_fidelity_bonds=False)
        # Should exclude 500k fidelity bond: 100k + 50k = 150k
        assert balance == 150_000

    @pytest.mark.asyncio
    async def test_get_balance_for_offers_excludes_fidelity_bonds(
        self, wallet_with_timelocked: WalletService
    ):
        """get_balance_for_offers() excludes fidelity bond UTXOs.

        For md0 it also returns only the largest single UTXO value
        because merging md0 UTXOs is forbidden for privacy reasons.
        """
        balance = await wallet_with_timelocked.get_balance_for_offers(0)
        # md0 has 100k + 50k regular UTXOs, but only largest single UTXO counts
        assert balance == 100_000

    @pytest.mark.asyncio
    async def test_get_fidelity_bond_balance(self, wallet_with_timelocked: WalletService):
        """get_fidelity_bond_balance() returns only fidelity bond UTXOs."""
        balance = await wallet_with_timelocked.get_fidelity_bond_balance(0)
        # Should only include the 500k fidelity bond UTXO
        assert balance == 500_000

    @pytest.mark.asyncio
    async def test_get_fidelity_bond_balance_empty_mixdepth(
        self, wallet_with_timelocked: WalletService
    ):
        """get_fidelity_bond_balance() returns 0 for mixdepth without fidelity bonds."""
        balance = await wallet_with_timelocked.get_fidelity_bond_balance(2)
        # Mixdepth 2 has no fidelity bond UTXOs
        assert balance == 0

    @pytest.mark.asyncio
    async def test_get_total_balance_includes_fidelity_bonds_by_default(
        self, wallet_with_timelocked: WalletService
    ):
        """get_total_balance() includes fidelity bonds by default."""
        balance = await wallet_with_timelocked.get_total_balance()
        # MD0: 650k, MD1: 750k = 1.4m
        assert balance == 1_400_000

    @pytest.mark.asyncio
    async def test_get_total_balance_exclude_fidelity_bonds(
        self, wallet_with_timelocked: WalletService
    ):
        """get_total_balance(include_fidelity_bonds=False) excludes fidelity bonds."""
        balance = await wallet_with_timelocked.get_total_balance(include_fidelity_bonds=False)
        # MD0: 150k, MD1: 250k = 400k
        assert balance == 400_000

    def test_select_utxos_excludes_fidelity_bonds_by_default(
        self, wallet_with_timelocked: WalletService
    ):
        """select_utxos() excludes fidelity bond UTXOs by default."""
        selected = wallet_with_timelocked.select_utxos(0, 100_000, min_confirmations=1)
        # Should select 100k UTXO, not the 500k fidelity bond
        assert len(selected) == 1
        assert selected[0].value == 100_000
        assert not selected[0].is_fidelity_bond

    def test_select_utxos_cannot_reach_fidelity_bond_amount(
        self, wallet_with_timelocked: WalletService
    ):
        """select_utxos() fails when needing fidelity bond funds to reach target.

        In md0 this is a privacy-driven error because merging is forbidden.
        """
        # Request 200k - more than non-FB 150k, largest single UTXO is 100k
        with pytest.raises(ValueError, match="Cannot merge non-CJ md0 UTXOs for privacy reasons"):
            wallet_with_timelocked.select_utxos(0, 200_000, min_confirmations=1)

    def test_get_all_utxos_excludes_fidelity_bonds_by_default(
        self, wallet_with_timelocked: WalletService
    ):
        """get_all_utxos() excludes fidelity bond UTXOs by default."""
        all_utxos = wallet_with_timelocked.get_all_utxos(0, min_confirmations=1)
        # Should only include 2 regular UTXOs, not the fidelity bond
        assert len(all_utxos) == 2
        assert all(not u.is_fidelity_bond for u in all_utxos)

    def test_get_all_utxos_include_fidelity_bonds(self, wallet_with_timelocked: WalletService):
        """get_all_utxos(include_fidelity_bonds=True) includes fidelity bond UTXOs."""
        all_utxos = wallet_with_timelocked.get_all_utxos(
            0, min_confirmations=1, include_fidelity_bonds=True
        )
        # Should include all 3 UTXOs
        assert len(all_utxos) == 3
        assert any(u.is_fidelity_bond for u in all_utxos)

    def test_select_utxos_with_merge_excludes_fidelity_bonds_by_default(
        self, wallet_with_timelocked: WalletService
    ):
        """select_utxos_with_merge() excludes fidelity bond UTXOs by default."""
        # Use greedy to get all eligible UTXOs
        selected = wallet_with_timelocked.select_utxos_with_merge(
            1, 50_000, min_confirmations=1, merge_algorithm="greedy"
        )
        # Should only include 2 regular UTXOs in md1
        assert len(selected) == 2
        assert all(not u.is_fidelity_bond for u in selected)
        assert sum(u.value for u in selected) == 250_000

    def test_select_utxos_with_merge_include_fidelity_bonds(
        self, wallet_with_timelocked: WalletService
    ):
        """select_utxos_with_merge(include_fidelity_bonds=True) includes fidelity bonds."""
        selected = wallet_with_timelocked.select_utxos_with_merge(
            1, 50_000, min_confirmations=1, merge_algorithm="greedy", include_fidelity_bonds=True
        )
        # Should include all 3 UTXOs in md1
        assert len(selected) == 3
        assert any(u.is_fidelity_bond for u in selected)
        assert sum(u.value for u in selected) == 750_000


class TestSyncFidelityBondDeduplication:
    """Tests that sync_fidelity_bonds does not duplicate outpoints in cache."""

    @pytest.mark.asyncio
    async def test_repeat_sync_does_not_duplicate_cached_bond(
        self, wallet_with_timelocked: WalletService
    ):
        locktime = 1893456000
        address = wallet_with_timelocked.get_fidelity_bond_address(0, locktime)
        wallet_with_timelocked.backend.get_utxos = AsyncMock(
            return_value=[
                UTXOInfo(
                    txid="d" * 64,
                    vout=0,
                    value=500_000,
                    address=address,
                    confirmations=100,
                    scriptpubkey="0020" + "ee" * 32,
                    path="",
                    mixdepth=0,
                    locktime=locktime,
                )
            ]
        )

        await wallet_with_timelocked.sync_fidelity_bonds([locktime])
        await wallet_with_timelocked.sync_fidelity_bonds([locktime])

        outpoints = [
            (u.txid, u.vout)
            for u in wallet_with_timelocked.utxo_cache.get(0, [])
            if u.address == address
        ]
        assert outpoints.count(("d" * 64, 0)) == 1


class TestFreezeUnfreezeGuard:
    """Tests for fidelity-bond guard in bulk unfreeze path."""

    def test_unfreeze_non_fidelity_bonds_skips_fidelity_bonds(self) -> None:
        from jmwallet.cli.freeze import _unfreeze_non_fidelity_bonds

        wallet = MagicMock()
        utxos = [
            UTXOInfo(
                txid="e" * 64,
                vout=0,
                value=100_000,
                address="bc1qnormal",
                confirmations=10,
                scriptpubkey="0014" + "11" * 20,
                path="m/84'/0'/0'/0/0",
                mixdepth=0,
                frozen=True,
            ),
            UTXOInfo(
                txid="f" * 64,
                vout=1,
                value=500_000,
                address="bc1qbond",
                confirmations=100,
                scriptpubkey="0020" + "22" * 32,
                path="m/84'/0'/0'/2/0",
                mixdepth=0,
                locktime=0,
                frozen=True,
            ),
            UTXOInfo(
                txid="a" * 64,
                vout=2,
                value=50_000,
                address="bc1qunfrozen",
                confirmations=3,
                scriptpubkey="0014" + "33" * 20,
                path="m/84'/0'/0'/0/1",
                mixdepth=0,
                frozen=False,
            ),
        ]

        unfrozen_count, skipped_count = _unfreeze_non_fidelity_bonds(wallet, utxos)

        assert unfrozen_count == 1
        assert skipped_count == 1
        wallet.toggle_freeze_utxo.assert_called_once_with(utxos[0].outpoint)


# ---------------------------------------------------------------------------
# Frozen UTXO filtering tests
# ---------------------------------------------------------------------------


@pytest.fixture
def wallet_with_frozen(test_mnemonic: str, mock_backend) -> WalletService:
    """Create a WalletService with some frozen UTXOs."""
    ws = WalletService(
        mnemonic=test_mnemonic,
        backend=mock_backend,
        network="regtest",
        mixdepth_count=5,
        gap_limit=20,
    )

    ws.utxo_cache = {
        0: [
            UTXOInfo(
                txid="a" * 64,
                vout=0,
                value=100_000,
                address="bcrt1test1",
                confirmations=10,
                scriptpubkey="0014" + "aa" * 20,
                path="m/84'/0'/0'/0/0",
                mixdepth=0,
                frozen=True,  # Frozen
            ),
            UTXOInfo(
                txid="b" * 64,
                vout=0,
                value=50_000,
                address="bcrt1test2",
                confirmations=5,
                scriptpubkey="0014" + "bb" * 20,
                path="m/84'/0'/0'/0/1",
                mixdepth=0,
            ),
            UTXOInfo(
                txid="c" * 64,
                vout=0,
                value=30_000,
                address="bcrt1test3",
                confirmations=3,
                scriptpubkey="0014" + "cc" * 20,
                path="m/84'/0'/0'/0/2",
                mixdepth=0,
            ),
        ],
        1: [
            UTXOInfo(
                txid="d" * 64,
                vout=0,
                value=100_000,
                address="bcrt1test4",
                confirmations=10,
                scriptpubkey="0014" + "dd" * 20,
                path="m/84'/0'/1'/0/0",
                mixdepth=1,
                frozen=True,
            ),
            UTXOInfo(
                txid="e" * 64,
                vout=0,
                value=50_000,
                address="bcrt1test5",
                confirmations=5,
                scriptpubkey="0014" + "ee" * 20,
                path="m/84'/0'/1'/0/1",
                mixdepth=1,
            ),
            UTXOInfo(
                txid="f" * 64,
                vout=0,
                value=30_000,
                address="bcrt1test6",
                confirmations=3,
                scriptpubkey="0014" + "ff" * 20,
                path="m/84'/0'/1'/0/2",
                mixdepth=1,
            ),
        ],
    }

    return ws


class TestFrozenUTXOFiltering:
    """Tests that frozen UTXOs are excluded from coin selection and balances."""

    def test_select_utxos_excludes_frozen(self, wallet_with_frozen: WalletService):
        """select_utxos() skips frozen UTXOs."""
        selected = wallet_with_frozen.select_utxos(0, 40_000, min_confirmations=1)
        # Should select the 50k UTXO (not the frozen 100k)
        assert len(selected) == 1
        assert selected[0].value == 50_000
        assert not selected[0].frozen

    def test_select_utxos_insufficient_due_to_frozen(self, wallet_with_frozen: WalletService):
        """select_utxos() raises when frozen UTXOs would be needed.

        In md0 this is a privacy-driven error: no single UTXO covers the
        target and merging is forbidden.
        """
        # Spendable md0: 50k + 30k, but only single UTXO allowed.
        # Largest eligible (50k) < 90k -> raises.
        with pytest.raises(ValueError, match="Cannot merge non-CJ md0 UTXOs for privacy reasons"):
            wallet_with_frozen.select_utxos(0, 90_000, min_confirmations=1)

    def test_get_all_utxos_excludes_frozen(self, wallet_with_frozen: WalletService):
        """get_all_utxos() does not return frozen UTXOs."""
        all_utxos = wallet_with_frozen.get_all_utxos(0, min_confirmations=1)
        assert len(all_utxos) == 2
        assert all(not u.frozen for u in all_utxos)

    def test_select_utxos_with_merge_excludes_frozen(self, wallet_with_frozen: WalletService):
        """select_utxos_with_merge() greedy mode excludes frozen UTXOs."""
        selected = wallet_with_frozen.select_utxos_with_merge(
            1, 30_000, min_confirmations=1, merge_algorithm="greedy"
        )
        # Greedy selects all spendable in md1: 50k + 30k = 80k (not the frozen 100k)
        assert len(selected) == 2
        assert sum(u.value for u in selected) == 80_000
        assert all(not u.frozen for u in selected)

    @pytest.mark.asyncio
    async def test_get_balance_excludes_frozen(self, wallet_with_frozen: WalletService):
        """get_balance() excludes frozen UTXOs from the total."""
        balance = await wallet_with_frozen.get_balance(0)
        # Spendable: 50k + 30k = 80k (not 180k)
        assert balance == 80_000

    @pytest.mark.asyncio
    async def test_get_total_balance_excludes_frozen(self, wallet_with_frozen: WalletService):
        """get_total_balance() excludes frozen UTXOs across all mixdepths."""
        balance = await wallet_with_frozen.get_total_balance()
        # MD0: 80k (100k frozen out of 180k)
        # MD1: 80k (100k frozen out of 180k)
        # Total: 160k
        assert balance == 160_000


class TestFrozenUTXOWithFidelityBonds:
    """Tests for frozen UTXOs combined with fidelity bond filtering."""

    @pytest.fixture
    def wallet_frozen_and_fb(self, test_mnemonic: str, mock_backend) -> WalletService:
        """WalletService with both frozen and fidelity bond UTXOs."""
        ws = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network="regtest",
            mixdepth_count=5,
            gap_limit=20,
        )

        ws.utxo_cache = {
            0: [
                UTXOInfo(
                    txid="a" * 64,
                    vout=0,
                    value=100_000,
                    address="bcrt1test1",
                    confirmations=10,
                    scriptpubkey="0014" + "aa" * 20,
                    path="m/84'/0'/0'/0/0",
                    mixdepth=0,
                    frozen=True,  # Frozen regular UTXO
                ),
                UTXOInfo(
                    txid="b" * 64,
                    vout=0,
                    value=500_000,
                    address="bcrt1timelocked",
                    confirmations=100,
                    scriptpubkey="0020" + "bb" * 32,
                    path="m/84'/0'/0'/2/0:1893456000",
                    mixdepth=0,
                    locktime=1893456000,  # Fidelity bond
                ),
                UTXOInfo(
                    txid="c" * 64,
                    vout=0,
                    value=50_000,
                    address="bcrt1test3",
                    confirmations=5,
                    scriptpubkey="0014" + "cc" * 20,
                    path="m/84'/0'/0'/0/2",
                    mixdepth=0,
                ),
            ],
            1: [],
        }

        return ws

    @pytest.mark.asyncio
    async def test_balance_excludes_both_frozen_and_fb(self, wallet_frozen_and_fb: WalletService):
        """Balance excluding FBs also excludes frozen UTXOs."""
        balance = await wallet_frozen_and_fb.get_balance(0, include_fidelity_bonds=False)
        # Only the 50k unfrozen, non-FB UTXO
        assert balance == 50_000

    @pytest.mark.asyncio
    async def test_balance_includes_fb_but_excludes_frozen(
        self, wallet_frozen_and_fb: WalletService
    ):
        """Balance including FBs still excludes frozen UTXOs."""
        balance = await wallet_frozen_and_fb.get_balance(0, include_fidelity_bonds=True)
        # 500k FB + 50k unfrozen = 550k (not the 100k frozen)
        assert balance == 550_000

    def test_select_excludes_both(self, wallet_frozen_and_fb: WalletService):
        """select_utxos() excludes both frozen and FB UTXOs."""
        selected = wallet_frozen_and_fb.select_utxos(0, 40_000, min_confirmations=1)
        assert len(selected) == 1
        assert selected[0].value == 50_000
        assert not selected[0].frozen
        assert not selected[0].is_fidelity_bond


# ---------------------------------------------------------------------------
# Hot-reload of metadata store tests
# ---------------------------------------------------------------------------


class TestApplyFrozenStateHotReload:
    """Tests that _apply_frozen_state() re-reads metadata from disk."""

    def test_apply_picks_up_external_changes(self, test_mnemonic: str, mock_backend, tmp_path):
        """_apply_frozen_state() sees changes written by another process."""
        from jmwallet.wallet.utxo_metadata import UTXOMetadataStore

        metadata_path = tmp_path / "wallet_metadata.jsonl"

        # Create wallet with metadata store
        ws = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network="regtest",
            mixdepth_count=5,
            gap_limit=20,
        )
        ws.metadata_store = UTXOMetadataStore(path=metadata_path)
        ws.metadata_store.load()

        outpoint_a = "a" * 64 + ":0"

        # Pre-populate UTXO cache
        ws.utxo_cache = {
            0: [
                UTXOInfo(
                    txid="a" * 64,
                    vout=0,
                    value=100_000,
                    address="bcrt1test1",
                    confirmations=10,
                    scriptpubkey="0014" + "aa" * 20,
                    path="m/84'/0'/0'/0/0",
                    mixdepth=0,
                ),
            ],
        }

        # Simulate external process writing to the metadata file
        external_store = UTXOMetadataStore(path=metadata_path)
        external_store.freeze(outpoint_a)

        # Verify the UTXO is not yet frozen in our wallet's cache
        assert not ws.utxo_cache[0][0].frozen

        # Apply frozen state -- should re-read from disk
        ws._apply_frozen_state()

        # Now the UTXO should be frozen
        assert ws.utxo_cache[0][0].frozen

    def test_apply_clears_stale_frozen_state(self, test_mnemonic: str, mock_backend, tmp_path):
        """_apply_frozen_state() clears frozen state when metadata file changes."""
        from jmwallet.wallet.utxo_metadata import UTXOMetadataStore

        metadata_path = tmp_path / "wallet_metadata.jsonl"

        ws = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network="regtest",
            mixdepth_count=5,
            gap_limit=20,
        )
        ws.metadata_store = UTXOMetadataStore(path=metadata_path)

        outpoint_a = "a" * 64 + ":0"

        ws.utxo_cache = {
            0: [
                UTXOInfo(
                    txid="a" * 64,
                    vout=0,
                    value=100_000,
                    address="bcrt1test1",
                    confirmations=10,
                    scriptpubkey="0014" + "aa" * 20,
                    path="m/84'/0'/0'/0/0",
                    mixdepth=0,
                    frozen=True,  # Currently frozen in cache
                ),
            ],
        }

        # Freeze it in the metadata store first
        ws.metadata_store.freeze(outpoint_a)
        ws._apply_frozen_state()
        assert ws.utxo_cache[0][0].frozen

        # Now simulate external process unfreezing it
        external_store = UTXOMetadataStore(path=metadata_path)
        external_store.load()
        external_store.unfreeze(outpoint_a)

        # Apply again -- should re-read and clear the frozen state
        ws._apply_frozen_state()
        assert not ws.utxo_cache[0][0].frozen

    def test_apply_no_metadata_store_is_noop(self, test_mnemonic: str, mock_backend):
        """_apply_frozen_state() is a no-op when metadata_store is None."""
        ws = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network="regtest",
            mixdepth_count=5,
            gap_limit=20,
        )
        ws.metadata_store = None
        ws.utxo_cache = {
            0: [
                UTXOInfo(
                    txid="a" * 64,
                    vout=0,
                    value=100_000,
                    address="bcrt1test1",
                    confirmations=10,
                    scriptpubkey="0014" + "aa" * 20,
                    path="m/84'/0'/0'/0/0",
                    mixdepth=0,
                ),
            ],
        }

        # Should not raise
        ws._apply_frozen_state()
        assert not ws.utxo_cache[0][0].frozen
