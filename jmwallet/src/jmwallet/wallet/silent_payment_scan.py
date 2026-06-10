"""
On-chain scanning for BIP352 silent payments.

These are pure functions that turn raw transaction data (in the shape returned
by Bitcoin Core's ``getblock <hash> 3``, i.e. each input carries its prevout)
into detected silent payment outputs for a wallet. Keeping the logic separate
from any backend makes it straightforward to unit test and lets multiple
backends feed it.

A transaction is silent-payment-eligible when it has at least one taproot
output and at least one input on the BIP352 shared-secret input list. The
heavy lifting (ECDH, tweak derivation, output matching) lives in
:mod:`jmcore.silentpayments`; here we only parse and dispatch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jmcore.bitcoin import btc_to_sats, pubkey_to_p2tr_address
from jmcore.silentpayments import SilentPaymentInput
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jmwallet.wallet.silent_payments import SilentPaymentWallet


class SilentPaymentReceived(BaseModel):
    """A detected, spendable silent payment output."""

    model_config = ConfigDict(frozen=True)

    txid: str
    vout: int
    value: int
    address: str
    pubkey_xonly: bytes
    tweak: int
    label_tweak: int = 0

    def output_private_key(self, spend_privkey: int) -> int:
        """Private key (scalar) to spend this output."""
        from jmcore.constants import SECP256K1_N

        return (spend_privkey + self.tweak + self.label_tweak) % SECP256K1_N


def build_scan_inputs(tx: dict[str, Any]) -> list[SilentPaymentInput]:
    """Build silent payment inputs from a ``getblock`` verbosity-3 transaction.

    Inputs without a known prevout (e.g. coinbase) are skipped. Eligibility
    filtering by script type happens later inside the scanner.
    """
    inputs: list[SilentPaymentInput] = []
    for vin in tx.get("vin", []):
        if "coinbase" in vin:
            continue
        prevout = vin.get("prevout")
        if not prevout:
            continue
        spk_hex = prevout.get("scriptPubKey", {}).get("hex")
        if not spk_hex:
            continue
        witness = [bytes.fromhex(w) for w in vin.get("txinwitness", [])]
        inputs.append(
            SilentPaymentInput(
                txid=vin["txid"],
                vout=vin["vout"],
                scriptpubkey=bytes.fromhex(spk_hex),
                script_sig=bytes.fromhex(vin.get("scriptSig", {}).get("hex", "")),
                witness=witness,
            )
        )
    return inputs


def taproot_outputs(tx: dict[str, Any]) -> list[tuple[int, int, bytes]]:
    """Return ``(vout, value_sats, x_only_key)`` for each taproot output."""
    result: list[tuple[int, int, bytes]] = []
    for vout in tx.get("vout", []):
        spk = vout.get("scriptPubKey", {}).get("hex", "")
        if len(spk) == 68 and spk.startswith("5120"):
            result.append((vout["n"], btc_to_sats(vout["value"]), bytes.fromhex(spk[4:])))
    return result


def scan_block_transactions(
    sp_wallet: SilentPaymentWallet,
    transactions: Sequence[dict[str, Any]],
    labels: Sequence[int] = (),
    network: str = "mainnet",
) -> list[SilentPaymentReceived]:
    """Scan ``getblock`` verbosity-3 transactions for payments to ``sp_wallet``."""
    received: list[SilentPaymentReceived] = []
    for tx in transactions:
        tr_outputs = taproot_outputs(tx)
        if not tr_outputs:
            continue
        inputs = build_scan_inputs(tx)
        if not inputs:
            continue
        by_key = {xonly: (vout, value) for vout, value, xonly in tr_outputs}
        found = sp_wallet.scan(inputs, list(by_key), labels)
        for f in found:
            vout, value = by_key[f.pubkey_xonly]
            received.append(
                SilentPaymentReceived(
                    txid=tx["txid"],
                    vout=vout,
                    value=value,
                    address=pubkey_to_p2tr_address(f.pubkey_xonly, network),
                    pubkey_xonly=f.pubkey_xonly,
                    tweak=f.tweak,
                    label_tweak=f.label_tweak,
                )
            )
    return received
