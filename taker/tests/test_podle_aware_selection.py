"""Regression tests for PoDLE-aware automatic taker input selection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import initialize_test_commitment_history, make_taker_config, make_utxo
from jmcore.podle import generate_podle

from taker.taker import Taker


class _EmptyBlacklist:
    def is_blacklisted(self, _commitment: str) -> bool:
        return False


def _wallet(utxos: list) -> AsyncMock:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.wallet_fingerprint = "deadbeef"
    wallet.get_all_utxos = Mock(return_value=utxos)
    wallet.get_locked_input_outpoints = Mock(return_value=set())

    def select_utxos(
        _mixdepth: int,
        _target: int,
        _min_confirmations: int,
        include_utxos: list | None = None,
        **_kwargs: object,
    ) -> list:
        return list(include_utxos) if include_utxos else [utxos[0]]

    wallet.select_utxos = Mock(side_effect=select_utxos)
    return wallet


def _taker(tmp_path: Path, utxos: list) -> Taker:
    initialize_test_commitment_history(tmp_path)
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=False)
    backend.requires_neutrino_metadata = Mock(return_value=False)
    config = make_taker_config(
        data_dir=tmp_path,
        taker_utxo_age=5,
        taker_utxo_amtpercent=20,
        taker_utxo_retries=3,
    )
    return Taker(_wallet(utxos), backend, config)


def _private_key(address: str) -> bytes | None:
    if address == "bcrt1qexhausted":
        return b"\x01" * 32
    if address == "bcrt1qfresh":
        return b"\x02" * 32
    return None


def _exhaust(manager, utxo, private_key: bytes) -> None:  # noqa: ANN001
    outpoint = f"{utxo.txid}:{utxo.vout}"
    manager.used_commitments.update(
        generate_podle(private_key, outpoint, index).commitment.hex() for index in range(3)
    )


def test_fresh_utxo_inspection_does_not_consume_an_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    exhausted = make_utxo(txid_char="a", address="bcrt1qexhausted", value=5_000_000)
    fresh = make_utxo(txid_char="b", address="bcrt1qfresh", value=2_000_000)
    taker = _taker(tmp_path, [exhausted, fresh])
    _exhaust(taker.podle_manager, exhausted, b"\x01" * 32)
    used_before = set(taker.podle_manager.used_commitments)

    candidates = taker.podle_manager.get_fresh_commitment_utxos(
        [exhausted, fresh],
        cj_amount=1_000_000,
        private_key_getter=_private_key,
        min_confirmations=5,
        min_percent=20,
        max_retries=3,
    )

    assert candidates == [fresh]
    assert taker.podle_manager.used_commitments == used_before


def test_automatic_selection_replaces_exhausted_greedy_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    exhausted = make_utxo(txid_char="a", address="bcrt1qexhausted", value=5_000_000)
    fresh = make_utxo(txid_char="b", address="bcrt1qfresh", value=2_000_000)
    taker = _taker(tmp_path, [exhausted, fresh])
    taker._session.cj_amount = 1_000_000
    _exhaust(taker.podle_manager, exhausted, b"\x01" * 32)
    used_before = set(taker.podle_manager.used_commitments)

    selected = taker._select_coinjoin_utxos_with_podle(
        mixdepth=1,
        target_amount=1_001_000,
        private_key_getter=_private_key,
        excluded_outpoints=set(),
    )

    assert selected == [fresh]
    assert taker.wallet.select_utxos.call_count == 2
    assert taker.wallet.select_utxos.call_args.kwargs["include_utxos"] == [fresh]
    assert taker.podle_manager.used_commitments == used_before

    commitment = taker.podle_manager.generate_fresh_commitment(
        selected,
        cj_amount=1_000_000,
        private_key_getter=_private_key,
        min_confirmations=5,
        min_percent=20,
        max_retries=3,
    )
    assert commitment is not None
    assert commitment.utxo == f"{fresh.txid}:{fresh.vout}"
    assert len(taker.podle_manager.used_commitments) == len(used_before) + 1


def test_automatic_selection_keeps_compatible_greedy_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    selected_utxo = make_utxo(txid_char="a", address="bcrt1qexhausted", value=5_000_000)
    other = make_utxo(txid_char="b", address="bcrt1qfresh", value=2_000_000)
    taker = _taker(tmp_path, [selected_utxo, other])
    taker._session.cj_amount = 1_000_000

    selected = taker._select_coinjoin_utxos_with_podle(
        mixdepth=1,
        target_amount=1_001_000,
        private_key_getter=_private_key,
        excluded_outpoints=set(),
    )

    assert selected == [selected_utxo]
    taker.wallet.select_utxos.assert_called_once()


def test_automatic_selection_fails_only_when_pool_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    first = make_utxo(txid_char="a", address="bcrt1qexhausted", value=5_000_000)
    second = make_utxo(txid_char="b", address="bcrt1qfresh", value=2_000_000)
    taker = _taker(tmp_path, [first, second])
    taker._session.cj_amount = 1_000_000
    _exhaust(taker.podle_manager, first, b"\x01" * 32)
    _exhaust(taker.podle_manager, second, b"\x02" * 32)

    with pytest.raises(ValueError, match="No fresh PoDLE commitments remain"):
        taker._select_coinjoin_utxos_with_podle(
            mixdepth=1,
            target_amount=1_001_000,
            private_key_getter=_private_key,
            excluded_outpoints=set(),
        )

    taker.wallet.select_utxos.assert_not_called()
