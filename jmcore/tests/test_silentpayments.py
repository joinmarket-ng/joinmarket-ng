"""Tests for BIP352 Silent Payments primitives, driven by the BIP test vectors."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from coincurve import PublicKey

from jmcore.constants import SECP256K1_N
from jmcore.silentpayments import (
    SilentPaymentAddress,
    SilentPaymentError,
    SilentPaymentInput,
    create_label_tweak,
    create_labeled_address,
    create_outputs,
    extract_input_pubkey,
    parse_witness,
    scan_transaction,
)

TEST_VECTORS = Path(__file__).parent / "data" / "bip352_send_and_receive_test_vectors.json"


def load_vectors() -> list[dict]:
    with TEST_VECTORS.open() as handle:
        return json.load(handle)


def _build_input(entry: dict, *, with_key: bool) -> SilentPaymentInput:
    return SilentPaymentInput(
        txid=entry["txid"],
        vout=entry["vout"],
        scriptpubkey=bytes.fromhex(entry["prevout"]["scriptPubKey"]["hex"]),
        script_sig=bytes.fromhex(entry.get("scriptSig", "")),
        witness=parse_witness(entry.get("txinwitness", "")),
        private_key=int(entry["private_key"], 16) if with_key else None,
    )


@pytest.mark.parametrize("case", load_vectors(), ids=lambda c: c["comment"])
def test_sending(case: dict) -> None:
    for sending in case["sending"]:
        given = sending["given"]
        expected = sending["expected"]

        input_priv_keys: list[tuple[int, bool]] = []
        for entry in given["vin"]:
            vin = _build_input(entry, with_key=True)
            if extract_input_pubkey(vin) is None:
                continue
            assert vin.private_key is not None
            input_priv_keys.append((vin.private_key, vin.is_taproot()))

        outpoints = [_build_input(entry, with_key=True).outpoint() for entry in given["vin"]]
        recipients = []
        for r in given["recipients"]:
            decoded = SilentPaymentAddress.decode(r["address"])[0]
            recipients.extend([decoded] * r.get("count", 1))

        if not input_priv_keys:
            assert expected["outputs"][0] == []
            continue

        outputs = sorted(
            out.hex() for out in create_outputs(input_priv_keys, outpoints, recipients)
        )
        assert any(outputs == sorted(possible) for possible in expected["outputs"]), (
            f"{case['comment']}: {outputs} not in {expected['outputs']}"
        )


@pytest.mark.parametrize("case", load_vectors(), ids=lambda c: c["comment"])
def test_receiving(case: dict) -> None:
    for receiving in case["receiving"]:
        given = receiving["given"]
        expected = receiving["expected"]

        scan_priv = int(given["key_material"]["scan_priv_key"], 16)
        spend_priv = int(given["key_material"]["spend_priv_key"], 16)
        spend_pub = PublicKey.from_secret(spend_priv.to_bytes(32, "big")).format()
        scan_pub = PublicKey.from_secret(scan_priv.to_bytes(32, "big")).format()

        # Address derivation must match the expected published addresses.
        addresses = [
            SilentPaymentAddress(scan_pubkey=scan_pub, spend_pubkey=spend_pub).encode("mainnet")
        ]
        for label in given["labels"]:
            addresses.append(create_labeled_address(scan_priv, spend_pub, label, "mainnet"))
        assert addresses == expected["addresses"]

        labels = {
            PublicKey.from_secret(create_label_tweak(scan_priv, m).to_bytes(32, "big")).format(): (
                create_label_tweak(scan_priv, m)
            )
            for m in given["labels"]
        }

        inputs = [_build_input(entry, with_key=False) for entry in given["vin"]]
        taproot_outputs = [bytes.fromhex(o) for o in given["outputs"]]

        found = scan_transaction(scan_priv, spend_pub, inputs, taproot_outputs, labels)

        if (
            "outputs" in expected
            and isinstance(expected["outputs"], list)
            and (not expected["outputs"] or isinstance(expected["outputs"][0], dict))
        ):
            got = {(f.pubkey_xonly.hex(), (f.tweak + f.label_tweak) % SECP256K1_N) for f in found}
            want = {(o["pub_key"], int(o["priv_key_tweak"], 16)) for o in expected["outputs"]}
            assert got == want, case["comment"]
            # Verify the spend private key produces the expected output pubkey.
            for f in found:
                d = f.output_private_key(spend_priv)
                derived = PublicKey.from_secret(d.to_bytes(32, "big")).format()[1:]
                assert derived == f.pubkey_xonly
        elif "n_outputs" in expected:
            assert len(found) == expected["n_outputs"], case["comment"]


def test_address_roundtrip() -> None:
    scan = PublicKey.from_secret((12345).to_bytes(32, "big")).format()
    spend = PublicKey.from_secret((67890).to_bytes(32, "big")).format()
    addr = SilentPaymentAddress(scan_pubkey=scan, spend_pubkey=spend)

    encoded = addr.encode("mainnet")
    assert encoded.startswith("sp1")
    decoded, hrp = SilentPaymentAddress.decode(encoded)
    assert hrp == "sp"
    assert decoded.scan_pubkey == scan
    assert decoded.spend_pubkey == spend

    testnet = addr.encode("signet")
    assert testnet.startswith("tsp1")
    _, hrp = SilentPaymentAddress.decode(testnet)
    assert hrp == "tsp"


def test_invalid_address_checksum() -> None:
    scan = PublicKey.from_secret((1).to_bytes(32, "big")).format()
    spend = PublicKey.from_secret((2).to_bytes(32, "big")).format()
    encoded = SilentPaymentAddress(scan_pubkey=scan, spend_pubkey=spend).encode("mainnet")
    broken = encoded[:-1] + ("q" if encoded[-1] != "q" else "p")
    with pytest.raises(SilentPaymentError):
        SilentPaymentAddress.decode(broken)


def test_is_silent_payment_address() -> None:
    from jmcore.silentpayments import is_silent_payment_address

    scan = PublicKey.from_secret((1).to_bytes(32, "big")).format()
    spend = PublicKey.from_secret((2).to_bytes(32, "big")).format()
    addr = SilentPaymentAddress(scan_pubkey=scan, spend_pubkey=spend)

    assert is_silent_payment_address(addr.encode("mainnet"))
    assert is_silent_payment_address(addr.encode("signet"))
    # Not silent payment addresses.
    assert not is_silent_payment_address("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
    assert not is_silent_payment_address(
        "bc1p5cyxnuxmeuwuvkwfem96lqzszd02n6xdcjrs20cac6yqjjwudpxqkedrcr"
    )
    assert not is_silent_payment_address("not an address")
    assert not is_silent_payment_address("")
    # Malformed sp1 address (bad checksum) is not accepted.
    encoded = addr.encode("mainnet")
    assert not is_silent_payment_address(encoded[:-1] + ("q" if encoded[-1] != "q" else "p"))


def _non_curve_xonly() -> bytes:
    """An x-only key whose x coordinate is not on the secp256k1 curve."""
    from jmcore.silentpayments import _lift_x

    for i in range(2, 1000):
        candidate = i.to_bytes(32, "big")
        try:
            _lift_x(candidate)
        except Exception:  # noqa: BLE001 - any lift failure means off-curve
            return candidate
    raise AssertionError("no off-curve x found")


def test_scan_skips_non_curve_taproot_output() -> None:
    """A non-curve taproot output must not crash labeled scanning (DoS regression).

    An attacker can mine a P2TR output whose x coordinate is not a curve point.
    When the receiver scans with labels, lifting that x-only key used to raise a
    bare ValueError and abort the whole scan, hiding every later payment.
    """
    scan_priv = 0xA1B2C3D4E5F60718293A4B5C6D7E8F90A1B2C3D4E5F60718293A4B5C6D7E8F90
    spend_priv = 0x0F1E2D3C4B5A69788796A5B4C3D2E1F00F1E2D3C4B5A69788796A5B4C3D2E1F0
    spend_pub = PublicKey.from_secret(spend_priv.to_bytes(32, "big")).format()
    scan_pub = PublicKey.from_secret(scan_priv.to_bytes(32, "big")).format()

    m = 7
    labeled = SilentPaymentAddress.decode(
        create_labeled_address(scan_priv, spend_pub, m, "mainnet")
    )[0]

    sender_priv = 0xC0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE01
    sender_pub = PublicKey.from_secret(sender_priv.to_bytes(32, "big")).format()
    pubkey_hash = hashlib.new("ripemd160", hashlib.sha256(sender_pub).digest()).digest()
    vin = SilentPaymentInput(
        txid="33" * 32,
        vout=0,
        scriptpubkey=bytes([0x00, 0x14]) + pubkey_hash,
        witness=[b"\x00" * 71, sender_pub],
        private_key=sender_priv,
    )
    outputs = create_outputs([(sender_priv, False)], [vin.outpoint()], [labeled])
    assert len(outputs) == 1

    labels = {
        PublicKey.from_secret(create_label_tweak(scan_priv, m).to_bytes(32, "big")).format(): (
            create_label_tweak(scan_priv, m)
        )
    }

    # Inject the attacker output before the legitimate one. Without the fix the
    # call raises ValueError; with it the labeled payment is still detected.
    poisoned = [_non_curve_xonly(), outputs[0]]
    found = scan_transaction(scan_priv, spend_pub, [vin], poisoned, labels)
    assert len(found) == 1
    assert found[0].pubkey_xonly == outputs[0]
    assert scan_pub  # silence unused (address derivation parity sanity)
