"""Unit tests for the centralized wallet signing interface (issue #518).

These verify that ``WalletService.sign_input`` is the single place that accesses
private keys, and that it produces correct signatures/witness stacks for both
regular P2WPKH inputs and timelocked P2WSH fidelity bonds.
"""

from __future__ import annotations

from hashlib import sha256

import pytest
from jmcore.bitcoin import TxInput, TxOutput, create_p2tr_scriptpubkey
from jmcore.btc_script import mk_freeze_script
from jmcore.timenumber import timestamp_to_timenumber

from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.signer import SignedInput
from jmwallet.wallet.signing import (
    ParsedTransaction,
    TransactionSigningError,
    create_p2wpkh_script_code,
    verify_p2tr_signature,
    verify_p2wpkh_signature,
)

# A valid timenumber locktime (2020-01-01 00:00:00 UTC, timenumber 0).
BOND_LOCKTIME = 1_577_836_800


def _single_input_tx() -> ParsedTransaction:
    return ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[
            TxInput(
                txid_le=bytes(32),
                vout=0,
                scriptsig=b"",
                sequence=0xFFFFFFFF,
            )
        ],
        outputs=[TxOutput(value=50_000, script=bytes.fromhex("0014" + "00" * 20))],
        locktime=0,
        witnesses=[],
    )


def _p2wpkh_utxo(
    address: str, scriptpubkey: str = "0014" + "bb" * 20, value: int = 100_000
) -> UTXOInfo:
    return UTXOInfo(
        txid="aa" * 32,
        vout=0,
        value=value,
        address=address,
        confirmations=10,
        scriptpubkey=scriptpubkey,
        path="m/84'/0'/0'/0/0",
        mixdepth=0,
    )


class TestSignInputP2WPKH:
    def test_returns_verifiable_signature(self, wallet_service):
        address = wallet_service.get_address(0, 0, 0)
        key = wallet_service.get_key_for_address(address)
        assert key is not None
        from jmcore.bitcoin import pubkey_to_p2wpkh_script

        scriptpubkey = pubkey_to_p2wpkh_script(key.get_public_key_bytes(compressed=True)).hex()
        utxo = _p2wpkh_utxo(address, scriptpubkey)
        tx = _single_input_tx()
        tx.inputs[0].txid_le = bytes.fromhex(utxo.txid)[::-1]

        signed = wallet_service.sign_input(tx, 0, utxo)

        assert isinstance(signed, SignedInput)
        assert signed.signature[-1] == 1  # SIGHASH_ALL
        assert signed.witness == [signed.signature, signed.pubkey]

        script_code = create_p2wpkh_script_code(signed.pubkey)
        assert verify_p2wpkh_signature(
            tx, 0, script_code, utxo.value, signed.signature, signed.pubkey
        )

    def test_missing_key_raises(self, wallet_service):
        # Address that the wallet has never derived -> unknown key.
        utxo = _p2wpkh_utxo("bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m")
        tx = _single_input_tx()

        with pytest.raises(TransactionSigningError, match="Missing key"):
            tx.inputs[0].txid_le = bytes.fromhex(utxo.txid)[::-1]
            wallet_service.sign_input(tx, 0, utxo)

    def test_p2wsh_without_locktime_raises(self, wallet_service):
        address = wallet_service.get_address(0, 0, 0)
        utxo = UTXOInfo(
            txid="cc" * 32,
            vout=0,
            value=100_000,
            address=address,
            confirmations=10,
            # P2WSH scriptpubkey but no locktime metadata.
            scriptpubkey="0020" + "dd" * 32,
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
        tx = _single_input_tx()
        tx.inputs[0].txid_le = bytes.fromhex(utxo.txid)[::-1]

        with pytest.raises(TransactionSigningError, match="locktime not available"):
            wallet_service.sign_input(tx, 0, utxo)

    def test_mismatched_script_raises(self, wallet_service):
        address = wallet_service.get_address(0, 0, 0)
        utxo = _p2wpkh_utxo(address)
        tx = _single_input_tx()
        tx.inputs[0].txid_le = bytes.fromhex(utxo.txid)[::-1]

        with pytest.raises(TransactionSigningError, match="does not match wallet key"):
            wallet_service.sign_input(tx, 0, utxo)

    def test_mismatched_outpoint_raises(self, wallet_service):
        address = wallet_service.get_address(0, 0, 0)
        key = wallet_service.get_key_for_address(address)
        assert key is not None
        from jmcore.bitcoin import pubkey_to_p2wpkh_script

        utxo = _p2wpkh_utxo(
            address,
            pubkey_to_p2wpkh_script(key.get_public_key_bytes(compressed=True)).hex(),
        )

        with pytest.raises(TransactionSigningError, match="does not match transaction input"):
            wallet_service.sign_input(_single_input_tx(), 0, utxo)


class TestSignInputP2TR:
    def test_returns_verifiable_keypath_signature(self, wallet_service):
        key = wallet_service.master_key.derive(f"{wallet_service.root_path}/0'/0/0")
        address = key.get_p2tr_address(wallet_service.network)
        wallet_service.address_cache[address] = (0, 0, 0)
        scriptpubkey = create_p2tr_scriptpubkey(key.get_p2tr_output_xonly())
        utxo = UTXOInfo(
            txid="00" * 32,
            vout=0,
            value=100_000,
            address=address,
            confirmations=10,
            scriptpubkey=scriptpubkey.hex(),
            path=f"{wallet_service.root_path}/0'/0/0",
            mixdepth=0,
        )
        tx = _single_input_tx()

        signed = wallet_service.sign_input(
            tx,
            0,
            utxo,
            prevout_values=[utxo.value],
            prevout_scripts=[scriptpubkey],
        )

        assert len(signed.signature) == 64
        assert signed.witness == [signed.signature]
        assert signed.pubkey == key.get_p2tr_output_xonly()
        assert verify_p2tr_signature(
            tx,
            0,
            [utxo.value],
            [scriptpubkey],
            signed.signature,
            signed.pubkey,
        )


class TestSignInputFidelityBond:
    def test_timelocked_witness_stack(self, wallet_service):
        address = wallet_service.get_fidelity_bond_address(0, BOND_LOCKTIME)
        script = wallet_service.get_fidelity_bond_script(0, BOND_LOCKTIME)
        utxo = UTXOInfo(
            txid="ee" * 32,
            vout=0,
            value=200_000,
            address=address,
            confirmations=10,
            scriptpubkey=(b"\x00\x20" + sha256(script).digest()).hex(),
            path="m/84'/0'/0'/2/0",
            mixdepth=0,
            locktime=BOND_LOCKTIME,
        )
        tx = _single_input_tx()
        tx.inputs[0].txid_le = bytes.fromhex(utxo.txid)[::-1]
        tx.inputs[0].sequence = 0xFFFFFFFE
        tx.locktime = BOND_LOCKTIME

        signed = wallet_service.sign_input(tx, 0, utxo)

        # Witness stack for a timelocked P2WSH is [signature, witness_script].
        assert signed.witness[0] == signed.signature
        assert signed.witness[1] == script

        # The witness script must be the freeze script for this bond's pubkey.
        timenumber = timestamp_to_timenumber(BOND_LOCKTIME)
        bond_key = wallet_service.get_fidelity_bond_key(timenumber, BOND_LOCKTIME)
        expected_script = mk_freeze_script(
            bond_key.get_public_key_bytes(compressed=True).hex(), BOND_LOCKTIME
        )
        assert signed.witness[1] == expected_script

    def test_mismatched_script_raises(self, wallet_service):
        address = wallet_service.get_fidelity_bond_address(0, BOND_LOCKTIME)
        utxo = UTXOInfo(
            txid="ee" * 32,
            vout=0,
            value=200_000,
            address=address,
            confirmations=10,
            scriptpubkey="0020" + "ff" * 32,
            path="m/84'/0'/0'/2/0",
            mixdepth=0,
            locktime=BOND_LOCKTIME,
        )

        tx = _single_input_tx()
        tx.inputs[0].txid_le = bytes.fromhex(utxo.txid)[::-1]
        tx.inputs[0].sequence = 0xFFFFFFFE
        tx.locktime = BOND_LOCKTIME
        with pytest.raises(TransactionSigningError, match="does not match wallet key"):
            wallet_service.sign_input(tx, 0, utxo)

    def test_uppercase_bond_address_uses_lowercase_cache(self, wallet_service):
        address = wallet_service.get_fidelity_bond_address(0, BOND_LOCKTIME)
        script = wallet_service.get_fidelity_bond_script(0, BOND_LOCKTIME)
        utxo = UTXOInfo(
            txid="ee" * 32,
            vout=0,
            value=200_000,
            address=address.upper(),
            confirmations=10,
            scriptpubkey=(b"\x00\x20" + sha256(script).digest()).hex(),
            path="m/84'/0'/0'/2/0",
            mixdepth=0,
            locktime=BOND_LOCKTIME,
        )

        tx = _single_input_tx()
        tx.inputs[0].txid_le = bytes.fromhex(utxo.txid)[::-1]
        tx.inputs[0].sequence = 0xFFFFFFFE
        tx.locktime = BOND_LOCKTIME
        signed = wallet_service.sign_input(tx, 0, utxo)

        assert signed.witness[1] == script
