"""Maker-side verification of maker-to-maker channel-as-change (shared 2-of-2)."""

from __future__ import annotations

from coincurve import PrivateKey

# regtest addresses reused from the taker harness style
from jmcore.bitcoin import create_p2tr_scriptpubkey, scriptpubkey_to_address
from jmcore.models import NetworkType, OfferType
from jmswap.channel import combined_funding_address
from jmwallet.wallet.models import UTXOInfo
from taker.combine import plan_combine_session
from taker.tx_builder import build_coinjoin_tx

from maker.tx_verification import verify_unsigned_transaction


def _addr(byte: int) -> str:
    return scriptpubkey_to_address(create_p2tr_scriptpubkey(bytes([byte]) * 32), "regtest")


def _utxo(txid: str, value: int) -> UTXOInfo:
    return UTXOInfo(
        txid=txid,
        vout=0,
        value=value,
        address=_addr(0x20),
        confirmations=6,
        scriptpubkey=create_p2tr_scriptpubkey(b"\x20" * 32).hex(),
        path="m/0",
        mixdepth=0,
    )


def _build_combined_tx():
    a_pub = PrivateKey(b"\x11" * 32).public_key.format()
    b_pub = PrivateKey(b"\x22" * 32).public_key.format()
    # A change 501_000, B change 401_000, pooled 902_000
    plan = plan_combine_session(
        [("A", a_pub.hex(), 501_000), ("B", b_pub.hex(), 401_000)], "regtest"
    )
    shared = combined_funding_address([a_pub, b_pub], "regtest")
    data = {
        "A": {
            "utxos": [{"txid": "a" * 64, "vout": 0, "value": 1_500_000}],
            "cj_addr": _addr(0x03),
            "change_addr": _addr(0x04),
            "cjfee": 1000,
            "txfee": 0,
            "change_outputs": plan.change_outputs["A"],
        },
        "B": {
            "utxos": [{"txid": "b" * 64, "vout": 0, "value": 1_400_000}],
            "cj_addr": _addr(0x05),
            "change_addr": _addr(0x06),
            "cjfee": 1000,
            "txfee": 0,
        },
    }
    tx, _ = build_coinjoin_tx(
        [{"txid": "c" * 64, "vout": 0, "value": 5_000_000}],
        _addr(0x01),
        _addr(0x02),
        5_000_000,
        data,
        1_000_000,
        2000,
        "regtest",
        combine_groups=plan.combine_groups,
    )
    return tx.hex(), shared, data


def test_verifier_accepts_shared_output_with_combine_info() -> None:
    tx_hex, shared, data = _build_combined_tx()
    # Funder A verifies: its change is in the shared 2-of-2 (902_000), no plain change.
    ok, err = verify_unsigned_transaction(
        tx_hex=tx_hex,
        our_utxos={("a" * 64, 0): _utxo("a" * 64, 1_500_000)},
        cj_address=data["A"]["cj_addr"],
        change_address=data["A"]["change_addr"],
        amount=1_000_000,
        cjfee=1000,
        txfee=0,
        offer_type=OfferType.SW0_ABSOLUTE,
        network=NetworkType.REGTEST,
        combine_address=shared,
        combine_value=902_000,
    )
    assert ok, err
    # Folder B verifies the same shared output (902_000), no plain change of its own.
    ok_b, err_b = verify_unsigned_transaction(
        tx_hex=tx_hex,
        our_utxos={("b" * 64, 0): _utxo("b" * 64, 1_400_000)},
        cj_address=data["B"]["cj_addr"],
        change_address=data["B"]["change_addr"],
        amount=1_000_000,
        cjfee=1000,
        txfee=0,
        offer_type=OfferType.SW0_ABSOLUTE,
        network=NetworkType.REGTEST,
        combine_address=shared,
        combine_value=902_000,
    )
    assert ok_b, err_b


def test_verifier_rejects_combined_tx_without_combine_info() -> None:
    """Without the combine info, a maker requires its plain change output (absent here)."""
    tx_hex, _shared, data = _build_combined_tx()
    ok, err = verify_unsigned_transaction(
        tx_hex=tx_hex,
        our_utxos={("a" * 64, 0): _utxo("a" * 64, 1_500_000)},
        cj_address=data["A"]["cj_addr"],
        change_address=data["A"]["change_addr"],
        amount=1_000_000,
        cjfee=1000,
        txfee=0,
        offer_type=OfferType.SW0_ABSOLUTE,
        network=NetworkType.REGTEST,
    )
    assert not ok
    assert "Change address appears 0 times" in err


def _verify_ring(tx_hex: str, ring_outputs: list[tuple[str, int]]) -> tuple[bool, str]:
    return verify_unsigned_transaction(
        tx_hex=tx_hex,
        our_utxos={("a" * 64, 0): _utxo("a" * 64, 1_500_000)},
        cj_address=_addr(0x03),
        change_address=_addr(0x04),
        amount=1_000_000,
        cjfee=1_000,
        txfee=0,
        offer_type=OfferType.SW0_ABSOLUTE,
        network=NetworkType.REGTEST,
        ring_outputs=ring_outputs,
    )


def test_ring_verifier_requires_distinct_strictly_cofunded_outputs() -> None:
    """Ring relays cannot reuse an address or disguise a maker-only output as pooled."""
    from jmcore.bitcoin import TxInput, TxOutput, address_to_scriptpubkey, serialize_transaction

    address_a = _addr(0x31)
    address_b = _addr(0x32)
    ring = [(address_a, 250_000), (address_b, 251_000)]
    tx_hex = serialize_transaction(
        2,
        [TxInput.from_hex("a" * 64, 0)],
        [
            TxOutput(value=1_000_000, script=address_to_scriptpubkey(_addr(0x03))),
            TxOutput(value=400_000, script=address_to_scriptpubkey(address_a)),
            TxOutput(value=400_000, script=address_to_scriptpubkey(address_b)),
        ],
        0,
    ).hex()

    ok, err = _verify_ring(tx_hex, ring)
    assert ok, err

    duplicate_ok, duplicate_err = _verify_ring(tx_hex, [(address_a, 250_000), (address_a, 251_000)])
    assert duplicate_ok is False
    assert "duplicate output addresses" in duplicate_err

    maker_only_tx = serialize_transaction(
        2,
        [TxInput.from_hex("a" * 64, 0)],
        [
            TxOutput(value=1_000_000, script=address_to_scriptpubkey(_addr(0x03))),
            TxOutput(value=250_000, script=address_to_scriptpubkey(address_a)),
            TxOutput(value=400_000, script=address_to_scriptpubkey(address_b)),
        ],
        0,
    ).hex()
    maker_only_ok, maker_only_err = _verify_ring(maker_only_tx, ring)
    assert maker_only_ok is False
    assert "does not include partner funding" in maker_only_err
