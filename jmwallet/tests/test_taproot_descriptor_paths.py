"""Core tr() descriptors identify the internal key, never the tweaked output key."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from bitcointx.wallet import CBitcoinExtKey
from jmcore.bitcoin import address_to_scriptpubkey

from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.bip32 import mnemonic_to_seed
from jmwallet.wallet.service import WalletService

MNEMONIC = "abandon " * 11 + "about"


@pytest.mark.parametrize(
    "address_type,purpose,function", [("p2wpkh", 84, "wpkh"), ("p2tr", 86, "tr")]
)
@pytest.mark.parametrize("change,index", [(0, 0), (1, 1200)])
def test_core_internal_key_resolves_without_cached_address(
    address_type: str, purpose: int, function: str, change: int, index: int
) -> None:
    wallet = WalletService(
        MNEMONIC, AsyncMock(spec=BlockchainBackend), network="regtest", address_type=address_type
    )
    reference = CBitcoinExtKey.from_seed(mnemonic_to_seed(MNEMONIC)).derive_path(
        f"m/{purpose}'/1'/2'/{change}/{index}"
    )
    pubkey = bytes(reference.pub)
    if address_type == "p2tr":
        pubkey = pubkey[1:]
    desc = f"{function}([12345678/{change}/{index}]{pubkey.hex()})#checksum"
    mapping = {f"{function}({wallet.get_account_xpub(2)}/{change}/*)": (2, change)}
    assert not wallet.address_cache
    assert wallet._resolve_descriptor_path(desc) == (2, change, index)
    assert wallet._parse_descriptor_path(desc, mapping) == (2, change, index)
    assert wallet._parse_descriptor_path(desc, {}) is None
    assert wallet._parse_descriptor_path(desc, {"unselected": (1, change)}) is None
    assert wallet._resolve_descriptor_path(desc.replace(pubkey.hex(), "00" * len(pubkey))) is None


def test_tr_descriptor_does_not_mistake_output_key_for_internal_key() -> None:
    wallet = WalletService(
        MNEMONIC, AsyncMock(spec=BlockchainBackend), network="regtest", address_type="p2tr"
    )
    key = wallet.master_key.derive("m/86'/1'/0'/0/0")
    tweaked = key.get_p2tr_output_xonly().hex()
    mapping = {f"tr({wallet.get_account_xpub(0)}/0/*)": (0, 0)}
    assert wallet._parse_descriptor_path(f"tr([12345678/0/0]{tweaked})", mapping) is None
    assert wallet._resolve_descriptor_path(f"rawtr([12345678/0/0]{tweaked})") is None
    internal = key.get_public_key_bytes()[1:].hex()
    assert wallet._resolve_descriptor_path(f"rawtr([12345678/0/0]{internal})") is None


@pytest.mark.asyncio
async def test_taproot_scan_keeps_utxo_without_address_fallback() -> None:
    backend = AsyncMock(spec=BlockchainBackend)
    backend.get_block_height.return_value = 100
    wallet = WalletService(
        MNEMONIC, backend, network="regtest", address_type="p2tr", mixdepth_count=1
    )
    key = wallet.master_key.derive("m/86'/1'/0'/1/7")
    address = key.get_p2tr_address("regtest")
    backend.scan_descriptors = AsyncMock(
        return_value={
            "success": True,
            "unspents": [
                {
                    "txid": "aa" * 32,
                    "vout": 1,
                    "amount": 0.001,
                    "height": 95,
                    "scriptPubKey": address_to_scriptpubkey(address).hex(),
                    "desc": f"tr([12345678/1/7]{key.get_public_key_bytes()[1:].hex()})",
                }
            ],
        }
    )
    result = await wallet._sync_all_with_descriptors()
    assert result is not None
    assert len(result[0]) == 1
    assert result[0][0].address == address
    assert result[0][0].path == "m/86'/1'/0'/1/7"
    assert result[0][0].confirmations == 6
