"""Managed market invoices authorize only their original incoming ring edges."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bitcointx.core.key import CKey
from jmcore.channel_ring import RingNodeBinding
from jmcore.channel_ring_store import RingLifecycleState
from jmswap.buyout_messages import Outpoint
from jmswap.channel_ring_nodes import ChannelRingNodeClaimConflictError
from jmswap.lnd_peer import PaymentRouteHop
from test_market_sales import _sales

from maker.market_sales import MarketSales

LOCAL = bytes(CKey(b"\x61" * 32).pub).hex()
REMOTE = bytes(CKey(b"\x62" * 32).pub).hex()
TXID = "11" * 32
BINDING = RingNodeBinding(
    network="regtest",
    wallet_identity="aa" * 32,
    source_mixdepth=0,
    node_name="n0",
    local_node_id=LOCAL,
)


def setup_sales(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[MarketSales, SimpleNamespace, SimpleNamespace, SimpleNamespace, AsyncMock, MagicMock]:
    sales, bot, _, _ = _sales(tmp_path)
    certificate, macaroon = tmp_path / "tls.cert", tmp_path / "admin.macaroon"
    certificate.write_bytes(b"offline certificate")
    macaroon.write_bytes(b"offline macaroon")
    bot.config.channel_ring.nodes["n0"] = SimpleNamespace(
        lnd_grpc_url="127.0.0.1:10009", lnd_tls_cert_path=certificate, lnd_macaroon_path=macaroon
    )
    record = SimpleNamespace(
        node_binding=BINDING,
        state=RingLifecycleState.CONFIRMED_OPEN,
        active=False,
        manifest=SimpleNamespace(unsigned_txid=TXID),
        incoming_edge=SimpleNamespace(
            funding_outpoint=Outpoint(txid=TXID, vout=1), peer=SimpleNamespace(node_id=REMOTE)
        ),
    )
    report = SimpleNamespace(records=(record,), corruptions=())
    bot._channel_ring_store = SimpleNamespace(load_all=lambda: report)
    bot._channel_ring_nodes = SimpleNamespace(
        for_binding=MagicMock(return_value=SimpleNamespace(binding=BINDING))
    )
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr("jmswap.lnd_peer.LndPeerClient", lambda *_args: client)
    hint = PaymentRouteHop(
        node_id=REMOTE,
        chan_id=42,
        fee_base_msat=1_000,
        fee_proportional_millionths=1,
        cltv_expiry_delta=80,
    )
    resolver = AsyncMock(return_value=((hint,),))
    monkeypatch.setattr("jmswap.ring_invoice_hints.incoming_ring_route_hints", resolver)
    return sales, bot, report, record, resolver, client


async def test_confirmed_open_record_authorizes_only_its_incoming_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sales, bot, _, _, resolver, client = setup_sales(tmp_path, monkeypatch)
    assert await sales._invoice_hints("n0", 100)
    bot._channel_ring_nodes.for_binding.assert_called_once_with(BINDING)
    resolver.assert_awaited_once_with(client, BINDING, {Outpoint(txid=TXID, vout=1): REMOTE}, 100)


@pytest.mark.parametrize(
    "problem", ["corrupt", "pre_verified", "missing_manifest", "wrong_txid", "other_owner"]
)
async def test_incomplete_or_untrusted_provenance_adds_no_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    sales, _, report, record, resolver, client = setup_sales(tmp_path, monkeypatch)
    if problem == "corrupt":
        report.corruptions = (object(),)
    elif problem == "pre_verified":
        record.state = RingLifecycleState.PSBT_VERIFIED
    elif problem == "missing_manifest":
        record.manifest = None
    elif problem == "wrong_txid":
        record.manifest.unsigned_txid = "22" * 32
    else:
        record.node_binding = BINDING.model_copy(update={"node_name": "other"})
    assert await sales._invoice_hints("n0", 100) == ()
    resolver.assert_not_awaited()
    client.__aenter__.assert_not_awaited()


async def test_pool_ownership_rejection_cannot_fall_back_to_current_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sales, bot, _, _, resolver, _ = setup_sales(tmp_path, monkeypatch)
    bot._channel_ring_nodes.for_binding.side_effect = ChannelRingNodeClaimConflictError(
        "wallet mismatch"
    )
    with pytest.raises(ChannelRingNodeClaimConflictError):
        await sales._invoice_hints("n0", 100)
    resolver.assert_not_awaited()
