"""Tests for channel-as-change derivation, persistence and CoinJoin wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from jmcore.bitcoin import get_address_type
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.service import WalletService

from maker.channel_change import (
    ChannelChangeRecord,
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


def test_build_channel_change_sweep_verifies_under_funding_key(wallet: WalletService) -> None:
    """The cooperative-close sweep is a valid key-path spend of the funding output."""
    from coincurve import PublicKeyXOnly
    from jmcore.bitcoin import create_p2tr_scriptpubkey
    from jmcore.taproot import taproot_sighash

    from maker.channel_change import build_channel_change_sweep, derive_channel_change_address

    _, record = derive_channel_change_address(wallet, 0)
    dest_spk = create_p2tr_scriptpubkey(b"\x07" * 32)
    # a P2TR destination address for the sweep
    from jmcore.bitcoin import scriptpubkey_to_address

    dest = scriptpubkey_to_address(dest_spk, "regtest")
    tx = build_channel_change_sweep(wallet, record, "ab" * 32, 0, 200_000, dest)
    assert len(tx.witnesses[0]) == 1  # single key-path signature
    sighash = taproot_sighash(tx, 0, [200_000], [tx_funding_spk(wallet, record)])
    assert PublicKeyXOnly(tx_funding_spk(wallet, record)[2:]).verify(tx.witnesses[0][0], sighash)


def tx_funding_spk(wallet: WalletService, record: ChannelChangeRecord) -> bytes:
    from maker.channel_change import channel_funding_output

    key_a = wallet.master_key.derive(record.our_key_a_path)
    key_b = wallet.master_key.derive(record.our_key_b_path)
    spk, _ = channel_funding_output(
        key_a.get_public_key_bytes(compressed=True),
        key_b.get_public_key_bytes(compressed=True),
    )
    return spk


async def test_session_uses_channel_change_when_enabled(
    tmp_path: Path, wallet: WalletService
) -> None:
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
    # no LDK/peer configured -> recoverable self-2-of-2 P2TR funding output
    addr = await session._build_change_address(0, 100_000)
    assert get_address_type(addr) == "p2tr"
    # plain change when disabled
    session.channel_change = False
    plain = await session._build_change_address(0, 100_000)
    assert get_address_type(plain) == "p2wpkh"


class _FakeLdk:
    """Minimal external-funding LDK backend for the live channel-change path."""

    def __init__(self, funding_spk: bytes) -> None:
        self.funding_spk = funding_spk
        self.opened: tuple[str, str, int] | None = None
        self.completed: tuple[str, str, int] | None = None

    async def open_channel_external_funding(
        self, node_pubkey, address, channel_amount_sats, push_to_counterparty_msat=None
    ):
        self.opened = (node_pubkey, address, channel_amount_sats)
        return "ucid-7"

    async def get_funding_output(self, user_channel_id):
        assert self.opened is not None
        return True, self.funding_spk.hex(), self.opened[2]

    async def provide_funding_outpoint(
        self, user_channel_id, funding_txid, funding_vout, raw_tx_hex=None, input_utxos=None
    ):
        self.completed = (user_channel_id, funding_txid, funding_vout)


async def test_session_channel_change_opens_real_channel(wallet: WalletService) -> None:
    """With an LDK backend + peer, the change becomes a real channel funding output."""
    from jmcore.bitcoin import TxInput, TxOutput, get_txid, serialize_transaction
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession

    funding_spk = bytes([0x00, 0x20]) + b"\x11" * 32  # P2WSH (channel funding shape)
    ldk = _FakeLdk(funding_spk)
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
        ldk=ldk,
        channel_peer="02deadbeef@1.2.3.4:9735",
    )

    addr = await session._build_change_address(0, 250_000)
    from jmcore.bitcoin import scriptpubkey_to_address

    assert addr == scriptpubkey_to_address(funding_spk, "regtest")
    assert ldk.opened == ("02deadbeef", "1.2.3.4:9735", 250_000)
    assert session._channel_user_id == "ucid-7"
    assert session._channel_funding_spk == funding_spk

    # build a tx carrying the funding output at vout 1, then complete
    tx_in = TxInput.from_hex(txid="ab" * 32, vout=0, value=0, scriptpubkey="")
    outs = [
        TxOutput(value=500, script=bytes([0x00, 0x14]) + b"\x02" * 20),
        TxOutput(value=250_000, script=funding_spk),
    ]
    tx_hex = serialize_transaction(2, [tx_in], outs, 0).hex()
    await session._complete_channel_change(tx_hex)
    assert ldk.completed == ("ucid-7", get_txid(tx_hex), 1)
