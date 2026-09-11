"""Wallet-owned delegated bond credential signing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.btc_script import derive_bond_address
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.credential_market import BondCredential, SignedDocument
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_keys import MarketKeyError, MarketKeysClosedError
from jmcore.timenumber import timestamp_to_timenumber

from jmwallet.backends.base import BlockchainBackend, BondVerificationResult
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService

LOCKTIME = 1_893_456_000  # January 1, 2030 UTC, synthetic regtest bond.
NOW = 1_700_000_000
MNEMONIC = "all " * 11 + "all"
OTHER_MNEMONIC = "abandon " * 11 + "about"


def _wallet_with_bond(
    tmp_path: Path, mnemonic: str, txid: str
) -> tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint]:
    backend = AsyncMock(spec=BlockchainBackend)
    backend.get_block_hash.return_value = GENESIS_BLOCK_HASHES["regtest"]
    backend.get_block_height.return_value = 2017
    backend.get_median_time_past.return_value = NOW - 1
    wallet = WalletService(mnemonic, backend, network="regtest", data_dir=tmp_path)
    outpoint = ExternalPoDLEOutpoint(txid=txid, vout=0)
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
    wallet.sync_all = AsyncMock(side_effect=AssertionError("no sync"))  # type: ignore[method-assign]
    wallet.sync_mixdepth = AsyncMock(side_effect=AssertionError("no sync"))  # type: ignore[method-assign]
    return wallet, backend, outpoint


@pytest.fixture
def seller_wallet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint]:
    monkeypatch.setattr("jmwallet.wallet.service.time.time", lambda: NOW)
    return _wallet_with_bond(tmp_path, MNEMONIC, "11" * 32)


def _certificate_pubkey() -> str:
    return CKey.from_secret_bytes(b"\x02" * 32).pub.hex()


async def test_wallet_issues_verifiable_bond_credential_without_sync_or_key_export(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint],
) -> None:
    wallet, _, outpoint = seller_wallet
    certificate_pubkey = _certificate_pubkey()
    try:
        _, authorization = await wallet.create_market_authorization(outpoint)
        credential = await wallet.create_market_bond_credential(authorization, certificate_pubkey)

        assert type(credential) is BondCredential
        assert credential.cert_pubkey == certificate_pubkey
        assert credential.bond.outpoint == outpoint
        assert credential.cert_expiry == 2
        assert set(credential.model_dump()) == {
            "version",
            "bond",
            "cert_pubkey",
            "cert_expiry",
            "cert_signature",
        }
        credential.verify()
        wallet.sync_all.assert_not_awaited()
        wallet.sync_mixdepth.assert_not_awaited()
    finally:
        await wallet.close()


async def test_wallet_rejects_foreign_or_tampered_authorization(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint], tmp_path: Path
) -> None:
    wallet, backend, _ = seller_wallet
    other, _, other_outpoint = _wallet_with_bond(tmp_path, OTHER_MNEMONIC, "22" * 32)
    try:
        _, foreign_authorization = await other.create_market_authorization(other_outpoint)
        with pytest.raises(MarketKeyError, match="already-known"):
            await wallet.create_market_bond_credential(foreign_authorization, _certificate_pubkey())
        backend.get_block_hash.assert_not_awaited()

        tampered = SignedDocument.model_construct(
            body={**foreign_authorization.body, "seller_pubkey": _certificate_pubkey()},
            signature=foreign_authorization.signature,
        )
        with pytest.raises(ValueError, match="signed market document"):
            await wallet.create_market_bond_credential(tampered, _certificate_pubkey())
    finally:
        await other.close()
        await wallet.close()


async def test_wallet_rejects_invalid_certificate_key_before_chain_validation(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint],
) -> None:
    wallet, backend, outpoint = seller_wallet
    try:
        _, authorization = await wallet.create_market_authorization(outpoint)
        backend.reset_mock()
        with pytest.raises(MarketKeyError, match="certificate public key"):
            await wallet.create_market_bond_credential(authorization, "02" + "00" * 32)
        backend.get_block_hash.assert_not_awaited()
    finally:
        await wallet.close()


async def test_wallet_rejects_authorization_from_an_expired_period(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint],
) -> None:
    wallet, backend, outpoint = seller_wallet
    try:
        _, authorization = await wallet.create_market_authorization(outpoint)
        backend.get_block_height.return_value = 4033
        with pytest.raises(MarketKeyError, match="current wallet authority"):
            await wallet.create_market_bond_credential(authorization, _certificate_pubkey())
    finally:
        await wallet.close()


async def test_wallet_revocation_during_chain_validation_returns_no_credential(
    seller_wallet: tuple[WalletService, AsyncMock, ExternalPoDLEOutpoint],
) -> None:
    wallet, backend, outpoint = seller_wallet
    try:
        _, authorization = await wallet.create_market_authorization(outpoint)

        async def genesis(_height: int) -> str:
            wallet.market_keys.close()
            return GENESIS_BLOCK_HASHES["regtest"]

        backend.get_block_hash.side_effect = genesis
        with pytest.raises(MarketKeysClosedError):
            await wallet.create_market_bond_credential(authorization, _certificate_pubkey())
        backend.verify_bonds.assert_awaited_once()
    finally:
        await wallet.close()
