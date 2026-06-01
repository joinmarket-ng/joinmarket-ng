"""
End-to-end taproot CoinJoin tests against a real regtest node.

These exercise the BIP341 signing path that the maker and taker use for
taproot (tr0) CoinJoins, against Bitcoin Core consensus. The in-process unit
tests only check sign/verify self-consistency; here we prove that a
CoinJoin-shaped transaction with taproot inputs is actually accepted and
confirmed by the node.

Two shapes are covered:

  1. A multi-party CoinJoin whose inputs are two independently owned BIP86
     key-path taproot coins, with equal-value taproot CoinJoin outputs plus
     taproot change. Each input is signed by its owner over the *full* prevout
     set (BIP341 commits to every input's amount and scriptPubKey), exactly as
     the maker/taker assemble it.

  2. A CoinJoin that spends a *received silent payment* output (recovered
     key, no BIP32 path) alongside a BIP86 taproot coin, proving SP coins are
     spendable as CoinJoin inputs.

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

# Throwaway BIP39 test mnemonics (never used on mainnet).
_MAKER_MNEMONIC = (
    "legal winner thank year wave sausage worth useful legal winner thank yellow"
)
_TAKER_MNEMONIC = "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong"
_FEE = 1000


def _sink_address() -> str:
    pub = PrivateKey((7).to_bytes(32, "big")).public_key.format(compressed=True)
    return pubkey_to_p2wpkh_address(pub, "regtest")


async def _mature_coinbase_to(address: str) -> tuple[str, int, bytes]:
    """Mine a fresh, mature coinbase to ``address``; return (txid, value, spk).

    Uses the block hashes returned by generatetoaddress (not computed heights)
    so a concurrent auto-miner on the e2e profile cannot make us pick the wrong
    coinbase.
    """
    mined = await rpc_call("generatetoaddress", [101, address])
    block = await rpc_call("getblock", [mined[0], 2])
    cb = block["tx"][0]
    return (
        cb["txid"],
        round(cb["vout"][0]["value"] * 1e8),
        bytes.fromhex(cb["vout"][0]["scriptPubKey"]["hex"]),
    )


@pytest.mark.asyncio
async def test_taproot_coinjoin_multi_party_accepted() -> None:
    """Two independently owned BIP86 taproot inputs in one CoinJoin tx."""
    maker = HDKey.from_seed(mnemonic_to_seed(_MAKER_MNEMONIC, "")).derive(
        "m/86'/1'/0'/0/0"
    )
    taker = HDKey.from_seed(mnemonic_to_seed(_TAKER_MNEMONIC, "")).derive(
        "m/86'/1'/0'/0/0"
    )

    maker_txid, maker_val, maker_spk = await _mature_coinbase_to(
        maker.get_p2tr_address("regtest")
    )
    taker_txid, taker_val, taker_spk = await _mature_coinbase_to(
        taker.get_p2tr_address("regtest")
    )

    # CoinJoin shape: equal-value taproot CJ outputs + taproot change for each.
    # Derive the CJ amount from the actual coinbase values rather than hardcoding
    # it: this suite shares a regtest node whose block height (and therefore
    # coinbase subsidy) grows as other tests mine, so a fixed large amount would
    # eventually exceed the available inputs and produce negative change.
    cj_amount = (min(taker_val, maker_val) - _FEE) // 2
    assert cj_amount > 1000, f"coinbase subsidy too small: {taker_val=} {maker_val=}"
    maker_change_xonly = maker.get_p2tr_output_xonly()  # reuse for brevity
    taker_change_xonly = taker.get_p2tr_output_xonly()
    cj_out_xonly = (
        HDKey.from_seed(mnemonic_to_seed(_TAKER_MNEMONIC, ""))
        .derive("m/86'/1'/0'/0/1")
        .get_p2tr_output_xonly()
    )

    inputs = [
        TxInput.from_hex(taker_txid, 0, value=taker_val),
        TxInput.from_hex(maker_txid, 0, value=maker_val),
    ]
    outputs = [
        TxOutput(value=cj_amount, script=create_p2tr_scriptpubkey(cj_out_xonly)),
        TxOutput(value=cj_amount, script=create_p2tr_scriptpubkey(maker_change_xonly)),
        TxOutput(
            value=taker_val - cj_amount - _FEE,
            script=create_p2tr_scriptpubkey(taker_change_xonly),
        ),
        TxOutput(
            value=maker_val - cj_amount - _FEE,
            script=create_p2tr_scriptpubkey(maker_change_xonly),
        ),
    ]
    tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=inputs,
        outputs=outputs,
        locktime=0,
        witnesses=[],
    )

    # BIP341 commits to every input's value + scriptPubKey, ordered by index.
    prevout_values = [taker_val, maker_val]
    prevout_scripts = [taker_spk, maker_spk]

    taker_sig = sign_p2tr_input(
        tx, 0, prevout_values, prevout_scripts, PrivateKey(taker.get_p2tr_private_key())
    )
    maker_sig = sign_p2tr_input(
        tx, 1, prevout_values, prevout_scripts, PrivateKey(maker.get_p2tr_private_key())
    )

    raw = serialize_transaction(
        2, inputs, outputs, 0, witnesses=[[taker_sig], [maker_sig]]
    )
    accept = await rpc_call("testmempoolaccept", [[raw.hex()]])
    assert accept[0]["allowed"] is True, accept

    txid = await rpc_call("sendrawtransaction", [raw.hex()])
    await rpc_call("generatetoaddress", [1, _sink_address()])
    confirmed = await rpc_call("getrawtransaction", [txid, True])
    assert confirmed.get("confirmations", 0) >= 1


@pytest.mark.asyncio
async def test_coinjoin_spends_silent_payment_input() -> None:
    """A received silent payment output is spendable as a CoinJoin input."""
    sender_priv_int = 0xC0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE02
    sender_priv = PrivateKey(sender_priv_int.to_bytes(32, "big"))
    sender_pub = sender_priv.public_key.format(compressed=True)
    sender_spk = pubkey_to_p2wpkh_script(sender_pub)
    sender_addr = pubkey_to_p2wpkh_address(sender_pub, "regtest")

    cb_txid, cb_value, _ = await _mature_coinbase_to(sender_addr)

    # Receiver (taker) publishes a silent payment address; sender pays it.
    master = HDKey.from_seed(mnemonic_to_seed(_TAKER_MNEMONIC, ""))
    sp_wallet = SilentPaymentWallet(master, network="regtest")
    recipient = SilentPaymentAddress.decode(sp_wallet.get_address())[0]

    funding_in = SilentPaymentInput(txid=cb_txid, vout=0, scriptpubkey=sender_spk)
    sp_outputs = create_outputs(
        [(sender_priv_int, False)], [funding_in.outpoint()], [recipient]
    )
    sp_spk = create_p2tr_scriptpubkey(sp_outputs[0])

    txin = TxInput.from_hex(cb_txid, 0, value=cb_value)
    sp_value = cb_value - _FEE
    txout = TxOutput(value=sp_value, script=sp_spk)
    funding_tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[txin],
        outputs=[txout],
        locktime=0,
        witnesses=[],
    )
    fund_sig = sign_p2wpkh_input(
        funding_tx, 0, create_p2wpkh_script_code(sender_pub), cb_value, sender_priv, 1
    )
    funding_raw = serialize_transaction(
        2, [txin], [txout], 0, witnesses=[[fund_sig, sender_pub]]
    )
    funding_txid = await rpc_call("sendrawtransaction", [funding_raw.hex()])
    block_hash = (await rpc_call("generatetoaddress", [1, _sink_address()]))[0]
    block = await rpc_call("getblock", [block_hash, 3])

    received = scan_block_transactions(sp_wallet, block["tx"], network="regtest")
    assert len(received) == 1
    sp_coin = received[0]
    assert sp_coin.txid == funding_txid

    # A second BIP86 taproot coin to co-spend in the CoinJoin.
    maker = HDKey.from_seed(mnemonic_to_seed(_MAKER_MNEMONIC, "")).derive(
        "m/86'/1'/0'/0/2"
    )
    maker_txid, maker_val, maker_spk = await _mature_coinbase_to(
        maker.get_p2tr_address("regtest")
    )

    cj_amount = (min(sp_value, maker_val) - _FEE) // 2
    assert cj_amount > 1000, f"inputs too small: {sp_value=} {maker_val=}"
    cj_dest_xonly = master.derive("m/86'/1'/0'/0/5").get_p2tr_output_xonly()
    change_xonly = master.derive("m/86'/1'/0'/0/6").get_p2tr_output_xonly()

    inputs = [
        TxInput.from_hex(sp_coin.txid, sp_coin.vout, value=sp_value),
        TxInput.from_hex(maker_txid, 0, value=maker_val),
    ]
    outputs = [
        TxOutput(value=cj_amount, script=create_p2tr_scriptpubkey(cj_dest_xonly)),
        TxOutput(
            value=cj_amount,
            script=create_p2tr_scriptpubkey(maker.get_p2tr_output_xonly()),
        ),
        TxOutput(
            value=sp_value + maker_val - 2 * cj_amount - _FEE,
            script=create_p2tr_scriptpubkey(change_xonly),
        ),
    ]
    tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=inputs,
        outputs=outputs,
        locktime=0,
        witnesses=[],
    )

    sp_output_spk = create_p2tr_scriptpubkey(sp_coin.pubkey_xonly)
    prevout_values = [sp_value, maker_val]
    prevout_scripts = [sp_output_spk, maker_spk]

    # SP input: recovered output key, signed directly (no BIP86 taptweak).
    sp_priv = PrivateKey(sp_wallet.output_private_key(sp_coin).to_bytes(32, "big"))
    sp_sig = sign_p2tr_input(
        tx, 0, prevout_values, prevout_scripts, sp_priv, SIGHASH_DEFAULT
    )
    maker_sig = sign_p2tr_input(
        tx, 1, prevout_values, prevout_scripts, PrivateKey(maker.get_p2tr_private_key())
    )

    raw = serialize_transaction(
        2, inputs, outputs, 0, witnesses=[[sp_sig], [maker_sig]]
    )
    accept = await rpc_call("testmempoolaccept", [[raw.hex()]])
    assert accept[0]["allowed"] is True, accept

    txid = await rpc_call("sendrawtransaction", [raw.hex()])
    await rpc_call("generatetoaddress", [1, _sink_address()])
    confirmed = await rpc_call("getrawtransaction", [txid, True])
    assert confirmed.get("confirmations", 0) >= 1


@pytest.mark.asyncio
async def test_taproot_fidelity_bond_script_path_spend_accepted() -> None:
    """A Taproot fidelity bond is spendable via its CLTV tapleaf (BIP342).

    Proves the in-tree script-path signer produces a consensus-valid witness
    ``[signature, tapleaf_script, control_block]`` for a bond whose only spend
    path is the timelocked tapscript leaf committed under the BIP341 NUMS
    internal key.
    """
    from jmcore.btc_script import derive_taproot_bond_address
    from jmwallet.wallet.signing import sign_p2tr_script_path_input

    # Bond key at the JMP-0005 taproot bond path m/86'/coin'/0'/2/timenumber.
    bond_key = HDKey.from_seed(mnemonic_to_seed(_MAKER_MNEMONIC, "")).derive(
        "m/86'/1'/0'/2/0"
    )
    bond_priv = bond_key.private_key
    bond_xonly = bond_key.get_public_key_bytes(compressed=True)[1:]

    # Locktime safely below the regtest median-time-past so CLTV passes.
    mtp = (await rpc_call("getblockchaininfo"))["mediantime"]
    locktime = mtp - 7 * 24 * 3600
    bond = derive_taproot_bond_address(bond_xonly, locktime, network="regtest")

    # Fund the bond output from a matured coinbase (P2WPKH key-path).
    funder = PrivateKey((9).to_bytes(32, "big"))
    funder_pub = funder.public_key.format(compressed=True)
    cb_txid, cb_value, _ = await _mature_coinbase_to(
        pubkey_to_p2wpkh_address(funder_pub, "regtest")
    )
    bond_value = cb_value - _FEE
    fund_in = TxInput.from_hex(cb_txid, 0, value=cb_value)
    fund_out = TxOutput(value=bond_value, script=bond.scriptpubkey)
    funding_tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[fund_in],
        outputs=[fund_out],
        locktime=0,
        witnesses=[],
    )
    fund_sig = sign_p2wpkh_input(
        funding_tx, 0, create_p2wpkh_script_code(funder_pub), cb_value, funder, 1
    )
    funding_raw = serialize_transaction(
        2, [fund_in], [fund_out], 0, witnesses=[[fund_sig, funder_pub]]
    )
    bond_txid = await rpc_call("sendrawtransaction", [funding_raw.hex()])
    await rpc_call("generatetoaddress", [1, _sink_address()])

    # Spend the bond. CLTV requires nLockTime >= locktime and a non-final
    # input sequence.
    spend_in = TxInput.from_hex(bond_txid, 0, value=bond_value, sequence=0xFFFFFFFE)
    spend_out = TxOutput(
        value=bond_value - _FEE,
        script=create_p2tr_scriptpubkey(bond_key.get_p2tr_output_xonly()),
    )
    spend_tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[spend_in],
        outputs=[spend_out],
        locktime=locktime,
        witnesses=[],
    )
    sig = sign_p2tr_script_path_input(
        spend_tx,
        0,
        [bond_value],
        [bond.scriptpubkey],
        bond_priv,
        bond.tapleaf_script,
        SIGHASH_DEFAULT,
    )
    witness = [sig, bond.tapleaf_script, bond.control_block]
    raw = serialize_transaction(
        2, [spend_in], [spend_out], locktime, witnesses=[witness]
    )
    accept = await rpc_call("testmempoolaccept", [[raw.hex()]])
    assert accept[0]["allowed"] is True, accept

    txid = await rpc_call("sendrawtransaction", [raw.hex()])
    await rpc_call("generatetoaddress", [1, _sink_address()])
    confirmed = await rpc_call("getrawtransaction", [txid, True])
    assert confirmed.get("confirmations", 0) >= 1


@pytest.mark.asyncio
async def test_taproot_coinjoin_with_fidelity_bond_input_accepted() -> None:
    """A multi-party CoinJoin that spends an *expired* taproot fidelity bond.

    Combines the two halves of the Layer 4 work in a single consensus check:
    the maker contributes an expired Taproot bond (spent via its CLTV tapleaf,
    BIP342 script path), the taker contributes a BIP86 key-path taproot coin,
    and the transaction carries the bond's nLockTime with a non-final bond
    input. Bitcoin Core must accept this mixed-input, timelocked CoinJoin.
    """
    from jmcore.btc_script import derive_taproot_bond_address
    from jmwallet.wallet.signing import sign_p2tr_script_path_input

    # Maker's bond key at the JMP-0005 taproot bond path.
    bond_key = HDKey.from_seed(mnemonic_to_seed(_MAKER_MNEMONIC, "")).derive(
        "m/86'/1'/0'/2/0"
    )
    bond_priv = bond_key.private_key
    bond_xonly = bond_key.get_public_key_bytes(compressed=True)[1:]

    # An already-expired locktime (below regtest median-time-past) so CLTV passes.
    mtp = (await rpc_call("getblockchaininfo"))["mediantime"]
    locktime = mtp - 7 * 24 * 3600
    bond = derive_taproot_bond_address(bond_xonly, locktime, network="regtest")

    # Fund the bond output from a matured coinbase.
    funder = PrivateKey((11).to_bytes(32, "big"))
    funder_pub = funder.public_key.format(compressed=True)
    cb_txid, cb_value, _ = await _mature_coinbase_to(
        pubkey_to_p2wpkh_address(funder_pub, "regtest")
    )
    bond_value = cb_value - _FEE
    fund_in = TxInput.from_hex(cb_txid, 0, value=cb_value)
    fund_out = TxOutput(value=bond_value, script=bond.scriptpubkey)
    funding_tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[fund_in],
        outputs=[fund_out],
        locktime=0,
        witnesses=[],
    )
    fund_sig = sign_p2wpkh_input(
        funding_tx, 0, create_p2wpkh_script_code(funder_pub), cb_value, funder, 1
    )
    funding_raw = serialize_transaction(
        2, [fund_in], [fund_out], 0, witnesses=[[fund_sig, funder_pub]]
    )
    bond_txid = await rpc_call("sendrawtransaction", [funding_raw.hex()])
    await rpc_call("generatetoaddress", [1, _sink_address()])

    # Taker's BIP86 key-path taproot coin.
    taker = HDKey.from_seed(mnemonic_to_seed(_TAKER_MNEMONIC, "")).derive(
        "m/86'/1'/0'/0/0"
    )
    taker_txid, taker_val, taker_spk = await _mature_coinbase_to(
        taker.get_p2tr_address("regtest")
    )

    # CoinJoin shape: equal taproot CJ outputs + taproot change for each party.
    cj_amount = (min(bond_value, taker_val) - _FEE) // 2
    assert cj_amount > 1000, f"inputs too small: {bond_value=} {taker_val=}"
    taker_dest_xonly = (
        HDKey.from_seed(mnemonic_to_seed(_TAKER_MNEMONIC, ""))
        .derive("m/86'/1'/0'/0/1")
        .get_p2tr_output_xonly()
    )
    taker_change_xonly = taker.get_p2tr_output_xonly()
    maker_out_xonly = bond_key.get_p2tr_output_xonly()

    # Bond input is non-final so the transaction-wide nLockTime is enforced; the
    # taker's key-path input stays final.
    inputs = [
        TxInput.from_hex(bond_txid, 0, value=bond_value, sequence=0xFFFFFFFE),
        TxInput.from_hex(taker_txid, 0, value=taker_val),
    ]
    outputs = [
        TxOutput(value=cj_amount, script=create_p2tr_scriptpubkey(taker_dest_xonly)),
        TxOutput(value=cj_amount, script=create_p2tr_scriptpubkey(maker_out_xonly)),
        TxOutput(
            value=bond_value - cj_amount - _FEE,
            script=create_p2tr_scriptpubkey(maker_out_xonly),
        ),
        TxOutput(
            value=taker_val - cj_amount - _FEE,
            script=create_p2tr_scriptpubkey(taker_change_xonly),
        ),
    ]
    tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=inputs,
        outputs=outputs,
        locktime=locktime,
        witnesses=[],
    )

    # BIP341/342 sighash commits to every input's value + scriptPubKey.
    prevout_values = [bond_value, taker_val]
    prevout_scripts = [bond.scriptpubkey, taker_spk]

    # Maker bond input: script-path spend over its CLTV tapleaf.
    bond_sig = sign_p2tr_script_path_input(
        tx,
        0,
        prevout_values,
        prevout_scripts,
        bond_priv,
        bond.tapleaf_script,
        SIGHASH_DEFAULT,
    )
    # Taker input: ordinary BIP86 key-path spend.
    taker_sig = sign_p2tr_input(
        tx, 1, prevout_values, prevout_scripts, PrivateKey(taker.get_p2tr_private_key())
    )

    raw = serialize_transaction(
        2,
        inputs,
        outputs,
        locktime,
        witnesses=[
            [bond_sig, bond.tapleaf_script, bond.control_block],
            [taker_sig],
        ],
    )
    accept = await rpc_call("testmempoolaccept", [[raw.hex()]])
    assert accept[0]["allowed"] is True, accept

    txid = await rpc_call("sendrawtransaction", [raw.hex()])
    await rpc_call("generatetoaddress", [1, _sink_address()])
    confirmed = await rpc_call("getrawtransaction", [txid, True])
    assert confirmed.get("confirmations", 0) >= 1
