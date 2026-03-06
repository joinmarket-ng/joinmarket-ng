"""
Unit tests for Maker address-type parsing and P2TR generation.

Tests that the Maker correctly parses the optional address_type=p2tr
field from the !fill message and uses it to generate P2TR outputs,
even if the maker's own wallet defaults to P2WPKH.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from jmcore.models import Offer, OfferType
from jmcore.encryption import CryptoSession

from maker.protocol_handlers import ProtocolHandlersMixin
from maker.config import MakerConfig
from jmwallet.wallet.service import WalletService


class MockMakerBot(ProtocolHandlersMixin):
    def __init__(self, wallet, config, backend):
        self.wallet = wallet
        self.config = config
        self.backend = backend
        self.current_offers = []
        self.active_sessions = {}
        self.offer_manager = MagicMock()
        self.offer_manager.get_offer_by_id.side_effect = lambda offers, oid: next((o for o in offers if o.oid == oid), None)
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
    
    master_key = MagicMock()
    derived_cj = MagicMock()
    derived_change = MagicMock()
    
    derived_cj.get_p2tr_address.return_value = "bcrt1p_cj_mock_p2tr_address"
    derived_change.get_p2tr_address.return_value = "bcrt1p_change_mock_p2tr_address"
    
    # Routing master_key.derive() to return correct derived keys based on index
    # cj_output_mixdepth will be 1, max_mixdepth will be 0
    # cj path: m/84'/1'/0'/1'/1/0
    # change path: m/84'/1'/0'/0'/1/0
    def derive_side_effect(path):
        if "1'/1/" in path:
            return derived_cj
        return derived_change
        
    master_key.derive.side_effect = derive_side_effect
    wallet.master_key = master_key
    
    wallet.get_change_address.side_effect = lambda m, i: f"bcrt1q_mock_p2wpkh_address_{m}_{i}"
    wallet.get_next_address_index.return_value = 0
    
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
    fill_data = f"fill {offer.oid} 10000000 {taker_nacl_pubkey} P0000000000000000000000000000000000000000000000000000000000000000 address_type=p2tr"
    
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
    fill_data = f"fill {offer.oid} 10000000 {taker_nacl_pubkey} P0000000000000000000000000000000000000000000000000000000000000000"
    
    await maker_bot._handle_fill(taker_nick, fill_data, io_channel)
    assert maker_bot.last_response is not None
    
    session = maker_bot.active_sessions[taker_nick]
    assert session.requested_address_type is None


@pytest.mark.asyncio
async def test_coinjoin_session_generates_p2tr_addresses(mock_wallet):
    """Test that CoinjoinSession generates P2TR if requested_address_type='p2tr'."""
    from maker.coinjoin import CoinJoinSession
    
    offer = create_mock_offer()
    backend = AsyncMock()
    
    session = CoinJoinSession(
        taker_nick="J5TestTaker",
        offer=offer,
        wallet=mock_wallet,
        backend=backend,
        requested_address_type="p2tr"
    )
    
    from jmwallet.wallet.models import UTXOInfo
    utxo = UTXOInfo(
        txid="a"*64, vout=0, value=1000000, address="mock",
        confirmations=10, scriptpubkey="0014"+"00"*20,
        path="m/84'/1'/0'/0/0", mixdepth=0
    )
    mock_wallet.select_utxos_with_merge.return_value = [utxo]
    
    session.amount = 100000
    _, cj_address, change_address, _ = await session._select_our_utxos()
    
    assert cj_address == "bcrt1p_cj_mock_p2tr_address"
    assert change_address == "bcrt1p_change_mock_p2tr_address"


@pytest.mark.asyncio
async def test_coinjoin_session_generates_p2wpkh_addresses_by_default(mock_wallet):
    """Test that CoinjoinSession defaults to wallet type if not requested."""
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
        txid="b"*64, vout=1, value=1000000, address="mock",
        confirmations=10, scriptpubkey="0014"+"00"*20,
        path="m/84'/1'/0'/0/0", mixdepth=0
    )
    mock_wallet.select_utxos_with_merge.return_value = [utxo]
    
    session.amount = 100000
    _, cj_address, change_address, _ = await session._select_our_utxos()
    
    assert cj_address.startswith("bcrt1q_mock_p2wpkh_address")
    assert change_address.startswith("bcrt1q_mock_p2wpkh_address")
