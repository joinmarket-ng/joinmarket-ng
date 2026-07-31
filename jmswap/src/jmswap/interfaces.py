from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from jmswap.models import FundingOutput, InvoiceDetails


class LightningBackend(Protocol):
    async def create_hold_invoice(self, amount_sat: int, payment_hash: str) -> str: ...

    async def create_invoice(self, amount_sat: int) -> tuple[str, InvoiceDetails]: ...

    async def decode_invoice(self, invoice: str) -> InvoiceDetails: ...

    async def payment_in_flight_or_paid(self, payment_hash: str) -> bool: ...


class ChainBackend(Protocol):
    async def current_height(self) -> int: ...

    async def funding_output(self, txid: str, vout: int) -> FundingOutput | None: ...

    async def broadcast(self, raw_transaction: str) -> str: ...


class RpcAdapter(Protocol):
    async def request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]: ...


EncryptedRequestCallback = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class DirectOnionAdapter:
    """Authenticated encrypted callback carrying the canonical method and payload."""

    def __init__(self, callback: EncryptedRequestCallback) -> None:
        self._callback = callback

    async def request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._callback(method, payload)


class HttpAdapter(DirectOnionAdapter):
    """HTTP can use the same transport-neutral canonical callback boundary."""
