"""
Tests for wallet address info functionality.

Tests the extended wallet info feature that shows detailed address
information including derivation paths, statuses, and xpubs.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

import pytest

from jmwallet.history import (
    TransactionHistoryEntry,
    append_history_entry,
    create_maker_history_entry,
    get_address_history_types,
    get_utxo_label,
)
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService


class TestAddressStatusDetermination:
    """Tests for address status determination logic."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        """Create a wallet for testing."""
        return WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )

    def test_determine_status_deposit(self, wallet):
        """Test deposit status for external address with funds."""
        status = wallet._determine_address_status(
            address="bc1q_external",
            balance=100000,
            is_external=True,
            used_addresses=set(),
            history_addresses={},
        )
        assert status == "deposit"

    def test_determine_status_cj_out(self, wallet):
        """Test cj-out status for CoinJoin output address with funds."""
        status = wallet._determine_address_status(
            address="bc1q_internal",
            balance=50000,
            is_external=False,
            used_addresses={"bc1q_internal"},
            history_addresses={"bc1q_internal": "cj_out"},
        )
        assert status == "cj-out"

    def test_determine_status_non_cj_change(self, wallet):
        """Test non-cj-change status for change address not from CJ."""
        status = wallet._determine_address_status(
            address="bc1q_change",
            balance=30000,
            is_external=False,
            used_addresses={},
            history_addresses={},
        )
        assert status == "non-cj-change"

    def test_determine_status_cj_change(self, wallet):
        """Change output that came from a CoinJoin transaction must be
        labeled 'cj-change' (not 'non-cj-change'): it is deanonymising
        and should be displayed distinctly so the user can avoid merging
        it with other coins."""
        status = wallet._determine_address_status(
            address="bc1q_cj_change",
            balance=50000,
            is_external=False,
            used_addresses={"bc1q_cj_change"},
            history_addresses={"bc1q_cj_change": "change"},
        )
        assert status == "cj-change"

    def test_determine_status_new(self, wallet):
        """Test new status for unused address."""
        status = wallet._determine_address_status(
            address="bc1q_new",
            balance=0,
            is_external=True,
            used_addresses=set(),
            history_addresses={},
        )
        assert status == "new"

    def test_determine_status_used_empty(self, wallet):
        """Test used-empty status for address that had funds."""
        status = wallet._determine_address_status(
            address="bc1q_spent",
            balance=0,
            is_external=True,
            used_addresses={"bc1q_spent"},
            history_addresses={"bc1q_spent": "cj_out"},
        )
        assert status == "used-empty"

    def test_determine_status_flagged(self, wallet):
        """Test flagged status for address shared but tx failed."""
        status = wallet._determine_address_status(
            address="bc1q_flagged",
            balance=0,
            is_external=True,
            used_addresses={"bc1q_flagged"},
            history_addresses={"bc1q_flagged": "flagged"},
        )
        assert status == "flagged"

    def test_determine_status_pending_cj_out_external(self, wallet):
        """A CoinJoin destination address with funds but whose history
        entry is still 'flagged' (broadcast but not yet confirmed: the
        monitor flips success=True only after first confirmation) must
        be labeled cj-out, not deposit. Regression test for the bug
        where pending CJ outputs briefly show as deposits.
        """
        status = wallet._determine_address_status(
            address="bc1q_pending_cj",
            balance=100000,
            is_external=True,
            used_addresses={"bc1q_pending_cj"},
            history_addresses={"bc1q_pending_cj": "flagged"},
        )
        assert status == "cj-out"

    def test_determine_status_pending_cj_change_internal(self, wallet):
        """A CoinJoin change address with funds but still pending (history
        entry success=False → flagged) must be labeled cj-change, not
        non-cj-change.
        """
        status = wallet._determine_address_status(
            address="bc1q_pending_change",
            balance=40000,
            is_external=False,
            used_addresses={"bc1q_pending_change"},
            history_addresses={"bc1q_pending_change": "flagged"},
        )
        assert status == "cj-change"

    def test_determine_status_reused_multiple_utxos(self, wallet):
        """An address holding more than one UTXO has been paid to more than
        once and must be flagged 'reused' (legacy wallet parity), taking
        precedence over deposit/cj-out."""
        from jmwallet.wallet.models import UTXOInfo

        def _utxo(vout: int, label: str | None = None) -> UTXOInfo:
            return UTXOInfo(
                txid="a" * 64,
                vout=vout,
                value=100000,
                address="bc1q_reused",
                confirmations=3,
                scriptpubkey="0014" + "00" * 20,
                path="m/84'/1'/0'/0/0",
                mixdepth=0,
                label=label,
            )

        # Two UTXOs on a deposit address -> reused (overrides "deposit").
        status = wallet._determine_address_status(
            address="bc1q_reused",
            balance=200000,
            is_external=True,
            used_addresses=set(),
            history_addresses={},
            utxos=[_utxo(0), _utxo(1)],
        )
        assert status == "reused"

        # Two UTXOs on a cj-out address -> reused (overrides "cj-out").
        status = wallet._determine_address_status(
            address="bc1q_reused",
            balance=200000,
            is_external=False,
            used_addresses={"bc1q_reused"},
            history_addresses={"bc1q_reused": "cj_out"},
            utxos=[_utxo(0), _utxo(1)],
        )
        assert status == "reused"

    def test_determine_status_reused_autofrozen_single_utxo(self, wallet):
        """A single UTXO carrying the forced-address-reuse auto-freeze label
        means funds landed on an already-used-then-emptied address; surface it
        as 'reused' even though only one UTXO remains."""
        from jmwallet.wallet.models import UTXOInfo
        from jmwallet.wallet.utxo_metadata import AUTO_FREEZE_REUSE_LABEL

        utxo = UTXOInfo(
            txid="a" * 64,
            vout=0,
            value=50000,
            address="bc1q_refunded",
            confirmations=1,
            scriptpubkey="0014" + "00" * 20,
            path="m/84'/1'/0'/0/0",
            mixdepth=0,
            label=AUTO_FREEZE_REUSE_LABEL,
        )
        status = wallet._determine_address_status(
            address="bc1q_refunded",
            balance=50000,
            is_external=True,
            used_addresses={"bc1q_refunded"},
            history_addresses={},
            utxos=[utxo],
        )
        assert status == "reused"

    def test_determine_status_single_utxo_not_reused(self, wallet):
        """A single, normally-labeled UTXO must keep its usual status."""
        from jmwallet.wallet.models import UTXOInfo

        utxo = UTXOInfo(
            txid="a" * 64,
            vout=0,
            value=100000,
            address="bc1q_external",
            confirmations=3,
            scriptpubkey="0014" + "00" * 20,
            path="m/84'/1'/0'/0/0",
            mixdepth=0,
            label="deposit",
        )
        status = wallet._determine_address_status(
            address="bc1q_external",
            balance=100000,
            is_external=True,
            used_addresses=set(),
            history_addresses={},
            utxos=[utxo],
        )
        assert status == "deposit"

    def test_internal_transfer_not_labeled_as_coinjoin(self, wallet):
        """Regression for issue #517.

        An internal wallet transfer (plain ``jm-wallet send`` between
        mixdepths, recorded as ``role="send"``) must not be surfaced as a
        CoinJoin. Its destination should be ``deposit`` and its change should
        be ``non-cj-change``, end-to-end through ``get_address_history_types``.
        """
        from jmwallet.history import (
            append_history_entry,
            create_send_history_entry,
        )

        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            dest = "bc1qdestmd1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            change = "bc1qchangemd0xxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
            append_history_entry(
                create_send_history_entry(
                    destination=dest,
                    change_address=change,
                    amount=20_000,
                    mining_fee=200,
                    source_mixdepth=0,
                    selected_utxos=[("aa" * 32, 0)],
                    txid="bb" * 32,
                    success=True,
                ),
                data_dir,
            )

            history_addresses = get_address_history_types(data_dir)

            dest_status = wallet._determine_address_status(
                address=dest,
                balance=20_000,
                is_external=True,
                used_addresses={dest},
                history_addresses=history_addresses,
            )
            change_status = wallet._determine_address_status(
                address=change,
                balance=5_000,
                is_external=False,
                used_addresses={change},
                history_addresses=history_addresses,
            )

            assert dest_status == "deposit"
            assert change_status == "non-cj-change"

    def test_wallet_service_does_not_retain_mnemonic_or_passphrase(
        self, mock_backend, test_mnemonic, test_network
    ):
        """WalletService should not keep mnemonic/passphrase as instance attributes."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            passphrase="secret-passphrase",
            mixdepth_count=5,
        )

        assert "mnemonic" not in vars(wallet)
        assert "passphrase" not in vars(wallet)


class TestGetNextAddressIndex:
    """Tests for get_next_address_index method."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        """Create a wallet for testing."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}
        return wallet

    def test_returns_zero_when_no_addresses_used(self, wallet):
        """Test that index 0 is returned when no addresses are used."""
        index = wallet.get_next_address_index(mixdepth=0, change=0)
        assert index == 0

    def test_returns_next_after_utxo(self, wallet):
        """Test that next index after UTXO address is returned."""
        addr_2 = wallet.get_receive_address(0, 2)
        utxo = UTXOInfo(
            txid="0" * 64,
            vout=0,
            value=100000,
            address=addr_2,
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path=f"{wallet.root_path}/0'/0/2",
            mixdepth=0,
        )
        wallet.utxo_cache[0] = [utxo]

        index = wallet.get_next_address_index(mixdepth=0, change=0)
        assert index == 3

    def test_uses_addresses_with_history_after_spend(self, wallet):
        """
        Test that addresses_with_history is used to prevent reuse after spend.

        This is the key bug scenario: an address receives funds (index 0),
        then funds are spent (internal send). After the spend, UTXO cache
        no longer has the address, but addresses_with_history should track it
        to prevent reuse.
        """
        # Simulate: address at index 0 received funds, then was spent
        addr_0 = wallet.get_receive_address(0, 0)
        wallet.addresses_with_history.add(addr_0)
        # No UTXOs remain (all spent)
        wallet.utxo_cache[0] = []

        index = wallet.get_next_address_index(mixdepth=0, change=0)
        # Should return 1, not 0, because addr_0 was used
        assert index == 1

    def test_uses_highest_index_from_addresses_with_history(self, wallet):
        """Test that the highest index from addresses_with_history is used."""
        # Addresses 0, 2, and 5 had history (1, 3, 4 were skipped for some reason)
        wallet.get_receive_address(0, 0)  # Cache address
        wallet.get_receive_address(0, 2)  # Cache address
        addr_5 = wallet.get_receive_address(0, 5)  # Cache address

        wallet.addresses_with_history.add(wallet.get_receive_address(0, 0))
        wallet.addresses_with_history.add(wallet.get_receive_address(0, 2))
        wallet.addresses_with_history.add(addr_5)

        index = wallet.get_next_address_index(mixdepth=0, change=0)
        # Should return 6, the next after the highest used (5)
        assert index == 6

    def test_combines_utxo_cache_and_addresses_with_history(self, wallet):
        """Test that both UTXO cache and addresses_with_history are considered."""
        # Address at index 3 is in UTXO cache (current balance)
        addr_3 = wallet.get_receive_address(0, 3)
        utxo = UTXOInfo(
            txid="0" * 64,
            vout=0,
            value=100000,
            address=addr_3,
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path=f"{wallet.root_path}/0'/0/3",
            mixdepth=0,
        )
        wallet.utxo_cache[0] = [utxo]

        # Address at index 7 was spent (in history but no UTXO)
        addr_7 = wallet.get_receive_address(0, 7)
        wallet.addresses_with_history.add(addr_7)

        index = wallet.get_next_address_index(mixdepth=0, change=0)
        # Should return 8, the next after the highest (7 from history)
        assert index == 8

    def test_respects_mixdepth_separation(self, wallet):
        """Test that different mixdepths have independent indices."""
        # Mixdepth 0 has used address at index 5
        addr_m0 = wallet.get_receive_address(0, 5)
        wallet.addresses_with_history.add(addr_m0)

        # Mixdepth 1 should still return 0
        index_m1 = wallet.get_next_address_index(mixdepth=1, change=0)
        assert index_m1 == 0

        # Mixdepth 0 should return 6
        index_m0 = wallet.get_next_address_index(mixdepth=0, change=0)
        assert index_m0 == 6

    def test_respects_change_separation(self, wallet):
        """Test that external and internal addresses have independent indices."""
        # External (change=0) has used address at index 3
        addr_ext = wallet.get_receive_address(0, 3)
        wallet.addresses_with_history.add(addr_ext)

        # Internal (change=1) should still return 0
        index_int = wallet.get_next_address_index(mixdepth=0, change=1)
        assert index_int == 0

        # External should return 4
        index_ext = wallet.get_next_address_index(mixdepth=0, change=0)
        assert index_ext == 4

    def test_get_address_uses_cached_path(self, wallet):
        """Repeated path lookups should use cached address without re-deriving."""
        addr = wallet.get_address(0, 0, 0)

        original_derive = wallet.master_key.derive

        def fail_derive(path: str):
            raise AssertionError(f"derive called unexpectedly for path {path}")

        wallet.master_key.derive = fail_derive
        try:
            assert wallet.get_address(0, 0, 0) == addr
        finally:
            wallet.master_key.derive = original_derive


class TestNextUnusedUnflaggedAddress:
    """Tests for get_next_unused_unflagged_address method."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        """Create a wallet for testing."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}
        return wallet

    def test_get_next_address_no_history(self, wallet):
        """Test getting next address with no history."""
        address, index = wallet.get_next_unused_unflagged_address(0, set())
        assert index == 0
        assert address  # Should return valid address

    def test_get_next_address_starts_after_blockchain_history(self, wallet):
        """Test that next address starts after the highest used on blockchain."""
        # Addresses 0 and 1 had blockchain history (received funds, now spent)
        addr_0 = wallet.get_receive_address(0, 0)
        addr_1 = wallet.get_receive_address(0, 1)
        addr_2 = wallet.get_receive_address(0, 2)
        wallet.addresses_with_history.add(addr_0)
        wallet.addresses_with_history.add(addr_1)

        # Even with empty used_addresses (CoinJoin history), should start at index 2
        address, index = wallet.get_next_unused_unflagged_address(0, set())
        assert index == 2
        assert address == addr_2

    def test_get_next_address_skips_flagged_after_history(self, wallet):
        """Test that flagged addresses are skipped after the blockchain history index."""
        # Address 0 had blockchain history
        addr_0 = wallet.get_receive_address(0, 0)
        wallet.addresses_with_history.add(addr_0)

        # Address 1 was flagged in a CoinJoin (shared but tx failed)
        addr_1 = wallet.get_receive_address(0, 1)
        addr_2 = wallet.get_receive_address(0, 2)
        used = {addr_1}

        # Should return index 2 (skipping flagged index 1)
        address, index = wallet.get_next_unused_unflagged_address(0, used)
        assert index == 2
        assert address == addr_2

    def test_get_next_address_different_mixdepths(self, wallet):
        """Test getting next address from different mixdepths."""
        # Mixdepth 0 has used address at index 0
        addr_m0_0 = wallet.get_receive_address(0, 0)
        wallet.addresses_with_history.add(addr_m0_0)

        # Mixdepth 1 has no history
        addr_m1_0 = wallet.get_receive_address(1, 0)

        # Mixdepth 0 should be at index 1 (next after used index 0)
        addr, idx = wallet.get_next_unused_unflagged_address(0, set())
        assert idx == 1

        # Mixdepth 1 should still be at index 0
        addr, idx = wallet.get_next_unused_unflagged_address(1, set())
        assert idx == 0
        assert addr == addr_m1_0

    def test_get_next_address_with_utxos(self, wallet):
        """Test that addresses with current UTXOs affect the starting index."""
        # Address at index 2 has a UTXO
        addr_2 = wallet.get_receive_address(0, 2)
        addr_3 = wallet.get_receive_address(0, 3)
        utxo = UTXOInfo(
            txid="0" * 64,
            vout=0,
            value=100000,
            address=addr_2,
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path=f"{wallet.root_path}/0'/0/2",
            mixdepth=0,
        )
        wallet.utxo_cache[0] = [utxo]

        # Should return index 3 (next after the UTXO at index 2)
        address, index = wallet.get_next_unused_unflagged_address(0, set())
        assert index == 3
        assert address == addr_3


class TestGetNextAfterLastUsedAddress:
    """Tests for get_next_after_last_used_address method."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        """Create a wallet for testing."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}
        return wallet

    def test_no_history_returns_index_0(self, wallet):
        """Test getting next address when no addresses have been used."""
        # With no history, should return index 0 (next after -1)
        address, index = wallet.get_next_after_last_used_address(0, set())
        assert index == 0
        addr_0 = wallet.get_receive_address(0, 0)
        assert address == addr_0

    def test_with_blockchain_history(self, wallet):
        """Test getting next address after blockchain history."""
        # Mark address at index 0 and 2 as used via blockchain history
        addr_0 = wallet.get_receive_address(0, 0)
        addr_2 = wallet.get_receive_address(0, 2)
        addr_3 = wallet.get_receive_address(0, 3)
        wallet.addresses_with_history.add(addr_0)
        wallet.addresses_with_history.add(addr_2)

        # Should return index 3 (next after highest used index 2)
        address, index = wallet.get_next_after_last_used_address(0, set())
        assert index == 3
        assert address == addr_3

    def test_with_utxos(self, wallet):
        """Test that addresses with current UTXOs affect the next index."""
        # Address at index 3 has a UTXO
        addr_3 = wallet.get_receive_address(0, 3)
        addr_4 = wallet.get_receive_address(0, 4)
        utxo = UTXOInfo(
            txid="0" * 64,
            vout=0,
            value=100000,
            address=addr_3,
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path=f"{wallet.root_path}/0'/0/3",
            mixdepth=0,
        )
        wallet.utxo_cache[0] = [utxo]

        # Should return index 4 (next after the UTXO at index 3)
        address, index = wallet.get_next_after_last_used_address(0, set())
        assert index == 4
        assert address == addr_4

    def test_different_mixdepths(self, wallet):
        """Test getting next address from different mixdepths."""
        # Mixdepth 0 has used address at index 2
        addr_m0_2 = wallet.get_receive_address(0, 2)
        addr_m0_3 = wallet.get_receive_address(0, 3)
        wallet.addresses_with_history.add(addr_m0_2)

        # Mixdepth 1 has no history
        addr_m1_0 = wallet.get_receive_address(1, 0)

        # Mixdepth 0 should return index 3 (next after highest used 2)
        addr, idx = wallet.get_next_after_last_used_address(0, set())
        assert idx == 3
        assert addr == addr_m0_3

        # Mixdepth 1 should return index 0 (next after -1, no history)
        addr, idx = wallet.get_next_after_last_used_address(1, set())
        assert idx == 0
        assert addr == addr_m1_0

    def test_with_coinjoin_history(self, wallet):
        """Test that CoinJoin history is considered for next address."""
        # Mark addresses at index 1 and 4 as used in CoinJoin history
        addr_1 = wallet.get_receive_address(0, 1)
        addr_4 = wallet.get_receive_address(0, 4)
        addr_5 = wallet.get_receive_address(0, 5)
        used_addresses = {addr_1, addr_4}

        # Should return index 5 (next after highest used index 4)
        address, index = wallet.get_next_after_last_used_address(0, used_addresses)
        assert index == 5
        assert address == addr_5

    def test_ignores_gaps(self, wallet):
        """Test that gaps in address usage are ignored."""
        # Mark addresses at index 0, 2, and 5 as used (gaps at 1, 3, 4)
        addr_0 = wallet.get_receive_address(0, 0)
        addr_2 = wallet.get_receive_address(0, 2)
        addr_5 = wallet.get_receive_address(0, 5)
        addr_6 = wallet.get_receive_address(0, 6)
        wallet.addresses_with_history.add(addr_0)
        wallet.addresses_with_history.add(addr_2)
        wallet.addresses_with_history.add(addr_5)

        # Should return index 6 (next after highest used 5, ignoring gaps)
        address, index = wallet.get_next_after_last_used_address(0, set())
        assert index == 6
        assert address == addr_6

    def test_issued_receive_addresses_advance_index(self, wallet):
        """Addresses already issued to callers (API/CLI) must not be reissued.

        Regression: the jmwalletd /address/new/{mixdepth} endpoint uses this
        picker (via get_next_safe_deposit_address); issued-but-unfunded
        addresses must advance the index.
        """
        addr_0 = wallet.get_receive_address(0, 0)
        addr_1 = wallet.get_receive_address(0, 1)
        wallet.issued_receive_addresses.add(addr_0)

        address, index = wallet.get_next_after_last_used_address(0, set())
        assert index == 1
        assert address == addr_1

    def test_reserved_addresses_advance_index(self, wallet):
        """Addresses reserved for in-progress CoinJoin sessions are skipped."""
        addr_0 = wallet.get_receive_address(0, 0)
        addr_1 = wallet.get_receive_address(0, 1)
        wallet.reserve_addresses({addr_0})

        address, index = wallet.get_next_after_last_used_address(0, set())
        assert index == 1
        assert address == addr_1


class TestAddressHistoryTypes:
    """Tests for get_address_history_types function."""

    def test_empty_history(self):
        """Test with no history."""
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            result = get_address_history_types(data_dir)
            assert result == {}

    def test_successful_coinjoin_addresses(self):
        """Test addresses from successful CoinJoin."""
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            entry = TransactionHistoryEntry(
                timestamp="2024-01-01T00:00:00",
                role="maker",
                success=True,
                txid="abc123",
                cj_amount=100000,
                destination_address="bc1q_cj_out",
                change_address="bc1q_change",
            )
            append_history_entry(entry, data_dir)

            result = get_address_history_types(data_dir)
            assert result["bc1q_cj_out"] == "cj_out"
            assert result["bc1q_change"] == "change"

    def test_failed_coinjoin_addresses_flagged(self):
        """Test addresses from failed CoinJoin are flagged."""
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            entry = TransactionHistoryEntry(
                timestamp="2024-01-01T00:00:00",
                role="taker",
                success=False,
                failure_reason="Timed out",
                txid="",
                cj_amount=100000,
                destination_address="bc1q_failed_dest",
                change_address="bc1q_failed_change",
            )
            append_history_entry(entry, data_dir)

            result = get_address_history_types(data_dir)
            assert result["bc1q_failed_dest"] == "flagged"
            assert result["bc1q_failed_change"] == "flagged"

    def test_mixed_history(self):
        """Test with both successful and failed entries."""
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            # Successful CoinJoin
            entry1 = TransactionHistoryEntry(
                timestamp="2024-01-01T00:00:00",
                role="maker",
                success=True,
                txid="abc123",
                cj_amount=100000,
                destination_address="bc1q_success",
                change_address="bc1q_success_change",
            )
            append_history_entry(entry1, data_dir)

            # Failed CoinJoin
            entry2 = TransactionHistoryEntry(
                timestamp="2024-01-02T00:00:00",
                role="taker",
                success=False,
                failure_reason="Error",
                txid="",
                cj_amount=50000,
                destination_address="bc1q_failed",
                change_address="",
            )
            append_history_entry(entry2, data_dir)

            result = get_address_history_types(data_dir)
            assert result["bc1q_success"] == "cj_out"
            assert result["bc1q_success_change"] == "change"
            assert result["bc1q_failed"] == "flagged"


class TestUTXOLabels:
    """Tests for get_utxo_label function."""

    def test_deposit_label_for_unknown_address(self):
        """Test that unknown addresses get 'deposit' label."""
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            # No history, so all addresses should be deposits
            label = get_utxo_label("bc1q_unknown", data_dir)
            assert label == "deposit"

    def test_cj_out_label(self):
        """Test that CoinJoin output addresses get 'cj-out' label."""
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            entry = TransactionHistoryEntry(
                timestamp="2024-01-01T00:00:00",
                role="maker",
                success=True,
                txid="abc123",
                cj_amount=100000,
                destination_address="bc1q_cj_out",
                change_address="",
            )
            append_history_entry(entry, data_dir)

            label = get_utxo_label("bc1q_cj_out", data_dir)
            assert label == "cj-out"

    def test_cj_change_label(self):
        """Test that CoinJoin change addresses get 'cj-change' label."""
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            entry = TransactionHistoryEntry(
                timestamp="2024-01-01T00:00:00",
                role="taker",
                success=True,
                txid="abc123",
                cj_amount=100000,
                destination_address="bc1q_cj_out",
                change_address="bc1q_change",
            )
            append_history_entry(entry, data_dir)

            label = get_utxo_label("bc1q_change", data_dir)
            assert label == "cj-change"

    def test_flagged_label(self):
        """Test that failed CoinJoin addresses get 'flagged' label."""
        with TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            entry = TransactionHistoryEntry(
                timestamp="2024-01-01T00:00:00",
                role="taker",
                success=False,
                failure_reason="Timed out",
                txid="",
                cj_amount=100000,
                destination_address="bc1q_failed",
                change_address="bc1q_failed_change",
            )
            append_history_entry(entry, data_dir)

            assert get_utxo_label("bc1q_failed", data_dir) == "flagged"
            assert get_utxo_label("bc1q_failed_change", data_dir) == "flagged"


class TestAddressInfoForMixdepth:
    """Tests for get_address_info_for_mixdepth method."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        """Create a wallet for testing."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )
        # Initialize empty UTXO cache
        wallet.utxo_cache = {i: [] for i in range(5)}
        return wallet

    def test_empty_mixdepth(self, wallet):
        """Test getting addresses for empty mixdepth."""
        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=0,
            gap_limit=3,
            used_addresses=set(),
            history_addresses={},
        )
        # Should return gap_limit addresses (no used addresses)
        assert len(addresses) == 3
        for addr_info in addresses:
            assert addr_info.status == "new"
            assert addr_info.balance == 0
            assert addr_info.is_external is True

    def test_mixdepth_with_utxos(self, wallet):
        """Test getting addresses when there are UTXOs."""
        # Add a UTXO at index 5
        addr_5 = wallet.get_receive_address(0, 5)
        utxo = UTXOInfo(
            txid="0" * 64,
            vout=0,
            value=100000,
            address=addr_5,
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path=f"{wallet.root_path}/0'/0/5",
            mixdepth=0,
        )
        wallet.utxo_cache[0] = [utxo]

        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=0,
            gap_limit=3,
            used_addresses=set(),
            history_addresses={},
        )
        # Should return addresses 0 through 5 + gap_limit = 0-8
        assert len(addresses) == 9  # 0-5 (funded at 5) + 3 gap = 9

        # Address at index 5 should have balance
        addr_5_info = addresses[5]
        assert addr_5_info.balance == 100000
        assert addr_5_info.status == "deposit"

        # Earlier addresses should be "new"
        assert addresses[0].status == "new"
        assert addresses[0].balance == 0

    def test_multiply_funded_address_is_reused(self, wallet):
        """End-to-end (through get_address_info_for_mixdepth): an address with
        two UTXOs is surfaced as 'reused', with its combined balance."""
        addr_2 = wallet.get_receive_address(0, 2)
        wallet.utxo_cache[0] = [
            UTXOInfo(
                txid=c * 64,
                vout=0,
                value=100000,
                address=addr_2,
                confirmations=6,
                scriptpubkey="0014" + "00" * 20,
                path=f"{wallet.root_path}/0'/0/2",
                mixdepth=0,
            )
            for c in ("0", "1")
        ]

        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=0,
            gap_limit=2,
            used_addresses=set(),
            history_addresses={},
        )
        addr_2_info = addresses[2]
        assert addr_2_info.balance == 200000
        assert len(addr_2_info.utxos) == 2
        assert addr_2_info.status == "reused"
        # The underlying classification is preserved (issue #564): the CLI
        # renders it as "deposit (reused)" instead of losing the UTXO type.
        assert addr_2_info.base_status == "deposit"

    def test_reused_address_keeps_cj_out_base_status(self, wallet):
        """A reused CoinJoin output address keeps its cj-out classification in
        base_status while status is 'reused' (issue #564)."""
        addr_3 = wallet.get_receive_address(0, 3)
        wallet.utxo_cache[0] = [
            UTXOInfo(
                txid=c * 64,
                vout=0,
                value=100000,
                address=addr_3,
                confirmations=6,
                scriptpubkey="0014" + "00" * 20,
                path=f"{wallet.root_path}/0'/0/3",
                mixdepth=0,
            )
            for c in ("2", "3")
        ]

        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=0,
            gap_limit=2,
            used_addresses={addr_3},
            history_addresses={addr_3: "cj_out"},
        )
        addr_3_info = addresses[3]
        assert addr_3_info.status == "reused"
        assert addr_3_info.base_status == "cj-out"

    def test_non_reused_address_has_no_base_status(self, wallet):
        """base_status stays None for addresses that are not reused."""
        addr_5 = wallet.get_receive_address(0, 5)
        utxo = UTXOInfo(
            txid="0" * 64,
            vout=0,
            value=100000,
            address=addr_5,
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path=f"{wallet.root_path}/0'/0/5",
            mixdepth=0,
        )
        wallet.utxo_cache[0] = [utxo]

        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=0,
            gap_limit=3,
            used_addresses=set(),
            history_addresses={},
        )
        assert addresses[5].status == "deposit"
        assert addresses[5].base_status is None
        assert addresses[0].status == "new"
        assert addresses[0].base_status is None

    def test_internal_addresses(self, wallet):
        """Test getting internal (change) addresses."""
        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=1,
            gap_limit=2,
            used_addresses=set(),
            history_addresses={},
        )
        for addr_info in addresses:
            assert addr_info.is_external is False
            assert "/1/" in addr_info.path  # Internal branch

    def test_addresses_with_history(self, wallet):
        """Test address status reflects history."""
        # Get address and mark it as CJ output
        addr = wallet.get_change_address(0, 0)

        # Add UTXO
        utxo = UTXOInfo(
            txid="0" * 64,
            vout=0,
            value=50000,
            address=addr,
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path=f"{wallet.root_path}/0'/1/0",
            mixdepth=0,
        )
        wallet.utxo_cache[0] = [utxo]

        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=1,
            gap_limit=2,
            used_addresses={addr},
            history_addresses={addr: "cj_out"},
        )

        # First address should be cj-out with balance
        assert addresses[0].status == "cj-out"
        assert addresses[0].balance == 50000

    def test_spent_address_shows_used_empty_not_new(self, wallet):
        """Test that a spent address (now empty) shows 'used-empty', not 'new'.

        Regression test for bug: After spending from an address, the address that
        previously had funds and was labeled "non-cj-change" would show as "new"
        instead of "used-empty" because `addresses_with_history` was not being
        checked when calculating max_used_index.
        """
        # Simulate an address at index 5 that HAD funds but is now empty
        # (spent in a non-CoinJoin transaction)
        addr_5 = wallet.get_change_address(0, 5)

        # Mark the address as having blockchain history (simulating it was used)
        # This is what happens during wallet sync when an address had UTXOs
        wallet.addresses_with_history.add(addr_5)

        # No UTXOs (the address is now empty after spending)
        wallet.utxo_cache[0] = []

        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=1,  # Internal/change addresses
            gap_limit=3,
            used_addresses=set(),  # No CoinJoin history
            history_addresses={},  # No CoinJoin history
        )

        # Should return addresses 0 through 5 + gap_limit = 0-8
        # Even though there's no balance, the address at index 5 has history
        assert len(addresses) >= 9  # 0-5 (history at 5) + 3 gap = 9

        # Address at index 5 should be "used-empty", NOT "new"
        addr_5_info = addresses[5]
        assert addr_5_info.balance == 0
        assert addr_5_info.status == "used-empty"

        # Addresses 6-8 (gap) should be "new"
        for i in [6, 7, 8]:
            assert addresses[i].status == "new"


class TestAccountXpub:
    """Tests for xpub generation."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        backend = Mock()
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        """Create a wallet for testing."""
        return WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )

    def test_get_account_xpub_mainnet(self, mock_backend, test_mnemonic):
        """Test xpub generation for mainnet."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network="mainnet",
            mixdepth_count=5,
        )
        xpub = wallet.get_account_xpub(0)
        assert xpub.startswith("xpub")

    def test_get_account_xpub_testnet(self, mock_backend, test_mnemonic):
        """Test xpub generation for testnet."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network="testnet",
            mixdepth_count=5,
        )
        xpub = wallet.get_account_xpub(0)
        assert xpub.startswith("tpub")

    def test_different_mixdepths_different_xpubs(self, wallet):
        """Test that different mixdepths produce different xpubs."""
        xpub_0 = wallet.get_account_xpub(0)
        xpub_1 = wallet.get_account_xpub(1)
        xpub_2 = wallet.get_account_xpub(2)

        assert xpub_0 != xpub_1
        assert xpub_1 != xpub_2
        assert xpub_0 != xpub_2


class TestAccountZpub:
    """Tests for zpub generation."""

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        backend = Mock()
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        """Create a wallet for testing."""
        return WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )

    def test_get_account_zpub_mainnet(self, mock_backend, test_mnemonic):
        """Test zpub generation for mainnet."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network="mainnet",
            mixdepth_count=5,
        )
        zpub = wallet.get_account_zpub(0)
        assert zpub.startswith("zpub")

    def test_get_account_zpub_testnet(self, mock_backend, test_mnemonic):
        """Test zpub generation for testnet."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network="testnet",
            mixdepth_count=5,
        )
        zpub = wallet.get_account_zpub(0)
        assert zpub.startswith("vpub")

    def test_different_mixdepths_different_zpubs(self, wallet):
        """Test that different mixdepths produce different zpubs."""
        zpub_0 = wallet.get_account_zpub(0)
        zpub_1 = wallet.get_account_zpub(1)
        zpub_2 = wallet.get_account_zpub(2)

        assert zpub_0 != zpub_1
        assert zpub_1 != zpub_2
        assert zpub_0 != zpub_2

    def test_zpub_xpub_different_same_key(self, wallet):
        """Test that zpub and xpub are different for the same account."""
        zpub = wallet.get_account_zpub(0)
        xpub = wallet.get_account_xpub(0)

        assert zpub != xpub
        assert zpub.startswith("zpub") or zpub.startswith("vpub")
        assert xpub.startswith("xpub") or xpub.startswith("tpub")


class TestAddressReservation:
    """Tests for address reservation during CoinJoin sessions.

    Address reservation prevents reuse of addresses that have been shared with
    takers but where the CoinJoin hasn't completed yet (concurrent sessions).
    """

    @pytest.fixture
    def mock_backend(self):
        """Create a mock backend."""
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        """Create a wallet for testing."""
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}
        return wallet

    def test_reserve_addresses_adds_to_set(self, wallet):
        """Test that reserve_addresses adds addresses to reserved_addresses."""
        addr1 = wallet.get_change_address(0, 0)
        addr2 = wallet.get_change_address(1, 0)

        wallet.reserve_addresses({addr1, addr2})

        assert addr1 in wallet.reserved_addresses
        assert addr2 in wallet.reserved_addresses

    def test_reserved_addresses_skipped_by_get_next_address_index(self, wallet):
        """Test that reserved addresses cause get_next_address_index to skip past them."""
        # Reserve address at index 0 for change in mixdepth 0
        addr_0 = wallet.get_change_address(0, 0)
        wallet.reserve_addresses({addr_0})

        # Next address should be index 1
        index = wallet.get_next_address_index(mixdepth=0, change=1)
        assert index == 1

    def test_multiple_reserved_addresses_skipped(self, wallet):
        """Test that multiple reserved addresses are all skipped."""
        # Reserve addresses at indices 0, 1, 2
        addrs = {
            wallet.get_change_address(0, 0),
            wallet.get_change_address(0, 1),
            wallet.get_change_address(0, 2),
        }
        wallet.reserve_addresses(addrs)

        # Next address should be index 3
        index = wallet.get_next_address_index(mixdepth=0, change=1)
        assert index == 3

    def test_reserved_addresses_respect_mixdepth(self, wallet):
        """Test that reserved addresses only affect their own mixdepth."""
        # Reserve address at index 5 in mixdepth 0
        addr_m0 = wallet.get_change_address(0, 5)
        wallet.reserve_addresses({addr_m0})

        # Mixdepth 0 change should be 6
        index_m0 = wallet.get_next_address_index(mixdepth=0, change=1)
        assert index_m0 == 6

        # Mixdepth 1 change should still be 0
        index_m1 = wallet.get_next_address_index(mixdepth=1, change=1)
        assert index_m1 == 0

    def test_reserved_addresses_combined_with_history(self, wallet):
        """Test that reserved addresses work alongside addresses_with_history."""
        # Address 0 had blockchain history
        addr_0 = wallet.get_change_address(0, 0)
        wallet.addresses_with_history.add(addr_0)

        # Address 1 is reserved (shared in current session)
        addr_1 = wallet.get_change_address(0, 1)
        wallet.reserve_addresses({addr_1})

        # Next should be index 2
        index = wallet.get_next_address_index(mixdepth=0, change=1)
        assert index == 2

    def test_reserved_addresses_combined_with_utxos(self, wallet):
        """Test reserved addresses work with UTXOs."""
        # UTXO at index 3
        addr_3 = wallet.get_change_address(0, 3)
        utxo = UTXOInfo(
            txid="0" * 64,
            vout=0,
            value=100000,
            address=addr_3,
            confirmations=6,
            scriptpubkey="0014" + "00" * 20,
            path=f"{wallet.root_path}/0'/1/3",
            mixdepth=0,
        )
        wallet.utxo_cache[0] = [utxo]

        # Reserved at index 5
        addr_5 = wallet.get_change_address(0, 5)
        wallet.reserve_addresses({addr_5})

        # Next should be 6 (past reserved)
        index = wallet.get_next_address_index(mixdepth=0, change=1)
        assert index == 6

    def test_concurrent_sessions_get_different_addresses(self, wallet):
        """Test that concurrent CoinJoin sessions get different addresses.

        This is the key bug scenario: two concurrent !fill requests should
        result in different CJ output addresses, not the same one.
        """
        # First session gets addresses
        cj_addr_1 = wallet.get_change_address(1, wallet.get_next_address_index(1, 1))
        change_addr_1 = wallet.get_change_address(0, wallet.get_next_address_index(0, 1))

        # Reserve them (this happens when !ioauth is sent)
        wallet.reserve_addresses({cj_addr_1, change_addr_1})

        # Second session should get different addresses
        cj_addr_2 = wallet.get_change_address(1, wallet.get_next_address_index(1, 1))
        change_addr_2 = wallet.get_change_address(0, wallet.get_next_address_index(0, 1))

        # They should be different
        assert cj_addr_1 != cj_addr_2
        assert change_addr_1 != change_addr_2

    def test_external_addresses_can_be_reserved(self, wallet):
        """Test that external (receive) addresses can also be reserved."""
        # Reserve external address at index 0
        addr_0 = wallet.get_receive_address(0, 0)
        wallet.reserve_addresses({addr_0})

        # Next external should be 1
        index = wallet.get_next_address_index(mixdepth=0, change=0)
        assert index == 1

    def test_reserved_addresses_pruned_when_persisted_in_history(self, wallet):
        """Reserved addresses should be trimmed after durable history tracks them."""
        with TemporaryDirectory() as tmpdir:
            wallet.data_dir = Path(tmpdir)

            cj_addr = wallet.get_change_address(1, 0)
            change_addr = wallet.get_change_address(0, 0)
            wallet.reserve_addresses({cj_addr, change_addr})
            assert len(wallet.reserved_addresses) == 2

            entry = create_maker_history_entry(
                taker_nick="J5taker",
                cj_amount=100000,
                fee_received=10,
                txfee_contribution=5,
                cj_address=cj_addr,
                change_address=change_addr,
                our_utxos=[("a" * 64, 0)],
                txid="b" * 64,
                network="regtest",
                wallet_fingerprint=wallet.wallet_fingerprint,
            )
            append_history_entry(entry, wallet.data_dir)

            # Trigger pruning through address index calculation path
            _ = wallet.get_next_address_index(mixdepth=0, change=1)
            assert len(wallet.reserved_addresses) == 0


class TestIssuedReceiveAddresses:
    """Tests for issued receive-address tracking."""

    @pytest.fixture
    def mock_backend(self):
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()
        return backend

    @pytest.fixture
    def wallet(self, mock_backend, test_mnemonic, test_network):
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend,
            network=test_network,
            mixdepth_count=5,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}
        return wallet

    def test_get_new_address_returns_unique_addresses(self, wallet):
        """Repeated get_new_address() calls must not return the same address."""
        first = wallet.get_new_address(0)
        second = wallet.get_new_address(0)
        third = wallet.get_new_address(0)

        assert first != second
        assert second != third
        assert first != third

    def test_issued_receive_addresses_advance_next_index(self, wallet):
        """Issued receive addresses should be considered used for index selection."""
        first = wallet.get_new_address(0)
        assert first in wallet.issued_receive_addresses

        next_index = wallet.get_next_address_index(mixdepth=0, change=0)
        assert next_index == 1


class TestSafeDepositAddress:
    """Tests for the privacy-critical async deposit-address picker.

    These guard the Layer 4b defense-in-depth: even if the bulk
    address-history sync is incomplete (RPC truncation, node crash,
    stale persisted state), the per-candidate
    ``address_has_history`` check must catch previously-funded
    addresses before they are proposed as fresh deposits.
    """

    @pytest.fixture
    def mock_backend_with_verifier(self, used_set: set[str]):
        """Backend that returns True from ``address_has_history`` for a
        configurable set of addresses (the persisted "used" set)."""
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()

        async def address_has_history(addr: str) -> bool:
            return addr in used_set

        backend.address_has_history = address_has_history
        return backend

    @pytest.fixture
    def used_set(self):
        return set()

    @pytest.fixture
    def wallet(self, mock_backend_with_verifier, test_mnemonic, test_network):
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=mock_backend_with_verifier,
            network=test_network,
            mixdepth_count=5,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}
        return wallet

    @pytest.mark.asyncio
    async def test_returns_first_candidate_when_clean(self, wallet):
        """No on-chain history -> first candidate is accepted."""
        addr, idx = await wallet.get_next_safe_deposit_address(0)
        assert idx == 0
        assert addr == wallet.get_receive_address(0, 0)

    @pytest.mark.asyncio
    async def test_get_new_address_verified_returns_unique_addresses(self, wallet):
        """Repeated get_new_address_verified() calls must not return the same
        address, even when none of the issued addresses is funded yet.

        Regression: jmwalletd GET /address/new/{mixdepth} switched from
        get_new_address() to get_new_address_verified(), whose picker did not
        consult issued_receive_addresses, so every call returned index 0.
        """
        first = await wallet.get_new_address_verified(0)
        second = await wallet.get_new_address_verified(0)
        third = await wallet.get_new_address_verified(0)

        assert len({first, second, third}) == 3
        assert first == wallet.get_receive_address(0, 0)
        assert second == wallet.get_receive_address(0, 1)
        assert third == wallet.get_receive_address(0, 2)

    @pytest.mark.asyncio
    async def test_skips_addresses_with_onchain_history(self, wallet, used_set: set[str]):
        """Backend says index 0 has history -> picker must advance to index 1
        and persist the catch so subsequent picks don't repropose it."""
        used_set.add(wallet.get_receive_address(0, 0))

        addr, idx = await wallet.get_next_safe_deposit_address(0)

        assert idx == 1, "must advance past the previously-funded address"
        assert addr == wallet.get_receive_address(0, 1)
        # The caught address must now be in the in-memory used set so
        # future picks (this run) skip it without re-asking the backend.
        assert wallet.get_receive_address(0, 0) in wallet.addresses_with_history

    @pytest.mark.asyncio
    async def test_skips_multiple_used_addresses(self, wallet, used_set: set[str]):
        """Two consecutive used addresses must both be skipped."""
        used_set.add(wallet.get_receive_address(0, 0))
        used_set.add(wallet.get_receive_address(0, 1))

        addr, idx = await wallet.get_next_safe_deposit_address(0)

        assert idx == 2
        assert addr == wallet.get_receive_address(0, 2)

    @pytest.mark.asyncio
    async def test_falls_back_when_verifier_returns_none(self, test_mnemonic, test_network):
        """Backend RPC failure (None) -> use sync-layer pick rather than
        block indefinitely. Preserves availability when bitcoind is
        unreachable; defense in depth degrades to the persisted store."""
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()

        async def verifier(addr: str) -> bool | None:
            return None  # simulate RPC failure

        backend.address_has_history = verifier

        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network=test_network,
            mixdepth_count=5,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}

        addr, idx = await wallet.get_next_safe_deposit_address(0)
        # Synchronous picker chooses index 0; we don't downgrade.
        assert idx == 0
        assert addr == wallet.get_receive_address(0, 0)

    @pytest.mark.asyncio
    async def test_works_without_verifier_method(self, test_mnemonic, test_network):
        """Backends without ``address_has_history`` (e.g. neutrino without
        this method) fall back to the sync picker; no regression."""
        backend = Mock(spec=["get_utxos", "close"])
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()

        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network=test_network,
            mixdepth_count=5,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}

        addr, idx = await wallet.get_next_safe_deposit_address(0)
        assert idx == 0
        assert addr == wallet.get_receive_address(0, 0)

    @pytest.mark.asyncio
    async def test_max_attempts_safety_limit(self, wallet, used_set: set[str]):
        """If the backend reports every candidate as used (misbehaving or
        a misconfigured wallet), the picker must raise rather than loop
        forever. This guards against an infinite descriptor-range walk."""

        async def always_used(addr: str) -> bool:
            return True

        wallet.backend.address_has_history = always_used

        with pytest.raises(RuntimeError, match="could not find an unused"):
            await wallet.get_next_safe_deposit_address(0, max_attempts=5)


class TestReservedAddressPersistence:
    """Reserved/issued deposit addresses persist across wallet restarts."""

    @pytest.fixture
    def mock_backend(self):
        backend = Mock()
        backend.get_utxos = AsyncMock(return_value=[])
        backend.close = AsyncMock()

        async def address_has_history(addr: str) -> bool:
            return False

        backend.address_has_history = address_has_history
        return backend

    def _make_wallet(self, backend, data_dir, mnemonic, network):
        wallet = WalletService(
            mnemonic=mnemonic,
            backend=backend,
            network=network,
            mixdepth_count=5,
            scan_range=20,
            data_dir=data_dir,
        )
        wallet.utxo_cache = {i: [] for i in range(5)}
        return wallet

    @pytest.mark.asyncio
    async def test_issued_address_not_reissued_after_restart(
        self, mock_backend, temp_data_dir, test_mnemonic, test_network
    ):
        """Regression: /address/new must advance the index across a restart.

        Previously issued addresses were tracked only in memory, so a fresh
        process handed out the same address again until it was funded.
        """
        wallet = self._make_wallet(mock_backend, temp_data_dir, test_mnemonic, test_network)
        first = await wallet.get_new_address_verified(0)
        assert first == wallet.get_receive_address(0, 0)

        # Simulate a restart: brand-new WalletService over the same data_dir.
        wallet2 = self._make_wallet(mock_backend, temp_data_dir, test_mnemonic, test_network)
        assert first in wallet2.get_reserved_addresses()
        second = await wallet2.get_new_address_verified(0)
        assert second != first
        assert second == wallet2.get_receive_address(0, 1)

    @pytest.mark.asyncio
    async def test_reserve_and_release_roundtrip(
        self, mock_backend, temp_data_dir, test_mnemonic, test_network
    ):
        wallet = self._make_wallet(mock_backend, temp_data_dir, test_mnemonic, test_network)
        addr = wallet.get_receive_address(0, 3)
        wallet.reserve_address(addr, "Alice")
        assert wallet.is_address_reserved(addr)
        assert wallet.get_reserved_addresses()[addr] == "Alice"

        # Label survives a restart.
        wallet2 = self._make_wallet(mock_backend, temp_data_dir, test_mnemonic, test_network)
        assert wallet2.get_reserved_addresses().get(addr) == "Alice"
        # And a reserved address is skipped by the deposit picker.
        picked, index = wallet2.get_next_after_last_used_address(0, set())
        assert index == 4

        # Releasing clears it durably.
        assert wallet2.unreserve_address(addr) is True
        wallet3 = self._make_wallet(mock_backend, temp_data_dir, test_mnemonic, test_network)
        assert addr not in wallet3.get_reserved_addresses()

    def test_reserved_empty_address_status_and_label(
        self, mock_backend, temp_data_dir, test_mnemonic, test_network
    ):
        """A reserved, unfunded address shows as 'reserved' with its label."""
        wallet = self._make_wallet(mock_backend, temp_data_dir, test_mnemonic, test_network)
        addr = wallet.get_receive_address(0, 0)
        wallet.reserve_address(addr, "Bob")

        infos = wallet.get_address_info_for_mixdepth(0, 0, gap_limit=6, used_addresses=set())
        info = next(i for i in infos if i.address == addr)
        assert info.status == "reserved"
        assert info.label == "Bob"
