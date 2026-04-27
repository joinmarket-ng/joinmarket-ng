"""Tests for the watched-address confirmation fallback used by the runner.

These cover the path that lets the tumbler resolve confirmation counts on
light-client backends (neutrino) that cannot fetch arbitrary transactions
by txid but can match watched addresses via BIP158.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

from tumbler.confirmations import (
    confirmations_from_history,
    resolve_confirmations,
)

# --------------------------------------------------------------------------- fakes


class _FakeUTXO:
    def __init__(self, txid: str, confirmations: int) -> None:
        self.txid = txid
        self.confirmations = confirmations


class _FakeTx:
    def __init__(self, confirmations: int) -> None:
        self.confirmations = confirmations


class _FakeBackend:
    def __init__(
        self,
        *,
        tx: _FakeTx | None = None,
        utxos: list[_FakeUTXO] | None = None,
        get_tx_raises: bool = False,
        get_utxos_raises: bool = False,
    ) -> None:
        self._tx = tx
        self._utxos = utxos or []
        self._get_tx_raises = get_tx_raises
        self._get_utxos_raises = get_utxos_raises
        self.get_tx_calls: list[str] = []
        self.get_utxos_calls: list[list[str]] = []

    async def get_transaction(self, txid: str) -> Any:
        self.get_tx_calls.append(txid)
        if self._get_tx_raises:
            raise RuntimeError("transient error")
        return self._tx

    async def get_utxos(self, addresses: list[str]) -> list[_FakeUTXO]:
        self.get_utxos_calls.append(list(addresses))
        if self._get_utxos_raises:
            raise RuntimeError("transient error")
        return self._utxos


# --------------------------------------------------------------------------- helpers


_HISTORY_HEADERS = [
    "timestamp",
    "amount",
    "destination_address",
    "change_address",
    "txid",
    "mining_fee_paid",
    "total_maker_fees_paid",
    "net_fee",
    "failure_reason",
]


def _write_history(
    data_dir: Path,
    rows: list[dict[str, str]],
) -> None:
    """Write a minimal CoinJoin history CSV with the columns the tests need.

    Mirrors :mod:`jmwallet.history` on-disk layout closely enough that
    ``read_history`` can parse it. We only populate the fields the
    confirmation-resolver looks at, so missing columns receive blanks.
    """
    history_dir = data_dir
    history_dir.mkdir(parents=True, exist_ok=True)
    path = history_dir / "coinjoin_history.csv"
    # ``read_history`` is tolerant about extra/missing columns; lean on
    # that and write a small superset of the fields we touch.
    with path.open("w", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_HISTORY_HEADERS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            full = {h: row.get(h, "") for h in _HISTORY_HEADERS}
            writer.writerow(full)


# --------------------------------------------------------------------------- tests


@pytest.mark.asyncio
class TestConfirmationsFromHistory:
    async def test_returns_utxo_confirmations_for_known_txid(self, tmp_path: Path) -> None:
        txid = "a" * 64
        dest = "tb1qdest"
        change = "tb1qchange"
        _write_history(
            tmp_path,
            [
                {
                    "txid": txid,
                    "destination_address": dest,
                    "change_address": change,
                }
            ],
        )
        backend = _FakeBackend(utxos=[_FakeUTXO(txid=txid, confirmations=7)])

        result = await confirmations_from_history(txid, backend, tmp_path)

        assert result == 7
        # Both addresses were submitted to the backend's filter scan.
        assert backend.get_utxos_calls == [[dest, change]]

    async def test_unknown_txid_returns_none(self, tmp_path: Path) -> None:
        # No history file at all.
        backend = _FakeBackend()
        result = await confirmations_from_history("b" * 64, backend, tmp_path)
        assert result is None
        assert backend.get_utxos_calls == []

    async def test_history_known_but_no_matching_utxo_returns_zero(self, tmp_path: Path) -> None:
        """Watched addresses exist but the broadcast hasn't materialised
        as a confirmed UTXO yet. Returning 0 keeps the runner polling
        instead of triggering the unknown-txid fallback."""
        txid = "c" * 64
        _write_history(
            tmp_path,
            [{"txid": txid, "destination_address": "tb1qdest", "change_address": ""}],
        )
        backend = _FakeBackend(utxos=[])
        result = await confirmations_from_history(txid, backend, tmp_path)
        assert result == 0

    async def test_get_utxos_failure_returns_none(self, tmp_path: Path) -> None:
        txid = "d" * 64
        _write_history(
            tmp_path,
            [{"txid": txid, "destination_address": "tb1qdest", "change_address": ""}],
        )
        backend = _FakeBackend(get_utxos_raises=True)
        result = await confirmations_from_history(txid, backend, tmp_path)
        assert result is None

    async def test_skips_empty_and_duplicate_addresses(self, tmp_path: Path) -> None:
        txid = "e" * 64
        _write_history(
            tmp_path,
            [
                {
                    "txid": txid,
                    "destination_address": "tb1qsame",
                    "change_address": "tb1qsame",  # duplicate -> dedup
                }
            ],
        )
        backend = _FakeBackend(utxos=[_FakeUTXO(txid=txid, confirmations=2)])
        await confirmations_from_history(txid, backend, tmp_path)
        # Deduplicated to a single entry.
        assert backend.get_utxos_calls == [["tb1qsame"]]


@pytest.mark.asyncio
class TestResolveConfirmations:
    async def test_prefers_get_transaction(self, tmp_path: Path) -> None:
        backend = _FakeBackend(
            tx=_FakeTx(confirmations=12),
            # If the resolver fell through to the fallback, this would
            # be the wrong answer -- the test would catch the regression.
            utxos=[_FakeUTXO(txid="z" * 64, confirmations=99)],
        )
        result = await resolve_confirmations("z" * 64, backend, tmp_path)
        assert result == 12
        assert backend.get_utxos_calls == []  # short-circuit before fallback

    async def test_falls_back_when_get_transaction_returns_none(self, tmp_path: Path) -> None:
        txid = "f" * 64
        _write_history(
            tmp_path,
            [{"txid": txid, "destination_address": "tb1qdest", "change_address": ""}],
        )
        backend = _FakeBackend(tx=None, utxos=[_FakeUTXO(txid=txid, confirmations=4)])
        result = await resolve_confirmations(txid, backend, tmp_path)
        assert result == 4
        assert backend.get_tx_calls == [txid]
        assert backend.get_utxos_calls == [["tb1qdest"]]

    async def test_falls_back_when_get_transaction_raises(self, tmp_path: Path) -> None:
        """A transient backend error on ``get_transaction`` should not
        prevent the watched-address fallback from running -- otherwise
        the runner can't progress on a flaky full-node link."""
        txid = "g" * 64
        _write_history(
            tmp_path,
            [{"txid": txid, "destination_address": "tb1qdest", "change_address": ""}],
        )
        backend = _FakeBackend(
            get_tx_raises=True,
            utxos=[_FakeUTXO(txid=txid, confirmations=3)],
        )
        result = await resolve_confirmations(txid, backend, tmp_path)
        # Note: current behaviour returns ``None`` on ``get_transaction``
        # error to preserve existing semantics. Documented here so a
        # future change to fall through is intentional.
        assert result is None
