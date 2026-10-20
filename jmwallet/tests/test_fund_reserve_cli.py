"""`jm-wallet fund-reserve` funds a mixdepth's node from that mixdepth only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from jmcore.bitcoin import scriptpubkey_to_address
from typer.testing import CliRunner

from jmwallet.cli import app

pytest.importorskip("jmswap")

MNEMONIC = "abandon " * 11 + "about"
TAPROOT = scriptpubkey_to_address(bytes([0x51, 0x20]) + b"\x01" * 32, "signet")


def _config(tmp_path: Path, mode: str = "managed") -> None:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    mnemonic_file.write_text(MNEMONIC)
    (tmp_path / "config.toml").write_text(
        '[bitcoin]\nbackend_type = "descriptor_wallet"\n'
        '[network_config]\nnetwork = "signet"\n'
        f'[wallet]\nmnemonic_file = "{mnemonic_file}"\naddress_type = "p2tr"\n'
        f'[lightning]\nmode = "{mode}"\nreserve_min_sats = 25000\nreserve_max_sats = 30000\n'
    )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: tuple[int, str | None] | Exception,
    *args: str,
) -> tuple[int, list[tuple[Any, ...]], list[int]]:
    from jmswap import lightning_reserve

    queried: list[int] = []
    sent: list[tuple[Any, ...]] = []

    async def managed_reserve_target(
        settings: Any, seed: bytes, mixdepth: int
    ) -> tuple[int, str | None]:
        queried.append(mixdepth)
        if isinstance(target, Exception):
            raise target
        return target

    async def send_transaction(*positional: Any, **keywords: Any) -> None:
        sent.append(positional)

    monkeypatch.setattr(lightning_reserve, "managed_reserve_target", managed_reserve_target)
    monkeypatch.setattr("jmwallet.cli.reserve._send_transaction", send_transaction)
    result = CliRunner().invoke(app, ["fund-reserve", *args, "--data-dir", str(tmp_path)])
    return result.exit_code, sent, queried


def test_a_low_reserve_is_funded_from_its_own_mixdepth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path)
    code, sent, queried = _run(tmp_path, monkeypatch, (10_000, TAPROOT), "2", "--yes")
    assert code == 0
    assert queried == [2]
    [(_mnemonic, destination, amount, mixdepth, *rest)] = sent
    # The default reaches the minimum without passing the maximum.
    assert (destination, amount, mixdepth) == (TAPROOT, 20_000, 2)
    broadcast, skip_confirmation, interactive = rest[3:6]
    assert (broadcast, skip_confirmation, interactive) == (True, True, False)


def test_a_funded_reserve_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(tmp_path)
    code, sent, _ = _run(tmp_path, monkeypatch, (25_000, None), "0", "--amount", "5000")
    assert (code, sent) == (0, [])


@pytest.mark.parametrize("amount", ["20001", "0"])
def test_an_amount_outside_the_range_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, amount: str
) -> None:
    _config(tmp_path)
    code, sent, _ = _run(tmp_path, monkeypatch, (10_000, TAPROOT), "0", "--amount", amount)
    assert (code, sent) == (1, [])


def test_an_unreachable_node_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jmswap.lightning_reserve import ReserveError

    _config(tmp_path)
    code, sent, _ = _run(tmp_path, monkeypatch, ReserveError("cannot reach the node"), "0")
    assert (code, sent) == (1, [])


@pytest.mark.parametrize("mode", ["off", "manual"])
def test_only_managed_nodes_are_funded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _config(tmp_path, mode)
    code, sent, queried = _run(tmp_path, monkeypatch, (0, TAPROOT), "0")
    assert (code, sent, queried) == (1, [], [])
