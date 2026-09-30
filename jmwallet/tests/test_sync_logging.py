"""Tests for wallet synchronization log levels and history-failure diagnostics."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest
from _jmwallet_test_helpers import TEST_BOND_ADDRESS, TEST_BOND_LOCKTIME
from loguru import logger

from jmwallet.backends.base import UTXO
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.service import WalletService


@contextmanager
def _captured_logs() -> Generator[list[tuple[str, str]], None, None]:
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append((message.record["level"].name, message.record["message"])),
        level="DEBUG",
    )
    try:
        yield records
    finally:
        logger.remove(sink_id)


def _assert_debug_only(records: list[tuple[str, str]], fragments: list[str]) -> None:
    for fragment in fragments:
        levels = [level for level, message in records if fragment in message]
        assert levels, f"Expected a log containing {fragment!r}"
        assert set(levels) == {"DEBUG"}


@pytest.mark.asyncio
async def test_legacy_sync_summaries_are_debug(test_mnemonic: str) -> None:
    backend = MagicMock()
    backend.supports_descriptor_scan = False
    backend.supports_watch_address = False
    wallet = WalletService(test_mnemonic, backend, network="regtest", mixdepth_count=1)
    wallet.sync_mixdepth = AsyncMock(return_value=[])  # type: ignore[method-assign]

    with _captured_logs() as records:
        await wallet.sync_all()
        await wallet.sync_all()

    _assert_debug_only(records, ["Syncing all mixdepths", "Sync complete: 0 total UTXOs"])


@pytest.mark.asyncio
async def test_descriptor_scan_summaries_are_debug(test_mnemonic: str) -> None:
    backend = MagicMock()
    backend.supports_descriptor_scan = True
    backend.supports_watch_address = False
    backend.get_block_height = AsyncMock(return_value=100)
    backend.scan_descriptors = AsyncMock(return_value={"success": True, "unspents": []})
    wallet = WalletService(
        test_mnemonic,
        backend,
        network="regtest",
        mixdepth_count=1,
        scan_range=1,
    )

    with _captured_logs() as records:
        await wallet.sync_all()
        await wallet.sync_all()

    _assert_debug_only(records, ["Syncing all mixdepths", "Descriptor sync complete"])


@pytest.mark.asyncio
async def test_loaded_descriptor_wallet_sync_summaries_are_debug(test_mnemonic: str) -> None:
    backend = DescriptorWalletBackend(wallet_name="test_sync_logging")
    backend._wallet_loaded = True
    backend._descriptors_imported = True
    backend.get_max_descriptor_range = AsyncMock(return_value=-1)  # type: ignore[method-assign]
    backend.get_all_utxos = AsyncMock(return_value=[])  # type: ignore[method-assign]
    backend.get_addresses_with_history = AsyncMock(return_value=set())  # type: ignore[method-assign]
    wallet = WalletService(test_mnemonic, backend, network="regtest", mixdepth_count=1)
    wallet.check_and_upgrade_descriptor_range = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )

    try:
        with _captured_logs() as records:
            await wallet.sync_with_descriptor_wallet()
            await wallet.sync_with_descriptor_wallet()
    finally:
        await backend.close()

    _assert_debug_only(
        records,
        ["Syncing via descriptor wallet", "Descriptor wallet sync complete"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout(""),
        httpx.ConnectTimeout(""),
        httpx.PoolTimeout(""),
        httpx.ReadError(""),
        ValueError("private wallet history details"),
    ],
    ids=["read-timeout", "connect-timeout", "pool-timeout", "read-error", "rpc-error"],
)
async def test_history_failure_logs_exception_class_without_private_details(
    test_mnemonic: str, tmp_path: Path, error: Exception
) -> None:
    backend = DescriptorWalletBackend(wallet_name="test_history_logging")
    backend._wallet_loaded = True
    backend._descriptors_imported = True
    backend.get_max_descriptor_range = AsyncMock(return_value=-1)  # type: ignore[method-assign]
    backend.get_all_utxos = AsyncMock(return_value=[])  # type: ignore[method-assign]
    rpc = AsyncMock(side_effect=error)
    backend._rpc_call = rpc  # type: ignore[method-assign]
    wallet = WalletService(
        test_mnemonic, backend, network="regtest", mixdepth_count=1, scan_range=1, data_dir=tmp_path
    )
    wallet.check_and_upgrade_descriptor_range = AsyncMock(  # type: ignore[method-assign]
        return_value=False
    )
    used_address = wallet.get_address(0, 0, 0)
    wallet._record_history_address(used_address)
    records: list[tuple[str, bool, bool]] = []
    sink_id = logger.add(
        lambda message: records.append(
            (
                message.record["message"],
                message.record["extra"].get("sensitive", False),
                message.record["exception"] is not None,
            )
        ),
        level="ERROR",
    )
    try:
        with pytest.raises(type(error)) as raised:
            await backend.get_addresses_with_history()
        assert raised.value is error
        assert await wallet.sync_with_descriptor_wallet() == {0: []}
    finally:
        logger.remove(sink_id)
        await backend.close()

    ordinary = [message for message, sensitive, _ in records if not sensitive]
    exception_name = type(error).__name__
    assert any(f"listsinceblock failed ({exception_name})" in message for message in ordinary)
    assert any(
        f"Could not fetch addresses with history ({exception_name})" in message
        and "in-memory enumeration is incomplete" in message
        for message in ordinary
    )
    assert all("private wallet history details" not in message for message in ordinary)
    assert all(used_address not in message for message in ordinary)
    assert any(sensitive and has_exception for _, sensitive, has_exception in records)
    assert wallet.addresses_with_history == {used_address}
    assert wallet.metadata_store is not None
    assert wallet.metadata_store.get_used_addresses() == {used_address}
    assert rpc.call_args_list == [
        call("listsinceblock", ["", 1, True, True, True]),
        call("listsinceblock", ["", 1, True, True, True]),
    ]


@pytest.mark.asyncio
async def test_rediscovered_fidelity_bond_logs_are_debug(test_mnemonic: str) -> None:
    backend = MagicMock()
    backend.supports_watch_address = False
    backend.get_utxos = AsyncMock(
        return_value=[
            UTXO(
                txid="ab" * 32,
                vout=0,
                value=1_000_000,
                address=TEST_BOND_ADDRESS,
                confirmations=6,
                scriptpubkey="0020" + "cd" * 32,
                height=100,
            )
        ]
    )
    wallet = WalletService(test_mnemonic, backend, network="mainnet", mixdepth_count=1)
    bond = (TEST_BOND_ADDRESS, TEST_BOND_LOCKTIME, 0)

    with _captured_logs() as records:
        await wallet.sync_fidelity_bonds([TEST_BOND_LOCKTIME], bond_addresses=[bond])
        await wallet.sync_fidelity_bonds([TEST_BOND_LOCKTIME], bond_addresses=[bond])

    _assert_debug_only(records, ["Found fidelity bond UTXO:", "Found 1 fidelity bond UTXOs"])
