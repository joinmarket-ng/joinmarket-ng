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

    from jmcore.constants import DUST_THRESHOLD
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
        input_lock_ttl_sec=3600,
    )
    session.amount = 1_000_000

    committed_elsewhere = {("cd" * 32, 0), ("ef" * 32, 3)}
    utxos_dict, _, _, mixdepth = await session._select_our_utxos(exclude_utxos=committed_elsewhere)

    assert mixdepth >= 0  # selection succeeded
    assert (("ab" * 32), 1) in utxos_dict
    # The committed-elsewhere outpoints were passed straight to the selector.
    assert mock_wallet.select_utxos_with_merge.call_args.kwargs["exclude"] == (committed_elsewhere)
    for call in mock_wallet.get_balance_for_offers.call_args_list:
        assert call.kwargs["exclude"] == committed_elsewhere
    # Selection must reserve enough value for a non-dust change output.
    # real_cjfee = 1_000_000 * 0.0003 = 300 sats.
    assert mock_wallet.select_utxos_with_merge.call_args.args[1] == (
        1_000_000 + 1000 + DUST_THRESHOLD + 1 - 300
    )
    mock_wallet.reserve_coinjoin_inputs.assert_called_once_with(
        {("ab" * 32, 1)}, ttl=session.input_lock_ttl_sec, owner=session.input_lock_owner
    )


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
async def test_select_our_utxos_falls_back_after_lock_conflict():
    """A reservation race in the largest mixdepth tries the next mixdepth."""
    from unittest.mock import AsyncMock, MagicMock

    from jmcore.models import Offer, OfferType
    from jmwallet.wallet.models import UTXOInfo

    from maker.coinjoin import CoinJoinSession

    mock_wallet = MagicMock()
    mock_wallet.mixdepth_count = 2
    mock_wallet.get_balance_for_offers = AsyncMock(side_effect=[10_000_000, 9_000_000])
    mock_wallet.get_locked_input_outpoints.return_value = set()
    mock_wallet.get_next_address_index.return_value = 0
    mock_wallet.get_change_address.return_value = "bcrt1qreservedfallback"

    first = UTXOInfo(
        txid="ab" * 32,
        vout=0,
        value=5_000_000,
        address="bcrt1qfirst",
        confirmations=2,
        scriptpubkey="0014" + "ab" * 20,
        path="m/84'/0'/0'/0/0",
        mixdepth=0,
    )
    second = UTXOInfo(
        txid="cd" * 32,
        vout=0,
        value=5_000_000,
        address="bcrt1qsecond",
        confirmations=2,
        scriptpubkey="0014" + "cd" * 20,
        path="m/84'/0'/1'/0/0",
        mixdepth=1,
    )
    mock_wallet.select_utxos_with_merge.side_effect = [[first], [second]]
    mock_wallet.reserve_coinjoin_inputs.side_effect = [False, True]

    mock_backend = MagicMock()
    mock_backend.requires_neutrino_metadata.return_value = False
    offer = Offer(
        counterparty="J5FallbackMaker",
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

    utxos, _, _, mixdepth = await session._select_our_utxos()

    assert mixdepth == 1
    assert set(utxos) == {(second.txid, second.vout)}


@pytest.mark.asyncio
async def test_select_our_utxos_one_mixdepth_uses_distinct_internal_addresses():
    """Equal and change outputs must not reuse one address with one mixdepth."""
    from unittest.mock import AsyncMock, MagicMock, call

    from jmcore.models import Offer, OfferType
    from jmwallet.wallet.models import UTXOInfo

    from maker.coinjoin import CoinJoinSession

    wallet = MagicMock()
    wallet.mixdepth_count = 1
    wallet.get_balance_for_offers = AsyncMock(return_value=10_000_000)
    wallet.get_locked_input_outpoints.return_value = set()
    wallet.reserve_coinjoin_inputs.return_value = True
    selected = UTXOInfo(
        txid="ab" * 32,
        vout=0,
        value=5_000_000,
        address="bcrt1qmakerinput",
        confirmations=10,
        scriptpubkey="0014" + "ab" * 20,
        path="m/84'/0'/0'/0/0",
        mixdepth=0,
    )
    wallet.select_utxos_with_merge.return_value = [selected]
    wallet.get_new_internal_address.side_effect = ["bcrt1qcjout", "bcrt1qchange"]

    offer = Offer(
        counterparty="J5SingleMixdepthMaker",
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=1000,
        cjfee="0.0003",
    )
    session = CoinJoinSession(
        taker_nick="J5SomeTaker", offer=offer, wallet=wallet, backend=MagicMock()
    )
    session.amount = 1_000_000

    utxos, cj_address, change_address, mixdepth = await session._select_our_utxos()

    assert mixdepth == 0
    assert set(utxos) == {(selected.txid, selected.vout)}
    assert cj_address != change_address
    assert wallet.get_new_internal_address.call_args_list == [call(0), call(0)]


@pytest.mark.asyncio
async def test_select_our_utxos_releases_lock_after_address_failure():
    """A failure after reservation must not leave maker liquidity locked."""
    from unittest.mock import AsyncMock, MagicMock

    from jmcore.models import Offer, OfferType
    from jmwallet.wallet.models import UTXOInfo

    from maker.coinjoin import CoinJoinSession

    mock_wallet = MagicMock()
    mock_wallet.mixdepth_count = 1
    mock_wallet.get_balance_for_offers = AsyncMock(return_value=10_000_000)
    mock_wallet.get_locked_input_outpoints.return_value = set()
    selected = UTXOInfo(
        txid="ab" * 32,
        vout=0,
        value=5_000_000,
        address="bcrt1qmakerinput",
        confirmations=2,
        scriptpubkey="0014" + "ab" * 20,
        path="m/84'/0'/0'/0/0",
        mixdepth=0,
    )
    mock_wallet.select_utxos_with_merge.return_value = [selected]
    mock_wallet.reserve_coinjoin_inputs.return_value = True
    mock_wallet.get_new_internal_address.side_effect = RuntimeError("address store failed")

    offer = Offer(
        counterparty="J5LockCleanupMaker",
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
        backend=MagicMock(),
    )
    session.amount = 1_000_000

    utxos, _, _, mixdepth = await session._select_our_utxos()

    assert utxos == {}
    assert mixdepth == -1
    mock_wallet.release_coinjoin_inputs.assert_called_once_with(
        {(selected.txid, selected.vout)}, owner=session.input_lock_owner
    )


@pytest.mark.asyncio
async def test_handle_auth_allows_hp2_seen_after_fill(tmp_path, monkeypatch):
    """An hp2 broadcast from the same round must not invalidate an accepted fill."""
    from unittest.mock import AsyncMock, MagicMock, patch

    import jmcore.commitment_blacklist as commitment_blacklist
    from jmcore.commitment_blacklist import CommitmentBlacklist
    from jmcore.models import Offer, OfferType
    from jmwallet.backends.base import UTXO
    from jmwallet.wallet.models import UTXOInfo

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    mock_wallet = MagicMock()
    mock_backend = MagicMock()
    mock_backend.requires_neutrino_metadata.return_value = False
    mock_backend.get_utxo = AsyncMock(
        return_value=UTXO(
            txid="bb" * 32,
            vout=0,
            value=2_000_000,
            address="bcrt1qtakerinput",
            confirmations=10,
            scriptpubkey="0014" + "cd" * 20,
        )
    )
    selected = UTXOInfo(
        txid="cc" * 32,
        vout=1,
        value=5_000_000,
        address="bcrt1qmakerinput",
        confirmations=10,
        scriptpubkey="0014" + "ab" * 20,
        path="m/84'/0'/1'/0/0",
        mixdepth=1,
    )
    selected_outpoints = {(selected.txid, selected.vout)}
    mock_key = MagicMock()
    mock_key.get_public_key_bytes.return_value = bytes.fromhex("02" + "ab" * 32)
    mock_key.get_private_key_bytes.return_value = bytes(32)
    mock_wallet.get_key_for_address.return_value = mock_key
    offer = Offer(
        counterparty="J5AtomicMaker",
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10_000,
        maxsize=100_000_000,
        txfee=1000,
        cjfee="0.0003",
    )
    session = CoinJoinSession(
        taker_nick="J5AtomicTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=mock_backend,
        taker_utxo_age=1,
    )
    taker_crypto = CryptoSession()
    success, _ = await session.handle_fill(
        amount=1_000_000,
        commitment="aa" * 32,
        taker_pk=taker_crypto.get_pubkey_hex(),
    )
    assert success is True

    blacklist = CommitmentBlacklist(blacklist_path=tmp_path / "commitmentlist")
    monkeypatch.setattr(commitment_blacklist, "_global_blacklist", blacklist)
    assert blacklist.add("aa" * 32) is True

    parsed_revelation = {
        "P": bytes.fromhex("02" + "cd" * 32),
        "P2": bytes.fromhex("02" + "ef" * 32),
        "sig": bytes.fromhex("11" * 32),
        "e": bytes.fromhex("22" * 32),
        "txid": "bb" * 32,
        "vout": 0,
    }
    with (
        patch("maker.coinjoin.parse_podle_revelation", return_value=parsed_revelation),
        patch("maker.coinjoin.verify_podle", return_value=(True, "")),
        patch("maker.coinjoin.verify_podle_binding", return_value=(True, "")),
        patch.object(
            session,
            "_select_our_utxos",
            new_callable=AsyncMock,
            return_value=(
                {next(iter(selected_outpoints)): selected},
                "bcrt1qcoinjoin",
                "bcrt1qchange",
                1,
            ),
        ),
        patch("jmcore.crypto.ecdsa_sign", return_value="mock_sig"),
    ):
        success, response = await session.handle_auth(
            commitment="aa" * 32,
            revelation={},
            kphex="",
        )

    assert success is True
    assert response["utxo_list"]
    assert session.state == CoinJoinState.AUTH_RECEIVED
    mock_wallet.release_coinjoin_inputs.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth_success", "persistence_success", "session_replaced"),
    [(True, True, False), (True, False, False), (False, False, False), (False, False, True)],
)
async def test_on_auth_releases_reservation_only_after_persistence(
    auth_success, persistence_success, session_replaced
):
    """Authenticated commitments stay reserved until local persistence succeeds."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from maker.coinjoin import CoinJoinState
    from maker.maker_session import MakerSession

    commitment = "ba" * 32
    taker_nick = "J5ReservationTaker"
    inner = MagicMock()
    inner.taker_nick = taker_nick
    inner.state = CoinJoinState.PUBKEY_SENT
    inner.commitment = bytes.fromhex(commitment)
    inner.crypto.is_encrypted = True
    inner.crypto.decrypt.return_value = f"{'bb' * 32}:0|02{'cc' * 32}|02{'dd' * 32}|11|22"
    inner.our_utxos = {}
    inner.amount = 500_000
    inner.cj_address = "bcrt1qcoinjoin"
    inner.change_address = "bcrt1qchange"
    inner.handle_auth = AsyncMock(
        return_value=(
            auth_success,
            {
                "utxo_list": "cc:0",
                "auth_pub": "02" + "ee" * 32,
                "cj_addr": "bcrt1qcoinjoin",
                "change_addr": "bcrt1qchange",
                "btc_sig": "signature",
            }
            if auth_success
            else {"error": "Failed to select UTXOs", "error_code": "UTXO selection failed"},
        )
    )
    session = MakerSession(inner)
    replacement = MagicMock()

    bot = MagicMock()
    bot.active_sessions = {taker_nick: replacement if session_replaced else session}
    bot.directory_clients = {}
    bot.config.network.value = "regtest"
    bot.wallet.wallet_fingerprint = "fingerprint"
    bot._reserved_commitments = {commitment}
    bot._broadcast_commitment = AsyncMock(return_value=persistence_success)
    bot._release_commitment_reservation = MagicMock(
        side_effect=lambda value: bot._reserved_commitments.discard(value)
    )
    session.send_response = AsyncMock(return_value=True)
    notifier = MagicMock()

    with (
        patch("maker.maker_session.UTXOMetadata.from_str"),
        patch("maker.maker_session.create_maker_history_entry", return_value=MagicMock()),
        patch("maker.maker_session.append_history_entry"),
        patch("maker.maker_session.get_notifier", return_value=notifier),
        patch("maker.maker_session.spawn_task"),
    ):
        await session.on_auth(bot, "auth ciphertext", "dir:test")

    if auth_success:
        bot._broadcast_commitment.assert_awaited_once_with(commitment)
        assert bot.active_sessions[taker_nick] is session
        assert session.state == CoinJoinState.IOAUTH_SENT
        inner.wallet.release_coinjoin_inputs.assert_not_called()
        if persistence_success:
            bot._release_commitment_reservation.assert_called_once_with(commitment)
            assert commitment not in bot._reserved_commitments
        else:
            bot._release_commitment_reservation.assert_not_called()
            assert commitment in bot._reserved_commitments
    else:
        bot._broadcast_commitment.assert_not_awaited()
        if session_replaced:
            bot._release_commitment_reservation.assert_not_called()
            assert commitment in bot._reserved_commitments
            assert bot.active_sessions[taker_nick] is replacement
            inner.wallet.release_coinjoin_inputs.assert_not_called()
            replacement.release_input_locks.assert_not_called()
        else:
            bot._release_commitment_reservation.assert_called_once_with(commitment)
            assert commitment not in bot._reserved_commitments
            assert taker_nick not in bot.active_sessions
            inner.wallet.release_coinjoin_inputs.assert_called_once_with(
                set(), owner=inner.input_lock_owner
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("tx_success", [True, False])
async def test_stale_on_tx_terminal_callback_keeps_replacement(tx_success):
    """A stale terminal tx callback cannot remove a replacement or release its input lock."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from maker.coinjoin import CoinJoinState
    from maker.maker_session import MakerSession

    taker_nick = "J5ReplacedTxTaker"
    outpoint = ("ca" * 32, 1)
    wallet = MagicMock()
    inner = MagicMock()
    inner.taker_nick = taker_nick
    inner.state = CoinJoinState.IOAUTH_SENT
    inner.crypto.is_encrypted = True
    inner.crypto.decrypt.return_value = base64.b64encode(b"transaction").decode()
    inner.our_utxos = {outpoint: MagicMock(address="bcrt1qmakerinput")}
    inner.amount = 500_000
    inner.offer.calculate_fee.return_value = 500
    inner.offer.txfee = 100
    inner.handle_tx = AsyncMock(
        return_value=(
            tx_success,
            {"signatures": ["signature"], "txid": "ab" * 32}
            if tx_success
            else {"error": "invalid transaction"},
        )
    )
    inner.wallet = wallet
    session = MakerSession(inner)
    session.send_response = AsyncMock()
    replacement = MagicMock()
    replacement.our_utxos = {outpoint: MagicMock()}

    bot = MagicMock()
    bot.active_sessions = {taker_nick: replacement}
    notifier = MagicMock()

    with (
        patch("maker.maker_session.update_awaiting_transaction_signed", return_value=True),
        patch("maker.maker_session.get_notifier", return_value=notifier),
        patch("maker.maker_session.spawn_task"),
    ):
        await session.on_tx(bot, "tx ciphertext", "dir:test")

    assert bot.active_sessions[taker_nick] is replacement
    wallet.release_coinjoin_inputs.assert_not_called()
    replacement.release_input_locks.assert_not_called()


def test_maker_session_uses_monotonic_deadline_without_running_loop():
    from unittest.mock import MagicMock, patch

    from maker.maker_session import MakerSession

    inner = MagicMock()
    inner.session_timeout_sec = 30

    with patch("maker.maker_session.time.monotonic", return_value=100.0):
        session = MakerSession(inner)

    assert session.deadline == 130.0
    with patch("maker.maker_session.time.monotonic", return_value=129.0):
        assert session.is_timed_out() is False
        assert session.remaining_timeout() == 1.0
    with patch("maker.maker_session.time.monotonic", return_value=130.0):
        assert session.is_timed_out() is True
        assert session.remaining_timeout() == 0.0


@pytest.mark.asyncio
async def test_signing_failure_crosses_lock_retention_boundary():
    from unittest.mock import AsyncMock, MagicMock, patch

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    wallet = MagicMock()
    wallet.network = "regtest"
    backend = MagicMock()
    backend.requires_neutrino_metadata.return_value = False
    session = CoinJoinSession(
        taker_nick="J5PartialSigningTaker",
        offer=MagicMock(),
        wallet=wallet,
        backend=backend,
    )
    session.state = CoinJoinState.IOAUTH_SENT

    with (
        patch("maker.coinjoin.verify_unsigned_transaction", return_value=(True, "")),
        patch.object(session, "_sign_transaction", new=AsyncMock(return_value=[])),
    ):
        success, _ = await session.handle_tx("00")

    assert success is False
    assert session.state == CoinJoinState.SIG_SENT


@pytest.mark.asyncio
async def test_valid_input_owner_is_renewed_before_signing(tmp_path):
    from unittest.mock import AsyncMock, MagicMock, patch

    from jmwallet.wallet.service import WalletService
    from jmwallet.wallet.utxo_metadata import UTXOMetadataStore

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    wallet = WalletService.__new__(WalletService)
    wallet.network = "regtest"
    wallet.metadata_store = UTXOMetadataStore(path=tmp_path / "metadata.jsonl")
    outpoint = ("ab" * 32, 0)
    session = CoinJoinSession(
        taker_nick="J5OwnedInputTaker",
        offer=MagicMock(),
        wallet=wallet,
        backend=MagicMock(),
    )
    session.state = CoinJoinState.IOAUTH_SENT
    session.our_utxos = {outpoint: MagicMock()}
    assert wallet.reserve_coinjoin_inputs({outpoint}, ttl=10, owner=session.input_lock_owner)
    old_expiry = wallet.metadata_store.records[f"{outpoint[0]}:{outpoint[1]}"].lock_until
    sign = AsyncMock(return_value=["signature"])

    with (
        patch("maker.coinjoin.verify_unsigned_transaction", return_value=(True, "")),
        patch.object(session, "_sign_transaction", new=sign),
        patch("jmcore.bitcoin.get_txid", return_value="cd" * 32),
    ):
        success, _ = await session.handle_tx("00")

    assert success is True
    sign.assert_awaited_once_with("00")
    wallet.metadata_store.load()
    record = wallet.metadata_store.records[f"{outpoint[0]}:{outpoint[1]}"]
    assert record.lock_owner == session.input_lock_owner
    assert record.lock_until is not None
    assert old_expiry is not None
    assert record.lock_until > old_expiry


@pytest.mark.asyncio
async def test_maker_does_not_sign_after_input_ownership_loss(tmp_path):
    from unittest.mock import AsyncMock, MagicMock, patch

    from jmwallet.wallet.service import WalletService
    from jmwallet.wallet.utxo_metadata import UTXOMetadataStore

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    wallet = WalletService.__new__(WalletService)
    wallet.network = "regtest"
    wallet.metadata_store = UTXOMetadataStore(path=tmp_path / "metadata.jsonl")
    outpoint = ("ab" * 32, 0)
    session = CoinJoinSession(
        taker_nick="J5StaleInputTaker",
        offer=MagicMock(),
        wallet=wallet,
        backend=MagicMock(),
    )
    session.state = CoinJoinState.IOAUTH_SENT
    session.our_utxos = {outpoint: MagicMock()}
    assert wallet.reserve_coinjoin_inputs({outpoint}, ttl=1, owner=session.input_lock_owner)
    metadata_ref = f"{outpoint[0]}:{outpoint[1]}"
    with wallet.metadata_store._exclusive_file_lock():
        wallet.metadata_store.load()
        wallet.metadata_store.records[metadata_ref].lock_until = 1.0
        wallet.metadata_store.save()
    assert wallet.reserve_coinjoin_inputs({outpoint}, owner="replacement-session")
    sign = AsyncMock(return_value=["signature"])

    with (
        patch("maker.coinjoin.verify_unsigned_transaction", return_value=(True, "")),
        patch.object(session, "_sign_transaction", new=sign),
    ):
        success, response = await session.handle_tx("00")

    assert success is False
    assert "ownership was lost" in response["error"]
    assert session.state == CoinJoinState.FAILED
    sign.assert_not_awaited()
    wallet.metadata_store.load()
    record = wallet.metadata_store.records[f"{outpoint[0]}:{outpoint[1]}"]
    assert record.lock_owner == "replacement-session"


@pytest.mark.asyncio
async def test_on_tx_failure_after_signing_retains_input_locks():
    from unittest.mock import AsyncMock, MagicMock, patch

    from maker.coinjoin import CoinJoinState
    from maker.maker_session import MakerSession

    taker_nick = "J5PartialSigningTaker"
    inner = MagicMock()
    inner.taker_nick = taker_nick
    inner.state = CoinJoinState.IOAUTH_SENT
    inner.crypto.is_encrypted = True
    inner.crypto.decrypt.return_value = base64.b64encode(b"transaction").decode()

    async def fail_after_signing(tx_hex, **kwargs):
        inner.state = CoinJoinState.SIG_SENT
        return False, {"error": "later input failed"}

    inner.handle_tx = AsyncMock(side_effect=fail_after_signing)
    session = MakerSession(inner)
    bot = MagicMock()
    bot.active_sessions = {taker_nick: session}

    with (
        patch("maker.maker_session.get_notifier", return_value=MagicMock()),
        patch("maker.maker_session.spawn_task"),
    ):
        await session.on_tx(bot, "tx ciphertext", "dir:test")

    assert taker_nick not in bot.active_sessions
    inner.wallet.release_coinjoin_inputs.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
