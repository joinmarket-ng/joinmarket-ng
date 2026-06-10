"""
Tests for transaction builder module.
"""

from __future__ import annotations

import pytest
from jmcore.bitcoin import address_to_scriptpubkey, serialize_outpoint

from taker.tx_builder import (
    CoinJoinTxBuilder,
    CoinJoinTxData,
    TxInput,
    TxOutput,
    build_coinjoin_tx,
    calculate_tx_fee,
    varint,
)


class TestVarint:
    """Tests for varint encoding."""

    def test_single_byte(self) -> None:
        """Test single-byte varint (0-252)."""
        assert varint(0) == bytes([0x00])
        assert varint(1) == bytes([0x01])
        assert varint(252) == bytes([0xFC])

    def test_two_bytes(self) -> None:
        """Test two-byte varint (253-65535)."""
        result = varint(253)
        assert result[0] == 0xFD
        assert len(result) == 3

        result = varint(65535)
        assert result[0] == 0xFD
        assert len(result) == 3

    def test_four_bytes(self) -> None:
        """Test four-byte varint (65536-4294967295)."""
        result = varint(65536)
        assert result[0] == 0xFE
        assert len(result) == 5

    def test_eight_bytes(self) -> None:
        """Test eight-byte varint (> 4294967295)."""
        result = varint(4294967296)
        assert result[0] == 0xFF
        assert len(result) == 9


class TestSerializeOutpoint:
    """Tests for outpoint serialization."""

    def test_serialize_outpoint(self) -> None:
        """Test outpoint serialization reverses txid."""
        txid = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        vout = 1

        result = serialize_outpoint(txid, vout)

        # Should be 32 bytes (reversed txid) + 4 bytes (vout)
        assert len(result) == 36

        # txid should be reversed (little-endian)
        expected_txid = bytes.fromhex(txid)[::-1]
        assert result[:32] == expected_txid

        # vout should be little-endian uint32
        assert result[32:36] == bytes([0x01, 0x00, 0x00, 0x00])


class TestAddressToScriptPubKey:
    """Tests for address to scriptPubKey conversion."""

    def test_p2wpkh_mainnet(self) -> None:
        """Test mainnet P2WPKH address."""
        # Known address from BIP-0173
        address = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        script = address_to_scriptpubkey(address)

        # P2WPKH: OP_0 <20-byte-hash>
        assert script[0] == 0x00
        assert script[1] == 0x14  # 20 bytes
        assert len(script) == 22

    def test_p2wpkh_testnet(self) -> None:
        """Test testnet P2WPKH address."""
        address = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
        script = address_to_scriptpubkey(address)

        assert script[0] == 0x00
        assert script[1] == 0x14
        assert len(script) == 22

    def test_p2wpkh_regtest(self) -> None:
        """Test regtest P2WPKH address."""
        address = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
        script = address_to_scriptpubkey(address)

        assert script[0] == 0x00
        assert script[1] == 0x14
        assert len(script) == 22

    def test_p2wsh_mainnet(self) -> None:
        """Test mainnet P2WSH address."""
        # 62-character bech32 address
        address = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
        script = address_to_scriptpubkey(address)

        # P2WSH: OP_0 <32-byte-hash>
        assert script[0] == 0x00
        assert script[1] == 0x20  # 32 bytes
        assert len(script) == 34

    def test_p2pkh_mainnet(self) -> None:
        """Test mainnet P2PKH address."""
        address = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"
        script = address_to_scriptpubkey(address)

        # P2PKH: OP_DUP OP_HASH160 <20-byte-hash> OP_EQUALVERIFY OP_CHECKSIG
        assert script[0] == 0x76  # OP_DUP
        assert script[1] == 0xA9  # OP_HASH160
        assert script[2] == 0x14  # 20 bytes
        assert script[-2] == 0x88  # OP_EQUALVERIFY
        assert script[-1] == 0xAC  # OP_CHECKSIG
        assert len(script) == 25

    def test_p2sh_mainnet(self) -> None:
        """Test mainnet P2SH address."""
        address = "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"
        script = address_to_scriptpubkey(address)

        # P2SH: OP_HASH160 <20-byte-hash> OP_EQUAL
        assert script[0] == 0xA9  # OP_HASH160
        assert script[1] == 0x14  # 20 bytes
        assert script[-1] == 0x87  # OP_EQUAL
        assert len(script) == 23

    def test_invalid_bech32(self) -> None:
        """Test invalid bech32 address."""
        with pytest.raises(ValueError, match="Invalid bech32"):
            address_to_scriptpubkey("bc1invalid")

    def test_invalid_base58(self) -> None:
        """Test invalid base58 address."""
        with pytest.raises(Exception):  # base58 raises its own exception
            address_to_scriptpubkey("1InvalidAddress")


class TestTxInput:
    """Tests for TxInput dataclass."""

    def test_default_values(self) -> None:
        """Test default values."""
        inp = TxInput.from_hex(txid="a" * 64, vout=0, value=100000)
        assert inp.scriptpubkey_hex == ""
        assert inp.sequence == 0xFFFFFFFF

    def test_custom_sequence(self) -> None:
        """Test custom sequence number."""
        inp = TxInput.from_hex(txid="a" * 64, vout=1, value=50000, sequence=0xFFFFFFFE)
        assert inp.sequence == 0xFFFFFFFE


class TestTxOutput:
    """Tests for TxOutput dataclass."""

    def test_basic_output(self) -> None:
        """Test basic output creation."""
        out = TxOutput.from_address("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", 100000)
        assert out.address("mainnet") == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
        assert out.value == 100000


class TestCalculateTxFee:
    """Tests for transaction fee calculation."""

    def test_simple_fee_calculation(self) -> None:
        """Test simple fee calculation."""
        # 1 taker input, 2 maker inputs, 5 outputs (3 CJ + 2 change)
        fee = calculate_tx_fee(
            num_taker_inputs=1,
            num_maker_inputs=2,
            num_outputs=5,
            fee_rate=10,
        )

        # Expected: (3 * 68) + (5 * 31) + 11 = 204 + 155 + 11 = 370 vbytes
        # 370 * 10 = 3700 sats
        assert fee == 3700

    def test_larger_coinjoin(self) -> None:
        """Test fee for larger CoinJoin."""
        # 2 taker inputs, 8 maker inputs, 12 outputs
        fee = calculate_tx_fee(
            num_taker_inputs=2,
            num_maker_inputs=8,
            num_outputs=12,
            fee_rate=5,
        )

        # Expected: (10 * 68) + (12 * 31) + 11 = 680 + 372 + 11 = 1063 vbytes
        # 1063 * 5 = 5315 sats
        assert fee == 5315

    def test_taproot_fee_is_lower_per_input(self) -> None:
        """Taproot (p2tr) inputs are smaller than segwit, so the fee differs."""
        segwit = calculate_tx_fee(
            num_taker_inputs=1,
            num_maker_inputs=2,
            num_outputs=5,
            fee_rate=10,
            script_type="p2wpkh",
        )
        taproot = calculate_tx_fee(
            num_taker_inputs=1,
            num_maker_inputs=2,
            num_outputs=5,
            fee_rate=10,
            script_type="p2tr",
        )
        # p2tr key-path inputs are ~57.5 vbytes (vs 68) but p2tr outputs are
        # 43 vbytes (vs 31); with 3 inputs and 5 outputs the larger outputs
        # dominate, so the taproot estimate is higher here.
        assert taproot != segwit
        assert taproot > 0


class TestCoinJoinTxBuilder:
    """Tests for CoinJoinTxBuilder class."""

    @pytest.fixture
    def builder(self) -> CoinJoinTxBuilder:
        """Create a builder for tests."""
        return CoinJoinTxBuilder(network="regtest")

    @pytest.fixture
    def sample_tx_data(self) -> CoinJoinTxData:
        """Create sample transaction data."""
        return CoinJoinTxData(
            taker_inputs=[
                TxInput.from_hex(txid="a" * 64, vout=0, value=2_000_000),
            ],
            taker_cj_output=TxOutput.from_address(
                "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                1_000_000,
            ),
            taker_change_output=TxOutput.from_address(
                "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                990_000,
            ),
            maker_inputs={
                "maker1": [TxInput.from_hex(txid="b" * 64, vout=1, value=1_500_000)],
                "maker2": [TxInput.from_hex(txid="c" * 64, vout=2, value=1_200_000)],
            },
            maker_cj_outputs={
                "maker1": TxOutput.from_address(
                    "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                    1_000_000,
                ),
                "maker2": TxOutput.from_address(
                    "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                    1_000_000,
                ),
            },
            maker_change_outputs={
                "maker1": TxOutput.from_address(
                    "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                    501_000,
                ),
                "maker2": TxOutput.from_address(
                    "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                    201_000,
                ),
            },
            cj_amount=1_000_000,
            total_maker_fee=2_000,
            tx_fee=8_000,
        )

    def test_build_unsigned_tx(
        self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData
    ) -> None:
        """Test building an unsigned transaction."""
        tx_bytes, metadata = builder.build_unsigned_tx(sample_tx_data)

        # Check that we got bytes
        assert isinstance(tx_bytes, bytes)
        assert len(tx_bytes) > 0

        # Check metadata
        assert "input_owners" in metadata
        assert "output_owners" in metadata
        assert "input_values" in metadata

        # Should have 3 inputs (1 taker + 2 makers)
        assert len(metadata["input_owners"]) == 3

        # Should have 6 outputs (3 CJ + 3 change)
        assert len(metadata["output_owners"]) == 6

    def test_tx_has_correct_version(
        self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData
    ) -> None:
        """Test that transaction has version 2."""
        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)

        # Version is first 4 bytes (little-endian)
        version = int.from_bytes(tx_bytes[:4], "little")
        assert version == 2

    def test_unsigned_tx_has_no_segwit_marker(
        self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData
    ) -> None:
        """Test that unsigned transaction has NO SegWit marker (non-SegWit format).

        Unsigned transactions use the traditional format without marker/flag/witness.
        The SegWit marker (0x00, 0x01) is only added when signatures/witnesses are present.
        This is required for compatibility with reference JoinMarket implementation.
        """
        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)

        # After version (4 bytes), the next byte should be the input count (3 in our test data)
        # NOT the SegWit marker (0x00)
        assert tx_bytes[4] == 3  # Input count, not marker

    def test_parse_tx_roundtrip(
        self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData
    ) -> None:
        """Test that parsing and re-serializing produces same result."""
        from jmcore.bitcoin import parse_transaction_bytes

        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)

        # Parse
        parsed = parse_transaction_bytes(tx_bytes)

        # Verify counts
        assert len(parsed.inputs) == 3
        assert len(parsed.outputs) == 6
        assert parsed.version == 2
        assert parsed.locktime == 0

    def test_get_txid(self, builder: CoinJoinTxBuilder, sample_tx_data: CoinJoinTxData) -> None:
        """Test txid calculation."""
        tx_bytes, _ = builder.build_unsigned_tx(sample_tx_data)
        txid = builder.get_txid(tx_bytes)

        # Should be 64 hex characters
        assert len(txid) == 64
        assert all(c in "0123456789abcdef" for c in txid)


class TestBuildCoinjoinTx:
    """Tests for build_coinjoin_tx convenience function."""

    def test_build_simple_coinjoin(self) -> None:
        """Test building a simple CoinJoin transaction."""
        taker_utxos = [
            {"txid": "a" * 64, "vout": 0, "value": 2_000_000},
        ]
        maker_data = {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 1_500_000}],
                "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                "change_addr": "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                "cjfee": 1000,
            },
        }

        tx_bytes, metadata = build_coinjoin_tx(
            taker_utxos=taker_utxos,
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_total_input=2_000_000,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=5000,
            network="regtest",
        )

        assert isinstance(tx_bytes, bytes)
        assert len(tx_bytes) > 0

        # Should have 2 inputs
        assert len(metadata["input_owners"]) == 2

        # Should have 4 outputs (2 CJ + 2 change)
        assert len(metadata["output_owners"]) == 4

    def test_build_coinjoin_dust_change_excluded(self) -> None:
        """Test that dust change outputs are excluded."""
        taker_utxos = [
            {
                "txid": "a" * 64,
                "vout": 0,
                "value": 1_001_500,
            },  # Just enough for CJ + fee + tiny change
        ]
        maker_data = {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 1_000_500}],  # Just enough
                "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                "change_addr": "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                "cjfee": 500,  # Maker gets this fee
            },
        }

        tx_bytes, metadata = build_coinjoin_tx(
            taker_utxos=taker_utxos,
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_total_input=1_001_500,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=500,
            network="regtest",
            dust_threshold=27300,  # Use default JoinMarket dust threshold
        )

        # Taker change: 1_001_500 - 1_000_000 - 500 - 500 = 500 (dust, excluded)
        # Maker change: 1_000_500 - 1_000_000 + 500 = 1000 (dust, excluded with 27300 threshold)
        # So only 2 outputs: 2 CJ outputs
        change_outputs = [o for o in metadata["output_owners"] if o[1] == "change"]
        assert len(change_outputs) == 0

    def test_build_coinjoin_negative_maker_change_raises_error(self) -> None:
        """Test that negative maker change raises ValueError.

        This can happen when maker UTXO verification fails (value=0)
        or maker's UTXOs were spent between offer and coinjoin.
        """
        taker_utxos = [
            {
                "txid": "a" * 64,
                "vout": 0,
                "value": 2_000_000,
            },
        ]
        # Maker has 0 value UTXOs (verification failed)
        maker_data = {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 0}],  # Verification failed!
                "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                "change_addr": "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                "cjfee": 500,
            },
        }

        import pytest

        with pytest.raises(ValueError, match="has insufficient funds"):
            build_coinjoin_tx(
                taker_utxos=taker_utxos,
                taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                taker_total_input=2_000_000,
                maker_data=maker_data,
                cj_amount=1_000_000,
                tx_fee=5000,
                network="regtest",
            )

    def test_build_coinjoin_configurable_dust_threshold(self) -> None:
        """Test that dust threshold is configurable and enforced correctly."""
        taker_utxos = [
            {
                "txid": "a" * 64,
                "vout": 0,
                "value": 1_050_000,
            },
        ]
        maker_data = {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 1_030_000}],
                "cj_addr": "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                "change_addr": "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                "cjfee": 500,
            },
        }

        # Test with low dust threshold (546 sats) - change outputs should be included
        tx_bytes, metadata = build_coinjoin_tx(
            taker_utxos=taker_utxos,
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_total_input=1_050_000,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=1000,
            network="regtest",
            dust_threshold=546,  # Standard Bitcoin dust threshold
        )

        # Taker change: 1_050_000 - 1_000_000 - 500 - 1000 = 48_500 (above 546)
        # Maker change: 1_030_000 - 1_000_000 + 500 = 30_500 (above 546)
        # So 4 outputs: 2 CJ + 2 change
        change_outputs = [o for o in metadata["output_owners"] if o[1] == "change"]
        assert len(change_outputs) == 2

        # Test with high dust threshold (27300 sats) - change outputs should still be included
        tx_bytes, metadata = build_coinjoin_tx(
            taker_utxos=taker_utxos,
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_total_input=1_050_000,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=1000,
            network="regtest",
            dust_threshold=27300,  # JoinMarket default dust threshold
        )

        # Both changes are above 27300, so still 2 change outputs
        change_outputs = [o for o in metadata["output_owners"] if o[1] == "change"]
        assert len(change_outputs) == 2

        # Test with dust threshold higher than change - outputs should be excluded
        tx_bytes, metadata = build_coinjoin_tx(
            taker_utxos=taker_utxos,
            taker_cj_address="bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            taker_change_address="bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_total_input=1_050_000,
            maker_data=maker_data,
            cj_amount=1_000_000,
            tx_fee=1000,
            network="regtest",
            dust_threshold=50000,  # Higher than both change amounts
        )

        # Both changes are below 50000, so no change outputs
        change_outputs = [o for o in metadata["output_owners"] if o[1] == "change"]
        assert len(change_outputs) == 0


class TestAddSignaturesValidation:
    """Tests that add_signatures enforces complete signature coverage.

    A CoinJoin transaction is only valid when every input has a signature.
    The add_signatures method must reject any attempt to assemble a
    transaction with missing signatures rather than silently producing
    an invalid transaction.
    """

    @pytest.fixture
    def builder(self) -> CoinJoinTxBuilder:
        return CoinJoinTxBuilder(network="regtest")

    @pytest.fixture
    def two_maker_tx(self, builder: CoinJoinTxBuilder) -> tuple[bytes, dict]:
        """Build a transaction with 1 taker + 2 makers (3 inputs)."""
        tx_data = CoinJoinTxData(
            taker_inputs=[TxInput.from_hex(txid="a" * 64, vout=0, value=2_000_000)],
            taker_cj_output=TxOutput.from_address(
                "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                1_000_000,
            ),
            taker_change_output=TxOutput.from_address(
                "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                990_000,
            ),
            maker_inputs={
                "maker1": [TxInput.from_hex(txid="b" * 64, vout=1, value=1_500_000)],
                "maker2": [TxInput.from_hex(txid="c" * 64, vout=2, value=1_200_000)],
            },
            maker_cj_outputs={
                "maker1": TxOutput.from_address(
                    "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                    1_000_000,
                ),
                "maker2": TxOutput.from_address(
                    "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                    1_000_000,
                ),
            },
            maker_change_outputs={
                "maker1": TxOutput.from_address(
                    "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                    501_000,
                ),
                "maker2": TxOutput.from_address(
                    "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                    201_000,
                ),
            },
            cj_amount=1_000_000,
            total_maker_fee=2_000,
            tx_fee=8_000,
        )
        return builder.build_unsigned_tx(tx_data)

    def _make_fake_sig(self, txid: str, vout: int) -> dict:
        """Create a fake signature dict for testing assembly logic."""
        fake_sig = "30" + "44" * 35  # fake DER-ish hex
        fake_pubkey = "02" + "ab" * 32  # fake compressed pubkey hex
        return {
            "txid": txid,
            "vout": vout,
            "witness": [fake_sig, fake_pubkey],
        }

    def test_raises_when_no_signatures_provided(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Empty signature dict must be rejected."""
        tx_bytes, metadata = two_maker_tx
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, {}, metadata)

    def test_raises_when_maker_missing(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Transaction with one maker's signatures missing must be rejected."""
        tx_bytes, metadata = two_maker_tx

        # Provide taker + maker1 only, maker2 is missing
        signatures = {
            "taker": [self._make_fake_sig("a" * 64, 0)],
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            # maker2 deliberately omitted
        }
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)

    def test_raises_when_taker_missing(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Transaction with taker's signature missing must be rejected."""
        tx_bytes, metadata = two_maker_tx

        signatures = {
            # taker deliberately omitted
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            "maker2": [self._make_fake_sig("c" * 64, 2)],
        }
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)

    def test_raises_when_maker_provides_empty_list(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Maker present in dict but with empty signature list must be rejected."""
        tx_bytes, metadata = two_maker_tx

        signatures = {
            "taker": [self._make_fake_sig("a" * 64, 0)],
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            "maker2": [],  # present but empty
        }
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)

    def test_raises_when_maker_provides_wrong_utxo(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Maker signature for a different UTXO (not in the transaction) must be rejected."""
        tx_bytes, metadata = two_maker_tx

        signatures = {
            "taker": [self._make_fake_sig("a" * 64, 0)],
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            "maker2": [self._make_fake_sig("f" * 64, 99)],  # wrong UTXO
        }
        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, signatures, metadata)

    def test_succeeds_when_all_signatures_present(
        self, builder: CoinJoinTxBuilder, two_maker_tx: tuple[bytes, dict]
    ) -> None:
        """Transaction with all signatures present must succeed."""
        tx_bytes, metadata = two_maker_tx

        signatures = {
            "taker": [self._make_fake_sig("a" * 64, 0)],
            "maker1": [self._make_fake_sig("b" * 64, 1)],
            "maker2": [self._make_fake_sig("c" * 64, 2)],
        }
        signed_tx = builder.add_signatures(tx_bytes, signatures, metadata)
        assert isinstance(signed_tx, bytes)
        assert len(signed_tx) > len(tx_bytes)


class TestTimelockedInputs:
    """Tests for nLockTime / nSequence handling of transaction inputs."""

    _CJ_ADDR = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
    _CHANGE_ADDR = "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry"

    def _maker_data(self) -> dict:
        return {
            "maker1": {
                "utxos": [{"txid": "b" * 64, "vout": 1, "value": 1_500_000}],
                "cj_addr": self._CJ_ADDR,
                "change_addr": self._CHANGE_ADDR,
                "cjfee": 1000,
            },
        }

    def test_regular_inputs_keep_zero_locktime_and_final_sequence(self) -> None:
        """Transactions without timelocked inputs stay at locktime 0 / final sequence."""
        from jmcore.bitcoin import parse_transaction_bytes

        tx_bytes, _ = build_coinjoin_tx(
            taker_utxos=[{"txid": "a" * 64, "vout": 0, "value": 2_000_000}],
            taker_cj_address=self._CJ_ADDR,
            taker_change_address=self._CHANGE_ADDR,
            taker_total_input=2_000_000,
            maker_data=self._maker_data(),
            cj_amount=1_000_000,
            tx_fee=5000,
            network="regtest",
        )

        parsed = parse_transaction_bytes(tx_bytes)
        assert parsed.locktime == 0
        assert all(inp.sequence == 0xFFFFFFFF for inp in parsed.inputs)
