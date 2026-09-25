from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from jmcore.fee_quantization import QUANT_ABS, QUANT_REL


COMPOSE_FILE = Path(__file__).resolve().parents[1] / "docker-compose.yml"
E2E_MAKER_SERVICES = ("maker1", "maker2", "maker3", "maker4", "maker5")


def test_e2e_makers_advertise_distinct_public_grid_fees() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    fees: list[Decimal] = []

    for service_name in E2E_MAKER_SERVICES:
        environment = compose["services"][service_name]["environment"]
        values = dict(item.split("=", 1) for item in environment)
        fees.append(Decimal(values["MAKER__CJ_FEE_RELATIVE"]))

    assert len(set(fees)) == len(E2E_MAKER_SERVICES)
    assert all(fee in QUANT_REL for fee in fees)


def test_ring_makers_advertise_stable_public_grid_fees() -> None:
    compose_file = COMPOSE_FILE.parent / "jmswap" / "docker-compose.ring-e2e.yml"
    compose = yaml.safe_load(compose_file.read_text(encoding="utf-8"))

    for service_name in ("ring-maker1", "ring-maker2", "ring-maker3"):
        environment = compose["services"][service_name]["environment"]
        wire_fee = int(environment["MAKER__CJ_FEE_ABSOLUTE"]) + int(
            environment["MAKER__TX_FEE_CONTRIBUTION"]
        )
        assert wire_fee in QUANT_ABS
        assert float(environment["MAKER__TXFEE_CONTRIBUTION_FACTOR"]) == 0
        assert float(environment.get("MAKER__CJFEE_FACTOR", 0)) == 0
