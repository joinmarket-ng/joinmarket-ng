"""
Tests for the verify_bonds() method on blockchain backends.

Tests cover the base class default implementation and the Bitcoin Core
batched implementation. The neutrino and mempool implementations are
tested via integration/e2e tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jmwallet.backends.base import (
    UTXO,
    BlockchainBackend,
    BondVerificationRequest,
    BondVerificationResult,
)

# ---- Fixtures ----


def _make_bond_request(
    txid: str = "a" * 64,
    vout: int = 0,
    locktime: int = 1956528000,
) -> BondVerificationRequest:
    """Create a test bond verification request."""
    return BondVerificationRequest(
        txid=txid,
        vout=vout,
        utxo_pub=b"\x02" + b"\xab" * 32,
        locktime=locktime,
        address="bcrt1qfakeaddress",
        scriptpubkey="0020" + "cd" * 32,
    )


# ---- Base class default verify_bonds() ----


class TestBaseVerifyBonds:
    """Test the default verify_bonds() implementation in BlockchainBackend."""

    @pytest.fixture()
    def mock_backend(self) -> BlockchainBackend:
        """Create a mock backend with required abstract methods."""
        backend = MagicMock(spec=BlockchainBackend)
        # Make verify_bonds use the real default implementation
        backend.verify_bonds = BlockchainBackend.verify_bonds.__get__(backend)
        backend.get_block_height = AsyncMock(return_value=100000)
        backend.get_block_time = AsyncMock(return_value=1700000000)
        return backend

    @pytest.mark.asyncio()
    async def test_empty_bonds(self, mock_backend: BlockchainBackend) -> None:
        """verify_bonds with empty list returns empty list."""
        result = await mock_backend.verify_bonds([])
        assert result == []

    @pytest.mark.asyncio()
    async def test_valid_bond(self, mock_backend: BlockchainBackend) -> None:
        """verify_bonds returns valid result for confirmed UTXO."""
        mock_backend.get_utxo = AsyncMock(
            return_value=UTXO(
                txid="a" * 64,
                vout=0,
                value=100_000_000,
                address="bcrt1qfakeaddress",
                confirmations=1000,
                scriptpubkey="0020" + "cd" * 32,
                height=99001,
            )
        )

        bonds = [_make_bond_request()]
        results = await mock_backend.verify_bonds(bonds)

        assert len(results) == 1
        r = results[0]
        assert r.valid is True
        assert r.value == 100_000_000
        assert r.confirmations == 1000
        assert r.block_time == 1700000000
        assert r.error is None

    @pytest.mark.asyncio()
    async def test_utxo_not_found(self, mock_backend: BlockchainBackend) -> None:
        """verify_bonds returns invalid result when UTXO is not found."""
        mock_backend.get_utxo = AsyncMock(return_value=None)

        bonds = [_make_bond_request()]
        results = await mock_backend.verify_bonds(bonds)

        assert len(results) == 1
        assert results[0].valid is False
        assert "not found" in results[0].error.lower()

    @pytest.mark.asyncio()
    async def test_utxo_unconfirmed(self, mock_backend: BlockchainBackend) -> None:
        """verify_bonds returns invalid result when UTXO is unconfirmed."""
        mock_backend.get_utxo = AsyncMock(
            return_value=UTXO(
                txid="a" * 64,
                vout=0,
                value=100_000_000,
                address="bcrt1qfakeaddress",
                confirmations=0,
                scriptpubkey="0020" + "cd" * 32,
            )
        )

        bonds = [_make_bond_request()]
        results = await mock_backend.verify_bonds(bonds)

        assert len(results) == 1
        assert results[0].valid is False
        assert "unconfirmed" in results[0].error.lower()

    @pytest.mark.asyncio()
    async def test_get_utxo_exception(self, mock_backend: BlockchainBackend) -> None:
        """verify_bonds handles exceptions from get_utxo gracefully."""
        mock_backend.get_utxo = AsyncMock(side_effect=ConnectionError("RPC down"))

        bonds = [_make_bond_request()]
        results = await mock_backend.verify_bonds(bonds)

        assert len(results) == 1
        assert results[0].valid is False
        assert "RPC down" in results[0].error

    @pytest.mark.asyncio()
    async def test_scriptpubkey_mismatch(self, mock_backend: BlockchainBackend) -> None:
        """A confirmed UTXO whose scriptPubKey does not match the bond's
        derived script must be rejected. This blocks a malicious maker from
        pointing (txid, vout) at any large confirmed UTXO it does not own."""
        mock_backend.get_utxo = AsyncMock(
            return_value=UTXO(
                txid="a" * 64,
                vout=0,
                value=100_000_000,
                address="bcrt1qfakeaddress",
                confirmations=1000,
                # On-chain script differs from the bond request's "cd"*32.
                scriptpubkey="0020" + "aa" * 32,
                height=99001,
            )
        )

        bonds = [_make_bond_request()]
        results = await mock_backend.verify_bonds(bonds)

        assert len(results) == 1
        assert results[0].valid is False
        assert "scriptpubkey" in results[0].error.lower()

    @pytest.mark.asyncio()
    async def test_scriptpubkey_missing_fails_closed(self, mock_backend: BlockchainBackend) -> None:
        """If the backend cannot supply a scriptPubKey, verification must fail
        closed rather than silently skip the binding check."""
        mock_backend.get_utxo = AsyncMock(
            return_value=UTXO(
                txid="a" * 64,
                vout=0,
                value=100_000_000,
                address="bcrt1qfakeaddress",
                confirmations=1000,
                scriptpubkey="",
                height=99001,
            )
        )

        bonds = [_make_bond_request()]
        results = await mock_backend.verify_bonds(bonds)

        assert len(results) == 1
        assert results[0].valid is False

    @pytest.mark.asyncio()
    async def test_multiple_bonds(self, mock_backend: BlockchainBackend) -> None:
        """verify_bonds processes multiple bonds concurrently."""
        call_count = 0

        async def fake_get_utxo(txid: str, vout: int) -> UTXO | None:
            nonlocal call_count
            call_count += 1
            if vout == 0:
                return UTXO(
                    txid=txid,
                    vout=vout,
                    value=50_000_000,
                    address="bcrt1q1",
                    confirmations=500,
                    scriptpubkey="0020" + "cd" * 32,
                    height=99501,
                )
            return None  # Second bond not found

        mock_backend.get_utxo = fake_get_utxo

        bonds = [
            _make_bond_request(txid="a" * 64, vout=0),
            _make_bond_request(txid="b" * 64, vout=1),
        ]
        results = await mock_backend.verify_bonds(bonds)

        assert len(results) == 2
        assert results[0].valid is True
        assert results[0].value == 50_000_000
        assert results[1].valid is False
        assert call_count == 2

    @pytest.mark.asyncio()
    async def test_block_time_calculation(self, mock_backend: BlockchainBackend) -> None:
        """verify_bonds calculates correct confirmation height for block_time."""
        mock_backend.get_utxo = AsyncMock(
            return_value=UTXO(
                txid="a" * 64,
                vout=0,
                value=100_000_000,
                address="bcrt1q1",
                confirmations=200,
                scriptpubkey="0020" + "cd" * 32,
            )
        )

        bonds = [_make_bond_request()]
        await mock_backend.verify_bonds(bonds)

        # conf_height = 100000 - 200 + 1 = 99801
        mock_backend.get_block_time.assert_called_once_with(99801)


# ---- BondVerificationRequest / BondVerificationResult ----


class TestBondDataclasses:
    """Test BondVerificationRequest and BondVerificationResult dataclasses."""

    def test_request_creation(self) -> None:
        req = _make_bond_request()
        assert req.txid == "a" * 64
        assert req.vout == 0
        assert len(req.utxo_pub) == 33
        assert req.locktime == 1956528000
        assert req.address == "bcrt1qfakeaddress"

    def test_result_valid(self) -> None:
        result = BondVerificationResult(
            txid="a" * 64,
            vout=0,
            value=100_000_000,
            confirmations=1000,
            block_time=1700000000,
            valid=True,
        )
        assert result.valid is True
        assert result.error is None

    def test_result_invalid(self) -> None:
        result = BondVerificationResult(
            txid="a" * 64,
            vout=0,
            value=0,
            confirmations=0,
            block_time=0,
            valid=False,
            error="UTXO not found",
        )
        assert result.valid is False
        assert result.error == "UTXO not found"
