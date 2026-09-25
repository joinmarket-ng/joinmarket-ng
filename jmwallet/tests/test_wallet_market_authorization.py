"""Authorize wallet-native sellers without exporting keys or initiating sync."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jmcore.btc_script import derive_bond_address
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.credential_market import verify_authorization
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_keys import MarketKeyError, MarketKeysClosedError
from jmcore.timenumber import timestamp_to_timenumber

from jmwallet.backends.base import BlockchainBackend, BondVerificationResult
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService

LOCKTIME = 1_893_456_000  # January 1, 2030 UTC, synthetic regtest bond.
NOW = 1_700_000_000


@pytest.fixture
def seller_wallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint]:
    monkeypatch.setattr("jmwallet.wallet.service.time.time", lambda: NOW)
    backend = AsyncMock(spec=BlockchainBackend)
    backend.get_block_hash.return_value = GENESIS_BLOCK_HASHES["regtest"]
    backend.get_block_height.return_value = 2017
    backend.get_median_time_past.return_value = NOW - 1
    wallet = WalletService("all " * 11 + "all", backend, network="regtest", data_dir=tmp_path)
    outpoint = ExternalPoDLEOutpoint(txid="11" * 32, vout=0)
    key = wallet.get_fidelity_bond_key(0, LOCKTIME)
    address = derive_bond_address(key.get_public_key_bytes(compressed=True), LOCKTIME, "regtest")
    wallet.utxo_cache[0] = [
        UTXOInfo(
            txid=outpoint.txid,
            vout=outpoint.vout,
            value=100_000,
            address=address.address,
            confirmations=2,
            scriptpubkey=address.scriptpubkey.hex(),
            path=f"{wallet.root_path}/0'/2/{timestamp_to_timenumber(LOCKTIME)}",
            mixdepth=0,
            locktime=LOCKTIME,
            frozen=True,
        )
    ]
    backend.verify_bonds.return_value = [
        BondVerificationResult(
            txid=outpoint.txid,
            vout=outpoint.vout,
            value=100_000,
            confirmations=2,
            block_time=NOW - 100,
            valid=True,
        )
    ]
    monkeypatch.setattr(wallet, "sync_all", AsyncMock(side_effect=AssertionError("no sync")))
    monkeypatch.setattr(wallet, "sync_mixdepth", AsyncMock(side_effect=AssertionError("no sync")))
    return wallet, backend, outpoint


async def test_authorization_uses_owned_bond_and_verified_scope(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint], tmp_path: Path
) -> None:
    wallet, backend, outpoint = seller_wallet
    try:
        bound, document = await wallet.create_market_authorization(outpoint)
        authority = verify_authorization(document)
        assert authority.bond.outpoint == outpoint
        assert authority.seller_pubkey == bound.signing_public_key().hex()
        assert (
            authority.bond.pubkey
            == wallet.get_fidelity_bond_key(0, LOCKTIME).get_public_key_bytes(compressed=True).hex()
        )
        assert bound.scope.chain_hash == GENESIS_BLOCK_HASHES["regtest"]
        assert bound.scope.period == authority.period == 1
        assert bound.scope.bond == outpoint
        bound.validate_seller_authorization(authority)
        _, repeated = await wallet.create_market_authorization(outpoint)
        assert repeated == document
        request = backend.verify_bonds.call_args.args[0][0]
        assert request.scriptpubkey == wallet.utxo_cache[0][0].scriptpubkey
        assert not (tmp_path / "market").exists()
        assert not (tmp_path / "cmtdata").exists()
    finally:
        await wallet.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("confirmations", 0),
        ("value", 0),
        ("locktime", None),
        ("path", "m/84'/1'/0'/0/0"),
        ("address", "not-the-owned-address"),
        ("scriptpubkey", "0020" + "00" * 32),
        ("mixdepth", 1),
    ],
)
async def test_unusable_or_foreign_cached_bond_is_rejected(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint],
    field: str,
    value: object,
) -> None:
    wallet, backend, outpoint = seller_wallet
    setattr(wallet.utxo_cache[0][0], field, value)
    try:
        with pytest.raises(MarketKeyError):
            await wallet.create_market_authorization(outpoint)
        backend.verify_bonds.assert_not_awaited()
    finally:
        await wallet.close()


async def test_missing_cache_does_not_trigger_sync(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint],
) -> None:
    wallet, backend, outpoint = seller_wallet
    wallet.utxo_cache.clear()
    try:
        with pytest.raises(MarketKeyError, match="already-known"):
            await wallet.create_market_authorization(outpoint)
        backend.get_block_hash.assert_not_awaited()
    finally:
        await wallet.close()


@pytest.mark.parametrize("failure", ["genesis", "height", "clock", "spent", "outpoint", "value"])
async def test_chain_verification_failure_never_authorizes(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint], failure: str
) -> None:
    wallet, backend, outpoint = seller_wallet
    if failure == "genesis":
        backend.get_block_hash.return_value = GENESIS_BLOCK_HASHES["signet"]
    elif failure == "height":
        backend.get_block_height.return_value = 0
    elif failure == "clock":
        backend.get_median_time_past.return_value = LOCKTIME
    elif failure == "spent":
        backend.verify_bonds.return_value[0].valid = False
    elif failure == "outpoint":
        backend.verify_bonds.return_value[0].vout = 1
    else:
        backend.verify_bonds.return_value[0].value = 200_000
    try:
        with pytest.raises((MarketKeyError, ValueError)):
            await wallet.create_market_authorization(outpoint)
        if failure == "genesis":
            backend.verify_bonds.assert_not_awaited()
    finally:
        await wallet.close()


async def test_expired_local_clock_rejects_authorization(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet, _, outpoint = seller_wallet
    monkeypatch.setattr("jmwallet.wallet.service.time.time", lambda: LOCKTIME)
    try:
        with pytest.raises(MarketKeyError, match="no longer timelocked"):
            await wallet.create_market_authorization(outpoint)
    finally:
        await wallet.close()


async def test_revocation_during_backend_lookup_returns_no_authorization(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint],
) -> None:
    wallet, backend, outpoint = seller_wallet

    async def genesis(_height: int) -> str:
        wallet.market_keys.close()
        return GENESIS_BLOCK_HASHES["regtest"]

    backend.get_block_hash.side_effect = genesis
    try:
        with pytest.raises(MarketKeysClosedError):
            await wallet.create_market_authorization(outpoint)
        backend.verify_bonds.assert_not_awaited()
    finally:
        await wallet.close()
