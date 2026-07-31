from __future__ import annotations

from dataclasses import dataclass

from jmcore.bitcoin import parse_transaction, serialize_transaction

from jmswap.interfaces import ChainBackend
from jmswap.models import SwapRecord
from jmswap.persistence import LoadError, SwapStore
from jmswap.transaction import (
    validate_cooperative_transaction,
    validate_final_cooperative_transaction,
)


@dataclass(frozen=True)
class RecoveryReport:
    valid: list[SwapRecord]
    rebroadcast_txids: list[str]
    errors: list[LoadError]


async def validate_and_rebroadcast(store: SwapStore, chain: ChainBackend) -> RecoveryReport:
    report = store.scan()
    valid: list[SwapRecord] = []
    rebroadcast: list[str] = []
    errors = list(report.errors)
    for record in report.records:
        try:
            if record.cooperative_transaction is not None:
                if record.funding is None:
                    raise ValueError("cooperative transaction has no funding record")
                parsed = parse_transaction(record.cooperative_transaction)
                unsigned = serialize_transaction(
                    parsed.version, parsed.inputs, parsed.outputs, parsed.locktime
                ).hex()
                checked = validate_cooperative_transaction(
                    unsigned, funding=record.funding, network=record.network
                )
                txid = validate_final_cooperative_transaction(
                    record.cooperative_transaction,
                    raw_unsigned=unsigned,
                    contract=record.contract,
                    message=checked.message,
                )
                if txid != record.spending_txid:
                    raise ValueError("persisted cooperative transaction ID mismatch")
                rebroadcast.append(await chain.broadcast(record.cooperative_transaction))
            valid.append(record)
        except Exception as exc:  # noqa: BLE001
            errors.append(LoadError(store.directory / f"{record.id}.json", str(exc)))
    return RecoveryReport(valid, rebroadcast, errors)
