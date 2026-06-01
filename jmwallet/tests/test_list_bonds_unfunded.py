"""``list-bonds`` must surface registered-but-unfunded fidelity bonds.

``list-bonds`` is registry-only (offline); it never scans the blockchain
(use ``recover-bonds`` for that). Bonds created with ``generate-bond-address``
/ ``import-bond`` but not yet funded live in the per-wallet registry and must
be shown with an ``UNFUNDED`` status.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jmwallet.cli.bonds import _list_bonds_offline
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    save_registry,
)

UNFUNDED_ADDRESS = "bcrt1qunfundedbond00000000000000000000000000xyz"
FINGERPRINT = "deadbeef"


def _seed_unfunded_registry(data_dir: Path) -> None:
    registry = BondRegistry()
    registry.add_bond(
        FidelityBondInfo(
            address=UNFUNDED_ADDRESS,
            locktime=1893456000,
            locktime_human="2030-01-01 00:00:00",
            index=0,
            path="m/84'/1'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="regtest",
            created_at="2025-01-01T00:00:00",
        )
    )
    save_registry(registry, data_dir, FINGERPRINT)


def test_list_bonds_offline_shows_unfunded_registered_bond(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_unfunded_registry(tmp_path)

    _list_bonds_offline(data_dir=tmp_path, fingerprint=FINGERPRINT)

    out = capsys.readouterr().out
    assert UNFUNDED_ADDRESS in out
    assert "UNFUNDED" in out
