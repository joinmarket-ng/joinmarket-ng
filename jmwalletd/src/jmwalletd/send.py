"""Direct-send helper for jmwalletd.

Thin wrapper around :func:`jmwallet.wallet.spend.direct_send` that adapts the
result for the jmwalletd HTTP API response format.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from jmwallet.wallet.spend import (
    DEFAULT_MAX_FEE_RATE_SAT_VB,
    DirectSendResult,
    direct_send,
)

if TYPE_CHECKING:
    from jmwallet.wallet.service import WalletService


async def do_direct_send(
    *,
    wallet_service: WalletService,
    mixdepth: int,
    amount_sats: int,
    destination: str,
    fee_rate: float | None = None,
    fee_target_blocks: int | None = None,
    tx_fee_factor: float = 0.0,
    max_fee_rate_sat_vb: float = DEFAULT_MAX_FEE_RATE_SAT_VB,
) -> DirectSendResult:
    """Build and broadcast a direct (non-coinjoin) transaction.

    Delegates entirely to :func:`jmwallet.wallet.spend.direct_send`.
    ``fee_rate`` (sat/vB) takes priority; otherwise ``fee_target_blocks``
    drives backend estimation (``None`` keeps the spend module's default
    target). ``tx_fee_factor`` applies the reference fee-rate randomization,
    and ``max_fee_rate_sat_vb`` applies the operator's configured hard cap.
    """
    from jmcore.paths import get_default_data_dir
    from jmwalletd._backend import get_backend

    data_dir: Path = wallet_service.data_dir or get_default_data_dir()
    backend = await get_backend(data_dir, wallet_service=wallet_service)

    # Preserve fidelity bond UTXOs on every backend. Plain sync omits branch 2
    # on address-scanning backends and can drop the bond immediately before an
    # expired-bond sweep.
    await wallet_service.sync_with_registered_bonds()

    logger.info(
        "Direct send: {} sats from mixdepth {} to {} (max fee rate {:.2f} sat/vB)",
        amount_sats or "sweep",
        mixdepth,
        destination,
        max_fee_rate_sat_vb,
    )

    extra_kwargs: dict[str, int] = {}
    if fee_target_blocks is not None:
        extra_kwargs["fee_target_blocks"] = fee_target_blocks

    return await direct_send(
        wallet=wallet_service,
        backend=backend,
        mixdepth=mixdepth,
        amount_sats=amount_sats,
        destination=destination,
        fee_rate=fee_rate,
        tx_fee_factor=tx_fee_factor,
        max_fee_rate_sat_vb=max_fee_rate_sat_vb,
        **extra_kwargs,
    )
