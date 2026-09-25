"""The unbound experimental CLI cannot bypass native wallet readiness."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from jmcore.market_store import MarketStore
from jmcore.paths import get_market_store_path, get_used_commitments_path

import taker.market_cli as market_cli


def test_activated_ledger_blocks_cli_writes_but_allows_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with MarketStore(get_market_store_path(tmp_path), wallet_id="aa" * 32) as store:
        store.activate_wallet(get_used_commitments_path(tmp_path), history_confirmed=True)
    settings = Mock()
    settings.get_data_dir.return_value = tmp_path
    monkeypatch.setattr(market_cli, "_settings", lambda _args: settings)
    credential = tmp_path / "credential.json"
    credential.write_text("{}", encoding="ascii")

    assert market_cli.run(["seller", "add-inventory", "--credential", str(credential)]) == 1
    assert "wallet-bound seller commands are not available" in capsys.readouterr().err
    assert market_cli.run(["seller", "pending"]) == 0
    assert '"quotes":[]' in capsys.readouterr().out
