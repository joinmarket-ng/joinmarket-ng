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
                    scriptpubkey="0020" + "aa" * 32,
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
                scriptpubkey="0020" + "aa" * 32,
            )
        )

        bonds = [_make_bond_request()]
        await mock_backend.verify_bonds(bonds)

        # conf_height = 100000 - 200 + 1 = 99801
        mock_backend.get_block_time.assert_called_once_with(99801)


# ---- Bitcoin Core batched verify_bonds() ----


class TestBitcoinCoreVerifyBonds:
    """Test the BitcoinCoreBackend.verify_bonds() batch implementation."""

    @pytest.fixture()
    def mock_core_backend(self) -> MagicMock:
        """Create a mock Bitcoin Core backend."""
        from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

        backend = MagicMock(spec=BitcoinCoreBackend)
        backend.verify_bonds = BitcoinCoreBackend.verify_bonds.__get__(backend)
        backend.get_block_height = AsyncMock(return_value=100000)
        return backend

    @pytest.mark.asyncio()
    async def test_empty_bonds(self, mock_core_backend: MagicMock) -> None:
        """Empty input returns empty output."""
        result = await mock_core_backend.verify_bonds([])
        assert result == []

    @pytest.mark.asyncio()
    async def test_batch_gettxout(self, mock_core_backend: MagicMock) -> None:
        """verify_bonds batches gettxout and getblockheader calls."""
        # Mock _rpc_batch to return gettxout results
        gettxout_result = {
            "value": 1.5,
            "confirmations": 1000,
            "bestblock": "0" * 64,
        }
        block_hash = "f" * 64

        call_count = 0

        async def mock_rpc_batch(requests, client=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: batch gettxout
                return [gettxout_result]
            elif call_count == 2:
                # Second call: batch getblockhash
                return [block_hash]
            elif call_count == 3:
                # Third call: batch getblockheader
                return [{"time": 1700000000}]
            return []

        mock_core_backend._rpc_batch = mock_rpc_batch

        bonds = [_make_bond_request()]
        results = await mock_core_backend.verify_bonds(bonds)

        assert len(results) == 1
        r = results[0]
        assert r.valid is True
        assert r.value == 150_000_000  # 1.5 BTC in sats
        assert r.confirmations == 1000
        assert r.block_time == 1700000000
        assert call_count == 3

    @pytest.mark.asyncio()
    async def test_batch_not_found(self, mock_core_backend: MagicMock) -> None:
        """Bonds with None gettxout result are marked invalid."""

        async def mock_rpc_batch(requests, client=None):
            return [None]

        mock_core_backend._rpc_batch = mock_rpc_batch

        bonds = [_make_bond_request()]
        results = await mock_core_backend.verify_bonds(bonds)

        assert len(results) == 1
        assert results[0].valid is False
        assert "not found" in results[0].error.lower()

    @pytest.mark.asyncio()
    async def test_batch_unconfirmed(self, mock_core_backend: MagicMock) -> None:
        """Bonds with 0 confirmations are marked invalid."""
        gettxout_result = {
            "value": 1.0,
            "confirmations": 0,
        }

        async def mock_rpc_batch(requests, client=None):
            return [gettxout_result]

        mock_core_backend._rpc_batch = mock_rpc_batch

        bonds = [_make_bond_request()]
        results = await mock_core_backend.verify_bonds(bonds)

        assert len(results) == 1
        assert results[0].valid is False
        assert "unconfirmed" in results[0].error.lower()
        assert results[0].value == 100_000_000  # 1 BTC in sats

    @pytest.mark.asyncio()
    async def test_batch_multiple_bonds_shared_blocks(self, mock_core_backend: MagicMock) -> None:
        """Multiple bonds in the same block share a single getblockheader call."""
        # Two bonds confirmed at the same height (100 confs from tip 100000 = height 99901)
        gettxout_a = {"value": 1.0, "confirmations": 100}
        gettxout_b = {"value": 2.0, "confirmations": 100}

        call_count = 0

        async def mock_rpc_batch(requests, client=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [gettxout_a, gettxout_b]
            elif call_count == 2:
                # Only ONE getblockhash call (deduped!)
                assert len(requests) == 1
                return ["hash_99901"]
            elif call_count == 3:
                assert len(requests) == 1
                return [{"time": 1700000000}]
            return []

        mock_core_backend._rpc_batch = mock_rpc_batch

        bonds = [
            _make_bond_request(txid="a" * 64, vout=0),
            _make_bond_request(txid="b" * 64, vout=1),
        ]
        results = await mock_core_backend.verify_bonds(bonds)

        assert len(results) == 2
        assert all(r.valid for r in results)
        assert results[0].value == 100_000_000
        assert results[1].value == 200_000_000
        assert results[0].block_time == results[1].block_time == 1700000000

    @pytest.mark.asyncio()
    async def test_batch_mixed_valid_invalid(self, mock_core_backend: MagicMock) -> None:
        """Mix of valid and invalid bonds in the same batch."""
        call_count = 0

        async def mock_rpc_batch(requests, client=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [
                    {"value": 1.0, "confirmations": 500},  # valid
                    None,  # not found
                    {"value": 0.5, "confirmations": 0},  # unconfirmed
                ]
            elif call_count == 2:
                return ["hash_99501"]
            elif call_count == 3:
                return [{"time": 1700000000}]
            return []

        mock_core_backend._rpc_batch = mock_rpc_batch

        bonds = [
            _make_bond_request(txid="a" * 64, vout=0),
            _make_bond_request(txid="b" * 64, vout=1),
            _make_bond_request(txid="c" * 64, vout=2),
        ]
        results = await mock_core_backend.verify_bonds(bonds)

        assert len(results) == 3
        assert results[0].valid is True
        assert results[0].value == 100_000_000
        assert results[1].valid is False
        assert "not found" in results[1].error.lower()
        assert results[2].valid is False
        assert "unconfirmed" in results[2].error.lower()


# ---- JSON-RPC batch method ----


class TestRpcBatch:
    """Test the _rpc_batch method on BitcoinCoreBackend."""

    @pytest.mark.asyncio()
    async def test_rpc_batch_empty(self) -> None:
        """Empty batch returns empty results."""
        from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

        backend = MagicMock(spec=BitcoinCoreBackend)
        backend._rpc_batch = BitcoinCoreBackend._rpc_batch.__get__(backend)

        result = await backend._rpc_batch([])
        assert result == []

    @pytest.mark.asyncio()
    async def test_rpc_batch_sends_correct_payload(self) -> None:
        """_rpc_batch sends correct JSON-RPC batch payload."""
        from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

        backend = MagicMock(spec=BitcoinCoreBackend)
        backend._rpc_batch = BitcoinCoreBackend._rpc_batch.__get__(backend)

        # Mock the HTTP client
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"id": 0, "result": {"value": 1.0}, "error": None},
            {"id": 1, "result": {"value": 2.0}, "error": None},
        ]

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        backend.client = mock_client
        backend.rpc_url = "http://localhost:8332"

        result = await backend._rpc_batch(
            [
                ("gettxout", ["txid1", 0, True]),
                ("gettxout", ["txid2", 1, True]),
            ]
        )

        assert len(result) == 2
        assert result[0] == {"value": 1.0}
        assert result[1] == {"value": 2.0}

        # Verify the batch payload structure
        call_args = mock_client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert len(payload) == 2
        assert payload[0]["method"] == "gettxout"
        assert payload[0]["id"] == 0
        assert payload[1]["method"] == "gettxout"
        assert payload[1]["id"] == 1

    @pytest.mark.asyncio()
    async def test_rpc_batch_handles_errors(self) -> None:
        """_rpc_batch returns None for failed batch items."""
        from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

        backend = MagicMock(spec=BitcoinCoreBackend)
        backend._rpc_batch = BitcoinCoreBackend._rpc_batch.__get__(backend)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = [
            {"id": 0, "result": {"value": 1.0}, "error": None},
            {
                "id": 1,
                "result": None,
                "error": {"code": -5, "message": "No such mempool or blockchain transaction"},
            },
        ]

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        backend.client = mock_client
        backend.rpc_url = "http://localhost:8332"

        result = await backend._rpc_batch(
            [
                ("gettxout", ["txid1", 0, True]),
                ("gettxout", ["txid2", 1, True]),
            ]
        )

        assert result[0] == {"value": 1.0}
        assert result[1] is None  # Error item returns None

    @pytest.mark.asyncio()
    async def test_rpc_batch_out_of_order_response(self) -> None:
        """_rpc_batch handles responses returned out of order."""
        from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

        backend = MagicMock(spec=BitcoinCoreBackend)
        backend._rpc_batch = BitcoinCoreBackend._rpc_batch.__get__(backend)

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        # Responses in REVERSE order
        mock_response.json.return_value = [
            {"id": 1, "result": "second", "error": None},
            {"id": 0, "result": "first", "error": None},
        ]

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        backend.client = mock_client
        backend.rpc_url = "http://localhost:8332"

        result = await backend._rpc_batch(
            [
                ("method_a", []),
                ("method_b", []),
            ]
        )

        # Results indexed by ID, not response order
        assert result[0] == "first"
        assert result[1] == "second"


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
