"""Both pits share P2WSH fidelity bonds derived under BIP84; there are no Taproot bonds."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bitcointx.wallet import CBitcoinExtKey
from jmcore.bitcoin import (
    PSBTInput,
    TxInput,
    TxOutput,
    create_psbt,
    parse_transaction_bytes,
    script_to_p2wsh_address,
    serialize_transaction,
)
from jmcore.btc_script import mk_freeze_script
from jmcore.timenumber import timenumber_to_timestamp

from jmwallet.backends.base import UTXO
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.bip32 import mnemonic_to_seed
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    create_bond_info,
    load_registry,
    save_registry,
)
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import verify_p2wsh_signature

MNEMONIC = "abandon " * 11 + "about"
INDEX = 120
LOCKTIME = timenumber_to_timestamp(INDEX)


def _wallet(address_type: str, data_dir: Path) -> WalletService:
    backend = DescriptorWalletBackend(wallet_name="cross_pit_test")
    backend._wallet_loaded = True
    backend.get_max_descriptor_range = AsyncMock(return_value=0)
    backend.get_addresses_with_history = AsyncMock(return_value=[])
    wallet = WalletService(
        mnemonic=MNEMONIC,
        backend=backend,
        network="regtest",
        address_type=address_type,
        data_dir=data_dir,
        mixdepth_count=1,
    )
    wallet.check_and_upgrade_descriptor_range = AsyncMock(return_value=False)
    return wallet


def _bond(purpose: int) -> tuple[str, bytes, bytes, str]:
    path = f"m/{purpose}'/1'/0'/2/{INDEX}"
    key = CBitcoinExtKey.from_seed(mnemonic_to_seed(MNEMONIC)).derive_path(path)
    pubkey = bytes(key.pub)
    script = mk_freeze_script(pubkey.hex(), LOCKTIME)
    return path, pubkey, script, script_to_p2wsh_address(script, "regtest")


def _bond_psbt(script: bytes) -> bytes:
    return create_psbt(
        2,
        [TxInput(bytes.fromhex("ee" * 32), 0, b"", 0xFFFFFFFE)],
        [TxOutput(199_000, b"\x00\x14" + bytes(20))],
        LOCKTIME,
        [PSBTInput(200_000, b"\x00\x20" + sha256(script).digest(), script)],
    )


def test_both_pits_derive_the_same_bond(tmp_path: Path) -> None:
    sw = _wallet("p2wpkh", tmp_path)
    tr = _wallet("p2tr", tmp_path)
    path, pubkey, script, address = _bond(84)
    assert sw.wallet_fingerprint == tr.wallet_fingerprint
    assert sw.get_address(0, 0, 0) != tr.get_address(0, 0, 0)
    for wallet in (sw, tr):
        assert wallet.get_fidelity_bond_path(INDEX) == path
        assert wallet.get_fidelity_bond_key(0, LOCKTIME).get_public_key_bytes() == pubkey
        assert wallet.get_fidelity_bond_script(0, LOCKTIME) == script
        assert wallet.get_fidelity_bond_address(0, LOCKTIME) == address
        key = wallet.get_key_for_address(address)
        assert key is not None
        assert key.get_public_key_bytes() == pubkey


@pytest.mark.parametrize("address_type", ["p2wpkh", "p2tr"])
@pytest.mark.asyncio
async def test_registered_bond_survives_sync_and_signing(tmp_path: Path, address_type: str) -> None:
    wallet = _wallet(address_type, tmp_path)
    path, pubkey, script, address = _bond(84)
    registry = BondRegistry()
    registry.add_bond(
        create_bond_info(
            address=address,
            locktime=LOCKTIME,
            index=INDEX,
            path=path,
            pubkey_hex=pubkey.hex(),
            witness_script=script,
            network="regtest",
        )
    )
    save_registry(registry, tmp_path, wallet.wallet_fingerprint)
    wallet.backend.get_all_utxos = AsyncMock(
        return_value=[
            UTXO(
                txid="ee" * 32,
                vout=0,
                value=200_000,
                address=address,
                confirmations=10,
                scriptpubkey=(b"\x00\x20" + sha256(script).digest()).hex(),
            )
        ]
    )
    result = await wallet.sync_with_descriptor_wallet(wallet.load_registered_bond_addresses())
    assert len(result[0]) == 1
    utxo = result[0][0]
    assert utxo.path == f"{path}:{LOCKTIME}"
    assert wallet.get_fidelity_bond_addresses_info()[0].path == f"{path}:{LOCKTIME}"
    retained = load_registry(tmp_path, wallet.wallet_fingerprint)
    assert retained.bonds[0].path == path
    assert retained.bonds[0].pubkey == pubkey.hex()
    inputs = [TxInput(bytes.fromhex(utxo.txid)[::-1], 0, b"", 0xFFFFFFFE)]
    outputs = [TxOutput(199_000, b"\x00\x14" + bytes(20))]
    tx = parse_transaction_bytes(serialize_transaction(2, inputs, outputs, LOCKTIME))
    signed = wallet.sign_input(tx, 0, utxo)
    assert signed.witness[1] == script
    assert verify_p2wsh_signature(tx, 0, script, utxo.value, signed.signature, pubkey)


@pytest.mark.parametrize("address_type", ["p2wpkh", "p2tr"])
def test_bond_psbt_recognizes_exact_key_without_registry(tmp_path: Path, address_type: str) -> None:
    wallet = _wallet(address_type, tmp_path)
    path, pubkey, script, _ = _bond(84)
    raw = _bond_psbt(script)
    plan = wallet.prepare_psbt_signing(raw, scan_range=0)
    assert plan.signable_count == 1
    assert plan.inputs[0].utxo is not None
    assert plan.inputs[0].utxo.path == path
    signed = wallet.sign_input(plan.psbt.transaction, 0, plan.inputs[0].utxo)
    assert verify_p2wsh_signature(
        plan.psbt.transaction, 0, script, 200_000, signed.signature, pubkey
    )


def test_bip86_derived_bond_is_not_recognized(tmp_path: Path) -> None:
    """A Taproot wallet never treats a BIP86-derived key as a fidelity bond key."""
    wallet = _wallet("p2tr", tmp_path)
    _, _, script, _ = _bond(86)
    assert wallet.prepare_psbt_signing(_bond_psbt(script), scan_range=0).signable_count == 0
