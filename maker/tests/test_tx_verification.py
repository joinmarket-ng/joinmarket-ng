"""
Tests for transaction verification - MOST CRITICAL security component!
"""

from unittest.mock import patch

import pytest
from jmcore.models import NetworkType, OfferType
from jmwallet.wallet.models import UTXOInfo

from maker.tx_verification import (
    calculate_cj_fee,
    parse_transaction,
    verify_unsigned_transaction,
)


def test_calculate_cj_fee_absolute():
    """Test absolute fee calculation"""
    fee = calculate_cj_fee(OfferType.SW0_ABSOLUTE, 1000, 100_000_000)
    assert fee == 1000

    fee = calculate_cj_fee(OfferType.SWA_ABSOLUTE, "2000", 50_000_000)
    assert fee == 2000


def test_calculate_cj_fee_relative():
    """Test relative fee calculation"""
    fee = calculate_cj_fee(OfferType.SW0_RELATIVE, "0.0001", 100_000_000)
    assert fee == 10_000

    fee = calculate_cj_fee(OfferType.SWA_RELATIVE, "0.0002", 50_000_000)
    assert fee == 10_000


def test_verify_transaction_negative_profit():
    """
    CRITICAL TEST: Ensure negative profit is rejected.

    This prevents the maker from losing money!
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    mock_parsed_tx = {
        "inputs": [{"txid": "abc123", "vout": 0}],
        "outputs": [
            {"value": 50_000_000, "address": "bcrt1qcj"},
            {"value": 49_999_000, "address": "bcrt1qchange"},
        ],
    }

    with patch("maker.tx_verification.parse_transaction", return_value=mock_parsed_tx):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy_tx_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee=1000,
            txfee=2000,
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert not is_valid
    assert "Negative profit" in error


def test_verify_transaction_missing_utxo():
    """
    CRITICAL TEST: Ensure all our UTXOs must be in the transaction.
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        ),
        ("def456", 1): UTXOInfo(
            txid="def456",
            vout=1,
            value=50_000_000,
            address="bcrt1qtest2",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/1",
            mixdepth=0,
        ),
    }

    mock_parsed_tx = {
        "inputs": [{"txid": "abc123", "vout": 0}],
        "outputs": [
            {"value": 50_000_000, "address": "bcrt1qcj"},
            {"value": 49_990_000, "address": "bcrt1qchange"},
        ],
    }

    with patch("maker.tx_verification.parse_transaction", return_value=mock_parsed_tx):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy_tx_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee="0.001",
            txfee=10_000,
            offer_type=OfferType.SW0_RELATIVE,
        )

    assert not is_valid
    assert "Our UTXOs not included" in error


def test_calculate_expected_change():
    """
    Test change calculation formula:
    expected_change = my_total_in - amount - txfee + real_cjfee
    """
    my_total_in = 100_000_000
    amount = 50_000_000
    txfee = 10_000
    real_cjfee = 50_000

    expected_change = my_total_in - amount - txfee + real_cjfee

    assert expected_change == 50_040_000


def test_profit_calculation():
    """
    Test profit calculation:
    profit = real_cjfee - txfee

    Must be positive!
    """
    real_cjfee = 50_000
    txfee = 10_000
    profit = real_cjfee - txfee

    assert profit == 40_000
    assert profit > 0

    negative_case_cjfee = 5_000
    negative_case_txfee = 10_000
    negative_profit = negative_case_cjfee - negative_case_txfee

    assert negative_profit == -5_000
    assert negative_profit < 0


def test_script_to_address_network_hrp():
    """Test that script_to_address uses correct HRP for each network."""
    from jmcore.models import NetworkType

    from maker.tx_verification import get_bech32_hrp, script_to_address

    # P2WPKH script: OP_0 <20-byte-hash>
    # Using known hash for testing
    witness_program = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
    p2wpkh_script = bytes([0x00, 0x14]) + witness_program

    # Test HRP mapping
    assert get_bech32_hrp(NetworkType.MAINNET) == "bc"
    assert get_bech32_hrp(NetworkType.TESTNET) == "tb"
    assert get_bech32_hrp(NetworkType.SIGNET) == "tb"
    assert get_bech32_hrp(NetworkType.REGTEST) == "bcrt"

    # Test address generation for different networks
    mainnet_addr = script_to_address(p2wpkh_script, NetworkType.MAINNET)
    testnet_addr = script_to_address(p2wpkh_script, NetworkType.TESTNET)
    regtest_addr = script_to_address(p2wpkh_script, NetworkType.REGTEST)

    assert mainnet_addr.startswith("bc1")
    assert testnet_addr.startswith("tb1")
    assert regtest_addr.startswith("bcrt1")

    # Verify known address (BIP 173 test vector for mainnet)
    # bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4 is the address for this witness program
    assert mainnet_addr == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"


def test_verify_transaction_valid():
    """
    Test that a valid transaction passes verification.

    Change = total_in - cj_amount - txfee + cjfee
    Change = 100M - 50M - 1000 + 10000 = 50,009,000
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    mock_parsed_tx = {
        "inputs": [{"txid": "abc123", "vout": 0}],
        "outputs": [
            {"value": 50_000_000, "address": "bcrt1qcj"},
            {"value": 50_009_000, "address": "bcrt1qchange"},  # Correct change!
        ],
    }

    with patch("maker.tx_verification.parse_transaction", return_value=mock_parsed_tx):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy_tx_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee=10_000,  # 10k sats fee
            txfee=1000,  # 1k sats txfee
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert is_valid, f"Expected valid, got error: {error}"
    assert error == ""


def test_verify_transaction_cj_output_too_low():
    """
    CRITICAL TEST: Ensure CJ output below expected amount is rejected.
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    mock_parsed_tx = {
        "inputs": [{"txid": "abc123", "vout": 0}],
        "outputs": [
            {"value": 49_000_000, "address": "bcrt1qcj"},  # Too low!
            {"value": 50_000_000, "address": "bcrt1qchange"},
        ],
    }

    with patch("maker.tx_verification.parse_transaction", return_value=mock_parsed_tx):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy_tx_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee=10_000,
            txfee=1000,
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert not is_valid
    assert "CJ output value too low" in error


def test_verify_transaction_change_output_too_low():
    """
    CRITICAL TEST: Ensure change output below expected amount is rejected.
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    # Expected change: 100M - 50M - 1k + 10k = 50,009,000
    mock_parsed_tx = {
        "inputs": [{"txid": "abc123", "vout": 0}],
        "outputs": [
            {"value": 50_000_000, "address": "bcrt1qcj"},
            {"value": 40_000_000, "address": "bcrt1qchange"},  # Too low!
        ],
    }

    with patch("maker.tx_verification.parse_transaction", return_value=mock_parsed_tx):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy_tx_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee=10_000,
            txfee=1000,
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert not is_valid
    assert "Change output value too low" in error


def test_verify_transaction_cj_address_missing():
    """
    CRITICAL TEST: Ensure missing CJ address is rejected.

    Change = 100M - 50M - 1000 + 10000 = 50,009,000
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    mock_parsed_tx = {
        "inputs": [{"txid": "abc123", "vout": 0}],
        "outputs": [
            {"value": 50_000_000, "address": "bcrt1qother"},  # Wrong address!
            {"value": 50_009_000, "address": "bcrt1qchange"},  # Correct change
        ],
    }

    with patch("maker.tx_verification.parse_transaction", return_value=mock_parsed_tx):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy_tx_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",  # Not in outputs
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee=10_000,
            txfee=1000,
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert not is_valid
    assert "CJ address appears 0 times" in error


def test_verify_transaction_change_address_missing():
    """
    CRITICAL TEST: Ensure missing change address is rejected.

    Change = 100M - 50M - 1000 + 10000 = 50,009,000
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    mock_parsed_tx = {
        "inputs": [{"txid": "abc123", "vout": 0}],
        "outputs": [
            {"value": 50_000_000, "address": "bcrt1qcj"},
            {"value": 50_009_000, "address": "bcrt1qother"},  # Wrong address!
        ],
    }

    with patch("maker.tx_verification.parse_transaction", return_value=mock_parsed_tx):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy_tx_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",  # Not in outputs
            amount=50_000_000,
            cjfee=10_000,
            txfee=1000,
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert not is_valid
    assert "Change address appears 0 times" in error


def test_verify_transaction_duplicate_cj_address():
    """
    CRITICAL TEST: Ensure duplicate CJ address is rejected.

    This prevents confusion attacks where taker duplicates our address.

    Change = 100M - 50M - 1000 + 10000 = 50,009,000
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    mock_parsed_tx = {
        "inputs": [{"txid": "abc123", "vout": 0}],
        "outputs": [
            {"value": 50_000_000, "address": "bcrt1qcj"},
            {"value": 50_000_000, "address": "bcrt1qcj"},  # Duplicate!
            {"value": 50_009_000, "address": "bcrt1qchange"},  # Correct change
        ],
    }

    with patch("maker.tx_verification.parse_transaction", return_value=mock_parsed_tx):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy_tx_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee=10_000,
            txfee=1000,
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert not is_valid
    assert "CJ address appears 2 times" in error


def test_verify_transaction_parse_failure():
    """
    Test that parse failure is handled gracefully.
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    with patch("maker.tx_verification.parse_transaction", return_value=None):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="invalid_hex",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee=10_000,
            txfee=1000,
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert not is_valid
    assert "Failed to parse transaction" in error


def test_parse_transaction_real_tx():
    """
    Test parsing a real Bitcoin transaction.

    Using a simplified 1-in-1-out P2WPKH transaction.
    """
    from maker.tx_verification import parse_transaction

    # Simple regtest transaction:
    # Version: 02000000
    # Marker+Flag: 0001 (segwit)
    # Input count: 01
    # Input 1: txid (32 bytes) + vout (4 bytes) + scriptSig len (0) + sequence
    # Output count: 01
    # Output 1: value (8 bytes) + scriptPubKey (P2WPKH)
    # Witness data
    # Locktime

    # Minimal valid segwit tx (1 input, 1 P2WPKH output)
    tx_hex = (
        "02000000"  # version
        "0001"  # segwit marker+flag
        "01"  # input count
        "0000000000000000000000000000000000000000000000000000000000000001"  # txid (reversed)
        "00000000"  # vout
        "00"  # scriptSig length (empty for segwit)
        "ffffffff"  # sequence
        "01"  # output count
        "00e1f50500000000"  # value: 1 BTC = 100,000,000 sats (little endian)
        "16"  # scriptPubKey length: 22 bytes
        "0014751e76e8199196d454941c45d1b3a323f1433bd6"  # P2WPKH script
        "00"  # witness item count (empty witness - unsigned)
        "00000000"  # locktime
    )

    result = parse_transaction(tx_hex)

    assert result is not None
    assert len(result["inputs"]) == 1
    assert (
        result["inputs"][0]["txid"]
        == "0100000000000000000000000000000000000000000000000000000000000000"
    )
    assert result["inputs"][0]["vout"] == 0
    assert len(result["outputs"]) == 1
    assert result["outputs"][0]["value"] == 100_000_000


def test_parse_transaction_invalid_hex():
    """
    Test that invalid hex returns None.
    """
    from maker.tx_verification import parse_transaction

    result = parse_transaction("not_valid_hex")
    assert result is None

    result = parse_transaction("")
    assert result is None

    result = parse_transaction("0102")  # Too short
    assert result is None


def test_read_varint_edge_cases():
    """
    Test varint parsing for different sizes.
    """
    from maker.tx_verification import read_varint

    # Single byte (< 0xFD)
    value, offset = read_varint(bytes([0x10]), 0)
    assert value == 16
    assert offset == 1

    # 2-byte value (0xFD prefix)
    data = bytes([0xFD, 0x00, 0x01])  # 256 in little endian
    value, offset = read_varint(data, 0)
    assert value == 256
    assert offset == 3

    # 4-byte value (0xFE prefix)
    data = bytes([0xFE, 0x00, 0x00, 0x01, 0x00])  # 65536 in little endian
    value, offset = read_varint(data, 0)
    assert value == 65536
    assert offset == 5

    # 8-byte value (0xFF prefix)
    data = bytes([0xFF, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00])
    value, offset = read_varint(data, 0)
    assert value == 4294967296  # 2^32
    assert offset == 9


def test_script_to_address_unsupported_script():
    """
    Test handling of various script types.
    P2PKH is now supported (returns base58 address).
    P2WSH is supported (returns bech32 address).
    """
    from maker.tx_verification import script_to_address

    # P2PKH script (now supported - returns base58 address)
    p2pkh_script = bytes.fromhex("76a914751e76e8199196d454941c45d1b3a323f1433bd688ac")
    result = script_to_address(p2pkh_script)
    # Should be a valid base58 P2PKH mainnet address starting with '1'
    assert result.startswith("1")
    assert result == "1BgGZ9tcN4rm9KBzDn7KprQz87SZ26SAMH"

    # P2WSH script (now supported - returns bech32 address)
    p2wsh_script = bytes([0x00, 0x20]) + bytes(32)  # OP_0 <32-byte hash>
    result = script_to_address(p2wsh_script)
    # Should be a valid bech32 P2WSH mainnet address starting with 'bc1q'
    assert result.startswith("bc1q")


def test_verify_transaction_exception_handling():
    """
    Test that exceptions during verification are caught and return error.
    """
    our_utxos = {
        ("abc123", 0): UTXOInfo(
            txid="abc123",
            vout=0,
            value=100_000_000,
            address="bcrt1qtest1",
            confirmations=10,
            scriptpubkey="",
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
    }

    with patch("maker.tx_verification.parse_transaction", side_effect=Exception("Test error")):
        is_valid, error = verify_unsigned_transaction(
            tx_hex="dummy",
            our_utxos=our_utxos,
            cj_address="bcrt1qcj",
            change_address="bcrt1qchange",
            amount=50_000_000,
            cjfee=10_000,
            txfee=1000,
            offer_type=OfferType.SW0_ABSOLUTE,
        )

    assert not is_valid
    assert "Verification error" in error


class TestParseTransactionOutputValueRange:
    """Regression tests for output value range validation (MAX_MONEY / negative).

    Bitcoin Core treats output values as signed 64-bit integers and rejects
    values < 0 or > MAX_MONEY (21M BTC = 2,100,000,000,000,000 sats).
    Our parser must match this behavior to avoid logic mismatches with the
    reference implementation, which could allow a malicious taker to craft
    transactions that our maker interprets differently from the network.
    """

    def test_reject_output_value_exceeding_max_money(self) -> None:
        """Output value of 21,000,001 BTC exceeds MAX_MONEY and must be rejected."""
        # Non-segwit tx: 1 input (all zeros), 1 output with value = 21_000_001 * 1e8 sats
        tx_hex = (
            "01000000"  # version 1
            "01"  # 1 input
            "0000000000000000000000000000000000000000000000000000000000000000"  # txid
            "00000000"  # vout 0
            "00"  # empty scriptsig
            "ffffffff"  # sequence
            "01"  # 1 output
            "0021fd5ff0750700"  # value = 2_100_000_100_000_000 sats (> MAX_MONEY)
            "160014"
            "0000000000000000000000000000000000000000"  # P2WPKH script
            "00000000"  # locktime
        )
        result = parse_transaction(tx_hex, network="mainnet")
        assert result is None

    def test_reject_negative_output_value(self) -> None:
        """Output value with bit 63 set is negative as signed int64 and must be rejected."""
        # Non-segwit tx: 1 input (all zeros), 1 output with value = -(2^63 - 1)
        tx_hex = (
            "01000000"  # version 1
            "01"  # 1 input
            "0000000000000000000000000000000000000000000000000000000000000000"  # txid
            "00000000"  # vout 0
            "00"  # empty scriptsig
            "ffffffff"  # sequence
            "01"  # 1 output
            "0100000000000080"  # value = -9223372036854775807 (signed)
            "160014"
            "0000000000000000000000000000000000000000"  # P2WPKH script
            "00000000"  # locktime
        )
        result = parse_transaction(tx_hex, network="mainnet")
        assert result is None

    def test_accept_valid_output_value(self) -> None:
        """Output value of 1 BTC (100,000,000 sats) is valid and must parse."""
        tx_hex = (
            "01000000"  # version 1
            "01"  # 1 input
            "0000000000000000000000000000000000000000000000000000000000000000"  # txid
            "00000000"  # vout 0
            "00"  # empty scriptsig
            "ffffffff"  # sequence
            "01"  # 1 output
            "00e1f50500000000"  # value = 100_000_000 sats (1 BTC)
            "160014"
            "0000000000000000000000000000000000000000"  # P2WPKH script
            "00000000"  # locktime
        )
        result = parse_transaction(tx_hex, network="mainnet")
        assert result is not None
        assert result["outputs"][0]["value"] == 100_000_000

    def test_accept_max_money_boundary(self) -> None:
        """Output value exactly at MAX_MONEY (21M BTC) should be accepted."""
        import struct

        max_money = 21_000_000 * 100_000_000
        value_hex = struct.pack("<q", max_money).hex()
        tx_hex = (
            "01000000"
            "01"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "00000000"
            "00"
            "ffffffff"
            "01" + value_hex + "160014"
            "0000000000000000000000000000000000000000"
            "00000000"
        )
        result = parse_transaction(tx_hex, network="mainnet")
        assert result is not None
        assert result["outputs"][0]["value"] == max_money

    def test_reject_max_money_plus_one(self) -> None:
        """Output value one sat above MAX_MONEY must be rejected."""
        import struct

        over_max = 21_000_000 * 100_000_000 + 1
        value_hex = struct.pack("<q", over_max).hex()
        tx_hex = (
            "01000000"
            "01"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "00000000"
            "00"
            "ffffffff"
            "01" + value_hex + "160014"
            "0000000000000000000000000000000000000000"
            "00000000"
        )
        result = parse_transaction(tx_hex, network="mainnet")
        assert result is None

    def test_fuzz_crash_input(self) -> None:
        """Regression test for the original fuzzer crash input from PR #421.

        This malformed transaction has a varint-encoded output count of 0xFF
        that shifts the output value field into script data, producing a value
        that exceeds MAX_MONEY when decoded.
        """
        tx_hex = (
            "010000000001010000000000000000000000000000000000000001000000"
            "660000000000000000000000000000ffffffff0140420f000000008616"
            "001475420f00000000b6240500a89d7b4c48398a6f3b0021fc0000"
        )
        result = parse_transaction(tx_hex, network="mainnet")
        assert result is None


# ---------------------------------------------------------------------------
# Regression tests for transaction parser leniency (fuzzer-found crashes)
# ---------------------------------------------------------------------------


class TestParserLeniencyRegression:
    """Regression tests for structural leniency bugs found via fuzzing.

    These verify that malformed transaction hex inputs are properly
    rejected (return None) rather than silently accepted.
    """

    def test_non_standard_version(self) -> None:
        """Version 0x00B70000 must be rejected (only 1, 2, 3 are valid)."""
        tx_hex = "0000b7000000"
        result = parse_transaction(tx_hex, network=NetworkType.MAINNET)
        assert result is None

    def test_accepts_v3_truc_version(self) -> None:
        """Version 3 SegWit transactions (TRUC policy) should be accepted."""
        tx_hex = (
            "03000000"  # version 3
            "0001"  # segwit marker+flag
            "01"  # 1 input
            "0000000000000000000000000000000000000000000000000000000000000001"  # txid
            "00000000"  # vout
            "00"  # scriptsig len
            "ffffffff"  # sequence
            "01"  # 1 output
            "00e1f50500000000"  # value
            "16"  # script len
            "0014751e76e8199196d454941c45d1b3a323f1433bd6"  # P2WPKH
            "00"  # witness stack size (empty)
            "00000000"  # locktime
        )
        result = parse_transaction(tx_hex, network=NetworkType.MAINNET)
        assert result is not None
        assert len(result["inputs"]) == 1
        assert len(result["outputs"]) == 1

    def test_truncated_witness(self) -> None:
        """SegWit tx where nLockTime residue is 3 bytes (not 4) must be rejected.

        From fuzzer crash-1f46fc92: witness has 0 items but only 3 bytes
        remain after, not the required 4 for nLockTime.
        """
        tx_hex = (
            "01000000"  # version 1
            "0001"  # segwit marker+flag
            "01"  # 1 input
            "00000000000000000000000000000000"
            "00000100000000000000000000000000"  # txid
            "00000000"  # vout
            "00"  # empty scriptsig
            "ffffffff"  # sequence
            "01"  # 1 output
            "40420f0000000000"  # value (1M sats)
            "16"  # script len 22
            "0014751e76e0a152d5b6100500a89d7b4c48398a6f3b"  # P2WPKH
            "00"  # witness: 0 stack items
            "fc0000"  # only 3 bytes remain (need 4 for nLockTime)
        )
        result = parse_transaction(tx_hex, network=NetworkType.MAINNET)
        assert result is None

    def test_zero_inputs(self) -> None:
        """Transaction with zero inputs must be rejected."""
        tx_hex = "01000000000000000000"
        result = parse_transaction(tx_hex)
        assert result is None

    def test_trailing_garbage(self) -> None:
        """Transaction with extra bytes after nLockTime must be rejected.

        From fuzzer crash-5bfbca51: valid structure but 7 trailing bytes
        remain where only 4 (nLockTime) are expected.
        """
        tx_hex = (
            "01000000"  # version 1
            "0001"  # segwit marker+flag
            "01"  # 1 input
            "00000000000000000000000000000000"
            "00000060000000660000000000000000"  # txid
            "00000000"  # vout
            "00"  # empty scriptsig
            "ffffffff"  # sequence
            "01"  # 1 output
            "40420f0000000000"  # value
            "16"  # script len 22
            "001475420f00000000b6100500a89d7b4c48348a6f3b"  # P2WPKH
            "00"  # witness: 0 stack items
            "fc4e4e4e4e0000"  # 7 bytes remain (need exactly 4 for nLockTime)
        )
        result = parse_transaction(tx_hex, network=NetworkType.MAINNET)
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
