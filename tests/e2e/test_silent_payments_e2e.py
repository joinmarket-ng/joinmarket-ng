"""
End-to-end test for BIP352 Silent Payments against a real regtest node.

Exercises the full lifecycle against Bitcoin Core consensus, which the
in-process unit tests cannot cover:

  1. A sender spends a P2WPKH coinbase UTXO and creates a silent payment
     taproot output to the wallet's published SP address (``create_outputs``).
  2. The wallet scans the mined block (``getblock`` verbosity 3) and detects
     the incoming output with the correct value and address.
  3. The wallet recovers the output private key and spends the detected P2TR
     output via a BIP341 key-path Schnorr signature (``sign_p2tr_input``),
     which the node accepts and confirms.

Step 3 is the important one: it proves the in-tree Taproot sighash and
signing produce a consensus-valid spend of a real silent payment output, so
funds received this way are actually recoverable.

Requires: ``docker compose up -d`` (the default ``jm-bitcoin`` regtest node).
"""

from __future__ import annotations

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    create_p2tr_scriptpubkey,
    create_p2wpkh_script_code,
    pubkey_to_p2wpkh_address,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from jmcore.silentpayments import (
    SilentPaymentAddress,
    SilentPaymentInput,
    create_outputs,
)

from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.signing import SIGHASH_DEFAULT, sign_p2tr_input, sign_p2wpkh_input
from jmwallet.wallet.silent_payment_scan import scan_block_transactions
from jmwallet.wallet.silent_payments import SilentPaymentWallet

from .rpc_utils import rpc_call

pytestmark = pytest.mark.e2e

# A throwaway BIP39 test mnemonic (never used on mainnet).
_SP_MNEMONIC = (
    "legal winner thank year wave sausage worth useful legal winner thank yellow"
)
# Deterministic sender key controlling the coinbase UTXO we spend.
_SENDER_PRIV = 0xC0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE01
_FEE = 1000


def _sink_address() -> str:
    pub = PrivateKey((7).to_bytes(32, "big")).public_key.format(compressed=True)
    return pubkey_to_p2wpkh_address(pub, "regtest")


@pytest.mark.asyncio
async def test_silent_payment_receive_and_spend_against_regtest() -> None:
    sender_priv = PrivateKey(_SENDER_PRIV.to_bytes(32, "big"))
    sender_pub = sender_priv.public_key.format(compressed=True)
    sender_spk = pubkey_to_p2wpkh_script(sender_pub)
    sender_addr = pubkey_to_p2wpkh_address(sender_pub, "regtest")

    # Mine a mature coinbase to the sender. Use the block hashes returned by
    # generatetoaddress (not computed heights) so a concurrent auto-miner on the
    # e2e profile cannot make us pick the wrong coinbase.
    mined = await rpc_call("generatetoaddress", [101, sender_addr])
    cb_block = await rpc_call("getblock", [mined[0], 2])
    cb_tx = cb_block["tx"][0]
    cb_txid = cb_tx["txid"]
    cb_value = round(cb_tx["vout"][0]["value"] * 1e8)
    assert bytes.fromhex(cb_tx["vout"][0]["scriptPubKey"]["hex"]) == sender_spk

    # Receiver's published silent payment address.
    master = HDKey.from_seed(mnemonic_to_seed(_SP_MNEMONIC, ""))
    sp_wallet = SilentPaymentWallet(master, network="regtest")
    recipient = SilentPaymentAddress.decode(sp_wallet.get_address())[0]

    # Sender derives the silent payment taproot output and builds the funding tx.
    funding_in = SilentPaymentInput(txid=cb_txid, vout=0, scriptpubkey=sender_spk)
    sp_outputs = create_outputs(
        [(_SENDER_PRIV, False)], [funding_in.outpoint()], [recipient]
    )
    assert len(sp_outputs) == 1
    sp_spk = create_p2tr_scriptpubkey(sp_outputs[0])

    txin = TxInput.from_hex(cb_txid, 0, value=cb_value)
    txout = TxOutput(value=cb_value - _FEE, script=sp_spk)
    funding_tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[txin],
        outputs=[txout],
        locktime=0,
        witnesses=[],
    )
    sig = sign_p2wpkh_input(
        funding_tx, 0, create_p2wpkh_script_code(sender_pub), cb_value, sender_priv, 1
    )
    funding_raw = serialize_transaction(
        2, [txin], [txout], 0, witnesses=[[sig, sender_pub]]
    )
    funding_txid = await rpc_call("sendrawtransaction", [funding_raw.hex()])

    sink = _sink_address()
    funding_blockhash = (await rpc_call("generatetoaddress", [1, sink]))[0]
    funding_block = await rpc_call("getblock", [funding_blockhash, 3])

    # The receiver scans the block and detects the payment.
    received = scan_block_transactions(
        sp_wallet, funding_block["tx"], network="regtest"
    )
    assert len(received) == 1
    detected = received[0]
    assert detected.txid == funding_txid
    assert detected.value == cb_value - _FEE
    assert detected.address.startswith("bcrt1p")

    # The receiver recovers the key and spends the P2TR output (key-path spend).
    spend_priv = PrivateKey(sp_wallet.output_private_key(detected).to_bytes(32, "big"))
    sp_output_spk = create_p2tr_scriptpubkey(detected.pubkey_xonly)
    spend_in = TxInput.from_hex(funding_txid, detected.vout, value=detected.value)
    spend_out = TxOutput(value=detected.value - _FEE, script=sender_spk)
    spend_tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[spend_in],
        outputs=[spend_out],
        locktime=0,
        witnesses=[],
    )
    schnorr = sign_p2tr_input(
        spend_tx, 0, [detected.value], [sp_output_spk], spend_priv, SIGHASH_DEFAULT
    )
    spend_raw = serialize_transaction(
        2, [spend_in], [spend_out], 0, witnesses=[[schnorr]]
    )

    accept = await rpc_call("testmempoolaccept", [[spend_raw.hex()]])
    assert accept[0]["allowed"] is True, accept

    spend_txid = await rpc_call("sendrawtransaction", [spend_raw.hex()])
    await rpc_call("generatetoaddress", [1, sink])
    confirmed = await rpc_call("getrawtransaction", [spend_txid, True])
    assert confirmed.get("confirmations", 0) >= 1
