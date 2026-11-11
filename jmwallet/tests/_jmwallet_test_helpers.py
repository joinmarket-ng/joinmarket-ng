"""
Shared test helpers for jmwallet tests.

Constants and factory functions used across jmwallet test files.
Separated from conftest.py to avoid import collisions when running
tests from the monorepo root.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, replace
from typing import Any

from jmcore.bitcoin import TxInput, TxOutput, get_txid, serialize_transaction

from jmwallet.wallet.psbt import parse_witness_utxo

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)

TEST_RPC_URL = os.getenv("BITCOIN_RPC_URL", "http://localhost:18443")
TEST_RPC_USER = "test"
TEST_RPC_PASSWORD = "test"

TEST_BOND_ADDRESS = "bc1qxl3vzaf0cxwl9c0jsyyphwdekc6j0xh48qlfv8ja39qzqn92u7ws5arznw"
TEST_BOND_LOCKTIME = 1736899200  # 2025-01-15 00:00:00 UTC

# Fake txid used across multiple tests (64 hex chars)
TEST_FAKE_TXID = "abc123" * 10 + "ab"


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def psbt_input_with_parent(
    tx_input: TxInput, records: list[tuple[bytes, bytes]]
) -> tuple[TxInput, list[tuple[bytes, bytes]]]:
    """Replace a fixture outpoint with a consistent synthetic funding transaction.

    The original outpoint seeds the parent to keep distinct fixture inputs
    distinct. Callers can mutate the resulting records to test authentication.
    """
    witness = next((value for key, value in records if key == b"\x01"), None)
    if witness is None:
        return tx_input, list(records)
    utxo = parse_witness_utxo(witness)
    outputs = [TxOutput(0, b"\x6a") for _ in range(tx_input.vout)]
    outputs.append(TxOutput(utxo.value, utxo.script_pubkey))
    parent = serialize_transaction(2, [TxInput.from_hex(tx_input.txid, 0)], outputs, 0)
    authenticated = replace(tx_input, txid_le=bytes.fromhex(get_txid(parent.hex()))[::-1])
    return authenticated, [(b"\x00", parent), *records]


def make_mock_rpc(
    responses: dict[str, Any],
    *,
    default: Any = None,
    strict: bool = True,
) -> Any:
    """Create a mock RPC dispatcher function from a method->response mapping.

    Args:
        responses: Dict mapping RPC method names to their return values.
                   Values can be callables ``(params, use_wallet) -> Any``
                   for dynamic responses, or plain values returned as-is.
        default: Value returned for methods not in *responses* when
                 *strict* is False.
        strict: If True (default), raise ``ValueError`` for unknown methods.

    Returns:
        An async function compatible with ``backend._rpc_call``.

    Example::

        backend._rpc_call = make_mock_rpc({
            "listwallets": ["test_wallet"],
            "getblockchaininfo": {"blocks": 1000},
        })
    """

    async def _mock_rpc(
        method: str,
        params: list[Any] | None = None,
        client: Any = None,
        use_wallet: bool = True,
    ) -> Any:
        if method in responses:
            value = responses[method]
            if callable(value):
                return value(params, use_wallet)
            return value
        if strict:
            raise ValueError(f"Unexpected RPC method: {method}")
        return default

    return _mock_rpc


# ---------------------------------------------------------------------------
# Curses test doubles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyEvent:
    """A key read and the terminal dimensions in effect for the next frame."""

    height: int
    width: int
    key: int


@dataclass(frozen=True)
class Frame:
    """A completed curses framebuffer captured by :class:`StrictScreen`."""

    height: int
    width: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class StrictScreen:
    """Small curses-window double that rejects off-screen writes and extra input reads."""

    def __init__(self, height: int, width: int, events: list[KeyEvent]) -> None:
        self._height = height
        self._width = width
        self._events = deque(events)
        self._rows: list[list[str]] = []
        self.frames: list[Frame] = []
        self.clear()

    @property
    def pending_events(self) -> int:
        return len(self._events)

    def getmaxyx(self) -> tuple[int, int]:
        return self._height, self._width

    def clear(self) -> None:
        self._rows = [[" "] * self._width for _ in range(self._height)]

    def erase(self) -> None:
        self.clear()

    def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
        if y < 0 or x < 0 or y >= self._height or x + len(text) > self._width:
            raise AssertionError(
                f"off-screen write at ({y}, {x}) for {len(text)} chars on "
                f"{self._height}x{self._width} screen"
            )
        self._rows[y][x : x + len(text)] = text

    def addnstr(self, y: int, x: int, text: str, count: int, attr: int = 0) -> None:
        self.addstr(y, x, text[:count], attr)

    def attron(self, _attr: int) -> None:
        pass

    def attroff(self, _attr: int) -> None:
        pass

    def keypad(self, _enabled: bool) -> None:
        pass

    def nodelay(self, _enabled: bool) -> None:
        pass

    def refresh(self) -> None:
        self.frames.append(
            Frame(self._height, self._width, tuple("".join(row) for row in self._rows))
        )

    def getch(self) -> int:
        if not self._events:
            raise AssertionError("TUI requested input after scripted events were exhausted")
        event = self._events.popleft()
        self._height = event.height
        self._width = event.width
        return event.key
