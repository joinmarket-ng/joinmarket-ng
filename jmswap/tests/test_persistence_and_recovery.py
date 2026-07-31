from __future__ import annotations

import json
import os

from coincurve import PrivateKey

from jmswap.client import SwapClient
from jmswap.interfaces import DirectOnionAdapter
from jmswap.models import FundingOutput
from jmswap.persistence import SwapStore
from jmswap.recovery import validate_and_rebroadcast

from .test_service_and_transactions import FakeChain, make_service


async def test_atomic_permissions_tamper_detection_and_rebroadcast(tmp_path: object) -> None:
    service, lightning, chain = make_service(tmp_path)
    client = SwapClient(
        DirectOnionAdapter(service.dispatch), network="regtest", lightning=lightning
    )
    record = await client.createswap(
        lightning_amount=1_001_000,
        expected_onchain_amount=995_495,
        maximum_miner_fee=1_000,
        current_height=chain.height,
        user_seckey=PrivateKey.from_int(2).secret,
        preimage=b"\xaa" * 32,
    )
    funding = FundingOutput(
        txid="55" * 32,
        vout=0,
        value=record.onchain_amount,
        script_pubkey=record.output_script,
        confirmations=1,
    )
    chain.outputs[(funding.txid, funding.vout)] = funding
    await service.register_funding(record.id, funding.txid, funding.vout)
    await client.claimtaprootswap(
        record,
        funding,
        destination_script=b"\x00\x14" + b"\x24" * 20,
        output_value=funding.value - 500,
    )
    path = service.store.directory / f"{record.id}.json"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(service.store.directory).st_mode & 0o777 == 0o700

    restart_chain = FakeChain()
    report = await validate_and_rebroadcast(service.store, restart_chain)
    assert [item.id for item in report.valid] == [record.id]
    assert len(report.rebroadcast_txids) == 1
    assert len(restart_chain.broadcasts) == 1

    data = json.loads(path.read_text())
    data["output_script"] = "00"
    path.write_text(json.dumps(data))
    tampered = service.store.scan()
    assert tampered.records == []
    assert len(tampered.errors) == 1


def test_store_rejects_path_traversal(tmp_path: object) -> None:
    store = SwapStore(tmp_path)  # type: ignore[arg-type]
    for invalid in ("../secret", "AA" * 32, "00"):
        try:
            store.load(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid swap ID accepted")
