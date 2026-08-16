"""Tests for channel-as-change derivation, persistence and CoinJoin wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

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
        self.opened: tuple[str, str, int, int | None] | None = None
        self.completed: tuple[str, str, int] | None = None
        self.cancel_external_channel_funding = AsyncMock()

    async def open_channel_external_funding(
        self, node_pubkey, address, channel_amount_sats, push_to_counterparty_msat=None
    ):
        self.opened = (
            node_pubkey,
            address,
            channel_amount_sats,
            push_to_counterparty_msat,
        )
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
    assert ldk.opened == ("02deadbeef", "1.2.3.4:9735", 250_000, None)
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


async def test_session_marks_ring_funding_completed_after_backend_success(
    tmp_path: Path, wallet: WalletService
) -> None:
    """The backend effect follows the durable channel-point intent and marks completion."""
    from jmcore.bitcoin import (
        TxInput,
        TxOutput,
        create_p2tr_scriptpubkey,
        get_txid,
        serialize_transaction,
    )
    from jmcore.models import Offer, OfferType
    from jmswap.ring_authorization import RingEdgeAuthorization, RingEdgeAuthorizationStore

    from maker.coinjoin import CoinJoinSession

    funding_spk = create_p2tr_scriptpubkey(b"\x48" * 32)
    ldk = _FakeLdk(funding_spk)
    authorization = RingEdgeAuthorization(
        edge_id="11" * 32,
        coinjoin_context="22" * 32,
        taker_nick="taker",
        taker_session_pubkey="33" * 32,
        funder_nick="funder",
        receiver_nick="receiver",
        funder_session_pubkey="44" * 32,
        receiver_session_pubkey="55" * 32,
        funder_ln_node="02" + "66" * 32 + "@funder.example:9735",
        receiver_ln_node="03" + "77" * 32 + "@receiver.example:9735",
        coinjoin_amount_sats=100_000,
        capacity_sats=250_000,
        funder_contribution_sats=150_000,
        receiver_contribution_sats=100_000,
        expires_at_unix=2_000_000_000,
    )
    store = RingEdgeAuthorizationStore(tmp_path / "ring")
    store.authorize(authorization, local_nick="funder", now_unix=1)
    from jmcore.bitcoin import scriptpubkey_to_address

    store.bind_funding(
        authorization.edge_id,
        authorization.authorization_id,
        scriptpubkey_to_address(funding_spk, "regtest"),
        funding_spk.hex(),
        pending_channel_id="ring-channel",
    )
    session = CoinJoinSession(
        taker_nick="taker",
        offer=Offer(
            counterparty="funder",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=1,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.0001",
        ),
        wallet=wallet,
        backend=wallet.backend,
        ldk=ldk,
        ring_edge_authorization_store=store,
    )
    session._channel_user_id = "ring-channel"
    session._channel_funding_spk = funding_spk
    session._channel_edge_id = authorization.edge_id
    session._channel_authorization_id = authorization.authorization_id
    tx_hex = serialize_transaction(
        2,
        [TxInput.from_hex("ab" * 32, 0)],
        [TxOutput(value=250_000, script=funding_spk)],
        0,
    ).hex()

    await session._complete_channel_change(tx_hex)

    record = store.load(authorization.edge_id)
    assert record is not None
    assert record.channel_point is not None
    assert record.channel_point.txid == get_txid(tx_hex)
    assert record.funding_completed is True
    assert ldk.completed == ("ring-channel", get_txid(tx_hex), 0)


async def test_lnd_funding_failure_refuses_to_sign(
    wallet: WalletService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A maker must not sign a CoinJoin that LND failed to pin to its channel."""
    from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    class _FakeLnd:
        requires_pre_signing_funding_completion = True

        def __init__(self) -> None:
            self.cancel_external_channel_funding = AsyncMock()

    backend = _FakeLnd()
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
        taker_nick="t", offer=offer, wallet=wallet, backend=wallet.backend, ldk=backend
    )
    session.state = CoinJoinState.IOAUTH_SENT
    session._channel_user_id = "pending-channel"
    session._channel_funding_spk = create_p2tr_scriptpubkey(b"\x40" * 32)
    session.change_address = scriptpubkey_to_address(session._channel_funding_spk, "regtest")
    complete = AsyncMock(side_effect=RuntimeError("LND rejected PSBT"))
    sign = AsyncMock(return_value=["signature"])
    monkeypatch.setattr(session, "_complete_channel_change", complete)
    monkeypatch.setattr(session, "_sign_transaction", sign)
    monkeypatch.setattr("maker.coinjoin.verify_unsigned_transaction", lambda **_kwargs: (True, ""))

    success, response = await session.handle_tx("00")

    assert success is False
    assert response == {"error": "channel-as-change funding verification failed"}
    assert session.state == CoinJoinState.FAILED
    complete.assert_awaited_once_with("00")
    sign.assert_not_awaited()
    backend.cancel_external_channel_funding.assert_awaited_once_with("pending-channel")


async def test_timed_out_session_cancels_pending_channel(
    wallet: WalletService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out round must not leave its external channel open in the backend."""
    from jmcore.bitcoin import create_p2tr_scriptpubkey
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    backend = _FakeLdk(create_p2tr_scriptpubkey(b"\x46" * 32))
    session = CoinJoinSession(
        taker_nick="t",
        offer=Offer(
            counterparty="x",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=1,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.0001",
        ),
        wallet=wallet,
        backend=wallet.backend,
        ldk=backend,
    )
    session.state = CoinJoinState.IOAUTH_SENT
    session._channel_user_id = "timed-out-channel"
    session._channel_funding_spk = backend.funding_spk
    monkeypatch.setattr(session, "is_timed_out", lambda: True)

    success, response = await session.handle_tx("00")

    assert success is False
    assert response == {"error": "Session timed out after 300s"}
    backend.cancel_external_channel_funding.assert_awaited_once_with("timed-out-channel")


async def test_duplicate_channel_open_is_rejected(wallet: WalletService) -> None:
    """Repeated chanreq messages must not allocate untracked pending channels."""
    from jmcore.bitcoin import create_p2tr_scriptpubkey
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession

    backend = _FakeLdk(create_p2tr_scriptpubkey(b"\x47" * 32))
    session = CoinJoinSession(
        taker_nick="t",
        offer=Offer(
            counterparty="x",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=1,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.0001",
        ),
        wallet=wallet,
        backend=wallet.backend,
        ldk=backend,
    )
    session._channel_user_id = "existing-channel"

    result = await session.open_external_channel("02deadbeef@1.2.3.4:9735", 250_000)

    assert result is None
    assert backend.opened is None


async def test_lnd_signing_failure_cancels_verified_funding(
    wallet: WalletService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PSBT shim must be cancelled when local input signing subsequently fails."""
    from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    class _FakeLnd:
        requires_pre_signing_funding_completion = True

        def __init__(self) -> None:
            self.cancel_external_channel_funding = AsyncMock()

    backend = _FakeLnd()
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
        taker_nick="t", offer=offer, wallet=wallet, backend=wallet.backend, ldk=backend
    )
    session.state = CoinJoinState.IOAUTH_SENT
    session._channel_user_id = "pending-channel"
    session._channel_funding_spk = create_p2tr_scriptpubkey(b"\x41" * 32)
    session.change_address = scriptpubkey_to_address(session._channel_funding_spk, "regtest")
    complete = AsyncMock()
    monkeypatch.setattr(session, "_complete_channel_change", complete)
    monkeypatch.setattr(session, "_sign_transaction", AsyncMock(return_value=[]))
    monkeypatch.setattr("maker.coinjoin.verify_unsigned_transaction", lambda **_kwargs: (True, ""))

    success, response = await session.handle_tx("00")

    assert success is False
    assert response == {"error": "Failed to sign transaction"}
    complete.assert_awaited_once_with("00")
    backend.cancel_external_channel_funding.assert_awaited_once_with("pending-channel")


async def test_failed_ring_plan_cancels_channel_and_signs_plain_change(
    wallet: WalletService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A taker ring fallback must not leave the maker trying to pin an absent output."""
    from jmcore.bitcoin import TxInput, TxOutput, create_p2tr_scriptpubkey, serialize_transaction
    from jmcore.models import Offer, OfferType

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    class _FakeLnd:
        requires_pre_signing_funding_completion = True

        def __init__(self) -> None:
            self.cancel_external_channel_funding = AsyncMock()

    backend = _FakeLnd()
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
        taker_nick="t", offer=offer, wallet=wallet, backend=wallet.backend, ldk=backend
    )
    session.state = CoinJoinState.IOAUTH_SENT
    session.change_address = wallet.get_change_address(0, 0)
    session._channel_user_id = "unused-ring-channel"
    session._channel_funding_spk = create_p2tr_scriptpubkey(b"\x42" * 32)
    sign = AsyncMock(return_value=["signature"])
    monkeypatch.setattr(session, "_sign_transaction", sign)
    monkeypatch.setattr("maker.coinjoin.verify_unsigned_transaction", lambda **_kwargs: (True, ""))
    tx_hex = serialize_transaction(
        2,
        [TxInput.from_hex("ab" * 32, 0)],
        [TxOutput(value=100_000, script=create_p2tr_scriptpubkey(b"\x43" * 32))],
        0,
    ).hex()

    success, response = await session.handle_tx(tx_hex)

    assert success is True
    assert response["signatures"] == ["signature"]
    assert session.state == CoinJoinState.SIG_SENT
    sign.assert_awaited_once_with(tx_hex)
    backend.cancel_external_channel_funding.assert_awaited_once_with("unused-ring-channel")
    assert session._channel_user_id is None
    assert session._channel_funding_spk is None


async def test_ring_relay_must_match_requested_channel_push(
    wallet: WalletService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The relayed own share must equal the immutable authorized contribution."""
    import time

    from jmcore.bitcoin import (
        TxInput,
        TxOutput,
        create_p2tr_scriptpubkey,
        get_txid,
        scriptpubkey_to_address,
        serialize_transaction,
    )
    from jmcore.models import Offer, OfferType
    from jmswap.ring_authorization import (
        RingEdgeAuthorization,
        RingEdgeAuthorizationStore,
        RingEdgeFunding,
    )

    from maker.coinjoin import CoinJoinSession, CoinJoinState

    class _FakeLnd:
        requires_pre_signing_funding_completion = True

        def __init__(self) -> None:
            self.cancel_external_channel_funding = AsyncMock()

    backend = _FakeLnd()
    store = RingEdgeAuthorizationStore(tmp_path / "ring")
    authorization = RingEdgeAuthorization(
        edge_id="11" * 32,
        coinjoin_context="22" * 32,
        taker_nick="t",
        taker_session_pubkey="33" * 32,
        funder_nick="funder",
        receiver_nick="receiver",
        funder_session_pubkey="44" * 32,
        receiver_session_pubkey="55" * 32,
        funder_ln_node="02" + "66" * 32 + "@funder.example:9735",
        receiver_ln_node="03" + "77" * 32 + "@receiver.example:9735",
        coinjoin_amount_sats=1_000_000,
        capacity_sats=500_000,
        funder_contribution_sats=325_000,
        receiver_contribution_sats=175_000,
        expires_at_unix=int(time.time()) + 60,
    )
    store.authorize(authorization, local_nick="funder")
    session = CoinJoinSession(
        taker_nick="t",
        offer=Offer(
            counterparty="x",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=1,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.0001",
        ),
        wallet=wallet,
        backend=wallet.backend,
        ldk=backend,
        ring_edge_authorization_store=store,
    )
    session.state = CoinJoinState.IOAUTH_SENT
    session._channel_user_id = "ring-channel"
    session._channel_funding_spk = create_p2tr_scriptpubkey(b"\x44" * 32)
    session._channel_capacity = 500_000
    session._channel_push_msat = 175_000_000
    session._ring_authorization_ids = {authorization.edge_id: authorization.authorization_id}
    funding_address = scriptpubkey_to_address(session._channel_funding_spk, "regtest")
    sign = AsyncMock(return_value=["signature"])
    monkeypatch.setattr(session, "_sign_transaction", sign)
    monkeypatch.setattr("maker.coinjoin.verify_unsigned_transaction", lambda **_kwargs: (True, ""))
    tx_hex = serialize_transaction(
        2,
        [TxInput.from_hex("ab" * 32, 0)],
        [TxOutput(value=500_000, script=session._channel_funding_spk)],
        0,
    ).hex()

    success, response = await session.handle_tx(
        tx_hex,
        ring=[
            RingEdgeFunding(
                edge_id=authorization.edge_id,
                authorization_id=authorization.authorization_id,
                funding_address=funding_address,
                contribution_sats=300_000,
            )
        ],
    )

    assert success is False
    assert response == {"error": "ring authorization verification failed"}
    sign.assert_not_awaited()
    backend.cancel_external_channel_funding.assert_awaited_once_with("ring-channel")

    session._bind_ring_funding(
        tx_hex,
        [
            RingEdgeFunding(
                edge_id=authorization.edge_id,
                authorization_id=authorization.authorization_id,
                funding_address=funding_address,
                contribution_sats=325_000,
            )
        ],
    )
    record = store.load(authorization.edge_id)
    assert record is not None
    assert record.funding_address == funding_address
    assert record.channel_point is not None
    assert record.channel_point.txid == get_txid(tx_hex)
    assert record.channel_point.vout == 0


def _addr_for_test(byte: int) -> str:
    from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address

    return scriptpubkey_to_address(create_p2tr_scriptpubkey(bytes([byte]) * 32), "regtest")
