"""Tests for channel-as-change derivation, persistence and CoinJoin wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from jmcore.bitcoin import get_address_type
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.service import WalletService

from maker.channel_change import (
    ChannelChangeStore,
    derive_channel_change_address,
    emit_channel_change_address,
)

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)


@pytest.fixture
def wallet() -> WalletService:
    backend = DescriptorWalletBackend(wallet_name="cc_test")
    backend._wallet_loaded = True
    backend._descriptors_imported = True
    return WalletService(
        mnemonic=TEST_MNEMONIC, backend=backend, network="regtest", mixdepth_count=5
    )


def test_derive_channel_change_is_p2tr_and_deterministic(wallet: WalletService) -> None:
    addr0, rec0 = derive_channel_change_address(wallet, 0)
    addr1, rec1 = derive_channel_change_address(wallet, 1)
    assert addr0.startswith("bcrt1p")
    assert get_address_type(addr0) == "p2tr"
    assert addr0 != addr1
    # deterministic from the seed (recoverable)
    addr0b, _ = derive_channel_change_address(wallet, 0)
    assert addr0b == addr0
    assert rec0.our_key_a_path.endswith("/0") and rec0.our_key_b_path.endswith("/1")
    assert rec1.index == 1


def test_store_increments_index_and_persists(tmp_path: Path, wallet: WalletService) -> None:
    store = ChannelChangeStore(tmp_path)
    assert store.next_index() == 0
    a0 = emit_channel_change_address(wallet, store)
    a1 = emit_channel_change_address(wallet, store)
    assert a0 != a1
    assert store.next_index() == 2
    records = store.list()
    assert [r.index for r in records] == [0, 1]
    assert records[0].address == a0
    assert all(get_address_type(r.address) == "p2tr" for r in records)


def test_recoverable_keys_match_funding_output(wallet: WalletService) -> None:
    """The persisted key paths re-derive the same funding output (recoverable)."""
    from jmswap.channel import channel_funding_address

    _, rec = derive_channel_change_address(wallet, 3)
    key_a = wallet.master_key.derive(rec.our_key_a_path)
    key_b = wallet.master_key.derive(rec.our_key_b_path)
    recomputed = channel_funding_address(
        key_a.get_public_key_bytes(compressed=True),
        key_b.get_public_key_bytes(compressed=True),
        "regtest",
    )
    assert recomputed == rec.address


def test_session_uses_channel_change_when_enabled(tmp_path: Path, wallet: WalletService) -> None:
    """CoinJoinSession._build_change_address emits a P2TR when channel_change is on."""
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession

    wallet.data_dir = tmp_path
    offer = Offer(
        counterparty="x",
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=1,
        maxsize=10_000_000,
        txfee=0,
        cjfee="0.0001",
    )
    session = CoinJoinSession(
        taker_nick="t",
        offer=offer,
        wallet=wallet,
        backend=wallet.backend,
        channel_change=True,
    )
    addr = session._build_change_address(0)
    assert get_address_type(addr) == "p2tr"
    # plain change when disabled
    session.channel_change = False
    plain = session._build_change_address(0)
    assert get_address_type(plain) == "p2wpkh"
