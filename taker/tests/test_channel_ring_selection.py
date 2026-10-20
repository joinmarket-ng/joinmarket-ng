"""No taker selection mode can silently reuse a node across source mixdepths."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_utxo

from taker.taker import Taker


def selection_taker(*, interactive: bool = False) -> Taker:
    taker = Taker.__new__(Taker)
    taker.config = cast(
        Any,
        SimpleNamespace(
            channel_ring=SimpleNamespace(enabled=True, taker_joins=True, mixdepth_nodes={1: "md1"}),
            select_utxos=interactive,
            taker_utxo_age=1,
        ),
    )
    taker.wallet = Mock()
    taker.wallet.mixdepth_count = 2
    taker.wallet.get_locked_input_outpoints.return_value = set()
    taker.wallet.get_utxo_label_from_wallet.return_value = "test"
    taker._session = cast(Any, SimpleNamespace(last_failure_reason=None))
    return taker


@pytest.mark.parametrize("mixdepth", [None, 0, 2])
async def test_eligibility_rejects_unmapped_mixdepth_before_wallet_queries(
    mixdepth: int | None,
) -> None:
    taker = selection_taker()
    assert await taker.check_utxo_eligibility(500_000, mixdepth) == (
        "Source mixdepth has no configured channel-ring node"
    )
    taker.wallet.get_utxos.assert_not_called()


async def test_explicit_inputs_cannot_bypass_source_binding() -> None:
    taker = selection_taker()
    taker._resolve_explicit_input_utxos = AsyncMock(return_value=[make_utxo(mixdepth=0)])
    assert await taker._prepare_requested_input_selection(500_000, 0, ["a" * 64 + ":0"]) is None
    assert (
        taker._session.last_failure_reason == "Source mixdepth has no configured channel-ring node"
    )


async def test_interactive_selection_marks_unmapped_inputs_unselectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = selection_taker(interactive=True)
    unmapped = make_utxo(mixdepth=0)
    mapped = make_utxo(txid_char="b", mixdepth=1)
    taker.wallet.get_utxos = AsyncMock(side_effect=lambda md: [unmapped] if md == 0 else [mapped])
    select = Mock(return_value=[mapped])
    monkeypatch.setattr("jmwallet.utxo_selector.select_utxos_interactive", select)

    assert await taker._maybe_select_utxos_interactively(0, None) == [mapped]
    assert select.call_args.kwargs["excluded_outpoints"] == {(unmapped.txid, unmapped.vout)}
    # The other mixdepth remains visible as context, not selectable liquidity.
    assert select.call_args.args[0] == [unmapped, mapped]


@pytest.mark.parametrize("mapped_node", [False, True])
async def test_maker_only_ring_accepts_source_without_local_lnd_node(
    mapped_node: bool,
) -> None:
    taker = selection_taker()
    taker.config.channel_ring.taker_joins = False
    if not mapped_node:
        taker.config.channel_ring.mixdepth_nodes = {}
    selected = make_utxo(mixdepth=0)
    taker._resolve_explicit_input_utxos = AsyncMock(return_value=[selected])

    result = await taker._prepare_requested_input_selection(
        500_000, 0, [f"{selected.txid}:{selected.vout}"]
    )

    assert result == ([selected], None, 0)
    assert taker.last_source_mixdepth == 0


@pytest.mark.parametrize("mapped_node", [False, True])
async def test_maker_only_interactive_selection_keeps_unmapped_utxos(
    monkeypatch: pytest.MonkeyPatch,
    mapped_node: bool,
) -> None:
    taker = selection_taker(interactive=True)
    taker.config.channel_ring.taker_joins = False
    if not mapped_node:
        taker.config.channel_ring.mixdepth_nodes = {}
    utxo = make_utxo(mixdepth=0)
    taker.wallet.get_utxos = AsyncMock(side_effect=lambda md: [utxo] if md == 0 else [])
    select = Mock(return_value=[utxo])
    monkeypatch.setattr("jmwallet.utxo_selector.select_utxos_interactive", select)

    assert await taker._maybe_select_utxos_interactively(0, None) == [utxo]
    assert (utxo.txid, utxo.vout) not in select.call_args.kwargs["excluded_outpoints"]
