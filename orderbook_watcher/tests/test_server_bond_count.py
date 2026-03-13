"""
Tests for unique fidelity bond counting in the server orderbook output.

Verifies that the per-directory bond_offer_count counts unique bonds (by UTXO),
not offers-with-bonds. A maker with dual offers (relative + absolute) backed by
the same bond should only count as one bond.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from jmcore.models import FidelityBond, Offer, OfferType, OrderBook
from jmcore.settings import OrderbookWatcherSettings

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.server import OrderbookServer

BOND_TXID = "a" * 64
BOND_TXID_2 = "b" * 64

BOND_DATA = {"utxo_txid": BOND_TXID, "utxo_vout": 0}
BOND_DATA_2 = {"utxo_txid": BOND_TXID_2, "utxo_vout": 0}

DUMMY_SCRIPT = "0020" + "ab" * 32


def _make_bond(counterparty: str, utxo_txid: str, utxo_vout: int = 0) -> FidelityBond:
    return FidelityBond(
        counterparty=counterparty,
        utxo_txid=utxo_txid,
        utxo_vout=utxo_vout,
        locktime=100_000,
        script=DUMMY_SCRIPT,
        utxo_confirmations=1000,
        cert_expiry=2_000_000,
    )


def _make_server() -> OrderbookServer:
    settings = OrderbookWatcherSettings()
    aggregator = MagicMock(spec=OrderbookAggregator)
    aggregator.directory_nodes = [("dir1.onion", 5222)]
    aggregator.mempool_api_url = "http://dummy.api"
    aggregator.node_statuses = {}
    aggregator.clients = {}
    return OrderbookServer(settings, aggregator)


def _make_offer(
    counterparty: str,
    oid: int,
    ordertype: OfferType,
    directory_node: str,
    bond_data: dict | None = None,
) -> Offer:
    return Offer(
        counterparty=counterparty,
        oid=oid,
        ordertype=ordertype,
        minsize=27300,
        maxsize=100_000_000,
        txfee=0,
        cjfee="0.0001" if ordertype == OfferType.SW0_RELATIVE else 250,
        fidelity_bond_data=bond_data,
        directory_node=directory_node,
        directory_nodes=[directory_node],
    )


def test_dual_offers_same_bond_counted_once() -> None:
    """A maker with two offers (rel + abs) backed by the same bond -> bond_offer_count=1."""
    server = _make_server()

    offers = [
        _make_offer("maker1", 0, OfferType.SW0_RELATIVE, "dir1.onion:5222", BOND_DATA),
        _make_offer("maker1", 1, OfferType.SW0_ABSOLUTE, "dir1.onion:5222", BOND_DATA),
    ]
    bonds = [_make_bond("maker1", BOND_TXID)]

    orderbook = OrderBook(
        offers=offers,
        fidelity_bonds=bonds,
        timestamp=datetime.now(UTC),
        directory_nodes=["dir1.onion:5222"],
    )

    result = server._format_orderbook(orderbook)

    stats = result["directory_stats"]["dir1.onion:5222"]
    assert stats["offer_count"] == 2
    assert stats["bond_offer_count"] == 1  # One unique bond, not two offers

    # The top-level fidelitybonds list should also have exactly 1 entry
    assert len(result["fidelitybonds"]) == 1


def test_two_makers_different_bonds_counted_separately() -> None:
    """Two makers with different bonds -> bond_offer_count=2."""
    server = _make_server()

    offers = [
        _make_offer("maker1", 0, OfferType.SW0_RELATIVE, "dir1.onion:5222", BOND_DATA),
        _make_offer("maker2", 0, OfferType.SW0_RELATIVE, "dir1.onion:5222", BOND_DATA_2),
    ]
    bonds = [
        _make_bond("maker1", BOND_TXID),
        _make_bond("maker2", BOND_TXID_2),
    ]

    orderbook = OrderBook(
        offers=offers,
        fidelity_bonds=bonds,
        timestamp=datetime.now(UTC),
        directory_nodes=["dir1.onion:5222"],
    )

    result = server._format_orderbook(orderbook)

    stats = result["directory_stats"]["dir1.onion:5222"]
    assert stats["bond_offer_count"] == 2


def test_offers_without_bonds_not_counted() -> None:
    """Offers without fidelity bonds should not inflate the bond count."""
    server = _make_server()

    offers = [
        _make_offer("maker1", 0, OfferType.SW0_RELATIVE, "dir1.onion:5222", BOND_DATA),
        _make_offer("maker1", 1, OfferType.SW0_ABSOLUTE, "dir1.onion:5222", BOND_DATA),
        _make_offer("maker2", 0, OfferType.SW0_RELATIVE, "dir1.onion:5222", None),
    ]
    bonds = [_make_bond("maker1", BOND_TXID)]

    orderbook = OrderBook(
        offers=offers,
        fidelity_bonds=bonds,
        timestamp=datetime.now(UTC),
        directory_nodes=["dir1.onion:5222"],
    )

    result = server._format_orderbook(orderbook)

    stats = result["directory_stats"]["dir1.onion:5222"]
    assert stats["offer_count"] == 3
    assert stats["bond_offer_count"] == 1  # Only maker1's unique bond


def test_dual_offers_two_makers_same_directory() -> None:
    """Two makers each with dual offers (4 total offers) -> bond_offer_count=2."""
    server = _make_server()

    offers = [
        _make_offer("maker1", 0, OfferType.SW0_RELATIVE, "dir1.onion:5222", BOND_DATA),
        _make_offer("maker1", 1, OfferType.SW0_ABSOLUTE, "dir1.onion:5222", BOND_DATA),
        _make_offer("maker2", 0, OfferType.SW0_RELATIVE, "dir1.onion:5222", BOND_DATA_2),
        _make_offer("maker2", 1, OfferType.SW0_ABSOLUTE, "dir1.onion:5222", BOND_DATA_2),
    ]
    bonds = [
        _make_bond("maker1", BOND_TXID),
        _make_bond("maker2", BOND_TXID_2),
    ]

    orderbook = OrderBook(
        offers=offers,
        fidelity_bonds=bonds,
        timestamp=datetime.now(UTC),
        directory_nodes=["dir1.onion:5222"],
    )

    result = server._format_orderbook(orderbook)

    stats = result["directory_stats"]["dir1.onion:5222"]
    assert stats["offer_count"] == 4
    assert stats["bond_offer_count"] == 2  # Two unique bonds, not four offers
