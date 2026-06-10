"""Tests for spending received silent payment outputs in a CoinJoin.

Received silent payments have no BIP32 path, so the wallet must surface them as
ordinary UTXOs for coin selection and recompute their key-path signing key on
demand from the stored tweaks.
"""

from __future__ import annotations

import hashlib

from coincurve import PrivateKey, PublicKey
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    create_p2tr_scriptpubkey,
    pubkey_to_p2tr_address,
)
from jmcore.silentpayments import SilentPaymentAddress, SilentPaymentInput, create_outputs

from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import (
    SIGHASH_DEFAULT,
    sign_p2tr_input,
    verify_p2tr_signature,
)
from jmwallet.wallet.silent_payment_scan import SilentPaymentReceived


def _spend_tx(spk: bytes) -> ParsedTransaction:
    return ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[TxInput(txid_le=bytes(32), vout=0, scriptsig=b"", sequence=0xFFFFFFFF)],
        outputs=[TxOutput(value=90000, script=spk)],
        locktime=0,
        witnesses=[],
    )


def _receive_silent_payment(wallet_service: WalletService) -> SilentPaymentReceived:
    """Construct a real incoming silent payment to ``wallet_service``."""
    sp = wallet_service.get_silent_payments()

    sender_priv = 0xC0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE01
    pub = PublicKey.from_secret(sender_priv.to_bytes(32, "big")).format(compressed=True)
    pubkey_hash = hashlib.new("ripemd160", hashlib.sha256(pub).digest()).digest()
    vin = SilentPaymentInput(
        txid="33" * 32,
        vout=0,
        scriptpubkey=bytes([0x00, 0x14]) + pubkey_hash,
        witness=[b"\x00" * 71, pub],
        private_key=sender_priv,
    )
    recipient = SilentPaymentAddress.decode(sp.get_address())[0]
    outputs = create_outputs([(sender_priv, False)], [vin.outpoint()], [recipient])
    found = sp.scan([vin], outputs)
    assert len(found) == 1

    f = found[0]
    return SilentPaymentReceived(
        txid="44" * 32,
        vout=1,
        value=120_000,
        address=pubkey_to_p2tr_address(f.pubkey_xonly, "mainnet"),
        pubkey_xonly=f.pubkey_xonly,
        tweak=f.tweak,
        label_tweak=f.label_tweak,
    )


def test_register_silent_payment_surfaces_utxo(wallet_service: WalletService) -> None:
    received = _receive_silent_payment(wallet_service)

    injected = wallet_service.register_silent_payment_utxos([received], mixdepth=0)

    assert len(injected) == 1
    utxo = injected[0]
    assert utxo.is_p2tr
    assert utxo.value == 120_000
    assert utxo.mixdepth == 0
    assert utxo.scriptpubkey == "5120" + received.pubkey_xonly.hex()
    # Surfaced for coin selection in the right mixdepth bucket.
    assert utxo in wallet_service.utxo_cache[0]

    # Re-registering the same outpoint must not create a duplicate.
    again = wallet_service.register_silent_payment_utxos([received], mixdepth=0)
    assert again == []
    assert len(wallet_service.utxo_cache[0]) == 1


def test_resolve_p2tr_signing_key_for_silent_payment(wallet_service: WalletService) -> None:
    received = _receive_silent_payment(wallet_service)
    wallet_service.register_silent_payment_utxos([received], mixdepth=0)

    resolved = wallet_service.resolve_p2tr_signing_key(received.address)
    assert resolved is not None
    priv_key, output_xonly = resolved

    assert isinstance(priv_key, PrivateKey)
    assert output_xonly == received.pubkey_xonly
    # The resolved private key must control the output key.
    assert priv_key.public_key.format(compressed=True)[1:] == received.pubkey_xonly

    spk = create_p2tr_scriptpubkey(received.pubkey_xonly)
    tx = _spend_tx(create_p2tr_scriptpubkey(PrivateKey().public_key.format()[1:]))
    sig = sign_p2tr_input(tx, 0, [received.value], [spk], priv_key, SIGHASH_DEFAULT)
    assert verify_p2tr_signature(tx, 0, [received.value], [spk], sig, output_xonly)


def test_resolve_p2tr_signing_key_unknown_address(wallet_service: WalletService) -> None:
    # A taproot address the wallet has never seen resolves to None.
    unknown = "bc1p" + "q" * 58
    assert wallet_service.resolve_p2tr_signing_key(unknown) is None


def test_sp_coins_persist_across_restart(test_mnemonic, mock_backend_imported, tmp_path) -> None:
    """A detected SP coin must survive a restart so it stays spendable.

    Registering writes the per-output tweaks to the metadata store; a fresh
    WalletService on the same data_dir re-hydrates them and can recompute the
    key-path signing key without re-scanning the chain.
    """
    wallet = WalletService(
        mnemonic=test_mnemonic,
        backend=mock_backend_imported,
        network="mainnet",
        mixdepth_count=5,
        data_dir=tmp_path,
    )
    received = _receive_silent_payment(wallet)
    wallet.register_silent_payment_utxos([received], mixdepth=2)

    # Reopen the wallet from the same data dir -> the coin comes back.
    reopened = WalletService(
        mnemonic=test_mnemonic,
        backend=mock_backend_imported,
        network="mainnet",
        mixdepth_count=5,
        data_dir=tmp_path,
    )

    bucket = reopened.utxo_cache.get(2, [])
    assert any(u.txid == received.txid and u.vout == received.vout for u in bucket)

    # And it must still be spendable: the signing key is recomputed from the
    # persisted tweaks alone.
    resolved = reopened.resolve_p2tr_signing_key(received.address)
    assert resolved is not None
    priv_key, output_xonly = resolved
    assert output_xonly == received.pubkey_xonly
    assert priv_key.public_key.format(compressed=True)[1:] == received.pubkey_xonly
