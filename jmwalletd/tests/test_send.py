"""Tests for the jmwalletd direct-send adapter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmwalletd.send import do_direct_send


@pytest.mark.asyncio
@patch("jmwalletd.send.direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_refreshes_registered_bonds(
    mock_get_backend: AsyncMock,
    mock_direct_send: AsyncMock,
) -> None:
    wallet = MagicMock()
    wallet.data_dir = None
    wallet.sync = AsyncMock()
    wallet.sync_with_registered_bonds = AsyncMock()
    backend = MagicMock()
    result = MagicMock()
    mock_get_backend.return_value = backend
    mock_direct_send.return_value = result

    actual = await do_direct_send(
        wallet_service=wallet,
        mixdepth=0,
        amount_sats=0,
        destination="bcrt1qdestination",
    )

    assert actual is result
    wallet.sync_with_registered_bonds.assert_awaited_once_with()
    wallet.sync.assert_not_awaited()
    mock_direct_send.assert_awaited_once_with(
        wallet=wallet,
        backend=backend,
        mixdepth=0,
        amount_sats=0,
        destination="bcrt1qdestination",
        fee_rate=None,
        max_fee_rate_sat_vb=1_000.0,
    )


@pytest.mark.asyncio
@patch("jmwalletd.send.direct_send", new_callable=AsyncMock)
@patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
async def test_direct_send_forwards_fee_overrides(
    mock_get_backend: AsyncMock,
    mock_direct_send: AsyncMock,
) -> None:
    """Fee overrides (configset [POLICY] tx_fees, issue #566) reach direct_send."""
    wallet = MagicMock()
    wallet.data_dir = None
    wallet.sync_with_registered_bonds = AsyncMock()
    mock_get_backend.return_value = MagicMock()
    mock_direct_send.return_value = MagicMock()

    await do_direct_send(
        wallet_service=wallet,
        mixdepth=1,
        amount_sats=5000,
        destination="bcrt1qdestination",
        fee_rate=2.5,
    )
    assert mock_direct_send.call_args.kwargs["fee_rate"] == 2.5
    assert "fee_target_blocks" not in mock_direct_send.call_args.kwargs

    await do_direct_send(
        wallet_service=wallet,
        mixdepth=1,
        amount_sats=5000,
        destination="bcrt1qdestination",
        fee_target_blocks=6,
    )
    assert mock_direct_send.call_args.kwargs["fee_rate"] is None
    assert mock_direct_send.call_args.kwargs["fee_target_blocks"] == 6
