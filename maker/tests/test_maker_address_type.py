"""
Unit tests for Maker address-type parsing and P2TR generation.

Tests that the Maker correctly parses the optional address_type=p2tr
field from the !fill message, rejects mismatched address types, and uses
the wallet's own address type when none is negotiated.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.encryption import CryptoSession
from jmcore.models import Offer, OfferType
from jmwallet.wallet.service import WalletService

from maker.config import MakerConfig
from maker.protocol_handlers import ProtocolHandlersMixin


class MockMakerBot(ProtocolHandlersMixin):
    def __init__(self, wallet, config, backend):
        self.wallet = wallet
        self.config = config
        self.backend = backend
        self.current_offers = []
        self.active_sessions = {}
        self.offer_manager = MagicMock()
        self.offer_manager.get_offer_by_id.side_effect = lambda offers, oid: next(
            (o for o in offers if o.oid == oid), None
        )
        self.offer_manager.validate_offer_fill.return_value = (True, None)
        self.last_response = None

    async def _send_response(self, nick: str, command: str, data: str, **kwargs) -> bool:
        self.last_response = f"!{command} {data}"
        return True

    @property
    def _own_wallet_nicks(self):
        return set()


@pytest.fixture
def mock_wallet():
    wallet = MagicMock(spec=WalletService)
    wallet.address_type = "p2wpkh"
    wallet.network = "regtest"
    wallet.mixdepth_count = 5
    wallet.root_path = "m/84'/1'/0'"
    wallet.address_cache = {}

    wallet.get_balance_for_offers = AsyncMock(return_value=10_000_000)
    wallet.get_change_address.side_effect = lambda m, i: f"bcrt1q_mock_p2wpkh_address_{m}_{i}"
    wallet.get_next_address_index.return_value = 0

    return wallet


@pytest.fixture
def mock_p2tr_wallet():
    wallet = MagicMock(spec=WalletService)
    wallet.address_type = "p2tr"
    wallet.network = "regtest"
    wallet.mixdepth_count = 5
    wallet.root_path = "m/86'/1'/0'"
    wallet.address_cache = {}

    wallet.get_balance_for_offers = AsyncMock(return_value=10_000_000)
    wallet.get_change_address.side_effect = lambda m, i: f"bcrt1p_mock_p2tr_address_{m}_{i}"
    wallet.get_next_address_index.return_value = 0

    # master_key.derive() is used in the cross-type path (P2TR wallet
    # generating P2WPKH addresses for legacy takers).
    def derive_side_effect(path: str) -> MagicMock:
        key = MagicMock()
        # HDKey.get_address() returns P2WPKH
        key.get_address.return_value = f"bcrt1q_from_bip86_{path.split('/')[-1]}"
        # HDKey.get_p2tr_address() returns P2TR
        key.get_p2tr_address.return_value = f"bcrt1p_from_bip86_{path.split('/')[-1]}"
        return key

    master_key = MagicMock()
    master_key.derive.side_effect = derive_side_effect
    wallet.master_key = master_key

    return wallet


@pytest.fixture
def maker_config():
    from maker.config import MergeAlgorithm

    config = MagicMock(spec=MakerConfig)
    config.session_timeout_sec = 300
    config.merge_algorithm = MergeAlgorithm.DEFAULT
    return config


@pytest.fixture
def maker_bot(mock_wallet, maker_config):
    backend = AsyncMock()
    return MockMakerBot(
        wallet=mock_wallet,
        backend=backend,
        config=maker_config,
    )


def create_mock_offer():
    return Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=100_000_000,
        txfee=500,
        cjfee=250,
        counterparty="J5TestMaker",
    )


@pytest.mark.asyncio
async def test_handle_fill_parses_address_type(maker_bot):
    """Test that `_handle_fill` correctly parses `address_type=p2tr`."""
    taker_nick = "J5TestTaker"
    io_channel = "direct"

    offer = create_mock_offer()
    maker_bot.current_offers = [offer]

    crypto = CryptoSession()
    taker_nacl_pubkey = crypto.get_pubkey_hex()
    fill_data = (
        f"fill {offer.oid} 10000000 {taker_nacl_pubkey} "
        "P0000000000000000000000000000000000000000000000000000000000000000 address_type=p2tr"
    )

    await maker_bot._handle_fill(taker_nick, fill_data, io_channel)
    assert maker_bot.last_response is not None
    assert maker_bot.last_response.startswith("!pubkey")

    session = maker_bot.active_sessions[taker_nick]
    assert session.requested_address_type == "p2tr"


@pytest.mark.asyncio
async def test_handle_fill_ignores_missing_address_type(maker_bot):
    """Test that `_handle_fill` handles legacy !fill messages gracefully."""
    taker_nick = "J5TestTakerLegacy"
    io_channel = "direct"

    offer = create_mock_offer()
    maker_bot.current_offers = [offer]

    crypto = CryptoSession()
    taker_nacl_pubkey = crypto.get_pubkey_hex()
    fill_data = (
        f"fill {offer.oid} 10000000 {taker_nacl_pubkey} "
        "P0000000000000000000000000000000000000000000000000000000000000000"
    )

    await maker_bot._handle_fill(taker_nick, fill_data, io_channel)
    assert maker_bot.last_response is not None

    session = maker_bot.active_sessions[taker_nick]
    assert session.requested_address_type is None


@pytest.mark.asyncio
async def test_coinjoin_session_rejects_mismatched_address_type(mock_wallet):
    """A P2WPKH maker must reject a taker that explicitly requests P2TR outputs.

    Generating P2TR addresses from a P2WPKH wallet is unsafe: those outputs would
    fall outside the wallet's wpkh() descriptors and become unrecoverable after a
    restart. The session raises ValueError so the CoinJoin is aborted.
    """
    from maker.coinjoin import CoinJoinSession

    offer = create_mock_offer()
    backend = AsyncMock()

    session = CoinJoinSession(
        taker_nick="J5TestTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=backend,
        requested_address_type="p2tr",
    )

    from jmwallet.wallet.models import UTXOInfo

    utxo = UTXOInfo(
        txid="a" * 64,
        vout=0,
        value=1000000,
        address="mock",
        confirmations=10,
        scriptpubkey="0014" + "00" * 20,
        path="m/84'/1'/0'/0/0",
        mixdepth=0,
    )
    mock_wallet.select_utxos_with_merge.return_value = [utxo]

    session.amount = 100000
    utxos, cj_addr, change_addr, _ = await session._select_our_utxos()

    # _select_our_utxos catches the ValueError internally and returns
    # an empty dict to signal failure (the CoinJoin is aborted).
    assert utxos == {}
    assert cj_addr == ""
    assert change_addr == ""


@pytest.mark.asyncio
async def test_p2tr_wallet_defaults_to_p2wpkh_for_legacy_taker(mock_p2tr_wallet):
    """A P2TR maker defaults to P2WPKH when the taker doesn't negotiate address_type.

    If a legacy taker receives P2TR outputs, its equal-amount output
    would be the only non-P2WPKH output — a fingerprint that destroys
    CoinJoin privacy.  So the maker defaults to P2WPKH.
    """
    from maker.coinjoin import CoinJoinSession

    offer = create_mock_offer()
    backend = AsyncMock()

    # No requested_address_type: defaults to p2wpkh for privacy
    session = CoinJoinSession(
        taker_nick="J5TestTaker",
        offer=offer,
        wallet=mock_p2tr_wallet,
        backend=backend,
    )

    from jmwallet.wallet.models import UTXOInfo

    utxo = UTXOInfo(
        txid="a" * 64,
        vout=0,
        value=1000000,
        address="mock",
        confirmations=10,
        scriptpubkey="5120" + "00" * 32,
        path="m/86'/1'/0'/0/0",
        mixdepth=0,
    )
    mock_p2tr_wallet.select_utxos_with_merge.return_value = [utxo]

    session.amount = 100000
    _, cj_address, change_address, _ = await session._select_our_utxos()

    # P2TR wallet generates P2WPKH for legacy taker
    assert cj_address.startswith("bcrt1q"), (
        f"Expected P2WPKH (bcrt1q) for legacy taker, got: {cj_address}"
    )
    assert change_address.startswith("bcrt1q"), (
        f"Expected P2WPKH (bcrt1q) for legacy taker, got: {change_address}"
    )
    # Addresses are cached for signing
    assert cj_address in mock_p2tr_wallet.address_cache
    assert change_address in mock_p2tr_wallet.address_cache


@pytest.mark.asyncio
async def test_coinjoin_session_generates_p2tr_when_taker_requests_and_wallet_matches(
    mock_p2tr_wallet,
):
    """A P2TR maker honours an explicit p2tr request from the taker (types match)."""
    from maker.coinjoin import CoinJoinSession

    offer = create_mock_offer()
    backend = AsyncMock()

    session = CoinJoinSession(
        taker_nick="J5TestTaker",
        offer=offer,
        wallet=mock_p2tr_wallet,
        backend=backend,
        requested_address_type="p2tr",
    )

    from jmwallet.wallet.models import UTXOInfo

    utxo = UTXOInfo(
        txid="b" * 64,
        vout=0,
        value=1000000,
        address="mock",
        confirmations=10,
        scriptpubkey="5120" + "00" * 32,
        path="m/86'/1'/0'/0/0",
        mixdepth=0,
    )
    mock_p2tr_wallet.select_utxos_with_merge.return_value = [utxo]

    session.amount = 100000
    _, cj_address, change_address, _ = await session._select_our_utxos()

    assert cj_address.startswith("bcrt1p_mock_p2tr_address")
    assert change_address.startswith("bcrt1p_mock_p2tr_address")


@pytest.mark.asyncio
async def test_coinjoin_session_generates_p2wpkh_addresses_by_default(mock_wallet):
    """Test that CoinjoinSession uses wallet type (p2wpkh) when nothing is requested."""
    from maker.coinjoin import CoinJoinSession

    offer = create_mock_offer()
    backend = AsyncMock()

    session = CoinJoinSession(
        taker_nick="J5TestTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=backend,
    )

    from jmwallet.wallet.models import UTXOInfo

    utxo = UTXOInfo(
        txid="b" * 64,
        vout=1,
        value=1000000,
        address="mock",
        confirmations=10,
        scriptpubkey="0014" + "00" * 20,
        path="m/84'/1'/0'/0/0",
        mixdepth=0,
    )
    mock_wallet.select_utxos_with_merge.return_value = [utxo]

    session.amount = 100000
    _, cj_address, change_address, _ = await session._select_our_utxos()

    assert cj_address.startswith("bcrt1q_mock_p2wpkh_address")
    assert change_address.startswith("bcrt1q_mock_p2wpkh_address")
