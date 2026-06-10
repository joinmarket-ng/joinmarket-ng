"""Tests for on-chain BIP352 silent payment scanning."""

from __future__ import annotations

import hashlib

import pytest
from _jmwallet_test_helpers import TEST_MNEMONIC
from coincurve import PublicKey
from jmcore.bitcoin import create_p2tr_scriptpubkey
from jmcore.silentpayments import SilentPaymentAddress, SilentPaymentInput, create_outputs

from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.silent_payment_scan import (
    SilentPaymentReceived,
    build_scan_inputs,
    scan_block_transactions,
    taproot_outputs,
)
from jmwallet.wallet.silent_payments import SilentPaymentWallet

SENDER_PRIV = 0xDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBEEFDEADBE01


def _sp_wallet() -> SilentPaymentWallet:
    master = HDKey.from_seed(mnemonic_to_seed(TEST_MNEMONIC, ""))
    return SilentPaymentWallet(master, network="mainnet")


def _p2wpkh_scriptpubkey(privkey: int) -> bytes:
    pub = PublicKey.from_secret(privkey.to_bytes(32, "big")).format(compressed=True)
    return bytes([0x00, 0x14]) + hashlib.new("ripemd160", hashlib.sha256(pub).digest()).digest()


def _make_block_tx(sp_wallet: SilentPaymentWallet, txid: str = "ab" * 32) -> dict:
    """Construct a getblock-verbosity-3 style tx paying the wallet's SP address."""
    sender_pub = PublicKey.from_secret(SENDER_PRIV.to_bytes(32, "big")).format(compressed=True)
    prev_txid = "cd" * 32
    vin = SilentPaymentInput(
        txid=prev_txid,
        vout=0,
        scriptpubkey=_p2wpkh_scriptpubkey(SENDER_PRIV),
        witness=[b"\x00" * 71, sender_pub],
        private_key=SENDER_PRIV,
    )
    recipient = SilentPaymentAddress.decode(sp_wallet.get_address())[0]
    outputs = create_outputs([(SENDER_PRIV, False)], [vin.outpoint()], [recipient])
    spk = create_p2tr_scriptpubkey(outputs[0])
    return {
        "txid": txid,
        "vin": [
            {
                "txid": prev_txid,
                "vout": 0,
                "scriptSig": {"hex": ""},
                "txinwitness": ["00" * 71, sender_pub.hex()],
                "prevout": {"scriptPubKey": {"hex": _p2wpkh_scriptpubkey(SENDER_PRIV).hex()}},
            }
        ],
        "vout": [
            {"n": 0, "value": 0.001, "scriptPubKey": {"hex": spk.hex()}},
            {"n": 1, "value": 0.5, "scriptPubKey": {"hex": "0014" + "11" * 20}},
        ],
    }


def test_build_scan_inputs_skips_coinbase() -> None:
    tx = {"vin": [{"coinbase": "00"}, {"txid": "aa" * 32, "vout": 0, "prevout": {}}], "vout": []}
    assert build_scan_inputs(tx) == []


def test_taproot_outputs_filters_non_taproot() -> None:
    tx = {
        "vout": [
            {"n": 0, "value": 0.01, "scriptPubKey": {"hex": "5120" + "ab" * 32}},
            {"n": 1, "value": 0.02, "scriptPubKey": {"hex": "0014" + "ab" * 20}},
        ]
    }
    outs = taproot_outputs(tx)
    assert len(outs) == 1
    assert outs[0][0] == 0
    assert outs[0][1] == 1_000_000


def test_scan_block_transactions_detects_payment() -> None:
    sp_wallet = _sp_wallet()
    tx = _make_block_tx(sp_wallet)

    received = scan_block_transactions(sp_wallet, [tx], network="mainnet")
    assert len(received) == 1
    r = received[0]
    assert isinstance(r, SilentPaymentReceived)
    assert r.txid == "ab" * 32
    assert r.vout == 0
    assert r.value == 100_000
    assert r.address.startswith("bc1p")

    # The recovered key must reproduce the taproot output.
    d = r.output_private_key(sp_wallet.spend_privkey)
    derived = PublicKey.from_secret(d.to_bytes(32, "big")).format(compressed=True)[1:]
    assert derived == r.pubkey_xonly


def test_scan_block_transactions_ignores_unrelated() -> None:
    sp_wallet = _sp_wallet()
    other = SilentPaymentWallet(
        HDKey.from_seed(mnemonic_to_seed(TEST_MNEMONIC, "different")), network="mainnet"
    )
    tx = _make_block_tx(other)
    assert scan_block_transactions(sp_wallet, [tx], network="mainnet") == []


class _FakeBackend:
    def __init__(self, blocks: dict[int, list[dict]], height: int) -> None:
        self._blocks = blocks
        self._height = height

    async def get_block_height(self) -> int:
        return self._height

    async def get_block_transactions(self, block_height: int) -> list[dict]:
        return self._blocks.get(block_height, [])


@pytest.mark.asyncio
async def test_wallet_scan_silent_payments_range() -> None:
    from jmwallet.wallet.service import WalletService

    sp_wallet = _sp_wallet()
    tx = _make_block_tx(sp_wallet)
    backend = _FakeBackend({100: [tx], 101: []}, height=101)

    wallet = WalletService(mnemonic=TEST_MNEMONIC, backend=backend, network="mainnet")
    received = await wallet.scan_silent_payments(100, 101)
    assert len(received) == 1
    assert received[0].txid == "ab" * 32
