"""Test fidelity bond protocol compatibility with reference implementation."""

import base64
import struct
from unittest.mock import AsyncMock, Mock

import pytest
from coincurve import PrivateKey

from maker.fidelity import FidelityBondInfo, create_fidelity_bond_proof


def test_bond_proof_format_matches_reference():
    """Verify our bond proof matches the reference implementation format.

    The reference implementation expects a 252-byte proof:
    - 72 bytes: Nick signature (DER, padded with 0xff)
    - 72 bytes: Certificate signature (DER, padded with 0xff)
    - 33 bytes: Certificate pubkey
    - 2 bytes: Certificate expiry (blocks / 2016)
    - 33 bytes: UTXO pubkey
    - 32 bytes: TXID (little-endian)
    - 4 bytes: Vout (little-endian)
    - 4 bytes: Locktime (little-endian)
    """
    # Create a test bond
    privkey = PrivateKey()
    pubkey = privkey.public_key.format(compressed=True)

    bond = FidelityBondInfo(
        txid="a" * 64,  # 32 bytes in hex
        vout=0,
        value=100_000_000,  # 1 BTC
        locktime=4102444800,  # Far future
        confirmation_time=1704067200,  # Jan 1, 2024
        bond_value=1000,
        pubkey=pubkey,
        private_key=privkey,
    )

    proof = create_fidelity_bond_proof(
        bond=bond,
        maker_nick="J5TestMakerNick1",
        taker_nick="J5TestTakerNick1",
        current_block_height=930000,  # Current block height for cert expiry calculation
    )

    assert proof is not None, "Proof creation should succeed"

    # Decode and verify structure
    decoded = base64.b64decode(proof)
    assert len(decoded) == 252, f"Proof should be 252 bytes, got {len(decoded)}"

    # Unpack using reference format
    unpacked = struct.unpack("<72s72s33sH33s32sII", decoded)

    nick_sig, cert_sig, cert_pub, cert_expiry, utxo_pub, txid_bytes, vout, locktime = unpacked

    # Verify cert_expiry encoding
    # Formula: ((block_height + 2) // 2016) + 1
    expected_cert_expiry = ((930000 + 2) // 2016) + 1
    assert cert_expiry == expected_cert_expiry, (
        f"Cert expiry should be {expected_cert_expiry}, got {cert_expiry}"
    )

    # Verify pubkeys: cert_pub is random (delegated), utxo_pub matches bond pubkey
    assert cert_pub != pubkey, "Cert pubkey should be a random ephemeral key"
    assert utxo_pub == pubkey, "UTXO pubkey should match bond pubkey"

    # Verify UTXO data - TXID in display format (big-endian)
    assert txid_bytes == bytes.fromhex(bond.txid), "TXID should match (display format)"
    assert vout == bond.vout, "Vout should match"
    assert locktime == bond.locktime, "Locktime should match"

    # Verify signatures have DER headers (0x30)
    # Signatures are padded with 0xff, so we need to find the DER header
    assert b"\x30" in nick_sig, "Nick signature should contain DER header"
    assert b"\x30" in cert_sig, "Cert signature should contain DER header"


@pytest.mark.asyncio
async def test_bond_sent_only_in_privmsg_response():
    """Verify bonds are only sent in PRIVMSG responses to orderbook requests.

    Protocol flow:
    1. Taker broadcasts: !orderbook
    2. Maker responds via PRIVMSG: !sw0reloffer <params>!tbond <proof>
    3. Taker parses bond from the PRIVMSG response

    The public broadcast should NOT include the bond.
    """
    from jmcore.models import Offer, OfferType

    from maker.bot import MakerBot

    # Create a minimal maker bot with a mock directory client
    mock_wallet = Mock()
    mock_backend = Mock()
    mock_config = Mock()
    mock_config.data_dir = "/tmp/test"
    # Add rate limiter config to prevent validation errors
    mock_config.message_rate_limit = 10
    mock_config.message_burst_limit = 100
    mock_config.orderbook_rate_limit = 1
    mock_config.orderbook_rate_interval = 60
    mock_config.orderbook_violation_ban_threshold = 5
    mock_config.orderbook_violation_warning_threshold = 3
    mock_config.orderbook_violation_severe_threshold = 10
    mock_config.orderbook_ban_duration = 3600
    # Directory pool plumbing
    mock_config.directory_servers = []
    mock_config.stream_isolation = False
    mock_config.socks_host = "127.0.0.1"
    mock_config.socks_port = 9050
    mock_config.connection_timeout = 120.0
    mock_config.onion_host = None
    mock_config.onion_serving_port = 5222

    maker = MakerBot(mock_wallet, mock_backend, mock_config)

    # Create a fidelity bond
    privkey = PrivateKey()
    pubkey = privkey.public_key.format(compressed=True)
    maker.fidelity_bond = FidelityBondInfo(
        txid="a" * 64,
        vout=0,
        value=100_000_000,
        locktime=4102444800,
        confirmation_time=1704067200,
        bond_value=1000,
        pubkey=pubkey,
        private_key=privkey,
    )

    # Create a test offer
    maker.current_offers = [
        Offer(
            counterparty=maker.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=1_000_000,
            maxsize=10_000_000,
            txfee=500,
            cjfee="0.0003",
        )
    ]

    # Mock directory client
    mock_client = AsyncMock()
    maker.directory_clients["test"] = mock_client

    # Test 1: Public announcement should NOT include bond
    await maker._announce_offers()
    mock_client.send_public_message.assert_called_once()
    public_msg = mock_client.send_public_message.call_args[0][0]
    assert "!tbond" not in public_msg, "Public broadcast should NOT contain !tbond"

    # Test 2: PRIVMSG response should include bond with taker-specific signature
    mock_client.reset_mock()
    await maker._send_offers_to_taker("J5TestTakerNick1")
    mock_client.send_private_message.assert_called_once()

    # Extract the call arguments
    call_args = mock_client.send_private_message.call_args
    taker_nick = call_args[0][0]
    command = call_args[0][1]
    data = call_args[0][2]

    assert taker_nick == "J5TestTakerNick1"
    assert command == "sw0reloffer"
    assert "!tbond" in data, "PRIVMSG should contain !tbond"

    # Verify the bond proof is valid and signed for the taker
    bond_start = data.find("!tbond ") + 7
    bond_proof_b64 = data[bond_start:].split()[0]

    # Decode and verify it's 252 bytes
    decoded = base64.b64decode(bond_proof_b64)
    assert len(decoded) == 252, f"Bond proof should be 252 bytes, got {len(decoded)}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
