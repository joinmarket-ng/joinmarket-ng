"""A maker never advertises a fidelity bond its data directory leased to another maker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bitcointx.core.key import CKey
from jmcore.credential_market import BondReference, bond_resource
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_store import MarketStore
from jmcore.models import NetworkType, Offer, OfferType

from maker.bond_leases import (
    STALE_SECONDS,
    AdvertisementRecordError,
    BondLeaseGuard,
    advertisement_record_path,
    read_advertisements,
    record_advertisement,
)
from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.fidelity import FidelityBondInfo

KEY = CKey(bytes([0x11]) * 32)
LOCKTIME = 4_102_444_800


def _bond(txid_byte: int, bond_value: int = 1_000) -> FidelityBondInfo:
    return FidelityBondInfo(
        txid=f"{txid_byte:02x}" * 32,
        vout=0,
        value=100_000_000,
        locktime=LOCKTIME,
        confirmation_time=1_704_067_200,
        bond_value=bond_value,
        pubkey=bytes(KEY.pub),
        private_key=KEY,
    )


def _store_path(data_dir: Path) -> Path:
    return data_dir / "market" / "seller.sqlite"


def _stock(data_dir: Path, bond: FidelityBondInfo, period: int = 0) -> None:
    """Stock a lease of ``bond``, as a seller designating it for rent would."""
    reference = BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=bond.txid, vout=bond.vout),
        pubkey=bytes(KEY.pub).hex(),
        locktime=LOCKTIME,
    )
    with MarketStore(_store_path(data_dir)) as store:
        store.add_inventory("bond", bond_resource(reference, period), None)


async def test_stocked_bond_is_blocked_and_others_are_allowed(tmp_path: Path) -> None:
    leased, free = _bond(0xAA), _bond(0xBB)
    _stock(tmp_path, leased)
    guard = BondLeaseGuard(_store_path(tmp_path), "regtest")

    assert await guard.refresh([leased, free], 1) == {(leased.txid, 0)}
    assert not guard.allows(free)  # nothing is recorded as advertised yet
    assert await guard.refresh([leased, free], 1, advertising=leased) == {(leased.txid, 0)}
    assert not guard.allows(leased)
    assert read_advertisements(guard.record_path) is None  # a blocked bond is never recorded
    await guard.refresh([leased, free], 1, advertising=free)
    assert guard.allows(free)


async def test_no_store_allows_the_bond(tmp_path: Path) -> None:
    guard = BondLeaseGuard(_store_path(tmp_path), "regtest")
    assert await guard.refresh([_bond(0xAA)], 1, advertising=_bond(0xAA)) == frozenset()
    assert guard.allows(_bond(0xAA))
    # Recorded through the next period, before the first proof.
    record = read_advertisements(advertisement_record_path(tmp_path))
    assert record is not None and record.through((_bond(0xAA).txid, 0)) == 1


async def test_unknown_unread_or_stale_history_allows_no_bond(tmp_path: Path) -> None:
    bond = _bond(0xAA)
    now = [0.0]
    guard = BondLeaseGuard(_store_path(tmp_path), "regtest", clock=lambda: now[0])
    assert not guard.allows(bond)  # never read

    assert await guard.refresh([bond], 1, advertising=bond) == frozenset()
    assert guard.allows(bond)
    now[0] = STALE_SECONDS + 1
    assert not guard.allows(bond)  # the background refresh stalled

    _store_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _store_path(tmp_path).write_bytes(b"not a database" * 100)
    assert await guard.refresh([bond], 1) is None
    assert not guard.allows(bond)


def _bot(tmp_path: Path, **config: object) -> MakerBot:
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    wallet.network = NetworkType.REGTEST
    bot = MakerBot(
        wallet=wallet,
        backend=MagicMock(),
        config=MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            data_dir=tmp_path,
            **config,
        ),
    )
    bot.current_offers = [
        Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=1_000_000,
            maxsize=10_000_000,
            txfee=500,
            cjfee="0.0003",
        )
    ]
    return bot


async def test_a_leased_bond_proof_is_suppressed_but_offers_are_sent(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot.fidelity_bond = _bond(0xAA)
    client = AsyncMock()
    bot.directory_clients["test"] = client

    # A bot built without start() has no guard and advertises as before.
    await bot._send_offers_to_taker("J5TestTakerNick1")
    assert "!tbond" in client.send_private_message.call_args[0][2]

    _stock(tmp_path, bot.fidelity_bond)
    bot.bond_lease_guard = BondLeaseGuard(_store_path(tmp_path), "regtest")
    await bot.bond_lease_guard.refresh([bot.fidelity_bond], 1)
    client.reset_mock()
    await bot._send_offers_to_taker("J5TestTakerNick1")
    client.send_private_message.assert_called_once()
    assert "!tbond" not in client.send_private_message.call_args[0][2]
    assert "!tbond" not in bot._format_offer_announcement(bot.current_offers[0], include_bond=True)


@pytest.mark.parametrize("explicit", [False, True])
async def test_start_installs_the_guard_and_never_selects_a_leased_bond(
    tmp_path: Path, explicit: bool
) -> None:
    best, second = _bond(0xAA, bond_value=2_000), _bond(0xBB, bond_value=1_000)
    _stock(tmp_path, best)
    bot = _bot(tmp_path, selected_fidelity_bond=(best.txid, 0) if explicit else None)
    bot.backend.get_block_height = AsyncMock(return_value=1)
    bot.wallet.sync_all = AsyncMock()
    bot.wallet.sync_with_descriptor_wallet = AsyncMock()
    bot.wallet.reconstruct_imported_state_safe = AsyncMock()
    bot.wallet.get_total_balance = AsyncMock(return_value=10_000_000)
    bot.offer_manager.create_offers = AsyncMock(side_effect=RuntimeError("stop after selection"))
    bonds = AsyncMock(return_value=[best, second])

    with (
        patch("maker.bot.find_fidelity_bonds", bonds),
        patch("maker.fidelity.find_fidelity_bonds", bonds),
        pytest.raises(RuntimeError, match="stop after selection"),
    ):
        await bot.start()

    assert isinstance(bot.bond_lease_guard, BondLeaseGuard)
    assert bot.fidelity_bond is second
    assert bot._advertised_bond() is second
    record = read_advertisements(advertisement_record_path(tmp_path))
    assert record is not None and record.through((second.txid, 0)) == 1


def test_the_record_blocks_rental_through_the_next_period_of_an_advertisement(
    tmp_path: Path,
) -> None:
    path = advertisement_record_path(tmp_path)
    advertised, other = ("aa" * 32, 0), ("bb" * 32, 1)
    assert read_advertisements(path) is None

    record = record_advertisement(path, 5)
    assert read_advertisements(path) == record
    # Unknown history: every bond may have been advertised through the next period.
    assert not record.rentable(other, 6)
    assert record.rentable(other, 7)
    record = record_advertisement(path, 7, advertised)
    assert not record.rentable(advertised, 8)
    assert record.rentable(advertised, 9)
    assert record.rentable(other, 8)
    # Recording again never moves a period back.
    assert record_advertisement(path, 6, advertised) == record
    assert record_advertisement(path, 9, None).through(advertised) == 8
    # A truncated record is unreadable, never an empty history.
    path.write_text('{"since": 7}', encoding="utf-8")
    with pytest.raises(AdvertisementRecordError, match="corrupt"):
        read_advertisements(path)


async def test_an_unreadable_record_suppresses_proofs_and_deleting_it_is_safe(
    tmp_path: Path,
) -> None:
    bond = _bond(0xAA)
    guard = BondLeaseGuard(_store_path(tmp_path), "regtest")
    record_advertisement(guard.record_path, 0, ("cc" * 32, 0))
    guard.record_path.write_text('{"version": 1, "since": -1}', encoding="utf-8")
    with pytest.raises(AdvertisementRecordError, match="corrupt"):
        read_advertisements(guard.record_path)
    assert await guard.refresh([bond], 1, advertising=bond) == frozenset()
    assert not guard.allows(bond)

    guard.record_path.unlink()
    await guard.refresh([bond], 4033, advertising=bond)
    assert guard.allows(bond)
    record = read_advertisements(guard.record_path)
    assert record is not None and not record.rentable(("cc" * 32, 0), 3)
    assert record.rentable(("cc" * 32, 0), 4)
