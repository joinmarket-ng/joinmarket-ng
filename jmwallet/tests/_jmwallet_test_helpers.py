"""
Shared test helpers for jmwallet tests.

Constants and factory functions used across jmwallet test files.
Separated from conftest.py to avoid import collisions when running
tests from the monorepo root.
"""

from __future__ import annotations

import os
from dataclasses import replace
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
