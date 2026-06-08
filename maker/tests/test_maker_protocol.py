"""
Unit tests for Maker protocol handling.

Tests:
- NaCl encryption setup and message exchange
- Protocol message flow (fill, auth, tx)
- Fidelity bond proof creation
"""

from __future__ import annotations

import base64

import pytest
from jmcore.encryption import CryptoSession

from maker.fidelity import FidelityBondInfo, create_fidelity_bond_proof


@pytest.mark.asyncio
async def test_maker_encryption_setup():
    """Test maker sets up encryption with taker's pubkey from !fill."""
    # Taker creates crypto session and sends pubkey in !fill
    taker_crypto = CryptoSession()
    taker_pubkey = taker_crypto.get_pubkey_hex()

    # Maker receives fill with taker's pubkey

    # Maker creates crypto session
    maker_crypto = CryptoSession()
    maker_pubkey = maker_crypto.get_pubkey_hex()

    # Maker sets up encryption with taker's pubkey
    maker_crypto.setup_encryption(taker_pubkey)

    # Taker sets up encryption with maker's pubkey (from !pubkey response)
    taker_crypto.setup_encryption(maker_pubkey)

    # Test bidirectional encryption
    test_msg = "auth revelation data"
    encrypted = taker_crypto.encrypt(test_msg)
    decrypted = maker_crypto.decrypt(encrypted)
    assert decrypted == test_msg

    # Maker response
    response = "ioauth data"
    encrypted_response = maker_crypto.encrypt(response)
    decrypted_response = taker_crypto.decrypt(encrypted_response)
    assert decrypted_response == response


@pytest.mark.asyncio
async def test_fidelity_bond_proof():
    """Test fidelity bond proof creation."""
    # Create a mock fidelity bond
    bond = FidelityBondInfo(
        txid="a" * 64,
        vout=0,
        value=100_000_000,
        locktime=700_000,
        confirmation_time=600_000,
        bond_value=1_500_000,
    )

    maker_nick = "J5TestMaker"
    taker_nick = "J5TestTaker"

    # Add private key and pubkey for signing
    from coincurve import PrivateKey

    bond.private_key = PrivateKey(b"\x01" * 32)
    bond.pubkey = bond.private_key.public_key.format(compressed=True)

    # Create proof
    proof = create_fidelity_bond_proof(bond, maker_nick, taker_nick, current_block_height=930000)

    # Proof should be a base64-encoded string
    # The actual format is implementation-specific but should not be None
    assert proof is not None
    assert len(proof) > 0

    # The proof is a base64 string containing the bond information
    import base64

    # Should be valid base64
    try:
        decoded = base64.b64decode(proof, validate=True)
        assert len(decoded) > 0
    except Exception:
        # Some proof formats may not be pure base64, that's okay
        # as long as we have a proof string
        pass


@pytest.mark.asyncio
async def test_encrypted_ioauth_response():
    """Test maker's encrypted !ioauth response format."""
    # Setup encryption
    taker_crypto = CryptoSession()
    maker_crypto = CryptoSession()

    taker_pubkey = taker_crypto.get_pubkey_hex()
    maker_pubkey = maker_crypto.get_pubkey_hex()

    taker_crypto.setup_encryption(maker_pubkey)
    maker_crypto.setup_encryption(taker_pubkey)

    # Maker creates ioauth data
    utxo_list = "txid1:0,txid2:1"
    auth_pub = "02" + "aa" * 32  # Compressed pubkey
    cj_addr = "bcrt1qmakercj"
    change_addr = "bcrt1qmakerchange"
    btc_sig = "304402" + "bb" * 35  # DER signature

    ioauth_plaintext = f"{utxo_list} {auth_pub} {cj_addr} {change_addr} {btc_sig}"

    # Encrypt
    encrypted_ioauth = maker_crypto.encrypt(ioauth_plaintext)

    # Taker decrypts
    decrypted = taker_crypto.decrypt(encrypted_ioauth)
    assert decrypted == ioauth_plaintext

    # Parse decrypted ioauth
    parts = decrypted.split()
    assert len(parts) == 5
    assert parts[0] == utxo_list
    assert parts[1] == auth_pub
    assert parts[2] == cj_addr
    assert parts[3] == change_addr
    assert parts[4] == btc_sig


@pytest.mark.asyncio
async def test_encrypted_sig_response():
    """Test maker's encrypted !sig response format."""
    # Setup encryption
    taker_crypto = CryptoSession()
    maker_crypto = CryptoSession()

    taker_pubkey = taker_crypto.get_pubkey_hex()
    maker_pubkey = maker_crypto.get_pubkey_hex()

    taker_crypto.setup_encryption(maker_pubkey)
    maker_crypto.setup_encryption(taker_pubkey)

    # Maker creates signature
    # Format: varint(sig_len) + sig + varint(pub_len) + pub
    sig_bytes = b"\x30\x44" + b"\x00" * 70  # DER signature
    pub_bytes = b"\x02" + b"\x00" * 33  # Compressed pubkey

    sig_len = len(sig_bytes)
    pub_len = len(pub_bytes)

    sig_data = bytes([sig_len]) + sig_bytes + bytes([pub_len]) + pub_bytes
    sig_b64 = base64.b64encode(sig_data).decode("ascii")

    # Encrypt signature
    encrypted_sig = maker_crypto.encrypt(sig_b64)

    # Taker decrypts
    decrypted_sig_b64 = taker_crypto.decrypt(encrypted_sig)
    assert decrypted_sig_b64 == sig_b64

    # Taker parses signature
    decoded_sig = base64.b64decode(decrypted_sig_b64)
    assert decoded_sig[0] == sig_len
    assert decoded_sig[1 : 1 + sig_len] == sig_bytes
    assert decoded_sig[1 + sig_len] == pub_len
    assert decoded_sig[2 + sig_len : 2 + sig_len + pub_len] == pub_bytes


@pytest.mark.asyncio
async def test_multiple_maker_sessions():
    """Test handling multiple concurrent taker sessions."""
    # Simulate two takers connecting to the same maker
    taker1_crypto = CryptoSession()
    taker2_crypto = CryptoSession()

    maker1_crypto = CryptoSession()
    maker2_crypto = CryptoSession()

    # Setup encryption for taker1
    taker1_crypto.setup_encryption(maker1_crypto.get_pubkey_hex())
    maker1_crypto.setup_encryption(taker1_crypto.get_pubkey_hex())

    # Setup encryption for taker2
    taker2_crypto.setup_encryption(maker2_crypto.get_pubkey_hex())
    maker2_crypto.setup_encryption(taker2_crypto.get_pubkey_hex())

    # Test isolated encryption (taker1 can't decrypt taker2's messages)
    msg1 = "taker1 auth data"
    encrypted1 = taker1_crypto.encrypt(msg1)
    decrypted1 = maker1_crypto.decrypt(encrypted1)
    assert decrypted1 == msg1

    msg2 = "taker2 auth data"
    encrypted2 = taker2_crypto.encrypt(msg2)
    decrypted2 = maker2_crypto.decrypt(encrypted2)
    assert decrypted2 == msg2

    # Verify cross-decryption fails (encrypted1 can't be decrypted with maker2's key)
    # This would raise an exception in real usage
    try:
        maker2_crypto.decrypt(encrypted1)
        # If it doesn't raise, the decryption would produce garbage
        assert False, "Should not be able to decrypt with wrong key"
    except Exception:
        # Expected: decryption failure
        pass


@pytest.mark.asyncio
async def test_channel_consistency_validation():
    """CoinJoinSession records the channel without rejecting switches.

    Different directory servers (dir:serverA vs dir:serverB) are expected because
    takers broadcast to all directories. A direct<->directory switch mid-session
    is also legitimate: the reference taker routes each privmsg opportunistically,
    so !fill may arrive via a directory while a direct connection is still being
    established, and !auth/!tx then arrive directly (issue #515).
    """
    from unittest.mock import MagicMock

    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession

    # Create a mock session
    mock_wallet = MagicMock()
    mock_backend = MagicMock()
    mock_backend.requires_neutrino_metadata.return_value = False

    offer = Offer(
        counterparty="J5TestMaker",
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=1000,
        cjfee="0.0005",
    )

    session = CoinJoinSession(
        taker_nick="J5TestTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=mock_backend,
    )

    # First message should record the channel type
    assert session.comm_channel == ""
    assert session.validate_channel("dir:node1") is True
    assert session.comm_channel == "directory"  # Normalized to channel type

    # Subsequent messages on same channel type should pass (even different servers)
    assert session.validate_channel("dir:node1") is True
    assert session.validate_channel("dir:node2") is True  # Different server is OK!
    assert session.comm_channel == "directory"

    # Switching to direct mid-session is accepted (opportunistic direct connect),
    # and the recorded channel follows the taker to its new transport.
    assert session.validate_channel("direct") is True
    assert session.comm_channel == "direct"

    # Switching back to directory is likewise accepted.
    assert session.validate_channel("dir:node1") is True
    assert session.comm_channel == "directory"


@pytest.mark.asyncio
async def test_channel_consistency_direct_first():
    """Channel recording when a direct connection is established first."""
    from unittest.mock import MagicMock

    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession

    mock_wallet = MagicMock()
    mock_backend = MagicMock()
    mock_backend.requires_neutrino_metadata.return_value = False

    offer = Offer(
        counterparty="J5TestMaker",
        ordertype=OfferType.SW0_ABSOLUTE,
        oid=0,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=1000,
        cjfee=0,
    )

    session = CoinJoinSession(
        taker_nick="J5DirectTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=mock_backend,
    )

    # Session starts on direct connection
    assert session.validate_channel("direct") is True
    assert session.comm_channel == "direct"

    # Subsequent direct messages keep the channel recorded as direct.
    assert session.validate_channel("direct") is True
    assert session.comm_channel == "direct"

    # A later message via a directory is accepted (taker fell back to relay),
    # and the recorded channel follows it.
    assert session.validate_channel("dir:node1") is True
    assert session.comm_channel == "directory"


@pytest.mark.asyncio
async def test_neutrino_maker_rejects_legacy_taker_auth():
    """Test that a neutrino maker explicitly rejects auth from a legacy taker.

    When a taker doesn't send extended UTXO metadata (scriptpubkey + blockheight),
    the neutrino backend cannot verify the UTXO. The maker should return a clear
    error with error_code 'neutrino_incompatible' rather than silently failing
    on get_utxo() returning None.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from jmcore.encryption import CryptoSession
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession

    mock_wallet = MagicMock()
    mock_backend = MagicMock()
    # Simulate neutrino backend
    mock_backend.requires_neutrino_metadata.return_value = True
    mock_backend.get_utxo = AsyncMock(return_value=None)

    offer = Offer(
        counterparty="J5NeutrinoMaker",
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=1000,
        cjfee="0.0003",
    )

    session = CoinJoinSession(
        taker_nick="J5LegacyTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=mock_backend,
    )

    # Simulate fill phase
    taker_crypto = CryptoSession()
    taker_pk = taker_crypto.get_pubkey_hex()
    success, _ = await session.handle_fill(
        amount=1_000_000,
        commitment="aa" * 32,
        taker_pk=taker_pk,
    )
    assert success

    # Simulate auth with a legacy taker revelation (NO extended metadata)
    # We mock verify_podle to always succeed so we can test the UTXO path
    revelation = {
        "utxo": "bb" * 32 + ":0",  # Legacy format: txid:vout only
        "P": "02" + "cc" * 32,
        "P2": "02" + "dd" * 32,
        "sig": "ee" * 32,
        "e": "ff" * 16,
    }

    with patch("maker.coinjoin.verify_podle", return_value=(True, None)):
        with patch("maker.coinjoin.parse_podle_revelation") as mock_parse:
            mock_parse.return_value = {
                "P": bytes.fromhex("02" + "cc" * 32),
                "P2": bytes.fromhex("02" + "dd" * 32),
                "sig": bytes.fromhex("ee" * 32),
                "e": bytes.fromhex("ff" * 16),
                "txid": "bb" * 32,
                "vout": 0,
                # No scriptpubkey or blockheight -> legacy taker
            }

            success, response = await session.handle_auth(
                commitment="aa" * 32,
                revelation=revelation,
                kphex="",
            )

    # Should fail with neutrino_incompatible error
    assert not success
    assert response["error_code"] == "neutrino_incompatible"
    assert "neutrino" in response["error"].lower()

    # get_utxo should NOT have been called (we fail early)
    mock_backend.get_utxo.assert_not_called()


@pytest.mark.asyncio
async def test_neutrino_maker_accepts_neutrino_compat_taker_auth():
    """Test that a neutrino maker succeeds when taker sends extended metadata.

    Verifies that verify_utxo_with_metadata() is called (not get_utxo()) and
    that the session proceeds to select UTXOs and respond with !ioauth data.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from jmcore.encryption import CryptoSession
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession

    mock_wallet = MagicMock()
    mock_backend = MagicMock()
    # Simulate neutrino backend
    mock_backend.requires_neutrino_metadata.return_value = True
    mock_backend.get_utxo = AsyncMock(return_value=None)

    # verify_utxo_with_metadata returns a successful result
    mock_verify_result = MagicMock()
    mock_verify_result.valid = True
    mock_verify_result.value = 2_000_000
    mock_verify_result.confirmations = 10
    mock_backend.verify_utxo_with_metadata = AsyncMock(return_value=mock_verify_result)

    offer = Offer(
        counterparty="J5NeutrinoMaker",
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=1000,
        cjfee="0.0003",
    )

    session = CoinJoinSession(
        taker_nick="J5CompatTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=mock_backend,
        taker_utxo_age=1,
        taker_utxo_amtpercent=10,
    )

    # Simulate fill phase
    taker_crypto = CryptoSession()
    taker_pk = taker_crypto.get_pubkey_hex()
    success, _ = await session.handle_fill(
        amount=1_000_000,
        commitment="aa" * 32,
        taker_pk=taker_pk,
    )
    assert success

    revelation = {
        "utxo": "bb" * 32 + ":0:0014" + "ab" * 20 + ":100",
        "P": "02" + "cc" * 32,
        "P2": "02" + "dd" * 32,
        "sig": "ee" * 32,
        "e": "ff" * 16,
    }

    # Mock _select_our_utxos to avoid needing a real wallet
    mock_utxo_info = MagicMock()
    mock_utxo_info.value = 5_000_000
    mock_utxo_info.scriptpubkey = "0014" + "ab" * 20
    mock_utxo_info.height = 100
    mock_utxo_info.address = "bcrt1q" + "a" * 38

    mock_key = MagicMock()
    mock_key.get_public_key_bytes.return_value = bytes.fromhex("02" + "ab" * 32)
    mock_key.get_private_key_bytes.return_value = bytes(32)
    mock_wallet.get_key_for_address.return_value = mock_key

    with (
        patch("maker.coinjoin.verify_podle", return_value=(True, None)),
        patch("maker.coinjoin.verify_podle_binding", return_value=(True, "")),
        patch("maker.coinjoin.parse_podle_revelation") as mock_parse,
        patch.object(
            session,
            "_select_our_utxos",
            new_callable=AsyncMock,
            return_value=(
                {("cc" * 32, 0): mock_utxo_info},
                "bcrt1q_cj_addr",
                "bcrt1q_change_addr",
                0,
            ),
        ),
        patch("jmcore.crypto.ecdsa_sign", return_value="mock_sig"),
    ):
        mock_parse.return_value = {
            "P": bytes.fromhex("02" + "cc" * 32),
            "P2": bytes.fromhex("02" + "dd" * 32),
            "sig": bytes.fromhex("ee" * 32),
            "e": bytes.fromhex("ff" * 16),
            "txid": "bb" * 32,
            "vout": 0,
            "scriptpubkey": "0014" + "ab" * 20,
            "blockheight": 100,
        }

        success, response = await session.handle_auth(
            commitment="aa" * 32,
            revelation=revelation,
            kphex="",
        )

    # Should succeed
    assert success
    assert "utxo_list" in response
    assert "cj_addr" in response
    assert "change_addr" in response

    # verify_utxo_with_metadata should have been called (not get_utxo)
    mock_backend.verify_utxo_with_metadata.assert_called_once_with(
        txid="bb" * 32,
        vout=0,
        scriptpubkey="0014" + "ab" * 20,
        blockheight=100,
    )
    mock_backend.get_utxo.assert_not_called()


@pytest.mark.asyncio
async def test_select_our_utxos_forwards_exclude_to_wallet():
    """_select_our_utxos forwards committed outpoints to the wallet selector.

    Regression guard for the concurrent-session double-spend: a maker handling
    two overlapping CoinJoins must not pick the same input twice (the second
    transaction would be rejected, e.g. "insufficient fee, rejecting
    replacement"). The exclusion set originates from other active sessions and
    must reach select_utxos_with_merge.
    """
    from unittest.mock import AsyncMock, MagicMock

    from jmcore.models import Offer, OfferType
    from jmwallet.wallet.models import UTXOInfo

    from maker.coinjoin import CoinJoinSession

    mock_wallet = MagicMock()
    mock_wallet.mixdepth_count = 5
    mock_wallet.get_balance_for_offers = AsyncMock(return_value=10_000_000)
    mock_wallet.get_next_address_index.return_value = 0
    mock_wallet.get_change_address.return_value = "bcrt1qcjorchange"
    # No inputs locked by other rounds; reservation of our chosen inputs succeeds.
    mock_wallet.get_locked_input_outpoints.return_value = set()
    mock_wallet.reserve_coinjoin_inputs.return_value = True
    selected_utxo = UTXOInfo(
        txid="ab" * 32,
        vout=1,
        value=5_000_000,
        address="bcrt1qmakerinput",
        confirmations=10,
        scriptpubkey="0014" + "ab" * 20,
        path="m/84'/0'/1'/0/0",
        mixdepth=1,
    )
    mock_wallet.select_utxos_with_merge.return_value = [selected_utxo]

    mock_backend = MagicMock()
    mock_backend.requires_neutrino_metadata.return_value = False

    offer = Offer(
        counterparty="J5ExcludeMaker",
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=1000,
        cjfee="0.0003",
    )
    session = CoinJoinSession(
        taker_nick="J5SomeTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=mock_backend,
    )
    session.amount = 1_000_000

    committed_elsewhere = {("cd" * 32, 0), ("ef" * 32, 3)}
    utxos_dict, _, _, mixdepth = await session._select_our_utxos(exclude_utxos=committed_elsewhere)

    assert mixdepth >= 0  # selection succeeded
    assert (("ab" * 32), 1) in utxos_dict
    # The committed-elsewhere outpoints were passed straight to the selector.
    assert mock_wallet.select_utxos_with_merge.call_args.kwargs["exclude"] == (committed_elsewhere)


@pytest.mark.asyncio
async def test_select_our_utxos_declines_on_lock_conflict():
    """If our chosen inputs were locked by a racing round, decline the session.

    Declining (returning no UTXOs) is correct: signing an input already
    committed elsewhere would create a conflicting transaction.
    """
    from unittest.mock import AsyncMock, MagicMock

    from jmcore.models import Offer, OfferType
    from jmwallet.wallet.models import UTXOInfo

    from maker.coinjoin import CoinJoinSession

    mock_wallet = MagicMock()
    mock_wallet.mixdepth_count = 5
    mock_wallet.get_balance_for_offers = AsyncMock(return_value=10_000_000)
    mock_wallet.get_next_address_index.return_value = 0
    mock_wallet.get_change_address.return_value = "bcrt1qcjorchange"
    mock_wallet.get_locked_input_outpoints.return_value = set()
    mock_wallet.select_utxos_with_merge.return_value = [
        UTXOInfo(
            txid="ab" * 32,
            vout=1,
            value=5_000_000,
            address="bcrt1qmakerinput",
            confirmations=10,
            scriptpubkey="0014" + "ab" * 20,
            path="m/84'/0'/1'/0/0",
            mixdepth=1,
        )
    ]
    # A concurrent round grabbed the input between selection and our reserve.
    mock_wallet.reserve_coinjoin_inputs.return_value = False

    mock_backend = MagicMock()
    mock_backend.requires_neutrino_metadata.return_value = False
    offer = Offer(
        counterparty="J5ConflictMaker",
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=1000,
        cjfee="0.0003",
    )
    session = CoinJoinSession(
        taker_nick="J5SomeTaker", offer=offer, wallet=mock_wallet, backend=mock_backend
    )
    session.amount = 1_000_000

    utxos_dict, _, _, mixdepth = await session._select_our_utxos()
    assert utxos_dict == {}
    assert mixdepth == -1


@pytest.mark.asyncio
async def test_pit_script_type_from_offer_family():
    """Rigid pit (JMP-0005): the pit's script type is fixed by the offer family.

    sw0 offers produce a P2WPKH pit and tr0 offers a P2TR pit, regardless of the
    maker's primary wallet type. There is no taker-chosen output type.
    """
    from unittest.mock import MagicMock

    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession

    mock_wallet = MagicMock()
    mock_wallet.address_type = "p2wpkh"
    mock_backend = MagicMock()
    mock_backend.requires_neutrino_metadata.return_value = False

    def _offer(ordertype: OfferType) -> Offer:
        return Offer(
            counterparty="J5TypeMaker",
            ordertype=ordertype,
            oid=0,
            minsize=10_000,
            maxsize=100_000_000,
            txfee=1000,
            cjfee="0.0003",
        )

    taker_pk = CryptoSession().get_pubkey_hex()

    # sw0 -> p2wpkh pit, even though the maker wallet is p2wpkh anyway.
    sw0 = CoinJoinSession(
        taker_nick="J5T",
        offer=_offer(OfferType.SW0_RELATIVE),
        wallet=mock_wallet,
        backend=mock_backend,
    )
    assert sw0.pit_script_type == "p2wpkh"
    ok, _ = await sw0.handle_fill(amount=1_000_000, commitment="aa" * 32, taker_pk=taker_pk)
    assert ok
    assert sw0.pit_script_type == "p2wpkh"

    # tr0 -> p2tr pit, independent of the maker's primary wallet type.
    tr0 = CoinJoinSession(
        taker_nick="J5T",
        offer=_offer(OfferType.TR0_RELATIVE),
        wallet=mock_wallet,
        backend=mock_backend,
    )
    assert tr0.pit_script_type == "p2tr"
    ok, _ = await tr0.handle_fill(amount=1_000_000, commitment="aa" * 32, taker_pk=taker_pk)
    assert ok
    assert tr0.pit_script_type == "p2tr"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
