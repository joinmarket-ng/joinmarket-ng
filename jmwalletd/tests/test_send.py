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
        max_fee_rate_sat_vb=1_000.0,
    )
