"""Hold invoices spread over several LND nodes (one per mixdepth)."""

from __future__ import annotations

from types import TracebackType
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from _taker_market_helpers import FakeHoldInvoices
from jmcore.credential_market import MarketError
from jmswap.lnd_invoices import payment_hash_for
from jmswap.lnd_peer import InvoiceState, PaymentRouteHop

from taker.market_settlement import MultiNodeHoldInvoices

PREIMAGE = bytes.fromhex("11" * 32)
HASH = payment_hash_for(PREIMAGE)


class _Node(FakeHoldInvoices):
    def __init__(self) -> None:
        super().__init__()
        self.private: list[bool] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _Node:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exited = True

    async def add_hold_invoice(
        self, payment_hash: bytes, amount_sats: int, expiry_seconds: int, *, private: bool = False
    ) -> str:
        self.private.append(private)
        return await super().add_hold_invoice(
            payment_hash, amount_sats, expiry_seconds, private=private
        )


class _Route:
    def __init__(self, node: str | None) -> None:
        self.node = node

    def __call__(self, _payment_hash: bytes) -> str | None:
        return self.node


def _invoices(route: _Route, **nodes: _Node) -> MultiNodeHoldInvoices:
    return MultiNodeHoldInvoices(cast(dict[str, Any], nodes), route)


async def test_invoices_are_private_and_created_on_the_routed_node() -> None:
    a, b = _Node(), _Node()
    route = _Route(None)
    async with _invoices(route, a=a, b=b) as invoices:
        assert a.entered and b.entered
        with pytest.raises(MarketError, match="No Lightning node may invoice"):
            await invoices.add_hold_invoice(HASH, 100, 600)
        route.node = "c"
        with pytest.raises(MarketError, match="No Lightning node may invoice"):
            await invoices.add_hold_invoice(HASH, 100, 600)
        route.node = "b"
        await invoices.add_hold_invoice(HASH, 100, 600)
        assert HASH in b.invoices and HASH not in a.invoices
        assert b.private == [True]
        # A retry reaches the node that already holds the invoice.
        route.node = "a"
        await invoices.add_hold_invoice(HASH, 100, 600)
        assert HASH not in a.invoices
    assert a.exited and b.exited


async def test_a_restarted_runtime_finds_old_invoices_on_their_node() -> None:
    a, b = _Node(), _Node()
    async with _invoices(_Route("a"), a=a, b=b) as invoices:
        await invoices.add_hold_invoice(HASH, 100, 600)
    a.pay(HASH, 100)
    # A fresh runtime whose route now names b still settles on node a.
    async with _invoices(_Route("b"), a=a, b=b) as invoices:
        status = await invoices.lookup(HASH)
        assert status is not None and status.state is InvoiceState.ACCEPTED
        await invoices.settle(PREIMAGE)
    assert a.invoices[HASH].state is InvoiceState.SETTLED
    assert HASH not in b.invoices


async def test_an_unreachable_node_makes_an_unknown_invoice_an_error() -> None:
    a, b = _Node(), _Node()
    async with _invoices(_Route("a"), a=a, b=b) as invoices:
        assert await invoices.lookup(HASH) is None
        b.fail.add("lookup")
        # Node b may hold it, so neither a lookup nor a new invoice may assume not.
        with pytest.raises(RuntimeError, match="injected lookup"):
            await invoices.lookup(HASH)
        with pytest.raises(RuntimeError, match="injected lookup"):
            await invoices.add_hold_invoice(HASH, 100, 600)
        with pytest.raises(RuntimeError, match="injected lookup"):
            await invoices.cancel(HASH)
        assert a.invoices == {}


async def test_a_found_invoice_does_not_need_every_node() -> None:
    a, b = _Node(), _Node()
    await a.add_hold_invoice(HASH, 100, 600)
    b.fail.add("lookup")
    async with _invoices(_Route("b"), b=b, a=a) as invoices:
        assert await invoices.lookup(HASH) is not None
        await invoices.cancel(HASH)
    assert a.invoices[HASH].state is InvoiceState.CANCELED


async def test_an_invoice_no_node_knows_cannot_be_settled() -> None:
    async with _invoices(_Route("a"), a=_Node()) as invoices:
        with pytest.raises(MarketError, match="No Lightning node"):
            await invoices.settle(PREIMAGE)


class _HintedNode(_Node):
    def __init__(self) -> None:
        super().__init__()
        self.invoice_hints: list[tuple[tuple[PaymentRouteHop, ...], ...]] = []

    async def add_hold_invoice(
        self,
        payment_hash: bytes,
        amount_sats: int,
        expiry_seconds: int,
        *,
        private: bool = False,
        invoice_route_hints: tuple[tuple[PaymentRouteHop, ...], ...] = (),
    ) -> str:
        self.invoice_hints.append(invoice_route_hints)
        return await super().add_hold_invoice(
            payment_hash, amount_sats, expiry_seconds, private=private
        )


def _hints() -> tuple[tuple[PaymentRouteHop, ...], ...]:
    return (
        (
            PaymentRouteHop(
                node_id="0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
                chan_id=42,
                fee_base_msat=1_000,
                fee_proportional_millionths=1,
                cltv_expiry_delta=80,
            ),
        ),
    )


async def test_new_hints_are_resolved_only_on_the_selected_original_owner() -> None:
    a, b = _Node(), _HintedNode()
    route = _Route("b")
    resolver = AsyncMock(return_value=_hints())
    async with MultiNodeHoldInvoices(
        cast(dict[str, Any], {"a": a, "b": b}), route, invoice_hints=resolver
    ) as invoices:
        first = await invoices.add_hold_invoice(HASH, 100, 600)
        resolver.assert_awaited_once_with("b", 100)
        assert b.invoice_hints == [_hints()] and a.invoices == {}
        route.node = "a"
        assert await invoices.add_hold_invoice(HASH, 100, 600) == first
        assert b.invoice_hints == [_hints()] and resolver.await_count == 1


async def test_old_invoice_recovery_does_not_require_new_hint_authority() -> None:
    a, b = _Node(), _HintedNode()
    original = await a.add_hold_invoice(HASH, 100, 600)
    resolver = AsyncMock(side_effect=AssertionError("old binding is unavailable"))
    async with MultiNodeHoldInvoices(
        cast(dict[str, Any], {"a": a, "b": b}), _Route("b"), invoice_hints=resolver
    ) as invoices:
        assert await invoices.add_hold_invoice(HASH, 100, 600) == original
        resolver.assert_not_awaited()
        assert b.invoices == {}


@pytest.mark.parametrize("amount,expiry", [(0, 600), (False, 600), (100, 0), (100, 3601)])
async def test_existing_invoice_recovery_preserves_request_validation(
    amount: int, expiry: int
) -> None:
    node = _Node()
    await node.add_hold_invoice(HASH, 100, 600)
    resolver = AsyncMock(side_effect=AssertionError("no new hint authority"))
    async with MultiNodeHoldInvoices(
        cast(dict[str, Any], {"a": node}), _Route("a"), invoice_hints=resolver
    ) as invoices:
        with pytest.raises(ValueError, match="amount_sats|expiry_seconds"):
            await invoices.add_hold_invoice(HASH, amount, expiry)
    resolver.assert_not_awaited()
    assert node.private == [False]


async def test_hint_failure_retains_owner_before_awaiting_metadata() -> None:
    a, b = _Node(), _HintedNode()
    route = _Route("b")
    resolver = AsyncMock(side_effect=OSError("policy lookup unavailable"))
    async with MultiNodeHoldInvoices(
        cast(dict[str, Any], {"a": a, "b": b}), route, invoice_hints=resolver
    ) as invoices:
        with pytest.raises(OSError):
            await invoices.add_hold_invoice(HASH, 100, 600)
        assert a.invoices == b.invoices == {}
        route.node = "a"
        resolver.side_effect = None
        resolver.return_value = _hints()
        await invoices.add_hold_invoice(HASH, 100, 600)
        assert HASH in b.invoices and HASH not in a.invoices
        assert [call.args[0] for call in resolver.await_args_list] == ["b", "b"]
