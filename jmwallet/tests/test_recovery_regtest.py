"""Real Bitcoin Core coverage for wallet recovery and PSBT broadcasting.

These tests require a regtest Bitcoin Core RPC endpoint. They exercise actual
descriptor recovery behavior and mempool acceptance rather than mocked RPCs.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from jmcore.bitcoin import PSBTInput, TxInput, TxOutput, create_psbt
from jmcore.timenumber import timenumber_to_timestamp
from typer.testing import CliRunner

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.backends.offline import OfflineBackend
from jmwallet.cli import app
from jmwallet.cli.mnemonic import (
    FIDELITY_BOND_RECOVERY_COMPLETE,
    FIDELITY_BOND_RECOVERY_PENDING,
    load_mnemonic_meta,
    save_mnemonic_meta,
)
from jmwallet.wallet.bond_registry import load_registry
from jmwallet.wallet.service import WalletService

pytestmark = [pytest.mark.docker, pytest.mark.e2e]

# jmwallet/tests/conftest.py isolates BITCOIN_RPC_URL for each test. Capture
# the usual e2e settings while pytest imports this module, before that fixture
# runs, so explicit BITCOIN_RPC_* overrides still work.
_RPC_CONFIG = {
    "rpc_url": os.environ.get("BITCOIN_RPC_URL", "http://127.0.0.1:18443"),
    "rpc_user": os.environ.get("BITCOIN_RPC_USER", "test"),
    "rpc_password": os.environ.get("BITCOIN_RPC_PASSWORD", "test"),
}
_RPC_TIMEOUT = 120.0
_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)
_BOND_AMOUNT_BTC = 0.001
_BOND_AMOUNT_SATS = 100_000


async def _rpc(
    config: dict[str, str],
    method: str,
    params: list[Any] | None = None,
    wallet: str | None = None,
) -> Any:
    """Make one Core JSON-RPC request using the repository e2e defaults."""
    url = config["rpc_url"].rstrip("/")
    if wallet is not None:
        url = f"{url}/wallet/{wallet}"
    payload = {
        "jsonrpc": "1.0",
        "id": "jmng-recovery-regtest",
        "method": method,
        "params": params or [],
    }
    async with httpx.AsyncClient(timeout=_RPC_TIMEOUT, trust_env=False) as client:
        response = await client.post(
            url,
            auth=(config["rpc_user"], config["rpc_password"]),
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"{method} RPC error: {data['error']}")
    return data["result"]


async def _create_wallet(config: dict[str, str], wallet_name: str) -> None:
    """Create a unique, descriptor-capable Core wallet for this test."""
    await _rpc(config, "createwallet", [wallet_name])


async def _close_backend(backend: DescriptorWalletBackend) -> None:
    """Unload only this test's uniquely named Core wallet before closing HTTP clients."""
    try:
        await backend.unload_wallet()
    except Exception:
        pass
    await backend.close()


def _address_descriptor(descriptors: list[dict[str, Any]], address: str) -> dict[str, Any]:
    base = f"addr({address})"
    matches = [
        descriptor
        for descriptor in descriptors
        if str(descriptor.get("desc", "")).split("#", 1)[0] == base
    ]
    assert len(matches) == 1, f"Expected one imported descriptor for {address}, got {matches}"
    return matches[0]


@pytest_asyncio.fixture
async def regtest_rpc_config() -> dict[str, str]:
    """Provide the usual e2e config and refuse to mutate a non-regtest Core."""
    config = dict(_RPC_CONFIG)
    chain = await _rpc(config, "getblockchaininfo")
    assert chain["chain"] == "regtest", f"Refusing to mutate {chain['chain']!r}, expected regtest"
    return config


@pytest_asyncio.fixture
async def funded_regtest_wallet(
    regtest_rpc_config: dict[str, str],
) -> AsyncGenerator[tuple[dict[str, str], str, str], None]:
    """Yield a unique funded wallet and mine address for one isolated test."""
    wallet_name = f"jmng_recovery_funder_{secrets.token_hex(6)}"
    await _create_wallet(regtest_rpc_config, wallet_name)
    miner_address = await _rpc(
        regtest_rpc_config, "getnewaddress", ["", "bech32"], wallet=wallet_name
    )
    await _rpc(regtest_rpc_config, "generatetoaddress", [101, miner_address], wallet=wallet_name)
    try:
        yield regtest_rpc_config, wallet_name, miner_address
    finally:
        try:
            await _rpc(regtest_rpc_config, "unloadwallet", [wallet_name])
        except Exception:
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("bond", [False, True])
async def test_sign_psbt_local_finalize_and_broadcast(
    funded_regtest_wallet: tuple[dict[str, str], str, str], tmp_path: Path, bond: bool
) -> None:
    """Core accepts locally finalized regular and bond spends without a backend wallet."""
    config, funder_wallet, miner_address = funded_regtest_wallet
    wallet = WalletService(_MNEMONIC, OfflineBackend(), network="regtest", mixdepth_count=1)
    locktime = timenumber_to_timestamp(0) if bond else 0
    address = wallet.get_fidelity_bond_address(0, locktime) if bond else wallet.get_address(0, 0, 0)
    funding_id = await _fund_bond(config, funder_wallet, miner_address, address)
    funding = await _rpc(config, "gettransaction", [funding_id], wallet=funder_wallet)
    decoded = await _rpc(config, "decoderawtransaction", [funding["hex"]])
    prevout = next(v for v in decoded["vout"] if v["scriptPubKey"].get("address") == address)
    destination = await _rpc(config, "getaddressinfo", [miner_address], wallet=funder_wallet)
    raw = create_psbt(
        version=2,
        inputs=[TxInput.from_hex(funding_id, prevout["n"], sequence=0xFFFFFFFE)],
        outputs=[TxOutput(_BOND_AMOUNT_SATS - 1_000, bytes.fromhex(destination["scriptPubKey"]))],
        locktime=locktime,
        psbt_inputs=[
            PSBTInput(
                witness_utxo_value=_BOND_AMOUNT_SATS,
                witness_utxo_script=bytes.fromhex(prevout["scriptPubKey"]["hex"]),
                witness_script=wallet.get_fidelity_bond_script(0, locktime) if bond else b"",
            )
        ],
    )
    # Use an isolated TOML config, not the user's backend or mnemonic settings.
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[bitcoin]\nbackend_type = "descriptor_wallet"\nrpc_url = "{config["rpc_url"]}"\n'
        f'rpc_user = "{config["rpc_user"]}"\nrpc_password = "{config["rpc_password"]}"\n'
    )
    wallets_before = await _rpc(config, "listwallets")
    result = await asyncio.to_thread(
        CliRunner().invoke,
        app,
        [
            "sign-psbt",
            base64.b64encode(raw).decode(),
            "--network",
            "regtest",
            "--scan-range",
            "1",
            "--data-dir",
            str(tmp_path),
            "--config-file",
            str(config_file),
            "--yes",
        ],
        env={"MNEMONIC": _MNEMONIC},
    )
    assert result.exit_code == 0, result.output
    txid = result.stdout.split("Transaction submitted: ", 1)[1].splitlines()[0]
    assert txid in await _rpc(config, "getrawmempool")
    assert await _rpc(config, "listwallets") == wallets_before
    mempool = await _rpc(config, "getmempoolentry", [txid])
    assert round(mempool["fees"]["base"] * 100_000_000) == 1_000


async def _fund_bond(
    config: dict[str, str],
    funder_wallet: str,
    miner_address: str,
    address: str,
) -> str:
    """Fund and confirm a historical bond before its descriptor is imported."""
    txid = str(
        await _rpc(
            config,
            "sendtoaddress",
            [address, _BOND_AMOUNT_BTC],
            wallet=funder_wallet,
        )
    )
    await _rpc(config, "generatetoaddress", [1, miner_address], wallet=funder_wallet)
    return txid


def _new_wallet(
    config: dict[str, str],
    data_dir: Path,
    *,
    mnemonic_file: Path | None = None,
) -> tuple[WalletService, DescriptorWalletBackend]:
    backend = DescriptorWalletBackend(
        rpc_url=config["rpc_url"],
        rpc_user=config["rpc_user"],
        rpc_password=config["rpc_password"],
        wallet_name=f"jmng_recovery_{secrets.token_hex(6)}",
    )
    wallet = WalletService(
        mnemonic=_MNEMONIC,
        backend=backend,
        network="regtest",
        mixdepth_count=1,
        data_dir=data_dir,
        mnemonic_file=mnemonic_file,
    )
    return wallet, backend


@pytest.mark.asyncio
async def test_inactive_bond_import_uses_now_timestamp(
    funded_regtest_wallet: tuple[dict[str, str], str, str], tmp_path: Path
) -> None:
    """An inactive ``addr()`` import records Core's ``now`` timestamp.

    Core applies a safety window around descriptor timestamps, so a transaction
    confirmed immediately before this import can already be visible. The
    separate recovery-rescan test verifies the required historical recovery.
    """
    config, funder_wallet, miner_address = funded_regtest_wallet
    wallet, backend = _new_wallet(config, tmp_path)
    locktime = timenumber_to_timestamp(0)
    address = wallet.get_fidelity_bond_address(0, locktime)
    await _fund_bond(config, funder_wallet, miner_address, address)

    try:
        await backend.create_wallet(disable_private_keys=True)
        await wallet.import_fidelity_bond_addresses([(address, locktime, 0)], rescan=False)

        descriptor = _address_descriptor(await backend.list_descriptors(), address)
        assert descriptor.get("active") is False
        assert "timestamp" in descriptor
    finally:
        await _close_backend(backend)


@pytest.mark.asyncio
async def test_recovery_rescan_finds_historical_inactive_bond_without_timestamp_change(
    funded_regtest_wallet: tuple[dict[str, str], str, str], tmp_path: Path
) -> None:
    """The owned Core rescan finds prior funding without rewriting ``now`` metadata."""
    config, funder_wallet, miner_address = funded_regtest_wallet
    wallet, backend = _new_wallet(config, tmp_path)
    locktime = timenumber_to_timestamp(1)
    address = wallet.get_fidelity_bond_address(1, locktime)
    txid = await _fund_bond(config, funder_wallet, miner_address, address)

    try:
        await backend.create_wallet(disable_private_keys=True)
        await wallet.import_fidelity_bond_addresses([(address, locktime, 1)], rescan=False)
        before = _address_descriptor(await backend.list_descriptors(), address)
        timestamp = before["timestamp"]

        await backend.rescan_for_recovery(0)

        found = [utxo for utxo in await backend.get_utxos([address]) if utxo.txid == txid]
        assert len(found) == 1
        assert found[0].value == _BOND_AMOUNT_SATS
        after = _address_descriptor(await backend.list_descriptors(), address)
        assert after["timestamp"] == timestamp
    finally:
        await _close_backend(backend)


async def _recover_canonical_bond(
    config: dict[str, str],
    funder_wallet: str,
    miner_address: str,
    data_dir: Path,
) -> tuple[WalletService, DescriptorWalletBackend, Path, str, int]:
    """Run the real 960-address explicit recovery flow for one funded canonical bond."""
    mnemonic_file = data_dir / "imported.mnemonic"
    mnemonic_file.write_text(_MNEMONIC)
    save_mnemonic_meta(mnemonic_file, fidelity_bond_recovery=FIDELITY_BOND_RECOVERY_PENDING)
    wallet, backend = _new_wallet(config, data_dir, mnemonic_file=mnemonic_file)
    locktime = timenumber_to_timestamp(2)
    address = wallet.get_fidelity_bond_address(2, locktime)
    txid = await _fund_bond(config, funder_wallet, miner_address, address)

    await wallet.setup_descriptor_wallet(rescan=False)
    recovered = await wallet.recover_fidelity_bonds()
    matches = [utxo for utxo in recovered if utxo.txid == txid and utxo.address == address]
    assert len(matches) == 1
    assert matches[0].value == _BOND_AMOUNT_SATS
    return wallet, backend, mnemonic_file, address, locktime


@pytest.mark.asyncio
async def test_explicit_canonical_recovery_persists_complete_state(
    funded_regtest_wallet: tuple[dict[str, str], str, str], tmp_path: Path
) -> None:
    """A successful real 960-address recovery durably records completion and the bond."""
    config, funder_wallet, miner_address = funded_regtest_wallet
    wallet, backend, mnemonic_file, address, locktime = await _recover_canonical_bond(
        config, funder_wallet, miner_address, tmp_path
    )
    try:
        meta = load_mnemonic_meta(mnemonic_file)
        assert meta[f"fidelity_bond_recovery.{wallet.wallet_fingerprint}"] == (
            FIDELITY_BOND_RECOVERY_COMPLETE
        )
        registered = load_registry(
            tmp_path, wallet.wallet_fingerprint, allow_legacy_fallback=False
        ).get_bond_by_address(address)
        assert registered is not None
        assert registered.locktime == locktime
        assert registered.is_funded
    finally:
        await _close_backend(backend)


@pytest.mark.asyncio
async def test_completed_recovery_does_not_auto_recover_in_fresh_wallet(
    funded_regtest_wallet: tuple[dict[str, str], str, str], tmp_path: Path
) -> None:
    """A fresh process honors durable completion instead of launching another 960 scan."""
    config, funder_wallet, miner_address = funded_regtest_wallet
    first, backend, mnemonic_file, _address, _locktime = await _recover_canonical_bond(
        config, funder_wallet, miner_address, tmp_path
    )
    try:
        fresh = WalletService(
            mnemonic=_MNEMONIC,
            backend=MagicMock(),
            network="regtest",
            mixdepth_count=1,
            data_dir=tmp_path,
            mnemonic_file=mnemonic_file,
        )
        fresh.discover_fidelity_bonds = AsyncMock()  # type: ignore[method-assign]

        await fresh._recover_imported_fidelity_bonds_if_needed()

        assert fresh.wallet_fingerprint == first.wallet_fingerprint
        fresh.discover_fidelity_bonds.assert_not_awaited()
    finally:
        await _close_backend(backend)
