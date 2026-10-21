"""PoDLE sales from the maker's current offer mixdepth."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import address_to_scriptpubkey
from jmcore.credential_market import BondReference, bond_resource
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.market_keys import WalletMarketKeys
from jmcore.market_store import MarketStore, MarketStoreConflictError
from jmcore.models import NetworkType
from jmcore.paths import get_market_store_path, get_used_commitments_path
from jmcore.settings import JoinMarketSettings
from jmwallet.backends.base import UTXO
from jmwallet.wallet.bip32 import mnemonic_to_seed
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from loguru import logger
from typer.testing import CliRunner

from maker import cli, market_sales
from maker.bond_leases import advertisement_record_path, record_advertisement
from maker.fidelity import FidelityBondInfo
from maker.market_sales import MarketSales, MarketSalesUnavailableError
from maker.offers import offer_mixdepth

MNEMONIC = "abandon " * 11 + "about"
TIP = 1_000


class _Backend:
    def __init__(self) -> None:
        self.chain: dict[tuple[str, int], UTXO] = {}
        self.fee: float | None = None

    def requires_neutrino_metadata(self) -> bool:
        return False

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        return self.chain.get((txid, vout))

    async def get_block_height(self) -> int:
        return TIP

    def can_estimate_fee(self) -> bool:
        return self.fee is not None

    async def estimate_fee(self, _target: int) -> float:
        assert self.fee is not None
        return self.fee

    async def close(self) -> None:
        return None


class _OfferManager:
    def __init__(self) -> None:
        self.mixdepth: int | None = 1

    async def current_offer_mixdepth(self) -> int | None:
        return self.mixdepth


class _Seller:
    def __init__(self) -> None:
        self.inventory: list[ExternalPoDLE] = []
        self.prices: list[int] = []
        self.conflict = False
        self.sold: dict[bytes, ExternalPoDLE] = {}
        self.leases_sold: set[bytes] = set()
        self.bond_prices: list[tuple[str, int]] = []
        self.bond_inventory: list[str] = []

    def sold_podle(self, payment_hash: bytes) -> ExternalPoDLE | None:
        return self.sold.get(payment_hash)

    def sold_product(self, payment_hash: bytes) -> str | None:
        if payment_hash in self.sold:
            return "podle"
        return "bond" if payment_hash in self.leases_sold else None

    def add_bond_inventory(self, bond: ExternalPoDLEOutpoint) -> None:
        if bond.txid in self.bond_inventory:
            raise MarketStoreConflictError("inventory resource has already been recorded")
        self.bond_inventory.append(bond.txid)

    def add_podle_inventory(self, credential: ExternalPoDLE) -> None:
        if self.conflict:
            raise MarketStoreConflictError("PoDLE commitment already has an owner")
        self.inventory.append(credential)

    def set_price_sats(self, price_sats: int, *, bond: ExternalPoDLEOutpoint | None = None) -> None:
        if bond is None:
            self.prices.append(price_sats)
        else:
            self.bond_prices.append((bond.txid, price_sats))


def _wallet(tmp_path: Path) -> tuple[WalletService, _Backend]:
    backend = _Backend()
    wallet = WalletService(
        MNEMONIC, backend, network="regtest", data_dir=tmp_path, address_type="p2tr"
    )
    return wallet, backend


def _coin(
    wallet: WalletService,
    backend: _Backend,
    mixdepth: int,
    index: int,
    *,
    confirmations: int = 6,
    frozen: bool = False,
    locktime: int | None = None,
    on_chain: bool = True,
) -> UTXOInfo:
    address = wallet.get_receive_address(mixdepth, index)
    script = address_to_scriptpubkey(address).hex()
    txid = f"{mixdepth + 1:x}{index:x}".ljust(64, "a")
    height = TIP - confirmations + 1
    utxo = UTXOInfo(
        txid=txid,
        vout=0,
        value=50_000 + index,
        address=address,
        confirmations=confirmations,
        scriptpubkey=script,
        path=f"m/86'/1'/{mixdepth}'/0/{index}",
        mixdepth=mixdepth,
        height=height,
        frozen=frozen,
        locktime=locktime,
    )
    wallet.utxo_cache.setdefault(mixdepth, []).append(utxo)
    if on_chain:
        backend.chain[(txid, 0)] = UTXO(
            txid=txid,
            vout=0,
            value=utxo.value,
            address=address,
            confirmations=confirmations,
            scriptpubkey=script,
            height=height,
        )
    return utxo


def _sales(
    tmp_path: Path, *, price: int | None = None
) -> tuple[MarketSales, SimpleNamespace, _Seller, _Backend]:
    wallet, backend = _wallet(tmp_path)
    config = SimpleNamespace(
        channel_ring=SimpleNamespace(nodes={}, mixdepth_nodes={0: "n0", 1: "n1"}),
        podle_price_sats=price,
        sell_podle=True,
        rent_bonds=False,
        bond_rent_rate=0.01,
    )
    bot = SimpleNamespace(
        wallet=wallet,
        config=config,
        running=True,
        fidelity_bond=None,
        offer_manager=_OfferManager(),
    )
    sales = MarketSales(JoinMarketSettings(data_dir=tmp_path), bot)  # type: ignore[arg-type]
    seller = _Seller()
    sales._seller = seller  # type: ignore[assignment]
    return sales, bot, seller, backend


def test_offer_mixdepth_is_the_largest_positive_balance() -> None:
    assert offer_mixdepth({0: 5, 1: 9, 2: 0}) == 1
    assert offer_mixdepth({0: 0, 1: 0}) is None
    assert offer_mixdepth({}) is None


async def test_sells_every_index_of_offer_mixdepth_coins_only(tmp_path: Path) -> None:
    sales, bot, seller, backend = _sales(tmp_path, price=700)
    wallet = bot.wallet
    sold = _coin(wallet, backend, 1, 0)
    young = _coin(wallet, backend, 1, 1, confirmations=2)
    frozen = _coin(wallet, backend, 1, 2, frozen=True)
    bond = _coin(wallet, backend, 1, 3, locktime=1_900_000_000)
    other = _coin(wallet, backend, 0, 0)

    await sales.refresh()

    assert sales.mixdepth == 1
    assert [c.index for c in seller.inventory] == [0, 1, 2]
    assert {c.outpoint.txid for c in seller.inventory} == {sold.txid}
    assert all(c.blockheight == sold.height and c.network == "regtest" for c in seller.inventory)
    assert sales._backing_sats(sold.txid, 0) == sold.value
    for unsold in (young, frozen, bond, other):
        assert sales._backing_sats(unsold.txid, 0) is None
    # A configured price is kept as configured.
    assert seller.prices == []

    await sales.refresh()
    assert len(seller.inventory) == 3


async def test_a_coin_is_stocked_only_once_its_backing_verifies(tmp_path: Path) -> None:
    sales, bot, seller, backend = _sales(tmp_path, price=700)
    coin = _coin(bot.wallet, backend, 1, 0, on_chain=False)

    await sales.refresh()
    assert seller.inventory == []

    backend.chain[(coin.txid, 0)] = UTXO(
        txid=coin.txid,
        vout=0,
        value=coin.value,
        address=coin.address,
        confirmations=coin.confirmations,
        scriptpubkey=coin.scriptpubkey,
        height=coin.height,
    )
    await sales.refresh()
    assert len(seller.inventory) == 3


async def test_already_owned_commitments_are_skipped(tmp_path: Path) -> None:
    sales, bot, seller, backend = _sales(tmp_path, price=700)
    coin = _coin(bot.wallet, backend, 1, 0)
    seller.conflict = True

    await sales.refresh()

    assert seller.inventory == [] and sales._backing_sats(coin.txid, 0) == coin.value


async def test_each_sale_is_invoiced_from_its_own_coins_node(tmp_path: Path) -> None:
    sales, bot, seller, backend = _sales(tmp_path, price=700)
    old = _coin(bot.wallet, backend, 1, 0)
    new = _coin(bot.wallet, backend, 0, 0)
    await sales.refresh()
    reserved_before_switch = b"\x01" * 32
    seller.sold[reserved_before_switch] = seller.inventory[0]

    bot.offer_manager.mixdepth = 0
    await sales.refresh()

    # A quote reserved before the switch keeps its coin and so its coin's node.
    assert sales._invoice_node(reserved_before_switch) == "n1"
    assert sales.mixdepth == 0
    assert sales._backing_sats(new.txid, 0) == new.value
    assert sales._backing_sats(old.txid, 0) is None
    assert {c.outpoint.txid for c in seller.inventory} == {old.txid, new.txid}
    # Unknown sales get no invoice at all.
    assert sales._invoice_node(b"\x02" * 32) is None
    # A spent coin's reservation can still be canceled on its own node.
    bot.wallet.utxo_cache[1].remove(old)
    assert sales._invoice_node(reserved_before_switch) == "n1"
    del bot.wallet.address_cache[old.address]
    assert sales._invoice_node(reserved_before_switch) is None

    # An offer mixdepth without a node sells nothing.
    bot.offer_manager.mixdepth = 2
    await sales.refresh()
    assert sales.mixdepth is None


async def test_a_held_payment_for_a_spent_coin_is_refunded(tmp_path: Path) -> None:
    sales, bot, seller, backend = _sales(tmp_path, price=700)
    coin = _coin(bot.wallet, backend, 1, 0)
    await sales.refresh()
    payment_hash = b"\x03" * 32
    seller.sold[payment_hash] = seller.inventory[0]
    hold = SimpleNamespace(payment_hash=payment_hash, product="podle")

    assert await sales._sale_check(hold) is True  # type: ignore[arg-type]
    del backend.chain[(coin.txid, 0)]
    assert await sales._sale_check(hold) is False  # type: ignore[arg-type]


async def test_default_price_follows_the_fee_rate(tmp_path: Path) -> None:
    sales, _bot, seller, backend = _sales(tmp_path)
    await sales.refresh()
    backend.fee = 10.0
    await sales.refresh()
    # Half of twice a 111 vB P2TR spend's fee.
    assert seller.prices == [111, 1_110]


async def test_missing_preconditions_pause_sales_with_a_reason(tmp_path: Path) -> None:
    sales, bot, *_ = _sales(tmp_path)
    sales._seller = None
    with pytest.raises(MarketSalesUnavailableError, match="owned fidelity bond"):
        await sales._ensure_seller()
    # A leased (or cold-certificate) bond cannot authorize sales: no owner key.
    bot.fidelity_bond = SimpleNamespace(txid="11" * 32, vout=0, private_key=None)
    with pytest.raises(MarketSalesUnavailableError, match="owned fidelity bond"):
        await sales._ensure_seller()
    bot.fidelity_bond = SimpleNamespace(txid="11" * 32, vout=0, private_key=object())
    with pytest.raises(MarketSalesUnavailableError, match="activate-market-ledger"):
        await sales._ensure_seller()
    assert not get_market_store_path(tmp_path).exists()

    commitments = get_used_commitments_path(tmp_path)
    with MarketStore(get_market_store_path(tmp_path), wallet_id=bot.wallet.market_wallet_id) as s:
        s.activate_wallet(commitments, history_confirmed=True)
    with pytest.raises(MarketSalesUnavailableError, match="no Lightning node"):
        await sales._ensure_seller()


async def test_run_warns_once_and_keeps_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(market_sales, "RETRY_SECONDS", 0.01)
    sales, *_ = _sales(tmp_path)
    sales._seller = None
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        sales.start()
        await asyncio.sleep(0.1)
        await sales.stop()
    finally:
        logger.remove(sink)
    assert len(warnings) == 1 and "owned fidelity bond" in warnings[0]


def _cli_settings(tmp_path: Path) -> JoinMarketSettings:
    settings = JoinMarketSettings(data_dir=tmp_path)
    settings.network_config.network = NetworkType.REGTEST
    return settings


def _patch_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(cli, "setup_cli", lambda *args, **kwargs: _cli_settings(tmp_path))
    monkeypatch.setattr("jmcore.process_hardening.harden_current_process", lambda: None)
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: SimpleNamespace(mnemonic=MNEMONIC, bip39_passphrase=""),
    )
    keys = WalletMarketKeys(mnemonic_to_seed(MNEMONIC, ""), "regtest")
    return keys.ledger_identity()


def _ledger_state(tmp_path: Path, wallet_id: str) -> str:
    return MarketStore.diagnose_wallet(
        get_market_store_path(tmp_path),
        wallet_id=wallet_id,
        commitments_path=get_used_commitments_path(tmp_path),
    )["state"]


def test_ledger_activation_needs_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cli(tmp_path, monkeypatch)

    result = CliRunner().invoke(
        cli.app, ["activate-market-ledger", "--data-dir", str(tmp_path)], input="n\n"
    )

    assert result.exit_code == 1, result.output
    assert "complete" in result.output
    assert not get_market_store_path(tmp_path).exists()


def test_ledger_activation_is_explicit_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wallet_id = _patch_cli(tmp_path, monkeypatch)
    arguments = ["activate-market-ledger", "--data-dir", str(tmp_path), "--yes"]

    result = CliRunner().invoke(cli.app, arguments)
    assert result.exit_code == 0, result.output
    assert _ledger_state(tmp_path, wallet_id) == "ready"

    again = CliRunner().invoke(cli.app, arguments)
    assert again.exit_code == 0, again.output
    assert "already active" in again.output


async def test_started_seller_gets_the_routed_invoices_backing_and_sale_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import taker.wallet_market as wallet_market
    from jmcore.channel_ring import ChannelRingNodeConfig
    from taker.market_settlement import MultiNodeHoldInvoices

    sales, bot, *_ = _sales(tmp_path)
    sales._seller = None
    bot.fidelity_bond = SimpleNamespace(txid="11" * 32, vout=0, private_key=object())
    with MarketStore(get_market_store_path(tmp_path), wallet_id=bot.wallet.market_wallet_id) as s:
        s.activate_wallet(get_used_commitments_path(tmp_path), history_confirmed=True)
    (tmp_path / "tls.cert").write_bytes(b"cert")
    (tmp_path / "admin.macaroon").write_bytes(b"macaroon")
    bot.config.channel_ring.nodes = {
        "n1": ChannelRingNodeConfig(
            lnd_grpc_url="127.0.0.1:10009",
            lnd_tls_cert_path=tmp_path / "tls.cert",
            lnd_macaroon_path=tmp_path / "admin.macaroon",
            onion_endpoint="a" * 56 + ".onion:9735",
        )
    }
    # A configured [market] node is probed for earlier invoices.
    market = sales._settings.market
    market.lnd_grpc_url = "127.0.0.1:10010"
    market.lnd_tls_cert_path = str(tmp_path / "tls.cert")
    market.lnd_macaroon_path = str(tmp_path / "admin.macaroon")
    started: list[dict[str, Any]] = []

    class _Started:
        running = True

        def __init__(self, *_args: object, **kwargs: Any) -> None:
            started.append(kwargs)

        async def start(self) -> None:
            return None

    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _Started)
    await sales._ensure_seller()

    [kwargs] = started
    assert isinstance(kwargs["invoices"], MultiNodeHoldInvoices)
    assert set(kwargs["invoices"]._clients) == {"n1", market_sales.MARKET_NODE}
    assert kwargs["invoices"]._route == sales._invoice_node
    assert kwargs["backing_sats"] == sales._backing_sats
    assert kwargs["sale_check"] == sales._sale_check
    # The same period keeps the running seller.
    await sales._ensure_seller()
    assert len(started) == 1


LOCKTIME = 4_102_444_800
BOND_KEY = CKey(bytes([0x11]) * 32)


def _owned_bond(txid_byte: int, *, locktime: int = LOCKTIME, hot: bool = True) -> FidelityBondInfo:
    return FidelityBondInfo(
        txid=f"{txid_byte:02x}" * 32,
        vout=0,
        value=100_000_000,
        locktime=locktime,
        confirmation_time=1_704_067_200,
        bond_value=1,
        pubkey=bytes(BOND_KEY.pub),
        private_key=BOND_KEY if hot else None,
    )


def test_a_lease_lasts_and_is_priced_for_the_rest_of_its_period() -> None:
    assert market_sales.lease_blocks(1) == 2016
    assert market_sales.lease_blocks(2016) == 1
    # 1% a year on 1 BTC, for a full period of 2016 of 52560 blocks a year.
    assert market_sales.lease_price_sats(100_000_000, 0.01, 1) == 38_357
    assert market_sales.lease_price_sats(100_000_000, 0.01, 2016) == 20
    assert market_sales.lease_price_sats(1, 0.01, 2016) == 1
    # New leases are stocked in the first half of a period only.
    assert market_sales.lease_admission_open(2016 + 1008)
    assert not market_sales.lease_admission_open(2016 + 1009)


async def test_rents_only_idle_hot_bonds_after_every_period_they_may_have_been_advertised_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sales, bot, *_ = _sales(tmp_path)
    advertised, idle, other = _owned_bond(0xA1), _owned_bond(0xB2), _owned_bond(0xD4)
    cold = _owned_bond(0xC3, hot=False)
    expiring = _owned_bond(0xE5, locktime=int(time.time()) + 86_400)
    owned = [advertised, idle, cold, other, expiring]
    monkeypatch.setattr(market_sales, "find_fidelity_bonds", AsyncMock(return_value=owned))
    bot.fidelity_bond = advertised
    path = advertisement_record_path(tmp_path)
    # Created in period 0: any bond may have been advertised through period 1.
    record_advertisement(path, 0)
    assert await sales._rentals(1) == []
    assert await sales._rentals(2017) == []
    # The lease guard records the advertised bond in period 2, so through period 3.
    record_advertisement(path, 2, (advertised.txid, 0))
    assert await sales._rentals(4033) == [idle, other]
    # Not advertised now, but it may have been earlier in period 3.
    bot.fidelity_bond = None
    assert await sales._rentals(6049) == [idle, other]
    assert await sales._rentals(8065) == [advertised, idle, other]


async def test_after_half_a_period_only_already_stocked_leases_stay_for_sale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sales, *_ = _sales(tmp_path)
    stocked, fresh = _owned_bond(0xB2), _owned_bond(0xD4)
    monkeypatch.setattr(
        market_sales, "find_fidelity_bonds", AsyncMock(return_value=[stocked, fresh])
    )
    record_advertisement(advertisement_record_path(tmp_path), 0)
    reference = BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=stocked.txid, vout=0),
        pubkey=bytes(BOND_KEY.pub).hex(),
        locktime=LOCKTIME,
    )
    with MarketStore(get_market_store_path(tmp_path)) as store:
        store.add_inventory("bond", bond_resource(reference, 2), None)

    assert await sales._rentals(4032 + 1008) == [stocked, fresh]
    assert await sales._rentals(4032 + 1009) == [stocked]


async def test_a_bond_served_or_stocked_this_period_stays_whatever_its_admission_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sales, *_ = _sales(tmp_path)
    stocked, fresh = _owned_bond(0xB2), _owned_bond(0xE5)
    # Never admissible: its timelock ends too soon after the lease.
    served = _owned_bond(0xD4, locktime=int(time.time()) + 86_400)
    monkeypatch.setattr(
        market_sales, "find_fidelity_bonds", AsyncMock(return_value=[stocked, served, fresh])
    )
    path = advertisement_record_path(tmp_path)
    record_advertisement(path, 0)
    reference = BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid=stocked.txid, vout=0),
        pubkey=bytes(BOND_KEY.pub).hex(),
        locktime=LOCKTIME,
    )
    with MarketStore(get_market_store_path(tmp_path)) as store:
        store.add_inventory("bond", bond_resource(reference, 2), None)
    sales._period, sales._rented = 2, [served]

    assert await sales._rentals(4033) == [stocked, served, fresh]
    # An unreadable record admits nothing, but no held sale loses its authority.
    path.write_text("{", encoding="utf-8")
    assert await sales._rentals(4033) == [stocked, served]
    # Neither is kept into the next period.
    assert await sales._rentals(6049) == []


async def test_rental_pauses_without_a_mixdepth_0_node_or_a_readable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sales, bot, *_ = _sales(tmp_path)
    bond = _owned_bond(0xB2)
    monkeypatch.setattr(market_sales, "find_fidelity_bonds", AsyncMock(return_value=[bond]))
    path = advertisement_record_path(tmp_path)
    record_advertisement(path, 0)
    warnings: list[str] = []
    sink = logger.add(lambda m: warnings.append(m.record["message"]), level="WARNING")
    try:
        bot.config.channel_ring.mixdepth_nodes = {1: "n1"}
        assert await sales._rentals(4033) == []
        bot.config.channel_ring.mixdepth_nodes = {0: "n0"}
        path.write_text("{", encoding="utf-8")
        assert await sales._rentals(4033) == []
        # Deleting the record is safe: recreated, it pauses rental through the next period.
        path.unlink()
        assert await sales._rentals(4033) == []
        assert await sales._rentals(6049) == []
        assert await sales._rentals(8065) == [bond]
    finally:
        logger.remove(sink)
    assert any("mixdepth 0" in w for w in warnings)
    assert any("corrupt" in w for w in warnings)


async def test_rented_bonds_are_repriced_stocked_once_and_invoiced_from_mixdepth_0(
    tmp_path: Path,
) -> None:
    sales, _bot, seller, _backend = _sales(tmp_path)
    bond = _owned_bond(0xB2)
    sales._podle_served = False
    sales._rented = [bond]

    await sales.refresh()
    await sales.refresh()

    price = market_sales.lease_price_sats(bond.value, 0.01, TIP)
    assert seller.bond_prices == [(bond.txid, price)] * 2
    assert seller.bond_inventory == [bond.txid]
    # A renting-only seller neither prices nor stocks PoDLEs.
    assert seller.prices == [] and seller.inventory == []
    lease = b"\x04" * 32
    seller.leases_sold.add(lease)
    assert sales._invoice_node(lease) == "n0"


async def test_each_rented_bond_gets_its_own_authority_and_changes_rebuild_the_seller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import taker.wallet_market as wallet_market
    from jmcore.channel_ring import ChannelRingNodeConfig

    sales, bot, *_ = _sales(tmp_path)
    sales._seller = None
    bot.config.sell_podle = False
    bot.config.rent_bonds = True
    first, second = _owned_bond(0xB2), _owned_bond(0xD4)
    rentals = [first, second]
    monkeypatch.setattr(sales, "_rentals", AsyncMock(side_effect=lambda _height: list(rentals)))
    with MarketStore(get_market_store_path(tmp_path), wallet_id=bot.wallet.market_wallet_id) as s:
        s.activate_wallet(get_used_commitments_path(tmp_path), history_confirmed=True)
    (tmp_path / "tls.cert").write_bytes(b"cert")
    (tmp_path / "admin.macaroon").write_bytes(b"macaroon")
    bot.config.channel_ring.nodes = {
        "n0": ChannelRingNodeConfig(
            lnd_grpc_url="127.0.0.1:10009",
            lnd_tls_cert_path=tmp_path / "tls.cert",
            lnd_macaroon_path=tmp_path / "admin.macaroon",
            onion_endpoint="a" * 56 + ".onion:9735",
        )
    }
    started: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _Started:
        running = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            started.append((args, kwargs))

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(wallet_market, "WalletMarketSeller", _Started)
    await sales._ensure_seller()
    [(args, kwargs)] = started
    assert (args[2].bond.txid, args[2].products) == (first.txid, ["bond"])
    assert args[2].price_sats == market_sales.lease_price_sats(first.value, 0.01, TIP)
    assert [(o.bond.txid, o.products) for o in kwargs["additional"]] == [(second.txid, ["bond"])]
    await sales._ensure_seller()
    assert len(started) == 1

    # A change of the rented set rebuilds the runtime.
    rentals.pop()
    await sales._ensure_seller()
    assert len(started) == 2 and started[1][1]["additional"] == []
    # With PoDLE sales too, the advertised bond sells PoDLEs and the others are rented.
    bot.config.sell_podle = True
    bot.fidelity_bond = SimpleNamespace(txid="11" * 32, vout=0, private_key=object())
    await sales._ensure_seller()
    args, kwargs = started[2]
    assert (args[2].bond.txid, args[2].products) == ("11" * 32, ["podle"])
    assert [o.bond.txid for o in kwargs["additional"]] == [first.txid]

    bot.config.sell_podle = False
    rentals.clear()
    with pytest.raises(MarketSalesUnavailableError, match="no idle fidelity bond"):
        await sales._ensure_seller()
