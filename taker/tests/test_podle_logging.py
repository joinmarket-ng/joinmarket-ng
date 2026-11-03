"""Privacy tests for PoDLE manager logging."""

from __future__ import annotations

from pathlib import Path

import pytest
from _taker_test_helpers import initialize_test_commitment_history, make_utxo
from loguru import logger

from taker.podle_manager import PoDLEManager


def test_podle_outpoint_logs_are_sensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Outpoint-bearing PoDLE records must not reach ordinary log sinks."""
    initialize_test_commitment_history(tmp_path)
    utxo = make_utxo(txid_char="a", vout=2, value=25_000_000, address="bcrt1qtest1")
    outpoint = f"{utxo.txid}:{utxo.vout}"
    monkeypatch.setattr(
        "taker.podle_manager.generate_podle",
        lambda _private_key, _utxo_str, _index: (_ for _ in ()).throw(
            RuntimeError(f"invalid outpoint {outpoint}")
        ),
    )
    records: list[tuple[str, dict[str, object]]] = []
    handler_id = logger.add(
        lambda message: records.append((message.record["message"], dict(message.record["extra"])))
    )
    try:
        commitment = PoDLEManager(data_dir=tmp_path).generate_fresh_commitment(
            wallet_utxos=[utxo],
            cj_amount=10_000_000,
            private_key_getter=lambda _address: b"\x01" * 32,
            min_confirmations=1,
        )
    finally:
        logger.remove(handler_id)

    assert commitment is None
    outpoint_records = [record for record in records if outpoint in record[0]]
    assert outpoint_records
    assert all(extra.get("sensitive") is True for _, extra in outpoint_records)
    assert ("Failed to generate PoDLE commitment", {}) in records
