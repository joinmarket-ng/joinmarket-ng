"""Tests for strict portable external PoDLE records."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from jmcore.bitcoin import hash160
from jmcore.external_podle import ExternalPoDLE
from jmcore.podle import generate_podle


def _record_data(*, index: int = 0) -> dict[str, object]:
    private_key = b"\x11" * 32
    txid = "ab" * 32
    proof = generate_podle(private_key, f"{txid}:1", index)
    return {
        "version": 1,
        "network": "regtest",
        "outpoint": {"txid": txid, "vout": 1},
        "P": proof.p.hex(),
        "P2": proof.p2.hex(),
        "sig": proof.sig.hex(),
        "e": proof.e.hex(),
        "commitment": proof.commitment.hex(),
        "index": index,
        "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
        "blockheight": 100,
    }


def test_external_podle_accepts_canonical_verified_record() -> None:
    record = ExternalPoDLE.model_validate(_record_data(index=7))

    commitment = record.to_podle_commitment()
    assert commitment.utxo == f"{'ab' * 32}:1"
    assert commitment.index == 7
    assert commitment.commitment.hex() == record.commitment


@pytest.mark.parametrize("field", ["P", "P2", "sig", "e", "commitment"])
def test_external_podle_rejects_proof_tampering(field: str) -> None:
    data = _record_data()
    value = data[field]
    assert isinstance(value, str)
    data[field] = ("0" if value[0] != "0" else "1") + value[1:]

    with pytest.raises(ValidationError):
        ExternalPoDLE.model_validate(data)


def test_external_podle_rejects_wrong_singleton_index() -> None:
    data = _record_data(index=0)
    data["index"] = 1

    with pytest.raises(ValidationError, match="invalid PoDLE proof"):
        ExternalPoDLE.model_validate(data)


def test_external_podle_rejects_noncanonical_or_unbound_script() -> None:
    uppercase = _record_data()
    uppercase["commitment"] = str(uppercase["commitment"]).upper()
    with pytest.raises(ValidationError, match="lowercase"):
        ExternalPoDLE.model_validate(uppercase)

    unbound = copy.deepcopy(_record_data())
    unbound["scriptpubkey"] = "0014" + "00" * 20
    with pytest.raises(ValidationError, match="not bound"):
        ExternalPoDLE.model_validate(unbound)
